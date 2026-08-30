"""
contracts/emails.py
Notification emails for the contract lifecycle. Same
EmailMultiAlternatives + render_to_string pattern as every other app
(payments/emails.py, users/emails.py, leads/email.py) — uses whatever
EMAIL_BACKEND is configured (Anymail/Brevo) automatically.

── Milestone escrow workflow (added) ───────────────────────────────────
send_milestone_paid_email is renamed send_milestone_funded_email to
match Milestone.mark_funded() — "funded" is the held-pending-delivery
state now, not the end of the story (see contracts/models.py's module
docstring). Three new functions cover the rest of the lifecycle:
send_milestone_submitted_email, send_milestone_revision_requested_email,
send_milestone_released_email — called by services.py, always inside a
try/except there, so a template or backend failure never blocks the
underlying status transition.
"""
import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.html import strip_tags

logger = logging.getLogger("contracts")


def _admin_recipients():
    return list(getattr(settings, "ADMIN_NOTIFICATION_EMAILS", []) or [])


def _send(subject, template_name, context, to_emails, reply_to=None, attachments=None):
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
    # (filename, bytes, mimetype) tuples — used by send_invoice_sent_email
    # to hand the client the actual PDF, not just a link.
    for filename, file_content, mimetype in (attachments or []):
        message.attach(filename, file_content, mimetype)
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
    """
    Two separate emails, not one sent to both audiences: the client
    needs to know what this means for them and what happens next, the
    admin just needs a fast heads-up with a link to act on it. One
    generic paragraph trying to serve both ends up useless for either.
    """
    _send(
        subject=f"You signed {contract.title} — here's what's next",
        template_name="email/contract_signed_client.html",
        context={"contract": contract},
        to_emails=[contract.client_email],
    )
    admins = _admin_recipients()
    if admins:
        _send(
            subject=f"Contract signed — {contract.title}",
            template_name="email/contract_signed_admin.html",
            context={"contract": contract},
            to_emails=admins,
        )
    logger.info("Contract-signed emails sent for contract %s", contract.id)


def send_milestone_invoice_email(milestone, payment_url):
    _send(
        subject=f"Payment due — {milestone.title} ({milestone.contract.title})",
        template_name="email/contract_milestone_invoice.html",
        context={"milestone": milestone, "contract": milestone.contract, "payment_url": payment_url},
        to_emails=[milestone.contract.client_email],
    )
    logger.info("Milestone invoice email sent for milestone %s", milestone.id)


def send_milestone_funded_email(milestone):
    """Payment confirmed — money is held pending delivery. Replaces the
    old send_milestone_paid_email (see this module's docstring)."""
    contract = milestone.contract
    _send(
        subject=f"Payment received — {milestone.title} ({contract.title})",
        template_name="email/contract_milestone_funded.html",
        context={"milestone": milestone, "contract": contract},
        to_emails=[contract.client_email] + _admin_recipients(),
    )
    logger.info("Milestone funded email sent for milestone %s", milestone.id)


def send_milestone_submitted_email(milestone, deliverable):
    """Called by services.submit_milestone_deliverable() for BOTH a
    first submission and a resubmission after revision — the subject
    line and template adapt to deliverable.version so the client isn't
    told 'work submitted' as if it were the first time when it isn't."""
    contract = milestone.contract
    is_resubmission = deliverable.version > 1
    _send(
        subject=(
            f"Revised work submitted — {milestone.title} ({contract.title})" if is_resubmission
            else f"Work submitted for review — {milestone.title} ({contract.title})"
        ),
        template_name="email/contract_milestone_submitted.html",
        context={
            "milestone": milestone, "contract": contract, "deliverable": deliverable,
            "is_resubmission": is_resubmission,
        },
        to_emails=[contract.client_email],
    )
    logger.info("Milestone submitted email sent for milestone %s (version %s)", milestone.id, deliverable.version)


