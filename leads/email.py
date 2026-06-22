import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.html import strip_tags

logger = logging.getLogger("leads")


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


def send_user_welcome(lead):
    """
    Branded confirmation email sent to the person who submitted the form.
    Sent from WELCOME_FROM_EMAIL (noreply@) but Reply-To points at
    REPLY_TO_EMAIL (info@), so the "just reply to this email" line in the
    template stays true even though the From address itself doesn't accept mail.
    """
    context = {"lead": lead, "site": settings.SITE_DOMAIN}
    _send_html_email(
        subject=f"We received your message, {lead.name.split()[0]}",
        template_name="email/user_welcome.html",
        context=context,
        to_emails=[lead.email],
        from_email=settings.WELCOME_FROM_EMAIL,
        reply_to=[settings.REPLY_TO_EMAIL],
    )
    logger.info("Welcome email sent to %s", lead.email)


def send_admin_notification(lead):
    """Internal notification with full lead detail, sent to the team from DEFAULT_FROM_EMAIL (info@)."""
    context = {"lead": lead, "site": settings.SITE_DOMAIN}
    _send_html_email(
        subject=f"New enquiry: {lead.name} — {lead.service}",
        template_name="email/admin_notification.html",
        context=context,
        to_emails=settings.ADMIN_NOTIFICATION_EMAILS,
        from_email=settings.DEFAULT_FROM_EMAIL,
    )
    logger.info("Admin notification sent for lead #%s", lead.id)


def sync_lead_to_brevo(lead):
    """Adds/updates the lead as a contact in Brevo. Non-fatal if it fails."""
    if not settings.BREVO_API_KEY:
        return

    import sib_api_v3_sdk
    from sib_api_v3_sdk.rest import ApiException

    configuration = sib_api_v3_sdk.Configuration()
    configuration.api_key["api-key"] = settings.BREVO_API_KEY
    api_instance = sib_api_v3_sdk.ContactsApi(sib_api_v3_sdk.ApiClient(configuration))

    name_parts = lead.name.strip().split(" ", 1)
    first_name = name_parts[0]
    last_name = name_parts[1] if len(name_parts) > 1 else ""

    contact = sib_api_v3_sdk.CreateContact(
        email=lead.email,
        attributes={
            "FIRSTNAME": first_name,
            "LASTNAME": last_name,
            "COMPANY": lead.company,
            "SMS": lead.phone,
            "SERVICE_INTEREST": lead.service,
        },
        list_ids=[settings.BREVO_CONTACT_LIST_ID],
        update_enabled=True,
    )

    try:
        api_instance.create_contact(contact)
        logger.info("Synced %s to Brevo contacts", lead.email)
        return True
    except ApiException as exc:
        logger.error("Brevo sync failed for %s: %s", lead.email, exc)
        return False
