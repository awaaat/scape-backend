"""
orders/engagement_hooks.py

Wires the engagement layer into the EXISTING orders code without editing it.
install() is called once from OrdersConfig.ready() (see the block appended to
orders/apps.py). It does five small, individually-guarded things:

  1. post_save(Order)        - intent alert when contact details are captured;
                               stops reminders + stamps "recovered" on submit.
  2. OrderQuickView.post     - wrapped so one-shot quick orders (which create
                               AND submit in a single call) don't also fire an
                               "order started" alert on top of the "new order".
  3. emails.send_client_recovery_email - wrapped to honour the do-not-email list.
  4. Order.attribution_rows  - wrapped to append a "Journey" line (lead score,
                               "RECOVERED by reminder #2") to the admin emails.
  5. Anymail tracking signal - hard bounces / spam complaints / unsubscribes
                               reported by Brevo go on the do-not-email list.

Every patch is idempotent and fails open: if anything here throws, the order
flow itself carries on untouched.
"""
import contextvars
import functools
import logging
import os
import sys
from datetime import timedelta

from django.conf import settings
from django.core.checks import Warning as CheckWarning
from django.core.checks import register
from django.db import transaction
from django.db.models.signals import post_save
from django.utils import timezone

from . import engagement_intel as intel
from . import engagement_notifications as notify
from .engagement_models import EmailSuppression, OrderEngagement

logger = logging.getLogger("orders")

# True while OrderQuickView.post is running (see _patch_quick_view).
QUICK_ORDER = contextvars.ContextVar("orders_quick_order", default=False)
_installed = False


def install():
    global _installed
    if _installed:
        return
    _installed = True
    from .models import Order

    post_save.connect(on_order_saved, sender=Order, dispatch_uid="orders_engagement_on_order_saved")
    for patch in (_patch_quick_view, _patch_detail_view, _guard_legacy_recovery,
                  _patch_attribution_rows, _connect_anymail, _register_checks, _maybe_start_ticker):
        try:
            patch()
        except Exception:  # pragma: no cover - never let a hook break startup
            logger.exception("Engagement hook %s failed to install", patch.__name__)


# ═════════════════════════════════════════════════════════════════════
#  1. post_save
# ═════════════════════════════════════════════════════════════════════
def on_order_saved(sender, instance, created=False, raw=False, **kwargs):
    if raw or not intel.engagement_enabled():
        return
    try:
        if instance.status == instance.Status.DRAFT:
            _on_draft_saved(instance)
        else:
            _on_order_closed(instance)
    except Exception:
        logger.exception("Engagement hook failed for order %s", instance.pk)


def _on_draft_saved(order):
    if not order.email or not intel.conf("ORDER_INTENT_ALERTS_ENABLED"):
        return
    eng = OrderEngagement.for_order(order)
    if eng.intent_notified_at or eng.intent_skipped_reason:
        return
    if QUICK_ORDER.get():
        OrderEngagement.objects.filter(
            pk=eng.pk, intent_notified_at__isnull=True, intent_skipped_reason="",
        ).update(intent_skipped_reason="quick_submit")
        return
    # Contact details arrive together on the contact step. If the name is
    # missing we wait: the sweeper sends the alert after a short grace period.
    if len((order.name or "").strip()) >= 2:
        pk = order.pk
        transaction.on_commit(lambda: notify.dispatch_intent(pk))


def _on_order_closed(order):
    """Submitted / spam / any non-draft status: stop reminders for good and,
    if a reminder preceded the submission, stamp it as the recovery."""
    eng = OrderEngagement.objects.filter(order_id=order.pk).first()
    if eng is None:
        return
    stop = "spam" if order.status == order.Status.SPAM else "submitted"
    fields = {}
    if eng.stop_reason != stop:
        fields["stop_reason"] = stop
    if eng.next_reminder_at is not None:
        fields["next_reminder_at"] = None
    if (stop == "submitted" and eng.recovered_by_stage is None and eng.lifetime_reminders and eng.last_reminder_at
            and timezone.now() - eng.last_reminder_at <= timedelta(hours=72)):
        fields["recovered_by_stage"] = eng.reminders_sent or eng.lifetime_reminders
    if fields:
        OrderEngagement.objects.filter(pk=eng.pk).update(**fields)
        for k, v in fields.items():
            setattr(eng, k, v)
        if stop == "submitted":
            intel.refresh_score(order, eng, force=True)  # final score for the "new order" email


# ═════════════════════════════════════════════════════════════════════
#  2 + 2b. view wrappers
# ═════════════════════════════════════════════════════════════════════
def _patch_quick_view():
    from . import views

    cls = views.OrderQuickView
    original = cls.post
    if getattr(original, "_engagement_wrapped", False):
        return

    @functools.wraps(original)
    def post(self, request, *args, **kwargs):
        token = QUICK_ORDER.set(True)
        try:
            return original(self, request, *args, **kwargs)
        finally:
            QUICK_ORDER.reset(token)

    post._engagement_wrapped = True
    cls.post = post


