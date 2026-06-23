import logging

from django.utils.deprecation import MiddlewareMixin
from ipware import get_client_ip
from user_agents import parse as parse_ua

from .models import Visitor

logger = logging.getLogger("visitors")

EXCLUDED_PREFIXES = ("/admin", "/static")


class VisitorTrackingMiddleware(MiddlewareMixin):
    """
    Ensures every browser session hitting the API has a Visitor record,
    keeping IP / device / browser info current. On first visit, triggers
    async enrichment (geo + company lookup) without blocking the request.
    """

    def process_request(self, request):
        if request.path.startswith(EXCLUDED_PREFIXES):
            return

        if not request.session.session_key:
            request.session.save()
        session_key = request.session.session_key

        ip, _ = get_client_ip(request)
        ua_string = request.META.get("HTTP_USER_AGENT", "")
        ua = parse_ua(ua_string)

        try:
            visitor, created = Visitor.objects.get_or_create(
                session_id=session_key,
                defaults={
                    "ip_address": ip,
                    "user_agent": ua_string[:1000],
                    "device_type": ua.device.family or "",
                    "browser": ua.browser.family or "",
                    "operating_system": ua.os.family or "",
                },
            )
            if not created:
                visitor.ip_address = ip or visitor.ip_address
                visitor.request_count += 1
                visitor.save(update_fields=["ip_address", "request_count", "last_seen"])
            else:
                visitor.request_count = 1
                visitor.save(update_fields=["request_count"])
                # Fire async enrichment only for brand-new visitors
                _trigger_enrichment(visitor)

        except Exception as exc:
            logger.error("Visitor tracking failed: %s", exc)
            visitor = None

        request.visitor = visitor


def _trigger_enrichment(visitor):
    try:
        from enrichment.services import enrich_visitor_async
        enrich_visitor_async(visitor)
    except Exception as exc:
        logger.warning("Could not trigger enrichment: %s", exc)