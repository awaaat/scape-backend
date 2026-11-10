"""
orders/engagement_engine.py

The scheduler. One entry point, run_engine(), is called every ~2 minutes by
Celery beat (orders.tasks.run_order_engagement_task), by cron
(`manage.py run_order_engagement`) or by the optional in-process ticker. It is
safe to run concurrently and to run twice: every side effect is guarded by an
ATOMIC CLAIM (UPDATE ... WHERE flag is unset), so overlapping workers can
never double-send.

Steps, in order:
  1. fire_pending_intents    - intent alerts the request hook missed (name
                               arrived late, thread was killed, mail failed)
  2. process_exit_signals    - "they left" beacon -> abandoned alert after a
                               short grace instead of waiting 30 minutes; then
                               the client recovery email once 30 minutes pass
  3. process_reminders       - the reminder ladder (evaluate() decides)
  4. process_escalations     - hot lead still pending / clicked but didn't finish

evaluate() is a pure decision function: given an order + its engagement row +
"now" it returns send / wait / stop with a reason. Everything the dry-run
command prints comes from it.
"""
import logging
import random
import threading
import time
from collections import Counter, namedtuple
from datetime import timedelta

from django.db import connection
from django.db.models import F
from django.utils import timezone

from . import emails
from . import engagement_intel as intel
from . import engagement_notifications as notify
from .engagement_models import EmailSuppression, OrderEngagement, OrderReminderLog
from .models import Order

logger = logging.getLogger("orders")

# Stop reasons that are never re-evaluated. Everything else (superseded,
# deadline_passed, exhausted, suppressed...) is cheap to re-check and can
# reverse, e.g. the client returns, edits their deadline, or you clear a
# suppression.
TERMINAL_STOPS = {"submitted", "spam", "expired", "send_failed"}
MAX_SEND_FAILURES = 4
BACKOFF_MINUTES = (15, 60, 240, 720)

Decision = namedtuple("Decision", "action reason stage total urgent variant due_at reset")


def _stop(reason):
    return Decision("stop", reason, 0, 0, False, "", None, False)


def _wait(reason, due_at=None):
    return Decision("wait", reason, 0, 0, False, "", due_at, False)


class Report:
    def __init__(self, dry_run=False):
        self.dry_run = dry_run
        self.counts = Counter()
        self.notes = []

    def add(self, key, n=1):
        self.counts[key] += n

    def note(self, text):
        self.notes.append(text)

    def as_dict(self):
        out = dict(self.counts)
        if self.dry_run:
            out["notes"] = list(self.notes)
        return out


# ═════════════════════════════════════════════════════════════════════
#  Shared predicates
# ═════════════════════════════════════════════════════════════════════
def _converted(order):
    """Same person already finished an order after this draft was started."""
    if not order.email:
        return False
    return (
        Order.objects.filter(email__iexact=order.email, submitted_at__gte=order.created_at)
        .exclude(status__in=[Order.Status.DRAFT, Order.Status.SPAM]).exclude(pk=order.pk).exists()
    )


def _superseded(order):
    """A newer draft from the same email exists: remind about that one only."""
    return Order.objects.filter(
        status=Order.Status.DRAFT, email__iexact=order.email, last_activity_at__gt=order.last_activity_at,
    ).exclude(pk=order.pk).exists()


def _base_drafts(now, since, days=14):
    qs = Order.objects.filter(status=Order.Status.DRAFT, last_activity_at__gte=now - timedelta(days=days))
    return qs.filter(created_at__gte=since) if since else qs


