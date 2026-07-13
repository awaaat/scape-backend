from decimal import Decimal

from django.db import IntegrityError
from django.test import TestCase

from payments.models import PaystackTransaction, PaystackWebhookEvent


class PaystackTransactionModelTests(TestCase):
    def test_amount_subunit_conversion(self):
        txn = PaystackTransaction.objects.create(
            reference="SCP-TEST1", purpose="property_report", external_reference="r1",
            email="a@example.com", amount=Decimal("500.00"), currency="KES",
        )
        self.assertEqual(txn.amount_subunit, 50000)

    def test_amount_subunit_handles_cents(self):
        txn = PaystackTransaction.objects.create(
            reference="SCP-TEST2", purpose="p", external_reference="r2",
            email="a@example.com", amount=Decimal("499.99"), currency="KES",
        )
        self.assertEqual(txn.amount_subunit, 49999)

    def test_reference_must_be_unique(self):
        PaystackTransaction.objects.create(
            reference="SCP-DUP", purpose="p", external_reference="r3", email="a@example.com", amount=Decimal("1.00"),
        )
        with self.assertRaises(IntegrityError):
            PaystackTransaction.objects.create(
                reference="SCP-DUP", purpose="p", external_reference="r4", email="b@example.com", amount=Decimal("1.00"),
            )


class PaystackWebhookEventModelTests(TestCase):
    def test_unique_constraint_on_event_type_and_reference(self):
        PaystackWebhookEvent.objects.create(event_type="charge.success", reference="SCP-A", signature_valid=True)
        with self.assertRaises(IntegrityError):
            PaystackWebhookEvent.objects.create(event_type="charge.success", reference="SCP-A", signature_valid=True)

    def test_same_reference_different_event_type_allowed(self):
        PaystackWebhookEvent.objects.create(event_type="charge.success", reference="SCP-B", signature_valid=True)
        # A different event type for the same reference is a distinct row — fine.
        PaystackWebhookEvent.objects.create(event_type="charge.failed", reference="SCP-B", signature_valid=True)
        self.assertEqual(PaystackWebhookEvent.objects.filter(reference="SCP-B").count(), 2)
