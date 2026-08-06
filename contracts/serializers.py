from rest_framework import serializers

from .models import (
    Contract, ContractChangeRequest, ContractDispute, ContractRevision, Message, Milestone,
)


# ── Admin-facing ─────────────────────────────────────────────────────

class MilestoneSerializer(serializers.ModelSerializer):
    class Meta:
        model = Milestone
        fields = [
            "id", "contract", "title", "description", "amount", "currency", "order",
            "due_date", "status", "paystack_reference", "invoiced_at", "paid_at",
            "created_at", "updated_at",
        ]
        read_only_fields = ["status", "paystack_reference", "invoiced_at", "paid_at", "created_at", "updated_at"]


class ContractRevisionSerializer(serializers.ModelSerializer):
    edited_by_name = serializers.SerializerMethodField()

    class Meta:
        model = ContractRevision
        fields = [
            "id", "contract", "version_number", "title", "scope_of_work", "total_value",
            "currency", "edited_by", "edited_by_name", "note", "created_at",
        ]
        read_only_fields = ["version_number", "edited_by", "created_at"]

    def get_edited_by_name(self, obj):
        return obj.edited_by.get_username() if obj.edited_by_id else ""


class MessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = Message
        fields = [
            "id", "contract", "sender_type", "sender_user", "sender_name", "body",
            "created_at", "read_by_admin_at", "read_by_client_at",
        ]
        read_only_fields = ["sender_type", "sender_user", "created_at", "read_by_admin_at", "read_by_client_at"]


class ContractSerializer(serializers.ModelSerializer):
    """Full, admin-facing representation."""
    milestones = MilestoneSerializer(many=True, read_only=True)
    has_open_dispute = serializers.BooleanField(read_only=True)

    class Meta:
        model = Contract
        fields = [
            "id", "lead", "created_by", "client_name", "client_email", "client_company",
            "client_user", "title", "scope_of_work", "total_value", "currency", "status",
            "esign_method", "esign_provider", "esign_envelope_id",
            "sent_at", "signed_at", "signed_by_name", "signed_by_email",
            "contract_document", "milestones", "has_open_dispute", "created_at", "updated_at",
        ]
        read_only_fields = [
            "status", "sent_at", "signed_at", "signed_by_name", "signed_by_email",
            "created_at", "updated_at",
        ]


class ContractCreateSerializer(serializers.ModelSerializer):
    """What an admin actually fills in to open a new deal."""

    class Meta:
        model = Contract
        fields = [
            "lead", "client_name", "client_email", "client_company",
            "title", "scope_of_work", "total_value", "currency", "esign_method",
        ]


class ContractTermsUpdateSerializer(serializers.Serializer):
    """Editing terms before signing logs a ContractRevision snapshot —
    see views.ContractRevisionListCreateView."""
    title = serializers.CharField(max_length=255, required=False)
    scope_of_work = serializers.CharField(required=False, allow_blank=True)
    total_value = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, allow_null=True)
    currency = serializers.CharField(max_length=3, required=False)
    note = serializers.CharField(required=False, allow_blank=True, default="")


# ── Change requests ────────────────────────────────────────────────────

class ContractChangeRequestSerializer(serializers.ModelSerializer):
    """Read shape — used by both the admin and client endpoints (the
    client only ever sees their own, via ClientChangeRequestListCreateView)."""
    requested_by_display = serializers.SerializerMethodField()
    reviewed_by_name = serializers.SerializerMethodField()

    class Meta:
        model = ContractChangeRequest
        fields = [
            "id", "contract", "requested_by_display", "proposed_title", "proposed_scope_of_work",
            "proposed_total_value", "proposed_currency", "reason", "status", "admin_note",
            "reviewed_by_name", "reviewed_at", "created_at",
        ]
        read_only_fields = fields

    def get_requested_by_display(self, obj):
        return obj.requested_by_user.get_username() if obj.requested_by_user_id else obj.requested_by_name

    def get_reviewed_by_name(self, obj):
        return obj.reviewed_by.get_username() if obj.reviewed_by_id else ""


