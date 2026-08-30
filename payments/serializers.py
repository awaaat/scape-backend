from decimal import Decimal

from rest_framework import serializers

from .models import PaystackTransaction, UserWallet, WalletTransaction


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


class WalletTransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = WalletTransaction
        fields = ["transaction_type", "currency", "amount", "balance_after", "reference", "note", "created_at"]
        read_only_fields = fields


class WalletSerializer(serializers.ModelSerializer):
    """
    Exposes both currency balances at once (rather than a single
    `balance`) — the frontend already knows which one is "theirs" via
    the logged-in user's country, but showing both keeps this endpoint
    honest about there being two independent balances, e.g. for a user
    who somehow ends up with a small leftover in the other currency.
    """
    transactions = WalletTransactionSerializer(many=True, read_only=True)

    class Meta:
        model = UserWallet
        fields = ["balance_kes", "balance_usd", "updated_at", "transactions"]
        read_only_fields = fields
