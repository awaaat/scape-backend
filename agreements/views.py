"""
agreements/views.py

Same split as contracts/views.py: Admin (/api/agreements/...) using
IsAgreementStaff, Client (/api/agreements/client/...) resolved
per-object via permissions.get_client_authorized_agreement (client_user
OR access token — client views deliberately set no permission_classes,
same as contracts.views' client views, relying on the project-wide
AllowAny default plus the manual per-object check).

Two error-status conventions carried over from services.py's own
docstrings, not invented here:
  - AgreementStateError -> 409 (Conflict) — its docstring explicitly
    calls this "an expected outcome of a race or a stale client, not a
    bug." AgreementStateError is a subclass of AgreementError, so it's
    always caught FIRST in any except chain below; catching the base
    class first would shadow it and everything would silently become 400s.
  - Every other AgreementError subclass (missing void reason, missing
    template variables, template version errors) -> 400.

Two gaps fixed here rather than left implicit, since nothing in
services.py or the serializers covers them:
  - AgreementTemplateListCreateView.perform_create — there was no path
    to create the FIRST version of a new template family (clone_
    template_version only ever copies an existing source). Handled
    here: version=1/is_latest=True/is_active=True set directly, and a
    family_key collision is rejected with a pointer to the clone
    endpoint instead of silently violating the unique_together.
  - AgreementTemplateDetailView.perform_update — nothing was actually
    enforcing the "body_html is immutable once used" rule models.py's
    docstring repeatedly promises. Handled here: editing body_html,
    variables_schema, or agreement_type on a template that already has
    at least one Agreement is rejected outright.
"""
import logging

from django.contrib.auth import get_user_model
from ipware import get_client_ip
from rest_framework import generics, serializers as drf_serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import pdf, services
from .models import Agreement, AgreementTemplate
from .permissions import IsAgreementStaff, get_client_authorized_agreement
from .serializers import (
    AgreementCreateSerializer, AgreementEventSerializer, AgreementSerializer,
    AgreementTemplateSerializer, ClientAgreementSerializer, ClientAgreementSummarySerializer,
    CloneTemplateVersionSerializer, CountersignAgreementSerializer, DeclineAgreementSerializer,
    SendAgreementSerializer, SignAgreementSerializer, VoidAgreementSerializer,
)

logger = logging.getLogger("agreements")

# Fields models.py's docstring says are immutable once a template has
# ever been used to create an Agreement — see AgreementTemplateDetailView.
_TEMPLATE_IMMUTABLE_FIELDS_ONCE_USED = {"body_html", "variables_schema", "agreement_type"}


def _client_ip_and_ua(request):
    client_ip, _ = get_client_ip(request)
    return client_ip or "", request.META.get("HTTP_USER_AGENT", "")


# ── Admin: Templates ───────────────────────────────────────────────────

class AgreementTemplateListCreateView(generics.ListCreateAPIView):
    """
    GET  — every template version across every family (filter client-side
    by family_key/is_latest; the list is never large enough to need
    server-side filtering yet).
    POST — creates the FIRST version of a NEW template family. To add a
    new version to an EXISTING family, use AgreementTemplateCloneView
    instead — this view rejects a family_key that already exists rather
    than letting a second "version 1" silently violate the
    unique_template_version_per_family constraint.
    """
    permission_classes = [IsAgreementStaff]
    queryset = AgreementTemplate.objects.all()
    serializer_class = AgreementTemplateSerializer

    def perform_create(self, serializer):
        family_key = serializer.validated_data.get("family_key")
        if family_key and AgreementTemplate.objects.filter(family_key=family_key).exists():
            raise drf_serializers.ValidationError({
                "family_key": "A template with this family_key already exists — "
                               "use POST /templates/<pk>/clone/ to add a new version instead.",
            })
        serializer.save(created_by=self.request.user, version=1, is_latest=True, is_active=True)


class AgreementTemplateDetailView(generics.RetrieveUpdateAPIView):
    permission_classes = [IsAgreementStaff]
    queryset = AgreementTemplate.objects.all()
    serializer_class = AgreementTemplateSerializer

    def perform_update(self, serializer):
        template = self.get_object()
        if template.agreements.exists():
            attempted = _TEMPLATE_IMMUTABLE_FIELDS_ONCE_USED & set(serializer.validated_data.keys())
            if attempted:
                raise drf_serializers.ValidationError({
                    field: "This template has already been used to create an Agreement — "
                           "this field is immutable. Create a new version via /clone/ instead."
                    for field in attempted
                })
        serializer.save()


