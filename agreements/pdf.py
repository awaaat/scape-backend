"""
agreements/pdf.py

Two distinct rendering jobs live here, deliberately kept separate:

  1. render_template_content() — merges an AgreementTemplate's
     {{ variable }} placeholders with real values into final HTML. This
     is TEXT merging, not PDF. Called exactly once, by
     services.create_agreement_from_template() at Agreement-creation
     time. Its output becomes Agreement.rendered_content_html and is
     never re-run after that — see models.py's module docstring on why
     content is frozen at creation, not re-rendered on every view/PDF
     export.

  2. render_agreement_document_pdf() / render_agreement_certificate_pdf()
     — take an already-created Agreement (whose rendered_content_html
     is already final) and lay it out as PDF via WeasyPrint, using
     agreement_document.html.j2. Same tool, same HTML-straight-to-PDF
     approach property_intel/pdf.py already uses for its own reports —
     no ReportLab, no separate print-layout engine to keep in sync with
     the HTML by hand.

Both jobs use the SAME Jinja2 Environment, with autoescape ON. This
matters for #1 specifically: AgreementTemplate.body_html itself is
staff-authored trusted HTML (static tags render as-is), but the VALUES
plugged into its {{ }} placeholders are not always staff-authored —
some come from client-facing forms upstream (e.g. a proposed company
name). Autoescape means a value containing "<script>" renders as
literal text in the document, not executable markup. This is a real
integrity boundary for a legal document, not a nice-to-have.
"""
import hashlib
import logging
import os

from django.conf import settings
from jinja2 import Environment, FileSystemLoader, select_autoescape
from weasyprint import HTML

logger = logging.getLogger("agreements")

TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "templates")
_jinja_env = Environment(
    loader=FileSystemLoader(TEMPLATE_DIR),
    autoescape=select_autoescape(["html", "j2"]),
)

# Set AGREEMENTS_COMPANY_NAME / AGREEMENTS_COMPANY_ADDRESS in
# settings/env once decided — same getattr-with-fallback pattern
# property_intel/pdf.py already uses for SCAPE_SUPPORT_PHONE. Falls
# back to sane defaults so document generation never breaks on a
# missing env var.
COMPANY_NAME = getattr(settings, "AGREEMENTS_COMPANY_NAME", "Scape")
COMPANY_ADDRESS = getattr(settings, "AGREEMENTS_COMPANY_ADDRESS", "")

DISPLAY_HASH_LENGTH = 12
DIRECTION_DISPLAY = {
    "mutual": "Mutual — both parties disclose",
    "disclosing_only": "One-way — we disclose to them",
    "receiving_only": "One-way — they disclose to us",
}


class TemplateRenderError(Exception):
    """Raised when merging an AgreementTemplate's placeholders with real
    values fails — e.g. a malformed {{ }} expression in body_html, or a
    required variable missing. Caller (services.py) treats this as a
    hard stop: an Agreement is never created with unresolved or
    silently-blank merge fields in its legally-binding content."""


class DocumentRenderError(Exception):
    """Raised on unrecoverable PDF rendering failure — mirrors
    property_intel.pdf.ReportRenderError. Caller treats this as a
    failed render, never a partial/corrupt PDF: nothing is uploaded to
    storage or linked from Agreement.generated_pdf_url unless this
    function returns successfully."""


def _short_hash(full_hash):
    """Truncates a full SHA-256 hex digest to a short, human-scannable
    footer value. Returns None (never a misleadingly-short empty
    string) if there's nothing to truncate yet — the template already
    renders a 'pending' fallback for that case."""
    if not full_hash:
        return None
    return full_hash[:DISPLAY_HASH_LENGTH]


def _fmt_dt(dt):
    """Human-readable timestamp for the document, or None if unset —
    the template renders its own 'awaiting signature' state for None,
    never a fake date."""
    if not dt:
        return None
    return dt.strftime("%d %B %Y, %H:%M UTC")


def _fmt_date(d):
    if not d:
        return None
    return d.strftime("%d %B %Y")


def render_template_content(template_obj, variables):
    """
    Merges AgreementTemplate.body_html's {{ variable }} placeholders
    against `variables` (a flat dict) and returns the fully-resolved
    HTML string. THE only place this merge happens — called once by
    services.create_agreement_from_template(); the result is frozen
    into Agreement.rendered_content_html from that point on.

    Deliberately does NOT validate `variables` against
    template_obj.variables_schema here — that's a pre-send admin-UX
    concern (services.py's job, before this is even called), not a
    rendering concern. This function's only job is a correct, safe
    merge of whatever it's given.
    """
    try:
        merge_template = _jinja_env.from_string(template_obj.body_html)
        return merge_template.render(**(variables or {}))
    except Exception as exc:
        logger.error("Template content merge failed for AgreementTemplate %s: %s", template_obj.id, exc)
        raise TemplateRenderError(str(exc)) from exc


