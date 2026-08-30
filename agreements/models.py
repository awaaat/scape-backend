"""
agreements/models.py

Standalone e-sign + legal-document layer, sitting alongside (not inside)
`contracts`. Exists for one reason: a signed relationship with a client
is rarely a single document. Before a paid engagement even exists there
may be an NDA. Once it does, there's often a Master Services Agreement /
rules-of-engagement doc governing how the two parties work together,
separate from the commercial terms `contracts.Contract` already owns.
Both need the same rigor a "real" e-sign platform gives you — versioned
content, tamper-evident hashing, a full forensic trail of who saw/signed
what and from where — without being welded into `Contract`'s shape,
which is a *commercial* agreement (title/scope/value/currency) and
shouldn't be forced to represent something that isn't one.

── Relationship to the rest of the codebase ─────────────────────────
`Agreement` can optionally point at a Lead, a Project, and/or a
Contract — all THREE are nullable, one-way FKs using STRING references
("leads.Lead", "projects.Project", "contracts.Contract"), the same
late-binding convention `projects.models.Project` already uses for its
own `contract` FK. This app imports nothing from theirs; none of them
import anything from this app. An NDA can exist before a Lead is even
qualified, or attach to nothing at all (a standalone company-wide NDA
with a vendor, say) — hence nullable everywhere, never required.

── Template versioning — the legally load-bearing design decision ───
Clause text changes over time (a lawyer redlines the confidentiality
term, governing law changes, whatever). If Client A signed under v1
wording, that MUST keep reading as v1 forever, even after v2 exists.
So: `AgreementTemplate` rows are versioned and, by convention, treated
as immutable content once created — editing `body_html` in place after
a template has ever been used to send an Agreement is a process
violation, not just bad practice. Need new wording? Create a new
version (services.clone_template_version, added in the services-layer
pass). This file can't enforce that with a DB constraint, but
`Agreement.template` uses on_delete=PROTECT specifically so a template
row that's ever been used can never be deleted out from under a
historical, possibly-signed Agreement — retire it via `is_active=False`
instead, never delete it.

Every `Agreement` snapshots the fully-rendered content
(`rendered_content_html`) at send time. After that, the Agreement's
copy is the only copy that matters — even if the source template is
later edited (violating the convention above) or deleted (impossible,
per PROTECT), the signed record still shows exactly what the client
actually read and signed.

── Security pattern — deliberately mirrors contracts.Contract ───────
No-login client access (hashed, expiring token OR a linked user
account — see has_client_access), tamper-evident SHA-256 hashing of
signed content, IP/user-agent capture at signature time. This is a
DELIBERATE duplication of `Contract`'s pattern rather than a shared
mixin — `contracts` is a live, working app and this is new, so the
safer move is to implement it fresh here and extract a shared base
later once both have proven out in production, not risk touching
`contracts` to accommodate this. Flagged here as a known, intentional
follow-up, not an oversight.

── What a real e-sign platform gives you that a "just add a signed_at
field" version doesn't ────────────────────────────────────────────────
  - AgreementEvent: an append-only log of every meaningful thing that
    happened to an Agreement (sent, viewed, signature started, signed,
    declined, voided, reminder sent, resent, countersigned) — this is
    what becomes a DocuSign-style "Certificate of Completion" PDF
    later. Never edited or deleted, ever.
  - Decline, not just sign — a client can explicitly refuse.
  - Void — an admin can invalidate a sent-or-even-signed agreement
    (wrong entity name, superseded by a new version, etc.), with a
    mandatory reason, because "just delete the row" is never the
    correct move for something that may have already been legally
    relied upon.
  - Countersignature — a real NDA/MSA is executed by BOTH sides. A
    client signing alone is not "fully executed" if the template
    requires a company signatory too (see `is_fully_executed`).
  - Hard expiry — a business deadline to sign by (`expires_at`),
    distinct from `access_token_expires_at` (the technical link
    expiry, which a resend can always refresh independent of the
    business deadline).
"""
import hashlib
import hmac
import secrets
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

