from django.contrib import admin

from .models import Lead


@admin.register(Lead)
class LeadAdmin(admin.ModelAdmin):
    list_display = [
        "name",
        "email",
        "service",
        "company",
        "created_at",
        "is_processed",
        "welcome_email_sent",
        "admin_notified",
    ]
    list_filter = ["service", "is_processed", "created_at"]
    search_fields = ["name", "email", "company", "phone", "message"]
    readonly_fields = [
        "created_at",
        "visitor",
        "welcome_email_sent",
        "admin_notified",
        "synced_to_brevo",
    ]
    list_editable = ["is_processed"]
    date_hierarchy = "created_at"
