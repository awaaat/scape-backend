from rest_framework import serializers
from django.db import models

from .models import (
    Contract, ContractChangeRequest, ContractDispute, ContractRevision, Invoice, Message,
    Milestone, MilestoneDeliverable, MilestoneEvent,
)


# ── Admin-facing ─────────────────────────────────────────────────────

class MilestoneDeliverableSerializer(serializers.ModelSerializer):
    submitted_by_name = serializers.SerializerMethodField()

    class Meta:
        model = MilestoneDeliverable
        fields = ["id", "milestone", "version", "summary", "file_url", "file_filename", "link_url", "submitted_by_name", "created_at"]
        read_only_fields = fields

    def get_submitted_by_name(self, obj):
        return obj.submitted_by.get_username() if obj.submitted_by_id else ""


class MilestoneEventSerializer(serializers.ModelSerializer):
    actor_display = serializers.SerializerMethodField()

    class Meta:
        model = MilestoneEvent
        fields = ["id", "milestone", "event_type", "actor_type", "actor_display", "metadata", "created_at"]
        read_only_fields = fields

    def get_actor_display(self, obj):
        return obj.actor_user.get_username() if obj.actor_user_id else obj.actor_name


class MilestoneSerializer(serializers.ModelSerializer):
    has_open_dispute = serializers.BooleanField(read_only=True)
    latest_deliverable = MilestoneDeliverableSerializer(read_only=True)

    class Meta:
        model = Milestone
        fields = [
            "id", "contract", "title", "description", "amount", "currency", "order",
            "due_date", "status", "paystack_reference", "invoice", "invoiced_at", "funded_at",
            "submitted_at", "auto_release_days", "auto_release_at", "released_at",
            "auto_released", "released_by_user", "has_open_dispute", "latest_deliverable",
            "created_at", "updated_at",
        ]
        read_only_fields = [
            "status", "paystack_reference", "invoice", "invoiced_at", "funded_at", "submitted_at",
            "auto_release_at", "released_at", "auto_released", "released_by_user",
            "created_at", "updated_at",
        ]


class InvoiceMilestoneSerializer(serializers.ModelSerializer):
    """Line-item shape for an invoice's bundled milestones — just enough
    to render the bill, not the full MilestoneSerializer."""
    class Meta:
        model = Milestone
        fields = ["id", "title", "description", "amount", "currency", "order", "status"]
        read_only_fields = fields


class InvoiceSerializer(serializers.ModelSerializer):
    milestones = InvoiceMilestoneSerializer(many=True, read_only=True)
    created_by_name = serializers.SerializerMethodField()
    manual_payment_recorded_by_name = serializers.SerializerMethodField()
    is_overdue = serializers.BooleanField(read_only=True)

    class Meta:
        model = Invoice
        fields = [
            "id", "contract", "invoice_number", "total_amount", "currency", "status", "notes",
            "issued_at", "due_at", "is_overdue", "created_by", "created_by_name",
            "sent_at", "sent_via_email", "sent_via_message", "pdf_url",
            "paystack_reference", "paid_at", "payment_method", "is_manual_payment",
            "manual_payment_reference", "manual_payment_notes",
            "manual_payment_recorded_by", "manual_payment_recorded_by_name",
            "void_reason", "milestones", "created_at", "updated_at",
        ]
        read_only_fields = [f for f in fields if f != "notes"]

    def get_created_by_name(self, obj):
        return obj.created_by.get_username() if obj.created_by_id else ""

    def get_manual_payment_recorded_by_name(self, obj):
        return obj.manual_payment_recorded_by.get_username() if obj.manual_payment_recorded_by_id else ""


class CreateInvoiceSerializer(serializers.Serializer):
    """Input for POST /contracts/milestones/<pk>/create-invoice/ — the
    milestone itself comes from the URL; this just carries optional
    overrides."""
    due_in_days = serializers.IntegerField(required=False, min_value=1)
    notes = serializers.CharField(required=False, allow_blank=True, default="")


class RecordManualInvoicePaymentSerializer(serializers.Serializer):
    payment_method = serializers.ChoiceField(choices=[
        c for c in Invoice._meta.get_field("payment_method").choices if c[0] != "paystack"
    ])
    manual_payment_reference = serializers.CharField(required=False, allow_blank=True, default="")
    manual_payment_notes = serializers.CharField(required=False, allow_blank=True, default="")


