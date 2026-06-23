import logging
import threading

import requests
from django.conf import settings

logger = logging.getLogger("enrichment")

# ---------------------------------------------------------------------------
# Primary: ip-api.com — free, no key needed, 45 req/min
# Gives: country, city, ISP, org (often company name for B2B traffic)
# ---------------------------------------------------------------------------

IP_API_URL = "http://ip-api.com/json/{ip}?fields=status,message,country,countryCode,regionName,city,org,isp,query"


def _lookup_ip_api(ip: str) -> dict:
    try:
        resp = requests.get(IP_API_URL.format(ip=ip), timeout=3)
        data = resp.json()
        if data.get("status") != "success":
            return {}
        org = data.get("org", "") or data.get("isp", "")
        # Strip AS number prefix e.g. "AS15169 Google LLC" -> "Google LLC"
        if org and org.startswith("AS"):
            parts = org.split(" ", 1)
            org = parts[1] if len(parts) > 1 else org
        return {
            "country": data.get("country", ""),
            "country_code": data.get("countryCode", ""),
            "region": data.get("regionName", ""),
            "city": data.get("city", ""),
            "company_name": org,
            "isp": data.get("isp", ""),
        }
    except Exception as exc:
        logger.warning("ip-api lookup failed for %s: %s", ip, exc)
        return {}


# ---------------------------------------------------------------------------
# Optional secondary: Clearbit Reveal — paid, high accuracy for B2B
# Only called if CLEARBIT_API_KEY is set in settings
# ---------------------------------------------------------------------------

def _lookup_clearbit(ip: str) -> dict:
    api_key = getattr(settings, "CLEARBIT_API_KEY", "")
    if not api_key:
        return {}
    try:
        resp = requests.get(
            f"https://reveal.clearbit.com/v1/companies/find?ip={ip}",
            auth=(api_key, ""),
            timeout=5,
        )
        if resp.status_code != 200:
            return {}
        data = resp.json()
        company = data.get("company") or {}
        return {
            "company_name": company.get("name", ""),
            "company_domain": company.get("domain", ""),
            "company_industry": company.get("category", {}).get("industry", ""),
            "company_size": company.get("metrics", {}).get("employeesRange", ""),
            "country": (company.get("geo") or {}).get("country", ""),
            "country_code": (company.get("geo") or {}).get("countryCode", ""),
            "city": (company.get("geo") or {}).get("city", ""),
        }
    except Exception as exc:
        logger.warning("Clearbit lookup failed for %s: %s", ip, exc)
        return {}


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def enrich_visitor(visitor) -> None:
    """
    Enriches a Visitor instance with IP geolocation and company data.
    Tries Clearbit first (if key present), falls back to ip-api.
    Saves only changed fields — safe to call multiple times.
    """
    ip = visitor.ip_address
    if not ip or ip in ("127.0.0.1", "::1"):
        return

    data = _lookup_clearbit(ip) or _lookup_ip_api(ip)
    if not data:
        return

    update_fields = []
    field_map = {
        "company_name": "company_name",
        "company_domain": "company_domain",
        "company_industry": "company_industry",
        "company_size": "company_size",
        "country": "country",
        "country_code": "country_code",
        "city": "city",
        "region": "region",
        "isp": "isp",
    }

    for data_key, model_field in field_map.items():
        value = data.get(data_key, "")
        if value and not getattr(visitor, model_field, ""):
            setattr(visitor, model_field, value)
            update_fields.append(model_field)

    if update_fields:
        update_fields.append("is_enriched")
        visitor.is_enriched = True
        visitor.save(update_fields=update_fields)
        logger.info("Enriched visitor %s: %s", visitor.session_id[:8], data.get("company_name", "unknown"))


def enrich_visitor_async(visitor) -> None:
    """Fire-and-forget enrichment — never blocks the request."""
    t = threading.Thread(target=enrich_visitor, args=(visitor,), daemon=True)
    t.start()