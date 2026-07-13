from django.contrib import admin

from .models import PaystackTransaction, PaystackWebhookEvent


@admin.register(PaystackTransaction)
class PaystackTransactionAdmin(admin.ModelAdmin):
    list_display = ("reference", "purpose", "external_reference", "email", "amount", "currency", "status", "channel", "created_at")
    list_filter = ("status", "purpose", "currency")
    search_fields = ("reference", "external_reference", "email", "paystack_transaction_id")
    readonly_fields = ("id", "created_at", "updated_at")
    ordering = ("-created_at",)


@admin.register(PaystackWebhookEvent)
class PaystackWebhookEventAdmin(admin.ModelAdmin):
    list_display = ("event_type", "reference", "signature_valid", "processed", "received_at")
    list_filter = ("event_type", "signature_valid", "processed")
    search_fields = ("reference",)
    readonly_fields = ("event_type", "reference", "raw_payload", "signature_valid", "received_at")
    ordering = ("-received_at",)
