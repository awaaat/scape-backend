from django.urls import path

from .views import InitializeTransactionView, PaystackWebhookView, VerifyTransactionView

urlpatterns = [
    path("payments/initialize/", InitializeTransactionView.as_view(), name="payments-initialize"),
    path("payments/webhook/", PaystackWebhookView.as_view(), name="payments-webhook"),
    path("payments/verify/<str:reference>/", VerifyTransactionView.as_view(), name="payments-verify"),
]
