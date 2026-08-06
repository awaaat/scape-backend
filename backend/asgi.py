"""
ASGI config for backend project.

Routes plain HTTP through Django as normal, and routes websocket
connections (contracts + projects live messaging) through Channels,
authenticated via JWT (see contracts/channels_auth.py — generic, not
contract-specific, so it's reused as-is for both apps rather than
duplicated).
"""
import os

import django
from channels.routing import ProtocolTypeRouter, URLRouter
from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "backend.settings")
django.setup()

from contracts.channels_auth import JWTAuthMiddlewareStack  # noqa: E402
from contracts.routing import websocket_urlpatterns as contracts_ws_urlpatterns  # noqa: E402
from projects.routing import websocket_urlpatterns as projects_ws_urlpatterns  # noqa: E402

application = ProtocolTypeRouter({
    "http": get_asgi_application(),
    "websocket": JWTAuthMiddlewareStack(
        URLRouter(contracts_ws_urlpatterns + projects_ws_urlpatterns)
    ),
})
