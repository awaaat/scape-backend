"""
projects/storage.py

Object storage for project attachments — Supabase Storage (S3-compatible
REST API), same principle and same wrapper shape as
property_intel/storage.py: never trust local disk (Render's filesystem
is ephemeral, wiped on every deploy/restart), always return OUR url,
and keep this the ONLY module in this app that knows about bucket
names/credentials.

Reuses the SUPABASE_URL / SUPABASE_SERVICE_KEY already configured for
property_intel — same Supabase project, different bucket
(PROJECTS_STORAGE_BUCKET) so the two apps' files don't mix.
"""
import logging
import uuid

import requests
from django.conf import settings

logger = logging.getLogger("projects")

SUPABASE_URL = getattr(settings, "SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = getattr(settings, "SUPABASE_SERVICE_KEY", "")
PROJECTS_STORAGE_BUCKET = getattr(settings, "PROJECTS_STORAGE_BUCKET", "project-attachments")

REQUEST_TIMEOUT_SECONDS = 20
MAX_UPLOAD_RETRIES = 2


class StorageUploadFailed(Exception):
    pass


def _safe_filename(original_name):
    """Keeps the extension (needed for content-type / user recognition)
    but replaces the name itself with a UUID — never trust a
    client-supplied filename as a storage path component."""
    ext = ""
    if "." in original_name:
        ext = "." + original_name.rsplit(".", 1)[-1].lower()
    return f"{uuid.uuid4().hex}{ext}"


def upload_project_attachment(uploaded_file, project_id):
    """
    Uploads a Django UploadedFile to Supabase Storage and returns
    (public_url, stored_filename). Raises StorageUploadFailed on
    permanent failure — callers (services.create_project /
    services.update_project_attachment) let that propagate as a 502 to
    the client rather than silently pretending the attachment saved.
    """
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        raise StorageUploadFailed(
            "Object storage is not configured (SUPABASE_URL / SUPABASE_SERVICE_KEY missing)."
        )

    stored_name = _safe_filename(uploaded_file.name)
    path = f"{project_id}/{stored_name}"
    content_type = uploaded_file.content_type or "application/octet-stream"

    url = f"{SUPABASE_URL}/storage/v1/object/{PROJECTS_STORAGE_BUCKET}/{path}"
    headers = {
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": content_type,
        "x-upsert": "true",
    }

    raw_bytes = uploaded_file.read()

    last_exc = None
    for attempt in range(1, MAX_UPLOAD_RETRIES + 1):
        try:
            resp = requests.put(url, data=raw_bytes, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            last_exc = exc
            logger.warning("Attachment upload attempt %s/%s failed for %s: %s", attempt, MAX_UPLOAD_RETRIES, path, exc)
            continue

        if resp.status_code in (200, 201):
            public_url = f"{SUPABASE_URL}/storage/v1/object/public/{PROJECTS_STORAGE_BUCKET}/{path}"
            return public_url, uploaded_file.name

        last_exc = StorageUploadFailed(f"HTTP {resp.status_code}: {resp.text[:300]}")
        logger.warning("Attachment upload attempt %s/%s rejected for %s: %s", attempt, MAX_UPLOAD_RETRIES, path, last_exc)

    logger.error("Attachment upload permanently failed for %s: %s", path, last_exc)
    raise StorageUploadFailed(str(last_exc))
