"""
property_intel/tasks.py

Celery tasks — nothing in views.py talks to Google or renders a PDF
synchronously. A broker's HTTP request returns as soon as a PropertyReport
row exists with status="pending"; everything slow happens here, off the
request/response cycle, so a slow Google API or a traffic spike never
produces a gunicorn/Render worker timeout.
"""
import logging
from datetime import timedelta

from celery import shared_task
from django.db import transaction
from django.utils import timezone

from . import storage
from .google_client import enrich_location_cell
from .models import PropertyReport
from .pdf import ReportRenderError, render_report_pdf
from .services import needs_enrichment
from .storage import StorageUploadFailed

logger = logging.getLogger("property_intel")

MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 30
STUCK_REPORT_CUTOFF_MINUTES = 15


@shared_task(bind=True, max_retries=MAX_RETRIES, default_retry_delay=RETRY_BACKOFF_SECONDS)
def generate_report_task(self, report_id):
    """
    Full pipeline for one PropertyReport:
      1. Mark 'generating' (idempotent — safe if this task runs twice for
         the same report, e.g. after a retry race).
      2. Enrich the LocationCell if stale/incomplete.
      3. Render the PDF, upload it, save the path + computed scores.
      4. Mark 'ready', or retry on a transient failure, or 'failed' with a
         reason once retries are exhausted — a report should never be left
         silently stuck in 'generating' forever.
    """
    try:
        report = PropertyReport.objects.select_related("pin__location_cell").get(pk=report_id)
    except PropertyReport.DoesNotExist:
        logger.error("generate_report_task: report %s no longer exists", report_id)
        return

    if report.status == "ready":
        # A duplicate dispatch (e.g. sweep_stuck_reports racing a normal
        # run) must never re-generate and re-spend Google API calls.
        logger.info("Report %s already ready — skipping duplicate task run.", report_id)
        return

    PropertyReport.objects.filter(pk=report_id).update(status="generating")
    cell = report.pin.location_cell

    try:
        if needs_enrichment(cell):
            cell, failures = enrich_location_cell(cell)
            if failures:
                logger.info("Report %s: enrichment had partial failures: %s", report_id, failures)
            cell.refresh_from_db()

        pdf_bytes, investment_score, accessibility_score, summary_text = render_report_pdf(report.pin, cell)

        storage_path = storage.upload_pdf_bytes(pdf_bytes, path=f"reports/{report.pin.id}/{report.id}.pdf")

        with transaction.atomic():
            PropertyReport.objects.filter(pk=report_id).update(
                status="ready",
                pdf_storage_path=storage_path,
                pdf_generated_at=timezone.now(),
                investment_score=investment_score,
                accessibility_score=accessibility_score,
                ai_summary_text=summary_text,
            )
        logger.info("Report %s generated successfully.", report_id)

    except ReportRenderError as exc:
        # A broken PDF render is not transient — retrying won't fix bad
        # data, so this fails immediately rather than burning 3 retries.
        logger.error("Report %s: unrecoverable render failure: %s", report_id, exc)
        PropertyReport.objects.filter(pk=report_id).update(status="failed", failure_reason=str(exc)[:2000])

    except StorageUploadFailed as exc:
        logger.warning("Report %s: storage upload failed (attempt %s/%s): %s", report_id, self.request.retries + 1, MAX_RETRIES, exc)
        _retry_or_fail(self, report_id, exc)

    except Exception as exc:  # noqa: BLE001 — anything else (network blips, Google quota) is treated as retryable
        logger.warning("Report %s: generation failed (attempt %s/%s): %s", report_id, self.request.retries + 1, MAX_RETRIES, exc)
        _retry_or_fail(self, report_id, exc)


def _retry_or_fail(task, report_id, exc):
    try:
        raise task.retry(exc=exc)
    except task.MaxRetriesExceededError:
        PropertyReport.objects.filter(pk=report_id).update(status="failed", failure_reason=str(exc)[:2000])
        logger.error("Report %s permanently failed after %s retries: %s", report_id, MAX_RETRIES, exc)


@shared_task
def sweep_stuck_reports():
    """
    Scheduled task — wire into CELERY_BEAT_SCHEDULE to run every few
    minutes. Catches reports stuck in 'generating' past the cutoff (e.g. a
    worker was killed mid-task, so Celery's own retry machinery never got
    a chance to run). Without this, a broker's report can hang forever
    with no automatic recovery — this is the safety net underneath the
    per-task retry logic above, not a replacement for it.
    """
    cutoff = timezone.now() - timedelta(minutes=STUCK_REPORT_CUTOFF_MINUTES)
    stuck_ids = list(
        PropertyReport.objects.filter(status="generating", updated_at__lt=cutoff).values_list("pk", flat=True)
    )
    for report_id in stuck_ids:
        generate_report_task.delay(str(report_id))
    if stuck_ids:
        logger.warning(
            "Re-queued %s stuck report(s) found in 'generating' past the %s-minute cutoff.",
            len(stuck_ids), STUCK_REPORT_CUTOFF_MINUTES,
        )
