"""
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

── Client change requests + disputes ─────────────────────────────────
Two models matching how a real marketplace (Upwork, etc.) treats a
signed-or-negotiating agreement: neither party can silently rewrite or
delete it.

  - ContractChangeRequest: the ONLY way a client can propose a change
    to negotiable terms. A request sits at "pending" until an admin
    approves (which applies it via the existing record_contract_revision
    — same audit trail negotiation already uses) or rejects it. There
    is no client-side edit/delete of the contract itself, on purpose.

  - ContractDispute: a client-filed dispute. It can be scoped two ways
    (see the model docstring below) — contract-wide (freezes all
    billing) or milestone-specific (freezes only that milestone's
    release, added below alongside the escrow rework).

── Milestone escrow rework (added) ────────────────────────────────────
Scape is a single-provider shop, not a multi-vendor marketplace — "the
provider" submitting work is always us, via the admin side of this
same API, never a separate onboarded third party. That's what makes
this simpler than actual Upwork: there's no payout/transfer step, no
provider payment-method verification, no marketplace take-rate. Money
that lands in Scape's Paystack account for a milestone IS Scape's
money the moment Paystack confirms it — "funded" vs "released" is a
bookkeeping/workflow distinction we maintain ourselves, not something
Paystack tracks, since Paystack has no marketplace-style hold-and-
release primitive.

The lifecycle a Milestone now moves through:

    pending → invoiced → funded → submitted → released
                                       ↕
                              revision_requested
                                       ↕
                                   disputed

  - pending: created, nothing sent to the client yet.
  - invoiced: a Paystack checkout has been generated; awaiting payment.
  - funded: Paystack confirmed payment (see contracts.signals). Money
    is in Scape's account, but bookkeeping treats it as held pending
    delivery — this replaces the old terminal "paid" status entirely.
  - submitted: a MilestoneDeliverable has been submitted for this
    funded milestone. submitted_at/auto_release_at start a clock —
    see is_past_auto_release_deadline(), consumed by a periodic task.
  - revision_requested: client asked for changes; provider resubmits,
    which returns the milestone to "submitted" with a new
    MilestoneDeliverable version.
  - released: client approved (or the auto-release deadline passed —
    see `auto_released`). Terminal success state. This is the actual
    "money earned" moment for reporting purposes, not "funded".
  - disputed: a milestone-scoped ContractDispute is open against this
    milestone specifically, freezing ITS release only — other
    milestones on the same contract are unaffected unless the dispute
    is contract-wide (see Contract.has_open_dispute vs
    Milestone.has_open_dispute).

Every transition is centralized through Milestone._transition(),
guarded by MILESTONE_ALLOWED_TRANSITIONS — the same explicit
allow-list pattern agreements.models.Agreement already uses for its
own status machine (see ALLOWED_STATUS_TRANSITIONS there). An illegal
jump (e.g. "pending" straight to "released") raises instead of
silently corrupting the ledger.

MilestoneDeliverable is the versioned record of what was actually
submitted each time (a milestone can have several, if revisions are
requested). MilestoneEvent is the append-only audit log of everything
that happens to a milestone — same "one append-only forensic log"
discipline as agreements.models.AgreementEvent.

── Required agreements gate (added) ───────────────────────────────────
Contract.required_agreement_types is the enterprise-grade guarantee
that a contract can't start moving money until its NDA/MSA (or
whatever's required) is fully executed on both sides. It's checked by
services.initiate_milestone_payment() the exact same way
has_open_dispute already is — one choke point, not a check duplicated
per view. See Contract.has_unsigned_required_agreements.

Deliberately NOT importing agreements.models here — Contract reads the
gate through the reverse `agreements` relation that
agreements.models.Agreement.contract's related_name already provides,
keeping the one-way FK direction the agreements app's own docstring
establishes.
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

# Mirrors agreements.models.AGREEMENT_TYPE_CHOICES values. Not a hard
# FK/import — Contract.required_agreement_types is a plain JSONField
# list of these strings, checked against the actual agreements app at
# query time via the reverse relation. Kept here only as documentation
# for whoever's populating this field from the admin or a serializer.
REQUIRED_AGREEMENT_TYPE_REFERENCE = ["nda", "msa", "dpa", "addendum", "other"]


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

    contract_document_url = models.URLField(
        max_length=500, blank=True,
        help_text="Final signed PDF — Supabase-hosted (see contracts/storage.py). Uploaded manually, or by a future e-sign integration.",
    )

    # ── Required agreements gate (added) ──────────────────────────────
    required_agreement_types = models.JSONField(
        default=list, blank=True,
        help_text="List of agreements.Agreement.agreement_type values (e.g. ['nda', 'msa']) that "
                   "must each have a fully-executed Agreement (see "
                   "agreements.models.Agreement.is_fully_executed) tied to this contract before ANY "
                   "milestone here can be invoiced. Empty list = no gate. Enforced by "
                   "services.initiate_milestone_payment(), the same choke point has_open_dispute uses.",
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
        invoice/checkout attempt — the single point where a CONTRACT-WIDE
        dispute freezes money movement across every milestone. Only counts
        disputes with milestone=None; a dispute scoped to one milestone
        (see ContractDispute.milestone) freezes just that milestone's
        release instead — see Milestone.has_open_dispute for that case.
        A property (not cached) so it's always correct even if a dispute
        was just resolved this request."""
        return self.disputes.filter(status__in=DISPUTE_STATUS_OPEN, milestone__isnull=True).exists()

    # ── Required agreements gate ──────────────────────────────────────
    @property
    def unsigned_required_agreement_types(self):
        """Which of required_agreement_types have no fully-executed
        Agreement yet, read via the reverse `agreements` relation (see
        module docstring for why this doesn't import agreements.models).
        Empty list means the gate is satisfied, or nothing is required."""
        if not self.required_agreement_types:
            return []
        executed_types = {a.agreement_type for a in self.agreements.all() if a.is_fully_executed}
        return [t for t in self.required_agreement_types if t not in executed_types]

    @property
    def has_unsigned_required_agreements(self):
        """Checked by services.initiate_milestone_payment() alongside
        has_open_dispute — a contract can't be billed against until every
        agreement type it requires is signed (and countersigned, if that
        template demands it) on both sides."""
        return bool(self.unsigned_required_agreement_types)

    def unbilled_milestones_up_to(self, order):
        """Every milestone on this contract with order <= the given value
        that's still 'pending' (i.e. never invoiced, or freed by a voided
        invoice) — the exact set services.create_invoice() auto-includes
        so an invoice for milestone N always also covers any earlier
        milestone that was skipped rather than paid in sequence."""
        return self.milestones.filter(order__lte=order, status="pending").order_by("order")


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


