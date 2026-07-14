"""
property_intel/views.py

Entry points for the broker-facing flow:

  POST /api/property/pins/                submit a location, get a report
                                           (or a "verify your phone" /
                                           "free reports exhausted" signal)
  POST /api/property/otp/request/         send an SMS OTP code
  POST /api/property/otp/verify/          verify it and resume generation
  GET  /api/property/reports/<id>/        poll report status

Nothing here talks to Google or renders a PDF directly — that's
tasks.generate_report_task, dispatched and never awaited. A request to
this view always returns quickly regardless of how slow enrichment is.

No payment processing lives in this app. When a device has exhausted its
free reports, the report is created with status="awaiting_payment" and the
response says so plainly — wiring an actual payment provider in is future
scope, deliberately kept out of property_intel.
"""
import logging
from decimal import Decimal

from django.conf import settings
from django.db.models import Sum
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

# property_intel is allowed to depend on payments (it calls the public
# services API to start a checkout) — the decoupling is one-directional.
# payments must never import anything from property_intel; see
# payments/models.py's docstring and property_intel/signals.py for the
# other half of this (payments -> property_intel via a generic signal).
from payments import services as payment_services

from .fraud import MANUAL_REVIEW_THRESHOLD, compute_suspicion_score, is_disposable_email
from .ip_intel import check_ip_intel
from .models import Broker, DeviceFingerprint, PropertyReport
from .otp import OTPError, request_otp, verify_otp
from .serializers import (
    OTPRequestSerializer,
    OTPVerifySerializer,
    PinSubmitSerializer,
    PropertyReportSerializer,
)
from .services import LocationParseError, create_pin
from .tasks import generate_report_task

logger = logging.getLogger("property_intel")

# What a paid report costs. A flat rate rather than anything dynamic for
# now — revisit if/when reports get tiered.
PROPERTY_REPORT_PRICE_KES = getattr(settings, "PROPERTY_REPORT_PRICE_KES", 250)

REPORT_PAYMENT_PURPOSE = "property_report"


def _initiate_report_payment(report, amount):
    """
    Starts a Paystack checkout for a report sitting in 'awaiting_payment',
    for `amount` KES -- the full report price, OR a smaller remainder if a
    partial wallet balance already covered part of it (see
    report.wallet_applied_kes and the callers of this function).
    Returns (authorization_url, error_message) — exactly one will be set.
    A failure here (Paystack down, bad config) must never 500 the whole
    pin-submission response; the report stays 'awaiting_payment' and the
    broker sees an honest message instead of a stack trace.
    """
    broker = report.pin.broker
    try:
        txn = payment_services.initialize_transaction(
            email=broker.email,
            amount=amount,
            currency="KES",
            purpose=REPORT_PAYMENT_PURPOSE,
            external_reference=str(report.id),
            callback_url=getattr(settings, "PAYSTACK_CALLBACK_URL", ""),
            metadata={"report_id": str(report.id), "pin_id": str(report.pin_id)},
        )
    except payment_services.PaymentInitializationError as exc:
        logger.error("Could not start payment for report %s: %s", report.id, exc)
        return None, "Payment could not be started right now — please try again shortly."

    PropertyReport.objects.filter(pk=report.pk).update(paystack_reference=txn.reference)
    return txn.authorization_url, None


def _try_pay_from_wallet(user_id, *, report_price):
    """
    'Auto-detect balance': before bouncing a logged-in broker to a fresh
    Paystack checkout, see if their wallet already covers this report.
    Returns True (and leaves a report_debit WalletTransaction behind) on
    success; returns False with zero side effects if there's no linked
    user, no wallet, or insufficient balance.
    """
    from django.contrib.auth import get_user_model
    from payments.models import UserWallet, WalletTransaction

    User = get_user_model()
    try:
        user = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        return False

    wallet = UserWallet.get_or_create_for_user(user)
    if not wallet.debit(report_price):
        return False

    WalletTransaction.objects.create(
        wallet=wallet,
        transaction_type="report_debit",
        amount=report_price,
        balance_after=wallet.balance,
        reference="",
        note="Report paid from wallet balance (auto-detected at submission)",
    )
    return True


