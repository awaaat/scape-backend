"""
Two receivers:
  - handle_milestone_payment: listens for payments.signals.payment_succeeded
    — this is where purpose="contract_milestone" gets meaning. payments
    itself has none. Mirrors property_intel/signals.py's
    handle_property_report_payment. Moves the milestone to "funded",
    NOT the old terminal "paid" — funding is now the start of the
    escrow workflow (submit → review → release), not the end of it.
    See contracts/models.py's module docstring for the full lifecycle.
  - broadcast_new_message: fires on every Message save and pushes it to
    the contract's websocket group. This is what makes messaging actually
    real-time regardless of whether the message came in over the REST
    API, the websocket consumer, or the Django admin — one creation path
    (services.post_message), one broadcast point.
"""
import json
import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import transaction as db_transaction
from django.db.models.signals import post_save
from django.dispatch import receiver
from rest_framework.renderers import JSONRenderer

from payments.signals import payment_succeeded

from . import emails
from .models import Invoice, Message, Milestone, MilestoneEvent
from .serializers import MessageSerializer
from .services import INVOICE_PAYMENT_PURPOSE, MILESTONE_PAYMENT_PURPOSE, settle_invoice_from_paystack

logger = logging.getLogger("contracts")


@receiver(post_save, sender=Message)
def broadcast_new_message(sender, instance, created, **kwargs):
    if not created:
        return

    channel_layer = get_channel_layer()
    if channel_layer is None:
        # CHANNEL_LAYERS isn't configured (e.g. not wired in yet, or a
        # management command / test run with no layer available) — the
        # message is still saved and still emailed; it just won't push
        # live. Never let a missing layer break message creation itself.
        logger.warning("broadcast_new_message: no channel layer configured — message %s not pushed live", instance.id)
        return

    # serializer.data leaves UUID/Decimal as native Python objects — fine
    # for DRF's own JSONRenderer, NOT fine for plain json.dumps (which is
    # what the websocket consumer's send_json uses) or for a Redis channel
    # layer's msgpack serializer. Render once through JSONRenderer (same
    # DjangoJSONEncoder DRF views already use) and load back into
    # plain str/float/dict — guaranteed serializable everywhere downstream.
    payload = json.loads(JSONRenderer().render(MessageSerializer(instance).data))
    async_to_sync(channel_layer.group_send)(
        f"contract_{instance.contract_id}",
        {"type": "chat.message", "message": payload},
    )


@receiver(payment_succeeded)
def handle_milestone_payment(sender, reference, purpose, external_reference,
                              amount, currency, email, channel, paid_at,
                              metadata, authorization_details=None, **kwargs):
    if purpose != MILESTONE_PAYMENT_PURPOSE:
        return

    try:
        milestone = Milestone.objects.select_related("contract").get(pk=external_reference)
    except (Milestone.DoesNotExist, ValueError, TypeError):
        logger.error(
            "payment_succeeded for purpose=%s reference=%s but no matching Milestone (external_reference=%r)",
            purpose, reference, external_reference,
        )
        return

    if milestone.status != "invoiced":
        # Already funded (webhook + manual verify racing — must be a
        # no-op the 2nd time), or further along, or in some other state
        # entirely (disputed/cancelled) that a late-arriving webhook
        # must never silently override. mark_funded() below guards this
        # too, but checking here avoids opening a transaction for a
        # request we already know is a no-op.
        return

    with db_transaction.atomic():
        changed = milestone.mark_funded(funded_at=paid_at)
        if not changed:
            return

        MilestoneEvent.objects.create(
            milestone=milestone, event_type="funded", actor_type="system",
            metadata={"paystack_reference": reference, "amount": str(amount), "currency": currency},
        )

        contract = milestone.contract
        if contract.status in ("signed", "sent", "negotiating"):
            contract.status = "active"
            contract.save(update_fields=["status"])

    logger.info("Milestone %s marked funded via %s (contract %s)", milestone.id, reference, milestone.contract_id)

    try:
        emails.send_milestone_funded_email(milestone)
    except Exception:
        logger.exception("Failed to send milestone-funded email for milestone %s", milestone.id)


@receiver(payment_succeeded)
def handle_invoice_payment(sender, reference, purpose, external_reference,
                            amount, currency, email, channel, paid_at,
                            metadata, authorization_details=None, **kwargs):
    """Bundled-invoice counterpart to handle_milestone_payment above —
    purpose="contract_invoice" gets meaning here. Settles the WHOLE
    invoice (and every milestone attached to it) through the same
    single choke point (_settle_invoice) record_manual_invoice_payment()
    uses, so in-platform and manually-recorded payments never drift."""
    if purpose != INVOICE_PAYMENT_PURPOSE:
        return

    try:
        invoice = Invoice.objects.select_related("contract").get(pk=external_reference)
    except (Invoice.DoesNotExist, ValueError, TypeError):
        logger.error(
            "payment_succeeded for purpose=%s reference=%s but no matching Invoice (external_reference=%r)",
            purpose, reference, external_reference,
        )
        return

    if invoice.status != "sent":
        # Already paid (webhook + manual verify racing — must be a
        # no-op the 2nd time), or voided after checkout started — a
        # late-arriving webhook must never resurrect or double-settle it.
        return

    settle_invoice_from_paystack(invoice, paid_at=paid_at, paystack_reference=reference)
    logger.info("Invoice %s settled via %s (contract %s)", invoice.invoice_number, reference, invoice.contract_id)