# ── Milestone escrow lifecycle ──────────────────────────────────────────

MILESTONE_STATUS_CHOICES = [
    ("pending", "Pending"),
    ("invoiced", "Invoiced"),
    ("funded", "Funded"),
    ("submitted", "Submitted"),
    ("revision_requested", "Revision requested"),
    ("released", "Released"),
    ("disputed", "Disputed"),
    ("waived", "Waived"),
    ("cancelled", "Cancelled"),
]

# Explicit allow-list of status transitions — same reasoning as
# agreements.models.ALLOWED_STATUS_TRANSITIONS: prevents an admin action,
# a stray webhook retry, or a race from moving a Milestone somewhere it
# has no business being. Enforced via can_transition_to(); actual
# mutation always happens through the guarded methods below.
MILESTONE_ALLOWED_TRANSITIONS = {
    "pending": {"invoiced", "waived", "cancelled"},
    # "pending" here (added) is ONLY reachable via Milestone.revert_to_pending(),
    # called exclusively by services.void_invoice() — voiding a bill must
    # free its milestones to be rebilled, not strand them "invoiced" forever
    # with no invoice attached.
    "invoiced": {"funded", "cancelled", "pending"},
    "funded": {"submitted", "disputed", "cancelled"},
    "submitted": {"revision_requested", "released", "disputed", "cancelled"},
    "revision_requested": {"submitted", "disputed", "cancelled"},
    "disputed": {"submitted", "revision_requested", "released", "cancelled"},
    "released": set(),   # terminal
    "waived": set(),     # terminal
    "cancelled": set(),  # terminal
}

