import re
from datetime import timedelta

from django.utils import timezone
from rest_framework import serializers

from .models import Order, OrderFile

_ANSWER_KEY_RE = re.compile(r"^[a-z0-9_]{1,40}$")


class OrderWriteSerializer(serializers.Serializer):
    """Everything is optional — the wizard saves step by step, and a half-filled
    order must never be rejected. Completeness (name/email/service) is only
    enforced at submit time, in services.missing_fields()."""

    # contact
    name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    email = serializers.EmailField(required=False, allow_blank=True)
    phone = serializers.CharField(max_length=40, required=False, allow_blank=True)
    country = serializers.CharField(max_length=80, required=False, allow_blank=True)
    # order
    service = serializers.CharField(max_length=150, required=False, allow_blank=True)
    academic_level = serializers.CharField(max_length=60, required=False, allow_blank=True)
    data_status = serializers.CharField(max_length=80, required=False, allow_blank=True)
    software = serializers.CharField(max_length=60, required=False, allow_blank=True)
    deadline_window = serializers.CharField(max_length=60, required=False, allow_blank=True)
    deadline = serializers.DateField(required=False, allow_null=True)
    topic = serializers.CharField(max_length=200, required=False, allow_blank=True)
    details = serializers.CharField(max_length=5000, required=False, allow_blank=True)
    answers = serializers.DictField(required=False)
    step = serializers.IntegerField(min_value=0, max_value=30, required=False)
    # create-time attribution (ignored on later saves)
    page_url = serializers.CharField(max_length=500, required=False, allow_blank=True)
    referrer = serializers.CharField(max_length=500, required=False, allow_blank=True)
    utm = serializers.DictField(child=serializers.CharField(max_length=200, allow_blank=True), required=False)
    # which storefront is calling (optional; otherwise matched from Origin)
    site = serializers.CharField(max_length=40, required=False, allow_blank=True)
    # honeypot — a real browser never fills this
    website = serializers.CharField(required=False, allow_blank=True)

    def to_internal_value(self, data):
        """Custom fields: any field a step of this site declares (e.g. "budget")
        may be sent top-level like a core field; it is filed under `answers`."""
        site = self.context.get("site")
        extra = {}
        if site and hasattr(data, "get"):
            for key in site["answer_fields"]:
                if key in data and key not in self.fields:
                    extra[key] = data[key]
        value = super().to_internal_value(data)
        if extra:
            try:
                cleaned = self.validate_answers(extra)
            except serializers.ValidationError as exc:
                raise serializers.ValidationError({"answers": exc.detail})
            value["answers"] = {**value.get("answers", {}), **cleaned}
        return value

    def validate_name(self, value):
        value = " ".join(value.split())
        if value and len(value) < 2:
            raise serializers.ValidationError("Name looks too short.")
        return value

    def validate_email(self, value):
        return value.strip().lower()

    def validate_phone(self, value):
        value = value.strip()
        if not value:
            return ""
        cleaned = re.sub(r"[^\d+]", "", value)
        if cleaned.count("+") > 1 or ("+" in cleaned and not cleaned.startswith("+")):
            raise serializers.ValidationError("Enter a valid phone / WhatsApp number with country code.")
        digits = cleaned.lstrip("+")
        if not 7 <= len(digits) <= 15:
            raise serializers.ValidationError("Enter a valid phone / WhatsApp number with country code.")
        return cleaned

    def validate_deadline(self, value):
        if value and value < timezone.localdate() - timedelta(days=1):
            raise serializers.ValidationError("Deadline can't be in the past.")
        return value

    def validate_answers(self, value):
        if len(value) > 25:
            raise serializers.ValidationError("Too many answers.")
        clean = {}
        for k, v in value.items():
            if not isinstance(k, str) or not _ANSWER_KEY_RE.match(k):
                raise serializers.ValidationError(f"Invalid answer key: {k!r}")
            if v is None:
                clean[k] = None  # means "remove this answer"
            elif isinstance(v, (bool, int, float)):
                clean[k] = v
            elif isinstance(v, str):
                if len(v) > 1000:
                    raise serializers.ValidationError(f"Answer '{k}' is too long.")
                clean[k] = v.strip()
            elif isinstance(v, list):
                if len(v) > 15 or not all(isinstance(i, str) and len(i) <= 100 for i in v):
                    raise serializers.ValidationError(f"Answer '{k}' is invalid.")
                clean[k] = [i.strip() for i in v]
            else:
                raise serializers.ValidationError(f"Answer '{k}' has an unsupported type.")
        return clean

    def validate_utm(self, value):
        if len(value) > 10:
            raise serializers.ValidationError("Too many campaign parameters.")
        return value


class OrderFileSerializer(serializers.ModelSerializer):
    class Meta:
        model = OrderFile
        fields = ["id", "original_name", "size_bytes", "created_at"]


class OrderSerializer(serializers.ModelSerializer):
    """What the client's browser gets back. Never includes admin_notes,
    notification flags, visitor data or file URLs."""

    token = serializers.CharField(source="public_token", read_only=True)
    files = OrderFileSerializer(many=True, read_only=True)
    is_editable = serializers.BooleanField(read_only=True)

    class Meta:
        model = Order
        fields = [
            "token", "reference", "site", "status", "is_editable", "current_step",
            "name", "email", "phone", "country",
            "service_slug", "service", "academic_level", "data_status", "software",
            "deadline_window", "deadline", "topic", "details", "extra_answers",
            "files", "created_at", "submitted_at",
        ]
        read_only_fields = fields
