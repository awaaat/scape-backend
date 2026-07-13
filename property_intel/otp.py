"""
property_intel/otp.py

SMS OTP escalation, triggered when fraud.compute_suspicion_score() crosses
OTP_THRESHOLD. The premise (see fraud.py) is that a second registered
Safaricom line is a real cost to acquire, unlike a second email address —
so confirming one is a meaningfully harder bar than anything client-side.

Uses Africa's Talking's SMS API (~KSh 1-2/SMS) — cheap enough to trigger
liberally without worrying about cost the way you would with, say, a phone
call or a paid identity-verification vendor.
"""
import hashlib
import hmac
import logging
import random
from datetime import timedelta

import requests
from django.conf import settings
from django.db.models import F
from django.utils import timezone

from .models import DeviceFingerprint, FraudReviewLog, OTPVerification

logger = logging.getLogger("property_intel")

AFRICASTALKING_USERNAME = getattr(settings, "AFRICASTALKING_USERNAME", "")
AFRICASTALKING_API_KEY = getattr(settings, "AFRICASTALKING_API_KEY", "")
AFRICASTALKING_SENDER_ID = getattr(settings, "AFRICASTALKING_SENDER_ID", "")
AFRICASTALKING_SMS_URL = "https://api.africastalking.com/version1/messaging"

OTP_LENGTH = 6
OTP_VALID_MINUTES = 10
REQUEST_TIMEOUT_SECONDS = 10


class OTPError(Exception):
    """User-facing OTP failure — message is safe to show the broker."""


def _generate_code():
    return "".join(random.choices("0123456789", k=OTP_LENGTH))


def _hash_code(code):
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def _constant_time_eq(a, b):
    """Avoids a timing side-channel on the hash comparison — not that a
    6-digit code needs much protecting, but it costs nothing to do right."""
    return hmac.compare_digest(a, b)


def _send_sms(phone_number, message):
    """
    Sends via Africa's Talking. In DEBUG with no credentials configured,
    logs the message instead of sending — lets local/dev testing exercise
    the whole flow without a real SMS account or incurring cost.
    """
    if not AFRICASTALKING_USERNAME or not AFRICASTALKING_API_KEY:
        if settings.DEBUG:
            logger.warning(
                "AFRICASTALKING not configured (DEBUG mode) — logging SMS instead of sending: %s -> %s",
                phone_number, message,
            )
            return True
        raise OTPError("SMS delivery is not configured — please contact support.")

    headers = {
        "apiKey": AFRICASTALKING_API_KEY,
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }
    payload = {"username": AFRICASTALKING_USERNAME, "to": phone_number, "message": message}
    if AFRICASTALKING_SENDER_ID:
        payload["from"] = AFRICASTALKING_SENDER_ID

    try:
        resp = requests.post(AFRICASTALKING_SMS_URL, data=payload, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        logger.error("Africa's Talking SMS send failed for %s: %s", phone_number, exc)
        raise OTPError("Could not send the verification code — please try again.") from exc

    if resp.status_code != 201:
        logger.error(
            "Africa's Talking rejected SMS to %s: HTTP %s — %s",
            phone_number, resp.status_code, resp.text[:300],
        )
        raise OTPError("Could not send the verification code — please try again.")

    return True


def request_otp(device_fingerprint: DeviceFingerprint, phone_number: str) -> OTPVerification:
    """
    Generates and sends a fresh OTP. Invalidates any prior unexpired code
    for this device implicitly — verify_otp() only ever checks the MOST
    RECENT row, so an old code silently stops working the moment a new one
    is requested, with no explicit revocation step needed.
    """
    code = _generate_code()
    otp = OTPVerification.objects.create(
        device_fingerprint=device_fingerprint,
        phone_number=phone_number,
        code_hash=_hash_code(code),
        expires_at=timezone.now() + timedelta(minutes=OTP_VALID_MINUTES),
    )
    _send_sms(
        phone_number,
        f"Your Scape Property Intel verification code is {code}. Valid for {OTP_VALID_MINUTES} minutes.",
    )
    logger.info("OTP requested for device %s -> %s", device_fingerprint.fingerprint_hash[:12], phone_number)
    return otp


def verify_otp(device_fingerprint: DeviceFingerprint, phone_number: str, code: str) -> bool:
    """
    Verifies against the most recent OTPVerification for this device.
    Returns True on success; raises OTPError with a user-facing message
    otherwise. Attempt-limited and expiry-checked BEFORE the hash
    comparison, so a guesser can't brute-force a stale row, and the
    attempt counter is incremented as an atomic DB-level F() update so
    concurrent verify calls can't race past MAX_ATTEMPTS.
    """
    otp = (
        OTPVerification.objects.filter(device_fingerprint=device_fingerprint, phone_number=phone_number)
        .order_by("-created_at")
        .first()
    )
    if otp is None:
        raise OTPError("No verification code was requested for this number.")

    if otp.verified_at:
        return True  # already verified — idempotent on a duplicate client retry

    if otp.is_expired:
        raise OTPError("This code has expired — please request a new one.")

    if otp.is_exhausted:
        raise OTPError("Too many incorrect attempts — please request a new code.")

    OTPVerification.objects.filter(pk=otp.pk).update(attempts=F("attempts") + 1)

    if not _constant_time_eq(otp.code_hash, _hash_code(code)):
        raise OTPError("Incorrect code — please try again.")

    OTPVerification.objects.filter(pk=otp.pk).update(verified_at=timezone.now())

    DeviceFingerprint.objects.filter(pk=device_fingerprint.pk).update(
        requires_otp_verification=False,
        otp_verified_phone=phone_number,
        otp_verified_at=timezone.now(),
    )
    FraudReviewLog.objects.create(
        device_fingerprint=device_fingerprint,
        action="otp_verified",
        score=device_fingerprint.suspicion_score,
        reasons=[f"Phone {phone_number} verified via OTP"],
    )
    logger.info("OTP verified for device %s", device_fingerprint.fingerprint_hash[:12])
    return True
