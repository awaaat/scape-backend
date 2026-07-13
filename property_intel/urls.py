from django.urls import path

from .views import OTPRequestView, OTPVerifyView, PinSubmitView, ReportCancelView, ReportListView, ReportRetryView, ReportStatusView, UsageView

urlpatterns = [
    path("property/pins/", PinSubmitView.as_view(), name="property-pin-submit"),
    path("property/otp/request/", OTPRequestView.as_view(), name="property-otp-request"),
    path("property/otp/verify/", OTPVerifyView.as_view(), name="property-otp-verify"),
    path("property/reports/", ReportListView.as_view(), name="property-report-list"),
    path("property/usage/", UsageView.as_view(), name="property-usage"),
    path("property/reports/<uuid:report_id>/", ReportStatusView.as_view(), name="property-report-status"),
    path("property/reports/<uuid:report_id>/retry/", ReportRetryView.as_view(), name="property-report-retry"),
    path("property/reports/<uuid:report_id>/cancel/", ReportCancelView.as_view(), name="property-report-cancel"),
]
