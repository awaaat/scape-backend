from django.conf import settings
from django.core.management.base import BaseCommand

from leads.email import send_admin_notification, send_user_welcome
from leads.models import Lead


class Command(BaseCommand):
    help = "Send a dummy welcome + admin notification email to verify Brevo SMTP works end to end."

    def add_arguments(self, parser):
        parser.add_argument("--to", type=str, help="Override the test recipient email", default=None)

    def handle(self, *args, **options):
        to_email = options["to"] or settings.ADMIN_NOTIFICATION_EMAILS[0]

        fake_lead = Lead(
            name="Test User",
            email=to_email,
            company="Test Co",
            phone="+1 555 0100",
            service="AI & Machine Learning",
            message="This is a test message to confirm SMTP delivery is working correctly.",
            page_url=settings.SITE_DOMAIN,
        )

        try:
            send_user_welcome(fake_lead)
            self.stdout.write(self.style.SUCCESS(f"Welcome email sent to {to_email}"))
        except Exception as exc:
            self.stdout.write(self.style.ERROR(f"Welcome email failed: {exc}"))
            return

        try:
            send_admin_notification(fake_lead)
            self.stdout.write(self.style.SUCCESS("Admin notification sent."))
        except Exception as exc:
            self.stdout.write(self.style.ERROR(f"Admin notification failed: {exc}"))
