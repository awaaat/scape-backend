from django.contrib import admin
from django.utils.html import format_html

from .models import (
    EducationEntry,
    EmploymentEntry,
    JobApplication,
    JobApplicationDemographics,
    JobPosting,
)


class JobApplicationInline(admin.TabularInline):
    """Shows applicants right on the job posting page — no need to jump around."""

    model = JobApplication
    extra = 0
    fields = ["full_name", "email", "status", "created_at"]
    readonly_fields = ["full_name", "email", "created_at"]
    can_delete = False
    show_change_link = True

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(JobPosting)
class JobPostingAdmin(admin.ModelAdmin):
    list_display = [
        "title",
        "department",
        "job_type",
        "location_type",
        "location",
        "is_active",
        "is_featured",
        "applications_count",
        "posted_at",
        "closing_date",
    ]
    list_filter = ["department", "job_type", "location_type", "experience_level", "is_active", "is_featured"]
    search_fields = ["title", "location", "description"]
    list_editable = ["is_active", "is_featured"]
    prepopulated_fields = {"slug": ("title",)}
    readonly_fields = ["posted_at", "updated_at"]
    date_hierarchy = "posted_at"
    inlines = [JobApplicationInline]

    fieldsets = (
        (
            "Basics",
            {
                "fields": (
                    "title",
                    "slug",
                    "department",
                    "job_type",
                    "location_type",
                    "location",
                    "experience_level",
                )
            },
        ),
        (
            "Content shown on the site",
            {
                "fields": ("summary", "description", "responsibilities", "requirements", "nice_to_have"),
                "description": (
                    "For 'Responsibilities', 'Requirements' and 'Nice to have' — put one item per line. "
                    "Each line becomes a bullet point on the public job page."
                ),
            },
        ),
        ("Compensation & availability", {"fields": ("salary_range", "positions_available")}),
        (
            "Visibility",
            {
                "fields": ("is_active", "is_featured", "closing_date"),
                "description": "Untick 'Is active' to pull a role down instantly without deleting its data or applicants.",
            },
        ),
        ("Timestamps", {"fields": ("posted_at", "updated_at")}),
    )

    def applications_count(self, obj):
        return obj.applications.count()

    applications_count.short_description = "Applicants"


class EducationEntryInline(admin.TabularInline):
    model = EducationEntry
    extra = 0
    fields = ["school", "degree", "field_of_study", "graduation_year", "gpa"]


class EmploymentEntryInline(admin.TabularInline):
    model = EmploymentEntry
    extra = 0
    fields = ["company", "job_title", "start_date", "end_date", "is_current"]


class JobApplicationDemographicsInline(admin.StackedInline):
    """
    Optional EEO data. Kept as its own inline (not shown by default fieldsets)
    so it's easy to remove entirely from an admin role's view via
    `get_inline_instances` if you need to restrict who sees it.
    """

    model = JobApplicationDemographics
    extra = 0
    can_delete = False
    fields = ["gender", "self_described_gender", "veteran_status", "disability_status", "ethnicity", "collected_at"]
    readonly_fields = ["collected_at"]


@admin.register(JobApplication)
class JobApplicationAdmin(admin.ModelAdmin):
    list_display = [
        "full_name",
        "job",
        "email",
        "country",
        "status",
        "years_of_experience",
        "work_authorization",
        "created_at",
    ]
    list_filter = ["status", "job", "country", "work_authorization", "remote_preference", "created_at"]
    search_fields = ["full_name", "email", "phone", "current_company", "job__title", "city"]
    list_editable = ["status"]
    date_hierarchy = "created_at"
    inlines = [EmploymentEntryInline, EducationEntryInline, JobApplicationDemographicsInline]

    readonly_fields = [
        "job",
        "visitor",
        "full_name",
        "email",
        "phone",
        "date_of_birth",
        "city",
        "country",
        "postal_code",
        "ip_address",
        "user_agent",
        "linkedin_url",
        "github_url",
        "stackoverflow_url",
        "portfolio_url",
        "current_company",
        "years_of_experience",
        "work_authorization",
        "visa_sponsorship_required",
        "expected_salary",
        "notice_period",
        "earliest_start_date",
        "remote_preference",
        "open_to_relocation",
        "resume_filename",
        "resume_upload_failed",
        "resume_link",
        "cover_letter",
        "how_heard",
        "consent_given",
        "consent_given_at",
        "privacy_policy_version",
        "confirmation_email_sent",
        "admin_notified",
        "created_at",
        "updated_at",
    ]

    fieldsets = (
        (
            "Applicant",
            {
                "fields": (
                    "full_name",
                    "email",
                    "phone",
                    "date_of_birth",
                    "city",
                    "country",
                    "postal_code",
                )
            },
        ),
        (
            "Links",
            {"fields": ("linkedin_url", "github_url", "stackoverflow_url", "portfolio_url")},
        ),
        (
            "Work authorization & preferences",
            {
                "fields": (
                    "work_authorization",
                    "visa_sponsorship_required",
                    "remote_preference",
                    "open_to_relocation",
                )
            },
        ),
        (
            "Compensation & availability",
            {"fields": ("expected_salary", "notice_period", "earliest_start_date")},
        ),
        (
            "Application",
            {
                "fields": (
                    "job",
                    "current_company",
                    "years_of_experience",
                    "cover_letter",
                    "how_heard",
                )
            },
        ),
        (
            "Resume",
            {
                "fields": ("resume_filename", "resume_upload_failed", "resume_link"),
                "description": "Click 'View resume' to open a temporary signed link (valid 1 hour).",
            },
        ),
        (
            "Recruiting status",
            {
                "fields": ("status", "internal_notes"),
                "description": "Change 'Status' as this candidate moves through your pipeline. 'Internal notes' are never shown to the applicant.",
            },
        ),
        (
            "Consent & compliance",
            {"fields": ("consent_given", "consent_given_at", "privacy_policy_version")},
        ),
        (
            "System",
            {
                "fields": (
                    "visitor",
                    "ip_address",
                    "user_agent",
                    "confirmation_email_sent",
                    "admin_notified",
                    "created_at",
                    "updated_at",
                )
            },
        ),
    )

    def has_add_permission(self, request):
        # Applications only come in through the public form, not created by hand in admin.
        return False

    def resume_link(self, obj):
        return "Sent as a Gmail attachment at submission time — check the resume inbox."

    resume_link.short_description = "Resume file"