# ═════════════════════════════════════════════════════════════════════
#  evaluate(): the decision
# ═════════════════════════════════════════════════════════════════════
def evaluate(order, eng, now):
    if order.status != Order.Status.DRAFT:
        return _stop("spam" if order.status == Order.Status.SPAM else "submitted")
    email = (order.email or "").strip().lower()
    if not email:
        return _stop("no_email")
    suppressed = EmailSuppression.reason_for(email)
    if suppressed:
        return _stop(suppressed)
    if eng.reminders_paused:
        return _wait("paused")
    if not intel.conf("ORDER_REMINDERS_ENABLED"):
        return _wait("reminders_disabled")
    if _converted(order):
        return _stop("converted")
    if _superseded(order):
        return _stop("superseded")
    days_left = order.days_left
    if days_left is not None and days_left < 0:
        return _stop("deadline_passed")
    if order.last_activity_at < now - timedelta(days=int(intel.conf("ORDER_REMINDER_MAX_AGE_DAYS"))):
        return _stop("expired")
    if eng.lifetime_reminders >= int(intel.conf("ORDER_REMINDER_MAX_LIFETIME")):
        return _stop("exhausted")

    # Failed-send backoff: don't hammer a failing provider / bad address.
    if eng.send_failures and eng.next_reminder_at and now < eng.next_reminder_at:
        return _wait("backoff", eng.next_reminder_at)

    sent = eng.reminders_sent
    reset = False
    if sent and eng.last_reminder_at and order.last_activity_at > eng.last_reminder_at:
        sent, reset = 0, True  # they came back after the last reminder: fresh ladder

    ladder, urgent = intel.ladder_for(order, eng.lead_tier)
    if not ladder:
        return _wait("disabled_for_site")
    stage = sent + 1
    if stage > len(ladder):
        return _stop("exhausted")

    candidate = order.last_activity_at + timedelta(hours=ladder[stage - 1])
    if sent and eng.last_reminder_at:
        candidate = max(candidate, eng.last_reminder_at + timedelta(hours=int(intel.conf("ORDER_REMINDER_MIN_GAP_HOURS"))))
    due = intel.next_send_time(candidate, intel.infer_timezone(order), salt=order.pk)
    if now < due:
        return Decision("wait", "not_due", stage, len(ladder), urgent, "", due, reset)

    recent = OrderReminderLog.objects.filter(
        email__iexact=email, status=OrderReminderLog.Status.SENT, sent_at__gte=now - timedelta(days=7),
    ).count()
    if recent >= int(intel.conf("ORDER_REMINDER_MAX_PER_EMAIL_WEEK")):
        return Decision("wait", "frequency_cap", stage, len(ladder), urgent, "", now + timedelta(hours=6), reset)

    return Decision("send", "due", stage, len(ladder), urgent, intel.ab_variant(order, stage), due, reset)


# ═════════════════════════════════════════════════════════════════════
#  1. intent fallback
# ═════════════════════════════════════════════════════════════════════
def fire_pending_intents(now, report, limit=200):
    if not intel.conf("ORDER_INTENT_ALERTS_ENABLED"):
        return
    since = intel.engagement_since()
    cutoff = now - timedelta(seconds=int(intel.conf("ORDER_INTENT_GRACE_SECONDS")))
    qs = _base_drafts(now, since, days=2).filter(last_activity_at__lt=cutoff).exclude(email="").order_by("created_at")
    for order in qs[:limit]:
        eng = OrderEngagement.objects.filter(order_id=order.pk).first()
        if eng and (eng.intent_notified_at or eng.intent_skipped_reason or eng.intent_failures >= notify.MAX_INTENT_FAILURES):
            continue
        if report.dry_run:
            report.add("intent_would_send")
            report.note(f"intent: would alert for {order.reference} <{order.email}>")
            continue
        outcome = notify.deliver_intent(order.pk)
        if outcome == "sent":
            report.add("intent_sent")
        elif outcome == "failed":
            report.add("intent_failed")


# ═════════════════════════════════════════════════════════════════════
#  2. exit signals
# ═════════════════════════════════════════════════════════════════════
def process_exit_signals(now, report, limit=200):
    since = intel.engagement_since()
    grace = timedelta(minutes=int(intel.conf("ORDER_EXIT_GRACE_MINUTES")))

    early = (
        _base_drafts(now, since)
        .filter(abandoned_notified=False, engagement__exit_signalled_at__isnull=False,
                engagement__exit_signalled_at__lt=now - grace,
                engagement__exit_signalled_at__gte=F("last_activity_at"))
        .exclude(email="", phone="").select_related("visitor").order_by("last_activity_at")
    )
    for order in early[:limit]:
        if report.dry_run:
            report.add("exit_would_alert")
            report.note(f"exit: would send abandoned alert for {order.reference}")
            continue
        if not Order.objects.filter(pk=order.pk, abandoned_notified=False).update(abandoned_notified=True):
            continue  # the 30-minute sweeper (or another worker) got there first
        if _converted(order):
            report.add("exit_converted")
            continue
        try:
            emails.send_admin_email(order, "abandoned")
            report.add("exit_alerts")
        except Exception:
            logger.exception("Early abandoned alert failed for %s", order.reference)

    # The early alert flips abandoned_notified, which makes the legacy sweeper skip
    # the client recovery email. Send it here once the normal 30 minutes have passed.
    idle = timedelta(minutes=int(getattr(emails.settings, "ORDER_ABANDON_MINUTES", 30)))
    late = (
        _base_drafts(now, since)
        .filter(abandoned_notified=True, recovery_sent=False, engagement__exit_signalled_at__isnull=False,
                last_activity_at__lt=now - idle)
        .exclude(email="").order_by("last_activity_at")
    )
    for order in late[:limit]:
        if report.dry_run:
            report.add("recovery_would_send")
            continue
        if not Order.objects.filter(pk=order.pk, recovery_sent=False).update(recovery_sent=True):
            continue
        if _converted(order) or EmailSuppression.reason_for(order.email):
            report.add("recovery_skipped")
            continue
        try:
            emails.send_client_recovery_email(order)
            report.add("recovery_sent")
        except Exception:
            logger.exception("Recovery email failed for %s", order.reference)


