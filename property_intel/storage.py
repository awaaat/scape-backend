"""
property_intel/storage.py

Object storage wrapper — Supabase Storage (S3-compatible REST API). Same
principle as jobs/ app's resume handling: never trust local disk (Render's
filesystem is ephemeral, wiped on every deploy/restart), always return OUR
url, and keep this the ONLY module that knows about bucket names/credentials.

Google's Static Maps / Street View URLs embed an API key as a query param —
google_client.py fetches those bytes server-side and hands them here so the
key is never exposed to anyone opening a PDF or inspecting network traffic.
"""
import logging

import requests
from django.conf import settings

logger = logging.getLogger("property_intel")

SUPABASE_URL = getattr(settings, "SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = getattr(settings, "SUPABASE_SERVICE_KEY", "")
SUPABASE_BUCKET = getattr(settings, "SUPABASE_STORAGE_BUCKET", "property-intel")

REQUEST_TIMEOUT_SECONDS = 20
MAX_UPLOAD_RETRIES = 2


class StorageUploadFailed(Exception):
    pass


def _upload(raw_bytes, path, content_type):
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        raise StorageUploadFailed(
            "Object storage is not configured (SUPABASE_URL / SUPABASE_SERVICE_KEY missing)."
        )

    url = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{path}"
    headers = {
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": content_type,
        # Regenerating a report / re-enriching a cell overwrites the same
        # path cleanly instead of erroring on "object already exists".
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
            return f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_BUCKET}/{path}"

        last_exc = StorageUploadFailed(f"HTTP {resp.status_code}: {resp.text[:300]}")
        logger.warning("Storage upload attempt %s/%s rejected for %s: %s", attempt, MAX_UPLOAD_RETRIES, path, last_exc)

    logger.error("Storage upload permanently failed for %s: %s", path, last_exc)
    raise StorageUploadFailed(str(last_exc))


def upload_image_bytes(raw_bytes, path, content_type="image/jpeg"):
    return _upload(raw_bytes, path, content_type)


def upload_pdf_bytes(raw_bytes, path, content_type="application/pdf"):
    return _upload(raw_bytes, path, content_type)
