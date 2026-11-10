"""
orders/engagement_views.py

Public endpoints of the engagement layer (mounted under /api/orders/):

  POST <token>/leave/              exit beacon  (navigator.sendBeacon on pagehide)
  POST <token>/return/             "they're back" beacon (tab became visible again)
  GET  <token>/resume/?s=2&v=A     tracked resume link used in reminder emails;
                                   records the click, then 302s to the real
                                   front-end resume URL
  GET  unsubscribe/<signed>/       confirmation page (a GET never unsubscribes:
                                   mail-security scanners prefetch links)
  POST unsubscribe/<signed>/       performs it (also the RFC 8058 one-click target)

Same posture as the rest of orders/views.py: no JWT, scoped throttles only.
"""
import logging
import re

from django.conf import settings
from django.db.models import F
from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.utils import timezone
from rest_framework import status
from rest_framework.negotiation import BaseContentNegotiation
from rest_framework.response import Response

from . import emails
from . import engagement_intel as intel
from . import engagement_notifications as notify
from .engagement_models import EmailSuppression, OrderEngagement, OrderReminderLog
from .models import Order
from .views import PublicView

logger = logging.getLogger("orders")

_BOT_UA = re.compile(
    r"bot|crawl|spider|preview|scanner|monitor|facebookexternalhit|slack|headless|python-requests|curl|wget|"
    r"barracuda|proofpoint|mimecast|safelinks|urldefense|trendmicro|symantec",
    re.I,
)


class AnyAccept(BaseContentNegotiation):
    """Browsers, mail gateways and sendBeacon send arbitrary Accept headers.
    These endpoints return redirects / HTML / 204, so never 406 on Accept."""

    def select_parser(self, request, parsers):
        return parsers[0]

    def select_renderer(self, request, renderers, format_suffix=None):
        return renderers[0], renderers[0].media_type


class EngagementPublicView(PublicView):
    content_negotiation_class = AnyAccept


class OrderLeaveView(EngagementPublicView):
    """Always answers 204, even for unknown tokens: a beacon can't act on the
    response and we don't want to confirm which tokens exist."""
    throttle_scope = "order_save"

    def post(self, request, token):
        if intel.engagement_enabled():
            order = Order.objects.filter(public_token=token, status=Order.Status.DRAFT).first()
            if order is not None:
                eng = OrderEngagement.for_order(order)
                OrderEngagement.objects.filter(pk=eng.pk).update(
                    exit_signalled_at=timezone.now(), exit_signal_count=F("exit_signal_count") + 1,
                )
        return Response(status=status.HTTP_204_NO_CONTENT)


class OrderReturnView(EngagementPublicView):
    throttle_scope = "order_save"

    def post(self, request, token):
        OrderEngagement.objects.filter(order__public_token=token, exit_signalled_at__isnull=False).update(
            exit_signalled_at=None,
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


def _record_click(request, order, stage):
    """Count a click only when it looks human: not a HEAD, not a known
    scanner user-agent, and not within seconds of the send (mail-security
    gateways pre-fetch every link the moment a message arrives)."""
    if request.method == "HEAD" or _BOT_UA.search(request.META.get("HTTP_USER_AGENT", "") or ""):
        return
    logs = OrderReminderLog.objects.filter(order=order, status=OrderReminderLog.Status.SENT)
    log = (logs.filter(stage=stage).order_by("-sent_at").first() if stage else None) or logs.order_by("-sent_at").first()
    now = timezone.now()
    if log is None or (now - log.sent_at).total_seconds() < int(intel.conf("ORDER_CLICK_MIN_SECONDS")):
        return
    OrderReminderLog.objects.filter(pk=log.pk).update(
        click_count=F("click_count") + 1, clicked_at=log.clicked_at or now,
    )
    eng = OrderEngagement.for_order(order)
    OrderEngagement.objects.filter(pk=eng.pk).update(clicks=F("clicks") + 1, last_clicked_at=now)


class OrderResumeRedirectView(EngagementPublicView):
    throttle_scope = "order_read"

    def get(self, request, token):
        order = Order.objects.filter(public_token=token).first()
        if order is None:
            dest = getattr(settings, "ORDER_FRONTEND_URL", "") or "/"
        else:
            try:
                if order.status == Order.Status.DRAFT and intel.engagement_enabled():
                    stage = request.GET.get("s", "")
                    _record_click(request, order, int(stage) if stage.isdigit() else 0)
            except Exception:
                logger.exception("Click tracking failed for %s", order.reference)
            dest = emails.resume_url(order)
        resp = HttpResponseRedirect(dest)
        resp["Cache-Control"] = "no-store"
        resp["Referrer-Policy"] = "no-referrer"
        return resp


def _mask(email):
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}" if domain else email


class OrderUnsubscribeView(EngagementPublicView):
    throttle_scope = "order_read"

    def _page(self, request, state, email="", code=200):
        resp = render(request, "orders/unsubscribe.html", {"state": state, "masked": _mask(email) if email else ""})
        resp.status_code = code
        resp["Cache-Control"] = "no-store"
        return resp

    def get(self, request, signed):
        email = notify.read_unsubscribe_token(signed)
        if not email:
            return self._page(request, "invalid", code=400)
        return self._page(request, "confirm", email)

    def post(self, request, signed):
        email = notify.read_unsubscribe_token(signed)
        if not email:
            return self._page(request, "invalid", code=400)
        EmailSuppression.add(email, EmailSuppression.Reason.UNSUBSCRIBED, source="link")
        OrderEngagement.objects.filter(order__email__iexact=email).update(stop_reason="unsubscribed", next_reminder_at=None)
        logger.info("Unsubscribed %s from order reminders", email)
        return self._page(request, "done", email)
