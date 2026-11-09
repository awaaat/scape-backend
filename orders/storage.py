"""
orders/storage.py

Dataset / document uploads for orders. Same Supabase Storage approach as
projects/storage.py (Render's disk is ephemeral — never write uploads there).
Reuses the project-attachments bucket by default so there's nothing new to
create in Supabase; set ORDERS_STORAGE_BUCKET to use a dedicated one.
"""
import logging
import uuid

import requests
from django.conf import settings

logger = logging.getLogger("orders")

TIMEOUT = 20
RETRIES = 2


class StorageUploadFailed(Exception):
    pass


def _cfg():
    return (
        getattr(settings, "SUPABASE_URL", "").rstrip("/"),
        getattr(settings, "SUPABASE_SERVICE_KEY", ""),
        getattr(settings, "ORDERS_STORAGE_BUCKET", "project-attachments"),
    )


def _safe_name(original):
    ext = "." + original.rsplit(".", 1)[-1].lower() if "." in original else ""
    return f"{uuid.uuid4().hex}{ext}"


def upload_order_file(uploaded_file, reference):
    """Returns (public_url, storage_path). Raises StorageUploadFailed."""
    base, key, bucket = _cfg()
    if not base or not key:
        raise StorageUploadFailed("Object storage is not configured (SUPABASE_URL / SUPABASE_SERVICE_KEY).")

    path = f"orders/{reference}/{_safe_name(uploaded_file.name)}"
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": uploaded_file.content_type or "application/octet-stream",
        "x-upsert": "true",
    }
    data = uploaded_file.read()
    last = None
    for attempt in range(1, RETRIES + 1):
        try:
            resp = requests.put(f"{base}/storage/v1/object/{bucket}/{path}", data=data, headers=headers, timeout=TIMEOUT)
        except requests.RequestException as exc:
            last = exc
            logger.warning("Order upload %s/%s failed for %s: %s", attempt, RETRIES, path, exc)
            continue
        if resp.status_code in (200, 201):
            return f"{base}/storage/v1/object/public/{bucket}/{path}", path
        last = StorageUploadFailed(f"HTTP {resp.status_code}: {resp.text[:200]}")
        logger.warning("Order upload %s/%s rejected for %s: %s", attempt, RETRIES, path, last)
    raise StorageUploadFailed(str(last))


def delete_order_file(storage_path):
    """Best-effort. The DB row is the source of truth; a leftover object is harmless."""
    base, key, bucket = _cfg()
    if not (base and key and storage_path):
        return
    try:
        requests.delete(
            f"{base}/storage/v1/object/{bucket}/{storage_path}",
            headers={"Authorization": f"Bearer {key}"}, timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.warning("Could not delete %s from storage: %s", storage_path, exc)
