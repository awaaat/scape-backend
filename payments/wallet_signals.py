"""
payments/wallet_signals.py
Receives wallet_topup_succeeded and credits the user wallet.
Zero knowledge of property_intel — pure payments-layer concern.
"""
import logging
from django.contrib.auth import get_user_model
from django.dispatch import receiver
from .signals import wallet_topup_succeeded
from .models import UserWallet, WalletTransaction

logger = logging.getLogger("payments")
User = get_user_model()


@receiver(wallet_topup_succeeded)
def credit_user_wallet(sender, user_email, amount, currency, reference, **kwargs):
    try:
        user = User.objects.get(email__iexact=user_email)
    except User.DoesNotExist:
        logger.warning("wallet topup: no user found for email %s (ref %s)", user_email, reference)
        return

    wallet = UserWallet.get_or_create_for_user(user)
    wallet.credit(amount)
    WalletTransaction.objects.create(
        wallet=wallet,
        transaction_type="topup",
        amount=amount,
        balance_after=wallet.balance,
        reference=reference,
        note=f"Paystack topup {currency} {amount}",
    )
    logger.info("Wallet credited KES %s for user %s (ref %s)", amount, user_email, reference)
