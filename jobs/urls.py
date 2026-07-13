from django.urls import path

from .views import JobApplicationCreateView, JobPostingDetailView, JobPostingListView

urlpatterns = [
    path("jobs/", JobPostingListView.as_view(), name="job-list"),
    path("jobs/<slug:slug>/", JobPostingDetailView.as_view(), name="job-detail"),
    path("jobs/<slug:slug>/apply/", JobApplicationCreateView.as_view(), name="job-apply"),
]
