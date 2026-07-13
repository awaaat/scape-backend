from django.contrib import admin

from .models import UserSignup


@admin.register(UserSignup)
class UserSignupAdmin(admin.ModelAdmin):
    list_display = ["full_name", "email", "phone", "email_verified", "consent_given", "is_active", "created_at"]
    list_filter = ["email_verified", "consent_given", "is_active", "created_at"]
    search_fields = ["full_name", "email", "phone"]
    readonly_fields = [
        "id",
        "email_verification_token_hash",
        "email_verification_expires_at",
        "email_verified_at",
        "created_at",
        "updated_at",
    ]
    fieldsets = (
        (None, {"fields": ("id", "full_name", "email", "phone", "is_active")}),
        ("Origin", {"fields": ("visitor", "ip_address", "user_agent")}),
        ("Consent", {"fields": ("consent_given", "consent_given_at", "privacy_policy_version")}),
        (
            "Email verification",
            {"fields": ("email_verified", "email_verified_at", "email_verification_token_hash", "email_verification_expires_at")},
        ),
        ("Timestamps", {"fields": ("created_at", "updated_at")}),
    )
