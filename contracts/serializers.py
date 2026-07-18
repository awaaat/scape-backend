from rest_framework import serializers

from .models import Contract, ContractRevision, Message, Milestone


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

    class Meta:
        model = Contract
        fields = [
            "id", "lead", "created_by", "client_name", "client_email", "client_company",
            "client_user", "title", "scope_of_work", "total_value", "currency", "status",
            "esign_method", "esign_provider", "esign_envelope_id",
            "sent_at", "signed_at", "signed_by_name", "signed_by_email",
            "contract_document", "milestones", "created_at", "updated_at",
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


# ── Client-facing (deliberately excludes anything internal) ──────────

class ClientContractSerializer(serializers.ModelSerializer):
    class Meta:
        model = Contract
        fields = [
            "id", "client_name", "client_email", "client_company",
            "title", "scope_of_work", "total_value", "currency", "status",
            "sent_at", "signed_at", "signed_by_name", "contract_document", "created_at",
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