DEFAULT_AUTO_RELEASE_DAYS = 7


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
    # See module docstring / payments/models.py's own reasoning. Legacy
    # single-milestone billing path (MilestoneInvoiceView) still uses this;
    # any milestone billed through the newer Invoice model (below) leaves
    # this blank and uses invoice.paystack_reference instead — Invoice is
    # the payment record once one exists for this milestone.
    paystack_reference = models.CharField(
        max_length=100, blank=True, db_index=True,
        help_text="Set once a PaystackTransaction has been initialized for this milestone.",
    )
    invoiced_at = models.DateTimeField(null=True, blank=True)

    # A milestone can be on at most one non-void Invoice at a time —
    # enforced in services.create_invoice(), not at the DB level, since
    # "non-void" is a status condition, not a uniqueness constraint SQL
    # can express directly.
    invoice = models.ForeignKey(
        "Invoice", on_delete=models.SET_NULL, null=True, blank=True, related_name="milestones",
    )

    # Money captured by Paystack — the old "paid_at" renamed to reflect
    # that this is now a HELD state, not the end of the story.
    funded_at = models.DateTimeField(null=True, blank=True)

    # ── Submission / review clock ─────────────────────────────────────
    submitted_at = models.DateTimeField(null=True, blank=True)
    auto_release_days = models.PositiveIntegerField(
        default=DEFAULT_AUTO_RELEASE_DAYS,
        help_text="Days after submission before this milestone auto-releases if the client takes no action.",
    )
    auto_release_at = models.DateTimeField(
        null=True, blank=True,
        help_text="submitted_at + auto_release_days. Cleared whenever a revision is requested, since the "
                   "clock restarts on the next submission.",
    )

    # ── Release ────────────────────────────────────────────────────────
    released_at = models.DateTimeField(null=True, blank=True)
    auto_released = models.BooleanField(
        default=False, help_text="True if release() fired via the auto-release deadline rather than an explicit client approval.",
    )
    released_by_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="milestones_released",
        help_text="The client who explicitly approved release. Blank for auto-released or token-link clients.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["contract", "order", "created_at"]

    def __str__(self):
        return f"{self.title} — {self.amount} {self.currency} ({self.status})"

    # ── Guarded transition machinery ──────────────────────────────────
    def can_transition_to(self, new_status):
        return new_status in MILESTONE_ALLOWED_TRANSITIONS.get(self.status, set())

    def _transition(self, new_status, *, extra_fields=None):
        """Central guarded status mutation. Every method below calls this
        instead of setting self.status directly, so an illegal jump (e.g.
        'pending' straight to 'released') raises loudly instead of
        silently corrupting the ledger. Mirrors agreements.models.
        Agreement's ALLOWED_STATUS_TRANSITIONS/can_transition_to pattern."""
        if not self.can_transition_to(new_status):
            raise ValueError(f"Milestone {self.id}: cannot move '{self.status}' -> '{new_status}'.")
        self.status = new_status
        self.save(update_fields=["status", *(extra_fields or [])])

    # ── Invoicing / funding ────────────────────────────────────────────
    def mark_invoiced(self, reference):
        if self.status != "pending":
            return False
        self.paystack_reference = reference
        self.invoiced_at = timezone.now()
        self._transition("invoiced", extra_fields=["paystack_reference", "invoiced_at"])
        return True

    def attach_to_invoice(self, invoice):
        """New Invoice-based billing path — parallel to mark_invoiced()
        but the payment reference lives on Invoice, not here (see
        Invoice.paystack_reference). Called only by
        services.create_invoice(). revert_to_pending() below undoes
        exactly this pairing if the invoice is later voided."""
        if self.status != "pending":
            return False
        self.invoice = invoice
        self.invoiced_at = timezone.now()
        self._transition("invoiced", extra_fields=["invoice", "invoiced_at"])
        return True

    def revert_to_pending(self):
        """Called only by services.void_invoice() — undoes mark_invoiced()
        so a milestone freed from a voided invoice can be billed again."""
        if self.status != "invoiced":
            return False
        self.invoice = None
        self.invoiced_at = None
        self._transition("pending", extra_fields=["invoice", "invoiced_at"])
        return True

    def mark_funded(self, *, funded_at=None):
        """Called by contracts.signals when payments confirms the
        PaystackTransaction tied to this milestone. Idempotent against a
        webhook/manual-verify race — a second call once already funded
        (or once further along) is a no-op, not an error, same reasoning
        the old mark_paid() this replaces used."""
        if self.status != "invoiced":
            return False
        self.funded_at = funded_at or timezone.now()
        self._transition("funded", extra_fields=["funded_at"])
        return True

    # ── Deliverable submission / review ────────────────────────────────
    def submit(self, *, auto_release_days=None):
        """Moves funded (or revision_requested, i.e. a resubmission) into
        submitted, and starts the auto-release clock. The actual
        MilestoneDeliverable row is created by services.
        submit_milestone_deliverable(), which calls this — never call
        this directly from a view."""
        if self.status not in ("funded", "revision_requested"):
            return False
        if auto_release_days:
            self.auto_release_days = auto_release_days
        self.submitted_at = timezone.now()
        self.auto_release_at = self.submitted_at + timedelta(days=self.auto_release_days)
        self._transition(
            "submitted", extra_fields=["submitted_at", "auto_release_at", "auto_release_days"],
        )
        return True

    def request_revision(self):
        """Client asks for changes. Clears the auto-release clock — it
        restarts fresh on the next submit()."""
        if self.status != "submitted":
            return False
        self.auto_release_at = None
        self._transition("revision_requested", extra_fields=["auto_release_at"])
        return True

    def release(self, *, released_by_user=None, auto=False):
        """The actual 'funds recognized as earned' moment. Callable from
        submitted, revision_requested (client changes their mind and
        approves anyway), or disputed (dispute resolved in Scape's favor).
        auto=True marks this as fired by the deadline rather than an
        explicit approval — see is_past_auto_release_deadline()."""
        if self.status not in ("submitted", "revision_requested", "disputed"):
            return False
        self.released_at = timezone.now()
        self.auto_released = auto
        self.released_by_user = released_by_user
        self._transition(
            "released", extra_fields=["released_at", "auto_released", "released_by_user"],
        )
        return True

    def is_past_auto_release_deadline(self):
        """Consumed by a periodic task (celery beat) that sweeps
        submitted milestones and auto-releases the ones the client never
        acted on. Not enforced here — this file has no business running
        a scheduler — just exposes the check services/tasks call."""
        return (
            self.status == "submitted"
            and self.auto_release_at is not None
            and timezone.now() >= self.auto_release_at
        )

    # ── Disputes (milestone-scoped) ─────────────────────────────────────
    @property
    def has_open_dispute(self):
        """True if EITHER this specific milestone has an open
        milestone-scoped dispute, OR the whole contract is under a
        contract-wide freeze (Contract.has_open_dispute). Checked by
        services before allowing submit()/release() on this milestone."""
        if self.contract.has_open_dispute:
            return True
        return self.disputes.filter(status__in=DISPUTE_STATUS_OPEN).exists()

    def mark_disputed(self):
        if self.status not in ("funded", "submitted", "revision_requested"):
            return False
        self._transition("disputed")
        return True

    def resolve_dispute(self, *, new_status, released_by_user=None):
        """new_status is one of the states disputed can resolve back
        into: 'submitted' (unresolved, back under review), 'revision_requested',
        'released' (resolved in Scape's favor), or 'cancelled'."""
        if self.status != "disputed":
            return False
        if new_status == "released":
            return self.release(released_by_user=released_by_user)
        self._transition(new_status)
        return True

    # ── Terminal non-payment states ─────────────────────────────────────
    def waive(self):
        if self.status not in ("pending", "invoiced"):
            return False
        self._transition("waived")
        return True

    def cancel(self):
        if self.status in ("released", "waived", "cancelled"):
            return False
        self._transition("cancelled")
        return True

    @property
    def latest_deliverable(self):
        return self.deliverables.order_by("-version").first()


