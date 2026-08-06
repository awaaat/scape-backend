"""
projects/consumers.py

One consumer, one job: the live message thread for a single project.
Auth mirrors permissions.get_owned_project() — staff always in, the
project's own owner always in, anyone else rejected. Simpler than
contracts.ContractMessageConsumer because there's no no-login
access-token path here: a project always belongs to an authenticated
account, so the JWT already resolved onto scope["user"] by
JWTAuthMiddleware (see contracts/channels_auth.py — reused as-is, it's
not contract-specific) is the only identity source.

Message creation goes through services.post_project_message() — the
same function the REST endpoints call — so
signals.broadcast_new_project_message (the post_save receiver) is what
actually pushes the message out to everyone in the group, including
the sender's own other open tabs. This consumer does not send the
message back itself; it relies on that broadcast, so REST-sent and
socket-sent messages behave identically.
"""
import json
import logging

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer

from . import services
from .models import Project

logger = logging.getLogger("projects")


class ProjectMessageConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        self.project_id = self.scope["url_route"]["kwargs"]["project_id"]
        user = self.scope.get("user")

        identity = await self._resolve_identity(user)
        if identity is None:
            # Never reveal WHY (unknown project vs. not the owner vs. no
            # auth) — same "no further hint" posture as
            # permissions.get_owned_project() over HTTP.
            await self.close(code=4403)
            return

        self.sender_type, self.sender_user, self.sender_name = identity
        self.group_name = f"project_{self.project_id}"
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()
        logger.info("WS connected: project=%s as %s", self.project_id, self.sender_type)

    async def disconnect(self, close_code):
        if hasattr(self, "group_name"):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def receive_json(self, content, **kwargs):
        body = (content.get("body") or "").strip()
        if not body:
            await self.send_json({"error": "body is required"})
            return

        try:
            await self._create_message(body)
        except Exception:
            logger.exception("WS message create failed: project=%s", self.project_id)
            await self.send_json({"error": "Could not send message — please try again."})

    async def receive(self, text_data=None, bytes_data=None, **kwargs):
        # AsyncJsonWebsocketConsumer.receive() raises on non-JSON input by
        # default — override to send a clean client-facing error instead
        # of tearing down the connection over one malformed frame.
        if text_data is not None:
            try:
                content = json.loads(text_data)
            except json.JSONDecodeError:
                await self.send_json({"error": "Malformed message — expected JSON."})
                return
            await self.receive_json(content, **kwargs)

    # ── Group event handler — dispatched by channel layer group_send with
    # {"type": "chat.message", ...} from signals.broadcast_new_project_message ──
    async def chat_message(self, event):
        await self.send_json(event["message"])

    # ── DB access, off the event loop ─────────────────────────────────
    @database_sync_to_async
    def _resolve_identity(self, user):
        try:
            project = Project.objects.get(pk=self.project_id)
        except (Project.DoesNotExist, ValueError, TypeError):
            return None

        if user is None or not getattr(user, "is_authenticated", False):
            return None

        if user.is_staff:
            return ("admin", user, user.get_username())

        if project.owner_id == user.id:
            return ("client", user, user.get_username())

        return None

    @database_sync_to_async
    def _create_message(self, body):
        project = Project.objects.get(pk=self.project_id)
        return services.post_project_message(
            project=project, sender_type=self.sender_type,
            sender_user=self.sender_user, sender_name=self.sender_name, body=body,
        )
