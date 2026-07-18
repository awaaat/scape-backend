"""
contracts/signals.py
Two receivers:
  - handle_milestone_payment: listens for payments.signals.payment_succeeded
    — this is where purpose="contract_milestone" gets meaning. payments
    itself has none. Mirrors property_intel/signals.py's
    handle_property_report_payment.
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
from .models import Message, Milestone
from .serializers import MessageSerializer
from .services import MILESTONE_PAYMENT_PURPOSE

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

    if milestone.status == "paid":
        return  # webhook + manual verify both landing here — must be a no-op the 2nd time

    with db_transaction.atomic():
        changed = milestone.mark_paid(paid_at=paid_at)
        if not changed:
            return

        contract = milestone.contract
        if contract.status in ("signed", "sent", "negotiating"):
            contract.status = "active"
            contract.save(update_fields=["status"])

    logger.info("Milestone %s marked paid via %s (contract %s)", milestone.id, reference, milestone.contract_id)

    try:
        emails.send_milestone_paid_email(milestone)
    except Exception:
        logger.exception("Failed to send milestone-paid email for milestone %s", milestone.id)
