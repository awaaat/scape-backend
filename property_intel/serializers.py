"""
property_intel/serializers.py
"""
from rest_framework import serializers

from .models import PropertyReport

PHONE_REGEX = r"^\+?\d{9,15}$"
OTP_CODE_REGEX = r"^\d{6}$"


class PinSubmitSerializer(serializers.Serializer):
    raw_input = serializers.CharField(max_length=2000, trim_whitespace=True)
    email = serializers.EmailField()
    fingerprint_hash = serializers.CharField(max_length=100)

    def validate_fingerprint_hash(self, value):
        value = value.strip()
        if len(value) < 16:
            raise serializers.ValidationError("Fingerprint hash looks invalid.")
        return value


class OTPRequestSerializer(serializers.Serializer):
    fingerprint_hash = serializers.CharField(max_length=100)
    phone_number = serializers.RegexField(regex=PHONE_REGEX, max_length=20)


class OTPVerifySerializer(serializers.Serializer):
    fingerprint_hash = serializers.CharField(max_length=100)
    phone_number = serializers.RegexField(regex=PHONE_REGEX, max_length=20)
    code = serializers.RegexField(regex=OTP_CODE_REGEX)
    report_id = serializers.UUIDField()


class PropertyReportSerializer(serializers.ModelSerializer):
    """
    Adds display-ready fields the frontend actually consumes (Dashboard.jsx
    reads report.address, .score, .status_display, .created_at_display —
    none of these are real model fields, they're derived here so the
    frontend never has to reach through pin/location_cell itself).
    """
    address = serializers.SerializerMethodField()
    score = serializers.IntegerField(source="investment_score", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    created_at_display = serializers.SerializerMethodField()

    class Meta:
        model = PropertyReport
        fields = [
            "id",
            "address",
            "score",
            "status",
            "status_display",
            "failure_reason",
            "investment_score",
            "accessibility_score",
            "ai_summary_text",
            "is_free_tier",
            "is_paid",
            "pdf_storage_path",
            "created_at",
            "created_at_display",
            "updated_at",
        ]
        read_only_fields = fields

    def get_address(self, obj):
        cell = obj.pin.location_cell
        return cell.formatted_address or obj.pin.raw_input

    def get_created_at_display(self, obj):
        return obj.created_at.strftime("%b %d, %Y")
