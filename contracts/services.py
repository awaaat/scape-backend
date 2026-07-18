"""
contracts/services.py

Orchestration layer, same role as payments/services.py: views stay thin,
this is where the actual logic lives. The only file in this app that
calls into payments.services — everything else stays model-level.
"""
import logging

from django.conf import settings

from payments import services as payment_services
from payments.models import PaystackTransaction

from . import emails
from .models import Contract, ContractRevision, Message, Milestone

logger = logging.getLogger("contracts")

MILESTONE_PAYMENT_PURPOSE = "contract_milestone"


class MilestonePaymentError(Exception):
    """Raised when a milestone payment could not be initiated."""


def _frontend_base():
    return getattr(settings, "FRONTEND_BASE_URL", "").rstrip("/")


def build_client_contract_link(contract, raw_token=None):
    base = _frontend_base()
    if raw_token:
        return f"{base}/contracts/{contract.id}?token={raw_token}"
    return f"{base}/contracts/{contract.id}"


def send_contract_to_client(contract):
    """
    Generates a fresh access token, marks the contract sent, and emails
    the client a link. Called once by admins when a deal is ready to go
    out for review/negotiation — safe to call again later to re-send
    (e.g. token expired) since generate_access_token() always issues a
    fresh one.
    """
    raw_token = contract.generate_access_token()
    contract.mark_sent()
    link = build_client_contract_link(contract, raw_token)
    try:
        emails.send_contract_invitation_email(contract, link)
    except Exception:
        logger.exception("Failed to send contract invitation email for contract %s", contract.id)
    return link


def record_contract_revision(contract, *, edited_by, title=None, scope_of_work=None,
                              total_value=None, currency=None, note=""):
    """
    Applies an edit to the live contract fields AND logs a
    ContractRevision snapshot of what it now says. Called by admins
    during negotiation — every change to negotiable terms goes through
    here, never a bare .save() on Contract, so the audit trail can't
    silently miss an edit.
    """
    if title is not None:
        contract.title = title
    if scope_of_work is not None:
        contract.scope_of_work = scope_of_work
    if total_value is not None:
        contract.total_value = total_value
    if currency is not None:
        contract.currency = currency
    if contract.status == "sent":
        contract.status = "negotiating"
    contract.save()

    next_version = (contract.revisions.order_by("-version_number").values_list(
        "version_number", flat=True
    ).first() or 0) + 1

    return ContractRevision.objects.create(
        contract=contract,
        version_number=next_version,
        title=contract.title,
        scope_of_work=contract.scope_of_work,
        total_value=contract.total_value,
        currency=contract.currency,
        edited_by=edited_by,
        note=note,
    )


def initiate_milestone_payment(milestone: Milestone):
    """
    Starts (or resumes) a Paystack checkout for a milestone. If the
    milestone is already invoiced with a still-usable PaystackTransaction,
    that one's authorization_url is returned instead of starting a
    second, duplicate charge for the same milestone.
    """
    if milestone.status == "paid":
        raise MilestonePaymentError("This milestone is already paid.")

    if milestone.status == "invoiced" and milestone.paystack_reference:
        existing = PaystackTransaction.objects.filter(reference=milestone.paystack_reference).first()
        if existing and existing.status == "pending" and existing.authorization_url:
            return existing

    contract = milestone.contract
    try:
        txn = payment_services.initialize_transaction(
            email=contract.client_email,
            amount=milestone.amount,
            currency=milestone.currency,
            purpose=MILESTONE_PAYMENT_PURPOSE,
            external_reference=str(milestone.id),
            callback_url=f"{_frontend_base()}/contracts/{contract.id}",
            metadata={
                "contract_id": str(contract.id),
                "milestone_id": str(milestone.id),
                "milestone_title": milestone.title,
            },
        )
    except payment_services.PaymentInitializationError as exc:
        raise MilestonePaymentError(str(exc)) from exc

    milestone.mark_invoiced(txn.reference)
    try:
        emails.send_milestone_invoice_email(milestone, txn.authorization_url)
    except Exception:
        logger.exception("Failed to send milestone invoice email for milestone %s", milestone.id)
    return txn


def post_message(*, contract, sender_type, sender_user=None, sender_name="", body):
    """
    THE single place a Message row gets created — called by
    AdminMessageListCreateView, ClientMessageListCreateView, AND
    ContractMessageConsumer (the websocket path). Keeping creation in
    one place means the real-time broadcast (signals.broadcast_new_message,
    a post_save receiver) and the email notification below fire for
    every message no matter which path sent it — including one created
    directly in Django admin, for free.
    """
    message = Message.objects.create(
        contract=contract, sender_type=sender_type, sender_user=sender_user,
        sender_name=sender_name, body=body,
    )
    try:
        emails.send_new_message_notification(message)
    except Exception:
        logger.exception("Failed to send new-message notification for message %s", message.id)
    return message