class MilestoneDeliverable(models.Model):
    """
    One versioned submission of work against a funded Milestone. A
    milestone can have several of these over its life (an initial
    submission, then another after request_revision()) — version
    increments each time, so the client-facing history reads as an
    actual submission log rather than the latest state silently
    overwriting the last.

    Created exclusively by services.submit_milestone_deliverable() —
    never a bare .create() in a view — so the paired MilestoneEvent row
    and the Milestone.submit() transition always happen together and
    can't drift apart.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    milestone = models.ForeignKey(Milestone, on_delete=models.CASCADE, related_name="deliverables")
    version = models.PositiveIntegerField(
        help_text="1 for the first submission, incremented on each resubmission after a revision request.",
    )

    summary = models.TextField(help_text="What was delivered — shown to the client for review.")
    file_url = models.URLField(max_length=500, blank=True)
    file_filename = models.CharField(max_length=255, blank=True)
    link_url = models.URLField(
        blank=True, help_text="External link (repo, staging URL, design file, etc.) in place of or alongside a file.",
    )

    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="milestone_deliverables_submitted",
        help_text="Always Scape's side — this is a single-provider shop, not a marketplace with third-party providers.",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["milestone", "version"]
        constraints = [
            models.UniqueConstraint(fields=["milestone", "version"], name="unique_deliverable_version_per_milestone"),
        ]

    def __str__(self):
        return f"{self.milestone_id} v{self.version}"


# ── Milestone audit log ─────────────────────────────────────────────────

MILESTONE_EVENT_TYPE_CHOICES = [
    ("created", "Created"),
    ("invoiced", "Invoiced"),
    ("funded", "Funded"),
    ("submitted", "Submitted"),
    ("resubmitted", "Resubmitted"),
    ("revision_requested", "Revision requested"),
    ("released", "Released"),
    ("auto_released", "Auto-released"),
    ("disputed", "Disputed"),
    ("dispute_resolved", "Dispute resolved"),
    ("waived", "Waived"),
    ("cancelled", "Cancelled"),
]

MILESTONE_EVENT_ACTOR_CHOICES = [
    ("admin", "Admin"),
    ("client", "Client"),
    ("system", "System"),
]


class MilestoneEvent(models.Model):
    """
    Append-only forensic log — never updated, never deleted. Same "one
    place creates it, nothing else touches it" discipline as
    agreements.models.AgreementEvent: written exclusively by the
    services-layer functions that also perform the matching Milestone
    transition, so this log can be trusted as a complete history when a
    client later disputes what happened and when.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    milestone = models.ForeignKey(Milestone, on_delete=models.CASCADE, related_name="events")

    event_type = models.CharField(max_length=30, choices=MILESTONE_EVENT_TYPE_CHOICES, db_index=True)
    actor_type = models.CharField(max_length=10, choices=MILESTONE_EVENT_ACTOR_CHOICES, default="system")
    actor_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="milestone_events",
    )
    actor_name = models.CharField(
        max_length=150, blank=True,
        help_text="Display name — set for a token-link client with no account, same reasoning as "
                  "contracts.models.Message.sender_name.",
    )

    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True, help_text="Flexible extra context, varies by event_type.")

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]  # chronological — reads top-to-bottom like an actual audit trail
        indexes = [
            models.Index(fields=["milestone", "event_type"]),
            models.Index(fields=["milestone", "created_at"]),
        ]

    def __str__(self):
        return f"{self.milestone_id} [{self.event_type}] {self.created_at:%Y-%m-%d %H:%M}"


