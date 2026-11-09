from django.contrib import admin, messages
from django.utils.html import format_html

from . import emails
from .models import Order, OrderFile


class OrderFileInline(admin.TabularInline):
    model = OrderFile
    extra = 0
    can_delete = True
    fields = ("original_name", "size_bytes", "link")
    readonly_fields = ("original_name", "size_bytes", "link")

    def link(self, obj):
        return format_html('<a href="{}" target="_blank" rel="noopener">Open</a>', obj.url) if obj.pk else ""

    def has_add_permission(self, request, obj=None):
        return False


def _set_status(status):
    def action(modeladmin, request, queryset):
        n = queryset.update(status=status)
        modeladmin.message_user(request, f"{n} order(s) marked '{status}'.", messages.SUCCESS)
    action.short_description = f"Mark selected as: {status}"
    action.__name__ = f"mark_{status}"
    return action


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = (
        "reference", "site", "status", "display_name", "email", "whatsapp", "service",
        "deadline", "days_left_col", "files_col", "last_activity_at",
    )
    list_filter = ("status", "site", "service_slug", "academic_level", "software", "submitted_at")
    search_fields = ("reference", "name", "email", "phone", "service", "topic", "details")
    date_hierarchy = "last_activity_at"
    list_select_related = ("visitor",)
    list_per_page = 50
    inlines = [OrderFileInline]
    actions = [
        _set_status("contacted"), _set_status("quoted"), _set_status("won"),
        _set_status("lost"), _set_status("spam"), "resend_admin_email",
    ]
    readonly_fields = (
        "reference", "public_token", "visitor", "created_at", "updated_at", "last_activity_at",
        "submitted_at", "current_step", "admin_notified", "confirmation_sent",
        "abandoned_notified", "recovery_sent", "package",
    )
    fieldsets = (
        ("Order package (copy-paste friendly)", {"fields": ("package",)}),
        ("Pipeline", {"fields": ("site", "status", "admin_notes")}),
        ("Contact", {"fields": ("name", "email", "phone", "country")}),
        ("Order", {"fields": (
            "service_slug", "service", "academic_level", "data_status", "software",
            "deadline_window", "deadline", "topic", "details", "extra_answers",
        )}),
        ("Funnel and attribution", {"fields": (
            "reference", "public_token", "current_step", "source_url", "referrer", "utm", "visitor",
            "created_at", "last_activity_at", "submitted_at",
        )}),
        ("Notifications", {"fields": ("admin_notified", "confirmation_sent", "abandoned_notified", "recovery_sent")}),
    )

    @admin.display(description="WhatsApp")
    def whatsapp(self, obj):
        if not obj.whatsapp_link:
            return "-"
        return format_html('<a href="{}" target="_blank" rel="noopener">{}</a>', obj.whatsapp_link, obj.phone)

    @admin.display(description="Due")
    def days_left_col(self, obj):
        d = obj.days_left
        if d is None:
            return "-"
        color = "#b42318" if d <= 3 else "inherit"
        return format_html('<span style="color:{};font-weight:600">{}d</span>', color, d)

    @admin.display(description="Files")
    def files_col(self, obj):
        return obj.files.count()

    @admin.display(description="Package")
    def package(self, obj):
        return format_html('<pre style="white-space:pre-wrap;margin:0">{}</pre>', obj.as_text()) if obj.pk else "-"

    @admin.action(description="Re-send the admin notification email")
    def resend_admin_email(self, request, queryset):
        sent = 0
        for order in queryset.exclude(status=Order.Status.DRAFT):
            emails.send_admin_email(order, "submitted")
            sent += 1
        self.message_user(request, f"Re-sent {sent} notification(s).", messages.SUCCESS)
