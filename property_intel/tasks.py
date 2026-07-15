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
from django.conf import settings
from django.core.mail import EmailMessage
from django.db import models, transaction
from django.utils import timezone

from . import storage
from .google_client import enrich_location_cell
from .models import PropertyReport
from .pdf import ReportRenderError, render_report_pdf
from .services import needs_enrichment
from .storage import StorageUploadFailed

logger = logging.getLogger("property_intel")

def _credit_back_report(report, reason=""):
    """
    A permanent failure/cancellation means the broker got nothing for what
    they spent — but "what they spent" differs by report type:

      - Paid report (is_paid=True): real KES was spent via wallet/Paystack.
        Crediting a free_reports_remaining slot would refund the wrong
        currency entirely — price_charged_kes goes back into the wallet
        balance instead.
      - Free-tier report: one free_reports_remaining slot back to the
        device is the correct, like-for-like refund.
      - Paid-tier report cancelled before payment ever landed (still
        awaiting_payment): nothing was charged, nothing was consumed —
        nothing to credit back.
    """
    if report.is_paid and report.price_charged_kes:
        _refund_wallet(report, reason=reason)
        return

    if not report.is_free_tier:
        logger.info(
            "Report %s: no charge landed and no free slot was consumed — nothing to credit (%s)",
            report.id, reason,
        )
        return

    from .models import DeviceFingerprint
    fingerprint_id = report.pin.broker.device_fingerprint_id
    if not fingerprint_id:
        logger.warning("Report %s: no device_fingerprint to credit — broker %s", report.id, report.pin.broker_id)
        return
    DeviceFingerprint.objects.filter(pk=fingerprint_id).update(
        free_reports_remaining=models.F("free_reports_remaining") + 1
    )
    logger.info("Report %s: credited 1 free report back to device %s (%s)", report.id, fingerprint_id, reason)


def _refund_wallet(report, reason=""):
    """
    Credit price_charged_kes back to the broker's wallet balance — this is
    what actually populates 'My Balance' on the Dashboard. No-op (with a
    warning, not a silent swallow) if the broker has no linked dashboard
    user, since there's no wallet to refund into.
    """
    user_id = report.pin.broker.user_id
    if not user_id:
        logger.warning(
            "Report %s: paid report has no linked user — cannot refund KES %s to wallet (broker %s)",
            report.id, report.price_charged_kes, report.pin.broker_id,
        )
        return

    from django.contrib.auth import get_user_model
    from payments.models import UserWallet, WalletTransaction

    User = get_user_model()
    try:
        user = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        logger.warning("Report %s: linked user %s no longer exists — cannot refund wallet", report.id, user_id)
        return

    wallet = UserWallet.get_or_create_for_user(user)
    wallet.credit(report.price_charged_kes)
    WalletTransaction.objects.create(
        wallet=wallet,
        transaction_type="refund",
        amount=report.price_charged_kes,
        balance_after=wallet.balance,
        reference=str(report.id),
        note=f"Refund for report {report.id} ({reason})",
    )
    logger.info(
        "Report %s: refunded KES %s to wallet for user %s (%s)",
        report.id, report.price_charged_kes, user_id, reason,
    )


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

    if report.status == "cancelled":
        logger.info("Report %s was cancelled — skipping.", report_id)
        return

    PropertyReport.objects.filter(pk=report_id).update(status="generating")
    cell = report.pin.location_cell

    try:
        if needs_enrichment(cell):
            cell, failures = enrich_location_cell(cell)
            if failures:
                logger.info("Report %s: enrichment had partial failures: %s", report_id, failures)
            cell.refresh_from_db()

        report.refresh_from_db(fields=["status"])
        if report.status == "cancelled":
            logger.info("Report %s cancelled mid-generation — stopping before render.", report_id)
            return

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

        try:
            broker_email = report.pin.broker.email
            EmailMessage(
                subject="Your Scape property report is ready",
                body=(
                    f"Your property report is ready.\n\n"
                    f"Download it here: {storage_path}\n\n"
                    f"This link stays live for a limited time -- save a copy if you need it later."
                ),
                from_email=settings.DEFAULT_FROM_EMAIL,
                to=[broker_email],
            ).send(fail_silently=False)
            logger.info("Report %s ready-email sent to %s", report_id, broker_email)
        except Exception as exc:
            logger.error("Report %s: ready-email failed to send: %s", report_id, exc)

    except ReportRenderError as exc:
        # A broken PDF render is not transient — retrying won't fix bad
        # data, so this fails immediately rather than burning 3 retries.
        logger.error("Report %s: unrecoverable render failure: %s", report_id, exc)
        PropertyReport.objects.filter(pk=report_id).update(status="failed", failure_reason=str(exc)[:2000])
        _credit_back_report(report, reason="unrecoverable render failure")

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
        report = PropertyReport.objects.select_related("pin__broker").get(pk=report_id)
        PropertyReport.objects.filter(pk=report_id).update(status="failed", failure_reason=str(exc)[:2000])
        _credit_back_report(report, reason=f"exhausted {MAX_RETRIES} retries")
        logger.error("Report %s permanently failed after %s retries: %s", report_id, MAX_RETRIES, exc)


REPORT_RETENTION_DAYS = 7


@shared_task
def purge_expired_reports():
    """
    Scheduled task -- wire into CELERY_BEAT_SCHEDULE to run once a day.
    Deletes PropertyReport rows (and their stored PDFs) once they're
    older than REPORT_RETENTION_DAYS. Only touches reports that actually
    finished one way or another (ready/failed/cancelled) -- never deletes
    something still pending/generating/awaiting payment, no matter how
    old, since that would silently destroy an in-flight report.
    """
    cutoff = timezone.now() - timedelta(days=REPORT_RETENTION_DAYS)
    expired = PropertyReport.objects.filter(
        status__in=["ready", "failed", "cancelled"],
        created_at__lt=cutoff,
    )

    count = 0
    for report in expired.iterator():
        if report.pdf_storage_path:
            storage.delete_object(report.pdf_storage_path)
        report.delete()
        count += 1

    if count:
        logger.info("purge_expired_reports: deleted %s report(s) older than %s days.", count, REPORT_RETENTION_DAYS)


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
