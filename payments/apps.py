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
        # No signal receivers to wire up here — this app only ever SENDS
        # payment_succeeded, it never receives anything. Nothing to import.
        pass
