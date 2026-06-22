from django.contrib import admin

from .models import PageView, Visitor


class PageViewInline(admin.TabularInline):
    model = PageView
    extra = 0
    readonly_fields = ["url", "referrer", "title", "created_at"]
    can_delete = False
    max_num = 0


@admin.register(Visitor)
class VisitorAdmin(admin.ModelAdmin):
    list_display = [
        "session_id_short",
        "ip_address",
        "browser",
        "operating_system",
        "device_type",
        "request_count",
        "first_seen",
        "last_seen",
        "is_lead",
    ]
    list_filter = ["is_lead", "browser", "operating_system", "device_type"]
    search_fields = ["session_id", "ip_address", "user_agent"]
    readonly_fields = ["session_id", "first_seen", "last_seen", "request_count"]
    inlines = [PageViewInline]

    def session_id_short(self, obj):
        return obj.session_id[:12] + "…"

    session_id_short.short_description = "Session"


@admin.register(PageView)
class PageViewAdmin(admin.ModelAdmin):
    list_display = ["visitor", "url", "referrer", "created_at"]
    list_filter = ["created_at"]
    search_fields = ["url", "referrer", "visitor__session_id"]
    readonly_fields = ["created_at"]
