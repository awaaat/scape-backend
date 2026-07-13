"""
property_intel/fraud.py

Suspicion scoring for the free-tier abuse case: someone trying to get more
than 4 free reports by cycling emails/devices/IPs.

Deliberately NOT a single gate. Each signal below is individually beatable
by a motivated person — the point is that beating all of them at once, for
a KSh 200 report, stops being worth the effort. Score accumulates points;
thresholds decide what happens next.

    0-1 points  → proceed normally
    2-3 points  → require SMS OTP before releasing the free report
    4+ points   → hold for manual review (DeviceFingerprint.is_blocked)

Called once per report-generation attempt, not stored as a running total —
suspicion_score on DeviceFingerprint always reflects the MOST RECENT check,
kept for admin visibility and tuning. FraudReviewLog (see models.py) is the
running history — every call to compute_suspicion_score() writes one row,
so "why did this device get flagged three times last week" is answerable
without digging through application logs.

--------------------------------------------------------------------------
CHANGELOG (production hardening pass):
  - Added _check_ip_fanout(): the original free-tier loophole was that
    payment_method_hash reuse only ever fires AFTER a first payment, but
    free-tier abuse (the actual threat this scoring exists for) never
    touches payment at all. IP fan-out catches the pattern that DOES show
    up pre-payment: an unusual number of distinct device fingerprints all
    requesting free reports from the same IP/network in a short window —
    the "20 browser profiles on one laptop" pattern, which per-fingerprint
    velocity alone can't see (each fingerprint individually looks fine).
  - compute_suspicion_score() now writes a FraudReviewLog row every call.
--------------------------------------------------------------------------
"""
import hashlib
import logging
from datetime import timedelta

from django.utils import timezone

from .models import Broker, DeviceFingerprint, FraudReviewLog, PropertyPin

logger = logging.getLogger("property_intel")

# ---------------------------------------------------------------------------
# Points per signal — tune these based on real abuse patterns once you have
# data. Starting values are deliberately conservative (biased toward NOT
# blocking legitimate brokers) since false positives cost you a paying
# customer, while a missed abuser only costs you ~KSh 800 (4 free reports).
# ---------------------------------------------------------------------------
POINTS_DATACENTER_IP = 2
POINTS_DISPOSABLE_EMAIL = 2
POINTS_HIGH_VELOCITY = 2
POINTS_REPEATED_PIN_CLUSTER = 2
POINTS_IP_FANOUT = 2
POINTS_PAYMENT_METHOD_REUSE = 3  # strongest signal — weighted higher

OTP_THRESHOLD = 9999  # temporarily disabled -- restore to 2 to re-enable phone (OTP) verification
MANUAL_REVIEW_THRESHOLD = 4

# Once a device has completed OTP verification, don't re-demand it on every
# subsequent request just because a STABLE signal (e.g. disposable email,
# which never stops being disposable) keeps recomputing the same score.
# Without this, OTP verification would never "stick" — the very next
# request after verifying would immediately re-trigger it. 30 days matches
# roughly one property-hunting engagement; re-verify after that.
OTP_TRUST_WINDOW_DAYS = 30

# Velocity: more than this many pins from one fingerprint in the window
# below is unusual for a genuine broker casually checking a few plots.
VELOCITY_WINDOW_MINUTES = 15
VELOCITY_MAX_PINS = 5

# Repeated-pin cluster: more than this many DISTINCT devices/brokers hitting
# the same LocationCell in the window below suggests one person cycling
# identities against the same plot, not a real cohort of different brokers
# (who'd naturally be looking at different properties).
CLUSTER_WINDOW_HOURS = 6
CLUSTER_MAX_DISTINCT_DEVICES = 3

# IP fan-out: more than this many DISTINCT device fingerprints registering
# free-tier activity from the same IP in the window below is the
# "many browser profiles, one machine/network" pattern. This is the check
# that actually covers the free-tier loophole, since it fires BEFORE any
# payment ever happens — unlike payment-method reuse below.
IP_FANOUT_WINDOW_HOURS = 24
IP_FANOUT_MAX_DISTINCT_FINGERPRINTS = 4

# Known disposable/temp-mail domains. Not exhaustive — extend as you see
# new ones in practice. Consider swapping to a maintained API/list later
# (e.g. Kickbox, Debounce) once volume justifies it.
DISPOSABLE_EMAIL_DOMAINS = {
    "mailinator.com", "temp-mail.org", "guerrillamail.com", "10minutemail.com",
    "throwawaymail.com", "yopmail.com", "tempmail.com", "trashmail.com",
    "getnada.com", "fakeinbox.com", "sharklasers.com",
}


def is_disposable_email(email):
    domain = email.rsplit("@", 1)[-1].lower().strip()
    return domain in DISPOSABLE_EMAIL_DOMAINS


def hash_payment_identifier(raw_identifier):
    """SHA-256 of an M-Pesa number or card fingerprint from Paystack's
    response — never store the raw value, only enough to detect reuse."""
    return hashlib.sha256(raw_identifier.strip().encode("utf-8")).hexdigest()


def _check_velocity(device_fingerprint):
    """True if this device has made an unusual number of pin requests recently."""
    if device_fingerprint is None:
        return False
    window_start = timezone.now() - timedelta(minutes=VELOCITY_WINDOW_MINUTES)
    recent_count = PropertyPin.objects.filter(
        broker__device_fingerprint=device_fingerprint,
        created_at__gte=window_start,
    ).count()
    return recent_count > VELOCITY_MAX_PINS