def send_milestone_revision_requested_email(milestone, note):
    """Client asked for changes — notifies the admin side (Scape is the
    sole provider, so this is an internal notification, not a client-
    facing one — see contracts/models.py's module docstring)."""
    contract = milestone.contract
    _send(
        subject=f"Revision requested — {milestone.title} ({contract.title})",
        template_name="email/contract_milestone_revision_requested.html",
        context={"milestone": milestone, "contract": contract, "note": note},
        to_emails=_admin_recipients(),
    )
    logger.info("Milestone revision-requested email sent for milestone %s", milestone.id)


def send_milestone_released_email(milestone, auto=False):
    """Funds recognized as earned — the actual 'money moment'. `auto`
    distinguishes an auto_release_milestone() sweep firing off the
    deadline from an explicit client approval, purely for the subject
    line/copy; the recipients and event are the same either way."""
    contract = milestone.contract
    subject = (
        f"Milestone auto-released — {milestone.title} ({contract.title})" if auto
        else f"Milestone released — {milestone.title} ({contract.title})"
    )
    _send(
        subject=subject,
        template_name="email/contract_milestone_released.html",
        context={"milestone": milestone, "contract": contract, "auto": auto},
        to_emails=[contract.client_email] + _admin_recipients(),
    )
    logger.info("Milestone released email sent for milestone %s (auto=%s)", milestone.id, auto)


def send_change_request_submitted_email(change_request):
    contract = change_request.contract
    _send(
        subject=f"Change requested — {contract.title}",
        template_name="email/contract_change_request_submitted.html",
        context={"change_request": change_request, "contract": contract},
        to_emails=_admin_recipients(),
    )
    logger.info("Change-request-submitted email sent for request %s", change_request.id)


def send_change_request_decided_email(change_request):
    contract = change_request.contract
    _send(
        subject=f"Your requested change was {change_request.status} — {contract.title}",
        template_name="email/contract_change_request_decided.html",
        context={"change_request": change_request, "contract": contract},
        to_emails=[contract.client_email],
    )
    logger.info("Change-request-decided email sent for request %s", change_request.id)


def send_dispute_raised_email(dispute):
    contract = dispute.contract
    _send(
        subject=f"Dispute filed — {contract.title}",
        template_name="email/contract_dispute_raised.html",
        context={"dispute": dispute, "contract": contract},
        to_emails=_admin_recipients(),
    )
    logger.info("Dispute-raised email sent for dispute %s", dispute.id)


def send_dispute_resolved_email(dispute):
    contract = dispute.contract
    _send(
        subject=f"Your dispute has been resolved — {contract.title}",
        template_name="email/contract_dispute_resolved.html",
        context={"dispute": dispute, "contract": contract},
        to_emails=[contract.client_email] + _admin_recipients(),
    )
    logger.info("Dispute-resolved email sent for dispute %s", dispute.id)


def send_invoice_sent_email(invoice, payment_url, pdf_bytes):
    """The PDF is attached directly (see contracts/invoice_pdf.py) — the
    client gets the full itemized document in-hand, not just a link
    that could break or be revoked independently of the email itself."""
    contract = invoice.contract
    _send(
        subject=f"Invoice {invoice.invoice_number} — {contract.title}",
        template_name="email/contract_invoice_sent.html",
        context={
            "invoice": invoice, "contract": contract, "payment_url": payment_url,
            "due_at": invoice.due_at.strftime("%d %B %Y"),
            "company_name": getattr(settings, "INVOICE_COMPANY_NAME", ""),
        },
        to_emails=[contract.client_email],
        attachments=[(f"{invoice.invoice_number}.pdf", pdf_bytes, "application/pdf")],
    )
    logger.info("Invoice-sent email sent for invoice %s", invoice.id)


def send_invoice_paid_email(invoice):
    contract = invoice.contract
    _send(
        subject=f"Payment received — Invoice {invoice.invoice_number} ({contract.title})",
        template_name="email/contract_invoice_paid.html",
        context={
            "invoice": invoice, "contract": contract,
            "paid_at": invoice.paid_at.strftime("%d %B %Y") if invoice.paid_at else "",
            "company_name": getattr(settings, "INVOICE_COMPANY_NAME", ""),
        },
        to_emails=[contract.client_email] + _admin_recipients(),
    )
    logger.info("Invoice-paid email sent for invoice %s", invoice.id)
