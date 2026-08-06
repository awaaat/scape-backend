"""
projects/views.py

Two families of endpoints, same split as contracts/views.py:
  - Client (/api/projects/...)       — IsAuthenticated, scoped to
    request.user's own projects via get_owned_project() / get_queryset().
  - Admin  (/api/projects/admin/...) — IsProjectStaff, sees everything.

Write endpoints (project submission, message sending) carry a
throttle_scope (see backend/settings.py REST_FRAMEWORK.DEFAULT_THROTTLE_RATES
— "project_submit", "project_message") applied only to the mutating
method via get_throttles(), so a client polling their own message
thread isn't rate-limited by the same budget as actually sending
messages.
"""
import logging

from django.db.models import Count, Q
from django.utils import timezone
from rest_framework import generics, status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from . import services
from .models import Project, ProjectMessage, ProjectStatusChange
from .pagination import ProjectResultsPagination
from .permissions import IsProjectStaff, get_owned_project
from .serializers import (
    AdminProjectSerializer,
    AdminProjectStatusUpdateSerializer,
    ConvertToContractSerializer,
    MessageCreateSerializer,
    ProjectCreateSerializer,
    ProjectMessageSerializer,
    ProjectSerializer,
    ProjectStatusChangeSerializer,
    ProjectUpdateSerializer,
)

logger = logging.getLogger("projects")


def _scoped_throttle_for(scope):
    """Builds a ScopedRateThrottle instance pinned to the given scope,
    without needing a class attribute (which would apply to every
    method on the view, including cheap GETs)."""
    throttle = ScopedRateThrottle()
    throttle.scope = scope
    return throttle


# ── Client: Projects ───────────────────────────────────────────────────

