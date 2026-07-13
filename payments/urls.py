from django.urls import path

from .views import InitializeTransactionView, PaymentHistoryView, PaystackWebhookView, VerifyTransactionView, WalletView

urlpatterns = [
    path("payments/initialize/", InitializeTransactionView.as_view(), name="payments-initialize"),
    path("payments/webhook/", PaystackWebhookView.as_view(), name="payments-webhook"),
    path("payments/verify/<str:reference>/", VerifyTransactionView.as_view(), name="payments-verify"),
    path("payments/history/", PaymentHistoryView.as_view(), name="payments-history"),
    path("payments/wallet/", WalletView.as_view(), name="payments-wallet"),
]
