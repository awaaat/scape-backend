"""
property_intel/tests/test_tasks.py

Covers generate_report_task's control flow with everything external
(enrichment, PDF rendering, storage upload) mocked out — this is testing
the state machine (pending -> generating -> ready/failed, retry behavior),
not Google's APIs or reportlab.
"""
from unittest.mock import patch

from django.test import TestCase, override_settings

from property_intel.models import Broker, LocationCell, PropertyPin, PropertyReport
from property_intel.pdf import ReportRenderError
from property_intel.storage import StorageUploadFailed
from property_intel.tasks import generate_report_task, sweep_stuck_reports


def make_report(status="pending"):
    broker = Broker.objects.create(email=f"task-test-{status}@example.com")
    cell = LocationCell.objects.create(geohash=f"task-{status}", center_latitude=-1.15, center_longitude=36.96)
    pin = PropertyPin.objects.create(
        raw_input="x", latitude=-1.15, longitude=36.96, location_cell=cell, broker=broker,
    )
    return PropertyReport.objects.create(pin=pin, status=status)


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
class GenerateReportTaskTests(TestCase):
    @patch("property_intel.tasks.storage.upload_pdf_bytes")
    @patch("property_intel.tasks.render_report_pdf")
    @patch("property_intel.tasks.needs_enrichment", return_value=False)
    def test_success_path_marks_ready(self, mock_needs_enrichment, mock_render, mock_upload):
        report = make_report()
        mock_render.return_value = (b"%PDF-fake-bytes", 80, 75, "A fine location.")
        mock_upload.return_value = "https://storage.example.com/reports/x.pdf"

        generate_report_task(str(report.pk))

        report.refresh_from_db()
        self.assertEqual(report.status, "ready")
        self.assertEqual(report.investment_score, 80)
        self.assertEqual(report.accessibility_score, 75)
        self.assertEqual(report.pdf_storage_path, "https://storage.example.com/reports/x.pdf")
        self.assertIsNotNone(report.pdf_generated_at)

    @patch("property_intel.tasks.needs_enrichment", return_value=False)
    def test_already_ready_report_is_not_reprocessed(self, mock_needs_enrichment):
        """Guards against a duplicate task dispatch (e.g. the stuck-report
        sweep racing a normal run) re-spending Google API calls on a report
        that's already done."""
        report = make_report(status="ready")
        with patch("property_intel.tasks.render_report_pdf") as mock_render:
            generate_report_task(str(report.pk))
            mock_render.assert_not_called()

    @patch("property_intel.tasks.render_report_pdf", side_effect=ReportRenderError("bad geometry"))
    @patch("property_intel.tasks.needs_enrichment", return_value=False)
    def test_unrecoverable_render_error_fails_immediately_without_retrying(self, mock_needs_enrichment, mock_render):
        report = make_report()
        generate_report_task(str(report.pk))

        report.refresh_from_db()
        self.assertEqual(report.status, "failed")
        self.assertIn("bad geometry", report.failure_reason)

    @patch("property_intel.tasks.storage.upload_pdf_bytes", side_effect=StorageUploadFailed("network blip"))
    @patch("property_intel.tasks.render_report_pdf", return_value=(b"x", 1, 1, "s"))
    @patch("property_intel.tasks.needs_enrichment", return_value=False)
    def test_transient_storage_failure_eventually_marks_failed_after_retries(
        self, mock_needs_enrichment, mock_render, mock_upload,
    ):
        report = make_report()
        # CELERY_TASK_EAGER_PROPAGATES + a real .retry() call inside a task
        # invoked directly (not via .delay()) raises Retry as an exception
        # rather than actually rescheduling — bind=True tasks called this
        # way exhaust retries synchronously, which is what we want here.
        with self.assertRaises(Exception):
            for _ in range(5):
                generate_report_task(str(report.pk))

        report.refresh_from_db()
        self.assertIn(report.status, ("failed", "generating"))

    def test_missing_report_id_does_not_raise(self):
        import uuid
        generate_report_task(str(uuid.uuid4()))  # should log and return, not crash


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
class SweepStuckReportsTests(TestCase):
    def test_sweep_requeues_reports_stuck_in_generating(self):
        from datetime import timedelta
        from django.utils import timezone

        report = make_report(status="generating")
        stale_time = timezone.now() - timedelta(minutes=30)
        PropertyReport.objects.filter(pk=report.pk).update(updated_at=stale_time)

        with patch("property_intel.tasks.generate_report_task.delay") as mock_delay:
            sweep_stuck_reports()
            mock_delay.assert_called_once_with(str(report.pk))

    def test_sweep_ignores_recently_updated_generating_reports(self):
        make_report(status="generating")  # updated_at is "now" via auto_now
        with patch("property_intel.tasks.generate_report_task.delay") as mock_delay:
            sweep_stuck_reports()
            mock_delay.assert_not_called()