class ProjectListCreateView(generics.ListCreateAPIView):
    """
    GET  /api/projects/  — the logged-in client's own projects.
    POST /api/projects/  — post a new project (multipart if attaching a file).
    """
    permission_classes = [IsAuthenticated]
    pagination_class = ProjectResultsPagination
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get_queryset(self):
        return Project.objects.filter(owner=self.request.user)

    def get_serializer_class(self):
        return ProjectCreateSerializer if self.request.method == "POST" else ProjectSerializer

    def get_throttles(self):
        if self.request.method == "POST":
            return [_scoped_throttle_for("project_submit")]
        return super().get_throttles()

    def create(self, request, *args, **kwargs):
        serializer = ProjectCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            project = services.create_project(owner=request.user, validated_data=serializer.validated_data)
        except services.AttachmentUploadError:
            return Response(
                {"error": "Could not upload your attachment right now — please try again in a moment."},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response(ProjectSerializer(project).data, status=status.HTTP_201_CREATED)


class ProjectDetailView(APIView):
    """
    GET   /api/projects/<pk>/ — view your own project.
    PATCH /api/projects/<pk>/ — edit it, only while still client-editable
    (see models.Project.is_client_editable / CLIENT_EDITABLE_STATUSES) —
    once work has actually started, further changes go through the
    message thread instead of silently rewriting the brief.
    """
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get(self, request, pk):
        project = get_owned_project(request, pk)
        return Response(ProjectSerializer(project).data)

    def patch(self, request, pk):
        project = get_owned_project(request, pk)

        if not (request.user.is_staff or project.is_client_editable()):
            return Response(
                {"error": "This project can no longer be edited directly — send a message instead."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = ProjectUpdateSerializer(project, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        try:
            project = services.update_project(project=project, validated_data=serializer.validated_data)
        except services.AttachmentUploadError:
            return Response(
                {"error": "Could not upload your attachment right now — please try again in a moment."},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response(ProjectSerializer(project).data)


class ProjectUnreadSummaryView(APIView):
    """GET /api/projects/unread-count/ — total unread admin replies
    across all of the client's own projects, for a dashboard badge."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        count = ProjectMessage.objects.filter(
            project__owner=request.user, sender_type="admin", read_by_client_at__isnull=True,
        ).count()
        return Response({"unread_messages": count})


# ── Client: Messages ───────────────────────────────────────────────────

class ClientMessageListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get_throttles(self):
        if self.request.method == "POST":
            return [_scoped_throttle_for("project_message")]
        return []

    def get(self, request, pk):
        project = get_owned_project(request, pk)
        messages = project.messages.all()
        return Response(ProjectMessageSerializer(messages, many=True).data)

    def post(self, request, pk):
        project = get_owned_project(request, pk)

        serializer = MessageCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # This is the CLIENT-side endpoint (/api/projects/<pk>/messages/) —
        # always logged as sender_type="client", same as
        # contracts.ClientMessageListCreateView, even for the staff member
        # who's allowed to hit it for support/testing (get_owned_project()
        # lets staff through). Staff replying AS admin use the separate
        # /api/projects/admin/<pk>/messages/ endpoint below.
        message = services.post_project_message(
            project=project, sender_type="client", sender_user=request.user,
            sender_name=request.user.get_username(), body=serializer.validated_data["body"],
        )
        return Response(ProjectMessageSerializer(message).data, status=status.HTTP_201_CREATED)


class ClientMarkMessagesReadView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        project = get_owned_project(request, pk)
        unread = project.messages.filter(sender_type="admin", read_by_client_at__isnull=True)
        count = unread.update(read_by_client_at=timezone.now())
        return Response({"marked_read": count})


# ── Admin: Projects ─────────────────────────────────────────────────────

class AdminProjectListView(generics.ListAPIView):
    """GET /api/projects/admin/ — all projects. Supports ?status= and
    ?assigned_admin= filters for a basic triage view. Annotates
    unread_client_messages in the same query rather than N+1 counting
    per row (see serializers.AdminProjectSerializer.get_unread_client_messages)."""
    permission_classes = [IsProjectStaff]
    serializer_class = AdminProjectSerializer
    pagination_class = ProjectResultsPagination

    def get_queryset(self):
        qs = (
            Project.objects
            .select_related("owner", "assigned_admin", "contract")
            .annotate(
                _unread_client_messages_count=Count(
                    "messages", filter=Q(messages__sender_type="client", messages__read_by_admin_at__isnull=True)
                )
            )
        )
        status_filter = self.request.query_params.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter)
        assigned = self.request.query_params.get("assigned_admin")
        if assigned:
            qs = qs.filter(assigned_admin_id=assigned)
        return qs


class AdminProjectDetailView(APIView):
    """
    GET   /api/projects/admin/<pk>/ — full detail.
    PATCH /api/projects/admin/<pk>/ — update status and/or assigned_admin.
    Status changes go through services.change_project_status(), which
    validates the transition AND writes a ProjectStatusChange audit row.
    """
    permission_classes = [IsProjectStaff]

    def get(self, request, pk):
        try:
            project = Project.objects.select_related("owner", "assigned_admin", "contract").get(pk=pk)
        except Project.DoesNotExist:
            return Response({"error": "Project not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(AdminProjectSerializer(project).data)

    def patch(self, request, pk):
        try:
            project = Project.objects.get(pk=pk)
        except Project.DoesNotExist:
            return Response({"error": "Project not found."}, status=status.HTTP_404_NOT_FOUND)

        # instance= is required here — AdminProjectStatusUpdateSerializer
        # .validate_status() reads self.instance.status to enforce the
        # allowed-transitions map.
        serializer = AdminProjectStatusUpdateSerializer(instance=project, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        if "assigned_admin" in data:
            project.assigned_admin = data["assigned_admin"]
            project.save(update_fields=["assigned_admin", "updated_at"])

        if "status" in data:
            try:
                project = services.change_project_status(
                    project=project, new_status=data["status"], changed_by=request.user,
                    note=data.get("note", ""),
                )
            except services.ProjectConversionError as exc:
                return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(AdminProjectSerializer(project).data)


class AdminProjectStatusHistoryView(APIView):
    """GET /api/projects/admin/<pk>/status-history/ — the audit trail
    written by services.change_project_status()."""
    permission_classes = [IsProjectStaff]

    def get(self, request, pk):
        changes = ProjectStatusChange.objects.filter(project_id=pk).select_related("changed_by")
        return Response(ProjectStatusChangeSerializer(changes, many=True).data)


class AdminProjectConvertToContractView(APIView):
    """
    POST /api/projects/admin/<pk>/convert-to-contract/

    Formalizes a project into a real contracts.Contract — see
    services.convert_project_to_contract for exactly what gets copied
    over and why. From here on, milestones/e-signature/payments are
    handled entirely by the contracts app; this project keeps existing
    as the (now-linked) original intake record.
    """
    permission_classes = [IsProjectStaff]

    def post(self, request, pk):
        try:
            project = Project.objects.get(pk=pk)
        except Project.DoesNotExist:
            return Response({"error": "Project not found."}, status=status.HTTP_404_NOT_FOUND)

        serializer = ConvertToContractSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            contract = services.convert_project_to_contract(
                project, created_by=request.user,
                title=data.get("title"), scope_of_work=data.get("scope_of_work"),
                total_value=data.get("total_value"), currency=data.get("currency"),
            )
        except services.ProjectConversionError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            {"contract_id": str(contract.id), "project": AdminProjectSerializer(project).data},
            status=status.HTTP_201_CREATED,
        )


class AdminUnreadSummaryView(APIView):
    """GET /api/projects/admin/unread-count/ — unread client messages
    across ALL projects, plus a count of projects still awaiting first
    triage. Meant for an admin dashboard badge."""
    permission_classes = [IsProjectStaff]

    def get(self, request):
        unread_messages = ProjectMessage.objects.filter(
            sender_type="client", read_by_admin_at__isnull=True,
        ).count()
        pending_review = Project.objects.filter(status="submitted").count()
        return Response({"unread_messages": unread_messages, "pending_review": pending_review})


# ── Admin: Messages ────────────────────────────────────────────────────

class AdminMessageListCreateView(APIView):
    permission_classes = [IsProjectStaff]

    def get_throttles(self):
        if self.request.method == "POST":
            return [_scoped_throttle_for("project_message")]
        return []

    def get(self, request, pk):
        messages = ProjectMessage.objects.filter(project_id=pk)
        return Response(ProjectMessageSerializer(messages, many=True).data)

    def post(self, request, pk):
        try:
            project = Project.objects.get(pk=pk)
        except Project.DoesNotExist:
            return Response({"error": "Project not found."}, status=status.HTTP_404_NOT_FOUND)

        serializer = MessageCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        message = services.post_project_message(
            project=project, sender_type="admin", sender_user=request.user,
            sender_name=request.user.get_username(), body=serializer.validated_data["body"],
        )
        return Response(ProjectMessageSerializer(message).data, status=status.HTTP_201_CREATED)


class AdminMarkMessagesReadView(APIView):
    permission_classes = [IsProjectStaff]

    def post(self, request, pk):
        unread = ProjectMessage.objects.filter(project_id=pk, sender_type="client", read_by_admin_at__isnull=True)
        count = unread.update(read_by_admin_at=timezone.now())
        return Response({"marked_read": count})
