from django.apps import AppConfig


class OrdersConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "orders"

    def ready(self):
        # Open CORS for /api/orders/ to every host registered in catalog.SITES,
        # so adding a site on a brand-new domain needs no settings change.
        try:
            from corsheaders.signals import check_request_enabled
        except ImportError:  # pragma: no cover
            return
        from . import catalog

        def allow_registered_sites(sender, request, **kwargs):
            if not request.path.startswith("/api/orders/"):
                return False
            return catalog.site_for_origin(request.META.get("HTTP_ORIGIN", "")) is not None

        check_request_enabled.connect(
            allow_registered_sites, weak=False, dispatch_uid="orders_registered_sites_cors"
        )


# ═══ ORDER ENGAGEMENT LAYER (install_order_engagement.sh) ═══
# Extends ready() without touching the original body: wires signals, safe wrappers and checks.
_ready_before_engagement = OrdersConfig.ready


def _ready_with_engagement(self):
    _ready_before_engagement(self)
    from . import engagement_hooks
    engagement_hooks.install()


OrdersConfig.ready = _ready_with_engagement
