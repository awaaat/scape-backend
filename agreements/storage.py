"""
agreements/storage.py

Object storage for generated Agreement PDFs — Supabase Storage
(S3-compatible REST API), same principle as property_intel/storage.py
and projects/storage.py: never trust local disk (Render's filesystem
is ephemeral), always return OUR url, and keep this the ONLY module in
this app that knows about bucket names/credentials. Reuses the same
SUPABASE_URL / SUPABASE_SERVICE_KEY already configured for those two
apps — one Supabase project, a separate bucket (AGREEMENTS_STORAGE_
BUCKET) so files don't mix.

── Deliberately DIFFERENT from projects/storage.py's upload pattern ──
projects/storage.py mints a fresh UUID filename on every upload,
because every upload there is a genuinely distinct user attachment.
That pattern is wrong here: services.py re-renders and re-uploads an
Agreement's document PDF up to three times over its life (on send, on
sign, on countersign) — same document, later state. Minting a new
object each time would mean every Agreement leaves 2-3 permanently
orphaned PDFs behind, forever, for no reason. On a free Supabase plan
(1GB storage, capped monthly egress) with a business that hasn't broken
even yet, that's not a hypothetical cost to shrug off.

So: ONE stable path per (agreement, kind) — "{agreement_id}/document.pdf"
and "{agreement_id}/certificate.pdf" — uploaded with x-upsert so every
re-render overwrites the previous version in place. An agreement's
storage footprint is exactly 1-2 small PDFs, never more, no matter how
many times it's resent or its document regenerated.

delete_agreement_documents() exists for genuinely wasted rows (an
abandoned draft, say) but is NOT wired into services.py's normal flow
and should not be called against anything that's ever been sent —
these are the actual legal documents; storage cost is the wrong thing
to optimize against retention obligations. See its own docstring.
"""
import logging

import requests
from django.conf import settings

logger = logging.getLogger("agreements")

SUPABASE_URL = getattr(settings, "SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = getattr(settings, "SUPABASE_SERVICE_KEY", "")
AGREEMENTS_STORAGE_BUCKET = getattr(settings, "AGREEMENTS_STORAGE_BUCKET", "agreement-documents")

REQUEST_TIMEOUT_SECONDS = 20
MAX_UPLOAD_RETRIES = 2


class StorageUploadFailed(Exception):
    pass


def _document_path(agreement):
    return f"{agreement.id}/document.pdf"


def _certificate_path(agreement):
    return f"{agreement.id}/certificate.pdf"


def _upload(raw_bytes, path, content_type="application/pdf"):
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        raise StorageUploadFailed(
            "Object storage is not configured (SUPABASE_URL / SUPABASE_SERVICE_KEY missing)."
        )

    url = f"{SUPABASE_URL}/storage/v1/object/{AGREEMENTS_STORAGE_BUCKET}/{path}"
    headers = {
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": content_type,
        # Overwrite the same object in place — see module docstring on
        # why a stable path per (agreement, kind) matters here.
        "x-upsert": "true",
    }

    last_exc = None
    for attempt in range(1, MAX_UPLOAD_RETRIES + 1):
        try:
            resp = requests.put(url, data=raw_bytes, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            last_exc = exc
            logger.warning("Storage upload attempt %s/%s failed for %s: %s", attempt, MAX_UPLOAD_RETRIES, path, exc)
            continue

        if resp.status_code in (200, 201):
            return f"{SUPABASE_URL}/storage/v1/object/public/{AGREEMENTS_STORAGE_BUCKET}/{path}"

        if resp.status_code == 429:
            # Free-tier rate limit. Worth its own log line rather than
            # folding into the generic "rejected" branch below, since
            # this is the one failure mode where "we're on the free
            # plan" is the actual explanation, not a code bug.
            last_exc = StorageUploadFailed("Rate limited by Supabase Storage (HTTP 429).")
            logger.warning("Storage upload rate-limited (attempt %s/%s) for %s", attempt, MAX_UPLOAD_RETRIES, path)
            continue

        last_exc = StorageUploadFailed(f"HTTP {resp.status_code}: {resp.text[:300]}")
        logger.warning("Storage upload attempt %s/%s rejected for %s: %s", attempt, MAX_UPLOAD_RETRIES, path, last_exc)

    logger.error("Storage upload permanently failed for %s: %s", path, last_exc)
    raise StorageUploadFailed(str(last_exc))


def upload_agreement_document_pdf(pdf_bytes, agreement):
    """
    Uploads/overwrites the rendered Agreement document at its one
    stable path. Called by services.py's _regenerate_document_pdf()
    every time the document's content actually changes (send, sign,
    countersign) — each call replaces the previous object rather than
    adding a new one.
    """
    return _upload(pdf_bytes, _document_path(agreement))


def upload_agreement_certificate_pdf(pdf_bytes, agreement):
    """
    Uploads/overwrites the Certificate of Completion at its one stable
    path. In practice this is generated once execution completes and
    never changes again (the AgreementEvent trail it's built from is
    itself only appended to before that point) — upsert here is a
    safety net, not something expected to fire twice in normal use.
    """
    return _upload(pdf_bytes, _certificate_path(agreement))


def delete_agreement_documents(agreement):
    """
    Deletes BOTH objects for an Agreement, if present. For cleaning up
    genuinely wasted storage — an abandoned draft, a test row, a
    duplicate created by mistake — never for anything that's actually
    been sent to a client. A voided Agreement is exactly the case where
    the PDF must NOT be deleted: void_agreement() explicitly keeps the
    row (never a delete, per models.py's module docstring) precisely so
    the record — including its rendered document — survives. Do not
    wire this into services.py's normal flow.

    Non-fatal: logs and returns False on failure rather than raising,
    matching property_intel.storage.delete_object's reasoning — a
    cleanup action failing should never become the caller's problem.
    """
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        logger.warning("Cannot delete agreement documents for %s -- storage not configured.", agreement.id)
        return False

    ok = True
    for path in (_document_path(agreement), _certificate_path(agreement)):
        url = f"{SUPABASE_URL}/storage/v1/object/{AGREEMENTS_STORAGE_BUCKET}/{path}"
        headers = {"Authorization": f"Bearer {SUPABASE_SERVICE_KEY}"}
        try:
            resp = requests.delete(url, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            logger.warning("Storage delete failed for %s: %s", path, exc)
            ok = False
            continue

        if resp.status_code not in (200, 204):
            logger.warning("Storage delete rejected for %s: HTTP %s: %s", path, resp.status_code, resp.text[:300])
            ok = False

    return ok