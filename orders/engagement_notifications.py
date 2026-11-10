"""
orders/engagement_notifications.py

Everything the engagement layer SENDS.

  ADMIN (to ORDER_NOTIFICATION_EMAILS - your three inboxes)
    deliver_intent()          "<Name> <email> started an order"  (separate from
                              the abandoned and submitted alerts)
    send_escalation_alert()   hot lead still pending / clicked but didn't finish
    send_digest()             daily pipeline digest, ranked by lead score

  CLIENT
    send_reminder()           stage-aware reminder with one-click unsubscribe,
                              tracked resume link and A/B subject variants

Reuses the private helpers in orders/emails.py (_run, _clean, _admin_recipients,
resume_url) so threading, header-injection safety and recipient routing stay
identical to the existing emails.
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.core import signing
from django.core.mail import EmailMultiAlternatives
from django.db.models import F
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.timesince import timesince

from . import emails
from . import engagement_intel as intel
from .engagement_models import EmailSuppression, OrderEngagement, OrderReminderLog

logger = logging.getLogger("orders")

UNSUB_SALT = "orders.unsubscribe.v1"
MAX_INTENT_FAILURES = 5


# ═════════════════════════════════════════════════════════════════════
#  URLs and tokens
# ═════════════════════════════════════════════════════════════════════
def unsubscribe_token(email):
    return signing.dumps({"e": EmailSuppression.normalise(email)}, salt=UNSUB_SALT, compress=True)


def read_unsubscribe_token(token):
    """Returns the email, or '' for a tampered / malformed token."""
    try:
        data = signing.loads(token, salt=UNSUB_SALT)
    except signing.BadSignature:
        return ""
    return EmailSuppression.normalise(data.get("e", "")) if isinstance(data, dict) else ""


def unsubscribe_url(email):
    base = intel.api_base()
    return f"{base}/api/orders/unsubscribe/{unsubscribe_token(email)}/" if base else ""


def tracked_resume_url(order, stage, variant):
    """Goes via our redirect so clicks are measured; falls back to the direct
    resume link when the API base URL isn't configured."""
    base = intel.api_base()
    if not base:
        return emails.resume_url(order)
    return f"{base}/api/orders/{order.public_token}/resume/?s={stage}&v={variant}"


def _admin_url(order):
    admin_base = (getattr(settings, "ORDERS_ADMIN_BASE_URL", "") or settings.SITE_DOMAIN).rstrip("/")
    return f"{admin_base}/admin/orders/order/{order.pk}/change/"


def _first_name(order):
    return order.name.split()[0] if (order.name or "").strip() else ""


def _build(subject, text, html, to, reply_to=None, headers=None):
    msg = EmailMultiAlternatives(
        subject=emails._clean(subject)[:200], body=text, from_email=settings.DEFAULT_FROM_EMAIL,
        to=to, reply_to=reply_to or None, headers=headers or None,
    )
    msg.attach_alternative(html, "text/html")
    return msg


# ═════════════════════════════════════════════════════════════════════
#  ADMIN: intent alert  ("someone started an order")
# ═════════════════════════════════════════════════════════════════════
def _history(order, limit=5):
    from .models import Order
    prev = Order.objects.filter(email__iexact=order.email).exclude(pk=order.pk).order_by("-created_at")[:limit]
    return [
        {"reference": o.reference, "status": o.get_status_display(), "service": o.service or "-",
         "when": f"{timesince(o.created_at)} ago"}
        for o in prev
    ]


def _admin_context(order, eng, **extra):
    ctx = {
        "order": order, "site": order.site_info, "eng": eng,
        "rows": order.summary_rows(),
        "attribution": _attribution_without_journey(order),
        "files": list(order.files.all()),
        "admin_url": _admin_url(order),
        "resume_url": emails.resume_url(order),
        "progress_pct": intel.progress_pct(order),
        "step_no": order.current_step + 1, "step_total": len(order.site_info.get("steps") or []),
        "breakdown": eng.score_breakdown if eng else [],
    }
    ctx.update(extra)
    return ctx


