from django.apps import AppConfig


class PropertyIntelConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'property_intel'

    def ready(self):
        # Connects the payment_succeeded receiver in signals.py. Imported
        # here (not at module top-level) per Django's standard pattern —
        # ready() is guaranteed to run after every app's models are loaded,
        # so this can safely import payments.signals and property_intel's
        # own models without any import-order risk.
        from . import signals  # noqa: F401
