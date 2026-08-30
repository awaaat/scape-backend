"""
contracts/storage.py

Object storage for contract-related files — Supabase Storage
(S3-compatible REST API), same pattern as projects/storage.py and
agreements/storage.py: never trust local disk (Render's filesystem is
ephemeral, wiped on every deploy/restart).

Two distinct upload shapes, matching the two fields that need this:
  - upload_deliverable_file() — a genuinely new file each submission,
    fresh UUID path per upload (same reasoning as projects/storage.py).
  - upload_contract_document() — ONE stable path per contract
    ("{contract_id}/document.pdf"), upserted in place (same reasoning
    as agreements/storage.py's document path) — there's only ever one
    "final signed PDF" of record per contract.
"""
import logging
import uuid

import requests
from django.conf import settings

logger = logging.getLogger("contracts")

SUPABASE_URL = getattr(settings, "SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = getattr(settings, "SUPABASE_SERVICE_KEY", "")
CONTRACTS_STORAGE_BUCKET = getattr(settings, "CONTRACTS_STORAGE_BUCKET", "contract-files")

REQUEST_TIMEOUT_SECONDS = 20
MAX_UPLOAD_RETRIES = 2


class StorageUploadFailed(Exception):
    pass


def _safe_filename(original_name):
    ext = ""
    if "." in original_name:
        ext = "." + original_name.rsplit(".", 1)[-1].lower()
    return f"{uuid.uuid4().hex}{ext}"


def _put(raw_bytes, path, content_type):
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        raise StorageUploadFailed(
            "Object storage is not configured (SUPABASE_URL / SUPABASE_SERVICE_KEY missing)."
        )

    url = f"{SUPABASE_URL}/storage/v1/object/{CONTRACTS_STORAGE_BUCKET}/{path}"
    headers = {
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": content_type,
        "x-upsert": "true",
    }

    last_exc = None
    for attempt in range(1, MAX_UPLOAD_RETRIES + 1):
        try:
            resp = requests.put(url, data=raw_bytes, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            last_exc = exc
            logger.warning("Upload attempt %s/%s failed for %s: %s", attempt, MAX_UPLOAD_RETRIES, path, exc)
            continue

        if resp.status_code in (200, 201):
            return f"{SUPABASE_URL}/storage/v1/object/public/{CONTRACTS_STORAGE_BUCKET}/{path}"

        last_exc = StorageUploadFailed(f"HTTP {resp.status_code}: {resp.text[:300]}")
        logger.warning("Upload attempt %s/%s rejected for %s: %s", attempt, MAX_UPLOAD_RETRIES, path, last_exc)

    logger.error("Upload permanently failed for %s: %s", path, last_exc)
    raise StorageUploadFailed(str(last_exc))


def upload_deliverable_file(uploaded_file, milestone_id):
    """Returns (public_url, original_filename)."""
    stored_name = _safe_filename(uploaded_file.name)
    path = f"{milestone_id}/{stored_name}"
    content_type = uploaded_file.content_type or "application/octet-stream"
    raw_bytes = uploaded_file.read()
    url = _put(raw_bytes, path, content_type)
    return url, uploaded_file.name


def upload_contract_document(uploaded_file, contract_id):
    """Returns public_url. Overwrites the same object on every call."""
    path = f"{contract_id}/document.pdf"
    content_type = uploaded_file.content_type or "application/pdf"
    raw_bytes = uploaded_file.read()
    return _put(raw_bytes, path, content_type)


def upload_invoice_pdf(pdf_bytes, invoice_id):
    """Returns public_url. Bytes-native (not an UploadedFile) since this
    is a server-generated PDF (see contracts/invoice_pdf.py), never a
    user upload. Stable one-object-per-invoice path, upserted in place —
    same reasoning as upload_contract_document, but this document is
    genuinely never re-rendered after send (see invoice_pdf.py)."""
    path = f"invoices/{invoice_id}/invoice.pdf"
    return _put(pdf_bytes, path, "application/pdf")
