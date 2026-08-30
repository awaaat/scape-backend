from django.urls import path

from .views import (
    BusinessQueryCreateView,
    BusinessReportCheckoutView,
    BusinessReportDetailView,
    BusinessReportListView,
)

app_name = "business_intel"

urlpatterns = [
    path("business-intel/queries/", BusinessQueryCreateView.as_view(), name="query-create"),
    path("business-intel/reports/", BusinessReportListView.as_view(), name="report-list"),
    path("business-intel/reports/<uuid:pk>/", BusinessReportDetailView.as_view(), name="report-detail"),
    path("business-intel/reports/<uuid:pk>/checkout/", BusinessReportCheckoutView.as_view(), name="report-checkout"),
]