DEFAULT_ACCESS_TOKEN_VALID_DAYS = getattr(settings, "AGREEMENTS_ACCESS_TOKEN_VALID_DAYS", 30)
DEFAULT_SIGNING_DEADLINE_DAYS = getattr(settings, "AGREEMENTS_DEFAULT_SIGNING_DEADLINE_DAYS", 14)


# ── Template ──────────────────────────────────────────────────────────

AGREEMENT_TYPE_CHOICES = [
    ("nda", "Non-Disclosure Agreement"),
    ("msa", "Master Services Agreement / Rules of Engagement"),
    ("dpa", "Data Processing Agreement"),
    ("addendum", "Addendum / Amendment"),
    ("other", "Other"),
]

NDA_DIRECTION_CHOICES = [
    ("mutual", "Mutual (both parties disclose)"),
    ("disclosing_only", "One-way — we disclose to them"),
    ("receiving_only", "One-way — they disclose to us"),
]


class AgreementTemplate(models.Model):
    """
    A versioned piece of reusable legal-document content. `family_key`
    groups versions together (e.g. every version of "Standard Mutual
    NDA" shares one family_key; `version` increments within it) —
    `AgreementTemplate.objects.filter(family_key=X).order_by("-version")`
    is the version history for that document family.

    `body_html` uses `{{ variable }}` merge-field placeholders, rendered
    at send time (see agreements/pdf.py, added in the rendering pass) —
    never rendered or re-rendered after that point; see module
    docstring on why edits-in-place are a process violation.

    `is_latest` is a denormalized convenience flag (not derived via a
    property) so "which template do I offer by default when creating a
    new NDA" is a cheap indexed lookup instead of a per-family MAX()
    query on every template picker render. Maintained explicitly by
    the services layer when a new version is cloned — never flipped by
    a bare .save() here, consistent with how this codebase treats every
    other piece of derived state (see contracts.services.
    record_contract_revision's next_version calculation for the same
    reasoning applied to a different model).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    family_key = models.SlugField(
        max_length=100, db_index=True,
        help_text="Stable identifier shared by every version of this document family, "
                   "e.g. 'standard-mutual-nda'. Set once on the first version; every "
                   "subsequent version of the same document reuses it.",
    )
    version = models.PositiveIntegerField()

    agreement_type = models.CharField(max_length=20, choices=AGREEMENT_TYPE_CHOICES, db_index=True)
    name = models.CharField(max_length=255, help_text="Human-facing label, e.g. 'Standard Mutual NDA'.")
    description = models.TextField(
        blank=True, help_text="Internal admin-facing notes about this version — what changed, why, who approved it.",
    )

    body_html = models.TextField(
        help_text="Full clause content with {{ variable }} merge-field placeholders. "
                   "Treated as immutable once any Agreement has been created from this "
                   "version — create a new version instead of editing this field.",
    )
    variables_schema = models.JSONField(
        default=list, blank=True,
        help_text="List of merge-field definitions this template expects, e.g. "
                   "[{'key': 'client_name', 'label': 'Client legal name', 'required': true}]. "
                   "Drives the admin form and pre-send validation; never referenced at render time.",
    )

    # Sensible defaults an Agreement created from this template will
    # pre-fill, but every Agreement stores its own copy of these — see
    # Agreement.confidentiality_term_months / governing_law — so editing
    # a template's defaults never reaches back to touch an already-sent
    # or signed Agreement.
    default_confidentiality_term_months = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="NDA-relevant default. Meaningless for other agreement_types; left blank for those.",
    )
    default_governing_law = models.CharField(max_length=150, blank=True)
    requires_countersignature = models.BooleanField(
        default=True,
        help_text="Whether an Agreement created from this template needs a company-side "
                   "signatory too before it counts as fully executed. See Agreement.is_fully_executed.",
    )
    default_signing_deadline_days = models.PositiveIntegerField(
        default=DEFAULT_SIGNING_DEADLINE_DAYS,
        help_text="Days from send until Agreement.expires_at, unless overridden per-Agreement.",
    )

    is_active = models.BooleanField(
        default=True, db_index=True,
        help_text="False = retired. Never delete a template that's been used (see Agreement.template's "
                   "on_delete=PROTECT) — retire it here instead so history stays intact.",
    )
    is_latest = models.BooleanField(
        default=True, db_index=True,
        help_text="Denormalized — true for exactly one version per family_key at a time. "
                   "Maintained by the services layer, not derived here.",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="agreement_templates_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(
        auto_now=True,
        help_text="Only non-legal-content fields (is_active, description, defaults) should ever "
                   "change after creation — see module docstring. body_html changes belong in a new version.",
    )

    class Meta:
        ordering = ["family_key", "-version"]
        constraints = [
            models.UniqueConstraint(fields=["family_key", "version"], name="unique_template_version_per_family"),
        ]
        indexes = [
            models.Index(fields=["agreement_type", "is_active"]),
            models.Index(fields=["family_key", "is_latest"]),
        ]

    def __str__(self):
        return f"{self.name} v{self.version} ({self.get_agreement_type_display()})"


# ── Agreement ─────────────────────────────────────────────────────────

AGREEMENT_STATUS_CHOICES = [
    ("draft", "Draft"),
    ("sent", "Sent"),
    ("viewed", "Viewed"),
    ("signed", "Signed"),
    ("declined", "Declined"),
    ("voided", "Voided"),
    ("expired", "Expired"),
]

# Explicit allow-list of status transitions — same reasoning as
# projects.models.ALLOWED_STATUS_TRANSITIONS: prevents an admin action
# or a race (e.g. a webhook-like event landing after a void) from
# moving an Agreement somewhere it has no business being. Enforced via
# can_transition_to(); actual mutation happens in the guarded methods
# below, called from the services layer.
ALLOWED_STATUS_TRANSITIONS = {
    "draft": {"sent", "voided"},
    "sent": {"viewed", "signed", "declined", "voided", "expired"},
    "viewed": {"signed", "declined", "voided", "expired"},
    "signed": {"voided"},  # administrative void of an already-signed doc — requires void_reason, rare, logged
    "declined": {"voided"},
    "expired": {"sent"},  # resending after expiry issues a fresh token and reopens it
    "voided": set(),  # terminal — supersede with a new Agreement, never reopen a voided one
}

AGREEMENT_STATUS_OPEN = {"draft", "sent", "viewed"}
SIGNABLE_STATUSES = {"sent", "viewed"}

SIGNATURE_METHOD_CHOICES = [
    ("typed", "Typed name"),
    ("drawn", "Drawn signature"),
    ("uploaded", "Uploaded signature image"),
]


class Agreement(models.Model):
    """
    One sent-or-signed instance of a legal document — an NDA, an MSA,
    whatever `agreement_type` says. THE model this whole app exists to
    provide. See module docstring for the security pattern, the
    countersignature concept, and why the three relationship FKs are
    all optional string references.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # PROTECT, deliberately not SET_NULL: once a template has been used
    # to create even one Agreement, it must never be deletable — that
    # would silently orphan the provenance of a possibly-signed legal
    # document. Retire the template (is_active=False) instead; this
    # Agreement's own rendered_content_html is unaffected either way.
    template = models.ForeignKey(
        AgreementTemplate, on_delete=models.PROTECT, related_name="agreements",
    )
    # Denormalized off the template at creation time — see Contract's
    # identical reasoning for client_name/client_email: this row must
    # stay self-contained and correct even if the template is later
    # retired or its own agreement_type field is (very unusually)
    # changed.
    agreement_type = models.CharField(max_length=20, choices=AGREEMENT_TYPE_CHOICES, db_index=True)
    template_version = models.PositiveIntegerField(help_text="Snapshot of template.version at creation time.")

    title = models.CharField(max_length=255, help_text="e.g. 'Mutual NDA — Acme Corp'.")

    # THE legally binding content — fully merge-field-resolved HTML,
    # frozen at send time. Never re-rendered from the template after
    # that; see module + AgreementTemplate docstrings.
    rendered_content_html = models.TextField(blank=True)
    content_variables = models.JSONField(
        default=dict, blank=True,
        help_text="The actual merge-field values used to produce rendered_content_html — kept for "
                   "audit/regeneration purposes, never re-applied to change rendered_content_html itself.",
    )

    # ── Optional context — every FK nullable, one-way, string refs to
    # avoid any import coupling. An Agreement can exist attached to
    # any stage of the pipeline, or none at all. ──────────────────────
    lead = models.ForeignKey(
        "leads.Lead", on_delete=models.SET_NULL, null=True, blank=True, related_name="agreements",
    )
    project = models.ForeignKey(
        "projects.Project", on_delete=models.SET_NULL, null=True, blank=True, related_name="agreements",
    )
    contract = models.ForeignKey(
        "contracts.Contract", on_delete=models.SET_NULL, null=True, blank=True, related_name="agreements",
    )

    # ── Client identity — denormalized, self-contained, same reasoning
    # as contracts.Contract. ──────────────────────────────────────────
    client_name = models.CharField(max_length=150)
    client_email = models.EmailField(db_index=True)
    client_company = models.CharField(max_length=150, blank=True)
    client_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="client_agreements",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="agreements_created",
    )

    status = models.CharField(max_length=20, choices=AGREEMENT_STATUS_CHOICES, default="draft", db_index=True)

    # ── No-login client access — identical pattern to
    # contracts.Contract.access_token_hash. A leaked DB backup should
    # never let anyone open someone else's NDA. ───────────────────────
    access_token_hash = models.CharField(max_length=64, blank=True)
    access_token_expires_at = models.DateTimeField(null=True, blank=True)

    # ── Timeline ───────────────────────────────────────────────────
    sent_at = models.DateTimeField(null=True, blank=True)
    first_viewed_at = models.DateTimeField(
        null=True, blank=True, help_text="First time the client opened the link. Later views are in AgreementEvent.",
    )
    expires_at = models.DateTimeField(
        null=True, blank=True,
        help_text="Business deadline to sign by — distinct from access_token_expires_at (the technical link "
                   "expiry). Past this, a periodic task flips status to 'expired'; resending issues a fresh "
                   "token AND a fresh deadline.",
    )

    # ── Signature — tamper-evident, same shape as
    # contracts.Contract.record_signature. ────────────────────────────
    signed_at = models.DateTimeField(null=True, blank=True)
    signed_by_name = models.CharField(max_length=150, blank=True)
    signed_by_email = models.EmailField(blank=True)
    signed_by_title = models.CharField(
        max_length=150, blank=True, help_text="Signatory's role, e.g. 'Director', 'CEO' — standard on legal docs.",
    )
    signature_method = models.CharField(max_length=20, choices=SIGNATURE_METHOD_CHOICES, default="typed")
    signed_ip = models.GenericIPAddressField(null=True, blank=True)
    signed_user_agent = models.TextField(blank=True)
    signed_geolocation = models.JSONField(
        default=dict, blank=True,
        help_text="Optional country/region derived from signed_ip (see property_intel.ip_intel) — "
                   "evidentiary detail, never required for signing to succeed.",
    )
    signed_content_hash = models.CharField(
        max_length=64, blank=True,
        help_text="SHA-256 of rendered_content_html at the moment of signing. rendered_content_html should "
                   "never change after send, so this is defense-in-depth against DB-level tampering, not the "
                   "primary integrity mechanism — the primary one is that the content is frozen at all.",
    )

    # ── Decline ────────────────────────────────────────────────────
    declined_at = models.DateTimeField(null=True, blank=True)
    declined_reason = models.TextField(blank=True)

    # ── Void — administrative invalidation, never a delete. ───────────
    voided_at = models.DateTimeField(null=True, blank=True)
    voided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="agreements_voided",
    )
    void_reason = models.TextField(blank=True)

    # ── Countersignature — company-side execution. An Agreement whose
    # template requires this is NOT fully executed on client-signature
    # alone; see is_fully_executed. ────────────────────────────────────
    requires_countersignature = models.BooleanField(default=True)
    countersigned_at = models.DateTimeField(null=True, blank=True)
    countersigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="agreements_countersigned",
    )
    countersigned_by_title = models.CharField(max_length=150, blank=True)

    # ── Legal terms — general (governing_law applies to any type);
    # NDA-specific fields are blank/null for non-NDA agreement_types. ──
    effective_date = models.DateField(
        null=True, blank=True, help_text="When the agreement takes effect — may differ from signed_at.",
    )
    governing_law = models.CharField(max_length=150, blank=True)
    direction = models.CharField(
        max_length=20, choices=NDA_DIRECTION_CHOICES, blank=True,
        help_text="NDA-only. Blank/ignored for other agreement_types.",
    )
    confidentiality_term_months = models.PositiveIntegerField(
        null=True, blank=True, help_text="NDA-only — how long confidentiality obligations survive termination.",
    )

    # ── Generated documents — Supabase-hosted, see agreements/storage.py
    # and agreements/pdf.py (rendering pass). Never local disk — same
    # reasoning projects.models.Project already documents for its own
    # attachment_url (Render's filesystem is ephemeral). ──────────────
    generated_pdf_url = models.URLField(
        max_length=500, blank=True, help_text="Rendered document as sent/signed.",
    )
    certificate_pdf_url = models.URLField(
        max_length=500, blank=True,
        help_text="DocuSign-style 'Certificate of Completion' — the AgreementEvent trail as a PDF, "
                   "generated once fully executed.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["client_email", "status"]),
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["agreement_type", "status"]),
            models.Index(fields=["contract", "status"]),
        ]

    def __str__(self):
        return f"{self.title} — {self.client_name} ({self.status})"

    # ── Status transitions ────────────────────────────────────────
    def can_transition_to(self, new_status):
        if new_status == self.status:
            return True
        return new_status in ALLOWED_STATUS_TRANSITIONS.get(self.status, set())

    # ── No-login access token — identical pattern to contracts.Contract ──
    def generate_access_token(self, *, valid_days=None):
        """Returns the RAW token (caller emails/links this); only the
        hash is persisted. Overwrites any previous token — most-recent-
        wins, same as Contract.generate_access_token()."""
        raw_token = secrets.token_urlsafe(32)
        self.access_token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        self.access_token_expires_at = timezone.now() + timedelta(
            days=valid_days if valid_days is not None else DEFAULT_ACCESS_TOKEN_VALID_DAYS
        )
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
        """True if EITHER the logged-in user is this agreement's client
        OR the supplied token is valid — either path is sufficient."""
        if user is not None and getattr(user, "is_authenticated", False) and self.client_user_id == user.id:
            return True
        if token:
            return self.verify_access_token(token)
        return False

    # ── Content integrity ────────────────────────────────────────
    def content_hash(self):
        return hashlib.sha256((self.rendered_content_html or "").encode("utf-8")).hexdigest()

    # ── Timeline mutations — guarded/idempotent, mirroring
    # contracts.Contract.mark_sent / Milestone.mark_paid's pattern.
    # Business-rule validation (is this transition even allowed right
    # now, should a notification fire) belongs to the services layer,
    # not here — these methods trust the caller and only guard against
    # double-application. ────────────────────────────────────────────
    def mark_sent(self, *, expires_in_days=None):
        if self.status == "draft":
            self.status = "sent"
        self.sent_at = timezone.now()
        self.expires_at = timezone.now() + timedelta(
            days=expires_in_days if expires_in_days is not None else DEFAULT_SIGNING_DEADLINE_DAYS
        )
        self.save(update_fields=["status", "sent_at", "expires_at"])

    def mark_viewed(self):
        if not self.first_viewed_at:
            self.first_viewed_at = timezone.now()
        if self.status == "sent":
            self.status = "viewed"
        self.save(update_fields=["status", "first_viewed_at"])

    def record_signature(self, *, name, email, title="", method="typed", ip_address="", user_agent="",
                          geolocation=None):
        """Idempotent — a second call against an already-signed
        Agreement is a no-op (returns False), matching
        Milestone.mark_paid()'s guard against a webhook/verify race."""
        if self.status == "signed":
            return False
        self.signed_by_name = name
        self.signed_by_email = email
        self.signed_by_title = title
        self.signature_method = method
        self.signed_ip = ip_address or None
        self.signed_user_agent = user_agent or ""
        self.signed_geolocation = geolocation or {}
        self.signed_content_hash = self.content_hash()
        self.signed_at = timezone.now()
        self.status = "signed"
        self.save(update_fields=[
            "signed_by_name", "signed_by_email", "signed_by_title", "signature_method",
            "signed_ip", "signed_user_agent", "signed_geolocation", "signed_content_hash",
            "signed_at", "status",
        ])
        return True

    def record_decline(self, *, reason=""):
        if self.status == "declined":
            return False
        self.status = "declined"
        self.declined_at = timezone.now()
        self.declined_reason = reason
        self.save(update_fields=["status", "declined_at", "declined_reason"])
        return True

    def void(self, *, by, reason):
        """Administrative invalidation — never a delete. Allowed even
        against an already-signed Agreement (see
        ALLOWED_STATUS_TRANSITIONS) because a signed document can still
        need superseding (wrong entity, wrong template), but always
        requires a reason, and is itself terminal — nothing reopens a
        voided Agreement; issue a new one instead."""
        if self.status == "voided":
            return False
        self.status = "voided"
        self.voided_at = timezone.now()
        self.voided_by = by
        self.void_reason = reason
        self.save(update_fields=["status", "voided_at", "voided_by", "void_reason"])
        return True

    def mark_expired(self):
        if self.status not in AGREEMENT_STATUS_OPEN:
            return False
        self.status = "expired"
        self.save(update_fields=["status"])
        return True

    def record_countersignature(self, *, by, title=""):
        """Idempotent. A no-op if this Agreement's template doesn't
        require one, or it's already been done."""
        if not self.requires_countersignature or self.countersigned_at:
            return False
        self.countersigned_at = timezone.now()
        self.countersigned_by = by
        self.countersigned_by_title = title
        self.save(update_fields=["countersigned_at", "countersigned_by", "countersigned_by_title"])
        return True

    # ── Derived state ────────────────────────────────────────────
    @property
    def is_expired(self):
        return bool(self.expires_at) and timezone.now() >= self.expires_at and self.status in AGREEMENT_STATUS_OPEN

    @property
    def is_signable(self):
        return self.status in SIGNABLE_STATUSES and not self.is_expired

    @property
    def is_fully_executed(self):
        """The actual 'this is legally binding on both sides' moment —
        client signature alone is not enough when the template requires
        a company countersignature too."""
        if self.status != "signed":
            return False
        return not self.requires_countersignature or self.countersigned_at is not None


