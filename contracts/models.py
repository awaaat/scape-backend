"""
contracts/models.py

Covers the whole post-lead lifecycle: a won deal becomes a Contract,
gets negotiated (ContractRevision snapshots + Message thread), gets
signed, and gets paid out in pieces (Milestone).

Client access works two ways at once, deliberately:
  - client_user: set once the client has a real Scape login.
  - access_token_hash: a hashed, expiring token (same pattern as
    users.UserSignup's email-verification token) so a client with no
    account can still open a secure link and negotiate/sign/pay.
Either one is enough — see Contract.has_client_access().

Payments reuse payments.PaystackTransaction exactly the way
property_intel does: Milestone stores only the opaque `reference`
string, never a FK into payments — that app has zero knowledge of its
callers by design (see payments/models.py), and this keeps it that way.

── Client change requests + disputes (added) ────────────────────────
Two new models, matching how a real marketplace (Upwork, etc.) treats
a signed-or-negotiating agreement: neither party can silently rewrite
or delete it.

  - ContractChangeRequest: the ONLY way a client can propose a change
    to negotiable terms. A request sits at "pending" until an admin
    approves (which applies it via the existing record_contract_revision
    — same audit trail negotiation already uses) or rejects it. There
    is no client-side edit/delete of the contract itself, on purpose.

  - ContractDispute: a client-filed dispute against the whole contract.
    While one is open (status "open" or "under_review"),
    Contract.has_open_dispute is True, which
    services.initiate_milestone_payment() checks and refuses to start
    or resume ANY milestone invoice/checkout for that contract —
    billing is frozen contract-wide until an admin resolves it. This
    is the one behavioral change to existing milestone-payment code;
    everything else in this file is additive.
"""
import hashlib
import hmac
import secrets
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

from leads.models import Lead

ACCESS_TOKEN_VALID_DAYS = 30

CONTRACT_STATUS_CHOICES = [
    ("draft", "Draft"),
    ("sent", "Sent"),
    ("negotiating", "Negotiating"),
    ("signed", "Signed"),
    ("active", "Active"),
    ("completed", "Completed"),
    ("cancelled", "Cancelled"),
]

ESIGN_METHOD_CHOICES = [
    ("self_built", "Self-built"),
    ("third_party", "Third-party"),
]

# Statuses in which a client is allowed to propose a change to
# negotiable terms — mirrors the states record_contract_revision
# already treats as "still being negotiated." Once signed, a change
# needs a real addendum/new contract, not a silent edit.
CHANGE_REQUEST_ELIGIBLE_STATUSES = {"sent", "negotiating"}


