"""
Orchestration layer, same role as payments/services.py: views stay thin,
this is where the actual logic lives. The only file in this app that
calls into payments.services — everything else stays model-level.

── Milestone escrow workflow (added) ──────────────────────────────────
Every function below that moves a Milestone through its lifecycle
(submit_milestone_deliverable, request_milestone_revision,
approve_milestone_release, auto_release_milestone) follows the same
shape: call the guarded model method to actually change status, then
write exactly one MilestoneEvent row via _log_milestone_event(), then
best-effort email — never the other order, and never a bare status
mutation from a view. That's what makes MilestoneEvent trustworthy as
a complete history later.

initiate_milestone_payment() now checks TWO freezes before starting or
resuming a checkout, both re-evaluated live on every call:
  - contract.has_open_dispute        (contract-wide dispute freeze)
  - contract.has_unsigned_required_agreements  (NDA/MSA gate, added)
Both existed as the exact same kind of single-choke-point check before
this file's own docstring described has_open_dispute; this just adds
the second gate next to it rather than introducing a new pattern.
"""
import logging
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction as db_transaction
from django.utils import timezone

from payments import services as payment_services
from payments.models import PaystackTransaction

from . import emails, invoice_pdf, storage
from .models import (
    Contract, ContractChangeRequest, ContractDispute, ContractRevision, Invoice, InvoiceCounter,
    Message, Milestone, MilestoneDeliverable, MilestoneEvent,
)

logger = logging.getLogger("contracts")

MILESTONE_PAYMENT_PURPOSE = "contract_milestone"

# Default mapping from a resolved ContractDispute status to the
# Milestone status its scoped milestone moves into. Callers of
# resolve_dispute() can override per-dispute via
# milestone_resolution_status — this is just the sensible default so
# an admin resolving a routine dispute doesn't have to specify it.
DISPUTE_TO_MILESTONE_STATUS = {
    "resolved_provider_favor": "released",
    "resolved_client_favor": "cancelled",
    "resolved_mutual": "submitted",
    "withdrawn": "submitted",
}


class MilestonePaymentError(Exception):
    """Raised when a milestone payment could not be initiated."""