def _attribution_without_journey(order):
    """The monkey-patched attribution_rows() appends a 'Journey' row; the
    engagement emails show that information in their own blocks instead."""
    original = getattr(type(order).attribution_rows, "__wrapped__", None)
    return original(order) if original else order.attribution_rows()


def _status_line(order):
    if order.status != order.Status.DRAFT:
        return f"Already submitted as {order.reference}."
    return f"Still filling it in (step {order.current_step + 1} of {len(order.site_info.get('steps') or [])})."


def _send_admin_engagement(order, eng, *, kind, subject, accent, badge, headline, callout, to):
    ctx = _admin_context(order, eng, kind=kind, accent=accent, badge=badge, headline=headline, callout=callout,
                         history=_history(order), status_line=_status_line(order),
                         reminders=list(order.reminder_logs.all()[:6]))
    text_lines = [badge, headline, "", callout, "", order.as_text(), "", f"Admin: {ctx['admin_url']}"]
    if eng and eng.lead_score:
        text_lines += ["", f"Lead score: {eng.lead_score} ({eng.get_lead_tier_display()})"]
        text_lines += [f"  {'+' if p > 0 else ''}{p}  {label}" for label, p in eng.score_breakdown]
    msg = _build(
        subject, "\n".join(text_lines), render_to_string("email/order_admin_engagement.html", ctx), to,
        reply_to=[order.email] if order.email else None,
    )
    msg.send(fail_silently=False)


def _mark_skipped(eng, reason):
    OrderEngagement.objects.filter(
        pk=eng.pk, intent_notified_at__isnull=True, intent_skipped_reason="",
    ).update(intent_skipped_reason=reason)


def deliver_intent(pk):
    """Idempotent + concurrency-safe. Safe to call from the request hook, a
    background thread AND the sweeper - the atomic claim means exactly one
    caller sends. Returns a short status string (used by tests / the sweeper)."""
    from .models import Order

    if not (intel.engagement_enabled() and intel.conf("ORDER_INTENT_ALERTS_ENABLED")):
        return "disabled"
    order = Order.objects.select_related("visitor").filter(pk=pk).first()
    if order is None or not order.email:
        return "no_email"
    eng = OrderEngagement.for_order(order)
    if eng.intent_notified_at or eng.intent_skipped_reason:
        return "done"
    if eng.intent_failures >= MAX_INTENT_FAILURES:
        return "gave_up"

    since = intel.engagement_since()
    if since and order.created_at < since:
        _mark_skipped(eng, "pre_existing")
        return "pre_existing"

    now = timezone.now()
    hours = int(intel.conf("ORDER_INTENT_DEDUPE_HOURS") or 0)
    if hours and OrderEngagement.objects.filter(
        order__email__iexact=order.email, intent_notified_at__gte=now - timedelta(hours=hours),
    ).exclude(order_id=order.pk).exists():
        _mark_skipped(eng, "deduped")
        return "deduped"

    claimed = OrderEngagement.objects.filter(
        pk=eng.pk, intent_notified_at__isnull=True, intent_skipped_reason="",
    ).update(intent_notified_at=now)
    if not claimed:
        return "already"

    try:
        to = emails._admin_recipients(order)
        if not to:
            raise RuntimeError("no admin recipients configured (ORDER_NOTIFICATION_EMAILS)")
        attempt = Order.objects.filter(email__iexact=order.email, created_at__lt=order.created_at).exclude(pk=order.pk).count() + 1
        OrderEngagement.objects.filter(pk=eng.pk).update(intent_attempt_no=attempt)
        eng.intent_attempt_no = attempt
        intel.refresh_score(order, eng, force=True)

        tier = eng.lead_tier.upper()
        site_key = order.site_info["key"]
        tag = f"{tier} " if eng.lead_tier == "hot" else ""
        bits = [f"[{tag}ORDER STARTED · {site_key}] {order.display_name} <{order.email}>",
                order.service or "has not picked a service yet"]
        if attempt > 1:
            bits.append(f"attempt #{attempt} from this email")
        callout = (
            f"{order.display_name} ({order.email}) just started an order on {order.site_info['name']}. "
            f"{_status_line(order)} This alert is sent as soon as we capture their contact details, whether or not they "
            f"finish. You will get a separate alert if they abandon the order and another when they submit it."
        )
        _send_admin_engagement(
            order, eng, kind="intent", subject=" · ".join(bits), accent="#3b3fb6",
            badge="Order started · intent captured", headline="Someone is placing an order", callout=callout, to=to,
        )
        logger.info("Intent alert sent for %s (%s)", order.reference, order.email)
        return "sent"
    except Exception:
        OrderEngagement.objects.filter(pk=eng.pk).update(intent_notified_at=None, intent_failures=F("intent_failures") + 1)
        logger.exception("Intent alert failed for %s; will retry from the sweeper", order.reference)
        return "failed"


