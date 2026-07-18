"""
contracts/views.py

Two families of endpoints:
  - Admin (/api/contracts/...)         — IsContractStaff, full control.
  - Client (/api/contracts/client/...) — access resolved per-object via
    permissions.get_client_authorized_contract() (client_user OR token).
"""
import logging

from django.utils import timezone
from ipware import get_client_ip
from rest_framework import generics, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import emails, services
from .models import Contract, Message, Milestone
from .permissions import IsContractStaff, get_client_authorized_contract
from .serializers import (
    ClientContractSerializer, ClientMessageCreateSerializer, ClientMilestoneSerializer,
    ContractCreateSerializer, ContractRevisionSerializer, ContractSerializer,
    ContractTermsUpdateSerializer, MessageSerializer, MilestoneSerializer, SignContractSerializer,
)

logger = logging.getLogger("contracts")


# ── Admin: Contracts ─────────────────────────────────────────────────

class ContractListCreateView(generics.ListCreateAPIView):
    permission_classes = [IsContractStaff]
    queryset = Contract.objects.all()

    def get_serializer_class(self):
        return ContractCreateSerializer if self.request.method == "POST" else ContractSerializer

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)


class ContractDetailView(generics.RetrieveUpdateAPIView):
    permission_classes = [IsContractStaff]
    queryset = Contract.objects.all()
    serializer_class = ContractSerializer


class ContractSendView(APIView):
    """POST /api/contracts/<pk>/send/ — generates a fresh access link and emails the client."""
    permission_classes = [IsContractStaff]

    def post(self, request, pk):
        try:
            contract = Contract.objects.get(pk=pk)
        except Contract.DoesNotExist:
            return Response({"error": "Contract not found."}, status=status.HTTP_404_NOT_FOUND)

        link = services.send_contract_to_client(contract)
        return Response({"link": link, "contract": ContractSerializer(contract).data})


class ContractRevisionListCreateView(APIView):
    """
    GET  /api/contracts/<pk>/revisions/ — history of terms edits.
    POST /api/contracts/<pk>/revisions/ — apply an edit, logging a new revision.
    """
    permission_classes = [IsContractStaff]

    def get(self, request, pk):
        try:
            contract = Contract.objects.get(pk=pk)
        except Contract.DoesNotExist:
            return Response({"error": "Contract not found."}, status=status.HTTP_404_NOT_FOUND)
        revisions = contract.revisions.all()
        return Response(ContractRevisionSerializer(revisions, many=True).data)

    def post(self, request, pk):
        try:
            contract = Contract.objects.get(pk=pk)
        except Contract.DoesNotExist:
            return Response({"error": "Contract not found."}, status=status.HTTP_404_NOT_FOUND)

        serializer = ContractTermsUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        revision = services.record_contract_revision(
            contract,
            edited_by=request.user,
            title=data.get("title"),
            scope_of_work=data.get("scope_of_work"),
            total_value=data.get("total_value"),
            currency=data.get("currency"),
            note=data.get("note", ""),
        )
        return Response(ContractRevisionSerializer(revision).data, status=status.HTTP_201_CREATED)


# ── Admin: Milestones ─────────────────────────────────────────────────

class MilestoneListCreateView(generics.ListCreateAPIView):
    permission_classes = [IsContractStaff]
    serializer_class = MilestoneSerializer

    def get_queryset(self):
        return Milestone.objects.filter(contract_id=self.kwargs["pk"])

    def perform_create(self, serializer):
        serializer.save(contract_id=self.kwargs["pk"])


