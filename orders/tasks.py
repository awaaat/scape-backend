from celery import shared_task

from . import services


@shared_task
def sweep_abandoned_orders_task():
    return services.sweep_abandoned_orders()


# ═══ ORDER ENGAGEMENT LAYER (install_order_engagement.sh) ═══
@shared_task
def run_order_engagement_task():
    """Every ~2 min: intent fallback, early exit alerts, reminder ladder, escalations."""
    from . import engagement_engine
    return engagement_engine.run_engine()


@shared_task
def send_order_digest_task():
    """Once a day: ranked pipeline digest to the admin inboxes."""
    from . import engagement_notifications
    return engagement_notifications.send_digest()