# ═════════════════════════════════════════════════════════════════════
#  3. reminder ladder
# ═════════════════════════════════════════════════════════════════════
def _persist_decision(eng, d):
    """Cache the outcome on the engagement row (for the admin + digest)."""
    fields = {}
    if d.action == "stop":
        if eng.stop_reason != d.reason:
            fields["stop_reason"] = d.reason
        if eng.next_reminder_at is not None:
            fields["next_reminder_at"] = None
    else:
        if eng.stop_reason and eng.stop_reason not in TERMINAL_STOPS:
            fields["stop_reason"] = ""
        if d.reason == "not_due" and d.due_at and eng.next_reminder_at != d.due_at:
            fields["next_reminder_at"] = d.due_at
    if d.reset and eng.reminders_sent:
        fields["reminders_sent"] = 0
    if fields:
        OrderEngagement.objects.filter(pk=eng.pk).update(**fields)
        for k, v in fields.items():
            setattr(eng, k, v)


def _send_one(order, eng, d, now, report):
    """Claim -> send -> record. On failure the claim is rolled back and the
    order backs off (15m, 1h, 4h, 12h) before giving up for good."""
    start_sent = 0 if d.reset else eng.reminders_sent
    claimed = OrderEngagement.objects.filter(
        pk=eng.pk, reminders_sent=eng.reminders_sent, last_reminder_at=eng.last_reminder_at,
    ).update(reminders_sent=start_sent + 1, lifetime_reminders=F("lifetime_reminders") + 1,
             last_reminder_at=now, next_reminder_at=None, send_failures=0)
    if not claimed:
        report.add("reminder_lost_claim")
        return

    log = notify.send_reminder(order, eng, d.stage, d.total, d.urgent, d.variant)
    if log.status == OrderReminderLog.Status.SENT:
        report.add("reminders_sent")
        return

    failures = eng.send_failures + 1
    back = BACKOFF_MINUTES[min(failures - 1, len(BACKOFF_MINUTES) - 1)]
    OrderEngagement.objects.filter(pk=eng.pk).update(
        reminders_sent=eng.reminders_sent, lifetime_reminders=F("lifetime_reminders") - 1,
        last_reminder_at=eng.last_reminder_at, send_failures=failures,
        next_reminder_at=now + timedelta(minutes=back),
        stop_reason="send_failed" if failures >= MAX_SEND_FAILURES else eng.stop_reason,
    )
    report.add("reminders_failed")


def _min_first_offset_hours():
    base = [int(h) for h in intel.conf("ORDER_REMINDER_LADDER_HOURS")] or [24]
    urgent = [int(h) for h in intel.conf("ORDER_REMINDER_URGENT_LADDER_HOURS")] or [24]
    return max(1, min(round(base[0] * 0.75), urgent[0]))


def process_reminders(now, report, limit=500):
    since = intel.engagement_since()
    horizon = now - timedelta(hours=_min_first_offset_hours())
    qs = (
        _base_drafts(now, since, days=int(intel.conf("ORDER_REMINDER_MAX_AGE_DAYS")))
        .filter(last_activity_at__lt=horizon).exclude(email="")
        .exclude(engagement__stop_reason__in=TERMINAL_STOPS).exclude(engagement__reminders_paused=True)
        .select_related("visitor").order_by("last_activity_at")
    )
    for order in qs[:limit]:
        try:
            eng = OrderEngagement.for_order(order)
            if report.dry_run:
                eng.lead_score, eng.lead_tier, eng.score_breakdown = intel.score_order(order, eng)
            else:
                intel.refresh_score(order, eng)
            d = evaluate(order, eng, now)
            if report.dry_run:
                label = f"{order.reference} <{order.email}> tier={eng.lead_tier}({eng.lead_score}) -> {d.action}:{d.reason}"
                if d.stage:
                    label += f" stage {d.stage}/{d.total}" + (" URGENT" if d.urgent else "")
                if d.due_at:
                    label += f" due {d.due_at:%Y-%m-%d %H:%M} UTC"
                report.note(label)
                report.add(f"would_{d.action}")
                continue
            _persist_decision(eng, d)
            report.add(f"{d.action}:{d.reason}")
            if d.action == "send":
                _send_one(order, eng, d, now, report)
        except Exception:
            logger.exception("Reminder evaluation failed for order %s", order.pk)
            report.add("errors")


