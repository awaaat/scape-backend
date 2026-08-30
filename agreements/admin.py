from django.contrib import admin

from .models import Agreement, AgreementEvent, AgreementTemplate


@admin.register(AgreementTemplate)
class AgreementTemplateAdmin(admin.ModelAdmin):
    list_display = ("family_key", "version", "agreement_type", "is_latest", "is_active", "created_at")
    list_filter = ("agreement_type", "is_latest", "is_active")
    search_fields = ("family_key", "title")


class AgreementEventInline(admin.TabularInline):
    model = AgreementEvent
    extra = 0
    readonly_fields = [f.name for f in AgreementEvent._meta.fields]
    can_delete = False


@admin.register(Agreement)
class AgreementAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "client_name", "client_email", "agreement_type", "status", "created_at")
    list_filter = ("status", "agreement_type", "requires_countersignature")
    search_fields = ("title", "client_name", "client_email")
    inlines = [AgreementEventInline]
