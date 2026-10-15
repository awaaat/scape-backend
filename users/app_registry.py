"""
users/app_registry.py

One account system, many front-end apps (property, client portal, thesis...).
Everything that needs to know "which app is this person in, and where do we
send them back to?" asks this module. Nothing else hardcodes app names/URLs.

ADDING A NEW APP = add one entry to settings.SCAPE_APPS. No code changes.

Security rule: the client may only ever send an app KEY (e.g. "portal").
It can never send a URL, so signup/reset emails can't be turned into an
open-redirect / phishing vector. URLs always come from server settings.
"""
import logging
from urllib.parse import urlencode, urlparse

from django.conf import settings

logger = logging.getLogger(__name__)

DEFAULT_PATHS = {
    "verify": "/verify-email",
    "reset": "/reset-password",
    "dashboard": "/dashboard",
    "login": "/login",
}

# Used only when settings.SCAPE_APPS is absent, so nothing crashes pre-config.
_BUILTIN_APPS = {
    "property": {"name": "Scape Property Intelligence", "show_free_reports": True},
    "portal": {"name": "Scape Client Portal"},
    "thesis": {"name": "Scape Thesis"},
}


def _registry():
    configured = getattr(settings, "SCAPE_APPS", None)
    if configured:
        return configured
    return {key: dict(cfg) for key, cfg in _BUILTIN_APPS.items()}


def valid_sources():
    return tuple(_registry().keys())


def default_source():
    wanted = getattr(settings, "SCAPE_DEFAULT_APP", "property")
    registry = _registry()
    if wanted in registry:
        return wanted
    return next(iter(registry))


def normalize_source(value):
    if not isinstance(value, str):
        return None
    value = value.strip().lower()
    return value if value in _registry() else None


def app_config(source):
    registry = _registry()
    return registry.get(normalize_source(source) or default_source(), {})


def brand_for(source):
    return app_config(source).get("name") or "Scape"


def show_free_reports(source):
    return bool(app_config(source).get("show_free_reports", False))


def base_url_for(source):
    cfg = app_config(source)
    base = cfg.get("base_url") or getattr(settings, "FRONTEND_BASE_URL", "") or ""
    return base.rstrip("/")


def path_for(source, kind):
    cfg = app_config(source)
    return (cfg.get("paths") or {}).get(kind) or DEFAULT_PATHS[kind]


def build_url(source, kind, **params):
    url = f"{base_url_for(source)}{path_for(source, kind)}"
    return f"{url}?{urlencode(params)}" if params else url


def _origin_of(url):
    try:
        parsed = urlparse(url or "")
    except Exception:
        return ""
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}".lower()


def _origins_for(source):
    cfg = app_config(source)
    origins = {_origin_of(base_url_for(source))}
    origins.update(_origin_of(o) for o in cfg.get("extra_origins", []) or [])
    origins.discard("")
    return origins


def detect_source(request=None, explicit=None):
    """
    Best-effort "which app is calling?" -> a valid key or None (never guesses).
      1. explicit key from the client body (validated against the registry)
      2. the request's Origin / Referer matched against each app's origins
    """
    src = normalize_source(explicit)
    if src:
        return src
    if request is not None:
        for header in ("HTTP_ORIGIN", "HTTP_REFERER"):
            origin = _origin_of(request.META.get(header, ""))
            if not origin:
                continue
            for candidate in valid_sources():
                if origin in _origins_for(candidate):
                    return candidate
    return None


def resolve_source(request=None, explicit=None, fallback=None):
    """detect_source(), else `fallback` (e.g. the account's stored source), else the default app."""
    return detect_source(request, explicit) or normalize_source(fallback) or default_source()
