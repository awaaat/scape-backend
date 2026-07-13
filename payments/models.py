"""
payments/models.py

Standalone payments app for Scaepe. This app has ZERO knowledge of
property_intel, or of any other feature app — no FK to PropertyReport, no
import of property_intel anywhere in this file. Deliberately so:

  - PaystackTransaction.external_reference is an OPAQUE string the caller
    hands us when initializing a transaction (e.g. property_intel passes
    str(report.id)). We store it and echo it straight back in the
    payment_succeeded signal — we never query, join against, or interpret
    it in any way. As far as this app is concerned it could be a report
    id, a subscription id, or nothing at all.
  - PaystackTransaction.purpose is likewise an opaque tag the caller
    chooses (e.g. "property_report"). It exists purely so a signal
    receiver can cheaply filter "is this event mine?" without this app
    needing to know what the tags mean.

This is what makes payments genuinely reusable for whatever Scaepe charges
for next, instead of being property_intel's payment code wearing a
different app label.
"""
import uuid

from django.db import models


class PaystackTransaction(models.Model):
    """
    One row per payment attempt. Created in 'pending' state the moment we
    call Paystack's Initialize Transaction endpoint — NOT after payment
    succeeds — so an abandoned checkout is still visible/reconcilable
    instead of leaving a gap between "we asked for money" and "we know
    what happened."
    """

    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("success", "Success"),
        ("failed", "Failed"),
        ("abandoned", "Abandoned"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Our own reference, generated before calling Paystack and sent AS the
    # `reference` param on Initialize — never let Paystack generate it,
    # because we need to be able to look this row up the instant we create
    # it, before any response comes back.
    reference = models.CharField(max_length=100, unique=True, db_index=True)

    # Opaque caller-supplied fields — see module docstring. Never joined
    # against, never dereferenced.
    purpose = models.CharField(
        max_length=50, db_index=True,
        help_text="Caller-chosen tag, e.g. 'property_report'. Used only for signal-receiver filtering.",
    )
    external_reference = models.CharField(
        max_length=100, db_index=True,
        help_text="Opaque id from the calling app (e.g. a property_intel report id). Never validated or looked up here.",
    )

    email = models.EmailField()
    amount = models.DecimalField(
        max_digits=10, decimal_places=2,
        help_text="Amount in the major currency unit (e.g. KES, not cents).",
    )
    currency = models.CharField(max_length=3, default="KES")

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending", db_index=True)

    # Populated from Paystack's Initialize response.
    authorization_url = models.URLField(blank=True)
    access_code = models.CharField(max_length=100, blank=True)

    # Populated once confirmed (via webhook or manual verify).
    paystack_transaction_id = models.CharField(
        max_length=50, blank=True, help_text="Paystack's own numeric transaction id (data.id), once known.",
    )
    channel = models.CharField(max_length=30, blank=True, help_text="e.g. 'mobile_money', 'card' — from Paystack's response.")
    authorization_details = models.JSONField(
        default=dict, blank=True,
        help_text="Raw data.authorization object from Paystack's verify/webhook response — "
                   "bin/last4 for cards, provider-specific fields for mobile money. Stored as-is; "
                   "interpreted downstream by whichever app needs it (e.g. property_intel's "
                   "anti-abuse hashing), never parsed here.",
    )
    paid_at = models.DateTimeField(null=True, blank=True)
    failure_reason = models.TextField(blank=True)

    # Whatever extra context the caller wants echoed back on success —
    # never inspected or required by this app.
    metadata = models.JSONField(default=dict, blank=True)

    callback_url = models.URLField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["purpose", "external_reference"]),
            models.Index(fields=["status", "created_at"]),
        ]

    def __str__(self):
        return f"{self.reference} — {self.amount} {self.currency} ({self.status})"

    @property
    def amount_subunit(self):
        """Paystack's API wants the lowest denomination (cents/kobo), as an integer."""
        return int((self.amount * 100).to_integral_value())


class PaystackWebhookEvent(models.Model):
    """
    Append-only log of every webhook delivery received, BEFORE any
    processing decision is made. Two jobs:

      1. Idempotency — Paystack retries webhooks on anything but a 200,
         and may also just double-send. unique_together below means a
         retried delivery for an event we've already recorded is a no-op
         at the database level, not something application code has to
         reason about with a lock.
      2. Forensics — if a signature check fails or an event references a
         reference we don't recognise, this table is the only record that
         the delivery even happened, independent of whether we trusted it.
    """

    event_type = models.CharField(max_length=100, db_index=True, help_text="Paystack's `event` field, e.g. 'charge.success'.")
    reference = models.CharField(max_length=100, db_index=True, help_text="The `data.reference` field from the payload.")

    paystack_transaction = models.ForeignKey(
        PaystackTransaction, on_delete=models.SET_NULL, null=True, blank=True, related_name="webhook_events",
    )

    signature_valid = models.BooleanField(default=False)
    raw_payload = models.JSONField(default=dict, blank=True)

    processed = models.BooleanField(
        default=False, help_text="True once this event actually caused a state transition (vs. being a duplicate/no-op/invalid delivery).",
    )
    processing_note = models.TextField(blank=True)

    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-received_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["event_type", "reference"], name="unique_event_per_reference",
            )
        ]
        indexes = [models.Index(fields=["reference", "event_type"])]

    def __str__(self):
        return f"{self.event_type} — {self.reference} ({'processed' if self.processed else 'not processed'})"


class UserWallet(models.Model):
    """
    One row per Django auth user. Balance in KES (major unit, 2dp).
    Topped up when a wallet_topup payment succeeds; deducted atomically
    when a paid PropertyReport is dispatched for generation.
    Never goes negative — the deduction method returns False if insufficient.
    """
    from django.conf import settings as _settings
    user = models.OneToOneField(
        "auth.User", on_delete=models.CASCADE, related_name="wallet"
    )
    balance = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return f"Wallet({self.user_id}) KES {self.balance}"

    @classmethod
    def get_or_create_for_user(cls, user):
        wallet, _ = cls.objects.get_or_create(user=user)
        return wallet

    def credit(self, amount):
        """Add amount to balance atomically."""
        from django.db.models import F
        UserWallet.objects.filter(pk=self.pk).update(balance=F("balance") + amount)
        self.refresh_from_db(fields=["balance"])

    def debit(self, amount):
        """
        Deduct amount atomically. Returns True if successful, False if
        insufficient funds. Uses a conditional UPDATE so two concurrent
        report submissions cannot both succeed against the same balance.
        """
        from django.db.models import F
        updated = UserWallet.objects.filter(
            pk=self.pk, balance__gte=amount
        ).update(balance=F("balance") - amount)
        if updated:
            self.refresh_from_db(fields=["balance"])
            return True
        return False


class WalletTransaction(models.Model):
    """Append-only ledger — every credit and debit, with reason."""
    TYPES = [
        ("topup", "Top-up"),
        ("report_debit", "Report charge"),
        ("refund", "Refund"),
    ]
    wallet = models.ForeignKey(UserWallet, on_delete=models.CASCADE, related_name="transactions")
    transaction_type = models.CharField(max_length=20, choices=TYPES)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    balance_after = models.DecimalField(max_digits=10, decimal_places=2)
    reference = models.CharField(max_length=100, blank=True, help_text="Paystack ref or report id")
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_transaction_type_display()} KES {self.amount} — wallet {self.wallet_id}"