class AgreementTemplateCloneView(APIView):
    """POST /api/agreements/templates/<pk>/clone/ — creates the next
    version in this template's family. See services.clone_template_version."""
    permission_classes = [IsAgreementStaff]

    def post(self, request, pk):
        try:
            source_template = AgreementTemplate.objects.get(pk=pk)
        except AgreementTemplate.DoesNotExist:
            return Response({"error": "Template not found."}, status=status.HTTP_404_NOT_FOUND)

        serializer = CloneTemplateVersionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        new_template = services.clone_template_version(
            source_template, created_by=request.user, **serializer.validated_data,
        )
        return Response(AgreementTemplateSerializer(new_template).data, status=status.HTTP_201_CREATED)


# ── Admin: Agreements ──────────────────────────────────────────────────

class AgreementListCreateView(generics.ListAPIView):
    """
    GET  — every Agreement (admin-facing full shape, events nested).
    POST — creates one via services.create_agreement_from_template().
    Not a plain ListCreateAPIView: creation here is a real orchestration
    call (template render + validation + freeze), not a bare model save,
    so POST is handled explicitly rather than through perform_create.
    """
    permission_classes = [IsAgreementStaff]
    queryset = Agreement.objects.select_related(
        "template", "created_by", "voided_by", "countersigned_by",
    ).prefetch_related("events").all()
    serializer_class = AgreementSerializer

    def post(self, request):
        serializer = AgreementCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        client_user = None
        client_user_id = data.get("client_user")
        if client_user_id:
            client_user = get_user_model().objects.filter(pk=client_user_id).first()
            if client_user is None:
                return Response(
                    {"error": f"No user with id {client_user_id}."}, status=status.HTTP_400_BAD_REQUEST,
                )

        try:
            agreement = services.create_agreement_from_template(
                data["template"],
                created_by=request.user,
                client_name=data["client_name"],
                client_email=data["client_email"],
                client_company=data.get("client_company", ""),
                client_user=client_user,
                title=data["title"],
                variables=data.get("variables") or {},
                lead=data.get("lead"),
                project=data.get("project"),
                contract=data.get("contract"),
                effective_date=data.get("effective_date"),
                governing_law=data.get("governing_law") or None,
                direction=data.get("direction", ""),
                confidentiality_term_months=data.get("confidentiality_term_months"),
                requires_countersignature=data.get("requires_countersignature"),
            )
        except services.MissingTemplateVariablesError as exc:
            return Response(
                {"error": str(exc), "missing_keys": exc.missing_keys}, status=status.HTTP_400_BAD_REQUEST,
            )
        except pdf.TemplateRenderError as exc:
            return Response({"error": f"Template rendering failed: {exc}"}, status=status.HTTP_400_BAD_REQUEST)

        return Response(AgreementSerializer(agreement).data, status=status.HTTP_201_CREATED)


class AgreementDetailView(generics.RetrieveUpdateAPIView):
    """
    PATCH here is intentionally narrow — AgreementSerializer's
    read_only_fields lock down everything except lead/project/contract
    (re-linking an Agreement after creation, e.g. once a Lead converts
    to a Project). Every field that actually matters legally only ever
    changes through a services.py function, called from the action
    views below, never through this endpoint.
    """
    permission_classes = [IsAgreementStaff]
    queryset = Agreement.objects.select_related(
        "template", "created_by", "voided_by", "countersigned_by",
    ).prefetch_related("events").all()
    serializer_class = AgreementSerializer


