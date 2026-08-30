"""
agreements/serializers.py

Mirrors contracts/serializers.py's conventions deliberately: full
ModelSerializers for admin read/list views, plain Serializers for
action payloads (send/sign/decline/void/countersign — these aren't
"edit a field," they're "do a thing," so they don't pretend to be
model forms), and a separate, deliberately-smaller client-facing
serializer that never exposes internal fields (voided_by, created_by,
signed_ip forensics, etc.) to whoever's on the other end of an
access-token link.

Nothing here calls into services.py — these are shape/input-validation
only. Every action payload below maps 1:1 onto a services.py function's
keyword arguments; the view layer is what actually calls
services.sign_agreement(**serializer.validated_data) etc. and is where
AgreementError/AgreementStateError/MissingTemplateVariablesError get
caught and turned into 400/404/409 responses — not here.
"""
from rest_framework import serializers

from .models import Agreement, AgreementEvent, AgreementTemplate


# ── Templates ────────────────────────────────────────────────────────────

class AgreementTemplateSerializer(serializers.ModelSerializer):
    """Full, admin-facing representation of a template version."""

    created_by_name = serializers.SerializerMethodField()

    class Meta:
        model = AgreementTemplate
        fields = [
            "id", "family_key", "version", "agreement_type", "name", "description",
            "body_html", "variables_schema", "default_confidentiality_term_months",
            "default_governing_law", "requires_countersignature", "default_signing_deadline_days",
            "is_active", "is_latest", "created_by", "created_by_name", "created_at", "updated_at",
        ]
        # version/is_latest are maintained by services.clone_template_version(),
        # never hand-set — see that function's docstring on why a bare
        # .save() here would break the "exactly one is_latest per family" invariant.
        read_only_fields = ["version", "is_latest", "created_by", "created_at", "updated_at"]

    def get_created_by_name(self, obj):
        return obj.created_by.get_username() if obj.created_by_id else ""


class CloneTemplateVersionSerializer(serializers.Serializer):
    """
    POST body for creating a new version of an existing template family
    — maps directly onto services.clone_template_version()'s keyword
    arguments. `source_template` itself is resolved by the view from
    the URL (which family/version you're cloning from), not from this
    body.

    Every field is optional: omitting one means "carry the source
    template's value forward unchanged" (see clone_template_version's
    field_overrides.get(key, source_template.key) pattern) — this
    serializer's validated_data is passed straight through as
    **field_overrides, so only fields actually present here should
    reach that call, which the view achieves by dropping unset keys
    before calling (partial=True-style: read validated_data, don't
    fill blanks with None).
    """
    body_html = serializers.CharField(required=False, allow_blank=False)
    description = serializers.CharField(required=False, allow_blank=True, default="")
    agreement_type = serializers.ChoiceField(
        choices=AgreementTemplate._meta.get_field("agreement_type").choices, required=False,
    )
    name = serializers.CharField(max_length=255, required=False)
    variables_schema = serializers.JSONField(required=False)
    default_confidentiality_term_months = serializers.IntegerField(
        required=False, allow_null=True, min_value=1,
    )
    default_governing_law = serializers.CharField(max_length=150, required=False, allow_blank=True)
    requires_countersignature = serializers.BooleanField(required=False)
    default_signing_deadline_days = serializers.IntegerField(required=False, min_value=1)


# ── Audit trail ──────────────────────────────────────────────────────────

class AgreementEventSerializer(serializers.ModelSerializer):
    """Read-only — AgreementEvent rows are never created or edited through
    the API directly, only via services._log_event() as a side effect
    of some other action. This is what a Certificate of Completion PDF
    is generated from; the API exposure is purely so an admin dashboard
    can render the same trail without waiting for a PDF."""

    actor_display = serializers.SerializerMethodField()

    class Meta:
        model = AgreementEvent
        fields = [
            "id", "agreement", "event_type", "actor_type", "actor_user", "actor_display",
            "actor_name", "ip_address", "user_agent", "metadata", "created_at",
        ]
        read_only_fields = fields

    def get_actor_display(self, obj):
        return obj.actor_user.get_username() if obj.actor_user_id else (obj.actor_name or obj.get_actor_type_display())


# ── Agreements — admin-facing ─────────────────────────────────────────────