INVOICE_STATUS_CHOICES = [
    ("draft", "Draft"),
    ("sent", "Sent"),
    ("paid", "Paid"),
    ("void", "Void"),
]

PAYMENT_METHOD_CHOICES = [
    ("paystack", "Paystack (in-platform)"),
    ("bank_transfer", "Bank transfer"),
    ("mpesa_paybill", "M-Pesa Paybill"),
    ("payoneer", "Payoneer"),
    ("paypal", "PayPal"),
    ("wise", "Wise"),
    ("remitly", "Remitly"),
    ("sendwave", "Sendwave"),
    ("crypto", "Crypto"),
    ("other", "Other"),
]


class InvoiceCounter(models.Model):
    """One row per calendar year — the sequence source for human-readable
    invoice numbers ("SCP-2026-000123"). A dedicated row (rather than
    Invoice.objects.count()+1) makes numbering race-safe under
    select_for_update() and immune to gaps from deleted rows."""
    year = models.PositiveIntegerField(unique=True)
    last_number = models.PositiveIntegerField(default=0)

    def __str__(self):
        return f"{self.year}: last #{self.last_number}"


class Invoice(models.Model):
    """
    A real billing document — distinct from the legacy per-milestone
    "invoice" (Milestone.mark_invoiced(), which really just meant
    "checkout started"). This is the client-facing PDF: one or more
    milestones, a fixed total frozen at creation, a due date, and an
    audit trail of how it was sent and how it was paid.

    Sequencing rule (see Contract.unbilled_milestones_up_to and
    services.create_invoice): every Invoice, once created, is always a
    complete, self-contained bill relative to its own highest-order
    milestone — any earlier unbilled milestone on the same contract is
    automatically folded in at creation time, never left for a payment
    step to discover. Paying an invoice is therefore always all-or-
    nothing across everything it lists.

    Exactly one non-void Invoice may be outstanding (draft/sent) per
    contract at a time — enforced in services.create_invoice(), not
    here, to keep the "why" next to the other billing-freeze checks
    (has_open_dispute, has_unsigned_required_agreements).
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    contract = models.ForeignKey(Contract, on_delete=models.CASCADE, related_name="invoices")

    invoice_number = models.CharField(max_length=30, unique=True, editable=False)

    # Frozen at creation from the sum of its milestones — an invoice
    # already sent to a client must never silently change total even if
    # a milestone amount is edited afterward.
    total_amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3, default="KES")

    status = models.CharField(max_length=10, choices=INVOICE_STATUS_CHOICES, default="draft", db_index=True)
    notes = models.TextField(blank=True)

    issued_at = models.DateTimeField(auto_now_add=True)
    due_at = models.DateTimeField()

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="invoices_created",
    )

    # ── Sending ──────────────────────────────────────────────────────
    sent_at = models.DateTimeField(null=True, blank=True)
    sent_via_email = models.BooleanField(default=False)
    sent_via_message = models.BooleanField(default=False)

    pdf_url = models.URLField(max_length=500, blank=True)

    # ── In-platform payment (Paystack) ──────────────────────────────
    paystack_reference = models.CharField(max_length=100, blank=True, db_index=True)

    # ── Settlement (either path — in-platform or manual) ────────────
    paid_at = models.DateTimeField(null=True, blank=True)
    payment_method = models.CharField(max_length=20, choices=PAYMENT_METHOD_CHOICES, blank=True)
    is_manual_payment = models.BooleanField(default=False)
    manual_payment_reference = models.CharField(
        max_length=150, blank=True, help_text="Transaction ID / receipt number the client provided, if paid outside the platform.",
    )
    manual_payment_notes = models.TextField(blank=True)
    manual_payment_recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="invoices_recorded",
    )

    void_reason = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["contract", "status"])]

    def __str__(self):
        return f"{self.invoice_number} — {self.contract.title} ({self.status})"

    @property
    def is_overdue(self):
        return self.status == "sent" and timezone.now() > self.due_at


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
    A client-filed dispute. Scoped one of two ways, chosen by whether
    `milestone` is set:

      - milestone=None (contract-wide): Contract.has_open_dispute is
        True while status is "open"/"under_review", freezing ALL
        milestone invoicing across the entire contract (see
        services.initiate_milestone_payment) — the original, broadest
        freeze. Use this for e.g. a dispute about the relationship
        itself, not one specific deliverable.

      - milestone=<Milestone> (milestone-scoped): only THAT milestone's
        Milestone.has_open_dispute is True, and only ITS submit()/
        release() are frozen — a client disputing milestone 2's
        deliverable doesn't stop milestone 3 from being funded and
        worked on in parallel.

    Written by services.raise_dispute() / services.resolve_dispute(),
    never edited directly. Resolving a milestone-scoped dispute calls
    Milestone.resolve_dispute() to move that milestone out of
    "disputed"; resolving a contract-wide one just lifts the freeze,
    since Contract.has_open_dispute re-evaluates live on every call.
    """
    contract = models.ForeignKey(Contract, on_delete=models.CASCADE, related_name="disputes")
    milestone = models.ForeignKey(
        Milestone, on_delete=models.SET_NULL, null=True, blank=True, related_name="disputes",
        help_text="If set, this dispute is scoped to one milestone's release only. If null, it's "
                   "contract-wide and freezes ALL milestone invoicing on the contract.",
    )

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
        scope = f"milestone {self.milestone_id}" if self.milestone_id else "contract-wide"
        return f"{self.contract_id} dispute ({self.status}, {scope})"

    def is_open(self):
        return self.status in DISPUTE_STATUS_OPEN