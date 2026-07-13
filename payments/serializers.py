from decimal import Decimal

from rest_framework import serializers

from .models import PaystackTransaction


class InitializeTransactionSerializer(serializers.Serializer):
    """
    Input for POST /api/payments/initialize/. `purpose` and
    `external_reference` are opaque as far as this app is concerned — see
    payments/models.py docstring — validated here only for presence/shape,
    never for meaning.
    """
    email = serializers.EmailField()
    amount = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal("0.01"))
    currency = serializers.CharField(max_length=3, default="KES")
    purpose = serializers.CharField(max_length=50)
    external_reference = serializers.CharField(max_length=100)
    callback_url = serializers.URLField(required=False, allow_blank=True, default="")
    metadata = serializers.JSONField(required=False, default=dict)


class PaystackTransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = PaystackTransaction
        fields = [
            "reference", "purpose", "external_reference", "amount", "currency",
            "status", "authorization_url", "channel", "paid_at", "created_at",
        ]
        read_only_fields = fields
