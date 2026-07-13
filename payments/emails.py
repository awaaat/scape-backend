"""
payments/emails.py
Generic receipt/failure emails. Stays agnostic of what was purchased —
only uses fields every PaystackTransaction has (email, amount, currency,
reference, purpose). `purpose` is prettified for display only, never
interpreted.
"""
import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.html import strip_tags

logger = logging.getLogger("payments")


def _pretty_purpose(purpose):
    return (purpose or "payment").replace("_", " ").title()


def send_payment_receipt_email(txn):
    context = {
        "reference": txn.reference,
        "amount": txn.amount,
        "currency": txn.currency,
        "purpose_label": _pretty_purpose(txn.purpose),
        "channel": txn.channel or "N/A",
        "paid_at": txn.paid_at,
        "support_email": getattr(settings, "REPLY_TO_EMAIL", settings.DEFAULT_FROM_EMAIL),
    }
    html_body = render_to_string("email/payment_receipt.html", context)
    message = EmailMultiAlternatives(
        subject=f"Payment received — {context['purpose_label']} ({txn.reference})",
        body=strip_tags(html_body),
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[txn.email],
        reply_to=[getattr(settings, "REPLY_TO_EMAIL", settings.DEFAULT_FROM_EMAIL)],
    )
    message.attach_alternative(html_body, "text/html")
    message.send(fail_silently=False)
    logger.info("Payment receipt email sent for %s to %s", txn.reference, txn.email)


def send_payment_failed_email(txn):
    context = {
        "reference": txn.reference,
        "amount": txn.amount,
        "currency": txn.currency,
        "purpose_label": _pretty_purpose(txn.purpose),
        "failure_reason": txn.failure_reason or "The payment provider declined this transaction.",
        "support_email": getattr(settings, "REPLY_TO_EMAIL", settings.DEFAULT_FROM_EMAIL),
    }
    html_body = render_to_string("email/payment_failed.html", context)
    message = EmailMultiAlternatives(
        subject=f"Payment failed — {context['purpose_label']} ({txn.reference})",
        body=strip_tags(html_body),
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[txn.email],
        reply_to=[getattr(settings, "REPLY_TO_EMAIL", settings.DEFAULT_FROM_EMAIL)],
    )
    message.attach_alternative(html_body, "text/html")
    message.send(fail_silently=False)
    logger.info("Payment failed email sent for %s to %s", txn.reference, txn.email)
