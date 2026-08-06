from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from .models import Contract, ContractChangeRequest, ContractDispute, Milestone
from .services import (
    ChangeRequestError, DisputeError, MilestonePaymentError, decide_change_request,
    initiate_milestone_payment, raise_dispute, request_contract_change, resolve_dispute,
)


def make_staff(**overrides):
    defaults = dict(username="admin@example.com", email="admin@example.com", password="testpass123", is_staff=True)
    defaults.update(overrides)
    return get_user_model().objects.create_user(**defaults)


def make_client_user(**overrides):
    defaults = dict(username="client@example.com", email="client@example.com", password="testpass123")
    defaults.update(overrides)
    return get_user_model().objects.create_user(**defaults)


def auth_client(user):
    client = APIClient()
    access = str(RefreshToken.for_user(user).access_token)
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
    return client


def make_contract(client_user=None, **overrides):
    defaults = dict(
        client_name="Jane Doe", client_email="client@example.com", title="Website rebuild",
        scope_of_work="Rebuild the marketing site.", total_value=Decimal("100000.00"),
        currency="KES", status="negotiating",
    )
    defaults.update(overrides)
    contract = Contract.objects.create(client_user=client_user, **defaults)
    return contract


class ChangeRequestServiceTests(TestCase):
    def setUp(self):
        self.owner = make_client_user()
        self.staff = make_staff()
        self.contract = make_contract(client_user=self.owner)

    def test_request_change_while_negotiating_succeeds(self):
        cr = request_contract_change(
            self.contract, requested_by_user=self.owner, reason="Need more time.",
            proposed_total_value=Decimal("120000.00"),
        )
        self.assertEqual(cr.status, "pending")

    def test_cannot_request_change_once_signed(self):
        self.contract.status = "signed"
        self.contract.save(update_fields=["status"])
        with self.assertRaises(ChangeRequestError):
            request_contract_change(self.contract, requested_by_user=self.owner, reason="Too late", proposed_total_value=Decimal("1"))

    def test_cannot_have_two_pending_requests(self):
        request_contract_change(self.contract, requested_by_user=self.owner, reason="First", proposed_total_value=Decimal("1"))
        with self.assertRaises(ChangeRequestError):
            request_contract_change(self.contract, requested_by_user=self.owner, reason="Second", proposed_total_value=Decimal("2"))

    def test_requires_at_least_one_proposed_field(self):
        # enforced at the serializer layer, not the service — confirm via the API
        client_api = auth_client(self.owner)
        response = client_api.post(
            reverse("contracts-client-change-requests", args=[self.contract.id]),
            {"reason": "I want something different but forgot to say what"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_approve_applies_revision(self):
        cr = request_contract_change(
            self.contract, requested_by_user=self.owner, reason="Bigger scope",
            proposed_total_value=Decimal("150000.00"),
        )
        decide_change_request(cr, approved=True, reviewed_by=self.staff, admin_note="Sure, makes sense.")
        self.contract.refresh_from_db()
        cr.refresh_from_db()
        self.assertEqual(cr.status, "approved")
        self.assertEqual(self.contract.total_value, Decimal("150000.00"))
        self.assertEqual(self.contract.revisions.count(), 1)

    def test_reject_does_not_touch_contract(self):
        cr = request_contract_change(
            self.contract, requested_by_user=self.owner, reason="Bigger scope",
            proposed_total_value=Decimal("999999.00"),
        )
        decide_change_request(cr, approved=False, reviewed_by=self.staff, admin_note="Not this time.")
        self.contract.refresh_from_db()
        self.assertEqual(self.contract.total_value, Decimal("100000.00"))
        self.assertEqual(self.contract.revisions.count(), 0)

    def test_cannot_decide_twice(self):
        cr = request_contract_change(self.contract, requested_by_user=self.owner, reason="x", proposed_total_value=Decimal("1"))
        decide_change_request(cr, approved=True, reviewed_by=self.staff)
        with self.assertRaises(ChangeRequestError):
            decide_change_request(cr, approved=False, reviewed_by=self.staff)

    def test_client_cannot_decide_own_request(self):
        cr = request_contract_change(self.contract, requested_by_user=self.owner, reason="x", proposed_total_value=Decimal("1"))
        client_api = auth_client(self.owner)
        response = client_api.post(reverse("contracts-change-request-decide", args=[cr.id]), {"approve": True}, format="json")
        self.assertEqual(response.status_code, 403)


class DisputeServiceTests(TestCase):
    def setUp(self):
        self.owner = make_client_user()
        self.staff = make_staff()
        self.contract = make_contract(client_user=self.owner, status="active")

    def test_raise_dispute_marks_contract_disputed(self):
        raise_dispute(self.contract, category="missed_deadline", description="Deadline missed by 2 weeks.", raised_by_user=self.owner)
        self.assertTrue(self.contract.has_open_dispute)

    def test_cannot_raise_two_open_disputes(self):
        raise_dispute(self.contract, category="other", description="First issue here.", raised_by_user=self.owner)
        with self.assertRaises(DisputeError):
            raise_dispute(self.contract, category="other", description="Second issue here.", raised_by_user=self.owner)

    @patch("contracts.services.payment_services.initialize_transaction")
    def test_open_dispute_blocks_milestone_payment(self, mock_init):
        milestone = Milestone.objects.create(contract=self.contract, title="Deposit", amount=Decimal("50000.00"))
        raise_dispute(self.contract, category="payment_issue", description="Wrong amount charged last time.", raised_by_user=self.owner)

        with self.assertRaises(MilestonePaymentError):
            initiate_milestone_payment(milestone)
        mock_init.assert_not_called()

    @patch("contracts.services.payment_services.initialize_transaction")
    def test_resolved_dispute_unblocks_milestone_payment(self, mock_init):
        from types import SimpleNamespace
        mock_init.return_value = SimpleNamespace(reference="ref123", authorization_url="https://pay.example/ref123")

        milestone = Milestone.objects.create(contract=self.contract, title="Deposit", amount=Decimal("50000.00"))
        dispute = raise_dispute(self.contract, category="payment_issue", description="Wrong amount charged last time.", raised_by_user=self.owner)
        resolve_dispute(dispute, new_status="resolved_mutual", resolved_by=self.staff, resolution_note="Refunded the difference.")

        self.assertFalse(self.contract.has_open_dispute)
        initiate_milestone_payment(milestone)  # should not raise
        mock_init.assert_called_once()

    def test_under_review_keeps_billing_frozen(self):
        dispute = raise_dispute(self.contract, category="other", description="Investigating a claim here.", raised_by_user=self.owner)
        resolve_dispute(dispute, new_status="under_review", resolved_by=self.staff)
        self.assertTrue(self.contract.has_open_dispute)

    def test_cannot_resolve_already_resolved_dispute(self):
        dispute = raise_dispute(self.contract, category="other", description="Some issue described here.", raised_by_user=self.owner)
        resolve_dispute(dispute, new_status="resolved_mutual", resolved_by=self.staff)
        with self.assertRaises(DisputeError):
            resolve_dispute(dispute, new_status="resolved_client_favor", resolved_by=self.staff)


class DisputeAPITests(TestCase):
    def setUp(self):
        self.owner = make_client_user()
        self.staff = make_staff()
        self.contract = make_contract(client_user=self.owner, status="active")

    def test_client_can_file_dispute(self):
        client_api = auth_client(self.owner)
        response = client_api.post(
            reverse("contracts-client-disputes", args=[self.contract.id]),
            {"category": "missed_deadline", "description": "The delivery was two weeks late."},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(ContractDispute.objects.filter(contract=self.contract).count(), 1)

    def test_staff_only_on_open_disputes_dashboard(self):
        client_api = auth_client(self.owner)
        response = client_api.get(reverse("contracts-admin-disputes-open"))
        self.assertEqual(response.status_code, 403)

    def test_staff_can_resolve_dispute(self):
        dispute = raise_dispute(self.contract, category="other", description="Something went wrong here.", raised_by_user=self.owner)
        client_api = auth_client(self.staff)
        response = client_api.post(
            reverse("contracts-dispute-resolve", args=[dispute.id]),
            {"status": "resolved_client_favor", "resolution_note": "Refunded in full."},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        dispute.refresh_from_db()
        self.assertEqual(dispute.status, "resolved_client_favor")