# ── Audit trail ───────────────────────────────────────────────────────

EVENT_TYPE_CHOICES = [
    ("created", "Created"),
    ("sent", "Sent"),
    ("resent", "Resent"),
    ("reminder_sent", "Reminder sent"),
    ("viewed", "Viewed"),
    ("signature_started", "Signature started"),
    ("signed", "Signed"),
    ("countersigned", "Countersigned"),
    ("declined", "Declined"),
    ("voided", "Voided"),
    ("expired", "Expired"),
    ("downloaded", "Document downloaded"),
]

ACTOR_TYPE_CHOICES = [
    ("admin", "Admin"),
    ("client", "Client"),
    ("system", "System"),
]


class AgreementEvent(models.Model):
    """
    Append-only forensic log — never updated, never deleted. This is
    what a "Certificate of Completion" PDF is generated from once an
    Agreement is fully executed (see Agreement.certificate_pdf_url).
    Every meaningful thing that happens to an Agreement gets a row
    here, written by the services layer (never a bare .save() call
    scattered across views) — the same "one place creates it" discipline
    contracts.services.post_message() and projects.services.
    post_project_message() already apply to their own append-only logs.
    """

    agreement = models.ForeignKey(Agreement, on_delete=models.CASCADE, related_name="events")

    event_type = models.CharField(max_length=30, choices=EVENT_TYPE_CHOICES, db_index=True)
    actor_type = models.CharField(max_length=10, choices=ACTOR_TYPE_CHOICES, default="system")
    actor_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="agreement_events",
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
        ordering = ["created_at"]  # chronological — this reads top-to-bottom like an actual audit trail
        indexes = [
            models.Index(fields=["agreement", "event_type"]),
            models.Index(fields=["agreement", "created_at"]),
        ]

    def __str__(self):
        return f"{self.agreement_id} [{self.event_type}] {self.created_at:%Y-%m-%d %H:%M}"
