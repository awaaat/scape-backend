from django.urls import path

from .views import OTPRequestView, OTPVerifyView, PinSubmitView, ReportListView, ReportStatusView, UsageView

urlpatterns = [
    path("property/pins/", PinSubmitView.as_view(), name="property-pin-submit"),
    path("property/otp/request/", OTPRequestView.as_view(), name="property-otp-request"),
    path("property/otp/verify/", OTPVerifyView.as_view(), name="property-otp-verify"),
    path("property/reports/", ReportListView.as_view(), name="property-report-list"),
    path("property/usage/", UsageView.as_view(), name="property-usage"),
    path("property/reports/<uuid:report_id>/", ReportStatusView.as_view(), name="property-report-status"),
]
