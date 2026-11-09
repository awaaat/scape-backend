from django.core.management.base import BaseCommand

from orders.services import sweep_abandoned_orders


class Command(BaseCommand):
    help = (
        "Notify admins about abandoned order drafts, email the client a resume link, "
        "and retry any submitted order whose admin email never went out. "
        "Use as a cron job if you don't run Celery beat."
    )

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS(f"Order sweep: {sweep_abandoned_orders()}"))
