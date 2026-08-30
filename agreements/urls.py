from django.urls import path

from . import views

urlpatterns = [
    path("agreements/templates/", views.AgreementTemplateListCreateView.as_view(), name="agreements-templates-list-create"),
    path("agreements/templates/<uuid:pk>/", views.AgreementTemplateDetailView.as_view(), name="agreements-templates-detail"),
    path("agreements/templates/<uuid:pk>/clone/", views.AgreementTemplateCloneView.as_view(), name="agreements-templates-clone"),
    path("agreements/", views.AgreementListCreateView.as_view(), name="agreements-list-create"),
    path("agreements/<uuid:pk>/", views.AgreementDetailView.as_view(), name="agreements-detail"),
    path("agreements/<uuid:pk>/events/", views.AgreementEventListView.as_view(), name="agreements-events"),
    path("agreements/<uuid:pk>/send/", views.AgreementSendView.as_view(), name="agreements-send"),
    path("agreements/<uuid:pk>/void/", views.AgreementVoidView.as_view(), name="agreements-void"),
    path("agreements/<uuid:pk>/countersign/", views.AgreementCountersignView.as_view(), name="agreements-countersign"),
    path("agreements/client/mine/", views.ClientAgreementListView.as_view(), name="agreements-client-mine"),
    path("agreements/client/<uuid:pk>/", views.ClientAgreementDetailView.as_view(), name="agreements-client-detail"),
    path("agreements/client/<uuid:pk>/sign/", views.ClientSignAgreementView.as_view(), name="agreements-client-sign"),
    path("agreements/client/<uuid:pk>/decline/", views.ClientDeclineAgreementView.as_view(), name="agreements-client-decline"),
]
