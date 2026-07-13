"""
payments/services.py

Orchestration layer: creates/updates PaystackTransaction rows and fires
payment_succeeded. This is the ONLY file in this app that touches both
paystack.py (the HTTP client) and models.py (persistence) — views.py stays
thin and just calls into here.

Still zero knowledge of property_intel or any other caller. Everything
caller-specific arrives as opaque strings (purpose, external_reference,
metadata) and is never interpreted.
"""
import logging
import secrets
from decimal import Decimal, ROUND_HALF_UP

from django.utils import timezone

from . import emails, paystack
from .models import PaystackTransaction, PaystackWebhookEvent
from .signals import payment_succeeded, wallet_topup_succeeded

logger = logging.getLogger("payments")


class PaymentInitializationError(Exception):
    """Raised when a transaction could not be started — bad input or Paystack itself failing."""


def generate_reference():
    """
    Our own reference, generated BEFORE calling Paystack, so a
    PaystackTransaction row exists (status='pending') even if the network
    call to Paystack fails outright — the alternative (creating the row
    only after a successful Initialize) would mean a failed Initialize
    leaves no trace anywhere.
    """
    return f"SCP-{secrets.token_hex(10).upper()}"


def initialize_transaction(*, email, amount, purpose, external_reference,
                            currency="KES", callback_url="", metadata=None):
    """
    Starts a Paystack checkout. Returns the created PaystackTransaction.

    `amount` is Decimal/float/str in the MAJOR currency unit (e.g. "500.00"
    KES, not cents) — converted to the lowest denomination for Paystack
    internally.
    """
    amount = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if amount <= 0:
        raise PaymentInitializationError("Amount must be greater than zero.")

    reference = generate_reference()
    txn = PaystackTransaction.objects.create(
        reference=reference,
        purpose=purpose,
        external_reference=str(external_reference),
        email=email,
        amount=amount,
        currency=currency,
        callback_url=callback_url or "",
        metadata=metadata or {},
        status="pending",
    )

    try:
        data = paystack.initialize_transaction(
            email=email,
            amount_subunit=txn.amount_subunit,
            currency=currency,
            reference=reference,
            callback_url=callback_url or None,
            metadata=metadata or None,
        )
    except paystack.PaystackAPIError as exc:
        # The row stays — 'pending' with no authorization_url is itself
        # meaningful (an init that never even got a checkout link),
        # distinguishable in admin from one that got a link but was
        # abandoned before paying.
        logger.error("initialize_transaction: Paystack call failed for %s: %s", reference, exc)
        raise PaymentInitializationError(str(exc)) from exc

    PaystackTransaction.objects.filter(pk=txn.pk).update(
        authorization_url=data.get("authorization_url", ""),
        access_code=data.get("access_code", ""),
    )
    txn.refresh_from_db(fields=["authorization_url", "access_code"])
    return txn


