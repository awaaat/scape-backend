from django.urls import path

from . import views

urlpatterns = [
    # ── Admin ──────────────────────────────────────────────────────
    path("contracts/", views.ContractListCreateView.as_view(), name="contracts-list-create"),
    path("contracts/<uuid:pk>/", views.ContractDetailView.as_view(), name="contracts-detail"),
    path("contracts/<uuid:pk>/send/", views.ContractSendView.as_view(), name="contracts-send"),
    path("contracts/<uuid:pk>/revisions/", views.ContractRevisionListCreateView.as_view(), name="contracts-revisions"),
    path("contracts/<uuid:pk>/milestones/", views.MilestoneListCreateView.as_view(), name="contracts-milestones"),
    path("contracts/milestones/<int:pk>/", views.MilestoneDetailView.as_view(), name="contracts-milestone-detail"),
    path("contracts/milestones/<int:pk>/invoice/", views.MilestoneInvoiceView.as_view(), name="contracts-milestone-invoice"),
    path("contracts/<uuid:pk>/messages/", views.AdminMessageListCreateView.as_view(), name="contracts-messages"),
    path("contracts/<uuid:pk>/messages/mark-read/", views.AdminMarkMessagesReadView.as_view(), name="contracts-messages-mark-read"),

    # ── Client ─────────────────────────────────────────────────────
    path("contracts/client/<uuid:pk>/", views.ClientContractDetailView.as_view(), name="contracts-client-detail"),
    path("contracts/client/<uuid:pk>/sign/", views.ClientSignContractView.as_view(), name="contracts-client-sign"),
    path("contracts/client/<uuid:pk>/milestones/", views.ClientMilestoneListView.as_view(), name="contracts-client-milestones"),
    path("contracts/client/milestones/<int:pk>/pay/", views.ClientMilestonePayView.as_view(), name="contracts-client-milestone-pay"),
    path("contracts/client/<uuid:pk>/messages/", views.ClientMessageListCreateView.as_view(), name="contracts-client-messages"),
    path("contracts/client/<uuid:pk>/messages/mark-read/", views.ClientMarkMessagesReadView.as_view(), name="contracts-client-messages-mark-read"),
]
