"""
Run from ~/scape_backend: python3 patch3_signals.py
Fixes the payment_succeeded handler's wallet debit, which was trying to
debit the FULL Paystack amount from the wallet a second time -- in
practice always a no-op. Now it debits exactly report.wallet_applied_kes
(the partial balance earmarked at checkout by patch2), the amount that
was actually promised, not charged again.
"""
path = "property_intel/signals.py"
with open(path, "r") as f:
    content = f.read()

old = '''    # Debit the user's wallet if this report was funded from balance
    # (purpose still "property_report" — the wallet paid for it upstream).
    # If the user has no wallet or insufficient balance, we still proceed
    # since the Paystack payment already confirmed — wallet just stays at 0.
    try:
        from payments.models import UserWallet, WalletTransaction
        from django.contrib.auth import get_user_model
        User = get_user_model()
        user = User.objects.filter(email__iexact=email).first()
        if user:
            wallet = UserWallet.get_or_create_for_user(user)
            debited = wallet.debit(amount)
            WalletTransaction.objects.create(
                wallet=wallet,
                transaction_type="report_debit",
                amount=amount,
                balance_after=wallet.balance,
                reference=reference,
                note=f"Report {report.id} generated",
            )
            if not debited:
                logger.info("Report %s: wallet balance was 0 or insufficient — Paystack payment covers it directly", report.id)
    except Exception as exc:
        logger.warning("Report %s: wallet debit failed (non-fatal): %s", report.id, exc)'''

new = '''    # If a partial wallet balance was earmarked at checkout (see
    # PinSubmitView / OTPVerifyView in property_intel/views.py --
    # report.wallet_applied_kes), debit exactly that amount now that
    # payment is actually confirmed -- never before, so an abandoned
    # Paystack checkout never strands the balance. `amount` here is only
    # the REMAINDER Paystack charged after that balance was applied, not
    # the full report price -- it must never be debited from the wallet
    # again on top of the earmarked amount.
    if report.wallet_applied_kes and report.pin.broker.user_id:
        try:
            from payments.models import UserWallet, WalletTransaction
            wallet = UserWallet.get_or_create_for_user(report.pin.broker.user)
            debited = wallet.debit(report.wallet_applied_kes)
            if debited:
                WalletTransaction.objects.create(
                    wallet=wallet,
                    transaction_type="report_debit",
                    amount=report.wallet_applied_kes,
                    balance_after=wallet.balance,
                    reference=reference,
                    note=f"Partial balance applied toward report {report.id} (remainder paid via Paystack)",
                )
            else:
                logger.warning(
                    "Report %s: expected to debit KES %s of earmarked wallet balance but it had "
                    "changed since checkout -- Paystack already covered the remainder in full, so "
                    "the report itself is unaffected.",
                    report.id, report.wallet_applied_kes,
                )
        except Exception as exc:
            logger.warning("Report %s: wallet debit failed (non-fatal): %s", report.id, exc)'''

if content.count(old) != 1:
    raise SystemExit("Patch 3 FAILED: anchor not found or not unique -- no changes written.")
with open(path, "w") as f:
    f.write(content.replace(old, new, 1))
print("Patch 3 OK: signals.py now debits only the earmarked wallet_applied_kes, once, when confirmed")
