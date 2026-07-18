"""
contracts/consumers.py

One consumer, one job: the live message thread for a single contract.
Auth mirrors permissions.get_client_authorized_contract() exactly —
staff always in, a matching client_user always in, otherwise the
contract's own access token (query param `token`, distinct from the
JWT `access` param JWTAuthMiddleware already consumed) is required.

Message creation goes through services.post_message() — the same
function the REST endpoints call — so signals.broadcast_new_message
(the post_save receiver) is what actually pushes the message out to
everyone in the group, including the sender's own other open tabs.
This consumer does not send the message back itself; it relies on
that broadcast, so REST-sent and socket-sent messages behave
identically.
"""
import json
import logging
from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer

from . import services
from .models import Contract

logger = logging.getLogger("contracts")


class ContractMessageConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        self.contract_id = self.scope["url_route"]["kwargs"]["contract_id"]
        query = parse_qs(self.scope.get("query_string", b"").decode("utf-8"))
        contract_token = (query.get("token") or [None])[0]
        user = self.scope.get("user")

        identity = await self._resolve_identity(user, contract_token)
        if identity is None:
            # Never reveal WHY (unknown contract vs. wrong token vs. no
            # auth) — same "404 or 403, no further hint" posture as
            # permissions.get_client_authorized_contract() over HTTP.
            await self.close(code=4403)
            return

        self.sender_type, self.sender_user, self.sender_name = identity
        self.group_name = f"contract_{self.contract_id}"
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()
        logger.info("WS connected: contract=%s as %s", self.contract_id, self.sender_type)

    async def disconnect(self, close_code):
        if hasattr(self, "group_name"):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def receive_json(self, content, **kwargs):
        body = (content.get("body") or "").strip()
        if not body:
            await self.send_json({"error": "body is required"})
            return

        sender_name = self.sender_name
        if self.sender_type == "client":
            override = (content.get("sender_name") or "").strip()
            if override:
                sender_name = override

        try:
            await self._create_message(body, sender_name)
        except Exception:
            logger.exception("WS message create failed: contract=%s", self.contract_id)
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
    # {"type": "chat.message", ...} from signals.broadcast_new_message ──
    async def chat_message(self, event):
        await self.send_json(event["message"])

    # ── DB access, off the event loop ─────────────────────────────────
    @database_sync_to_async
    def _resolve_identity(self, user, contract_token):
        try:
            contract = Contract.objects.get(pk=self.contract_id)
        except (Contract.DoesNotExist, ValueError, TypeError):
            return None

        if user is not None and getattr(user, "is_authenticated", False) and user.is_staff:
            return ("admin", user, user.get_username())

        if user is not None and getattr(user, "is_authenticated", False) and contract.client_user_id == user.id:
            return ("client", user, user.get_username())

        if contract_token and contract.verify_access_token(contract_token):
            return ("client", None, contract.client_name)

        return None

    @database_sync_to_async
    def _create_message(self, body, sender_name):
        contract = Contract.objects.get(pk=self.contract_id)
        return services.post_message(
            contract=contract, sender_type=self.sender_type,
            sender_user=self.sender_user, sender_name=sender_name, body=body,
        )
