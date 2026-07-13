from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.text import slugify

from visitors.models import Visitor

from .constants import (
    COUNTRY_CHOICES,
    DISABILITY_STATUS_CHOICES,
    GENDER_CHOICES,
    NOTICE_PERIOD_CHOICES,
    REMOTE_PREFERENCE_CHOICES,
    VETERAN_STATUS_CHOICES,
    WORK_AUTH_CHOICES,
)


class JobPosting(models.Model):
    """A single role you're hiring for. Managed entirely from /admin/."""

    DEPARTMENT_CHOICES = [
        ("engineering", "Engineering"),
        ("data", "Data & Analytics"),
        ("ai_ml", "AI & Machine Learning"),
        ("sales", "Sales"),
        ("marketing", "Marketing"),
        ("customer_success", "Customer Success"),
        ("operations", "Operations"),
        ("finance", "Finance"),
        ("hr", "People & HR"),
        ("other", "Other"),
    ]

    JOB_TYPE_CHOICES = [
        ("full_time", "Full-time"),
        ("part_time", "Part-time"),
        ("contract", "Contract"),
        ("internship", "Internship"),
        ("freelance", "Freelance"),
    ]

    LOCATION_TYPE_CHOICES = [
        ("remote", "Remote"),
        ("onsite", "On-site"),
        ("hybrid", "Hybrid"),
    ]

    EXPERIENCE_LEVEL_CHOICES = [
        ("entry", "Entry Level"),
        ("mid", "Mid Level"),
        ("senior", "Senior"),
        ("lead", "Lead / Principal"),
        ("executive", "Executive"),
    ]

    title = models.CharField(max_length=200)
    slug = models.SlugField(max_length=220, unique=True, blank=True)
    department = models.CharField(max_length=30, choices=DEPARTMENT_CHOICES, default="other")
    job_type = models.CharField(max_length=20, choices=JOB_TYPE_CHOICES, default="full_time")
    location_type = models.CharField(max_length=10, choices=LOCATION_TYPE_CHOICES, default="remote")
    location = models.CharField(
        max_length=150, blank=True, help_text="e.g. 'Nairobi, Kenya' or 'Worldwide'"
    )
    experience_level = models.CharField(
        max_length=15, choices=EXPERIENCE_LEVEL_CHOICES, default="mid"
    )

    summary = models.CharField(
        max_length=300, blank=True, help_text="One-line summary shown on the jobs list page"
    )
    description = models.TextField(help_text="Full role description")
    responsibilities = models.TextField(
        blank=True, help_text="One item per line — rendered as a bullet list on the site"
    )
    requirements = models.TextField(
        blank=True, help_text="One item per line — rendered as a bullet list on the site"
    )
    nice_to_have = models.TextField(
        blank=True, help_text="One item per line — rendered as a bullet list on the site"
    )

    salary_range = models.CharField(
        max_length=100,
        blank=True,
        help_text="e.g. '$60,000 - $80,000 / year'. Leave blank to hide salary entirely.",
    )
    positions_available = models.PositiveSmallIntegerField(default=1)

    is_active = models.BooleanField(
        default=True, db_index=True, help_text="Untick to hide this posting without deleting it"
    )
    is_featured = models.BooleanField(
        default=False, help_text="Featured jobs are pinned to the top of the public list"
    )

    posted_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    closing_date = models.DateField(
        null=True, blank=True, help_text="Optional — applications stop being accepted after this date"
    )

    class Meta:
        ordering = ["-is_featured", "-posted_at"]

    def __str__(self):
        return f"{self.title} ({self.get_department_display()})"

    def save(self, *args, **kwargs):
        if not self.slug:
            base_slug = slugify(self.title)[:200] or "role"
            slug = base_slug
            counter = 1
            while JobPosting.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                counter += 1
                slug = f"{base_slug}-{counter}"
            self.slug = slug
        super().save(*args, **kwargs)

    @property
    def is_open(self):
        """True only if active AND (no closing date OR closing date hasn't passed)."""
        if not self.is_active:
            return False
        if self.closing_date and self.closing_date < timezone.now().date():
            return False
        return True

    def _split_lines(self, text):
        return [line.strip() for line in text.splitlines() if line.strip()]

    @property
    def lines_responsibilities(self):
        return self._split_lines(self.responsibilities)

    @property
    def lines_requirements(self):
        return self._split_lines(self.requirements)

    @property
    def lines_nice_to_have(self):
        return self._split_lines(self.nice_to_have)


