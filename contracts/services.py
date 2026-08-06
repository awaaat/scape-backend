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
from .models import (
    Contract, ContractChangeRequest, ContractDispute, ContractRevision, Message, Milestone,
)

logger = logging.getLogger("contracts")

MILESTONE_PAYMENT_PURPOSE = "contract_milestone"


class MilestonePaymentError(Exception):
    """Raised when a milestone payment could not be initiated."""


class ChangeRequestError(Exception):
    """Raised when a change request can't be filed or decided."""


class DisputeError(Exception):
    """Raised when a dispute can't be filed or resolved."""


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
    during negotiation, AND by decide_change_request() below when a
    client's proposed change is approved — every change to negotiable
    terms goes through here, never a bare .save() on Contract, so the
    audit trail can't silently miss an edit.
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

    Refuses outright — for BOTH the admin-invoice and client-pay call
    sites, since they both route through here — if the contract has an
    open dispute (see Contract.has_open_dispute). This is the actual
    "billing freeze" behavior: one choke point, not a check duplicated
    in every view that could move money.
    """
    contract = milestone.contract
    if contract.has_open_dispute:
        raise MilestonePaymentError(
            "This contract has an open dispute — billing is frozen until it's resolved."
        )

    if milestone.status == "paid":
        raise MilestonePaymentError("This milestone is already paid.")

    if milestone.status == "invoiced" and milestone.paystack_reference:
        existing = PaystackTransaction.objects.filter(reference=milestone.paystack_reference).first()
        if existing and existing.status == "pending" and existing.authorization_url:
            return existing

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


# ── Change requests ────────────────────────────────────────────────────

def request_contract_change(contract, *, requested_by_user=None, requested_by_name="", reason,
                             proposed_title=None, proposed_scope_of_work=None,
                             proposed_total_value=None, proposed_currency=None):
    """
    THE only way a ContractChangeRequest gets created. Refuses if the
    contract is past the negotiable stage (see
    Contract.is_change_request_eligible), or if there's already a
    pending request — a client can't queue up five competing proposals
    before an admin has looked at the first one; withdraw or wait.
    """
    if not contract.is_change_request_eligible():
        raise ChangeRequestError(
            f"This contract is '{contract.status}' — changes can no longer be requested this way."
        )

    if contract.change_requests.filter(status="pending").exists():
        raise ChangeRequestError("There's already a pending change request on this contract.")

    change_request = ContractChangeRequest.objects.create(
        contract=contract,
        requested_by_user=requested_by_user,
        requested_by_name=requested_by_name,
        proposed_title=proposed_title or "",
        proposed_scope_of_work=proposed_scope_of_work or "",
        proposed_total_value=proposed_total_value,
        proposed_currency=proposed_currency or "",
        reason=reason,
    )
    try:
        emails.send_change_request_submitted_email(change_request)
    except Exception:
        logger.exception("Failed to send change-request-submitted email for request %s", change_request.id)
    return change_request


def decide_change_request(change_request: ContractChangeRequest, *, approved, reviewed_by, admin_note=""):
    """
    THE only way a ContractChangeRequest moves out of "pending".
    Approving applies the proposed fields via record_contract_revision()
    — the exact same function an admin-initiated edit uses — so an
    approved client change is indistinguishable in the ContractRevision
    audit trail from an admin's own edit, aside from who's on the note.
    """
    if change_request.status != "pending":
        raise ChangeRequestError(f"This change request is already '{change_request.status}'.")

    change_request.reviewed_by = reviewed_by
    change_request.admin_note = admin_note

    from django.utils import timezone
    change_request.reviewed_at = timezone.now()

    if approved:
        record_contract_revision(
            change_request.contract,
            edited_by=reviewed_by,
            title=change_request.proposed_title or None,
            scope_of_work=change_request.proposed_scope_of_work or None,
            total_value=change_request.proposed_total_value,
            currency=change_request.proposed_currency or None,
            note=f"Applied client change request #{change_request.id}. {admin_note}".strip(),
        )
        change_request.status = "approved"
    else:
        change_request.status = "rejected"

    change_request.save(update_fields=["status", "admin_note", "reviewed_by", "reviewed_at"])

    try:
        emails.send_change_request_decided_email(change_request)
    except Exception:
        logger.exception("Failed to send change-request-decided email for request %s", change_request.id)
    return change_request


# ── Disputes ─────────────────────────────────────────────────────────

def raise_dispute(contract, *, category, description, raised_by_user=None, raised_by_name=""):
    """
    THE only way a ContractDispute gets created. Refuses if the
    contract already has an open dispute — one active dispute per
    contract at a time; additional concerns go in the resolution note
    or the message thread, not a second parallel dispute.
    """
    if contract.has_open_dispute:
        raise DisputeError("There's already an open dispute on this contract.")

    dispute = ContractDispute.objects.create(
        contract=contract, category=category, description=description,
        raised_by_user=raised_by_user, raised_by_name=raised_by_name,
    )
    try:
        emails.send_dispute_raised_email(dispute)
    except Exception:
        logger.exception("Failed to send dispute-raised email for dispute %s", dispute.id)
    return dispute


def resolve_dispute(dispute: ContractDispute, *, new_status, resolved_by, resolution_note=""):
    """
    THE only way a ContractDispute's status changes after filing.
    Moving to "under_review" logs progress without lifting the billing
    freeze; moving to any resolved_*/withdrawn status lifts it
    immediately, since Contract.has_open_dispute re-evaluates live on
    every call to initiate_milestone_payment().
    """
    if not dispute.is_open():
        raise DisputeError(f"This dispute is already '{dispute.status}'.")

    from django.utils import timezone

    dispute.status = new_status
    dispute.resolution_note = resolution_note
    if new_status != "under_review":
        dispute.resolved_by = resolved_by
        dispute.resolved_at = timezone.now()
    dispute.save(update_fields=["status", "resolution_note", "resolved_by", "resolved_at"])

    if new_status != "under_review":
        try:
            emails.send_dispute_resolved_email(dispute)
        except Exception:
            logger.exception("Failed to send dispute-resolved email for dispute %s", dispute.id)
    return dispute
