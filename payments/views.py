"""
payments/views.py

Three endpoints:
    POST /api/payments/initialize/          start a checkout
    POST /api/payments/webhook/              Paystack calls this
    GET  /api/payments/verify/<reference>/   manual/fallback verification

The webhook view is deliberately a plain Django View (not DRF APIView) —
Paystack sends no auth header we'd check via DRF's auth classes, no CSRF
token (it's not a browser), and signature verification needs the exact
raw request bytes before anything parses/re-serializes them.
"""
import json
import logging

from django.http import JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import paystack, services
from .models import PaystackTransaction
from .serializers import InitializeTransactionSerializer, PaystackTransactionSerializer

logger = logging.getLogger("payments")


class InitializeTransactionView(APIView):
    """POST /api/payments/initialize/ — returns an authorization_url the frontend redirects the payer to."""

    throttle_scope = "payments_initialize"

    def post(self, request):
        serializer = InitializeTransactionSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        data = serializer.validated_data

        try:
            txn = services.initialize_transaction(
                email=data["email"],
                amount=data["amount"],
                currency=data.get("currency") or "KES",
                purpose=data["purpose"],
                external_reference=data["external_reference"],
                callback_url=data.get("callback_url", ""),
                metadata=data.get("metadata") or {},
            )
        except services.PaymentInitializationError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

        return Response(PaystackTransactionSerializer(txn).data, status=status.HTTP_201_CREATED)


class VerifyTransactionView(APIView):
    """
    GET /api/payments/verify/<reference>/ — asks Paystack directly and
    confirms locally if it says success. Fallback path for when the
    webhook hasn't landed yet (or at all) by the time the frontend polls.
    """

    def get(self, request, reference):
        try:
            txn = services.verify_and_confirm(reference)
        except paystack.PaystackAPIError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

        if txn is None:
            return Response({"error": "Unknown reference."}, status=status.HTTP_404_NOT_FOUND)

        return Response(PaystackTransactionSerializer(txn).data, status=status.HTTP_200_OK)


@method_decorator(csrf_exempt, name="dispatch")
class PaystackWebhookView(View):
    """
    POST /api/payments/webhook/ — Paystack's server calling us, not a
    browser. Always returns 200 once the delivery is safely logged
    (even for events we don't act on), so Paystack doesn't retry-storm
    us for events we're intentionally ignoring. Only a bad signature gets
    a non-200, since that's the one case retrying is pointless and we'd
    rather Paystack's dashboard flag it.
    """

    def post(self, request, *args, **kwargs):
        raw_body = request.body  # must read raw bytes BEFORE any JSON parsing, for signature verification
        signature = request.headers.get("X-Paystack-Signature", "")
        signature_valid = paystack.verify_webhook_signature(raw_body, signature)

        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            logger.warning("Webhook: unparseable body (signature_valid=%s)", signature_valid)
            return JsonResponse({"error": "Invalid payload."}, status=400)

        event_type = payload.get("event", "")
        data = payload.get("data", {}) or {}
        reference = data.get("reference", "")

        if not signature_valid:
            logger.warning("Webhook: signature verification FAILED for event=%s reference=%s", event_type, reference)
            # Still log it (signature_valid=False) for forensics, but don't act on it.
            services.record_webhook_event(event_type, reference or "unknown", payload, signature_valid=False)
            return JsonResponse({"error": "Invalid signature."}, status=401)

        if not reference:
            logger.warning("Webhook: valid signature but no data.reference — event=%s", event_type)
            return JsonResponse({"status": "ignored"}, status=200)

        event_row, created = services.record_webhook_event(event_type, reference, payload, signature_valid=True)

        if not created:
            # Already processed this exact (event_type, reference) before —
            # a Paystack retry. Acknowledge without reprocessing.
            logger.info("Webhook: duplicate delivery ignored — event=%s reference=%s", event_type, reference)
            return JsonResponse({"status": "duplicate"}, status=200)

        if event_type == "charge.success" and data.get("status") == "success":
            txn = services.confirm_transaction(
                reference,
                paystack_transaction_id=str(data.get("id", "")),
                channel=data.get("channel", ""),
                authorization_details=data.get("authorization", {}) or {},
            )
            event_row.paystack_transaction = txn
            event_row.processed = txn is not None
            event_row.processing_note = "confirmed" if txn else "unknown reference"
            event_row.save(update_fields=["paystack_transaction", "processed", "processing_note"])
        elif event_type == "charge.failed":
            services.mark_transaction_failed(reference, reason=data.get("gateway_response", ""))
            event_row.processed = True
            event_row.processing_note = "marked failed"
            event_row.save(update_fields=["processed", "processing_note"])
        else:
            event_row.processing_note = "event type not acted on"
            event_row.save(update_fields=["processing_note"])

        return JsonResponse({"status": "ok"}, status=200)


class PaymentHistoryView(APIView):
    """GET /api/payments/history/ — the logged-in user's own payment
    history, matched by their account email. Read-only, no cross-user
    leakage: filtered strictly to request.user.email."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        txns = PaystackTransaction.objects.filter(email__iexact=request.user.email).order_by("-created_at")
        return Response(PaystackTransactionSerializer(txns, many=True).data, status=status.HTTP_200_OK)