def dispatch_intent(pk):
    """Fire-and-forget from the request thread (same threading model as the
    existing order emails, honouring ORDERS_SYNC_EMAIL)."""
    emails._run(lambda: deliver_intent(pk))


# ═════════════════════════════════════════════════════════════════════
#  ADMIN: escalation alerts
# ═════════════════════════════════════════════════════════════════════
def send_escalation_alert(order, eng, reason):
    to = emails._admin_recipients(order)
    if not to:
        raise RuntimeError("no admin recipients configured")
    site_key = order.site_info["key"]
    if reason == "clicked_not_finished":
        last = order.reminder_logs.filter(clicked_at__isnull=False).order_by("-clicked_at").first()
        stage = last.stage if last else eng.lifetime_reminders
        subject = f"[WARM SIGNAL · {site_key}] {order.display_name} clicked reminder #{stage} but has not finished"
        badge, accent = "Clicked but did not finish", "#0d6b5a"
        headline = "They came back to the link, then stopped"
        callout = ("They opened their resume link and did not complete the order. This is the best moment for a personal "
                   "message: a quick WhatsApp or reply usually converts someone who is already looking.")
    else:
        subject = (f"[HOT LEAD PENDING · {site_key}] {order.display_name} · {order.service or 'no service'} · "
                   f"score {eng.lead_score}")
        badge, accent = "Hot lead still pending", "#b42318"
        headline = "High-value lead, still unfinished"
        callout = (f"Scored {eng.lead_score}/100 and has been idle for {timesince(order.last_activity_at)}. Automated "
                   f"reminders are running, but a personal follow-up now is likely to win this one.")
    _send_admin_engagement(order, eng, kind="escalation", subject=subject, accent=accent, badge=badge,
                           headline=headline, callout=callout, to=to)


# ═════════════════════════════════════════════════════════════════════
#  CLIENT: reminders
# ═════════════════════════════════════════════════════════════════════
def reminder_kind(stage, total, urgent):
    if stage >= total and total > 1:
        return "final"
    if urgent:
        return "urgent"
    return {1: "nudge", 2: "value"}.get(stage, "help")


def _deadline_phrase(order):
    d = order.days_left
    if d is None or d < 0:
        return ""
    when = "today" if d == 0 else ("tomorrow" if d == 1 else f"in {d} days")
    return f"{order.deadline:%d %b} ({when})"