class ClientChangeRequestCreateSerializer(serializers.Serializer):
    """POST body a client submits to propose a change. Every proposed_*
    field is optional — only the ones actually being changed need to be
    set — but at least one must be present (see validate())."""
    proposed_title = serializers.CharField(max_length=255, required=False, allow_blank=True)
    proposed_scope_of_work = serializers.CharField(required=False, allow_blank=True)
    proposed_total_value = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, allow_null=True)
    proposed_currency = serializers.CharField(max_length=3, required=False, allow_blank=True)
    reason = serializers.CharField()
    sender_name = serializers.CharField(max_length=150, required=False, allow_blank=True)

    def validate_reason(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Tell us what you'd like changed and why.")
        return value

    def validate(self, attrs):
        proposed_fields = ["proposed_title", "proposed_scope_of_work", "proposed_total_value", "proposed_currency"]
        if not any(attrs.get(f) not in (None, "") for f in proposed_fields):
            raise serializers.ValidationError("Propose at least one change (title, scope, value, or currency).")
        return attrs


class ChangeRequestDecisionSerializer(serializers.Serializer):
    """POST body an admin submits to approve or reject a pending change request."""
    approve = serializers.BooleanField()
    admin_note = serializers.CharField(required=False, allow_blank=True, default="")


# ── Disputes ─────────────────────────────────────────────────────────

class ContractDisputeSerializer(serializers.ModelSerializer):
    raised_by_display = serializers.SerializerMethodField()
    resolved_by_name = serializers.SerializerMethodField()
    contract_title = serializers.CharField(source="contract.title", read_only=True)

    class Meta:
        model = ContractDispute
        fields = [
            "id", "contract", "contract_title", "raised_by_display", "category", "description",
            "status", "resolution_note", "resolved_by_name", "resolved_at", "created_at",
        ]
        read_only_fields = fields

    def get_raised_by_display(self, obj):
        return obj.raised_by_user.get_username() if obj.raised_by_user_id else obj.raised_by_name

    def get_resolved_by_name(self, obj):
        return obj.resolved_by.get_username() if obj.resolved_by_id else ""


class ClientDisputeCreateSerializer(serializers.Serializer):
    category = serializers.ChoiceField(choices=ContractDispute._meta.get_field("category").choices)
    description = serializers.CharField()
    sender_name = serializers.CharField(max_length=150, required=False, allow_blank=True)

    def validate_description(self, value):
        value = value.strip()
        if len(value) < 10:
            raise serializers.ValidationError("Give us a bit more detail about what went wrong (10+ characters).")
        return value


class DisputeResolveSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=[
        c for c in ContractDispute._meta.get_field("status").choices
        if c[0] not in ("open", "under_review")
    ] + [("under_review", "Under review")])
    resolution_note = serializers.CharField(required=False, allow_blank=True, default="")


# ── Client-facing contract/milestone views (deliberately excludes
# anything internal) ────────────────────────────────────────────────

class ClientContractSerializer(serializers.ModelSerializer):
    has_open_dispute = serializers.BooleanField(read_only=True)

    class Meta:
        model = Contract
        fields = [
            "id", "client_name", "client_email", "client_company",
            "title", "scope_of_work", "total_value", "currency", "status",
            "sent_at", "signed_at", "signed_by_name", "contract_document",
            "has_open_dispute", "created_at",
        ]
        read_only_fields = fields


class ClientMilestoneSerializer(serializers.ModelSerializer):
    class Meta:
        model = Milestone
        fields = ["id", "title", "description", "amount", "currency", "order", "due_date", "status", "paid_at"]
        read_only_fields = fields


class SignContractSerializer(serializers.Serializer):
    full_name = serializers.CharField(max_length=150)
    email = serializers.EmailField()
    consent = serializers.BooleanField()

    def validate_consent(self, value):
        if not value:
            raise serializers.ValidationError("You must confirm agreement to the contract terms to sign.")
        return value


class ClientMessageCreateSerializer(serializers.Serializer):
    sender_name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    body = serializers.CharField()
