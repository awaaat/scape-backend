
"""
users/storage.py

Avatar uploads — same Supabase Storage (S3-compatible REST) pattern as
contracts/storage.py, projects/storage.py, agreements/storage.py. One
stable path per user ("{user_id}/avatar.<ext>"), upserted in place —
there's only ever one current avatar per account, same reasoning as
contracts/storage.py's upload_contract_document.
"""
import logging

import requests
from django.conf import settings

logger = logging.getLogger("users")

SUPABASE_URL = getattr(settings, "SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = getattr(settings, "SUPABASE_SERVICE_KEY", "")
AVATARS_STORAGE_BUCKET = getattr(settings, "AVATARS_STORAGE_BUCKET", "user-avatars")

REQUEST_TIMEOUT_SECONDS = 20
MAX_UPLOAD_RETRIES = 2

ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_AVATAR_BYTES = 5 * 1024 * 1024  # 5MB


class StorageUploadFailed(Exception):
    pass


class InvalidAvatarUpload(Exception):
    pass


def _extension_for(content_type):
    return {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}.get(content_type, "")


def _put(raw_bytes, path, content_type):
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        raise StorageUploadFailed(
            "Object storage is not configured (SUPABASE_URL / SUPABASE_SERVICE_KEY missing)."
        )

    url = f"{SUPABASE_URL}/storage/v1/object/{AVATARS_STORAGE_BUCKET}/{path}"
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
            logger.warning("Avatar upload attempt %s/%s failed for %s: %s", attempt, MAX_UPLOAD_RETRIES, path, exc)
            continue

        if resp.status_code in (200, 201):
            return f"{SUPABASE_URL}/storage/v1/object/public/{AVATARS_STORAGE_BUCKET}/{path}"

        last_exc = StorageUploadFailed(f"HTTP {resp.status_code}: {resp.text[:300]}")
        logger.warning("Avatar upload attempt %s/%s rejected for %s: %s", attempt, MAX_UPLOAD_RETRIES, path, last_exc)

    logger.error("Avatar upload permanently failed for %s: %s", path, last_exc)
    raise StorageUploadFailed(str(last_exc))


def upload_avatar(uploaded_file, user_id):
    """Validates type/size, then returns the public URL. Overwrites
    whatever avatar this user already had."""
    content_type = uploaded_file.content_type or ""
    if content_type not in ALLOWED_CONTENT_TYPES:
        raise InvalidAvatarUpload("Avatar must be a JPEG, PNG, or WebP image.")
    if uploaded_file.size > MAX_AVATAR_BYTES:
        raise InvalidAvatarUpload("Avatar must be under 5MB.")

    path = f"{user_id}/avatar{_extension_for(content_type)}"
    raw_bytes = uploaded_file.read()
    return _put(raw_bytes, path, content_type)
