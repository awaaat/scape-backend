"""
business_intel/views.py

Every view here requires login (IsAuthenticated). Free-tier gating
lives on Requester (see models.py, services.py), keyed to request.user.

Payment is NOT wired in yet. BusinessReportCheckoutView is a stub that
returns 501 -- it exists so the URL contract is stable once Paystack is
wired in (mirroring property_intel.views.ReportCheckoutView), but it
deliberately does not charge anyone yet.
"""
from django.conf import settings
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import services
from .models import BusinessReport
from .serializers import (
    BusinessQueryCreateSerializer,
    BusinessReportListSerializer,
    BusinessReportSerializer,
)
from .services import LocationParseError
from .tasks import generate_business_report


def _owns_report(report, user):
    if user.is_staff:
        return True
    return report.query.requester.user_id == user.pk


class BusinessQueryCreateView(APIView):
    """POST /api/business-intel/queries/ -- requires login. Free-tier
    gated per Requester.free_reports_remaining (default 5). Once
    exhausted, the report is created with status="awaiting_payment" and
    a 402 is returned. No Paystack call happens here."""

    permission_classes = [IsAuthenticated]
    throttle_scope = "business_intel_submit"

    def post(self, request):
        serializer = BusinessQueryCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        email = request.user.email
        if not email:
            return Response(
                {"detail": "Your account has no email on file -- add one before submitting a query."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            requester = services.get_or_create_requester(email, user=request.user)
            query, _cell = services.create_query(
                raw_location_input=data["raw_location_input"],
                business_category=data["business_category"],
                requester=requester,
                business_category_other=data.get("business_category_other", ""),
                investment_amount_kes=data.get("investment_amount_kes"),
                branches_planned=data.get("branches_planned", 1),
                target_segment=data.get("target_segment", ""),
            )
        except LocationParseError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        bypassed = email.strip().lower() in settings.ADMIN_BYPASS_EMAILS
        if bypassed or requester.consume_free_report():
            report = BusinessReport.objects.create(query=query)
            generate_business_report.delay(str(report.id))
            return Response({"report_id": str(report.id), "status": report.status}, status=status.HTTP_202_ACCEPTED)

        report = BusinessReport.objects.create(query=query, status="awaiting_payment")
        return Response(
            {
                "report_id": str(report.id),
                "status": report.status,
                "message": "You've used all your free reports. Payment isn't available yet -- contact us to continue.",
            },
            status=status.HTTP_402_PAYMENT_REQUIRED,
        )


class BusinessReportDetailView(APIView):
    """GET /api/business-intel/reports/<uuid:pk>/ -- requires login AND
    ownership (or staff)."""

    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        try:
            report = BusinessReport.objects.select_related("query__requester").get(pk=pk)
        except BusinessReport.DoesNotExist:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        if not _owns_report(report, request.user):
            return Response({"detail": "This report does not belong to you."}, status=status.HTTP_403_FORBIDDEN)

        return Response(BusinessReportSerializer(report).data)


class BusinessReportListView(APIView):
    """GET /api/business-intel/reports/ -- dashboard's report list for
    the logged-in user. Mirrors property_intel.views.ReportListView."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        reports = (
            BusinessReport.objects
            .filter(query__requester__user=request.user)
            .select_related("query")
            .order_by("-created_at")
        )
        return Response(BusinessReportListSerializer(reports, many=True).data)


class BusinessReportCheckoutView(APIView):
    """POST /api/business-intel/reports/<uuid:pk>/checkout/ -- STUB.
    Payment isn't implemented for business_intel yet. Wire this up the
    same way property_intel.views.ReportCheckoutView calls
    payments.services.initialize_transaction() once ready."""

    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        try:
            report = BusinessReport.objects.select_related("query__requester").get(pk=pk)
        except BusinessReport.DoesNotExist:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        if not _owns_report(report, request.user):
            return Response({"detail": "This report does not belong to you."}, status=status.HTTP_403_FORBIDDEN)

        return Response(
            {"detail": "Payment for business-intel reports isn't available yet. Contact us in the meantime."},
            status=status.HTTP_501_NOT_IMPLEMENTED,
        )
