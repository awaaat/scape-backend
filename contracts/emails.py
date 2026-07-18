"""
contracts/emails.py
Notification emails for the contract lifecycle. Same
EmailMultiAlternatives + render_to_string pattern as every other app
(payments/emails.py, users/emails.py, leads/email.py) — uses whatever
EMAIL_BACKEND is configured (Anymail/Brevo) automatically.
"""
import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.html import strip_tags

logger = logging.getLogger("contracts")


def _admin_recipients():
    return list(getattr(settings, "ADMIN_NOTIFICATION_EMAILS", []) or [])


def _send(subject, template_name, context, to_emails, reply_to=None):
    if not to_emails:
        return
    html_body = render_to_string(template_name, context)
    message = EmailMultiAlternatives(
        subject=subject,
        body=strip_tags(html_body),
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=to_emails,
        reply_to=reply_to or [getattr(settings, "REPLY_TO_EMAIL", settings.DEFAULT_FROM_EMAIL)],
    )
    message.attach_alternative(html_body, "text/html")
    message.send(fail_silently=False)


def send_contract_invitation_email(contract, link):
    _send(
        subject=f"Contract ready for your review — {contract.title}",
        template_name="email/contract_invitation.html",
        context={"contract": contract, "link": link},
        to_emails=[contract.client_email],
    )
    logger.info("Contract invitation email sent for contract %s to %s", contract.id, contract.client_email)


def send_new_message_notification(message):
    contract = message.contract
    if message.sender_type == "client":
        to_emails = _admin_recipients()
        subject = f"New message from {message.sender_name or contract.client_name} — {contract.title}"
    else:
        to_emails = [contract.client_email]
        subject = f"New message on your contract — {contract.title}"
    _send(
        subject=subject,
        template_name="email/contract_new_message.html",
        context={"contract": contract, "message": message},
        to_emails=to_emails,
    )
    logger.info("New-message notification sent for contract %s message %s", contract.id, message.id)


def send_contract_signed_email(contract):
    _send(
        subject=f"Signed — {contract.title}",
        template_name="email/contract_signed.html",
        context={"contract": contract},
        to_emails=[contract.client_email] + _admin_recipients(),
    )
    logger.info("Contract-signed email sent for contract %s", contract.id)


def send_milestone_invoice_email(milestone, payment_url):
    _send(
        subject=f"Payment due — {milestone.title} ({milestone.contract.title})",
        template_name="email/contract_milestone_invoice.html",
        context={"milestone": milestone, "contract": milestone.contract, "payment_url": payment_url},
        to_emails=[milestone.contract.client_email],
    )
    logger.info("Milestone invoice email sent for milestone %s", milestone.id)


def send_milestone_paid_email(milestone):
    contract = milestone.contract
    _send(
        subject=f"Payment received — {milestone.title} ({contract.title})",
        template_name="email/contract_milestone_paid.html",
        context={"milestone": milestone, "contract": contract},
        to_emails=[contract.client_email] + _admin_recipients(),
    )
    logger.info("Milestone paid email sent for milestone %s", milestone.id)