class AgreementSerializer(serializers.ModelSerializer):
    """
    Full, admin-facing representation. Nests the complete event trail —
    unlike Contract's Milestone nesting (which is a small, fixed set),
    an Agreement's events list only grows on meaningful state changes
    (sent/viewed/signed/etc, not per-request), so nesting the full
    trail here is cheap and saves a second round-trip for what's
    usually the first thing an admin wants to see on this record.
    """
    events = AgreementEventSerializer(many=True, read_only=True)
    created_by_name = serializers.SerializerMethodField()
    voided_by_name = serializers.SerializerMethodField()
    countersigned_by_name = serializers.SerializerMethodField()
    is_signable = serializers.BooleanField(read_only=True)
    is_fully_executed = serializers.BooleanField(read_only=True)
    is_expired = serializers.BooleanField(read_only=True)

    class Meta:
        model = Agreement
        fields = [
            "id", "template", "agreement_type", "template_version", "title",
            "rendered_content_html", "content_variables",
            "lead", "project", "contract",
            "client_name", "client_email", "client_company", "client_user",
            "created_by", "created_by_name", "status",
            "sent_at", "first_viewed_at", "expires_at",
            "signed_at", "signed_by_name", "signed_by_email", "signed_by_title",
            "signature_method", "signed_ip", "signed_content_hash",
            "declined_at", "declined_reason",
            "voided_at", "voided_by", "voided_by_name", "void_reason",
            "requires_countersignature", "countersigned_at",
            "countersigned_by", "countersigned_by_name", "countersigned_by_title",
            "effective_date", "governing_law", "direction", "confidentiality_term_months",
            "generated_pdf_url", "certificate_pdf_url",
            "is_signable", "is_fully_executed", "is_expired",
            "events", "created_at", "updated_at",
        ]
        # Every field here changes only through a services.py function —
        # this serializer is read/list-facing. Actual mutation happens
        # via the action serializers below, never a PATCH against this one.
        read_only_fields = [f for f in fields if f not in (
            "lead", "project", "contract",  # an admin may re-link these after creation
        )]

    def get_created_by_name(self, obj):
        return obj.created_by.get_username() if obj.created_by_id else ""

    def get_voided_by_name(self, obj):
        return obj.voided_by.get_username() if obj.voided_by_id else ""

    def get_countersigned_by_name(self, obj):
        return obj.countersigned_by.get_username() if obj.countersigned_by_id else ""


