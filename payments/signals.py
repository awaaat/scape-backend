"""
payments/signals.py

The entire integration surface of this app. Anything that needs to react
to money actually landing (property_intel today; anything Scaepe charges
for tomorrow) connects a receiver to payment_succeeded instead of this app
importing and calling into them directly.

Sent exactly once per transaction, from services.confirm_transaction(),
guarded by an atomic pending->success status transition — so a webhook
retry, a manual /verify/ call, and a race between the two can never fire
this twice for the same transaction.

kwargs sent with this signal:
    reference           str   — our PaystackTransaction.reference
    purpose              str   — opaque caller-chosen tag, e.g. "property_report"
    external_reference   str   — opaque caller-supplied id, e.g. a report id
    amount               Decimal — major currency unit
    currency              str
    email                 str
    channel               str   — e.g. "mobile_money", "card"
    paid_at               datetime
    metadata              dict  — whatever the caller passed at initialize time
"""
import django.dispatch

payment_succeeded = django.dispatch.Signal()
wallet_topup_succeeded = django.dispatch.Signal()
