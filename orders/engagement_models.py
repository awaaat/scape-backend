"""
orders/engagement_models.py

Persistence for the order ENGAGEMENT layer (intent capture, exit detection,
reminder ladder, lead scoring). Imported at the bottom of orders/models.py so
Django registers these models under the `orders` app. They reference
"orders.Order" by string, so there is no circular import.

  EmailSuppression  - global do-not-email list (unsubscribes, hard bounces,
                      spam complaints, manual). Checked before EVERY
                      client-facing marketing email.
  OrderEngagement   - one row per order: all the state the engine needs
                      (intent alert claimed?, exit signalled?, lead score,
                      reminder ladder position, next due time, stop reason...).
  OrderReminderLog  - one row per reminder attempt: what was sent, to whom,
                      which A/B variant, provider message id, clicks.
"""
from django.db import IntegrityError, models, transaction
from django.utils import timezone


class EmailSuppression(models.Model):
    class Reason(models.TextChoices):
        UNSUBSCRIBED = "unsubscribed", "Unsubscribed"
        BOUNCED = "bounced", "Hard bounce / invalid address"
        COMPLAINED = "complained", "Spam complaint"
        MANUAL = "manual", "Added manually"

    email = models.EmailField(unique=True)
    reason = models.CharField(max_length=14, choices=Reason.choices, default=Reason.UNSUBSCRIBED)
    source = models.CharField(max_length=40, blank=True, help_text="link, webhook, admin ...")
    note = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "email suppression"
        verbose_name_plural = "email suppressions (do-not-email list)"

    def __str__(self):
        return f"{self.email} ({self.reason})"

    @staticmethod
    def normalise(email):
        return (email or "").strip().lower()

    def save(self, *args, **kwargs):
        self.email = self.normalise(self.email)
        super().save(*args, **kwargs)

    @classmethod
    def add(cls, email, reason=Reason.UNSUBSCRIBED, source="", note=""):
        email = cls.normalise(email)
        if not email:
            return None
        obj, _ = cls.objects.get_or_create(
            email=email, defaults={"reason": reason, "source": source[:40], "note": note[:200]},
        )
        return obj

    @classmethod
    def reason_for(cls, email):
        """'' when the address may be emailed, otherwise the suppression reason."""
        email = cls.normalise(email)
        if not email:
            return ""
        return cls.objects.filter(email=email).values_list("reason", flat=True).first() or ""


class OrderEngagement(models.Model):
    class Tier(models.TextChoices):
        HOT = "hot", "Hot"
        WARM = "warm", "Warm"
        COLD = "cold", "Cold"

    order = models.OneToOneField("orders.Order", on_delete=models.CASCADE, related_name="engagement")

    # ── intent alert ("someone started an order") ───────────────────
    intent_notified_at = models.DateTimeField(null=True, blank=True)
    intent_skipped_reason = models.CharField(
        max_length=30, blank=True,
        help_text="quick_submit | deduped | pre_existing - why no intent alert was sent.",
    )
    intent_attempt_no = models.PositiveSmallIntegerField(default=1, help_text="Nth order attempt from this email.")
    intent_failures = models.PositiveSmallIntegerField(default=0)

    # ── exit detection ──────────────────────────────────────────────
    exit_signalled_at = models.DateTimeField(null=True, blank=True)
    exit_signal_count = models.PositiveSmallIntegerField(default=0)

    # ── lead scoring ────────────────────────────────────────────────
    lead_score = models.PositiveSmallIntegerField(default=0, db_index=True)
    lead_tier = models.CharField(max_length=5, choices=Tier.choices, default=Tier.COLD, db_index=True)
    score_breakdown = models.JSONField(default=list, blank=True)
    scored_at = models.DateTimeField(null=True, blank=True)

    # ── reminder ladder ─────────────────────────────────────────────
    reminders_sent = models.PositiveSmallIntegerField(default=0, help_text="Position on the CURRENT ladder (resets when they come back).")
    lifetime_reminders = models.PositiveSmallIntegerField(default=0)
    last_reminder_at = models.DateTimeField(null=True, blank=True)
    next_reminder_at = models.DateTimeField(null=True, blank=True, db_index=True)
    send_failures = models.PositiveSmallIntegerField(default=0)
    reminders_paused = models.BooleanField(default=False, help_text="Tick to stop automatic reminders for this order.")
    stop_reason = models.CharField(max_length=24, blank=True, db_index=True)

    # ── response signals ────────────────────────────────────────────
    clicks = models.PositiveIntegerField(default=0)
    last_clicked_at = models.DateTimeField(null=True, blank=True)
    recovered_by_stage = models.PositiveSmallIntegerField(
        null=True, blank=True, help_text="Set when the order was submitted shortly after this reminder.",
    )

    # ── escalations to the admin ────────────────────────────────────
    hot_alert_sent_at = models.DateTimeField(null=True, blank=True)
    click_alert_sent_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "order engagement"
        verbose_name_plural = "order engagement"

    def __str__(self):
        return f"Engagement for order #{self.order_id} [{self.lead_tier} {self.lead_score}]"

    @classmethod
    def for_order(cls, order):
        """Get-or-create, safe against two threads racing to create the row."""
        obj = cls.objects.filter(order_id=order.pk).first()
        if obj is not None:
            return obj
        try:
            with transaction.atomic():
                return cls.objects.create(order_id=order.pk)
        except IntegrityError:
            return cls.objects.get(order_id=order.pk)


class OrderReminderLog(models.Model):
    class Status(models.TextChoices):
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"

    order = models.ForeignKey("orders.Order", on_delete=models.CASCADE, related_name="reminder_logs")
    email = models.EmailField(db_index=True)
    stage = models.PositiveSmallIntegerField()
    variant = models.CharField(max_length=2, default="A")
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.SENT, db_index=True)
    subject = models.CharField(max_length=200, blank=True)
    provider_message_id = models.CharField(max_length=200, blank=True)
    error = models.CharField(max_length=300, blank=True)
    sent_at = models.DateTimeField(default=timezone.now, db_index=True)
    clicked_at = models.DateTimeField(null=True, blank=True)
    click_count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-sent_at"]
        indexes = [models.Index(fields=["email", "sent_at"])]

    def __str__(self):
        return f"{self.order_id} stage {self.stage}{self.variant} -> {self.email} [{self.status}]"
