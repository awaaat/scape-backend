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
    path("contracts/milestones/<int:pk>/submit/", views.MilestoneSubmitDeliverableView.as_view(), name="contracts-milestone-submit"),
    path("contracts/milestones/<int:pk>/events/", views.MilestoneEventListView.as_view(), name="contracts-milestone-events"),

    # ── Admin: invoices ────────────────────────────────────────────────
    path("contracts/<uuid:pk>/invoices/", views.ContractInvoiceListView.as_view(), name="contracts-invoices"),
    path("contracts/milestones/<int:pk>/create-invoice/", views.MilestoneCreateInvoiceView.as_view(), name="contracts-milestone-create-invoice"),
    path("contracts/invoices/<uuid:pk>/", views.InvoiceDetailView.as_view(), name="contracts-invoice-detail"),
    path("contracts/invoices/<uuid:pk>/send/", views.InvoiceSendView.as_view(), name="contracts-invoice-send"),
    path("contracts/invoices/<uuid:pk>/void/", views.InvoiceVoidView.as_view(), name="contracts-invoice-void"),
    path("contracts/invoices/<uuid:pk>/record-payment/", views.InvoiceRecordManualPaymentView.as_view(), name="contracts-invoice-record-payment"),

    path("contracts/<uuid:pk>/messages/", views.AdminMessageListCreateView.as_view(), name="contracts-messages"),
    path("contracts/<uuid:pk>/messages/mark-read/", views.AdminMarkMessagesReadView.as_view(), name="contracts-messages-mark-read"),

    # ── Admin: change requests ────────────────────────────────────────
    path("contracts/<uuid:pk>/change-requests/", views.ContractChangeRequestListView.as_view(), name="contracts-change-requests"),
    path("contracts/change-requests/<int:pk>/decide/", views.ContractChangeRequestDecisionView.as_view(), name="contracts-change-request-decide"),

    # ── Admin: disputes ────────────────────────────────────────────────
    path("contracts/<uuid:pk>/disputes/", views.ContractDisputeListView.as_view(), name="contracts-disputes"),
    path("contracts/admin/disputes/open/", views.AdminOpenDisputesView.as_view(), name="contracts-admin-disputes-open"),
    path("contracts/disputes/<int:pk>/resolve/", views.ContractDisputeResolveView.as_view(), name="contracts-dispute-resolve"),

    # ── Client ─────────────────────────────────────────────────────
    path("contracts/client/<uuid:pk>/", views.ClientContractDetailView.as_view(), name="contracts-client-detail"),
    path("contracts/client/<uuid:pk>/sign/", views.ClientSignContractView.as_view(), name="contracts-client-sign"),
    path("contracts/client/<uuid:pk>/milestones/", views.ClientMilestoneListView.as_view(), name="contracts-client-milestones"),
    path("contracts/client/milestones/<int:pk>/pay/", views.ClientMilestonePayView.as_view(), name="contracts-client-milestone-pay"),
    path("contracts/client/milestones/<int:pk>/request-revision/", views.ClientMilestoneRequestRevisionView.as_view(), name="contracts-client-milestone-request-revision"),
    path("contracts/client/milestones/<int:pk>/approve-release/", views.ClientMilestoneApproveReleaseView.as_view(), name="contracts-client-milestone-approve-release"),
    path("contracts/client/<uuid:pk>/messages/", views.ClientMessageListCreateView.as_view(), name="contracts-client-messages"),
    path("contracts/client/<uuid:pk>/messages/mark-read/", views.ClientMarkMessagesReadView.as_view(), name="contracts-client-messages-mark-read"),

    # ── Client: change requests + disputes ──────────────────────────────
    path("contracts/client/mine/", views.ClientContractListView.as_view(), name="contracts-client-mine"),
    path("contracts/client/<uuid:pk>/change-requests/", views.ClientChangeRequestListCreateView.as_view(), name="contracts-client-change-requests"),
    path("contracts/client/<uuid:pk>/disputes/", views.ClientDisputeListCreateView.as_view(), name="contracts-client-disputes"),

    # ── Client: invoices ───────────────────────────────────────────────
    path("contracts/client/<uuid:pk>/invoices/", views.ClientContractInvoiceListView.as_view(), name="contracts-client-invoices"),
    path("contracts/client/invoices/<uuid:pk>/", views.ClientInvoiceDetailView.as_view(), name="contracts-client-invoice-detail"),
    path("contracts/client/invoices/<uuid:pk>/pay/", views.ClientInvoicePayView.as_view(), name="contracts-client-invoice-pay"),
]