class Contract(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    lead = models.ForeignKey(
        Lead, on_delete=models.SET_NULL, null=True, blank=True, related_name="contracts",
        help_text="The inquiry this deal originated from, if any.",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="contracts_created",
    )

    # ── Client identity — always denormalized here so the contract is
    # self-contained even before/without a client account. ──────────────
    client_name = models.CharField(max_length=150)
    client_email = models.EmailField(db_index=True)
    client_company = models.CharField(max_length=150, blank=True)

    # Set once the client has a real login. Optional — access_token_hash
    # below covers clients who never create an account.
    client_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="client_contracts",
    )

    title = models.CharField(max_length=255)
    scope_of_work = models.TextField(blank=True)
    total_value = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3, default="KES")

    status = models.CharField(max_length=20, choices=CONTRACT_STATUS_CHOICES, default="draft", db_index=True)

    # ── No-login client access — hashed, same reasoning as
    # users.UserSignup.email_verification_token_hash: a leaked DB backup
    # should never let anyone open someone else's contract. ─────────────
    access_token_hash = models.CharField(max_length=64, blank=True)
    access_token_expires_at = models.DateTimeField(null=True, blank=True)

    # ── E-signature — self-built now, third-party-shaped from day one so
    # swapping in a DocuSign-style provider later is additive, not a
    # migration. ──────────────────────────────────────────────────────
    esign_method = models.CharField(max_length=20, choices=ESIGN_METHOD_CHOICES, default="self_built")
    esign_provider = models.CharField(
        max_length=50, blank=True, help_text="Set once a third-party e-sign provider is wired in, e.g. 'docusign'.",
    )
    esign_envelope_id = models.CharField(max_length=100, blank=True)

    sent_at = models.DateTimeField(null=True, blank=True)

    signed_at = models.DateTimeField(null=True, blank=True)
    signed_by_name = models.CharField(max_length=150, blank=True)
    signed_by_email = models.EmailField(blank=True)
    signed_ip = models.GenericIPAddressField(null=True, blank=True)
    signed_user_agent = models.TextField(blank=True)
    signed_content_hash = models.CharField(
        max_length=64, blank=True,
        help_text="SHA-256 of title+scope+value at the moment of signing — if the terms are ever "
                   "edited afterward, this stops matching, which is the tamper signal.",
    )

    contract_document = models.FileField(
        upload_to="contracts/signed/%Y/%m/", null=True, blank=True,
        help_text="Final signed PDF — uploaded manually, or by a future e-sign integration.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["client_email", "status"]),
            models.Index(fields=["status", "created_at"]),
        ]

    def __str__(self):
        return f"{self.title} — {self.client_name} ({self.status})"

    # ── No-login access token ────────────────────────────────────────
    def generate_access_token(self):
        """Returns the RAW token (caller emails/links this) while only the
        hash is persisted. Overwrites any previous token — most-recent-wins,
        same behavior as UserSignup.generate_verification_token()."""
        raw_token = secrets.token_urlsafe(32)
        self.access_token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        self.access_token_expires_at = timezone.now() + timedelta(days=ACCESS_TOKEN_VALID_DAYS)
        self.save(update_fields=["access_token_hash", "access_token_expires_at"])
        return raw_token

    def verify_access_token(self, raw_token):
        """Constant-time comparison against the stored hash, expiry-checked."""
        if not raw_token or not self.access_token_hash or not self.access_token_expires_at:
            return False
        if timezone.now() >= self.access_token_expires_at:
            return False
        candidate_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        return hmac.compare_digest(self.access_token_hash, candidate_hash)

    def has_client_access(self, *, user=None, token=None):
        """True if EITHER the logged-in user is this contract's client OR
        the supplied token is valid — either path is sufficient."""
        if user is not None and getattr(user, "is_authenticated", False) and self.client_user_id == user.id:
            return True
        if token:
            return self.verify_access_token(token)
        return False

    # ── Terms integrity / signing ────────────────────────────────────
    def content_snapshot(self):
        return f"{self.title}\n{self.scope_of_work}\n{self.total_value}\n{self.currency}"

    def content_hash(self):
        return hashlib.sha256(self.content_snapshot().encode("utf-8")).hexdigest()

    def record_signature(self, *, name, email, ip_address="", user_agent=""):
        self.signed_by_name = name
        self.signed_by_email = email
        self.signed_ip = ip_address or None
        self.signed_user_agent = user_agent or ""
        self.signed_content_hash = self.content_hash()
        self.signed_at = timezone.now()
        self.status = "signed"
        self.save(update_fields=[
            "signed_by_name", "signed_by_email", "signed_ip", "signed_user_agent",
            "signed_content_hash", "signed_at", "status",
        ])

    def mark_sent(self):
        if self.status == "draft":
            self.status = "sent"
        self.sent_at = timezone.now()
        self.save(update_fields=["status", "sent_at"])

    def is_change_request_eligible(self):
        return self.status in CHANGE_REQUEST_ELIGIBLE_STATUSES

    @property
    def has_open_dispute(self):
        """Checked by services.initiate_milestone_payment() before every
        invoice/checkout attempt — the single point where a dispute
        actually freezes money movement. A property (not cached) so it's
        always correct even if a dispute was just resolved this request."""
        return self.disputes.filter(status__in=["open", "under_review"]).exists()


