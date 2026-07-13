"""
payments/apps.py

Standalone payments app. Zero imports from property_intel (or any other
feature app) anywhere in this app — that's the whole point of splitting it
out. Anything that wants to know when money has actually landed listens to
payments.signals.payment_succeeded instead of this app reaching into it.
"""
from django.apps import AppConfig


class PaymentsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "payments"
    default = True

    def ready(self):
        from . import wallet_signals  # noqa: F401 -- registers credit_user_wallet + relay_wallet_topup
