"""
backend/celery.py

Celery application. The whole point: property_intel/tasks.py's
generate_report_task must NEVER run inside a gunicorn request/response
cycle — Google's enrichment pipeline alone can take 10-15+ seconds, well
past a typical request timeout, before a PDF is even rendered.

Run a worker with:
    celery -A backend worker --loglevel=info

Run the beat scheduler (for sweep_stuck_reports) with:
    celery -A backend beat --loglevel=info
"""
import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "backend.settings")

app = Celery("backend")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
