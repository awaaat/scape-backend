"""
agreements/emails.py

Notification emails for the agreement lifecycle. Same
EmailMultiAlternatives + render_to_string pattern as every other app
(contracts/emails.py, payments/emails.py, users/emails.py, leads/
email.py) — uses whatever EMAIL_BACKEND is configured (Anymail/Brevo)
automatically. Four functions, one per existing template in
agreements/templates/email/ — no fifth template invented here.

Two of the four are deliberately smarter than a plain "render and send"
would be, because the naive version of each is actually wrong for a
legal-document app:

  send_agreement_signed_email() — a client signature isn't necessarily
  the end of the story (see Agreement.is_fully_executed). Telling the
  client "signed!" while a required countersignature is still pending
  would misstate what actually happened — the document isn't binding
  yet. So this branches: fully executed -> the client gets told it's
  done, admins are bcc'd for the record; still awaiting countersignature
  -> ONLY admins are told ("your countersignature is needed"), because
  the client already knows they signed and doesn't need a second email
  about it that overstates where things stand. One template, one
  function, state-aware recipients and copy.

  send_agreement_voided_email() — an Agreement voided before it was
  ever sent (sent_at is None — an abandoned draft, a duplicate created
  by mistake) has no business emailing a client who never knew the
  document existed. Once it HAS been sent, the client is always
  included, since a document they may have been about to sign
  deserves an explicit explanation rather than silence.

send_agreement_expiring_soon_email() adds days_remaining and
is_final_notice to context so a single template can read as a gentle
nudge with days to spare and an urgent one on the last day, without
services.py or this module hardcoding two different copies for that.
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.html import strip_tags

logger = logging.getLogger("agreements")


def _admin_recipients():
    return list(getattr(settings, "ADMIN_NOTIFICATION_EMAILS", []) or [])


def _reply_to():
    # Falls back through AGREEMENTS_REPLY_TO_EMAIL -> the site-wide
    # REPLY_TO_EMAIL -> DEFAULT_FROM_EMAIL, same getattr-with-fallback
    # convention pdf.py already uses for AGREEMENTS_COMPANY_NAME —
    # lets a legal/ops inbox be configured for this app specifically
    # without requiring it.
    return getattr(
        settings, "AGREEMENTS_REPLY_TO_EMAIL",
        getattr(settings, "REPLY_TO_EMAIL", settings.DEFAULT_FROM_EMAIL),
    )


def _send(subject, template_name, context, to_emails, bcc=None):
    if not to_emails:
        return
    html_body = render_to_string(template_name, context)
    message = EmailMultiAlternatives(
        subject=subject,
        body=strip_tags(html_body),
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=to_emails,
        bcc=bcc or None,
        reply_to=[_reply_to()],
    )
    message.attach_alternative(html_body, "text/html")
    message.send(fail_silently=False)


def send_agreement_invitation_email(agreement, link):
    """Sent once per send_agreement_to_client() call — first send AND
    every resend, so a client whose link expired gets a working one
    without needing to ask."""
    _send(
        subject=f"{agreement.get_agreement_type_display()} ready for your signature — {agreement.title}",
        template_name="email/agreement_invitation.html",
        context={"agreement": agreement, "link": link},
        to_emails=[agreement.client_email],
    )
    logger.info("Agreement invitation email sent for agreement %s to %s", agreement.id, agreement.client_email)


def send_agreement_signed_email(agreement):
    """
    See module docstring — recipients and subject both depend on
    Agreement.is_fully_executed, not just on the fact that a signature
    was just recorded.
    """
    fully_executed = agreement.is_fully_executed

    if fully_executed:
        subject = f"Fully executed — {agreement.title}"
        to_emails = [agreement.client_email]
        bcc = _admin_recipients()
    else:
        subject = f"{agreement.client_name} has signed — your countersignature is needed on {agreement.title}"
        to_emails = _admin_recipients()
        bcc = None

    if not to_emails:
        return

    _send(
        subject=subject,
        template_name="email/agreement_signed.html",
        context={"agreement": agreement, "fully_executed": fully_executed},
        to_emails=to_emails,
        bcc=bcc,
    )
    logger.info(
        "Agreement-signed email sent for agreement %s (fully_executed=%s) to %s",
        agreement.id, fully_executed, to_emails,
    )


def send_agreement_voided_email(agreement):
    """See module docstring — the client is only included if this
    Agreement was ever actually sent to them."""
    ever_sent = agreement.sent_at is not None
    to_emails = _admin_recipients()
    if ever_sent:
        to_emails = [agreement.client_email] + to_emails

    if not to_emails:
        return

    _send(
        subject=f"Voided — {agreement.title}",
        template_name="email/agreement_voided.html",
        context={"agreement": agreement, "ever_sent": ever_sent},
        to_emails=to_emails,
    )
    logger.info("Agreement-voided email sent for agreement %s (ever_sent=%s) to %s", agreement.id, ever_sent, to_emails)


def send_agreement_expiring_soon_email(agreement):
    """
    services.send_expiring_soon_reminders() already guards against
    sending this more than once per day for a given Agreement (see its
    docstring) — this function doesn't re-check that itself, since
    dedup is a scheduling concern that belongs in the one place that
    decides *when* to call this, not in the sender.
    """
    now = timezone.now()
    delta = agreement.expires_at - now
    days_remaining = max(delta.days, 0)
    is_final_notice = delta <= timedelta(days=1)

    subject = ("Final notice: " if is_final_notice else "") + f"Signature needed soon — {agreement.title}"

    _send(
        subject=subject,
        template_name="email/agreement_expiring_soon.html",
        context={"agreement": agreement, "days_remaining": days_remaining, "is_final_notice": is_final_notice},
        to_emails=[agreement.client_email],
    )
    logger.info(
        "Expiring-soon email sent for agreement %s (days_remaining=%s, final_notice=%s)",
        agreement.id, days_remaining, is_final_notice,
    )