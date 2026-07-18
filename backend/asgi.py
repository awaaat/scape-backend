"""
ASGI config for backend project.

Routes plain HTTP through Django as normal, and routes websocket
connections (contracts live messaging) through Channels, authenticated
via JWT (see contracts/channels_auth.py).
"""
import os

import django
from channels.routing import ProtocolTypeRouter, URLRouter
from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "backend.settings")
django.setup()

from contracts.channels_auth import JWTAuthMiddlewareStack  # noqa: E402
from contracts.routing import websocket_urlpatterns  # noqa: E402

application = ProtocolTypeRouter({
    "http": get_asgi_application(),
    "websocket": JWTAuthMiddlewareStack(URLRouter(websocket_urlpatterns)),
})