class AgreementEventListView(APIView):
    """GET /api/agreements/<pk>/events/ — the audit trail alone, for
    when the full nested AgreementSerializer detail is more than a
    dashboard panel needs."""
    permission_classes = [IsAgreementStaff]

    def get(self, request, pk):
        try:
            agreement = Agreement.objects.get(pk=pk)
        except Agreement.DoesNotExist:
            return Response({"error": "Agreement not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(AgreementEventSerializer(agreement.events.all(), many=True).data)


class AgreementSendView(APIView):
    """POST /api/agreements/<pk>/send/ — first send, resend, or reopen
    after expiry; see services.send_agreement_to_client's docstring on
    why all three are one call."""
    permission_classes = [IsAgreementStaff]

    def post(self, request, pk):
        try:
            agreement = Agreement.objects.get(pk=pk)
        except Agreement.DoesNotExist:
            return Response({"error": "Agreement not found."}, status=status.HTTP_404_NOT_FOUND)

        serializer = SendAgreementSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            link = services.send_agreement_to_client(
                agreement, sent_by=request.user,
                expires_in_days=serializer.validated_data.get("expires_in_days"),
            )
        except services.AgreementStateError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_409_CONFLICT)

        return Response({"link": link, "agreement": AgreementSerializer(agreement).data})


class AgreementVoidView(APIView):
    """POST /api/agreements/<pk>/void/ — {reason: str}, mandatory."""
    permission_classes = [IsAgreementStaff]

    def post(self, request, pk):
        try:
            agreement = Agreement.objects.get(pk=pk)
        except Agreement.DoesNotExist:
            return Response({"error": "Agreement not found."}, status=status.HTTP_404_NOT_FOUND)

        serializer = VoidAgreementSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            agreement = services.void_agreement(
                agreement, by=request.user, reason=serializer.validated_data["reason"],
            )
        except services.AgreementStateError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_409_CONFLICT)
        except services.AgreementError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(AgreementSerializer(agreement).data)


class AgreementCountersignView(APIView):
    """POST /api/agreements/<pk>/countersign/ — company-side execution.
    `by` is request.user, never taken from the request body."""
    permission_classes = [IsAgreementStaff]

    def post(self, request, pk):
        try:
            agreement = Agreement.objects.get(pk=pk)
        except Agreement.DoesNotExist:
            return Response({"error": "Agreement not found."}, status=status.HTTP_404_NOT_FOUND)

        serializer = CountersignAgreementSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            agreement = services.countersign_agreement(
                agreement, by=request.user, title=serializer.validated_data.get("title", ""),
            )
        except services.AgreementStateError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_409_CONFLICT)

        return Response(AgreementSerializer(agreement).data)


# ── Client-facing ───────────────────────────────────────────────────────

class ClientAgreementListView(APIView):
    """GET /api/agreements/client/mine/ — every agreement tied to the
    logged-in client's account, newest first. Mirrors
    contracts.views' client/mine/ list — token-based (no-login) access
    doesn't apply here since there's no single request.user to filter
    by in that case; that path stays on ClientAgreementDetailView."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        agreements = Agreement.objects.filter(client_user=request.user).order_by("-created_at")
        return Response(ClientAgreementSummarySerializer(agreements, many=True).data)


class ClientAgreementDetailView(APIView):
    """
    GET /api/agreements/client/<pk>/ — every hit logs a 'viewed'
    AgreementEvent (see services.record_agreement_viewed's docstring:
    Agreement.mark_viewed() only records the FIRST view in
    first_viewed_at, but every view is still its own audit-trail row —
    a repeat GET here is not a bug, it's the certificate's audit trail
    doing its job).
    """

    def get(self, request, pk):
        agreement = get_client_authorized_agreement(request, pk)
        ip_address, user_agent = _client_ip_and_ua(request)
        services.record_agreement_viewed(agreement, ip_address=ip_address, user_agent=user_agent)
        return Response(ClientAgreementSerializer(agreement).data)


class ClientSignAgreementView(APIView):
    """POST /api/agreements/client/<pk>/sign/"""

    def post(self, request, pk):
        agreement = get_client_authorized_agreement(request, pk)

        serializer = SignAgreementSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        ip_address, user_agent = _client_ip_and_ua(request)
        try:
            agreement = services.sign_agreement(
                agreement, name=data["full_name"], email=data["email"], title=data.get("title", ""),
                method=data.get("method", "typed"), ip_address=ip_address, user_agent=user_agent,
                geolocation=data.get("geolocation") or {},
            )
        except services.AgreementStateError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_409_CONFLICT)

        return Response(ClientAgreementSerializer(agreement).data)


class ClientDeclineAgreementView(APIView):
    """POST /api/agreements/client/<pk>/decline/"""

    def post(self, request, pk):
        agreement = get_client_authorized_agreement(request, pk)

        serializer = DeclineAgreementSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        ip_address, user_agent = _client_ip_and_ua(request)
        try:
            agreement = services.record_agreement_decline(
                agreement, reason=data.get("reason", ""), declined_by_name=data.get("declined_by_name", ""),
                ip_address=ip_address, user_agent=user_agent,
            )
        except services.AgreementStateError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_409_CONFLICT)

        return Response(ClientAgreementSerializer(agreement).data)