def _patch_detail_view():
    """Loading a draft (GET /api/orders/<token>/) means the client is back:
    cancel any pending 'they left' signal."""
    from . import views

    cls = views.OrderDetailView
    original = cls.get
    if getattr(original, "_engagement_wrapped", False):
        return

    @functools.wraps(original)
    def get(self, request, token, *args, **kwargs):
        response = original(self, request, token, *args, **kwargs)
        try:
            OrderEngagement.objects.filter(
                order__public_token=token, exit_signalled_at__isnull=False,
            ).update(exit_signalled_at=None)
        except Exception:
            logger.exception("Could not clear exit signal")
        return response

    get._engagement_wrapped = True
    cls.get = get


# ═════════════════════════════════════════════════════════════════════
#  3. do-not-email guard on the legacy recovery email
# ═════════════════════════════════════════════════════════════════════
def _guard_legacy_recovery():
    from . import emails

    original = emails.send_client_recovery_email
    if getattr(original, "_engagement_guarded", False):
        return

    @functools.wraps(original)
    def guarded(order):
        reason = EmailSuppression.reason_for(order.email)
        if reason:
            logger.info("Recovery email to %s suppressed (%s)", order.email, reason)
            return None
        return original(order)

    guarded._engagement_guarded = True
    emails.send_client_recovery_email = guarded


# ═════════════════════════════════════════════════════════════════════
#  4. journey line in admin emails
# ═════════════════════════════════════════════════════════════════════
def _patch_attribution_rows():
    from .models import Order

    original = Order.attribution_rows
    if getattr(original, "_engagement_patched", False):
        return

    @functools.wraps(original)  # keeps __wrapped__, used by notifications to get the plain rows
    def attribution_rows(self):
        rows = original(self)
        try:
            if intel.engagement_enabled() and self.pk:
                line = intel.journey_summary(self)
                if line:
                    rows = [*rows, ("Journey", line)]
        except Exception:
            logger.exception("Journey row failed for order %s", self.pk)
        return rows

    attribution_rows._engagement_patched = True
    Order.attribution_rows = attribution_rows


# ═════════════════════════════════════════════════════════════════════
#  5. bounces / complaints / unsubscribes from Brevo (via Anymail)
# ═════════════════════════════════════════════════════════════════════
def on_anymail_event(sender, event, esp_name=None, **kwargs):
    try:
        etype = getattr(event, "event_type", "")
        raw = getattr(event, "esp_event", None)
        raw_type = raw.get("event", "") if isinstance(raw, dict) else ""
        if etype == "complained":
            reason = EmailSuppression.Reason.COMPLAINED
        elif etype == "unsubscribed":
            reason = EmailSuppression.Reason.UNSUBSCRIBED
        elif etype in ("bounced", "rejected"):
            if raw_type == "soft_bounce":
                return  # temporary (full mailbox etc.): not grounds for a permanent block
            reason = EmailSuppression.Reason.BOUNCED
        else:
            return
        recipient = getattr(event, "recipient", "")
        if EmailSuppression.add(recipient, reason, source="webhook", note=f"{esp_name or ''} {raw_type}".strip()):
            logger.info("Suppressed %s after %s event", recipient, etype)
    except Exception:
        logger.exception("Anymail event handling failed")


def _connect_anymail():
    try:
        from anymail.signals import tracking
    except Exception:  # anymail not installed / misconfigured
        return
    tracking.connect(on_anymail_event, dispatch_uid="orders_engagement_anymail_events")


# ═════════════════════════════════════════════════════════════════════
#  system checks (shown by `manage.py check` and at startup)
# ═════════════════════════════════════════════════════════════════════
def _register_checks():
    @register()
    def orders_engagement_checks(app_configs=None, **kwargs):
        problems = []
        if not intel.engagement_enabled():
            return problems
        if not intel.api_base():
            problems.append(CheckWarning(
                "ORDERS_API_BASE_URL is not set.",
                hint="Set it to this backend's public URL (e.g. https://api.scapedatasolutions.com). Until then reminder "
                     "emails use direct resume links (no click tracking) and a mailto-only unsubscribe, with no one-click "
                     "unsubscribe page.",
                id="orders.W001",
            ))
        if not getattr(settings, "ORDER_NOTIFICATION_EMAILS", None) and not getattr(settings, "ADMIN_NOTIFICATION_EMAILS", None):
            problems.append(CheckWarning("No admin notification recipients are configured.",
                                         hint="Set ORDER_NOTIFICATION_EMAILS.", id="orders.W002"))
        return problems


# ═════════════════════════════════════════════════════════════════════
#  optional in-process scheduler
# ═════════════════════════════════════════════════════════════════════
def _is_server_process():
    argv0 = os.path.basename(sys.argv[0] or "")
    if any(name in argv0 for name in ("gunicorn", "daphne", "uvicorn", "hypercorn")):
        return True
    if len(sys.argv) > 1 and sys.argv[1] == "runserver":
        return os.environ.get("RUN_MAIN") == "true" or "--noreload" in sys.argv
    return False


def _maybe_start_ticker():
    if not (getattr(settings, "ORDER_ENGINE_INPROCESS", False) and intel.engagement_enabled() and _is_server_process()):
        return
    from . import engagement_engine
    if engagement_engine.start_ticker(interval=int(getattr(settings, "ORDER_ENGINE_INTERVAL_SECONDS", 120))):
        logger.info("Order engagement ticker started (in-process scheduler)")
