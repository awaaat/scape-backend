"""
users/models.py

A lightweight signup record — name, email, phone — for follow-up. This is
deliberately NOT django.contrib.auth.User: no password, no login, no
session. Same reasoning as property_intel.Broker: the person never
authenticates against this app, so there's no reason to carry the weight
of Django's auth system for it. If a real login/portal is ever needed
later, that's a different, additive piece of work — this model is just
"who signed up, and can we prove their email is real."

Email verification uses a hashed token (never store the raw token) with
an expiry, the same pattern as property_intel.OTPVerification — a leaked
DB backup or a curious admin should never be able to verify emails on
someone else's behalf just by reading a column.
"""
import hashlib
import hmac
import secrets
import uuid
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError

from .phone_utils import normalize_kenyan_phone, InvalidKenyanPhone
from django.db import models
from django.utils import timezone

from visitors.models import Visitor

def validate_phone_field(value):
    """Model-level guard — mirrors the serializer's normalization so
    admin-created/edited records can't bypass it either."""
    try:
        normalize_kenyan_phone(value)
    except InvalidKenyanPhone as exc:
        raise DjangoValidationError(str(exc)) from exc

VERIFICATION_TOKEN_VALID_HOURS = 48


class UserSignup(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # The actual login identity — password, is_active-for-auth, etc. all
    # live on this. UserSignup stays the profile/marketing-metadata record
    # it always was (visitor tracking, consent, email verification).
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="signup_profile",
    )

    full_name = models.CharField(max_length=150)
    email = models.EmailField(unique=True)
    phone = models.CharField(max_length=20, validators=[validate_phone_field], db_index=True, unique=True)

    # Links back to the visitor session (see visitors/middleware.py) the
    # same way Lead and JobApplication already do — lets you see what
    # pages someone browsed before signing up, without duplicating that
    # tracking logic here.
    visitor = models.ForeignKey(
        Visitor, on_delete=models.SET_NULL, null=True, blank=True, related_name="signups"
    )
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)

    # ── Consent — same fields/reasoning as jobs.JobApplication ──────────
    consent_given = models.BooleanField(default=False)
    consent_given_at = models.DateTimeField(null=True, blank=True)
    privacy_policy_version = models.CharField(max_length=20, blank=True)

    # ── Email verification ──────────────────────────────────────────────
    email_verified = models.BooleanField(default=False, db_index=True)
    email_verification_token_hash = models.CharField(max_length=64, blank=True)
    email_verification_expires_at = models.DateTimeField(null=True, blank=True)
    email_verified_at = models.DateTimeField(null=True, blank=True)

    is_active = models.BooleanField(
        default=True, help_text="Untick to opt someone out/unsubscribe without deleting their record."
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.full_name} <{self.email}>"

    def generate_verification_token(self):
        """
        Returns the RAW token (the caller emails this) while only the hash
        is persisted. Overwrites any previous token — same "most recent
        wins, old one silently stops working" behavior as
        OTPVerification.request_otp() in property_intel, for the same
        reason: no separate revocation step needed.
        """
        raw_token = secrets.token_urlsafe(32)
        self.email_verification_token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        self.email_verification_expires_at = timezone.now() + timedelta(hours=VERIFICATION_TOKEN_VALID_HOURS)
        self.save(update_fields=["email_verification_token_hash", "email_verification_expires_at"])
        return raw_token

    def verify_token(self, raw_token):
        """Constant-time comparison against the stored hash — a verification
        token doesn't need much protecting, but it costs nothing to do right."""
        if self.email_verified:
            return True
        if not self.email_verification_token_hash or not self.email_verification_expires_at:
            return False
        if timezone.now() >= self.email_verification_expires_at:
            return False
        candidate_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        return hmac.compare_digest(self.email_verification_token_hash, candidate_hash)

    def mark_verified(self):
        self.email_verified = True
        self.email_verified_at = timezone.now()
        self.email_verification_token_hash = ""  # spent — can't be replayed
        self.save(update_fields=["email_verified", "email_verified_at", "email_verification_token_hash"])
