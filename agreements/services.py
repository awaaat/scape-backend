"""
agreements/services.py

Orchestration layer, same role as contracts/services.py and
payments/services.py: views stay thin, this is where the actual logic
lives. Every state change that matters legally — send, view, sign,
decline, void, countersign — goes through exactly one function here,
never a bare .save() on Agreement from a view or consumer, so the
AgreementEvent trail (and therefore the eventual Certificate of
Completion PDF) can never silently miss something that happened.

This file is written before agreements/storage.py and agreements/
emails.py exist, so it also fixes the interface those two modules must
satisfy:

  storage.upload_agreement_document_pdf(pdf_bytes, agreement) -> url
  storage.upload_agreement_certificate_pdf(pdf_bytes, agreement) -> url
      Same shape as projects/storage.py's upload_project_attachment —
      Supabase Storage, never local disk. Takes raw PDF bytes (what
      pdf.render_*_pdf() already returns) rather than an UploadedFile,
      since there's no client upload involved here — we generate the
      file ourselves.

  emails.send_agreement_invitation_email(agreement, link)
  emails.send_agreement_signed_email(agreement)
  emails.send_agreement_voided_email(agreement)
  emails.send_agreement_expiring_soon_email(agreement)
      Matches the four templates already sitting in
      agreements/templates/email/ — no decline or countersigned email
      exists by design; those are internal/admin-facing events with no
      corresponding client template yet.

Every email/storage call is wrapped so a failure there never blocks
the underlying state change — same reasoning as contracts.services:
the Agreement being sent/signed/voided is the fact that matters; a
failed notification is logged and swallowed, not allowed to leave the
Agreement stuck in an inconsistent status.
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from . import emails, pdf, storage
from .models import (
    AGREEMENT_STATUS_OPEN, SIGNABLE_STATUSES, Agreement, AgreementEvent, AgreementTemplate,
)

logger = logging.getLogger("agreements")


class AgreementError(Exception):
    """Base class for agreements-layer business-rule violations."""


class AgreementStateError(AgreementError):
    """Raised when an action is attempted against an Agreement whose
    current status doesn't allow it (e.g. signing an already-voided
    agreement). Views/serializers translate this into a 409, not a 500
    — it's an expected outcome of a race or a stale client, not a bug.
    """


class TemplateVersionError(AgreementError):
    """Raised when cloning or resolving an AgreementTemplate version
    fails — e.g. no versions exist yet for a family_key."""


class MissingTemplateVariablesError(AgreementError):
    """Raised when `variables` is missing a value for something
    template.variables_schema marks required. Carries missing_keys so
    the serializer/view layer can report exactly which fields, rather
    than a generic 400."""

    def __init__(self, missing_keys):
        self.missing_keys = list(missing_keys)
        super().__init__(f"Missing required template variables: {', '.join(self.missing_keys)}")


def _frontend_base():
    return getattr(settings, "FRONTEND_BASE_URL", "").rstrip("/")


def build_client_agreement_link(agreement, raw_token=None):
    base = _frontend_base()
    if raw_token:
        return f"{base}/agreements/{agreement.id}?token={raw_token}"
    return f"{base}/agreements/{agreement.id}"


def _log_event(agreement, event_type, *, actor_type="system", actor_user=None, actor_name="",
                ip_address=None, user_agent="", metadata=None):
    """THE single place an AgreementEvent row gets created. Every
    public function below that changes an Agreement's state calls this
    exactly once for that change — see models.AgreementEvent's
    docstring on why creation is centralized here rather than scattered
    across views."""
    return AgreementEvent.objects.create(
        agreement=agreement,
        event_type=event_type,
        actor_type=actor_type,
        actor_user=actor_user,
        actor_name=actor_name,
        ip_address=ip_address or None,
        user_agent=user_agent or "",
        metadata=metadata or {},
    )


def _validate_required_variables(template, variables):
    """
    Checks `variables` against template.variables_schema and raises
    MissingTemplateVariablesError listing every required key that's
    absent or blank. This is the validation pdf.render_template_content()'s
    docstring explicitly defers to services.py for — called once, here,
    before that merge ever runs, so a legally-binding Agreement can never
    be created with an unresolved {{ variable }} silently rendering as
    empty in the final document.

    A key counts as missing if it's absent from `variables` entirely OR
    present but blank/None — a required field left empty by a sloppy
    caller is exactly as bad as one never sent. variables_schema entries
    with no "key" (malformed template config) are skipped rather than
    raised on, since that's a template-authoring bug, not a caller error
    — TemplateRenderError (from pdf.py, on the actual merge) is the
    right signal for that, not this function.
    """
    variables = variables or {}
    missing = [
        field["key"]
        for field in (template.variables_schema or [])
        if field.get("key") and field.get("required") and not str(variables.get(field["key"], "")).strip()
    ]
    if missing:
        raise MissingTemplateVariablesError(missing)


# ── Template versioning ────────────────────────────────────────────────

def clone_template_version(source_template, *, created_by, body_html=None, description="", **field_overrides):
    """
    THE only way a new AgreementTemplate version gets created — see
    models.py's module docstring on why editing body_html in place on
    a used template is a process violation. Copies every content/config
    field from source_template, applies body_html (if changed) and any
    field_overrides (e.g. default_governing_law="Delaware, USA"),
    increments version within family_key, and flips is_latest so
    exactly one version per family is ever flagged current.
    """
    next_version = (
        AgreementTemplate.objects.filter(family_key=source_template.family_key)
        .order_by("-version").values_list("version", flat=True).first() or 0
    ) + 1

    new_template = AgreementTemplate.objects.create(
        family_key=source_template.family_key,
        version=next_version,
        agreement_type=field_overrides.get("agreement_type", source_template.agreement_type),
        name=field_overrides.get("name", source_template.name),
        description=description,
        body_html=body_html if body_html is not None else source_template.body_html,
        variables_schema=field_overrides.get("variables_schema", source_template.variables_schema),
        default_confidentiality_term_months=field_overrides.get(
            "default_confidentiality_term_months", source_template.default_confidentiality_term_months,
        ),
        default_governing_law=field_overrides.get(
            "default_governing_law", source_template.default_governing_law,
        ),
        requires_countersignature=field_overrides.get(
            "requires_countersignature", source_template.requires_countersignature,
        ),
        default_signing_deadline_days=field_overrides.get(
            "default_signing_deadline_days", source_template.default_signing_deadline_days,
        ),
        is_active=True,
        is_latest=True,
        created_by=created_by,
    )

    # Exactly one is_latest=True per family_key, always — flip every
    # other version off in the same operation that creates the new one,
    # rather than relying on a signal or a later cleanup pass.
    AgreementTemplate.objects.filter(
        family_key=source_template.family_key,
    ).exclude(pk=new_template.pk).update(is_latest=False)

    return new_template


# ── Creation ────────────────────────────────────────────────────────────

def create_agreement_from_template(template, *, created_by, client_name, client_email, title,
                                    variables=None, client_company="", client_user=None,
                                    lead=None, project=None, contract=None, effective_date=None,
                                    governing_law=None, direction="", confidentiality_term_months=None,
                                    requires_countersignature=None):
    """
    THE only way an Agreement gets created. Validates `variables`
    against template.variables_schema FIRST — see
    _validate_required_variables()'s docstring on why this has to
    happen before the merge, not after — then merges template.body_html
    against `variables` via pdf.render_template_content(). Lets
    pdf.TemplateRenderError propagate unwrapped; it's already the
    correct exception (a malformed template is a hard stop, per
    pdf.py's TemplateRenderError docstring), so re-wrapping it here
    would only hide which layer actually failed.

    governing_law and confidentiality_term_months fall back to the
    template's defaults when not explicitly provided; direction has no
    template-level default (it's meaningful only per-instance) and
    confidentiality_term_months only defaults for agreement_type="nda"
    — see AgreementTemplate's default_confidentiality_term_months
    docstring on why it's meaningless for other types.

    `lead`, `project`, `contract` are raw PKs (UUID or None) — NOT
    model instances. Passed straight through as `lead_id=`/etc. to
    Agreement.objects.create() below, specifically so this function
    never needs a real Lead/Project/Contract instance and this app
    never has to import those models to call it — see the create()
    call's own comment for why.
    """
    _validate_required_variables(template, variables)
    rendered_html = pdf.render_template_content(template, variables)

    if requires_countersignature is None:
        requires_countersignature = template.requires_countersignature
    if governing_law is None:
        governing_law = template.default_governing_law
    if confidentiality_term_months is None and template.agreement_type == "nda":
        confidentiality_term_months = template.default_confidentiality_term_months

    agreement = Agreement.objects.create(
        template=template,
        agreement_type=template.agreement_type,
        template_version=template.version,
        title=title,
        rendered_content_html=rendered_html,
        content_variables=variables or {},
        # _id assignment, not lead=lead/project=project/contract=contract:
        # accepts a raw PK directly, so this app never has to import
        # leads.Lead / projects.Project / contracts.Contract to satisfy
        # a ForeignKey descriptor's isinstance check — the decoupling
        # models.py's module docstring promises ("this app imports
        # nothing from theirs") only holds if creation works this way.
        # `lead`/`project`/`contract` here are raw PKs (or None) coming
        # straight from the view, never resolved model instances.
        lead_id=lead,
        project_id=project,
        contract_id=contract,
        client_name=client_name,
        client_email=client_email,
        client_company=client_company,
        client_user=client_user,
        created_by=created_by,
        effective_date=effective_date,
        governing_law=governing_law or "",
        direction=direction or "",
        confidentiality_term_months=confidentiality_term_months,
        requires_countersignature=requires_countersignature,
    )
    _log_event(agreement, "created", actor_type="admin", actor_user=created_by)
    return agreement


# ── Sending ─────────────────────────────────────────────────────────────

def _regenerate_document_pdf(agreement):
    """Re-renders and re-uploads the document PDF so it reflects the
    Agreement's current state — signature block filled in after
    signing, countersignature block filled in after that. Called from
    every function below that changes something the rendered document
    actually shows; never called for a change that doesn't (e.g.
    declining doesn't touch the document itself)."""
    pdf_bytes = pdf.render_agreement_document_pdf(agreement)
    agreement.generated_pdf_url = storage.upload_agreement_document_pdf(pdf_bytes, agreement)
    agreement.save(update_fields=["generated_pdf_url"])


def _generate_and_store_certificate(agreement):
    """Called once execution completes — from sign_agreement() when no
    countersignature is required, or from countersign_agreement() when
    it is. Harmless to call more than once (pdf.
    render_agreement_certificate_pdf just re-renders the current
    AgreementEvent trail), so no idempotency guard is needed here."""
    pdf_bytes = pdf.render_agreement_certificate_pdf(agreement)
    agreement.certificate_pdf_url = storage.upload_agreement_certificate_pdf(pdf_bytes, agreement)
    agreement.save(update_fields=["certificate_pdf_url"])


def send_agreement_to_client(agreement, *, sent_by=None, expires_in_days=None):
    """
    Generates a fresh access token, renders + uploads the document PDF,
    marks the Agreement sent, and emails the client a link. Handles
    three distinct cases as one function, distinguished only for
    logging purposes: first send (draft -> sent), a plain re-send
    (sent/viewed -> sent, e.g. because the link expired but the signing
    deadline hasn't), and reopening after the business deadline passed
    (expired -> sent) — see Agreement.expires_at's docstring on why a
    resend from "expired" gets a fresh deadline via expires_in_days
    while an ordinary resend from "sent"/"viewed" does not, by default,
    disturb one already in flight.
    """
    if not agreement.can_transition_to("sent"):
        raise AgreementStateError(f"An agreement in '{agreement.status}' status can't be sent.")

    is_first_send = agreement.status == "draft"
    reopened_from_expiry = agreement.status == "expired"

    raw_token = agreement.generate_access_token()

    agreement.status = "sent"
    agreement.mark_sent(expires_in_days=expires_in_days)

    _regenerate_document_pdf(agreement)

    _log_event(
        agreement,
        "sent" if is_first_send else "resent",
        actor_type="admin", actor_user=sent_by,
        metadata={"reopened_from_expiry": True} if reopened_from_expiry else {},
    )

    link = build_client_agreement_link(agreement, raw_token)
    try:
        emails.send_agreement_invitation_email(agreement, link)
    except Exception:
        logger.exception("Failed to send agreement invitation email for agreement %s", agreement.id)
    return link


# ── Client-side actions ──────────────────────────────────────────────────

def record_agreement_viewed(agreement, *, ip_address="", user_agent=""):
    """Logs every view as its own AgreementEvent (the certificate's
    audit trail should show all of them), while Agreement.mark_viewed()
    itself only records the first one in first_viewed_at — see that
    method's docstring."""
    agreement.mark_viewed()
    _log_event(agreement, "viewed", actor_type="client", ip_address=ip_address or None, user_agent=user_agent)


def sign_agreement(agreement, *, name, email, title="", method="typed", ip_address="", user_agent="",
                    geolocation=None):
    """
    THE only way an Agreement gets signed. Refuses outright if
    Agreement.is_signable is False (wrong status, or the business
    deadline already passed) rather than letting record_signature()'s
    own idempotency guard be the only thing standing between this and
    a signed-after-expiry agreement.
    """
    if not agreement.is_signable:
        raise AgreementStateError(f"This agreement can't be signed right now (status: '{agreement.status}').")

    signed = agreement.record_signature(
        name=name, email=email, title=title, method=method,
        ip_address=ip_address, user_agent=user_agent, geolocation=geolocation,
    )
    if not signed:
        raise AgreementStateError("This agreement has already been signed.")

    _log_event(agreement, "signed", actor_type="client", actor_name=name,
               ip_address=ip_address or None, user_agent=user_agent)

    _regenerate_document_pdf(agreement)

    if agreement.is_fully_executed:
        _generate_and_store_certificate(agreement)

    try:
        emails.send_agreement_signed_email(agreement)
    except Exception:
        logger.exception("Failed to send agreement-signed email for agreement %s", agreement.id)

    return agreement


def record_agreement_decline(agreement, *, reason="", declined_by_name="", ip_address="", user_agent=""):
    """THE only way an Agreement gets declined. No client-facing email
    template exists for this yet (see module docstring) — an admin
    finds out via the dashboard / AgreementEvent trail for now."""
    if not agreement.is_signable:
        raise AgreementStateError(f"This agreement can't be declined right now (status: '{agreement.status}').")

    declined = agreement.record_decline(reason=reason)
    if not declined:
        raise AgreementStateError("This agreement has already been declined.")

    _log_event(
        agreement, "declined", actor_type="client", actor_name=declined_by_name,
        ip_address=ip_address or None, user_agent=user_agent,
        metadata={"reason": reason} if reason else {},
    )
    return agreement


# ── Admin-side actions ────────────────────────────────────────────────────

def void_agreement(agreement, *, by, reason):
    """THE only way an Agreement gets voided — never a delete, always
    with a reason. Allowed even against an already-signed Agreement
    (see models.ALLOWED_STATUS_TRANSITIONS) since a signed document can
    still need superseding."""
    if not reason or not reason.strip():
        raise AgreementError("A reason is required to void an agreement.")
    if not agreement.can_transition_to("voided"):
        raise AgreementStateError(f"An agreement in '{agreement.status}' status can't be voided.")

    voided = agreement.void(by=by, reason=reason)
    if not voided:
        raise AgreementStateError("This agreement is already voided.")

    _log_event(agreement, "voided", actor_type="admin", actor_user=by, metadata={"reason": reason})

    try:
        emails.send_agreement_voided_email(agreement)
    except Exception:
        logger.exception("Failed to send agreement-voided email for agreement %s", agreement.id)
    return agreement


def countersign_agreement(agreement, *, by, title=""):
    """
    THE only way an Agreement gets countersigned. Only valid against a
    client-signed Agreement — countersigning before the client has
    signed isn't a real-world sequence this app supports (see
    Agreement.is_fully_executed's docstring on why client signature
    always comes first).
    """
    if agreement.status != "signed":
        raise AgreementStateError(
            f"Only a signed agreement can be countersigned (status: '{agreement.status}')."
        )

    countersigned = agreement.record_countersignature(by=by, title=title)
    if not countersigned:
        raise AgreementStateError(
            "This agreement doesn't require countersignature, or has already been countersigned."
        )

    _log_event(agreement, "countersigned", actor_type="admin", actor_user=by, actor_name=title)

    _regenerate_document_pdf(agreement)
    if agreement.is_fully_executed:
        _generate_and_store_certificate(agreement)
        # sign_agreement() already called this once when the client
        # signed, but that call saw fully_executed=False (a
        # countersignature was still outstanding) and so notified
        # admins only, per emails.send_agreement_signed_email's
        # docstring. Now that this countersignature is what actually
        # completes execution, the client needs the "fully executed"
        # version of that same email — a notification they were never
        # sent the first time around.
        try:
            emails.send_agreement_signed_email(agreement)
        except Exception:
            logger.exception("Failed to send fully-executed notification for agreement %s", agreement.id)

    return agreement


# ── Maintenance ───────────────────────────────────────────────────────────
# Neither function below is wired to a schedule — agreements/tasks.py
# doesn't exist yet (unlike contracts and property_intel, which already
# have one). Add a celery beat entry calling these once it does; they're
# written now so the business logic isn't blocked on that plumbing.

def expire_overdue_agreements():
    """Flips every open Agreement past its business deadline to
    'expired' and logs it. Safe to call repeatedly/concurrently —
    Agreement.mark_expired() is itself a guarded no-op once a given
    Agreement is no longer in AGREEMENT_STATUS_OPEN."""
    overdue = Agreement.objects.filter(status__in=AGREEMENT_STATUS_OPEN, expires_at__lt=timezone.now())
    count = 0
    for agreement in overdue:
        if agreement.mark_expired():
            _log_event(agreement, "expired", actor_type="system")
            count += 1
    return count


def send_expiring_soon_reminders(*, within_days=2):
    """
    Emails a reminder for every open, signable Agreement expiring
    within `within_days` that hasn't already gotten one today —
    checked against the AgreementEvent trail itself rather than a
    dedicated last-reminded field, consistent with this app treating
    the event log as the source of truth rather than duplicating state.
    """
    now = timezone.now()
    cutoff = now + timedelta(days=within_days)
    candidates = Agreement.objects.filter(
        status__in=SIGNABLE_STATUSES, expires_at__isnull=False, expires_at__gt=now, expires_at__lte=cutoff,
    )
    sent_count = 0
    for agreement in candidates:
        already_reminded_today = agreement.events.filter(
            event_type="reminder_sent", created_at__date=now.date(),
        ).exists()
        if already_reminded_today:
            continue
        try:
            emails.send_agreement_expiring_soon_email(agreement)
        except Exception:
            logger.exception("Failed to send expiring-soon email for agreement %s", agreement.id)
            continue
        _log_event(agreement, "reminder_sent", actor_type="system")
        sent_count += 1
    return sent_count