class VoidInvoiceSerializer(serializers.Serializer):
    void_reason = serializers.CharField(required=False, allow_blank=True, default="")


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
    has_unsigned_required_agreements = serializers.BooleanField(read_only=True)
    unsigned_required_agreement_types = serializers.ListField(read_only=True)

    class Meta:
        model = Contract
        fields = [
            "id", "lead", "created_by", "client_name", "client_email", "client_company",
            "client_user", "title", "scope_of_work", "total_value", "currency", "status",
            "esign_method", "esign_provider", "esign_envelope_id",
            "required_agreement_types", "has_unsigned_required_agreements",
            "unsigned_required_agreement_types",
            "sent_at", "signed_at", "signed_by_name", "signed_by_email",
            "contract_document_url", "milestones", "has_open_dispute", "created_at", "updated_at",
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
            "required_agreement_types",
        ]


class ContractTermsUpdateSerializer(serializers.Serializer):
    """Editing terms before signing logs a ContractRevision snapshot —
    see views.ContractRevisionListCreateView."""
    title = serializers.CharField(max_length=255, required=False)
    scope_of_work = serializers.CharField(required=False, allow_blank=True)
    total_value = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, allow_null=True)
    currency = serializers.CharField(max_length=3, required=False)
    note = serializers.CharField(required=False, allow_blank=True, default="")


# ── Milestone escrow actions ────────────────────────────────────────────

class SubmitMilestoneDeliverableSerializer(serializers.Serializer):
    """POST body for the admin-only 'I've delivered this' action —
    always Scape's side, see contracts/models.py's module docstring on
    why there's no separate onboarded-provider role here."""
    summary = serializers.CharField()
    file = serializers.FileField(required=False)
    link_url = serializers.URLField(required=False, allow_blank=True)

    def validate_summary(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Describe what was delivered.")
        return value

    def validate(self, attrs):
        if not attrs.get("file") and not attrs.get("link_url"):
            raise serializers.ValidationError("Attach a file or a link to the delivered work.")
        return attrs


class RequestMilestoneRevisionSerializer(serializers.Serializer):
    """Client-only — asks for changes to a submitted deliverable."""
    note = serializers.CharField()

    def validate_note(self, value):
        value = value.strip()
        if len(value) < 5:
            raise serializers.ValidationError("Explain what needs to change.")
        return value


class ApproveMilestoneReleaseSerializer(serializers.Serializer):
    """Client-only — approves a submitted deliverable and releases funds.
    No real fields needed; `confirm` exists purely so an empty POST body
    can't silently trigger a release by accident."""
    confirm = serializers.BooleanField()

    def validate_confirm(self, value):
        if not value:
            raise serializers.ValidationError("Confirm you want to approve and release this milestone.")
        return value


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
    milestone_title = serializers.SerializerMethodField()

    class Meta:
        model = ContractDispute
        fields = [
            "id", "contract", "contract_title", "milestone", "milestone_title",
            "raised_by_display", "category", "description",
            "status", "resolution_note", "resolved_by_name", "resolved_at", "created_at",
        ]
        read_only_fields = fields

    def get_raised_by_display(self, obj):
        return obj.raised_by_user.get_username() if obj.raised_by_user_id else obj.raised_by_name

    def get_resolved_by_name(self, obj):
        return obj.resolved_by.get_username() if obj.resolved_by_id else ""

    def get_milestone_title(self, obj):
        return obj.milestone.title if obj.milestone_id else ""


class ClientDisputeCreateSerializer(serializers.Serializer):
    """`milestone_id` is optional — set it to scope the freeze to one
    milestone's release only; leave it out for the original, broader
    contract-wide billing freeze. Validated against the contract this
    is being filed on via context['contract'], set by the view."""
    category = serializers.ChoiceField(choices=ContractDispute._meta.get_field("category").choices)
    description = serializers.CharField()
    milestone_id = serializers.UUIDField(required=False, allow_null=True)
    sender_name = serializers.CharField(max_length=150, required=False, allow_blank=True)

    def validate_description(self, value):
        value = value.strip()
        if len(value) < 10:
            raise serializers.ValidationError("Give us a bit more detail about what went wrong (10+ characters).")
        return value

    def validate_milestone_id(self, value):
        contract = self.context.get("contract")
        if value and contract and not contract.milestones.filter(id=value).exists():
            raise serializers.ValidationError("That milestone doesn't belong to this contract.")
        return value


class DisputeResolveSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=[
        c for c in ContractDispute._meta.get_field("status").choices
        if c[0] not in ("open", "under_review")
    ] + [("under_review", "Under review")])
    resolution_note = serializers.CharField(required=False, allow_blank=True, default="")
    # Only meaningful when resolving a milestone-scoped dispute — lets an
    # admin override the DISPUTE_TO_MILESTONE_STATUS default mapping
    # (services.resolve_dispute) case by case. Ignored for contract-wide
    # disputes (dispute.milestone is None).
    milestone_resolution_status = serializers.ChoiceField(
        choices=Milestone._meta.get_field("status").choices, required=False, allow_null=True,
    )


