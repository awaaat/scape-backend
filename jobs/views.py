import logging

from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .email import send_admin_new_application, send_applicant_confirmation, send_resume_to_gmail
from .models import JobPosting
from .serializers import JobApplicationSerializer, JobPostingDetailSerializer, JobPostingListSerializer

logger = logging.getLogger("jobs")


class JobPostingListView(APIView):
    """
    GET /api/jobs/
    Public list of open roles. Optional filters via query params:
      ?department=engineering
      ?job_type=full_time
      ?location_type=remote
    """

    def get(self, request):
        qs = JobPosting.objects.filter(is_active=True)

        department = request.query_params.get("department")
        if department:
            qs = qs.filter(department=department)

        job_type = request.query_params.get("job_type")
        if job_type:
            qs = qs.filter(job_type=job_type)

        location_type = request.query_params.get("location_type")
        if location_type:
            qs = qs.filter(location_type=location_type)

        today = timezone.now().date()
        qs = qs.filter(Q(closing_date__isnull=True) | Q(closing_date__gte=today))

        serializer = JobPostingListSerializer(qs, many=True)
        return Response(serializer.data)


class JobPostingDetailView(APIView):
    """GET /api/jobs/<slug>/ — full role details for a single open posting."""

    def get(self, request, slug):
        job = get_object_or_404(JobPosting, slug=slug, is_active=True)
        serializer = JobPostingDetailSerializer(job)
        return Response(serializer.data)


class JobApplicationCreateView(APIView):
    """
    POST /api/jobs/<slug>/apply/
    multipart/form-data — must include a `resume` file field (PDF/DOC/DOCX, max 5MB).
    """

    throttle_scope = "contact"

    def post(self, request, slug):
        job = get_object_or_404(JobPosting, slug=slug, is_active=True)

        serializer = JobApplicationSerializer(data=request.data, context={"job": job, "request": request})
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        application = serializer.save()

        visitor = getattr(request, "visitor", None)
        if visitor is not None:
            application.visitor = visitor
            application.save(update_fields=["visitor"])

        try:
            send_applicant_confirmation(application)
            application.confirmation_email_sent = True
        except Exception as exc:
            logger.error("Applicant confirmation failed for application #%s: %s", application.id, exc)

        try:
            send_admin_new_application(application)
            send_resume_to_gmail(
                application,
                resume_bytes=getattr(application, "_resume_bytes", None),
                resume_filename=application.resume_filename,
                resume_content_type=getattr(application, "_resume_content_type", None),
            )
            application.admin_notified = True
        except Exception as exc:
            logger.error("Admin notification failed for application #%s: %s", application.id, exc)

        application.save(update_fields=["confirmation_email_sent", "admin_notified"])

        return Response(
            {"message": "Application received — thank you! We'll be in touch if there's a match."},
            status=status.HTTP_201_CREATED,
        )