# ── Deprecated — DO NOT DELETE ────────────────────────────────────────────
# migrations/0001_initial.py references these two by dotted path (Django
# serializes FileField's `upload_to=` and `validators=` as direct import
# paths, not as strings). Deleting them breaks `migrate` on any fresh
# database forever, since Django replays every migration in order. Dead
# code — just leave them here untouched.
def resume_upload_path(instance, filename):  # pragma: no cover - legacy, unused
    return filename


def validate_resume_file(value):  # pragma: no cover - legacy, unused
    return None


RESUME_MAX_SIZE_MB = 5
# .doc (legacy binary Word format) intentionally excluded — no reliable way
# to validate/handle it, and it's rare among applicants.
RESUME_ALLOWED_EXTENSIONS = ["pdf", "docx"]


def validate_resume_upload(value):
    """Validates an in-memory uploaded resume before it's handed to storage.upload_resume()."""
    if value.size > RESUME_MAX_SIZE_MB * 1024 * 1024:
        raise ValidationError(f"Resume file is too large. Max size is {RESUME_MAX_SIZE_MB}MB.")
    ext = value.name.rsplit(".", 1)[-1].lower() if "." in value.name else ""
    if ext not in RESUME_ALLOWED_EXTENSIONS:
        raise ValidationError("Resume must be a PDF or DOCX file.")


class JobApplication(models.Model):
    """One application submitted by a candidate for a specific JobPosting."""

    STATUS_CHOICES = [
        ("new", "New"),
        ("reviewing", "Reviewing"),
        ("shortlisted", "Shortlisted"),
        ("interviewing", "Interviewing"),
        ("offer_extended", "Offer Extended"),
        ("hired", "Hired"),
        ("rejected", "Rejected"),
        ("withdrawn", "Withdrawn"),
    ]

    job = models.ForeignKey(JobPosting, on_delete=models.CASCADE, related_name="applications")
    visitor = models.ForeignKey(
        Visitor, on_delete=models.SET_NULL, null=True, blank=True, related_name="job_applications"
    )

    # ── Personal information ─────────────────────────────────────────
    full_name = models.CharField(max_length=150)
    email = models.EmailField()
    phone = models.CharField(max_length=30, blank=True)
    date_of_birth = models.DateField(
        null=True,
        blank=True,
        help_text="Collected on request. Keep access to this field restricted — "
        "most jurisdictions advise against hiring managers seeing DOB pre-decision.",
    )
    city = models.CharField(max_length=100, blank=True)
    country = models.CharField(max_length=2, choices=COUNTRY_CHOICES, blank=True)
    postal_code = models.CharField(max_length=20, blank=True)

    # ── Server-recorded metadata (never user-entered) ────────────────
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)

    # ── Professional links ───────────────────────────────────────────
    linkedin_url = models.URLField(blank=True)
    github_url = models.URLField(blank=True)
    stackoverflow_url = models.URLField(blank=True)
    portfolio_url = models.URLField(blank=True, help_text="Personal site, Behance, Dribbble, Kaggle, etc.")

    current_company = models.CharField(max_length=150, blank=True)
    years_of_experience = models.PositiveSmallIntegerField(null=True, blank=True)

    # ── Work authorization ───────────────────────────────────────────
    work_authorization = models.CharField(max_length=20, choices=WORK_AUTH_CHOICES, blank=True)
    visa_sponsorship_required = models.BooleanField(default=False)

    # ── Compensation & availability ──────────────────────────────────
    expected_salary = models.CharField(max_length=100, blank=True)
    notice_period = models.CharField(max_length=20, choices=NOTICE_PERIOD_CHOICES, blank=True)
    earliest_start_date = models.DateField(null=True, blank=True)
    remote_preference = models.CharField(max_length=20, choices=REMOTE_PREFERENCE_CHOICES, blank=True)
    open_to_relocation = models.BooleanField(default=False)

    # ── Resume: original file, stored (compressed if PDF) in Supabase ───
    resume_storage_path = models.CharField(
        max_length=500,
        blank=True,
        help_text="Deprecated — no longer used. Resumes are emailed directly, never stored. "
        "to generate a temporary link — never store a public URL here.",
    )
    resume_filename = models.CharField(max_length=255, blank=True, help_text="Original filename, for display.")
    resume_upload_failed = models.BooleanField(
        default=False, help_text="True if storage upload failed at submission time — application still saved."
    )

    cover_letter = models.TextField(blank=True)
    how_heard = models.CharField(max_length=150, blank=True, help_text="How did they hear about this role?")

    # ── Consent ───────────────────────────────────────────────────────
    consent_given = models.BooleanField(default=False)
    consent_given_at = models.DateTimeField(null=True, blank=True)
    privacy_policy_version = models.CharField(max_length=20, blank=True)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="new", db_index=True)
    internal_notes = models.TextField(blank=True, help_text="Not visible to the applicant")

    confirmation_email_sent = models.BooleanField(default=False)
    admin_notified = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["job", "email"], name="unique_application_per_job_email")
        ]

    def __str__(self):
        return f"{self.full_name} → {self.job.title}"