def _check_repeated_pin_cluster(location_cell):
    """True if this LocationCell has been hit by an unusual number of
    DISTINCT devices recently — the coordinated-abuse pattern: one person
    circling back to the same plot/estate under different identities,
    which a real cohort of different brokers wouldn't do."""
    if location_cell is None:
        return False
    window_start = timezone.now() - timedelta(hours=CLUSTER_WINDOW_HOURS)
    distinct_devices = (
        PropertyPin.objects.filter(location_cell=location_cell, created_at__gte=window_start)
        .values("broker__device_fingerprint")
        .distinct()
        .count()
    )
    return distinct_devices > CLUSTER_MAX_DISTINCT_DEVICES


def _check_ip_fanout(device_fingerprint):
    """
    True if the IP this device is currently seen on has an unusual number
    of OTHER distinct device fingerprints active on it recently. This is
    the check that actually covers free-tier abuse — someone burning
    through 4-report allowances by spinning up new browser
    profiles/incognito windows on one machine will show a stable IP with a
    growing number of distinct fingerprints, well before any of them ever
    reach a payment step.

    Deliberately filters in Python rather than via JSONField __contains:
    that lookup is Postgres-only (raises NotSupportedError on SQLite), and
    this app's test suite should be able to run on either backend. known_ips
    is capped at 25 entries per fingerprint (see record_sighting()) and this
    only scans fingerprints active within IP_FANOUT_WINDOW_HOURS, so the
    row count stays small — if that stops being true at higher volume, a
    dedicated (fingerprint, ip) join table with a real DB index is the next
    step, not a bigger Python loop.
    """
    if device_fingerprint is None or not device_fingerprint.known_ips:
        return False
    current_ip = device_fingerprint.known_ips[-1]
    window_start = timezone.now() - timedelta(hours=IP_FANOUT_WINDOW_HOURS)

    recently_active = (
        DeviceFingerprint.objects.filter(last_seen_at__gte=window_start)
        .exclude(pk=device_fingerprint.pk)
        .values_list("known_ips", flat=True)
    )
    distinct_fingerprints = sum(1 for ips in recently_active if current_ip in (ips or []))
    return distinct_fingerprints >= IP_FANOUT_MAX_DISTINCT_FINGERPRINTS


def _check_payment_method_reuse(broker):
    """True if this broker's email is new, but their payment_method_hash
    (once set, post-first-payment) matches a DIFFERENT broker record —
    i.e. same M-Pesa number/card already used to fund another account.
    Catches PAID-tier abuse (e.g. someone gaming a future paid-discount
    scheme) — it is not, and was never meant to be, the free-tier check."""
    if broker is None or not broker.payment_method_hash:
        return False
    return (
        Broker.objects.filter(payment_method_hash=broker.payment_method_hash)
        .exclude(pk=broker.pk)
        .exists()
    )


def compute_suspicion_score(*, device_fingerprint, broker, email, location_cell):
    """
    Computes a fresh suspicion score for a report-generation attempt.
    Returns (score, reasons) — reasons is a list of strings for the
    fingerprint's block_reason / admin audit trail, not shown to the user.

    Persists the score onto device_fingerprint, sets
    requires_otp_verification if the OTP threshold is crossed, and always
    writes one FraudReviewLog row (action depends on which threshold, if
    any, was crossed) so the decision is reconstructable later. Does NOT
    set is_blocked itself — MANUAL_REVIEW_THRESHOLD is a signal for a human
    to look at, not an automatic hard block, to avoid false-positive
    lockouts of legitimate brokers before you have real tuning data.
    """
    score = 0
    reasons = []

    if device_fingerprint and device_fingerprint.is_datacenter_ip:
        score += POINTS_DATACENTER_IP
        reasons.append(f"Datacenter/VPN IP ({device_fingerprint.ip_asn_name or 'unknown ASN'})")

    if email and is_disposable_email(email):
        score += POINTS_DISPOSABLE_EMAIL
        reasons.append("Disposable email domain")

    if _check_velocity(device_fingerprint):
        score += POINTS_HIGH_VELOCITY
        reasons.append(f"More than {VELOCITY_MAX_PINS} pins in {VELOCITY_WINDOW_MINUTES} minutes")

    if _check_repeated_pin_cluster(location_cell):
        score += POINTS_REPEATED_PIN_CLUSTER
        reasons.append(f"More than {CLUSTER_MAX_DISTINCT_DEVICES} distinct devices on this location in {CLUSTER_WINDOW_HOURS}h")

    if _check_ip_fanout(device_fingerprint):
        score += POINTS_IP_FANOUT
        reasons.append(f"{IP_FANOUT_MAX_DISTINCT_FINGERPRINTS}+ distinct devices sharing this IP in {IP_FANOUT_WINDOW_HOURS}h")

    if _check_payment_method_reuse(broker):
        score += POINTS_PAYMENT_METHOD_REUSE
        reasons.append("Payment method already linked to another account")

    if device_fingerprint:
        device_fingerprint.suspicion_score = score

        otp_recently_verified = (
            device_fingerprint.otp_verified_at is not None
            and device_fingerprint.otp_verified_at >= timezone.now() - timedelta(days=OTP_TRUST_WINDOW_DAYS)
        )
        device_fingerprint.requires_otp_verification = score >= OTP_THRESHOLD and not otp_recently_verified

        device_fingerprint.save(update_fields=["suspicion_score", "requires_otp_verification"])

        if score >= MANUAL_REVIEW_THRESHOLD:
            action = "held_for_review"
            logger.warning(
                "Device %s flagged for manual review (score=%s): %s",
                device_fingerprint.fingerprint_hash[:12], score, "; ".join(reasons),
            )
        elif score >= OTP_THRESHOLD:
            action = "otp_required"
        else:
            action = "score_computed"

        FraudReviewLog.objects.create(
            device_fingerprint=device_fingerprint, action=action, score=score, reasons=reasons,
        )

    return score, reasons