def build_reminder_copy(order, stage, total, variant, urgent):
    """(kind, subject, headline, paragraphs[list[str]], cta). Plain strings only."""
    kind = reminder_kind(stage, total, urgent)
    first = _first_name(order)
    who = first or "there"
    service = order.service or "data analysis"
    pct = intel.progress_pct(order)
    reply_within = order.site_info["promise"].get("reply_within", "24 hours")
    deadline = _deadline_phrase(order)
    ab = variant == "B"

    if kind == "urgent":
        subject = (f"Your deadline is {deadline}: finish your order" if not ab
                   else f"{who}, let's get your {service} started before your deadline")
        headline = "Your deadline is close"
        paras = [
            f"Your deadline is {deadline}. We can only start once your order is submitted, and urgent orders are prioritised.",
            f"Finish now and we'll reply with scope, timeline and price within {reply_within}. You pay nothing until you agree to the quote.",
        ]
        cta = "Finish my order now"
    elif kind == "final":
        subject = (f"Last reminder: your saved {service} order" if not ab else f"{who}, a final note about your {service} order")
        headline = "One last reminder"
        paras = [
            f"This is our last reminder about your {service} order.",
            "Your answers stay saved, so you can finish whenever you're ready. If you no longer need help, no action is needed.",
        ]
        cta = "Finish my order"
    elif kind == "nudge":
        subject = (f"{who}, your order is saved. Pick up where you left off" if not ab
                   else f"Your {service} order is waiting for you")
        headline = f"{who}, your order is saved" if first else "Your order is saved"
        paras = [
            f"You started an order for {service}" + (f" and got {pct}% of the way through." if pct >= 20 else "."),
            "Your answers are saved. Pick up exactly where you left off. Finishing takes a few minutes, and you pay nothing until you agree to a quote.",
        ]
        cta = "Finish my order"
    elif kind == "value":
        subject = (f"What happens after you finish your {service} order" if not ab
                   else f"{who}, want us to start on your {service}?")
        headline = "Ready when you are"
        paras = [
            f"Once you submit, a specialist reviews your request and replies with scope, timeline and price within {reply_within}.",
            "You pay nothing until you agree to the quote, so there's no risk in sending the details.",
        ]
        cta = "Complete my order"
    else:  # help
        subject = (f"Stuck on your order, {who}? Reply and we'll help" if not ab
                   else f"A quick question about your {service} request")
        headline = "Need a hand?"
        paras = [
            "Not sure how to answer one of the questions, or want to talk it through first?",
            "Just reply to this email with what you need and your deadline, and we'll take it from there. No form required.",
        ]
        wa = (intel.conf("ORDER_BUSINESS_WHATSAPP") or "").strip()
        if wa:
            paras.append(f"Prefer WhatsApp? Message us on {wa}.")
        cta = "Finish my order"
    return kind, subject, headline, paras, cta