# ═════════════════════════════════════════════════════════════════════
#  4. escalations to the admin
# ═════════════════════════════════════════════════════════════════════
def _escalate(order, eng, field, reason, report):
    claimed = OrderEngagement.objects.filter(pk=eng.pk, **{f"{field}__isnull": True}).update(**{field: timezone.now()})
    if not claimed:
        return
    try:
        notify.send_escalation_alert(order, eng, reason)
        report.add(f"escalation_{reason}")
    except Exception:
        OrderEngagement.objects.filter(pk=eng.pk).update(**{field: None})
        logger.exception("Escalation %s failed for %s", reason, order.reference)
        report.add("errors")


def process_escalations(now, report, limit=100):
    since = intel.engagement_since()
    base = _base_drafts(now, since)

    hot_after = timedelta(hours=int(intel.conf("ORDER_HOT_FOLLOWUP_HOURS")))
    hot = (OrderEngagement.objects.filter(order__in=base, lead_tier="hot", hot_alert_sent_at__isnull=True,
                                          order__last_activity_at__lt=now - hot_after)
           .select_related("order", "order__visitor")[:limit])
    for eng in hot:
        order = eng.order
        if _converted(order) or not (order.email or order.phone):
            continue
        if report.dry_run:
            report.add("would_escalate_hot")
            report.note(f"escalate: HOT {order.reference} score {eng.lead_score}")
            continue
        _escalate(order, eng, "hot_alert_sent_at", "hot_idle", report)

    click_after = timedelta(hours=int(intel.conf("ORDER_CLICK_FOLLOWUP_HOURS")))
    clicked = (OrderEngagement.objects.filter(order__in=base, clicks__gt=0, click_alert_sent_at__isnull=True,
                                              last_clicked_at__lt=now - click_after)
               .select_related("order", "order__visitor")[:limit])
    for eng in clicked:
        order = eng.order
        if order.last_activity_at > eng.last_clicked_at or _converted(order):
            continue  # they did come back and keep going
        if report.dry_run:
            report.add("would_escalate_click")
            continue
        _escalate(order, eng, "click_alert_sent_at", "clicked_not_finished", report)


# ═════════════════════════════════════════════════════════════════════
#  Entry point
# ═════════════════════════════════════════════════════════════════════
def run_engine(now=None, *, dry_run=False):
    if not intel.engagement_enabled():
        return {"disabled": True}
    now = now or timezone.now()
    report = Report(dry_run=dry_run)
    for step in (fire_pending_intents, process_exit_signals, process_reminders, process_escalations):
        try:
            step(now, report)
        except Exception:
            logger.exception("Engagement step %s crashed", step.__name__)
            report.add("errors")
    if any(v for k, v in report.counts.items() if not k.startswith(("wait:", "would_wait"))):
        logger.info("Order engagement run: %s", dict(report.counts))
    return report.as_dict()


# ═════════════════════════════════════════════════════════════════════
#  Optional in-process ticker (for deployments with no Celery beat/cron)
# ═════════════════════════════════════════════════════════════════════
_ticker_lock = threading.Lock()
_ticker_thread = None


def start_ticker(interval=120):
    """Idempotent. Multiple gunicorn workers each start one; the atomic claims
    above make that safe (it only costs a few redundant queries)."""
    global _ticker_thread
    with _ticker_lock:
        if _ticker_thread is not None and _ticker_thread.is_alive():
            return False

        def loop():
            time.sleep(random.uniform(15, 45))  # stagger workers / let the app finish booting
            while True:
                try:
                    run_engine()
                except Exception:
                    logger.exception("Order engagement ticker run failed")
                finally:
                    connection.close()
                time.sleep(interval + random.uniform(-10, 10))

        _ticker_thread = threading.Thread(target=loop, name="order-engagement-ticker", daemon=True)
        _ticker_thread.start()
        return True
