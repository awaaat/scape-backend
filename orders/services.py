"""
orders/services.py

The order funnel: create a draft, save it step by step, submit it, and sweep
abandoned drafts. Views stay thin; every rule lives here.

Why drafts exist: the moment a visitor picks a service and gives an email/phone
the order is recoverable. If they close the tab we still have them, notify you
('abandoned'), and send them a one-click resume link.
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from . import catalog, emails
from .models import Order

logger = logging.getLogger("orders")

SIMPLE_FIELDS = [
    "name", "email", "phone", "country", "academic_level", "data_status",
    "software", "deadline_window", "deadline", "topic", "details",
]


# ── create / save ────────────────────────────────────────────────────
def create_order(data, request=None):
    visitor = getattr(request, "visitor", None) if request is not None else None
    site = catalog.resolve_site(request, data.get("site"))
    for _ in range(3):  # reference collisions are astronomically unlikely, but be safe
        try:
            order = Order.objects.create(
                visitor=visitor, site=site["key"],
                source_url=(data.get("page_url") or "")[:500],
                referrer=(data.get("referrer") or "")[:500],
                utm=data.get("utm") or {},
            )
            break
        except IntegrityError:
            continue
    else:  # pragma: no cover
        raise RuntimeError("Could not allocate an order reference.")
    return apply_fields(order, data)


def apply_fields(order, data):
    for f in SIMPLE_FIELDS:
        if f in data:
            val = data[f]
            setattr(order, f, val if val is not None else (None if f == "deadline" else ""))
    if data.get("service"):
        order.service_slug, order.service = catalog.resolve_service(order.site_info, data["service"])
    if "answers" in data:
        merged = {**(order.extra_answers or {}), **data["answers"]}
        order.extra_answers = {k: v for k, v in merged.items() if v is not None}
    if "step" in data:
        order.current_step = data["step"]
    order.last_activity_at = timezone.now()
    order.save()
    _flag_visitor_as_lead(order)
    return order


def touch(order):
    Order.objects.filter(pk=order.pk).update(last_activity_at=timezone.now())


def _flag_visitor_as_lead(order):
    if order.visitor_id and (order.email or order.phone):
        from visitors.models import Visitor
        Visitor.objects.filter(pk=order.visitor_id, is_lead=False).update(
            is_lead=True, lead_created_at=timezone.now()
        )


# ── submit ───────────────────────────────────────────────────────────
_MISSING_MSG = {
    "name": "Please tell us your name.",
    "email": "We need your email to send the quote.",
    "service": "Please choose what you need help with.",
}


def missing_fields(values, required=("name", "email", "service")):
    """`values` is any dict-like. `required` comes from the site config. Returns {} when OK."""
    errors = {}
    for f in required:
        v = values.get(f)
        v = v.strip() if isinstance(v, str) else v
        too_short = f == "name" and len(v or "") < 2
        if not v or too_short:
            errors[f] = [_MISSING_MSG.get(f, "This field is required.")]
    return errors


def order_missing_fields(order):
    site = order.site_info
    values = {f: getattr(order, f, None) or (order.extra_answers or {}).get(f) for f in site["required"]}
    return missing_fields(values, site["required"])


def submit_order(order):
    """Idempotent. Returns (order, first_time). Only the first call notifies."""
    with transaction.atomic():
        locked = Order.objects.select_for_update().get(pk=order.pk)
        if locked.status != Order.Status.DRAFT:
            return locked, False
        now = timezone.now()
        locked.status = Order.Status.SUBMITTED
        locked.submitted_at = now
        locked.last_activity_at = now
        locked.save(update_fields=["status", "submitted_at", "last_activity_at", "updated_at"])
    emails.notify_submitted(locked)
    return locked, True


# ── sweeper (run by Celery beat or `manage.py sweep_orders`) ─────────
def sweep_abandoned_orders(now=None):
    now = now or timezone.now()
    idle = timedelta(minutes=getattr(settings, "ORDER_ABANDON_MINUTES", 30))
    stats = {"abandoned": 0, "recovery": 0, "retried": 0, "converted": 0}

    drafts = (
        Order.objects.filter(
            status=Order.Status.DRAFT,
            abandoned_notified=False,
            last_activity_at__lt=now - idle,
            last_activity_at__gte=now - timedelta(days=14),
        )
        .exclude(email="", phone="")
        .select_related("visitor")
    )
    for order in drafts:
        # Claim atomically so two overlapping sweeps never double-notify.
        if not Order.objects.filter(pk=order.pk, abandoned_notified=False).update(abandoned_notified=True):
            continue
        if order.email and Order.objects.filter(
            email__iexact=order.email, submitted_at__gte=order.created_at,
        ).exclude(status=Order.Status.DRAFT).exists():
            stats["converted"] += 1  # same person finished another order — don't nag
            continue
        try:
            emails.send_admin_email(order, "abandoned")
            stats["abandoned"] += 1
        except Exception:
            logger.exception("Abandoned-order admin email failed for %s", order.reference)
        if order.email and Order.objects.filter(pk=order.pk, recovery_sent=False).update(recovery_sent=True):
            try:
                emails.send_client_recovery_email(order)
                stats["recovery"] += 1
            except Exception:
                logger.exception("Recovery email failed for %s", order.reference)

    # Safety net: a submitted order whose admin email never went out gets retried.
    stuck = Order.objects.filter(
        status=Order.Status.SUBMITTED, admin_notified=False,
        submitted_at__lt=now - timedelta(minutes=3),
        submitted_at__gte=now - timedelta(days=2),
    ).select_related("visitor")
    for order in stuck:
        try:
            emails.send_admin_email(order, "submitted")
            stats["retried"] += 1
        except Exception:
            logger.exception("Retry of admin notification failed for %s", order.reference)

    if any(stats.values()):
        logger.info("Order sweep: %s", stats)
    return stats
