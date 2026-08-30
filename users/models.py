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

# Only country this app enforces Kenyan-format phone numbers for. Anywhere
# else, phone is optional free-form text (or omitted entirely) — see
# UserSignup.clean() and UserSignupCreateSerializer.validate() for the two
# places this same rule is applied (API path never calls full_clean(), so
# both need to agree independently rather than one delegating to the other).
KENYA_COUNTRY_CODE = "KE"

VERIFICATION_TOKEN_VALID_HOURS = 48


def validate_phone_field(value):
    """
    Legacy field-level validator — referenced by an already-generated
    migration (users/migrations/0003_alter_usersignup_phone.py) via its
    import path, so it must keep existing here even though current phone
    validation lives in UserSignup.clean() and
    UserSignupCreateSerializer.validate() instead (those have access to
    `country`, which a plain field validator never does). Not attached to
    the live `phone` field anymore — kept only so the historical migration
    can still be imported.
    """
    if not value:
        return
    try:
        normalize_kenyan_phone(value)
    except InvalidKenyanPhone as exc:
        raise DjangoValidationError(str(exc)) from exc


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

    # Nullable/blank: only required for Kenyan accounts (see clean() and
    # the serializer). Non-Kenyan signups may have no phone at all.
    # NULL, not "", when absent — Postgres treats multiple NULLs as
    # distinct under a unique index, so this doesn't collide the way two
    # blank strings would.
    phone = models.CharField(
        max_length=20, db_index=True, unique=True, null=True, blank=True,
    )

    # ISO 3166-1 alpha-2 country code, captured once at signup. Drives
    # which currency (payments.UserWallet) this user's wallet operates
    # in — see payments/views.py's WalletTopUpView. Deliberately not
    # geolocated: IP-based location is too unstable for a payments
    # decision (VPNs, roaming), so this is a value the client sets
    # themselves at registration and it becomes the durable source of
    # truth for "what currency does this person pay in."
    country = models.CharField(
        max_length=2, default=KENYA_COUNTRY_CODE, db_index=True,
        help_text="ISO 3166-1 alpha-2 country code (e.g. 'KE', 'US'), set at signup.",
    )

    # Public URL from users/storage.py's Supabase upload — same "URL, not
    # FileField" pattern as contracts.Contract.contract_document_url,
    # since actual bytes live in object storage, never on local disk.
    avatar_url = models.URLField(blank=True)

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

    def clean(self):
        """
        Conditional phone validation, run on full_clean() (e.g. the Django
        admin's ModelForm). Not wired in as a field-level `validators=[...]`
        entry because a plain validator only ever sees the phone value in
        isolation — it can't know this account's country, so it can't tell
        a valid US number from a malformed Kenyan one. The API path (see
        UserSignupCreateSerializer.validate()) never calls full_clean(), so
        it enforces the same rule independently rather than relying on this.
        """
        super().clean()
        country = (self.country or KENYA_COUNTRY_CODE).upper()
        if country == KENYA_COUNTRY_CODE:
            if not self.phone:
                raise DjangoValidationError({"phone": "Phone number is required for Kenyan accounts."})
            try:
                self.phone = normalize_kenyan_phone(self.phone)
            except InvalidKenyanPhone as exc:
                raise DjangoValidationError({"phone": str(exc)}) from exc

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
