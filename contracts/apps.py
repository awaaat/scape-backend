from django.apps import AppConfig


class ContractsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "contracts"

    def ready(self):
        # Connects the payment_succeeded receiver in signals.py — same
        # ready()-time import pattern as property_intel, for the same
        # reason: guaranteed to run after every app's models are loaded,
        # so this can safely import payments.signals and contracts' own
        # models without any import-order risk.
        from . import signals  # noqa: F401
