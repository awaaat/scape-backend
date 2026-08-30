from django.contrib.auth import get_user_model
from rest_framework import serializers

from .models import ALLOWED_ATTACHMENT_EXTENSIONS, Project, ProjectMessage, ProjectStatusChange


def _validate_attachment_file(value):
    """Shared by create and update — extension allow-list + size cap
    (settings.PROJECT_ATTACHMENT_MAX_SIZE_MB), enforced BEFORE anything
    is sent to Supabase (see storage.upload_project_attachment), so a
    rejected file never burns an upload call."""
    if value is None:
        return value
    from django.conf import settings

    ext = value.name.rsplit(".", 1)[-1].lower() if "." in value.name else ""
    if ext not in ALLOWED_ATTACHMENT_EXTENSIONS:
        raise serializers.ValidationError(
            f"'.{ext}' isn't an allowed file type. Allowed: {', '.join(sorted(ALLOWED_ATTACHMENT_EXTENSIONS))}."
        )
    max_mb = getattr(settings, "PROJECT_ATTACHMENT_MAX_SIZE_MB", 15)
    if value.size > max_mb * 1024 * 1024:
        raise serializers.ValidationError(f"Attachment must be smaller than {max_mb}MB.")
    return value



def _validate_budget_value(value):
    """Shared by create and update — mirrors the model's
    project_budget_non_negative CheckConstraint so bad input fails as a
    clean 400 here instead of an IntegrityError from the database."""
    if value is not None and value < 0:
        raise serializers.ValidationError("Budget can't be negative.")
    return value


def _validate_currency_value(value):
    """Shared by create and update — mirrors Project.CURRENCY_CHOICES."""
    from .models import Project

    valid_codes = {code for code, _ in Project.CURRENCY_CHOICES}
    value = (value or "").upper()
    if value not in valid_codes:
        raise serializers.ValidationError(
            f"'{value}' isn't a supported currency. Choose one of: {', '.join(sorted(valid_codes))}."
        )
    return value


# ── Client-facing ─────────────────────────────────────────────────────

class ProjectCreateSerializer(serializers.ModelSerializer):
    """What a client fills in to post a new project. owner/status are
    set server-side (see views.ProjectListCreateView.perform_create).
    `attachment` is write-only and not a model field — services.create_project
    uploads it to Supabase and populates attachment_url/filename/content_type."""

    attachment = serializers.FileField(write_only=True, required=False, allow_null=True)

    class Meta:
        model = Project
        fields = ["title", "description", "category", "budget", "currency", "deadline", "attachment"]

    def validate_title(self, value):
        value = value.strip()
        if len(value) < 3:
            raise serializers.ValidationError("Give your project a real title.")
        return value

    def validate_description(self, value):
        value = value.strip()
        if len(value) < 20:
            raise serializers.ValidationError("Add a bit more detail so we understand what you need (20+ characters).")
        return value

    def validate_attachment(self, value):
        return _validate_attachment_file(value)

    def validate_budget(self, value):
        return _validate_budget_value(value)

    def validate_currency(self, value):
        return _validate_currency_value(value)


class ProjectUpdateSerializer(serializers.ModelSerializer):
    """PATCH body for a client editing their own project — only while
    it's still in a client-editable status (see views.ProjectDetailView)."""

    attachment = serializers.FileField(write_only=True, required=False, allow_null=True)

    class Meta:
        model = Project
        fields = ["title", "description", "category", "budget", "currency", "deadline", "attachment"]
        extra_kwargs = {f: {"required": False} for f in ["title", "description", "category", "budget", "currency", "deadline"]}

    def validate_attachment(self, value):
        return _validate_attachment_file(value)

    def validate_budget(self, value):
        return _validate_budget_value(value)

    def validate_currency(self, value):
        return _validate_currency_value(value)


class ProjectSerializer(serializers.ModelSerializer):
    """Client-facing read representation — no internal-only fields
    like assigned_admin."""

    owner_name = serializers.SerializerMethodField()

    class Meta:
        model = Project
        fields = [
            "id", "owner_name", "title", "description", "category", "budget", "currency",
            "deadline", "attachment_url", "attachment_filename", "status", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "status", "created_at", "updated_at"]

    def get_owner_name(self, obj):
        return obj.owner.get_full_name() or obj.owner.get_username()


