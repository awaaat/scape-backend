from django.contrib import admin
from django.utils.html import format_html

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
        "company_name",
        "city",
        "country_code",
        "browser",
        "operating_system",
        "device_type",
        "request_count",
        "first_seen",
        "last_seen",
        "is_lead",
        "is_enriched",
    ]
    list_filter = [
        "is_lead",
        "is_enriched",
        "country_code",
        "company_industry",
        "browser",
        "operating_system",
        "device_type",
    ]
    search_fields = [
        "session_id",
        "ip_address",
        "user_agent",
        "company_name",
        "company_domain",
        "city",
        "country",
    ]
    readonly_fields = [
        "session_id",
        "first_seen",
        "last_seen",
        "request_count",
        "is_enriched",
        "ip_address",
        "company_name",
        "company_domain",
        "company_industry",
        "company_size",
        "isp",
        "country",
        "country_code",
        "region",
        "city",
    ]
    inlines = [PageViewInline]

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("page_views")

    def session_id_short(self, obj):
        return obj.session_id[:12] + "…"
    session_id_short.short_description = "Session"


@admin.register(PageView)
class PageViewAdmin(admin.ModelAdmin):
    list_display = ["visitor", "url", "referrer", "created_at"]
    list_filter = ["created_at"]
    search_fields = ["url", "referrer", "visitor__session_id", "visitor__company_name"]
    readonly_fields = ["created_at"]


# ── Cold Prospects proxy model + admin ──────────────────────────────────────

class ColdProspectProxy(Visitor):
    """Proxy so we can register a second admin view filtered to cold prospects."""
    class Meta:
        proxy = True
        verbose_name = "Cold Prospect"
        verbose_name_plural = "Cold Prospects"


@admin.register(ColdProspectProxy)
class ColdProspectAdmin(admin.ModelAdmin):
    """
    Shows enriched visitors who never converted — your warm company hit list.
    Filter: is_enriched=True, is_lead=False, company_name not blank.
    """
    list_display = [
        "company_name",
        "company_domain",
        "company_industry",
        "company_size",
        "city",
        "country_code",
        "request_count",
        "pages_visited",
        "first_seen",
        "last_seen",
        "outreach_link",
    ]
    list_filter = ["country_code", "company_industry", "company_size"]
    search_fields = ["company_name", "company_domain", "city", "country"]
    readonly_fields = [
        "session_id", "ip_address", "company_name", "company_domain",
        "company_industry", "company_size", "isp", "country", "country_code",
        "region", "city", "first_seen", "last_seen", "request_count",
    ]
    inlines = [PageViewInline]

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .filter(is_enriched=True, is_lead=False)
            .exclude(company_name="")
            .prefetch_related("page_views")
        )

    def pages_visited(self, obj):
        return obj.page_views.count()
    pages_visited.short_description = "Pages"

    def outreach_link(self, obj):
        if obj.company_domain:
            return format_html(
                '<a href="https://www.linkedin.com/search/results/companies/?keywords={}" target="_blank">LinkedIn →</a>',
                obj.company_name,
            )
        return "—"
    outreach_link.short_description = "Outreach"