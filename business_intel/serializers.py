from rest_framework import serializers

from .models import BusinessQuery, BusinessReport, BUSINESS_CATEGORY_CHOICES


class BusinessQueryCreateSerializer(serializers.Serializer):
    """Input for POST /api/business-intel/queries/. `email` is NOT a
    field here -- the endpoint is IsAuthenticated (see views.py), so
    identity comes from request.user, never client-supplied input."""

    raw_location_input = serializers.CharField()
    business_category = serializers.ChoiceField(choices=BUSINESS_CATEGORY_CHOICES)
    business_category_other = serializers.CharField(required=False, allow_blank=True)
    investment_amount_kes = serializers.DecimalField(max_digits=12, decimal_places=2, required=False)
    branches_planned = serializers.IntegerField(required=False, default=1, min_value=1)
    target_segment = serializers.ChoiceField(
        choices=[("budget", "Budget"), ("mid_market", "Mid-market"), ("premium", "Premium")],
        required=False, allow_blank=True,
    )

    def validate(self, data):
        if data["business_category"] == "other" and not data.get("business_category_other"):
            raise serializers.ValidationError(
                {"business_category_other": "Required when business_category is 'other'."}
            )
        return data


class BusinessReportSerializer(serializers.ModelSerializer):
    class Meta:
        model = BusinessReport
        fields = [
            "id", "status", "failure_reason",
            "opportunity_level", "recommendation", "confidence_score",
            "evidence", "risks",
            "market_gap_summary", "competition_summary", "momentum_summary", "timing",
            "ai_summary_text", "feature_snapshot_ids", "pdf_storage_path",
            "anomaly_risk_score", "anomaly_confidence", "is_anomalous",
            "is_paid", "price_charged_kes", "created_at",
        ]
        read_only_fields = fields


class BusinessReportListSerializer(serializers.ModelSerializer):
    """Lighter shape for GET /api/business-intel/reports/ (dashboard list)."""

    business_category = serializers.CharField(source="query.business_category", read_only=True)
    business_category_other = serializers.CharField(source="query.business_category_other", read_only=True)
    raw_location_input = serializers.CharField(source="query.raw_location_input", read_only=True)
    is_anomalous = serializers.BooleanField(read_only=True)

    class Meta:
        model = BusinessReport
        fields = [
            "id", "status", "opportunity_level", "recommendation",
            "confidence_score", "is_paid", "is_anomalous", "created_at",
            "business_category", "business_category_other", "raw_location_input",
        ]
        read_only_fields = fields