# ── Client-facing contract/milestone views (deliberately excludes
# anything internal) ────────────────────────────────────────────────

class ClientContractSerializer(serializers.ModelSerializer):
    has_open_dispute = serializers.BooleanField(read_only=True)

    class Meta:
        model = Contract
        fields = [
            "id", "client_name", "client_email", "client_company",
            "title", "scope_of_work", "total_value", "currency", "status",
            "sent_at", "signed_at", "signed_by_name", "contract_document_url",
            "has_open_dispute", "created_at",
        ]
        read_only_fields = fields


class ClientMilestoneDeliverableSerializer(serializers.ModelSerializer):
    """Client-safe subset — no submitted_by, that's internal."""
    class Meta:
        model = MilestoneDeliverable
        fields = ["id", "version", "summary", "file_url", "file_filename", "link_url", "created_at"]
        read_only_fields = fields


class ClientMilestoneSerializer(serializers.ModelSerializer):
    has_open_dispute = serializers.BooleanField(read_only=True)
    latest_deliverable = ClientMilestoneDeliverableSerializer(read_only=True)

    class Meta:
        model = Milestone
        fields = [
            "id", "title", "description", "amount", "currency", "order", "due_date", "status",
            "invoice", "funded_at", "submitted_at", "auto_release_at", "released_at",
            "has_open_dispute", "latest_deliverable",
        ]
        read_only_fields = fields


class ClientInvoiceSerializer(serializers.ModelSerializer):
    """Client-safe subset — no created_by/manual_payment internals, just
    what a client paying (or checking) the bill needs to see."""
    milestones = InvoiceMilestoneSerializer(many=True, read_only=True)
    is_overdue = serializers.BooleanField(read_only=True)

    class Meta:
        model = Invoice
        fields = [
            "id", "invoice_number", "total_amount", "currency", "status",
            "issued_at", "due_at", "is_overdue", "pdf_url", "paid_at",
            "milestones", "created_at",
        ]
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

class ClientContractListSerializer(serializers.ModelSerializer):
    """
    Lightweight summary shape for GET /api/contracts/client/mine/ — the
    dashboard Overview page's contracts card. Deliberately NOT the full
    ClientContractSerializer: no scope_of_work, no contract_document_url —
    this is a list of tiles, not a detail view.
    """
    has_open_dispute = serializers.BooleanField(read_only=True)
    milestones_total = serializers.SerializerMethodField()
    milestones_open = serializers.SerializerMethodField()
    amount_released = serializers.SerializerMethodField()

    class Meta:
        model = Contract
        fields = [
            "id", "title", "status", "total_value", "currency",
            "has_open_dispute", "milestones_total", "milestones_open",
            "amount_released", "created_at",
        ]
        read_only_fields = fields

    def get_milestones_total(self, obj):
        return obj.milestones.count()

    def get_milestones_open(self, obj):
        return obj.milestones.exclude(status__in=["released", "waived", "cancelled"]).count()

    def get_amount_released(self, obj):
        total = obj.milestones.filter(status="released").aggregate(total=models.Sum("amount"))["total"]
        return str(total or 0)