class ContractRevision(models.Model):
    """
    A snapshot of the negotiable terms, logged every time an admin edits
    them before signing. Pure audit trail — the live values always live
    on Contract itself; this is "what did version N say, and why did it
    change," useful if a client ever disputes what was agreed.
    """
    contract = models.ForeignKey(Contract, on_delete=models.CASCADE, related_name="revisions")
    version_number = models.PositiveIntegerField()

    title = models.CharField(max_length=255)
    scope_of_work = models.TextField(blank=True)
    total_value = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3)

    edited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="contract_revisions",
    )
    note = models.TextField(blank=True, help_text="What changed and why — shown to the client in the thread.")

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["contract", "version_number"]
        constraints = [
            models.UniqueConstraint(fields=["contract", "version_number"], name="unique_revision_per_contract"),
        ]

    def __str__(self):
        return f"{self.contract_id} v{self.version_number}"


MILESTONE_STATUS_CHOICES = [
    ("pending", "Pending"),
    ("invoiced", "Invoiced"),
    ("paid", "Paid"),
    ("waived", "Waived"),
    ("cancelled", "Cancelled"),
]


class Milestone(models.Model):
    contract = models.ForeignKey(Contract, on_delete=models.CASCADE, related_name="milestones")

    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3, default="KES")
    order = models.PositiveIntegerField(default=0)
    due_date = models.DateField(null=True, blank=True)

    status = models.CharField(max_length=20, choices=MILESTONE_STATUS_CHOICES, default="pending", db_index=True)

    # Opaque reference into payments.PaystackTransaction — never a FK.
    # See module docstring / payments/models.py's own reasoning.
    paystack_reference = models.CharField(
        max_length=100, blank=True, db_index=True,
        help_text="Set once a PaystackTransaction has been initialized for this milestone.",
    )
    invoiced_at = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["contract", "order", "created_at"]

    def __str__(self):
        return f"{self.title} — {self.amount} {self.currency} ({self.status})"

    def mark_invoiced(self, reference):
        if self.status not in ("pending",):
            return
        self.status = "invoiced"
        self.paystack_reference = reference
        self.invoiced_at = timezone.now()
        self.save(update_fields=["status", "paystack_reference", "invoiced_at"])

    def mark_paid(self, *, paid_at=None):
        """Guarded the same way payments.services.confirm_transaction() is:
        safe to call twice (webhook + manual verify racing) — a second call
        against an already-paid milestone is a no-op, not an error."""
        if self.status == "paid":
            return False
        self.status = "paid"
        self.paid_at = paid_at or timezone.now()
        self.save(update_fields=["status", "paid_at"])
        return True


class Message(models.Model):
    """A single message in the admin<->client thread for a contract."""
    SENDER_CHOICES = [("admin", "Admin"), ("client", "Client")]

    contract = models.ForeignKey(Contract, on_delete=models.CASCADE, related_name="messages")
    sender_type = models.CharField(max_length=10, choices=SENDER_CHOICES, db_index=True)
    sender_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="contract_messages",
        help_text="Set for an authenticated admin, or a client with a linked account. "
                  "Blank for a client messaging via an access-token link — sender_name covers that case.",
    )
    sender_name = models.CharField(
        max_length=150, blank=True,
        help_text="Display name. Always set for token-link clients (no account to pull a name from).",
    )
    body = models.TextField()

    created_at = models.DateTimeField(auto_now_add=True)
    read_by_admin_at = models.DateTimeField(null=True, blank=True)
    read_by_client_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"[{self.sender_type}] {self.contract_id}: {self.body[:40]}"

    def mark_read_by_admin(self):
        if not self.read_by_admin_at:
            self.read_by_admin_at = timezone.now()
            self.save(update_fields=["read_by_admin_at"])

    def mark_read_by_client(self):
        if not self.read_by_client_at:
            self.read_by_client_at = timezone.now()
            self.save(update_fields=["read_by_client_at"])


