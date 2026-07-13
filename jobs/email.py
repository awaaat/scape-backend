import logging
import threading

from django.conf import settings
from django.core.mail import EmailMessage, EmailMultiAlternatives, get_connection
from django.template.loader import render_to_string
from django.utils.html import strip_tags

logger = logging.getLogger("jobs")


def _send_html_email(subject, template_name, context, to_emails, from_email=None, reply_to=None):
    html_body = render_to_string(template_name, context)
    text_body = strip_tags(html_body)
    msg = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=from_email or settings.DEFAULT_FROM_EMAIL,
        to=to_emails,
        reply_to=reply_to or None,
    )
    msg.attach_alternative(html_body, "text/html")
    msg.send(fail_silently=False)


def _async(fn, *args, **kwargs):
    t = threading.Thread(target=fn, args=args, kwargs=kwargs, daemon=True)
    t.start()


def send_applicant_confirmation(application):
    context = {
        "application": application,
        "job": application.job,
        "site": settings.SITE_DOMAIN,
    }

    def _send():
        try:
            _send_html_email(
                subject=f"We received your application for {application.job.title}",
                template_name="email/application_received.html",
                context=context,
                to_emails=[application.email],
                from_email=getattr(settings, "WELCOME_FROM_EMAIL", settings.DEFAULT_FROM_EMAIL),
                reply_to=[getattr(settings, "REPLY_TO_EMAIL", settings.DEFAULT_FROM_EMAIL)],
            )
            logger.info("Application confirmation sent to %s", application.email)
        except Exception as exc:
            logger.error("Application confirmation failed for %s: %s", application.email, exc)

    _async(_send)


def send_admin_new_application(application):
    """
    Brevo-backed notification with the application details. Deliberately
    carries NO attachment — the resume itself goes via send_resume_to_gmail
    below, on a completely separate Gmail SMTP connection, so Brevo's
    attachment size / volume limits are never touched by resume files.
    """
    context = {
        "application": application,
        "job": application.job,
        "site": settings.SITE_DOMAIN,
    }

    def _send():
        try:
            _send_html_email(
                subject=f"New application: {application.full_name} — {application.job.title}",
                template_name="email/admin_new_application.html",
                context=context,
                to_emails=settings.ADMIN_NOTIFICATION_EMAILS,
                from_email=settings.DEFAULT_FROM_EMAIL,
            )
            logger.info("Admin notified of new application #%s", application.id)
        except Exception as exc:
            logger.error("Admin notification failed for application #%s: %s", application.id, exc)

    _async(_send)


def send_resume_to_gmail(application, resume_bytes, resume_filename, resume_content_type):
    """
    Sends the resume file straight to a Gmail inbox via a direct Gmail SMTP
    connection — deliberately bypasses Brevo (and its attachment/volume
    limits) since this is a one-off file delivery, not a transactional
    template email. The resume is never written to disk or any storage
    service; these bytes only ever live in memory for this one send.
    """
    if not resume_bytes:
        logger.warning("No resume bytes to send for application #%s — skipping Gmail send.", application.id)
        return

    if not settings.GMAIL_SMTP_USER or not settings.GMAIL_SMTP_APP_PASSWORD:
        logger.error("GMAIL_SMTP_USER / GMAIL_SMTP_APP_PASSWORD not set — cannot send resume.")
        return

    def _send():
        try:
            connection = get_connection(
                backend="django.core.mail.backends.smtp.EmailBackend",
                host="smtp.gmail.com",
                port=587,
                username=settings.GMAIL_SMTP_USER,
                password=settings.GMAIL_SMTP_APP_PASSWORD,
                use_tls=True,
            )
            msg = EmailMessage(
                subject=f"Resume: {application.full_name} — {application.job.title}",
                body=(
                    f"New application from {application.full_name} <{application.email}> "
                    f"for {application.job.title}.\n\nResume attached."
                ),
                from_email=settings.GMAIL_SMTP_USER,
                to=[settings.GMAIL_RESUME_RECIPIENT],
                connection=connection,
            )
            msg.attach(resume_filename or "resume", resume_bytes, resume_content_type or "application/octet-stream")
            msg.send(fail_silently=False)
            logger.info("Resume for application #%s sent to Gmail.", application.id)
        except Exception as exc:
            logger.error("Gmail resume send failed for application #%s: %s", application.id, exc)

    _async(_send)
