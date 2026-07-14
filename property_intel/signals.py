"""
property_intel/signals.py
Listens for payments.signals.payment_succeeded. This is where "purpose"
gets meaning — payments itself has none.
"""
import hashlib
import logging

from django.db import transaction as db_transaction
from django.dispatch import receiver

from payments.signals import payment_succeeded

from .models import Broker, DeviceFingerprint, FraudReviewLog, PropertyReport
from .tasks import generate_report_task

logger = logging.getLogger("property_intel")

REPORT_PAYMENT_PURPOSE = "property_report"


def _compute_payment_method_hash(authorization_details):
    """
    Stable fingerprint of the actual payment instrument, channel-aware.
    Returns "" when nothing usable is present — treat as "no signal",
    not as a hash collision with other empty results.
    """
    if not authorization_details:
        return ""

    channel = (authorization_details.get("channel") or "").lower()

    if channel == "card":
        bin_ = authorization_details.get("bin") or ""
        last4 = authorization_details.get("last4") or ""
        if not (bin_ and last4):
            return ""
        raw = f"card:{bin_}:{last4}"
    else:
        candidate = (
            authorization_details.get("mobile_money_number")
            or authorization_details.get("phone")
            or authorization_details.get("account_number")
            or ""
        )
        if not candidate:
            return ""
        raw = f"{channel or 'mobile_money'}:{candidate}"

    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _flag_shared_payment_method(broker, payment_method_hash):
    """
    Anti-abuse: if this exact payment instrument already paid from a
    DIFFERENT device, revoke the current device's free-tier allowance.
    A real phone number/card is costly to acquire, unlike a new email or
    a cleared-cookies browser — so a repeat hash is a strong signal.
    """
    if not payment_method_hash:
        return

    other_brokers = Broker.objects.filter(
        payment_method_hash=payment_method_hash
    ).exclude(pk=broker.pk).select_related("device_fingerprint")

    for other in other_brokers:
        fingerprint = other.device_fingerprint
        current_fp = broker.device_fingerprint
        if not fingerprint or not current_fp or fingerprint.pk == current_fp.pk:
            continue

        if current_fp.free_reports_remaining > 0:
            DeviceFingerprint.objects.filter(pk=current_fp.pk).update(free_reports_remaining=0)
            FraudReviewLog.objects.create(
                device_fingerprint=current_fp,
                action="score_computed",
                reasons=[
                    f"Same payment method previously used by broker {other.email} "
                    f"(device {fingerprint.fingerprint_hash[:12]}…) — free tier revoked."
                ],
            )
            logger.warning(
                "Free-tier revoked for device %s — shared payment_method_hash with broker %s",
                current_fp.fingerprint_hash[:12], other.email,
            )
        break


@receiver(payment_succeeded)
def handle_property_report_payment(sender, reference, purpose, external_reference,
                                    amount, currency, email, channel, paid_at,
                                    metadata, authorization_details=None, **kwargs):
    if purpose != REPORT_PAYMENT_PURPOSE:
        return

    try:
        report = PropertyReport.objects.select_related("pin__broker__device_fingerprint").get(
            pk=external_reference
        )
    except (PropertyReport.DoesNotExist, ValueError):
        logger.error(
            "payment_succeeded for purpose=%s reference=%s but no matching PropertyReport (external_reference=%r)",
            purpose, reference, external_reference,
        )
        return

    if report.is_paid:
        return  # webhook + manual verify both landing here — must be a no-op the 2nd time

    with db_transaction.atomic():
        report.is_paid = True
        report.paid_at = paid_at
        report.status = "pending"
        report.paystack_reference = reference
        report.save(update_fields=["is_paid", "paid_at", "status", "paystack_reference"])

        broker = report.pin.broker
        payment_method_hash = _compute_payment_method_hash(authorization_details or {})
        if payment_method_hash and not broker.payment_method_hash:
            broker.payment_method_hash = payment_method_hash
            broker.save(update_fields=["payment_method_hash"])

        if payment_method_hash:
            _flag_shared_payment_method(broker, payment_method_hash)

    # If a partial wallet balance was earmarked at checkout (see
    # PinSubmitView / OTPVerifyView in property_intel/views.py --
    # report.wallet_applied_kes), debit exactly that amount now that
    # payment is actually confirmed -- never before, so an abandoned
    # Paystack checkout never strands the balance. `amount` here is only
    # the REMAINDER Paystack charged after that balance was applied, not
    # the full report price -- it must never be debited from the wallet
    # again on top of the earmarked amount.
    if report.wallet_applied_kes and report.pin.broker.user_id:
        try:
            from payments.models import UserWallet, WalletTransaction
            wallet = UserWallet.get_or_create_for_user(report.pin.broker.user)
            debited = wallet.debit(report.wallet_applied_kes)
            if debited:
                WalletTransaction.objects.create(
                    wallet=wallet,
                    transaction_type="report_debit",
                    amount=report.wallet_applied_kes,
                    balance_after=wallet.balance,
                    reference=reference,
                    note=f"Partial balance applied toward report {report.id} (remainder paid via Paystack)",
                )
            else:
                logger.warning(
                    "Report %s: expected to debit KES %s of earmarked wallet balance but it had "
                    "changed since checkout -- Paystack already covered the remainder in full, so "
                    "the report itself is unaffected.",
                    report.id, report.wallet_applied_kes,
                )
        except Exception as exc:
            logger.warning("Report %s: wallet debit failed (non-fatal): %s", report.id, exc)

    generate_report_task.delay(str(report.id))
    logger.info("Report %s marked paid via %s, generation dispatched.", report.id, reference)