class MilestoneWorkflowError(Exception):
    """Raised when a submit/revision/release action isn't valid for a
    milestone's current state (wrong status, open dispute, etc.)."""


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
    sites, since they both route through here — if:
      - the contract has an open CONTRACT-WIDE dispute (a milestone-
        scoped dispute on a DIFFERENT milestone does not block this one), or
      - the contract still has required agreements (NDA/MSA/etc.) that
        aren't fully executed yet.
    Both are the actual freeze behaviors: one choke point each, not a
    check duplicated in every view that could move money.
    """
    contract = milestone.contract

    if contract.has_open_dispute:
        raise MilestonePaymentError(
            "This contract has an open dispute — billing is frozen until it's resolved."
        )

    if contract.has_unsigned_required_agreements:
        missing = ", ".join(contract.unsigned_required_agreement_types)
        raise MilestonePaymentError(
            f"This contract requires a fully executed agreement ({missing}) before any "
            f"milestone can be invoiced."
        )

    if milestone.status not in ("pending", "invoiced"):
        raise MilestonePaymentError(
            f"This milestone is '{milestone.status}' and can no longer be invoiced."
        )

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

    if milestone.status == "pending":
        milestone.mark_invoiced(txn.reference)
    else:
        # Already 'invoiced' but the previous checkout wasn't usable
        # (expired/abandoned) — refresh the reference without moving
        # status, since it's already where it needs to be.
        milestone.paystack_reference = txn.reference
        milestone.invoiced_at = timezone.now()
        milestone.save(update_fields=["paystack_reference", "invoiced_at"])

    _log_milestone_event(milestone, "invoiced", actor_type="system")
    try:
        emails.send_milestone_invoice_email(milestone, txn.authorization_url)
    except Exception:
        logger.exception("Failed to send milestone invoice email for milestone %s", milestone.id)
    return txn


# ── Invoicing (bundled milestone billing) ────────────────────────────────
INVOICE_PAYMENT_PURPOSE = "contract_invoice"
DEFAULT_INVOICE_DUE_DAYS = getattr(settings, "INVOICE_DEFAULT_DUE_DAYS", 14)
INVOICE_NUMBER_PREFIX = getattr(settings, "INVOICE_NUMBER_PREFIX", "SCP")


class InvoiceError(Exception):
    """Raised when an Invoice can't be created, sent, voided, or settled
    in its current state."""


def _next_invoice_number():
    """Race-safe sequential numbering via InvoiceCounter. Must be called
    from inside the caller's own db_transaction.atomic() block —
    select_for_update() only holds the row lock for the life of that
    transaction, which is what makes two admins invoicing at the same
    instant get distinct, gapless numbers instead of a duplicate."""
    year = timezone.now().year
    counter, _ = InvoiceCounter.objects.select_for_update().get_or_create(year=year)
    counter.last_number += 1
    counter.save(update_fields=["last_number"])
    return f"{INVOICE_NUMBER_PREFIX}-{year}-{counter.last_number:06d}"


def create_invoice(milestone: Milestone, *, due_in_days=None, notes="", created_by=None):
    """
    THE only way an Invoice gets created. Billing milestone N always
    bundles in every earlier 'pending' milestone on the same contract
    (see Contract.unbilled_milestones_up_to) — an invoice is never
    created that silently depends on an earlier one going out later, so
    "client skips 1 and 2, pays all three at once" is impossible to
    even construct: invoicing 3 always also pulls in 1 and 2.

    Refuses if:
      - the contract has a live billing freeze (open dispute / unsigned
        required agreements) — the exact same two checks
        initiate_milestone_payment() already uses, so this path can't
        be used to route around the freeze;
      - the milestone itself isn't 'pending';
      - the contract already has a non-void invoice outstanding (draft
        or sent) — one at a time, so the ledger is never ambiguous
        about which invoice covers which milestones.
    """
    contract = milestone.contract

    if contract.has_open_dispute:
        raise InvoiceError("This contract has an open dispute — billing is frozen until it's resolved.")
    if contract.has_unsigned_required_agreements:
        missing = ", ".join(contract.unsigned_required_agreement_types)
        raise InvoiceError(
            f"This contract requires a fully executed agreement ({missing}) before any milestone can be invoiced."
        )
    if milestone.status != "pending":
        raise InvoiceError(f"This milestone is '{milestone.status}' and can no longer be invoiced.")
    if contract.invoices.filter(status__in=["draft", "sent"]).exists():
        raise InvoiceError(
            "This contract already has an outstanding invoice — send or void it before creating another."
        )

    with db_transaction.atomic():
        milestones = list(contract.unbilled_milestones_up_to(milestone.order).select_for_update())
        if not milestones:
            raise InvoiceError("Nothing unbilled to invoice for this milestone.")

        currencies = {m.currency for m in milestones}
        if len(currencies) > 1:
            raise InvoiceError(
                f"Cannot bundle milestones with different currencies ({', '.join(sorted(currencies))}) into one invoice."
            )

        total_amount = sum((m.amount for m in milestones), Decimal("0"))
        due_at = timezone.now() + timedelta(
            days=due_in_days if due_in_days is not None else DEFAULT_INVOICE_DUE_DAYS
        )

        invoice = Invoice.objects.create(
            contract=contract,
            invoice_number=_next_invoice_number(),
            total_amount=total_amount,
            currency=currencies.pop(),
            due_at=due_at,
            notes=notes,
            created_by=created_by,
        )

        for m in milestones:
            m.attach_to_invoice(invoice)

    logger.info(
        "Invoice %s created for contract %s covering %s milestone(s), total %s %s",
        invoice.invoice_number, contract.id, len(milestones), invoice.total_amount, invoice.currency,
    )
    return invoice


def send_invoice(invoice: Invoice, *, sent_by=None):
    """
    Renders the PDF, uploads it, and emails the client with it attached.
    Safe to call again for a resend — re-renders and re-uploads
    (upload_invoice_pdf upserts in place) but never changes
    total_amount or invoice_number, which are frozen at create_invoice()
    time.
    """
    if invoice.status not in ("draft", "sent"):
        raise InvoiceError(f"This invoice is '{invoice.status}' and can no longer be sent.")

    try:
        pdf_bytes = invoice_pdf.render_invoice_pdf(invoice)
    except invoice_pdf.InvoiceRenderError as exc:
        raise InvoiceError(f"Failed to render invoice PDF: {exc}") from exc

    try:
        pdf_url = storage.upload_invoice_pdf(pdf_bytes, invoice.id)
    except storage.StorageUploadFailed as exc:
        raise InvoiceError(f"Failed to store invoice PDF: {exc}") from exc

    was_first_send = invoice.status == "draft"
    invoice.pdf_url = pdf_url
    invoice.status = "sent"
    if was_first_send:
        invoice.sent_at = timezone.now()
    invoice.sent_via_email = True
    invoice.save(update_fields=["pdf_url", "status", "sent_at", "sent_via_email"])

    payment_url = build_client_contract_link(invoice.contract)
    try:
        emails.send_invoice_sent_email(invoice, payment_url, pdf_bytes)
    except Exception:
        logger.exception("Failed to send invoice-sent email for invoice %s", invoice.id)

    logger.info("Invoice %s sent for contract %s", invoice.invoice_number, invoice.contract_id)
    return invoice


def void_invoice(invoice: Invoice, *, void_reason="", voided_by=None):
    """
    THE only way an Invoice is withdrawn after creation. Only legal from
    draft/sent — a paid invoice is a settled financial record and is
    never voided here (a refund is a separate process this system
    doesn't model). Frees every attached milestone back to 'pending' via
    Milestone.revert_to_pending() so they can be rolled into a future
    invoice instead of being stranded.
    """
    if invoice.status not in ("draft", "sent"):
        raise InvoiceError(f"This invoice is '{invoice.status}' and cannot be voided.")

    with db_transaction.atomic():
        for m in invoice.milestones.select_for_update():
            m.revert_to_pending()

        invoice.status = "void"
        invoice.void_reason = void_reason
        invoice.save(update_fields=["status", "void_reason"])

    logger.info("Invoice %s voided for contract %s (by %s)", invoice.invoice_number, invoice.contract_id, voided_by)
    return invoice


def initiate_invoice_payment(invoice: Invoice):
    """
    Starts (or resumes) a Paystack checkout for the WHOLE invoice —
    mirrors initiate_milestone_payment()'s shape, but the unit being
    paid is an Invoice, not a single Milestone. The same live freezes
    apply, since a sent invoice can sit around long enough for either to
    change before it's paid.
    """
    contract = invoice.contract

    if invoice.status != "sent":
        raise InvoiceError(f"This invoice is '{invoice.status}' and is not currently payable.")
    if contract.has_open_dispute:
        raise InvoiceError("This contract has an open dispute — billing is frozen until it's resolved.")
    if contract.has_unsigned_required_agreements:
        missing = ", ".join(contract.unsigned_required_agreement_types)
        raise InvoiceError(
            f"This contract requires a fully executed agreement ({missing}) before this invoice can be paid."
        )

    if invoice.paystack_reference:
        existing = PaystackTransaction.objects.filter(reference=invoice.paystack_reference).first()
        if existing and existing.status == "pending" and existing.authorization_url:
            return existing

    try:
        txn = payment_services.initialize_transaction(
            email=contract.client_email,
            amount=invoice.total_amount,
            currency=invoice.currency,
            purpose=INVOICE_PAYMENT_PURPOSE,
            external_reference=str(invoice.id),
            callback_url=f"{_frontend_base()}/contracts/{contract.id}",
            metadata={
                "contract_id": str(contract.id),
                "invoice_id": str(invoice.id),
                "invoice_number": invoice.invoice_number,
            },
        )
    except payment_services.PaymentInitializationError as exc:
        raise InvoiceError(str(exc)) from exc

    invoice.paystack_reference = txn.reference
    invoice.save(update_fields=["paystack_reference"])
    return txn


def _settle_invoice(invoice: Invoice, *, payment_method, paid_at=None, is_manual=False,
                     manual_payment_reference="", manual_payment_notes="", recorded_by=None,
                     paystack_reference=""):
    """
    THE single choke point where money actually changes hands for an
    Invoice — called from BOTH the in-platform Paystack success path
    (contracts.signals.handle_invoice_payment, via
    settle_invoice_from_paystack below) and
    record_manual_invoice_payment(), so there is exactly one place that
    marks an invoice paid and funds its milestones, never two copies of
    that logic that could drift apart.

    Idempotent: an already-'paid' invoice is a no-op return, not an
    error — same reasoning as payments.services.confirm_transaction()'s
    webhook/manual-verify race guard.
    """
    if invoice.status == "paid":
        return invoice
    if invoice.status == "void":
        raise InvoiceError("Cannot settle a voided invoice.")

    paid_at = paid_at or timezone.now()

    with db_transaction.atomic():
        invoice.status = "paid"
        invoice.paid_at = paid_at
        invoice.payment_method = payment_method
        invoice.is_manual_payment = is_manual
        update_fields = ["status", "paid_at", "payment_method", "is_manual_payment"]
        if paystack_reference:
            invoice.paystack_reference = paystack_reference
            update_fields.append("paystack_reference")
        if is_manual:
            invoice.manual_payment_reference = manual_payment_reference
            invoice.manual_payment_notes = manual_payment_notes
            invoice.manual_payment_recorded_by = recorded_by
            update_fields += ["manual_payment_reference", "manual_payment_notes", "manual_payment_recorded_by"]
        invoice.save(update_fields=update_fields)

        for m in invoice.milestones.select_related("contract").select_for_update():
            changed = m.mark_funded(funded_at=paid_at)
            if changed:
                _log_milestone_event(
                    m, "funded", actor_type="admin" if is_manual else "system", actor_user=recorded_by,
                    metadata={"invoice_id": str(invoice.id), "invoice_number": invoice.invoice_number},
                )

        contract = invoice.contract
        if contract.status in ("signed", "sent", "negotiating"):
            contract.status = "active"
            contract.save(update_fields=["status"])

    logger.info(
        "Invoice %s settled via %s (manual=%s) for contract %s",
        invoice.invoice_number, payment_method, is_manual, invoice.contract_id,
    )
    try:
        emails.send_invoice_paid_email(invoice)
    except Exception:
        logger.exception("Failed to send invoice-paid email for invoice %s", invoice.id)
    return invoice


def settle_invoice_from_paystack(invoice: Invoice, *, paid_at, paystack_reference):
    """Called only by contracts.signals.handle_invoice_payment — a thin
    public wrapper so signals.py never reaches into a private
    _settle_invoice() across the module boundary."""
    return _settle_invoice(invoice, payment_method="paystack", paid_at=paid_at, paystack_reference=paystack_reference)


def record_manual_invoice_payment(invoice: Invoice, *, payment_method, manual_payment_reference="",
                                   manual_payment_notes="", recorded_by=None):
    """
    Admin-recorded settlement for anything paid OUTSIDE Paystack — bank
    transfer, M-Pesa, Payoneer, PayPal, Wise, Remitly, Sendwave, crypto,
    or 'other'. Only legal against a 'sent' invoice (a draft was never
    actually presented to the client). Routes through the exact same
    _settle_invoice() the Paystack webhook path uses.
    """
    if invoice.status != "sent":
        raise InvoiceError(f"This invoice is '{invoice.status}' — only a sent invoice can be marked paid.")
    if payment_method == "paystack":
        raise InvoiceError("Use the Paystack checkout flow for in-platform payments, not manual recording.")

    return _settle_invoice(
        invoice, payment_method=payment_method, is_manual=True,
        manual_payment_reference=manual_payment_reference,
        manual_payment_notes=manual_payment_notes, recorded_by=recorded_by,
    )


SIGNED_MESSAGE_SENDER_NAME = "Scape Data Team"
SIGNED_MESSAGE_BODY = (
    "Thanks for signing! Here's what happens next: our team will set up the "
    "milestones for this contract, and you'll see each one appear here as work "
    "begins. When a milestone is ready to be paid, you'll get an invoice you can "
    "pay directly from this page. Feel free to message us here anytime with "
    "questions."
)


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


# ── Milestone escrow workflow ────────────────────────────────────────────

def _log_milestone_event(milestone, event_type, *, actor_type="system", actor_user=None,
                          actor_name="", metadata=None):
    """THE only place a MilestoneEvent gets created. Every workflow
    function below calls this immediately after its model-level
    transition succeeds — never before, so an event is never logged for
    a transition that actually got rejected."""
    return MilestoneEvent.objects.create(
        milestone=milestone, event_type=event_type, actor_type=actor_type,
        actor_user=actor_user, actor_name=actor_name, metadata=metadata or {},
    )


def submit_milestone_deliverable(milestone: Milestone, *, summary, file=None, link_url="", submitted_by=None):
    """
    THE only way a MilestoneDeliverable gets created — covers both the
    first submission (funded -> submitted) and every resubmission after
    a revision request (revision_requested -> submitted). Version
    number and the 'submitted' vs 'resubmitted' event type are derived
    here, not left for the caller to track.

    Always an admin action in this codebase — Scape is the sole
    provider, there's no separate onboarded-provider role submitting
    work (see contracts/models.py module docstring).
    """
    if milestone.has_open_dispute:
        raise MilestoneWorkflowError(
            "This milestone has an open dispute — resolve it before submitting work."
        )
    if milestone.status not in ("funded", "revision_requested"):
        raise MilestoneWorkflowError(
            f"This milestone is '{milestone.status}' — it must be funded before work can be submitted."
        )

    is_resubmission = milestone.status == "revision_requested"

    next_version = (milestone.deliverables.order_by("-version").values_list(
        "version", flat=True
    ).first() or 0) + 1

    file_url, file_filename = "", ""
    if file is not None:
        from . import storage
        file_url, file_filename = storage.upload_deliverable_file(file, milestone.id)

    deliverable = MilestoneDeliverable.objects.create(
        milestone=milestone, version=next_version, summary=summary,
        file_url=file_url, file_filename=file_filename, link_url=link_url, submitted_by=submitted_by,
    )

    milestone.submit()

    _log_milestone_event(
        milestone, "resubmitted" if is_resubmission else "submitted",
        actor_type="admin", actor_user=submitted_by,
        metadata={"deliverable_id": str(deliverable.id), "version": next_version},
    )
    try:
        emails.send_milestone_submitted_email(milestone, deliverable)
    except Exception:
        logger.exception("Failed to send milestone-submitted email for milestone %s", milestone.id)
    return deliverable


def request_milestone_revision(milestone: Milestone, *, note, requested_by_user=None, requested_by_name=""):
    """
    Client-side action only — the client is the one reviewing submitted
    work. Clears the auto-release clock (see Milestone.request_revision)
    so the provider isn't racing a deadline while reworking the
    deliverable; a fresh clock starts on the next submit().
    """
    if milestone.status != "submitted":
        raise MilestoneWorkflowError(
            f"This milestone is '{milestone.status}' — nothing is currently awaiting review."
        )

    milestone.request_revision()
    _log_milestone_event(
        milestone, "revision_requested", actor_type="client",
        actor_user=requested_by_user, actor_name=requested_by_name,
        metadata={"note": note},
    )
    try:
        emails.send_milestone_revision_requested_email(milestone, note)
    except Exception:
        logger.exception("Failed to send revision-requested email for milestone %s", milestone.id)
    return milestone


def approve_milestone_release(milestone: Milestone, *, released_by_user=None, released_by_name=""):
    """
    Client-side explicit approval — the actual 'funds recognized as
    earned' action. The only other path into 'released' is
    auto_release_milestone() below, firing off the deadline instead of
    a client action.
    """
    if milestone.has_open_dispute:
        raise MilestoneWorkflowError(
            "This milestone has an open dispute — resolve it before releasing funds."
        )
    if milestone.status not in ("submitted", "revision_requested"):
        raise MilestoneWorkflowError(
            f"This milestone is '{milestone.status}' — there's nothing to release."
        )

    milestone.release(released_by_user=released_by_user)
    _log_milestone_event(
        milestone, "released", actor_type="client",
        actor_user=released_by_user, actor_name=released_by_name,
    )
    try:
        emails.send_milestone_released_email(milestone)
    except Exception:
        logger.exception("Failed to send milestone-released email for milestone %s", milestone.id)
    return milestone


def auto_release_milestone(milestone: Milestone):
    """
    Called by a periodic task (celery beat) sweeping submitted
    milestones — never by a view directly. Re-checks the deadline AND
    the dispute freeze at the moment of firing, since either could have
    changed between the sweep query running and this call executing.
    Returns False (no-op) rather than raising, since a sweep task calls
    this in a loop and one already-handled milestone isn't an error.
    """
    if milestone.has_open_dispute:
        return False
    if not milestone.is_past_auto_release_deadline():
        return False

    milestone.release(auto=True)
    _log_milestone_event(milestone, "auto_released", actor_type="system")
    try:
        emails.send_milestone_released_email(milestone, auto=True)
    except Exception:
        logger.exception("Failed to send auto-release email for milestone %s", milestone.id)
    return True


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

def raise_dispute(contract, *, category, description, raised_by_user=None, raised_by_name="", milestone=None):
    """
    THE only way a ContractDispute gets created. Pass `milestone` to
    scope the freeze to that milestone's release only; leave it None
    for the original, broader contract-wide freeze on all billing (see
    Contract.has_open_dispute vs Milestone.has_open_dispute).

    Refuses if the relevant scope already has an open dispute — one
    active dispute per scope at a time; additional concerns go in the
    resolution note or the message thread, not a second parallel dispute.
    """
    if milestone is not None:
        if milestone.contract_id != contract.id:
            raise DisputeError("This milestone does not belong to this contract.")
        if milestone.has_open_dispute:
            raise DisputeError("There's already an open dispute on this milestone.")
    elif contract.has_open_dispute:
        raise DisputeError("There's already an open contract-wide dispute on this contract.")

    dispute = ContractDispute.objects.create(
        contract=contract, milestone=milestone, category=category, description=description,
        raised_by_user=raised_by_user, raised_by_name=raised_by_name,
    )

    if milestone is not None:
        milestone.mark_disputed()
        _log_milestone_event(
            milestone, "disputed", actor_type="client",
            actor_user=raised_by_user, actor_name=raised_by_name,
            metadata={"dispute_id": str(dispute.id), "category": category},
        )

    try:
        emails.send_dispute_raised_email(dispute)
    except Exception:
        logger.exception("Failed to send dispute-raised email for dispute %s", dispute.id)
    return dispute


def resolve_dispute(dispute: ContractDispute, *, new_status, resolved_by, resolution_note="",
                     milestone_resolution_status=None):
    """
    THE only way a ContractDispute's status changes after filing.
    Moving to "under_review" logs progress without lifting any freeze;
    moving to any resolved_*/withdrawn status lifts a contract-wide
    freeze immediately, since Contract.has_open_dispute re-evaluates
    live on every call to initiate_milestone_payment().

    If the dispute is milestone-scoped, resolving it ALSO moves that
    milestone out of 'disputed' via Milestone.resolve_dispute() — using
    milestone_resolution_status if the caller (an admin, deciding case
    by case) provides one, or the DISPUTE_TO_MILESTONE_STATUS default
    mapping otherwise.
    """
    if not dispute.is_open():
        raise DisputeError(f"This dispute is already '{dispute.status}'.")

    dispute.status = new_status
    dispute.resolution_note = resolution_note
    if new_status != "under_review":
        dispute.resolved_by = resolved_by
        dispute.resolved_at = timezone.now()
    dispute.save(update_fields=["status", "resolution_note", "resolved_by", "resolved_at"])

    if dispute.milestone_id and new_status != "under_review":
        target = milestone_resolution_status or DISPUTE_TO_MILESTONE_STATUS.get(new_status)
        if target:
            dispute.milestone.resolve_dispute(
                new_status=target,
                released_by_user=resolved_by if target == "released" else None,
            )
            _log_milestone_event(
                dispute.milestone, "dispute_resolved", actor_type="admin", actor_user=resolved_by,
                metadata={"dispute_id": str(dispute.id), "resolution": new_status, "milestone_status": target},
            )

    if new_status != "under_review":
        try:
            emails.send_dispute_resolved_email(dispute)
        except Exception:
            logger.exception("Failed to send dispute-resolved email for dispute %s", dispute.id)
    return dispute