from django.conf import settings
from django.utils import timezone
from rest_framework import serializers

from .models import (
    EducationEntry,
    EmploymentEntry,
    JobApplication,
    JobApplicationDemographics,
    JobPosting,
    validate_resume_upload,
)


class JobPostingListSerializer(serializers.ModelSerializer):
    department_display = serializers.CharField(source="get_department_display", read_only=True)
    job_type_display = serializers.CharField(source="get_job_type_display", read_only=True)
    location_type_display = serializers.CharField(source="get_location_type_display", read_only=True)
    experience_level_display = serializers.CharField(source="get_experience_level_display", read_only=True)

    class Meta:
        model = JobPosting
        fields = [
            "slug",
            "title",
            "department",
            "department_display",
            "job_type",
            "job_type_display",
            "location_type",
            "location_type_display",
            "location",
            "experience_level",
            "experience_level_display",
            "summary",
            "salary_range",
            "is_featured",
            "posted_at",
            "closing_date",
        ]


class JobPostingDetailSerializer(JobPostingListSerializer):
    responsibilities = serializers.ListField(source="lines_responsibilities", read_only=True)
    requirements = serializers.ListField(source="lines_requirements", read_only=True)
    nice_to_have = serializers.ListField(source="lines_nice_to_have", read_only=True)

    class Meta(JobPostingListSerializer.Meta):
        fields = JobPostingListSerializer.Meta.fields + [
            "description",
            "responsibilities",
            "requirements",
            "nice_to_have",
            "positions_available",
        ]


class JobApplicationSerializer(serializers.ModelSerializer):
    # Write-only: read into memory and emailed directly — never stored on disk or in any bucket.
    resume = serializers.FileField(write_only=True, validators=[validate_resume_upload])

    # Optional EEO block — entirely separate from the main model, entirely optional.
    gender = serializers.CharField(required=False, allow_blank=True, write_only=True)
    self_described_gender = serializers.CharField(required=False, allow_blank=True, write_only=True)
    veteran_status = serializers.CharField(required=False, allow_blank=True, write_only=True)
    disability_status = serializers.CharField(required=False, allow_blank=True, write_only=True)
    ethnicity = serializers.CharField(required=False, allow_blank=True, write_only=True)

    class Meta:
        model = JobApplication
        fields = [
            # Personal
            "full_name",
            "email",
            "phone",
            "date_of_birth",
            "city",
            "country",
            "postal_code",
            # Links
            "linkedin_url",
            "github_url",
            "stackoverflow_url",
            "portfolio_url",
            # Professional
            "current_company",
            "years_of_experience",
            # Work authorization
            "work_authorization",
            "visa_sponsorship_required",
            # Compensation & availability
            "expected_salary",
            "notice_period",
            "earliest_start_date",
            "remote_preference",
            "open_to_relocation",
            # Resume (write-only, see above) + freeform
            "resume",
            "cover_letter",
            "how_heard",
            # Consent
            "consent_given",
            # Optional EEO (write-only, see above)
            "gender",
            "self_described_gender",
            "veteran_status",
            "disability_status",
            "ethnicity",
        ]

    def validate_full_name(self, value):
        if len(value.strip()) < 2:
            raise serializers.ValidationError("Name looks too short.")
        return value

    def validate_date_of_birth(self, value):
        if value is None:
            return value
        age_days = (timezone.now().date() - value).days
        if age_days < 16 * 365:
            raise serializers.ValidationError("Applicant must be at least 16 years old.")
        if age_days > 100 * 365:
            raise serializers.ValidationError("Please double-check this date of birth.")
        return value

    def validate_consent_given(self, value):
        if not value:
            raise serializers.ValidationError(
                "You must accept the privacy policy / consent to data storage to apply."
            )
        return value

    def validate(self, attrs):
        job = self.context.get("job")
        if job is None or not job.is_open:
            raise serializers.ValidationError("This position is no longer accepting applications.")

        email = attrs.get("email")
        if email and JobApplication.objects.filter(job=job, email__iexact=email).exists():
            raise serializers.ValidationError(
                {"email": "You've already applied to this position with this email address."}
            )
        return attrs

    def create(self, validated_data):
        resume_file = validated_data.pop("resume")

        demographics_data = {
            "gender": validated_data.pop("gender", ""),
            "self_described_gender": validated_data.pop("self_described_gender", ""),
            "veteran_status": validated_data.pop("veteran_status", ""),
            "disability_status": validated_data.pop("disability_status", ""),
            "ethnicity": validated_data.pop("ethnicity", ""),
        }
        has_demographics = any(demographics_data.values())

        validated_data["job"] = self.context["job"]
        validated_data["resume_filename"] = resume_file.name
        validated_data["consent_given_at"] = timezone.now()
        validated_data["privacy_policy_version"] = getattr(settings, "PRIVACY_POLICY_VERSION", "1.0")

        request = self.context.get("request")
        if request is not None:
            validated_data["ip_address"] = _client_ip(request)
            validated_data["user_agent"] = request.META.get("HTTP_USER_AGENT", "")[:1000]

        application = JobApplication.objects.create(**validated_data)

        # Resume is never written to disk or any storage service — kept only
        # in memory long enough to attach it to the Gmail send in views.py,
        # then discarded when the request finishes.
        resume_file.seek(0)
        application._resume_bytes = resume_file.read()
        application._resume_content_type = getattr(resume_file, "content_type", None) or "application/octet-stream"

        if has_demographics:
            JobApplicationDemographics.objects.create(application=application, **demographics_data)

        return application


def _client_ip(request):
    """Respect a trusted reverse proxy's X-Forwarded-For; fall back to REMOTE_ADDR."""
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")