"""
users/emails.py

Verification / welcome / password-reset emails. Each one is sent in the context
of ONE Scape app (property / portal / thesis...): links point at THAT app's
front-end and the branding matches it. Pass `source` to override (e.g. the app
the person is using right now); otherwise the account's signup_source is used.
All senders are fire-and-log: a delivery failure never breaks the request.
"""
import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.html import strip_tags

from .app_registry import (
    brand_for,
    build_url,
    normalize_source,
    default_source,
    show_free_reports,
)

logger = logging.getLogger(__name__)


def _source_of(obj, override=None):
    return normalize_source(override) or normalize_source(getattr(obj, "signup_source", None)) or default_source()


def _support_email():
    return getattr(settings, "REPLY_TO_EMAIL", "info@scapedatasolutions.com")


def _send(subject, template, context, to_email):
    try:
        html_body = render_to_string(template, context)
    except Exception:
        logger.exception("Could not render %s for %s", template, to_email)
        return False

    try:
        message = EmailMultiAlternatives(
            subject=subject,
            body=strip_tags(html_body),
            from_email=getattr(settings, "WELCOME_FROM_EMAIL", settings.DEFAULT_FROM_EMAIL),
            to=[to_email],
            reply_to=[getattr(settings, "REPLY_TO_EMAIL", settings.DEFAULT_FROM_EMAIL)],
        )
        message.attach_alternative(html_body, "text/html")
        message.send(fail_silently=False)
        logger.info("%s sent to %s", template, to_email)
        return True
    except Exception:
        logger.exception("Failed to send %s to %s", template, to_email)
        return False


def build_verification_url(raw_token, signup_id, source=None):
    return build_url(normalize_source(source) or default_source(), "verify", token=raw_token, id=signup_id)


def build_reset_url(uid, raw_token, source=None):
    return build_url(normalize_source(source) or default_source(), "reset", uid=uid, token=raw_token)


def send_verification_email(signup, raw_token, source=None):
    source = _source_of(signup, source)
    brand = brand_for(source)
    context = {
        "full_name": signup.full_name,
        "verification_url": build_verification_url(raw_token, signup.id, source),
        "expires_in_hours": 48,
        "support_email": _support_email(),
        "brand_name": brand,
    }
    return _send(f"Verify your {brand} account", "email/verification_email.html", context, signup.email)


def send_welcome_email(signup, source=None):
    source = _source_of(signup, source)
    brand = brand_for(source)
    context = {
        "full_name": signup.full_name,
        "free_reports": getattr(settings, "PROPERTY_REPORT_FREE_TIER_DISPLAY", 3),
        "show_free_reports": show_free_reports(source),
        "dashboard_url": build_url(source, "dashboard"),
        "support_email": _support_email(),
        "brand_name": brand,
    }
    return _send(f"Welcome to {brand}", "email/welcome_email.html", context, signup.email)


def send_password_reset_email(user, uid, raw_token, source=None):
    signup = getattr(user, "signup_profile", None)
    source = _source_of(signup, source)
    brand = brand_for(source)
    context = {
        "full_name": signup.full_name if signup else (user.first_name or user.email),
        "reset_url": build_reset_url(uid, raw_token, source),
        "support_email": _support_email(),
        "brand_name": brand,
    }
    return _send(f"Reset your {brand} password", "email/password_reset_email.html", context, user.email)
