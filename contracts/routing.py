from django.urls import re_path

from . import consumers

websocket_urlpatterns = [
    re_path(
        r"^ws/contracts/(?P<contract_id>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})/messages/$",
        consumers.ContractMessageConsumer.as_asgi(),
    ),
]