# ── Client change requests ─────────────────────────────────────────────

CHANGE_REQUEST_STATUS_CHOICES = [
    ("pending", "Pending"),
    ("approved", "Approved"),
    ("rejected", "Rejected"),
    ("withdrawn", "Withdrawn"),
]


class ContractChangeRequest(models.Model):
    """
    THE only way a client can propose a change to a contract's
    negotiable terms — never a direct edit. Written by
    services.request_contract_change(); decided by
    services.decide_change_request(), which — if approved — applies the
    change through the EXISTING record_contract_revision() so it lands
    in the same ContractRevision audit trail an admin-initiated edit
    would, indistinguishable in history except for edited_by.
    """
    contract = models.ForeignKey(Contract, on_delete=models.CASCADE, related_name="change_requests")

    # Same dual-identity pattern as Message.sender_user/sender_name —
    # covers a client with a real account AND a no-login token client.
    requested_by_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="contract_change_requests",
    )
    requested_by_name = models.CharField(max_length=150, blank=True)

    # Only the fields actually being proposed are set — null means "no
    # change requested to this field." Same nullable-partial-update
    # shape as ContractTermsUpdateSerializer already uses for admin edits.
    proposed_title = models.CharField(max_length=255, blank=True)
    proposed_scope_of_work = models.TextField(blank=True)
    proposed_total_value = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    proposed_currency = models.CharField(max_length=3, blank=True)

    reason = models.TextField(help_text="Client's explanation for the requested change.")

    status = models.CharField(max_length=20, choices=CHANGE_REQUEST_STATUS_CHOICES, default="pending", db_index=True)
    admin_note = models.TextField(blank=True, help_text="Set by the admin when approving or rejecting.")
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="contract_change_requests_reviewed",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.contract_id} change request ({self.status})"


# ── Client disputes ─────────────────────────────────────────────────────

DISPUTE_STATUS_CHOICES = [
    ("open", "Open"),
    ("under_review", "Under review"),
    ("resolved_client_favor", "Resolved — client favor"),
    ("resolved_provider_favor", "Resolved — provider favor"),
    ("resolved_mutual", "Resolved — mutual agreement"),
    ("withdrawn", "Withdrawn"),
]

DISPUTE_STATUS_OPEN = {"open", "under_review"}

DISPUTE_CATEGORY_CHOICES = [
    ("deliverable_not_as_described", "Deliverable not as described"),
    ("missed_deadline", "Missed deadline"),
    ("payment_issue", "Payment issue"),
    ("communication_issue", "Communication issue"),
    ("other", "Other"),
]


class ContractDispute(models.Model):
    """
    A client-filed dispute against the whole contract. While status is
    "open" or "under_review", Contract.has_open_dispute is True, which
    freezes ALL milestone invoicing/checkout on this contract (see
    services.initiate_milestone_payment) until an admin resolves it —
    the actual "billing freeze" behavior, enforced in one place.
    Written by services.raise_dispute() / services.resolve_dispute(),
    never edited directly.
    """
    contract = models.ForeignKey(Contract, on_delete=models.CASCADE, related_name="disputes")

    raised_by_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="contract_disputes",
    )
    raised_by_name = models.CharField(max_length=150, blank=True)

    category = models.CharField(max_length=40, choices=DISPUTE_CATEGORY_CHOICES, default="other")
    description = models.TextField()

    status = models.CharField(max_length=30, choices=DISPUTE_STATUS_CHOICES, default="open", db_index=True)
    resolution_note = models.TextField(blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="contract_disputes_resolved",
    )
    resolved_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "created_at"]),
        ]

    def __str__(self):
        return f"{self.contract_id} dispute ({self.status})"

    def is_open(self):
        return self.status in DISPUTE_STATUS_OPEN
