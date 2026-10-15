import logging

from django.utils import timezone

logger = logging.getLogger(__name__)


def record_app_membership(user, source):
    """
    Idempotent. Never raises - tracking which apps someone uses must never
    be able to break signup or login.
    """
    if user is None or not source:
        return None
    from .models import AppMembership
    try:
        obj, created = AppMembership.objects.get_or_create(user=user, source=source)
        if not created:
            AppMembership.objects.filter(pk=obj.pk).update(last_seen_at=timezone.now())
        return obj
    except Exception:
        logger.exception("Could not record app membership user=%s source=%s", getattr(user, "pk", None), source)
        return None