# ── Admin-facing ─────────────────────────────────────────────────────

class ProjectStatusChangeSerializer(serializers.ModelSerializer):
    changed_by_name = serializers.SerializerMethodField()

    class Meta:
        model = ProjectStatusChange
        fields = ["id", "from_status", "to_status", "note", "changed_by_name", "created_at"]

    def get_changed_by_name(self, obj):
        return obj.changed_by.get_username() if obj.changed_by_id else "system"


class AdminProjectSerializer(serializers.ModelSerializer):
    """Full representation, includes internal fields the client never sees."""

    owner_name = serializers.SerializerMethodField()
    owner_email = serializers.SerializerMethodField()
    assigned_admin_name = serializers.SerializerMethodField()
    unread_client_messages = serializers.SerializerMethodField()

    class Meta:
        model = Project
        fields = [
            "id", "owner", "owner_name", "owner_email", "title", "description", "category",
            "budget", "currency", "deadline", "attachment_url", "attachment_filename", "status",
            "assigned_admin", "assigned_admin_name", "contract", "unread_client_messages",
            "created_at", "updated_at",
        ]
        read_only_fields = ["id", "owner", "contract", "created_at", "updated_at"]

    def get_owner_name(self, obj):
        return obj.owner.get_full_name() or obj.owner.get_username()

    def get_owner_email(self, obj):
        return obj.owner.email

    def get_assigned_admin_name(self, obj):
        return obj.assigned_admin.get_username() if obj.assigned_admin_id else ""

    def get_unread_client_messages(self, obj):
        # Relies on the caller having annotated this via .annotate(...)
        # where possible (see views.AdminProjectListView) — falls back
        # to a live count for single-object GETs so it's always correct,
        # just not always the cheapest path.
        if hasattr(obj, "_unread_client_messages_count"):
            return obj._unread_client_messages_count
        return obj.messages.filter(sender_type="client", read_by_admin_at__isnull=True).count()


class AdminProjectStatusUpdateSerializer(serializers.Serializer):
    """POST/PATCH body admins use to move a project along or assign it.
    Status transitions are validated against models.Project.can_transition_to()
    in validate_status() below — needs the instance, so the view must
    pass instance= when constructing this serializer."""

    status = serializers.ChoiceField(choices=Project._meta.get_field("status").choices, required=False)
    note = serializers.CharField(required=False, allow_blank=True, max_length=1000)
    assigned_admin = serializers.PrimaryKeyRelatedField(
        queryset=get_user_model().objects.filter(is_staff=True), required=False, allow_null=True,
    )

    def validate_status(self, value):
        project = self.instance
        if project is not None and not project.can_transition_to(value):
            raise serializers.ValidationError(
                f"Can't move a project from '{project.status}' to '{value}'."
            )
        return value


class ConvertToContractSerializer(serializers.Serializer):
    """POST body for /api/projects/admin/<pk>/convert-to-contract/ — all
    fields optional, defaulting to the project's own title/description/
    budget/currency (see services.convert_project_to_contract)."""

    title = serializers.CharField(max_length=255, required=False)
    scope_of_work = serializers.CharField(required=False, allow_blank=True)
    total_value = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, allow_null=True)
    currency = serializers.CharField(max_length=3, required=False)


# ── Messages (shared shape, admin and client both read this) ─────────

class ProjectMessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProjectMessage
        fields = [
            "id", "project", "sender_type", "sender_user", "sender_name", "body",
            "created_at", "read_by_admin_at", "read_by_client_at",
        ]
        read_only_fields = ["sender_type", "sender_user", "created_at", "read_by_admin_at", "read_by_client_at"]


class MessageCreateSerializer(serializers.Serializer):
    """POST body for sending a message, either side. sender_type/sender_user
    are always set server-side from the authenticated caller — never
    trusted from the client (see views.py)."""

    body = serializers.CharField(max_length=10000)

    def validate_body(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Message body can't be empty.")
        return value
