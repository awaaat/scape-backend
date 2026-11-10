from django.core.management.base import BaseCommand

from orders import engagement_engine, engagement_notifications


class Command(BaseCommand):
    help = (
        "Run the order engagement engine once: intent-alert fallback, early 'they left' alerts, the reminder "
        "ladder and hot-lead escalations. Use as a cron job (every 2-5 min) if you don't run Celery beat. "
        "--dry-run prints every decision and sends nothing. --digest sends the daily admin digest instead."
    )

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Show what WOULD happen; send and write nothing.")
        parser.add_argument("--digest", action="store_true", help="Send the daily pipeline digest and exit.")

    def handle(self, *args, **options):
        if options["digest"]:
            self.stdout.write(self.style.SUCCESS(f"Digest: {engagement_notifications.send_digest()}"))
            return
        result = engagement_engine.run_engine(dry_run=options["dry_run"])
        notes = result.pop("notes", [])
        for line in notes:
            self.stdout.write("  " + line)
        label = "DRY RUN" if options["dry_run"] else "Engagement run"
        self.stdout.write(self.style.SUCCESS(f"{label}: {result}"))
