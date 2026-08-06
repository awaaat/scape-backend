"""
projects/signals.py

One receiver: broadcast_new_project_message fires on every
ProjectMessage save and pushes it to the project's websocket group.
This is what makes messaging actually real-time regardless of whether
the message came in over the REST API, the websocket consumer, or the
Django admin — one creation path (services.post_project_message), one
broadcast point. Directly mirrors contracts.signals.broadcast_new_message.
"""
import json
import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db.models.signals import post_save
from django.dispatch import receiver
from rest_framework.renderers import JSONRenderer

from .models import ProjectMessage
from .serializers import ProjectMessageSerializer

logger = logging.getLogger("projects")


@receiver(post_save, sender=ProjectMessage)
def broadcast_new_project_message(sender, instance, created, **kwargs):
    if not created:
        return

    channel_layer = get_channel_layer()
    if channel_layer is None:
        # CHANNEL_LAYERS isn't configured (e.g. a management command or
        # test run with no layer available) — the message is still
        # saved and still emailed; it just won't push live. Never let a
        # missing layer break message creation itself.
        logger.warning(
            "broadcast_new_project_message: no channel layer configured — message %s not pushed live",
            instance.id,
        )
        return

    # serializer.data leaves UUID/Decimal as native Python objects — fine
    # for DRF's own JSONRenderer, NOT fine for plain json.dumps (what the
    # websocket consumer's send_json uses) or for a Redis channel layer's
    # msgpack serializer. Render once through JSONRenderer (same
    # DjangoJSONEncoder DRF views already use) and load back into plain
    # str/float/dict — guaranteed serializable everywhere downstream.
    payload = json.loads(JSONRenderer().render(ProjectMessageSerializer(instance).data))
    async_to_sync(channel_layer.group_send)(
        f"project_{instance.project_id}",
        {"type": "chat.message", "message": payload},
    )
