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
