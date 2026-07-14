"""
Run from ~/scape_backend: python3 patch2_views.py
Makes checkout charge (price - wallet balance) instead of always the full
price. A partial balance (e.g. KES 36 left after a prior report) is now
earmarked on the report (wallet_applied_kes) and only actually debited
once Paystack confirms the remainder -- see patch3_signals.py.
"""
path = "property_intel/views.py"
with open(path, "r") as f:
    content = f.read()


def apply(label, old, new, content):
    if old not in content:
        raise SystemExit(f"ERROR: anchor not found for [{label}] -- aborting without changes.")
    if content.count(old) > 1:
        raise SystemExit(f"ERROR: anchor for [{label}] is not unique -- aborting without changes.")
    return content.replace(old, new, 1)


content = apply(
    "Decimal import",
    "import logging\n\nfrom django.conf import settings",
    "import logging\nfrom decimal import Decimal\n\nfrom django.conf import settings",
    content,
)

old_initiate = '''def _initiate_report_payment(report):
    """
    Starts a Paystack checkout for a report sitting in 'awaiting_payment'.
    Returns (authorization_url, error_message) — exactly one will be set.
    A failure here (Paystack down, bad config) must never 500 the whole
    pin-submission response; the report stays 'awaiting_payment' and the
    broker sees an honest message instead of a stack trace.
    """
    broker = report.pin.broker
    try:
        txn = payment_services.initialize_transaction(
            email=broker.email,
            amount=PROPERTY_REPORT_PRICE_KES,
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
    return txn.authorization_url, None'''

new_initiate = '''def _initiate_report_payment(report, amount):
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
    return txn.authorization_url, None'''

content = apply("_initiate_report_payment signature", old_initiate, new_initiate, content)

old_try_wallet = '''def _try_pay_from_wallet(user_id, *, report_price):
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
    return True'''

new_try_wallet = '''def _try_pay_from_wallet(user_id, *, report_price):
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
    return UserWallet.get_or_create_for_user(user).balance'''

content = apply("_wallet_balance helper", old_try_wallet, new_try_wallet, content)

old_pin_checkout = '''        # No wallet balance (or anonymous) — start a Paystack checkout. The
        # report sits in 'awaiting_payment' until payments/signals.py's
        # receiver sees a payment_succeeded event for it (see
        # property_intel/signals.py) and flips it to 'pending' + dispatches
        # generation. Nothing here talks to Paystack directly — that's
        # payments/services.py.
        report = PropertyReport.objects.create(
            pin=pin, status="awaiting_payment", is_free_tier=False, price_charged_kes=PROPERTY_REPORT_PRICE_KES,
        )
        authorization_url, error = _initiate_report_payment(report)
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
                "amount_kes": str(PROPERTY_REPORT_PRICE_KES),
                "message": "You've used all your free reports on this device. Complete payment to generate this report.",
            },
            status=status.HTTP_402_PAYMENT_REQUIRED,
        )'''

new_pin_checkout = '''        # Wallet didn't cover the FULL price, but may still have a partial
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

        report = PropertyReport.objects.create(
            pin=pin, status="awaiting_payment", is_free_tier=False,
            price_charged_kes=PROPERTY_REPORT_PRICE_KES,
            wallet_applied_kes=wallet_applied if wallet_applied > 0 else None,
        )
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
                "message": "You've used all your free reports on this device. Complete payment to generate this report.",
            },
            status=status.HTTP_402_PAYMENT_REQUIRED,
        )'''

content = apply("PinSubmitView checkout branch", old_pin_checkout, new_pin_checkout, content)

old_otp_checkout = '''        # Free reports ran out in the gap between the original submission
        # and OTP verification (rare, but possible under concurrent use).
        report.status = "awaiting_payment"
        report.is_free_tier = False
        report.price_charged_kes = PROPERTY_REPORT_PRICE_KES
        report.save(update_fields=["status", "is_free_tier", "price_charged_kes"])
        authorization_url, error = _initiate_report_payment(report)
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
                "amount_kes": str(PROPERTY_REPORT_PRICE_KES),
                "message": "Your free reports on this device were used up while verifying. Complete payment to generate this report.",
            },
            status=status.HTTP_402_PAYMENT_REQUIRED,
        )'''

new_otp_checkout = '''        # Free reports ran out in the gap between the original submission
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
        )'''

content = apply("OTPVerifyView checkout branch", old_otp_checkout, new_otp_checkout, content)

with open(path, "w") as f:
    f.write(content)

print("views.py patched successfully:")
print(" - _initiate_report_payment now charges a given `amount`, not always the flat price.")
print(" - New _wallet_balance() reads balance without debiting it.")
print(" - Both checkout branches (PinSubmitView, OTPVerifyView) now earmark any partial")
print("   wallet balance on the report and send Paystack only the remainder.")