class EducationEntry(models.Model):
    """One education entry — manually entered by the applicant or recruiter."""

    application = models.ForeignKey(JobApplication, on_delete=models.CASCADE, related_name="education_history")
    school = models.CharField(max_length=200, blank=True)
    degree = models.CharField(max_length=200, blank=True)
    field_of_study = models.CharField(max_length=200, blank=True)
    graduation_year = models.PositiveSmallIntegerField(null=True, blank=True)
    gpa = models.CharField(max_length=20, blank=True)
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["order", "-graduation_year"]
        verbose_name_plural = "education entries"

    def __str__(self):
        return f"{self.degree or 'Education'} — {self.school or 'unknown school'}"


class EmploymentEntry(models.Model):
    """One employment history entry — manually entered by the applicant or recruiter."""

    application = models.ForeignKey(JobApplication, on_delete=models.CASCADE, related_name="employment_history")
    company = models.CharField(max_length=200, blank=True)
    job_title = models.CharField(max_length=200, blank=True)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    is_current = models.BooleanField(default=False)
    responsibilities = models.TextField(blank=True)
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["order", "-start_date"]
        verbose_name_plural = "employment entries"

    def __str__(self):
        return f"{self.job_title or 'Role'} @ {self.company or 'unknown company'}"


class JobApplicationDemographics(models.Model):
    """
    Optional, self-reported EEO data — kept on a separate table (not joined
    onto JobApplication by default) so it's easy to restrict access and
    exclude from normal recruiter views/exports. Never required, never used
    in the applicant list/search.
    """

    application = models.OneToOneField(
        JobApplication, on_delete=models.CASCADE, related_name="demographics"
    )
    gender = models.CharField(max_length=20, choices=GENDER_CHOICES, blank=True)
    self_described_gender = models.CharField(max_length=100, blank=True)
    veteran_status = models.CharField(max_length=20, choices=VETERAN_STATUS_CHOICES, blank=True)
    disability_status = models.CharField(max_length=20, choices=DISABILITY_STATUS_CHOICES, blank=True)
    ethnicity = models.CharField(max_length=100, blank=True)
    collected_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "demographics (EEO, optional)"
        verbose_name_plural = "demographics (EEO, optional)"

    def __str__(self):
        return f"Demographics for {self.application.full_name}"