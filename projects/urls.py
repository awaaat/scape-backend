from django.urls import path

from . import views

urlpatterns = [
    # ── Client ─────────────────────────────────────────────────────
    path("projects/", views.ProjectListCreateView.as_view(), name="projects-list-create"),
    path("projects/unread-count/", views.ProjectUnreadSummaryView.as_view(), name="projects-unread-count"),
    path("projects/<uuid:pk>/", views.ProjectDetailView.as_view(), name="projects-detail"),
    path("projects/<uuid:pk>/messages/", views.ClientMessageListCreateView.as_view(), name="projects-messages"),
    path("projects/<uuid:pk>/messages/mark-read/", views.ClientMarkMessagesReadView.as_view(), name="projects-messages-mark-read"),

    # ── Admin ──────────────────────────────────────────────────────
    path("projects/admin/", views.AdminProjectListView.as_view(), name="projects-admin-list"),
    path("projects/admin/unread-count/", views.AdminUnreadSummaryView.as_view(), name="projects-admin-unread-count"),
    path("projects/admin/<uuid:pk>/", views.AdminProjectDetailView.as_view(), name="projects-admin-detail"),
    path("projects/admin/<uuid:pk>/status-history/", views.AdminProjectStatusHistoryView.as_view(), name="projects-admin-status-history"),
    path("projects/admin/<uuid:pk>/convert-to-contract/", views.AdminProjectConvertToContractView.as_view(), name="projects-admin-convert"),
    path("projects/admin/<uuid:pk>/messages/", views.AdminMessageListCreateView.as_view(), name="projects-admin-messages"),
    path("projects/admin/<uuid:pk>/messages/mark-read/", views.AdminMarkMessagesReadView.as_view(), name="projects-admin-messages-mark-read"),
]
