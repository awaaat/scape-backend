"""
projects/emails.py
Notification emails for the project lifecycle. Same
EmailMultiAlternatives + render_to_string pattern as every other app
(contracts/emails.py, users/emails.py, payments/emails.py) — uses
whatever EMAIL_BACKEND is configured (Anymail/Brevo) automatically.
"""
import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.html import strip_tags

logger = logging.getLogger("projects")


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


def send_project_submitted_email(project):
    """Notifies admins that a client posted a new project. Caller
    (services.create_project) wraps this in try/except — a delivery
    failure here must never break project creation itself, same
    fire-and-log posture as everywhere else in the codebase."""
    _send(
        subject=f"New project submitted — {project.title}",
        template_name="email/project_submitted.html",
        context={"project": project},
        to_emails=_admin_recipients(),
    )
    logger.info("Project-submitted notification sent for project %s", project.id)


def send_project_acknowledgment_email(project):
    """Client-facing 'thanks, we got it' confirmation — sent the
    moment a project is submitted. Caller (services.create_project)
    wraps this in try/except, same posture as
    send_project_submitted_email above."""
    _send(
        subject=f"We received your project — {project.title}",
        template_name="email/project_acknowledgment.html",
        context={"project": project},
        to_emails=[project.owner.email],
    )
    logger.info("Project-acknowledgment email sent for project %s to %s", project.id, project.owner.email)


def send_new_message_notification(message):
    """Caller (services.post_project_message) wraps this in try/except —
    same posture as contracts.emails.send_new_message_notification."""
    project = message.project
    if message.sender_type == "client":
        to_emails = _admin_recipients()
        subject = f"New message from {message.sender_name or project.owner.get_username()} — {project.title}"
    else:
        to_emails = [project.owner.email]
        subject = f"New message on your project — {project.title}"
    _send(
        subject=subject,
        template_name="email/project_new_message.html",
        context={"project": project, "message": message},
        to_emails=to_emails,
    )
    logger.info("New-message notification sent for project %s message %s", project.id, message.id)