def _document_context(agreement):
    """Shared context-building for both document and certificate PDFs —
    kept as one function so the two renders can never drift out of
    sync on shared fields like party names or the integrity hash."""
    is_signed = bool(agreement.signed_at)
    return {
        "company_name": COMPANY_NAME,
        "company_address": COMPANY_ADDRESS,
        "document_type_label": agreement.get_agreement_type_display(),
        "title": agreement.title,
        "reference_id": str(agreement.id)[:12].upper(),
        "generated_date": _fmt_date(agreement.updated_at.date() if agreement.updated_at else None),
        "effective_date": _fmt_date(agreement.effective_date),
        "client_name": agreement.client_name,
        "client_company": agreement.client_company,
        "client_email": agreement.client_email,
        "content_html": agreement.rendered_content_html,
        "governing_law": agreement.governing_law,
        "direction": DIRECTION_DISPLAY.get(agreement.direction, agreement.direction) if agreement.direction else "",
        "confidentiality_term_months": agreement.confidentiality_term_months,
        "requires_countersignature": agreement.requires_countersignature,
        "signed_by_name": agreement.signed_by_name,
        "signed_by_title": agreement.signed_by_title,
        "signed_at": _fmt_dt(agreement.signed_at),
        "signed_ip": agreement.signed_ip,
        "signature_method": agreement.get_signature_method_display() if is_signed else None,
        "countersigned_by_name": agreement.countersigned_by.get_full_name() or agreement.countersigned_by.get_username()
            if agreement.countersigned_by else agreement.countersigned_by_title or None,
        "countersigned_by_title": agreement.countersigned_by_title,
        "countersigned_at": _fmt_dt(agreement.countersigned_at),
        "document_hash": _short_hash(agreement.signed_content_hash) if is_signed else None,
    }


def render_agreement_document_pdf(agreement):
    """
    Renders the agreement itself — letterhead, parties, clause body,
    signature block — to PDF bytes. Safe to call at any stage: before
    signing (signature block shows 'awaiting signature'), and again
    after (signature block fills in), which is exactly why
    services.py calls this twice — once at send time for the client's
    review copy, once at execution time for the final record.

    Raises DocumentRenderError on any failure; never returns a partial
    or silently-broken PDF.
    """
    try:
        context = _document_context(agreement)
        context["render_mode"] = "document"

        template = _jinja_env.get_template("agreement_document.html.j2")
        html_string = template.render(**context)

        return HTML(string=html_string, base_url=TEMPLATE_DIR).write_pdf()
    except Exception as exc:
        logger.error("Document PDF render failed for agreement %s: %s", agreement.id, exc)
        raise DocumentRenderError(str(exc)) from exc


def render_agreement_certificate_pdf(agreement):
    """
    Renders the Certificate of Completion — the full AgreementEvent
    trail as a standalone document. Caller (services.py) is expected to
    only call this once Agreement.is_fully_executed is True; this
    function itself doesn't enforce that (rendering an in-progress
    trail is harmless, just not the intended usage), keeping the
    business-rule decision in the services layer where the rest of
    this app's rules already live.
    """
    try:
        context = _document_context(agreement)
        context["render_mode"] = "certificate"
        context["audit_events"] = [
            {
                "created_at": _fmt_dt(event.created_at),
                "event_type": event.get_event_type_display(),
                "actor_name": event.actor_name or event.get_actor_type_display(),
                "actor_type": event.actor_type,
                "ip_address": event.ip_address or "",
            }
            for event in agreement.events.all()
        ]

        template = _jinja_env.get_template("agreement_document.html.j2")
        html_string = template.render(**context)

        return HTML(string=html_string, base_url=TEMPLATE_DIR).write_pdf()
    except Exception as exc:
        logger.error("Certificate PDF render failed for agreement %s: %s", agreement.id, exc)
        raise DocumentRenderError(str(exc)) from exc


def compute_content_hash(html_string):
    """Standalone SHA-256 helper, exposed for services.py to use when it
    needs a hash outside of Agreement.content_hash() (e.g. hashing a
    template's merged preview before an Agreement row even exists)."""
    return hashlib.sha256((html_string or "").encode("utf-8")).hexdigest()