def send_reminder(order, eng, stage, total, urgent, variant):
    """Sends one reminder SYNCHRONOUSLY (so the log reflects reality) and
    returns the OrderReminderLog row. Never raises."""
    kind, subject, headline, paras, cta = build_reminder_copy(order, stage, total, variant, urgent)
    reply_to = settings.REPLY_TO_EMAIL
    unsub = unsubscribe_url(order.email)
    mailto = f"mailto:{reply_to}?subject=unsubscribe"
    headers = {"List-Unsubscribe": f"<{unsub}>, <{mailto}>" if unsub else f"<{mailto}>"}
    if unsub:
        headers["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"

    resume = tracked_resume_url(order, stage, variant)
    ctx = {
        "order": order, "site": order.site_info, "kind": kind, "headline": headline, "paragraphs": paras, "cta": cta,
        "resume_url": resume, "progress_pct": intel.progress_pct(order), "saved_answers": len(order.summary_rows()),
        "deadline": _deadline_phrase(order), "urgent": urgent, "unsubscribe_url": unsub, "reply_to": reply_to,
        "preheader": paras[0],
    }
    text = "\n\n".join(
        [f"Hi {_first_name(order) or 'there'},", *paras, f"{cta}: {resume}",
         "Or just reply to this email and we'll help.",
         "-- \nYou're receiving this because you started an order at " + order.site_info["name"] + ". "
         + (f"Unsubscribe: {unsub}" if unsub else "Reply 'unsubscribe' to stop these reminders.")]
    )
    log = OrderReminderLog(order=order, email=order.email, stage=stage, variant=variant, subject=emails._clean(subject)[:200])
    try:
        msg = _build(subject, text, render_to_string("email/order_client_reminder.html", ctx), [order.email],
                     reply_to=[reply_to], headers=headers)
        msg.tags = ["order-reminder", f"stage-{stage}", f"variant-{variant}"]          # Anymail/Brevo
        msg.metadata = {"order_ref": order.reference, "stage": str(stage), "variant": variant}
        msg.send(fail_silently=False)
        status = getattr(msg, "anymail_status", None)
        mid = getattr(status, "message_id", "") if status is not None else ""
        log.provider_message_id = str(mid or "")[:200]
        log.status = OrderReminderLog.Status.SENT
    except Exception as exc:
        logger.exception("Reminder %s failed for %s", stage, order.reference)
        log.status = OrderReminderLog.Status.FAILED
        log.error = str(exc)[:300]
    log.sent_at = timezone.now()
    log.save()
    return log


# ═════════════════════════════════════════════════════════════════════
#  ADMIN: daily digest
# ═════════════════════════════════════════════════════════════════════
def build_digest(now=None):
    from .models import Order

    now = now or timezone.now()
    since = now - timedelta(hours=24)
    start = intel.engagement_since()
    drafts = Order.objects.filter(status=Order.Status.DRAFT, last_activity_at__gte=now - timedelta(days=14)).exclude(email="")
    if start:
        drafts = drafts.filter(created_at__gte=start)
    open_leads = []
    for eng in (OrderEngagement.objects.filter(order__in=drafts).select_related("order", "order__visitor")
                .order_by("-lead_score", "-order__last_activity_at")[:15]):
        o = eng.order
        if eng.stop_reason in ("unsubscribed", "bounced", "complained"):
            note = f"do not email ({eng.stop_reason})"
        elif eng.next_reminder_at:
            note = f"next reminder {eng.next_reminder_at:%d %b %H:%M} UTC"
        else:
            note = eng.stop_reason or "-"
        open_leads.append({"order": o, "eng": eng, "idle": timesince(o.last_activity_at), "note": note})
    stats = {
        "started": OrderEngagement.objects.filter(intent_notified_at__gte=since).count(),
        "submitted": Order.objects.filter(submitted_at__gte=since).count(),
        "reminders": OrderReminderLog.objects.filter(sent_at__gte=since, status="sent").count(),
        "clicks": OrderReminderLog.objects.filter(clicked_at__gte=since).count(),
        "recovered": OrderEngagement.objects.filter(
            recovered_by_stage__isnull=False, order__submitted_at__gte=since).count(),
    }
    return stats, open_leads


def send_digest(now=None):
    if not (intel.engagement_enabled() and intel.conf("ORDER_DIGEST_ENABLED")):
        return "disabled"
    stats, open_leads = build_digest(now)
    if not open_leads and not any(stats.values()):
        return "empty"
    to = emails._admin_recipients()
    if not to:
        return "no_recipients"
    admin_base = (getattr(settings, "ORDERS_ADMIN_BASE_URL", "") or settings.SITE_DOMAIN).rstrip("/")
    ctx = {"stats": stats, "open_leads": open_leads, "admin_list_url": f"{admin_base}/admin/orders/order/?status__exact=draft"}
    subject = (f"[ORDER DIGEST] {stats['started']} started · {stats['submitted']} submitted · "
               f"{len(open_leads)} open lead(s)")
    lines = [subject, ""]
    for l in open_leads:
        o, e = l["order"], l["eng"]
        lines.append(f"- [{e.get_lead_tier_display().upper()} {e.lead_score}] {o.display_name} <{o.email}> · "
                     f"{o.service or 'no service'} · idle {l['idle']} · {l['note']}")
    _build(subject, "\n".join(lines), render_to_string("email/order_admin_digest.html", ctx), to).send(fail_silently=False)
    return "sent"
