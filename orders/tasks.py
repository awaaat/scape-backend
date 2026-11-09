from celery import shared_task

from . import services


@shared_task
def sweep_abandoned_orders_task():
    return services.sweep_abandoned_orders()