def confirm_transaction(reference, *, paystack_transaction_id="", channel="", paid_at=None, authorization_details=None):
    """
    The single choke point where a transaction is marked paid and
    payment_succeeded is sent. Called from both the webhook handler and
    the manual /verify/ endpoint, so it has to be safe to call twice for
    the same reference (webhook AND a verify racing each other, or a
    retried webhook delivery).

    The `.filter(status="pending").update(...)` below is the guard: only
    the call that actually flips pending->success gets to send the
    signal. A second call for an already-success transaction is a no-op,
    not a duplicate signal send.

    Returns the PaystackTransaction, or None if it doesn't exist.
    """
    try:
        txn = PaystackTransaction.objects.get(reference=reference)
    except PaystackTransaction.DoesNotExist:
        logger.warning("confirm_transaction: unknown reference %s", reference)
        return None

    if txn.status == "success":
        return txn  # already processed — not an error, just nothing to do

    paid_at = paid_at or timezone.now()
    updated = PaystackTransaction.objects.filter(pk=txn.pk, status="pending").update(
        status="success",
        paystack_transaction_id=paystack_transaction_id or txn.paystack_transaction_id,
        channel=channel or txn.channel,
        paid_at=paid_at,
        authorization_details=authorization_details or txn.authorization_details,
    )
    txn.refresh_from_db()

    if not updated:
        # Lost a race to another call (or the transaction was already
        # failed/abandoned) — don't send the signal twice.
        return txn

    logger.info("Transaction %s confirmed paid (%s %s, purpose=%s)", reference, txn.amount, txn.currency, txn.purpose)

    # send_robust(), not send(): a receiver in property_intel (or whatever
    # feature app listens next) throwing must never propagate back up
    # through confirm_transaction() — that would turn a downstream bug
    # into a failed webhook response, which makes Paystack retry a
    # transaction that's already correctly marked paid on our side. Errors
    # are still visible — send_robust returns them per-receiver — just
    # logged here instead of raised.
    results = payment_succeeded.send_robust(
        sender=PaystackTransaction,
        reference=txn.reference,
        purpose=txn.purpose,
        external_reference=txn.external_reference,
        amount=txn.amount,
        currency=txn.currency,
        email=txn.email,
        channel=txn.channel,
        paid_at=txn.paid_at,
        metadata=txn.metadata,
        authorization_details=txn.authorization_details,
    )
    for receiver_fn, response in results:
        if isinstance(response, Exception):
            logger.error(
                "payment_succeeded receiver %r raised for transaction %s: %s",
                receiver_fn, reference, response,
            )
    try:
        emails.send_payment_receipt_email(txn)
    except Exception:
        logger.exception("Failed to send payment receipt email for %s", reference)
    return txn


def mark_transaction_failed(reference, reason=""):
    updated = PaystackTransaction.objects.filter(reference=reference, status="pending").update(
        status="failed", failure_reason=reason[:2000],
    )
    if not updated:
        return None
    try:
        txn = PaystackTransaction.objects.get(reference=reference)
    except PaystackTransaction.DoesNotExist:
        return None
    try:
        emails.send_payment_failed_email(txn)
    except Exception:
        logger.exception("Failed to send payment-failed email for %s", reference)
    return txn


def verify_and_confirm(reference):
    """
    Calls Paystack's own Verify endpoint (the source of truth) and, if it
    says the charge succeeded, runs it through confirm_transaction(). This
    is the fallback path for when a webhook is delayed or never arrives —
    the frontend can poll this after redirect-back from Paystack instead
    of trusting client-side state alone.
    """
    try:
        txn = PaystackTransaction.objects.get(reference=reference)
    except PaystackTransaction.DoesNotExist:
        return None

    if txn.status == "success":
        return txn

    data = paystack.verify_transaction(reference)
    paystack_status = data.get("status")  # Paystack's per-transaction status string, e.g. "success"/"failed"/"abandoned"

    if paystack_status == "success":
        return confirm_transaction(
            reference,
            paystack_transaction_id=str(data.get("id", "")),
            channel=data.get("channel", ""),
            authorization_details=data.get("authorization", {}) or {},
        )

    if paystack_status in ("failed", "abandoned"):
        PaystackTransaction.objects.filter(pk=txn.pk, status="pending").update(
            status=paystack_status, failure_reason=data.get("gateway_response", "")[:2000],
        )
        txn.refresh_from_db()

    return txn


def record_webhook_event(event_type, reference, raw_payload, signature_valid):
    """
    Logs the delivery unconditionally, BEFORE any decision about whether
    to act on it. get_or_create on the (event_type, reference) unique
    constraint means a retried delivery returns the existing row instead
    of raising an IntegrityError — the caller checks `created` to decide
    whether to actually process it.
    """
    return PaystackWebhookEvent.objects.get_or_create(
        event_type=event_type,
        reference=reference,
        defaults={"raw_payload": raw_payload, "signature_valid": signature_valid},
    )


def paid_via_reference(reference):
    """
    Lookup helper for callers that just want a yes/no answer — e.g. a
    report-status endpoint that wants to say "still awaiting payment" vs
    "paid, generating now" without pulling in the whole transaction row.
    """
    return PaystackTransaction.objects.filter(reference=reference, status="success").exists()
