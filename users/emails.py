"""
users/emails.py

Sends the signup verification email using the Anymail/Brevo backend
already configured in backend/settings.py (EMAIL_BACKEND =
"anymail.backends.brevo.EmailBackend"). No custom Brevo client needed —
that's why verification emails were silently failing before ("Brevo
client not wired up yet" in your logs was this exact import failing).
"""
import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.html import strip_tags

logger = logging.getLogger(__name__)


def build_verification_url(raw_token, signup_id):
    base_url = getattr(settings, "FRONTEND_BASE_URL", "").rstrip("/")
    return f"{base_url}/verify-email?token={raw_token}&id={signup_id}"


def send_welcome_email(signup):
    """
    Fired once, right after email verification succeeds (see
    VerifyEmailView.post in views.py). Fire-and-log like
    send_verification_email — a delivery failure here must never break
    the verification confirmation itself.
    """
    context = {
        "full_name": signup.full_name,
        "free_reports": getattr(settings, "PROPERTY_REPORT_FREE_TIER_DISPLAY", 3),
        "dashboard_url": f"{getattr(settings, 'FRONTEND_BASE_URL', '').rstrip('/')}/dashboard",
        "support_email": getattr(settings, "REPLY_TO_EMAIL", "info@scapedatasolutions.com"),
    }

    try:
        html_body = render_to_string("email/welcome_email.html", context)
    except Exception:
        logger.exception("Could not render welcome email template for %s", signup.email)
        return False

    text_body = strip_tags(html_body)

    try:
        message = EmailMultiAlternatives(
            subject="Welcome to Scape Property Intelligence",
            body=text_body,
            from_email=getattr(settings, "WELCOME_FROM_EMAIL", settings.DEFAULT_FROM_EMAIL),
            to=[signup.email],
            reply_to=[getattr(settings, "REPLY_TO_EMAIL", settings.DEFAULT_FROM_EMAIL)],
        )
        message.attach_alternative(html_body, "text/html")
        message.send(fail_silently=False)
        logger.info("Welcome email sent to %s", signup.email)
        return True
    except Exception:
        logger.exception("Failed to send welcome email to %s", signup.email)
        return False


def send_verification_email(signup, raw_token):
    """
    Fire-and-log: a delivery failure here must never break the signup
    itself. Returns True/False for whether the send call completed
    without raising (not a delivery guarantee).
    """
    verification_url = build_verification_url(raw_token, signup.id)

    context = {
        "full_name": signup.full_name,
        "verification_url": verification_url,
        "expires_in_hours": 48,
        "support_email": getattr(settings, "REPLY_TO_EMAIL", "info@scapedatasolutions.com"),
    }

    try:
        html_body = render_to_string("email/verification_email.html", context)
    except Exception:
        logger.exception("Could not render verification email template for %s", signup.email)
        return False

    text_body = strip_tags(html_body)

    try:
        message = EmailMultiAlternatives(
            subject="Verify your Scape Data Solutions account",
            body=text_body,
            from_email=getattr(settings, "WELCOME_FROM_EMAIL", settings.DEFAULT_FROM_EMAIL),
            to=[signup.email],
            reply_to=[getattr(settings, "REPLY_TO_EMAIL", settings.DEFAULT_FROM_EMAIL)],
        )
        message.attach_alternative(html_body, "text/html")
        message.send(fail_silently=False)
        logger.info("Verification email sent to %s", signup.email)
        return True
    except Exception:
        logger.exception("Failed to send verification email to %s", signup.email)
        return False
