from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from payments import paystack, services
from payments.models import PaystackTransaction
from payments.signals import payment_succeeded


class InitializeTransactionTests(TestCase):
    @patch("payments.services.paystack.initialize_transaction")
    def test_creates_pending_row_and_stores_paystack_response(self, mock_init):
        mock_init.return_value = {"authorization_url": "https://paystack.com/pay/x", "access_code": "abc"}
        txn = services.initialize_transaction(
            email="a@example.com", amount="500.00", purpose="property_report", external_reference="ref-1",
        )
        self.assertEqual(txn.status, "pending")
        self.assertEqual(txn.authorization_url, "https://paystack.com/pay/x")
        self.assertEqual(txn.amount, Decimal("500.00"))
        self.assertEqual(txn.amount_subunit, 50000)
        mock_init.assert_called_once()

    @patch("payments.services.paystack.initialize_transaction")
    def test_paystack_failure_leaves_row_pending_without_link(self, mock_init):
        mock_init.side_effect = paystack.PaystackAPIError("Paystack is down")
        with self.assertRaises(services.PaymentInitializationError):
            services.initialize_transaction(email="a@example.com", amount="500.00", purpose="x", external_reference="ref-2")
        txn = PaystackTransaction.objects.get(external_reference="ref-2")
        self.assertEqual(txn.status, "pending")
        self.assertEqual(txn.authorization_url, "")

    def test_rejects_non_positive_amount(self):
        with self.assertRaises(services.PaymentInitializationError):
            services.initialize_transaction(email="a@example.com", amount="0.00", purpose="x", external_reference="ref-3")


class ConfirmTransactionTests(TestCase):
    def setUp(self):
        self.received = []
        payment_succeeded.connect(self._handler, dispatch_uid="test-handler")
        self.addCleanup(payment_succeeded.disconnect, dispatch_uid="test-handler")

    def _handler(self, sender, **kwargs):
        self.received.append(kwargs)

    @patch("payments.services.paystack.initialize_transaction")
    def _make_pending_txn(self, mock_init, **overrides):
        mock_init.return_value = {"authorization_url": "https://paystack.com/pay/y", "access_code": "z"}
        defaults = dict(email="a@example.com", amount="500.00", purpose="property_report", external_reference="ref-x")
        defaults.update(overrides)
        return services.initialize_transaction(**defaults)

    def test_confirm_fires_signal_with_correct_kwargs(self):
        txn = self._make_pending_txn()
        result = services.confirm_transaction(txn.reference, paystack_transaction_id="999", channel="card")

        self.assertEqual(result.status, "success")
        self.assertEqual(len(self.received), 1)
        kwargs = self.received[0]
        self.assertEqual(kwargs["reference"], txn.reference)
        self.assertEqual(kwargs["purpose"], "property_report")
        self.assertEqual(kwargs["external_reference"], "ref-x")
        self.assertEqual(kwargs["amount"], Decimal("500.00"))
        self.assertEqual(kwargs["channel"], "card")

    def test_confirm_is_idempotent_signal_fires_once(self):
        txn = self._make_pending_txn()
        services.confirm_transaction(txn.reference)
        services.confirm_transaction(txn.reference)
        services.confirm_transaction(txn.reference)
        self.assertEqual(len(self.received), 1)

    def test_confirm_unknown_reference_returns_none_and_no_signal(self):
        result = services.confirm_transaction("SCP-DOES-NOT-EXIST")
        self.assertIsNone(result)
        self.assertEqual(len(self.received), 0)

    def test_receiver_exception_does_not_propagate(self):
        def bad_handler(sender, **kwargs):
            raise RuntimeError("boom")
        payment_succeeded.connect(bad_handler, dispatch_uid="bad-handler")
        self.addCleanup(payment_succeeded.disconnect, dispatch_uid="bad-handler")

        txn = self._make_pending_txn(external_reference="ref-y")
        # Must not raise, even though bad_handler throws — send_robust().
        result = services.confirm_transaction(txn.reference)
        self.assertEqual(result.status, "success")


class WebhookSignatureTests(TestCase):
    def test_valid_signature_accepted(self):
        import hmac, hashlib
        from django.conf import settings
        body = b'{"event": "charge.success"}'
        sig = hmac.new(settings.PAYSTACK_SECRET_KEY.encode(), body, hashlib.sha512).hexdigest()
        self.assertTrue(paystack.verify_webhook_signature(body, sig))

    def test_invalid_signature_rejected(self):
        body = b'{"event": "charge.success"}'
        self.assertFalse(paystack.verify_webhook_signature(body, "not-the-right-signature"))

    def test_missing_signature_rejected(self):
        body = b'{"event": "charge.success"}'
        self.assertFalse(paystack.verify_webhook_signature(body, ""))


class PaidViaReferenceTests(TestCase):
    @patch("payments.services.paystack.initialize_transaction")
    def test_paid_via_reference(self, mock_init):
        mock_init.return_value = {"authorization_url": "https://x", "access_code": "y"}
        txn = services.initialize_transaction(email="a@example.com", amount="500.00", purpose="p", external_reference="r")
        self.assertFalse(services.paid_via_reference(txn.reference))
        services.confirm_transaction(txn.reference)
        self.assertTrue(services.paid_via_reference(txn.reference))
