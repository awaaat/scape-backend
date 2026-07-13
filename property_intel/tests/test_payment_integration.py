"""
property_intel/tests/test_payment_integration.py

Tests the property_intel side of the payments split: the
payment_succeeded receiver in signals.py, and the two views.py branches
that now call payments.services.initialize_transaction() instead of just
returning a bare 402.

Never imports payments' internal models here — only its public
signals/services surface, exactly like the production code does.
"""
import uuid
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from payments import services as payment_services
from payments.signals import payment_succeeded

from property_intel.models import Broker, DeviceFingerprint, LocationCell, PropertyPin, PropertyReport, compute_geohash
from property_intel.signals import REPORT_PAYMENT_PURPOSE


class PaymentSucceededReceiverTests(TestCase):
    def setUp(self):
        self.fingerprint = DeviceFingerprint.objects.create(fingerprint_hash="fp-payment-test-000001", free_reports_remaining=0)
        self.broker = Broker.objects.create(email="broker@example.com", device_fingerprint=self.fingerprint)
        cell = LocationCell.objects.create(geohash=compute_geohash(-1.28, 36.82), center_latitude=-1.28, center_longitude=36.82)
        self.pin = PropertyPin.objects.create(
            raw_input="-1.28,36.82", input_type="coordinates", latitude=-1.28, longitude=36.82,
            location_cell=cell, broker=self.broker,
        )
        self.report = PropertyReport.objects.create(pin=self.pin, status="awaiting_payment", is_free_tier=False, price_charged_kes=500)

    @patch("property_intel.signals.generate_report_task.delay")
    def test_payment_succeeded_marks_report_paid_and_dispatches_generation(self, mock_delay):
        payment_succeeded.send_robust(
            sender=object(), reference="SCP-ABC", purpose=REPORT_PAYMENT_PURPOSE,
            external_reference=str(self.report.id), amount=Decimal("500.00"), currency="KES",
            email=self.broker.email, channel="mobile_money", paid_at=None, metadata={},
        )
        self.report.refresh_from_db()
        self.assertTrue(self.report.is_paid)
        self.assertEqual(self.report.status, "pending")
        self.assertEqual(self.report.paystack_reference, "SCP-ABC")
        self.assertEqual(self.report.price_charged_kes, Decimal("500.00"))
        mock_delay.assert_called_once_with(str(self.report.id))

    @patch("property_intel.signals.generate_report_task.delay")
    def test_ignores_events_for_other_purposes(self, mock_delay):
        payment_succeeded.send_robust(
            sender=object(), reference="SCP-XYZ", purpose="some_other_feature",
            external_reference=str(self.report.id), amount=Decimal("10.00"), currency="KES",
            email="x@example.com", channel="card", paid_at=None, metadata={},
        )
        self.report.refresh_from_db()
        self.assertFalse(self.report.is_paid)
        mock_delay.assert_not_called()

    @patch("property_intel.signals.generate_report_task.delay")
    def test_malformed_external_reference_does_not_raise(self, mock_delay):
        # Should log and return quietly — not raise back into the signal dispatcher.
        payment_succeeded.send_robust(
            sender=object(), reference="SCP-BAD", purpose=REPORT_PAYMENT_PURPOSE,
            external_reference="not-a-uuid-at-all", amount=Decimal("500.00"), currency="KES",
            email="x@example.com", channel="card", paid_at=None, metadata={},
        )
        mock_delay.assert_not_called()

    @patch("property_intel.signals.generate_report_task.delay")
    def test_duplicate_event_for_already_paid_report_does_not_redispatch(self, mock_delay):
        self.report.is_paid = True
        self.report.save(update_fields=["is_paid"])
        payment_succeeded.send_robust(
            sender=object(), reference="SCP-DUP", purpose=REPORT_PAYMENT_PURPOSE,
            external_reference=str(self.report.id), amount=Decimal("500.00"), currency="KES",
            email="x@example.com", channel="card", paid_at=None, metadata={},
        )
        mock_delay.assert_not_called()

    @patch("property_intel.signals.generate_report_task.delay")
    def test_unknown_report_id_does_not_raise(self, mock_delay):
        payment_succeeded.send_robust(
            sender=object(), reference="SCP-GHOST", purpose=REPORT_PAYMENT_PURPOSE,
            external_reference=str(uuid.uuid4()), amount=Decimal("500.00"), currency="KES",
            email="x@example.com", channel="card", paid_at=None, metadata={},
        )
        mock_delay.assert_not_called()


class PinSubmitPaymentFlowTests(TestCase):
    """Covers the two views.py branches that now call payment_services.initialize_transaction()."""

    def setUp(self):
        DeviceFingerprint.objects.create(fingerprint_hash="fp-exhausted-000000001", free_reports_remaining=0)

    @patch("payments.services.paystack.initialize_transaction")
    def test_free_tier_exhausted_returns_checkout_url(self, mock_init):
        mock_init.return_value = {"authorization_url": "https://paystack.com/pay/checkout1", "access_code": "c1"}
        resp = self.client.post("/api/property/pins/", data={
            "raw_input": "-1.30,36.80", "email": "broker2@example.com", "fingerprint_hash": "fp-exhausted-000000001",
        }, content_type="application/json")
        self.assertEqual(resp.status_code, 402, resp.content)
        body = resp.json()
        self.assertEqual(body["checkout_url"], "https://paystack.com/pay/checkout1")
        self.assertEqual(body["status"], "awaiting_payment")
        report = PropertyReport.objects.get(pk=body["report_id"])
        self.assertEqual(report.price_charged_kes, Decimal("500"))
        self.assertTrue(report.paystack_reference.startswith("SCP-"))

    @patch("payments.services.paystack.initialize_transaction")
    def test_payment_init_failure_returns_honest_402_without_crashing(self, mock_init):
        from payments import paystack
        mock_init.side_effect = paystack.PaystackAPIError("Paystack unreachable")
        resp = self.client.post("/api/property/pins/", data={
            "raw_input": "-1.30,36.80", "email": "broker3@example.com", "fingerprint_hash": "fp-exhausted-000000001",
        }, content_type="application/json")
        self.assertEqual(resp.status_code, 402)
        self.assertNotIn("checkout_url", resp.json())
        self.assertIn("message", resp.json())
