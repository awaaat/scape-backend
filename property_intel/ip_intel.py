"""
property_intel/ip_intel.py

Datacenter/VPN IP detection feeding DeviceFingerprint.is_datacenter_ip /
ip_asn_name (used by fraud.py). Reuses the same free ip-api.com lookup
pattern as enrichment/services.py rather than adding a new dependency or
paid vendor — this only needs a coarse signal, not billing-grade accuracy.
"""
import logging

import requests

logger = logging.getLogger("property_intel")

IP_API_URL = "http://ip-api.com/json/{ip}?fields=status,message,isp,org,proxy,hosting,query"
REQUEST_TIMEOUT_SECONDS = 3

# Checked in addition to ip-api's own 'hosting'/'proxy' flags, in case those
# are ever unavailable on the plan in use — substring match against the
# ISP/org name for well-known cloud/VPN providers.
DATACENTER_ISP_FRAGMENTS = [
    "amazon", "aws", "google cloud", "digitalocean", "linode", "ovh",
    "hetzner", "microsoft azure", "vultr", "contabo", "cloudflare",
    "leaseweb", "choopa", "m247", "psychz",
]


def check_ip_intel(ip):
    """
    Returns (is_datacenter: bool, asn_name: str). Best-effort — returns
    (False, "") on any failure rather than raising, since this is a fraud
    SIGNAL, not something that should ever block a legitimate broker's
    request just because a third-party lookup service is briefly down.
    """
    if not ip or ip in ("127.0.0.1", "::1"):
        return False, ""

    try:
        resp = requests.get(IP_API_URL.format(ip=ip), timeout=REQUEST_TIMEOUT_SECONDS)
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("IP intel lookup failed for %s: %s", ip, exc)
        return False, ""

    if data.get("status") != "success":
        return False, ""

    isp = data.get("isp", "") or data.get("org", "")
    is_datacenter = bool(data.get("hosting")) or bool(data.get("proxy")) or any(
        fragment in isp.lower() for fragment in DATACENTER_ISP_FRAGMENTS
    )
    return is_datacenter, isp
