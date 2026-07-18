"""
contracts/channels_auth.py

Channels has no notion of DRF authentication classes, and a browser
WebSocket handshake carries no Authorization header — so the access
token travels as a query param instead: wss://.../messages/?access=<jwt>

This validates it the exact same way
rest_framework_simplejwt.authentication.JWTAuthentication does for
HTTP requests (same token, same SIMPLE_JWT settings, same blacklist
check), and populates scope["user"] the same way Channels' own
AuthMiddlewareStack does for session auth — so consumers just read
self.scope["user"] like a normal DRF view would read request.user.

DEFAULT_AUTHENTICATION_CLASSES in this project is JWT-only (no
SessionAuthentication — see backend/settings.py REST_FRAMEWORK), so
this deliberately does NOT stack on top of Channels' session-based
AuthMiddlewareStack; it replaces it for consistency with how HTTP auth
already works here.

A missing/invalid token is not an error here — scope["user"] is just
left as AnonymousUser, same as an unauthenticated HTTP request.
ContractMessageConsumer decides what to do with that: staff get admin
access, a matching client_user gets client access, and anyone else
falls back to the contract's own access-token check (a *different*
token — the per-contract one from Contract.access_token_hash, also
passed as a query param).
"""
from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from django.contrib.auth.models import AnonymousUser
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError

_jwt_auth = JWTAuthentication()


@database_sync_to_async
def _resolve_user(raw_token):
    if not raw_token:
        return AnonymousUser()
    try:
        validated_token = _jwt_auth.get_validated_token(raw_token)
        return _jwt_auth.get_user(validated_token)
    except (InvalidToken, TokenError):
        return AnonymousUser()


class JWTAuthMiddleware:
    """ASGI middleware — wrap your websocket URLRouter with this (see
    backend/asgi.py wiring notes in the delivery message)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        query_string = scope.get("query_string", b"").decode("utf-8")
        raw_token = (parse_qs(query_string).get("access") or [None])[0]
        scope["user"] = await _resolve_user(raw_token)
        return await self.app(scope, receive, send)


def JWTAuthMiddlewareStack(app):
    """Named to match channels.auth.AuthMiddlewareStack's convention —
    use this directly in ProtocolTypeRouter, it already does everything
    AuthMiddlewareStack would, using JWT instead of sessions."""
    return JWTAuthMiddleware(app)
