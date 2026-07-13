import hashlib
import hmac
import json
from unittest.mock import patch

from django.conf import settings
from django.test import Client, TestCase

from payments import services
from payments.models import PaystackTransaction, PaystackWebhookEvent


def _signed_post(client, path, payload, secret=None):
    body = json.dumps(payload).encode()
    secret = secret if secret is not None else settings.PAYSTACK_SECRET_KEY
    sig = hmac.new(secret.encode(), body, hashlib.sha512).hexdigest()
    return client.post(path, data=body, content_type="application/json", HTTP_X_PAYSTACK_SIGNATURE=sig)


class InitializeTransactionViewTests(TestCase):
    @patch("payments.services.paystack.initialize_transaction")
    def test_initialize_returns_checkout_url(self, mock_init):
        mock_init.return_value = {"authorization_url": "https://paystack.com/pay/abc", "access_code": "code"}
        resp = self.client.post("/api/payments/initialize/", data=json.dumps({
            "email": "broker@example.com", "amount": "500.00", "purpose": "property_report", "external_reference": "rep-1",
        }), content_type="application/json")
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.json()["authorization_url"], "https://paystack.com/pay/abc")

    def test_initialize_rejects_missing_fields(self):
        resp = self.client.post("/api/payments/initialize/", data=json.dumps({"email": "broker@example.com"}), content_type="application/json")
        self.assertEqual(resp.status_code, 400)

    @patch("payments.services.paystack.initialize_transaction")
    def test_initialize_returns_502_on_paystack_failure(self, mock_init):
        from payments import paystack
        mock_init.side_effect = paystack.PaystackAPIError("down")
        resp = self.client.post("/api/payments/initialize/", data=json.dumps({
            "email": "broker@example.com", "amount": "500.00", "purpose": "property_report", "external_reference": "rep-2",
        }), content_type="application/json")
        self.assertEqual(resp.status_code, 502)


class WebhookViewTests(TestCase):
    @patch("payments.services.paystack.initialize_transaction")
    def _pending_txn(self, mock_init, external_reference="rep-x"):
        mock_init.return_value = {"authorization_url": "https://x", "access_code": "y"}
        return services.initialize_transaction(email="a@example.com", amount="500.00", purpose="property_report", external_reference=external_reference)

    def test_charge_success_confirms_transaction(self):
        txn = self._pending_txn()
        resp = _signed_post(self.client, "/api/payments/webhook/", {
            "event": "charge.success", "data": {"reference": txn.reference, "status": "success", "id": 1, "channel": "mobile_money"},
        })
        self.assertEqual(resp.status_code, 200)
        txn.refresh_from_db()
        self.assertEqual(txn.status, "success")
        self.assertEqual(PaystackWebhookEvent.objects.filter(reference=txn.reference).count(), 1)

    def test_duplicate_delivery_is_noop(self):
        txn = self._pending_txn()
        payload = {"event": "charge.success", "data": {"reference": txn.reference, "status": "success", "id": 1}}
        _signed_post(self.client, "/api/payments/webhook/", payload)
        resp2 = _signed_post(self.client, "/api/payments/webhook/", payload)
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(resp2.json()["status"], "duplicate")
        self.assertEqual(PaystackWebhookEvent.objects.filter(reference=txn.reference).count(), 1)

    def test_bad_signature_rejected_and_logged(self):
        txn = self._pending_txn()
        resp = _signed_post(self.client, "/api/payments/webhook/", {
            "event": "charge.success", "data": {"reference": txn.reference, "status": "success", "id": 1},
        }, secret="wrong-secret")
        self.assertEqual(resp.status_code, 401)
        txn.refresh_from_db()
        self.assertEqual(txn.status, "pending")  # untouched
        event = PaystackWebhookEvent.objects.get(reference=txn.reference)
        self.assertFalse(event.signature_valid)

    def test_charge_failed_marks_transaction_failed(self):
        txn = self._pending_txn()
        resp = _signed_post(self.client, "/api/payments/webhook/", {
            "event": "charge.failed", "data": {"reference": txn.reference, "status": "failed", "gateway_response": "Insufficient funds"},
        })
        self.assertEqual(resp.status_code, 200)
        txn.refresh_from_db()
        self.assertEqual(txn.status, "failed")

    def test_unrecognized_event_type_acknowledged_but_not_acted_on(self):
        txn = self._pending_txn()
        resp = _signed_post(self.client, "/api/payments/webhook/", {
            "event": "transfer.success", "data": {"reference": txn.reference, "status": "success", "id": 1},
        })
        self.assertEqual(resp.status_code, 200)
        txn.refresh_from_db()
        self.assertEqual(txn.status, "pending")  # not a charge event — untouched

    def test_malformed_body_returns_400(self):
        resp = self.client.post("/api/payments/webhook/", data=b"not json", content_type="application/json", HTTP_X_PAYSTACK_SIGNATURE="whatever")
        self.assertEqual(resp.status_code, 400)


class VerifyTransactionViewTests(TestCase):
    @patch("payments.services.paystack.initialize_transaction")
    def test_verify_confirms_when_paystack_says_success(self, mock_init):
        mock_init.return_value = {"authorization_url": "https://x", "access_code": "y"}
        txn = services.initialize_transaction(email="a@example.com", amount="500.00", purpose="property_report", external_reference="rep-v")

        with patch("payments.services.paystack.verify_transaction") as mock_verify:
            mock_verify.return_value = {"status": "success", "id": 42, "channel": "card"}
            resp = self.client.get(f"/api/payments/verify/{txn.reference}/")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "success")

    def test_verify_unknown_reference_404s(self):
        resp = self.client.get("/api/payments/verify/SCP-NOPE/")
        self.assertEqual(resp.status_code, 404)
