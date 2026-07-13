"""
payments/paystack.py

Thin wrapper around Paystack's HTTP API. No Django models touched here —
this file only knows how to talk to Paystack and how to verify that a
webhook actually came from Paystack. Orchestration (creating/updating
PaystackTransaction rows, firing signals) lives in services.py.
"""
import hashlib
import hmac
import logging

import requests
from django.conf import settings

logger = logging.getLogger("payments")

PAYSTACK_BASE_URL = "https://api.paystack.co"
REQUEST_TIMEOUT_SECONDS = 15


class PaystackAPIError(Exception):
    """Raised on any non-2xx response, or a response Paystack marked status=false."""


def _secret_key():
    key = getattr(settings, "PAYSTACK_SECRET_KEY", "")
    if not key:
        raise PaystackAPIError("PAYSTACK_SECRET_KEY is not configured.")
    return key


def _headers():
    return {
        "Authorization": f"Bearer {_secret_key()}",
        "Content-Type": "application/json",
    }


def initialize_transaction(*, email, amount_subunit, currency, reference, callback_url=None, metadata=None):
    """
    POST /transaction/initialize — starts a checkout. Returns Paystack's
    `data` dict, which includes `authorization_url` (redirect the broker
    here) and `access_code`.
    """
    payload = {
        "email": email,
        "amount": amount_subunit,
        "currency": currency,
        "reference": reference,
    }
    if callback_url:
        payload["callback_url"] = callback_url
    if metadata:
        payload["metadata"] = metadata

    try:
        resp = requests.post(
            f"{PAYSTACK_BASE_URL}/transaction/initialize",
            json=payload,
            headers=_headers(),
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        logger.error("Paystack initialize_transaction network error for %s: %s", reference, exc)
        raise PaystackAPIError(f"Could not reach Paystack: {exc}") from exc

    body = _parse_json(resp)
    if not resp.ok or not body.get("status"):
        message = body.get("message", f"HTTP {resp.status_code}")
        logger.error("Paystack initialize_transaction failed for %s: %s", reference, message)
        raise PaystackAPIError(message)

    return body["data"]


def verify_transaction(reference):
    """
    GET /transaction/verify/:reference — the source of truth for a
    transaction's actual state. Used both as a fallback when a webhook
    hasn't arrived yet, and to double-check a webhook's claim before
    trusting it.
    """
    try:
        resp = requests.get(
            f"{PAYSTACK_BASE_URL}/transaction/verify/{reference}",
            headers=_headers(),
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        logger.error("Paystack verify_transaction network error for %s: %s", reference, exc)
        raise PaystackAPIError(f"Could not reach Paystack: {exc}") from exc

    body = _parse_json(resp)
    if not resp.ok or not body.get("status"):
        message = body.get("message", f"HTTP {resp.status_code}")
        logger.error("Paystack verify_transaction failed for %s: %s", reference, message)
        raise PaystackAPIError(message)

    return body["data"]


def verify_webhook_signature(raw_body: bytes, signature_header: str) -> bool:
    """
    Paystack signs webhook bodies with HMAC-SHA512 of the raw request body,
    keyed with your secret key, sent as X-Paystack-Signature. This is the
    ONLY thing that proves a webhook call actually came from Paystack and
    not from anyone who guesses the URL — the payload itself is never
    trusted without this passing first.
    """
    if not signature_header:
        return False
    secret = getattr(settings, "PAYSTACK_SECRET_KEY", "")
    if not secret:
        logger.error("Cannot verify webhook signature — PAYSTACK_SECRET_KEY is not configured.")
        return False
    computed = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(computed, signature_header)


def _parse_json(resp):
    try:
        return resp.json()
    except ValueError:
        # Paystack (or a proxy in front of it) returning non-JSON — e.g. an
        # HTML error page during an outage. Same failure mode the
        # google_client.py quota-exceeded fix in property_intel guards
        # against; don't let a crashed .json() call take down the caller.
        logger.error("Paystack returned a non-JSON response: HTTP %s, body[:200]=%r", resp.status_code, resp.text[:200])
        return {"status": False, "message": f"Non-JSON response from Paystack (HTTP {resp.status_code})"}
