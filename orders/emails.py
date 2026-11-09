"""
orders/emails.py

Three emails:
  • admin  — the whole order packaged in one message (kind "submitted" or
             "abandoned"). Reply-To is the CLIENT, so hitting Reply in your
             inbox writes straight to them; there's a WhatsApp button too.
  • client — "we got your order" confirmation.
  • client — "you were one step away" resume link (abandoned drafts only).

Sending never blocks the request: messages are RENDERED in the request thread
(so DB reads happen safely there) and SENT in a daemon thread. Set
ORDERS_SYNC_EMAIL=True to send inline (tests, debugging).
"""
import logging
import threading
from types import SimpleNamespace

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import connection
from django.template.loader import render_to_string

from .models import Order

logger = logging.getLogger("orders")


def _admin_recipients(order=None):
    """Per-site override first, then ORDER_NOTIFICATION_EMAILS, then the
    project-wide ADMIN_NOTIFICATION_EMAILS."""
    site_list = order.site_info.get("notify_emails") if order is not None else None
    return list(
        site_list
        or getattr(settings, "ORDER_NOTIFICATION_EMAILS", None)
        or getattr(settings, "ADMIN_NOTIFICATION_EMAILS", [])
        or []
    )


def _clean(s):
    return " ".join(str(s).split())  # headers must never contain newlines


def _run(fn):
    if getattr(settings, "ORDERS_SYNC_EMAIL", False):
        try:
            fn()
        except Exception:
            logger.exception("Order email failed")
        return

    def wrapper():
        try:
            fn()
        except Exception:
            logger.exception("Order email failed")
        finally:
            connection.close()

    threading.Thread(target=wrapper, daemon=True).start()


def _build(subject, text, html, to, reply_to=None):
    msg = EmailMultiAlternatives(
        subject=_clean(subject)[:200],
        body=text,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=to,
        reply_to=reply_to or None,
    )
    msg.attach_alternative(html, "text/html")
    return msg


def resume_url(order):
    site = order.site_info
    base = (site["frontend_url"] or getattr(settings, "ORDER_FRONTEND_URL", "")).rstrip("/")
    return f"{base}{site['resume_path']}?resume={order.public_token}"


# ── admin ────────────────────────────────────────────────────────────
def _admin_subject(order, kind):
    if kind == "abandoned":
        tag = "ABANDONED ORDER"
    else:
        tag = "URGENT ORDER" if order.is_urgent else "NEW ORDER"
    bits = [f"[{tag} · {order.site_info['key']}] {order.reference}", order.display_name, order.service or "no service chosen"]
    if order.deadline:
        bits.append(f"due {order.deadline:%d %b} ({order.days_left}d)")
    if kind == "abandoned":
        bits.append(f"stopped at step {order.current_step + 1}")
    return " · ".join(bits)


def send_admin_email(order, kind="submitted"):
    to = _admin_recipients(order)
    if not to:
        logger.warning("ORDER_NOTIFICATION_EMAILS is empty — nothing sent for %s", order.reference)
        return
    if kind == "abandoned":
        accent, badge = "#9a6b00", "Started but did not finish"
        headline = "Hot lead going cold: follow up now"
    elif order.is_urgent:
        accent, badge = "#b42318", "Urgent: deadline within 3 days"
        headline = "New order, tight deadline"
    else:
        accent, badge = "#0d2b6b", "New order"
        headline = "New order received"

    admin_base = (getattr(settings, "ORDERS_ADMIN_BASE_URL", "") or settings.SITE_DOMAIN).rstrip("/")
    ctx = {
        "order": order, "site": order.site_info, "kind": kind, "accent": accent, "badge": badge, "headline": headline,
        "rows": order.summary_rows(), "attribution": order.attribution_rows(),
        "files": list(order.files.all()),
        "admin_url": f"{admin_base}/admin/orders/order/{order.pk}/change/",
        "resume_url": resume_url(order),
    }
    msg = _build(
        _admin_subject(order, kind),
        f"{badge}\n\n{order.as_text()}\n\nAdmin: {ctx['admin_url']}",
        render_to_string("email/order_admin.html", ctx),
        to,
        reply_to=[order.email] if order.email else None,
    )
    pk, flag = order.pk, ("admin_notified" if kind == "submitted" else None)

    def go():
        msg.send(fail_silently=False)
        if flag:
            Order.objects.filter(pk=pk).update(**{flag: True})
        logger.info("Admin %s email sent for %s", kind, order.reference)

    _run(go)


# ── client ───────────────────────────────────────────────────────────
def send_client_received_email(order):
    if not order.email:
        return
    ctx = {"order": order, "rows": order.summary_rows(), "site": order.site_info}
    text = (
        f"Hi {order.name.split()[0] if order.name else 'there'},\n\n"
        f"We've received your order {order.reference} at {order.site_info['name']}. We'll reply within 24 hours with scope, "
        f"timeline and price. You pay nothing until you agree.\n\n{order.as_text()}\n\n"
        f"Need to add files or details? Just reply to this email."
    )
    msg = _build(
        f"We received your order {order.reference}",
        text, render_to_string("email/order_client_received.html", ctx),
        [order.email], reply_to=[settings.REPLY_TO_EMAIL],
    )
    pk = order.pk

    def go():
        msg.send(fail_silently=False)
        Order.objects.filter(pk=pk).update(confirmation_sent=True)

    _run(go)


def send_client_recovery_email(order):
    if not order.email:
        return
    ctx = {"order": order, "resume_url": resume_url(order)}
    first = order.name.split()[0] if order.name else "there"
    text = (
        f"Hi {first},\n\nYou started an order with us ({order.service or 'data analysis help'}) "
        f"but didn't finish. Pick up exactly where you left off: {ctx['resume_url']}\n\n"
        f"Or just reply to this email with what you need and we'll take it from there."
    )
    msg = _build(
        f"{first}, your order is one step from done",
        text, render_to_string("email/order_client_recovery.html", ctx),
        [order.email], reply_to=[settings.REPLY_TO_EMAIL],
    )
    _run(lambda: msg.send(fail_silently=False))


# ── orchestration ────────────────────────────────────────────────────
def _sync_brevo(order):
    """Reuses the existing leads helper so submitted orders land in the same
    Brevo contact list. Tolerant: never lets a CRM hiccup touch the order."""
    try:
        from leads.email import sync_lead_to_brevo
        sync_lead_to_brevo(SimpleNamespace(
            name=order.name, email=order.email, company=order.academic_level,
            phone=order.phone, service=order.service,
        ))
    except Exception:
        logger.exception("Brevo sync failed for %s", order.reference)


def notify_submitted(order):
    """Called once, right after an order flips to SUBMITTED."""
    for fn, label in (
        (lambda: send_admin_email(order, "submitted"), "admin"),
        (lambda: send_client_received_email(order), "client"),
        (lambda: _sync_brevo(order), "brevo"),
    ):
        try:
            fn()
        except Exception:
            logger.exception("Submit notification (%s) failed for %s", label, order.reference)