class MilestoneDetailView(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = [IsContractStaff]
    queryset = Milestone.objects.all()
    serializer_class = MilestoneSerializer


class MilestoneInvoiceView(APIView):
    """POST /api/contracts/milestones/<pk>/invoice/ — starts the Paystack checkout for this milestone."""
    permission_classes = [IsContractStaff]

    def post(self, request, pk):
        try:
            milestone = Milestone.objects.select_related("contract").get(pk=pk)
        except Milestone.DoesNotExist:
            return Response({"error": "Milestone not found."}, status=status.HTTP_404_NOT_FOUND)

        try:
            result = services.initiate_milestone_payment(milestone)
        except services.MilestonePaymentError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response({
            "authorization_url": result.authorization_url,
            "reference": result.reference,
            "milestone": MilestoneSerializer(milestone).data,
        })


# ── Admin: Messages ────────────────────────────────────────────────

class AdminMessageListCreateView(APIView):
    permission_classes = [IsContractStaff]

    def get(self, request, pk):
        messages = Message.objects.filter(contract_id=pk)
        return Response(MessageSerializer(messages, many=True).data)

    def post(self, request, pk):
        try:
            contract = Contract.objects.get(pk=pk)
        except Contract.DoesNotExist:
            return Response({"error": "Contract not found."}, status=status.HTTP_404_NOT_FOUND)

        body = (request.data.get("body") or "").strip()
        if not body:
            return Response({"error": "body is required."}, status=status.HTTP_400_BAD_REQUEST)

        message = services.post_message(
            contract=contract, sender_type="admin", sender_user=request.user,
            sender_name=request.user.get_username(), body=body,
        )
        return Response(MessageSerializer(message).data, status=status.HTTP_201_CREATED)


class AdminMarkMessagesReadView(APIView):
    permission_classes = [IsContractStaff]

    def post(self, request, pk):
        unread = Message.objects.filter(contract_id=pk, sender_type="client", read_by_admin_at__isnull=True)
        count = unread.update(read_by_admin_at=timezone.now())
        return Response({"marked_read": count})


# ── Client-facing ─────────────────────────────────────────────────────

class ClientContractDetailView(APIView):
    def get(self, request, pk):
        contract = get_client_authorized_contract(request, pk)
        return Response(ClientContractSerializer(contract).data)


class ClientSignContractView(APIView):
    def post(self, request, pk):
        contract = get_client_authorized_contract(request, pk)

        if contract.status == "signed" or contract.signed_at:
            return Response({"error": "This contract has already been signed."}, status=status.HTTP_400_BAD_REQUEST)
        if contract.status not in ("sent", "negotiating"):
            return Response(
                {"error": "This contract isn't currently open for signature."}, status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = SignContractSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        client_ip, _ = get_client_ip(request)
        contract.record_signature(
            name=data["full_name"], email=data["email"],
            ip_address=client_ip or "", user_agent=request.META.get("HTTP_USER_AGENT", ""),
        )
        try:
            emails.send_contract_signed_email(contract)
        except Exception:
            logger.exception("Failed to send contract-signed email for contract %s", contract.id)
        return Response(ClientContractSerializer(contract).data)


class ClientMilestoneListView(APIView):
    def get(self, request, pk):
        contract = get_client_authorized_contract(request, pk)
        milestones = contract.milestones.all()
        return Response(ClientMilestoneSerializer(milestones, many=True).data)


class ClientMilestonePayView(APIView):
    """POST /api/contracts/client/milestones/<pk>/pay/ — starts/resumes checkout for this milestone."""

    def post(self, request, pk):
        try:
            milestone = Milestone.objects.select_related("contract").get(pk=pk)
        except Milestone.DoesNotExist:
            return Response({"error": "Milestone not found."}, status=status.HTTP_404_NOT_FOUND)

        # Reuses the same access check, keyed off the milestone's contract.
        get_client_authorized_contract(request, milestone.contract_id)

        try:
            result = services.initiate_milestone_payment(milestone)
        except services.MilestonePaymentError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response({"authorization_url": result.authorization_url, "reference": result.reference})


class ClientMessageListCreateView(APIView):
    def get(self, request, pk):
        contract = get_client_authorized_contract(request, pk)
        messages = contract.messages.all()
        return Response(MessageSerializer(messages, many=True).data)

    def post(self, request, pk):
        contract = get_client_authorized_contract(request, pk)

        serializer = ClientMessageCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        user = request.user if request.user and request.user.is_authenticated else None
        sender_name = data.get("sender_name") or (user.get_username() if user else contract.client_name)

        message = services.post_message(
            contract=contract, sender_type="client", sender_user=user,
            sender_name=sender_name, body=data["body"],
        )
        return Response(MessageSerializer(message).data, status=status.HTTP_201_CREATED)


class ClientMarkMessagesReadView(APIView):
    def post(self, request, pk):
        contract = get_client_authorized_contract(request, pk)
        unread = contract.messages.filter(sender_type="admin", read_by_client_at__isnull=True)
        count = unread.update(read_by_client_at=timezone.now())
        return Response({"marked_read": count})
