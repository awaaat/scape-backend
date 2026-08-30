from django.contrib import admin

from .models import (
    BusinessLocationCell,
    Requester,
    BusinessQuery,
    BusinessReport,
    BusinessAPICallLog,
)


@admin.register(BusinessLocationCell)
class BusinessLocationCellAdmin(admin.ModelAdmin):
    list_display = ("geohash", "formatted_address", "has_isochrones", "times_reused", "last_refreshed_at")
    search_fields = ("geohash", "formatted_address")
    readonly_fields = ("first_queried_at", "last_refreshed_at", "times_reused")


@admin.register(Requester)
class RequesterAdmin(admin.ModelAdmin):
    list_display = ("email", "user", "created_at", "last_active_at")
    search_fields = ("email",)


@admin.register(BusinessQuery)
class BusinessQueryAdmin(admin.ModelAdmin):
    list_display = ("id", "business_category", "latitude", "longitude", "requester", "created_at")
    list_filter = ("business_category", "target_segment")
    search_fields = ("id", "requester__email")


@admin.register(BusinessReport)
class BusinessReportAdmin(admin.ModelAdmin):
    list_display = ("id", "query", "status", "recommendation", "opportunity_level", "is_paid", "created_at")
    list_filter = ("status", "recommendation", "opportunity_level", "is_paid")
    readonly_fields = ("created_at", "updated_at")


@admin.register(BusinessAPICallLog)
class BusinessAPICallLogAdmin(admin.ModelAdmin):
    list_display = ("api", "category", "succeeded", "estimated_cost_usd", "created_at")
    list_filter = ("api", "succeeded")