class AgreementCreateSerializer(serializers.Serializer):
    """
    What an admin actually fills in to create an Agreement — maps
    directly onto services.create_agreement_from_template()'s keyword
    arguments. `template` is a full AgreementTemplate instance by the
    time it reaches that function; here it's just the PK the view
    resolves (PrimaryKeyRelatedField, filtered to is_active=True so a
    retired template can't be picked for a new Agreement — see
    AgreementTemplate.is_active's docstring).

    `variables` is validated for shape only (must be a JSON object) —
    the actual required-keys-present check against the template's
    variables_schema happens in services._validate_required_variables(),
    which needs the resolved template instance and so can't run here;
    duplicating it here would just be a second, easier-to-drift copy
    of the same rule.
    """
    template = serializers.PrimaryKeyRelatedField(
        queryset=AgreementTemplate.objects.filter(is_active=True),
    )
    client_name = serializers.CharField(max_length=150)
    client_email = serializers.EmailField()
    client_company = serializers.CharField(max_length=150, required=False, allow_blank=True, default="")
    client_user = serializers.IntegerField(required=False, allow_null=True)
    title = serializers.CharField(max_length=255)
    variables = serializers.JSONField(required=False, default=dict)

    lead = serializers.UUIDField(required=False, allow_null=True)
    project = serializers.UUIDField(required=False, allow_null=True)
    contract = serializers.UUIDField(required=False, allow_null=True)

    effective_date = serializers.DateField(required=False, allow_null=True)
    governing_law = serializers.CharField(max_length=150, required=False, allow_null=True)
    # direction's choices live on Agreement (NDA_DIRECTION_CHOICES), not
    # AgreementTemplate — Agreement is already imported above, so this
    # is resolved directly at class-body execution time, no post-hoc patch needed.
    direction = serializers.ChoiceField(
        choices=Agreement._meta.get_field("direction").choices, required=False, allow_blank=True, default="",
    )
    confidentiality_term_months = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    requires_countersignature = serializers.BooleanField(required=False, allow_null=True, default=None)

    def validate_variables(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError("variables must be a JSON object of {key: value} pairs.")
        return value


# ── Agreements — action payloads ──────────────────────────────────────────

class SendAgreementSerializer(serializers.Serializer):
    """
    POST body for send/resend/reopen — all three are the same
    services.send_agreement_to_client() call (see that function's
    docstring on why). expires_in_days is optional and, per that
    docstring, only actually matters when resending from 'expired';
    an in-flight 'sent'/'viewed' resend ignores it and keeps the
    existing deadline.
    """
    expires_in_days = serializers.IntegerField(required=False, allow_null=True, min_value=1, default=None)


class SignAgreementSerializer(serializers.Serializer):
    """POST body a client submits to sign — same consent-gate pattern
    as contracts.SignContractSerializer."""
    full_name = serializers.CharField(max_length=150)
    email = serializers.EmailField()
    title = serializers.CharField(max_length=150, required=False, allow_blank=True, default="")
    method = serializers.ChoiceField(
        choices=Agreement._meta.get_field("signature_method").choices, default="typed",
    )
    consent = serializers.BooleanField()
    geolocation = serializers.JSONField(required=False, default=dict)

    def validate_consent(self, value):
        if not value:
            raise serializers.ValidationError("You must confirm agreement to the document's terms to sign.")
        return value


class DeclineAgreementSerializer(serializers.Serializer):
    """POST body a client submits to explicitly refuse."""
    reason = serializers.CharField(required=False, allow_blank=True, default="")
    declined_by_name = serializers.CharField(max_length=150, required=False, allow_blank=True, default="")


class VoidAgreementSerializer(serializers.Serializer):
    """POST body an admin submits to invalidate a sent-or-signed
    Agreement. reason is mandatory — see services.void_agreement()'s
    own hard requirement; enforcing it here too just means the bad
    request never reaches the service layer at all."""
    reason = serializers.CharField()

    def validate_reason(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("A reason is required to void an agreement.")
        return value


class CountersignAgreementSerializer(serializers.Serializer):
    """POST body an admin submits to countersign. `by` (the actual
    signatory user) comes from request.user in the view, not this body
    — title is the only thing genuinely being supplied here."""
    title = serializers.CharField(max_length=150, required=False, allow_blank=True, default="")


# ── Client-facing (access-token or linked-account view) ───────────────────
# Deliberately excludes everything internal: created_by, voided_by,
# countersigned_by (the FK — countersigned_by_title is fine, it's just a
# role label), signed_ip/user_agent forensics, content_variables (raw
# merge-field inputs), and the full event trail. A client sees the
# document and its own status, not the forensic record kept for
# disputes — same reasoning as contracts.ClientContractSerializer.

class ClientAgreementSummarySerializer(serializers.ModelSerializer):
    """Lightweight list-view shape for GET /api/agreements/client/mine/ —
    excludes rendered_content_html (large, only needed when opening one
    specific agreement) but keeps everything needed to render a summary
    row: type, title, status, and the date fields."""
    is_signable = serializers.BooleanField(read_only=True)
    is_fully_executed = serializers.BooleanField(read_only=True)

    class Meta:
        model = Agreement
        fields = [
            "id", "agreement_type", "title", "status",
            "sent_at", "expires_at", "signed_at",
            "is_signable", "is_fully_executed", "created_at",
        ]
        read_only_fields = fields


class ClientAgreementSerializer(serializers.ModelSerializer):
    is_signable = serializers.BooleanField(read_only=True)
    is_fully_executed = serializers.BooleanField(read_only=True)

    class Meta:
        model = Agreement
        fields = [
            "id", "agreement_type", "title", "rendered_content_html",
            "client_name", "client_email", "client_company",
            "status", "sent_at", "expires_at",
            "signed_at", "signed_by_name", "signed_by_title",
            "requires_countersignature", "countersigned_at", "countersigned_by_title",
            "effective_date", "governing_law", "direction", "confidentiality_term_months",
            "generated_pdf_url", "certificate_pdf_url",
            "is_signable", "is_fully_executed", "created_at",
        ]
        read_only_fields = fields