def _wallet_balance(user_id):
    """
    Current wallet balance for a logged-in broker's linked user, WITHOUT
    debiting it -- used only to compute how much of a partial balance can
    be applied toward a report's price before sending the remainder to
    Paystack. Returns Decimal('0') for anonymous/no-wallet users. The
    actual debit happens later, only once payment is confirmed -- see
    property_intel/signals.py.
    """
    if not user_id:
        return Decimal("0")
    from django.contrib.auth import get_user_model
    from payments.models import UserWallet

    User = get_user_model()
    try:
        user = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        return Decimal("0")
    return UserWallet.get_or_create_for_user(user).balance


def _client_ip(request):
    """Respect a trusted reverse proxy's X-Forwarded-For; fall back to REMOTE_ADDR.
    Same pattern used in jobs/serializers.py — kept consistent across the codebase."""
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


class PinSubmitView(APIView):
    """
    The main entry point. One request in, one of four outcomes out:
      - blocked device                          -> 403
      - score crosses manual-review threshold   -> 202, held for a human
      - score crosses OTP threshold              -> 200, requires_otp=True
      - free report available                    -> 201, report queued
      - free reports exhausted                   -> 402, no payment yet
    """

    throttle_scope = "property_pin"

    def post(self, request):
        serializer = PinSubmitSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        data = serializer.validated_data

        ip = _client_ip(request)
        fingerprint = self._resolve_fingerprint(data["fingerprint_hash"], ip)

        if fingerprint.is_blocked:
            return Response(
                {
                    "error": "This device has been blocked from generating reports. "
                    "Contact support if you believe this is a mistake."
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        broker = self._resolve_broker(request, data["email"], fingerprint, ip)

        try:
            pin, cell = create_pin(data["raw_input"], broker=broker)
        except LocationParseError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        score, reasons = compute_suspicion_score(
            device_fingerprint=fingerprint, broker=broker, email=data["email"], location_cell=cell,
        )
        logger.info("Pin %s scored %s: %s", pin.id, score, "; ".join(reasons) or "no signals")

        if score >= MANUAL_REVIEW_THRESHOLD:
            report = PropertyReport.objects.create(pin=pin, status="awaiting_review", is_free_tier=True)
            return Response(
                {
                    "report_id": str(report.id),
                    "status": report.status,
                    "message": "This request needs a quick manual review before a report can be generated. We'll be in touch shortly.",
                },
                status=status.HTTP_202_ACCEPTED,
            )

        if fingerprint.requires_otp_verification:
            report = PropertyReport.objects.create(pin=pin, status="pending", is_free_tier=True)
            return Response(
                {
                    "report_id": str(report.id),
                    "requires_otp": True,
                    "message": "Please verify your phone number to continue — request a code, then confirm it.",
                },
                status=status.HTTP_200_OK,
            )

        if fingerprint.consume_free_report():
            report = PropertyReport.objects.create(pin=pin, status="pending", is_free_tier=True)
            generate_report_task.delay(str(report.id))
            return Response(PropertyReportSerializer(report).data, status=status.HTTP_201_CREATED)

        # Free tier exhausted — try the broker's wallet balance before
        # falling back to a fresh Paystack checkout. Only brokers linked to
        # a logged-in dashboard account have a wallet, so this is a clean
        # no-op for anonymous submissions.
        if broker.user_id and _try_pay_from_wallet(broker.user_id, report_price=PROPERTY_REPORT_PRICE_KES):
            report = PropertyReport.objects.create(
                pin=pin, status="pending", is_free_tier=False, is_paid=True, paid_at=timezone.now(),
                price_charged_kes=PROPERTY_REPORT_PRICE_KES,
            )
            generate_report_task.delay(str(report.id))
            return Response(PropertyReportSerializer(report).data, status=status.HTTP_201_CREATED)

        # Wallet didn't cover the FULL price, but may still have a partial
        # balance worth applying (e.g. KES 36 left from an earlier top-up).
        # Earmark it on the report and charge Paystack only the remainder --
        # the wallet itself isn't touched until payment_succeeded confirms
        # the remainder went through (property_intel/signals.py), so an
        # abandoned checkout never strands the balance.
        wallet_applied = _wallet_balance(broker.user_id)
        remainder = Decimal(str(PROPERTY_REPORT_PRICE_KES)) - wallet_applied
        if remainder <= 0:
            wallet_applied = Decimal(str(PROPERTY_REPORT_PRICE_KES))
            remainder = Decimal("1")

        # No Paystack call here -- that's what used to stall this response.
        # See ReportCheckoutView below; frontend calls it right after this
        # returns, as its own separately-timed-out request.
        report = PropertyReport.objects.create(
            pin=pin, status="awaiting_payment", is_free_tier=False,
            price_charged_kes=PROPERTY_REPORT_PRICE_KES,
            wallet_applied_kes=wallet_applied if wallet_applied > 0 else None,
        )
        return Response(
            {
                "report_id": str(report.id),
                "status": report.status,
                "amount_kes": str(remainder),
                "wallet_applied_kes": str(wallet_applied) if wallet_applied > 0 else None,
                "message": "You've used all your free reports on this device. Complete payment to generate this report.",
            },
            status=status.HTTP_402_PAYMENT_REQUIRED,
        )

    def _resolve_fingerprint(self, fingerprint_hash, ip):
        fingerprint, created = DeviceFingerprint.objects.get_or_create(fingerprint_hash=fingerprint_hash)
        is_new_ip = ip not in (fingerprint.known_ips or [])
        fingerprint.record_sighting(ip=ip)

        # Only spend a lookup call when this IP hasn't been seen on this
        # device before — repeat requests from the same IP shouldn't
        # re-query ip-api.com every time.
        if ip and (created or is_new_ip):
            is_datacenter, asn_name = check_ip_intel(ip)
            DeviceFingerprint.objects.filter(pk=fingerprint.pk).update(
                is_datacenter_ip=is_datacenter, ip_asn_name=asn_name,
            )
            fingerprint.is_datacenter_ip = is_datacenter
            fingerprint.ip_asn_name = asn_name

        return fingerprint

    def _resolve_broker(self, request, email, fingerprint, ip):
        broker, created = Broker.objects.get_or_create(
            email=email,
            defaults={
                "email_is_disposable": is_disposable_email(email),
                "device_fingerprint": fingerprint,
                "signup_ip": ip,
            },
        )
        if not created and broker.device_fingerprint_id != fingerprint.pk:
            # Same email now showing up on a different device — link it.
            # This is itself part of the fraud picture (see
            # DeviceFingerprint.linked_emails growing on the new device),
            # not something to silently ignore.
            broker.device_fingerprint = fingerprint
            broker.save(update_fields=["device_fingerprint"])

        # Link to the logged-in dashboard account, if any. request.user is
        # AnonymousUser (not None) when no valid JWT was sent — safe to
        # check .is_authenticated either way.
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated and broker.user_id != user.pk:
            broker.user = user
            broker.save(update_fields=["user"])

        fingerprint.record_sighting(email=email)
        return broker


class OTPRequestView(APIView):
    """POST /api/property/otp/request/ — sends the SMS code for a flagged device."""

    throttle_scope = "property_otp"

    def post(self, request):
        serializer = OTPRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        data = serializer.validated_data

        fingerprint = get_object_or_404(DeviceFingerprint, fingerprint_hash=data["fingerprint_hash"])
        if fingerprint.is_blocked:
            return Response({"error": "This device has been blocked."}, status=status.HTTP_403_FORBIDDEN)

        try:
            request_otp(fingerprint, data["phone_number"])
        except OTPError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response({"message": "Verification code sent."}, status=status.HTTP_200_OK)


class OTPVerifyView(APIView):
    """
    POST /api/property/otp/verify/ — verifies the code and, if correct,
    resumes the specific pending report that triggered the OTP requirement.
    report_id is required (not "most recent pending report for this
    device") so a client retry or a second in-flight tab can never resume
    the wrong report.
    """

    throttle_scope = "property_otp"

    def post(self, request):
        serializer = OTPVerifySerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        data = serializer.validated_data

        fingerprint = get_object_or_404(DeviceFingerprint, fingerprint_hash=data["fingerprint_hash"])

        try:
            verify_otp(fingerprint, data["phone_number"], data["code"])
        except OTPError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        report = get_object_or_404(
            PropertyReport.objects.select_related("pin__broker__device_fingerprint"),
            pk=data["report_id"],
        )
        if report.pin.broker.device_fingerprint_id != fingerprint.pk:
            # The verified phone doesn't belong to whoever owns this
            # report's device — never resume someone else's report.
            return Response({"error": "This report does not belong to the verified device."}, status=status.HTTP_403_FORBIDDEN)

        if report.status != "pending":
            return Response(PropertyReportSerializer(report).data, status=status.HTTP_200_OK)

        if fingerprint.consume_free_report():
            generate_report_task.delay(str(report.id))
            return Response(PropertyReportSerializer(report).data, status=status.HTTP_200_OK)

        # Free reports ran out in the gap between the original submission
        # and OTP verification (rare, but possible under concurrent use).
        # Same partial-wallet-balance logic as PinSubmitView -- see there
        # for why the wallet isn't debited until payment is confirmed.
        wallet_applied = _wallet_balance(report.pin.broker.user_id)
        remainder = Decimal(str(PROPERTY_REPORT_PRICE_KES)) - wallet_applied
        if remainder <= 0:
            wallet_applied = Decimal(str(PROPERTY_REPORT_PRICE_KES))
            remainder = Decimal("1")

        report.status = "awaiting_payment"
        report.is_free_tier = False
        report.price_charged_kes = PROPERTY_REPORT_PRICE_KES
        report.wallet_applied_kes = wallet_applied if wallet_applied > 0 else None
        report.save(update_fields=["status", "is_free_tier", "price_charged_kes", "wallet_applied_kes"])
        authorization_url, error = _initiate_report_payment(report, amount=remainder)
        if error:
            return Response(
                {"report_id": str(report.id), "status": report.status, "message": error},
                status=status.HTTP_402_PAYMENT_REQUIRED,
            )
        return Response(
            {
                "report_id": str(report.id),
                "status": report.status,
                "checkout_url": authorization_url,
                "amount_kes": str(remainder),
                "wallet_applied_kes": str(wallet_applied) if wallet_applied > 0 else None,
                "message": "Your free reports on this device were used up while verifying. Complete payment to generate this report.",
            },
            status=status.HTTP_402_PAYMENT_REQUIRED,
        )


class ReportStatusView(APIView):
    """GET /api/property/reports/<uuid:report_id>/ — for the frontend to poll."""

    def get(self, request, report_id):
        report = get_object_or_404(PropertyReport, pk=report_id)
        return Response(PropertyReportSerializer(report).data, status=status.HTTP_200_OK)


class ReportListView(APIView):
    """
    GET /api/property/reports/ — the dashboard's "My Reports" list.
    Requires login (overrides the app-wide AllowAny default) — this is
    the one endpoint in this app that reads back identity-linked data,
    everything else here is deliberately anonymous-friendly.
    Returns reports for every Broker record linked to request.user. Only
    pins submitted WHILE logged in get linked (see
    PinSubmitView._resolve_broker) — a broker submitting before ever
    logging in won't retroactively show up here.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        reports = (
            PropertyReport.objects
            .filter(pin__broker__user=request.user)
            .select_related("pin", "pin__location_cell")
            .order_by("-created_at")
        )
        return Response(PropertyReportSerializer(reports, many=True).data, status=status.HTTP_200_OK)


class UsageView(APIView):
    """
    GET /api/property/usage/ — free-tier usage for the logged-in user's
    dashboard (Pricing.jsx reads usage.freeReportsRemaining).

    A user can have multiple Broker records (one per device fingerprint
    linked while logged in — see PinSubmitView._resolve_broker). Remaining
    reports are summed across every DISTINCT device fingerprint linked to
    this user via a DB-level aggregate, not a Python loop, so this stays
    O(1) queries regardless of how many devices a user has accumulated.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        totals = (
            DeviceFingerprint.objects
            .filter(brokers__user=request.user)
            .distinct()
            .aggregate(
                remaining=Sum("free_reports_remaining"),
                used=Sum("free_reports_used_total"),
            )
        )
        default_free = getattr(settings, "PROPERTY_REPORT_FREE_TIER_DISPLAY", 5)
        remaining = totals["remaining"]
        return Response(
            {
                "freeReportsRemaining": default_free if remaining is None else remaining,
                "freeReportsUsedTotal": totals["used"] or 0,
            },
            status=status.HTTP_200_OK,
        )


class ReportRetryView(APIView):
    """POST /api/property/reports/<id>/retry/ — user-triggered retry for a
    failed report. No new charge: the broker already paid (or used a free
    slot) for the original attempt."""

    def post(self, request, report_id):
        report = get_object_or_404(PropertyReport.objects.select_related("pin__broker__user"), pk=report_id)

        user = getattr(request, "user", None)
        if report.pin.broker.user_id and user is not None and user.is_authenticated and report.pin.broker.user_id != user.pk:
            return Response({"error": "This report does not belong to you."}, status=status.HTTP_403_FORBIDDEN)

        if report.status != "failed":
            return Response(
                {"error": f"Only a failed report can be retried (current status: {report.status})."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        PropertyReport.objects.filter(pk=report_id).update(status="pending", failure_reason="")
        generate_report_task.delay(str(report.id))
        report.refresh_from_db()
        return Response(PropertyReportSerializer(report).data, status=status.HTTP_200_OK)


class ReportCancelView(APIView):
    """POST /api/property/reports/<id>/cancel/ — cancel a pending/generating
    report. Credits one free report back immediately."""

    def post(self, request, report_id):
        report = get_object_or_404(
            PropertyReport.objects.select_related("pin__broker__user", "pin__broker__device_fingerprint"),
            pk=report_id,
        )

        user = getattr(request, "user", None)
        if report.pin.broker.user_id and user is not None and user.is_authenticated and report.pin.broker.user_id != user.pk:
            return Response({"error": "This report does not belong to you."}, status=status.HTTP_403_FORBIDDEN)

        if report.status not in ("pending", "generating", "awaiting_payment"):
            return Response(
                {"error": f"Only a pending, generating, or awaiting-payment report can be cancelled (current status: {report.status})."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        from .tasks import _credit_back_report
        PropertyReport.objects.filter(pk=report_id).update(status="cancelled")
        _credit_back_report(report, reason="user cancelled")
        report.refresh_from_db()
        return Response(PropertyReportSerializer(report).data, status=status.HTTP_200_OK)


class ReportCheckoutView(APIView):
    """POST /api/property/reports/<id>/checkout/ -- the ONLY place that
    calls Paystack for a report payment. Split out of PinSubmitView.post
    so a live third-party call never blocks report submission itself."""

    def post(self, request, report_id):
        report = get_object_or_404(
            PropertyReport.objects.select_related("pin__broker__user"), pk=report_id,
        )

        user = getattr(request, "user", None)
        if report.pin.broker.user_id and user is not None and user.is_authenticated and report.pin.broker.user_id != user.pk:
            return Response({"error": "This report does not belong to you."}, status=status.HTTP_403_FORBIDDEN)

        if report.status != "awaiting_payment":
            return Response(
                {"error": f"This report isn't awaiting payment (current status: {report.status})."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if report.paystack_reference:
            from payments.models import PaystackTransaction
            existing = PaystackTransaction.objects.filter(reference=report.paystack_reference).first()
            if existing and existing.authorization_url and existing.status == "pending":
                return Response(
                    {
                        "report_id": str(report.id), "status": report.status,
                        "checkout_url": existing.authorization_url,
                        "message": "Complete payment to generate this report.",
                    },
                    status=status.HTTP_402_PAYMENT_REQUIRED,
                )

        wallet_applied = report.wallet_applied_kes or Decimal("0")
        remainder = Decimal(str(report.price_charged_kes)) - wallet_applied
        if remainder <= 0:
            remainder = Decimal("1")

        authorization_url, error = _initiate_report_payment(report, amount=remainder)
        if error:
            return Response(
                {"report_id": str(report.id), "status": report.status, "error": error, "message": error},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response(
            {
                "report_id": str(report.id), "status": report.status,
                "checkout_url": authorization_url, "amount_kes": str(remainder),
                "message": "Complete payment to generate this report.",
            },
            status=status.HTTP_402_PAYMENT_REQUIRED,
        )
