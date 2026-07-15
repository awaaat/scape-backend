import threading

from django.contrib import admin
from django.utils import timezone

from .models import (
    APICallLog,
    Broker,
    DeviceFingerprint,
    FraudReviewLog,
    LocationCell,
    OTPVerification,
    PropertyPin,
    PropertyReport,
)
from .tasks import generate_report_task


@admin.action(description="Approve selected reports (clears manual-review hold, queues generation)")
def approve_reports(modeladmin, request, queryset):
    approved = 0
    for report in queryset.filter(status="awaiting_review"):
        report.status = "pending"
        report.save(update_fields=["status"])
        threading.Thread(target=generate_report_task, args=(str(report.id),), daemon=True).start()
        approved += 1
    modeladmin.message_user(request, f"{approved} report(s) approved and queued for generation.")


@admin.register(PropertyReport)
class PropertyReportAdmin(admin.ModelAdmin):
    list_display = [
        "id", "status", "is_free_tier", "is_paid",
        "investment_score", "accessibility_score", "created_at",
    ]
    list_filter = ["status", "is_free_tier", "is_paid"]
    search_fields = ["id", "pin__raw_input", "pin__broker__email"]
    date_hierarchy = "created_at"
    actions = [approve_reports]
    readonly_fields = [
        "id", "pin", "pdf_storage_path", "pdf_generated_at",
        "investment_score", "accessibility_score", "ai_summary_text",
        "is_free_tier", "price_charged_kes", "is_paid",
        "paystack_reference", "paid_at", "created_at", "updated_at",
    ]

    def has_add_permission(self, request):
        # Reports only ever come from the pin-submission flow, never
        # created by hand — same pattern as JobApplicationAdmin.
        return False


@admin.action(description="Block selected devices")
def block_devices(modeladmin, request, queryset):
    for device in queryset:
        device.is_blocked = True
        device.block_reason = f"Manually blocked by {request.user} on {timezone.now():%Y-%m-%d %H:%M}."
        device.save(update_fields=["is_blocked", "block_reason"])
        FraudReviewLog.objects.create(
            device_fingerprint=device, action="manually_blocked",
            score=device.suspicion_score, reasons=["Manual block via admin"],
            actor=str(request.user),
        )
    modeladmin.message_user(request, f"{queryset.count()} device(s) blocked.")


@admin.action(description="Unblock selected devices")
def unblock_devices(modeladmin, request, queryset):
    for device in queryset:
        device.is_blocked = False
        device.save(update_fields=["is_blocked"])
        FraudReviewLog.objects.create(
            device_fingerprint=device, action="manually_unblocked",
            score=device.suspicion_score, reasons=["Manual unblock via admin"],
            actor=str(request.user),
        )
    modeladmin.message_user(request, f"{queryset.count()} device(s) unblocked.")


class FraudReviewLogInline(admin.TabularInline):
    model = FraudReviewLog
    extra = 0
    fields = ["action", "score", "reasons", "actor", "created_at"]
    readonly_fields = fields
    can_delete = False
    max_num = 0
    ordering = ["-created_at"]


@admin.register(DeviceFingerprint)
class DeviceFingerprintAdmin(admin.ModelAdmin):
    list_display = [
        "fingerprint_hash", "free_reports_remaining", "suspicion_score",
        "is_blocked", "is_datacenter_ip", "requires_otp_verification", "last_seen_at",
    ]
    list_filter = ["is_blocked", "is_datacenter_ip", "requires_otp_verification"]
    search_fields = ["fingerprint_hash", "ip_asn_name"]
    actions = [block_devices, unblock_devices]
    inlines = [FraudReviewLogInline]
    readonly_fields = [
        "fingerprint_hash", "free_reports_used_total", "first_seen_ip", "known_ips",
        "linked_emails", "suspicion_score", "is_datacenter_ip", "ip_asn_name",
        "otp_verified_phone", "otp_verified_at", "first_seen_at", "last_seen_at",
    ]


@admin.register(FraudReviewLog)
class FraudReviewLogAdmin(admin.ModelAdmin):
    list_display = ["device_fingerprint", "action", "score", "actor", "created_at"]
    list_filter = ["action"]
    search_fields = ["device_fingerprint__fingerprint_hash", "actor"]
    readonly_fields = [f.name for f in FraudReviewLog._meta.fields]

    def has_add_permission(self, request):
        # Append-only audit trail — every row comes from fraud.py or an
        # admin action above, never created by hand.
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Broker)
class BrokerAdmin(admin.ModelAdmin):
    list_display = ["email", "email_is_disposable", "device_fingerprint", "created_at"]
    list_filter = ["email_is_disposable"]
    search_fields = ["email"]


@admin.register(PropertyPin)
class PropertyPinAdmin(admin.ModelAdmin):
    list_display = ["id", "input_type", "latitude", "longitude", "location_cell", "broker", "was_cache_hit", "created_at"]
    list_filter = ["input_type", "was_cache_hit"]
    search_fields = ["raw_input", "broker__email"]
    date_hierarchy = "created_at"


@admin.register(LocationCell)
class LocationCellAdmin(admin.ModelAdmin):
    list_display = ["geohash", "formatted_address", "times_reused", "has_complete_data", "is_stale", "last_refreshed_at"]
    search_fields = ["geohash", "formatted_address"]


@admin.register(APICallLog)
class APICallLogAdmin(admin.ModelAdmin):
    list_display = ["api", "location_cell", "succeeded", "estimated_cost_usd", "created_at"]
    list_filter = ["api", "succeeded"]
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False


@admin.register(OTPVerification)
class OTPVerificationAdmin(admin.ModelAdmin):
    list_display = ["phone_number", "device_fingerprint", "attempts", "verified_at", "expires_at", "created_at"]
    readonly_fields = [f.name for f in OTPVerification._meta.fields]

    def has_add_permission(self, request):
        return False
