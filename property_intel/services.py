"""
property_intel/services.py

Two responsibilities, deliberately kept together because they're always
used back-to-back:

  1. PARSING/SANITIZING whatever a broker pasted — coordinates, a full
     Google Maps link, a shortened maps.app.goo.gl link, or a WhatsApp
     shared-location link — into a validated (latitude, longitude) pair.

  2. CACHE ORCHESTRATION — deciding whether that location already has a
     complete, fresh LocationCell (no Google calls needed) or whether it
     needs enrichment (handled by google_client.py).

Actual Google API calls live in property_intel/google_client.py, not here.
This file never imports Maps-specific request logic directly — it decides
WHEN enrichment is needed, google_client.py handles HOW to fetch it.

--------------------------------------------------------------------------
CHANGELOG:
  - Added merge_brokers_for_user(): bridges an anonymous Broker record
    (created off a DeviceFingerprint, no login involved) to a real User
    once that person signs up/logs in on a DIFFERENT device than the one
    they used the free tier on. See its docstring for why this is safe to
    call on every login and does NOT touch free-tier allowance.
--------------------------------------------------------------------------
"""
import re
import logging

import requests
from django.db.models import F

from .models import (
    Broker,
    LocationCell,
    PropertyPin,
    compute_geohash,
)

logger = logging.getLogger("property_intel")

# ---------------------------------------------------------------------------
# Sanity bounds — rough bounding box for Kenya, with generous padding.
# Purpose is NOT precise border checking (that's not our job), it's catching
# obvious garbage/abuse: swapped lat/lng, a stray "0,0", coordinates for a
# completely different continent pasted by accident or maliciously.
# ---------------------------------------------------------------------------
KENYA_LAT_BOUNDS = (-5.5, 5.5)
KENYA_LNG_BOUNDS = (33.0, 42.5)

# Domains we trust enough to follow a redirect on. Short links are resolved
# server-side via a HEAD request — never trust a redirect target blindly,
# and never let this function be used as an open URL-fetching proxy.
TRUSTED_SHORT_LINK_HOSTS = {"maps.app.goo.gl", "goo.gl"}
TRUSTED_RESOLVED_HOSTS = {"www.google.com", "maps.google.com", "google.com"}

SHORT_LINK_TIMEOUT_SECONDS = 5
MAX_REDIRECTS = 5

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# Raw "lat,lng" or "lat, lng" — e.g. "-1.153472, 36.964281"
RAW_COORDS_RE = re.compile(
    r"^\s*(-?\d{1,3}\.\d+)\s*,\s*(-?\d{1,3}\.\d+)\s*$"
)

# Coordinates embedded anywhere in text (WhatsApp forwards, captions, etc.)
EMBEDDED_COORDS_RE = re.compile(
    r"(-?\d{1,3}\.\d{3,})\s*,\s*(-?\d{1,3}\.\d{3,})"
)

# Standard Google Maps URL patterns:
#   https://www.google.com/maps/@-1.153472,36.964281,15z
#   https://maps.google.com/?q=-1.153472,36.964281
#   https://www.google.com/maps/place/.../@-1.153,36.964,17z/...
GOOGLE_MAPS_AT_RE = re.compile(r"@(-?\d{1,3}\.\d+),(-?\d{1,3}\.\d+)")
GOOGLE_MAPS_QUERY_RE = re.compile(r"[?&]q=(-?\d{1,3}\.\d+),(-?\d{1,3}\.\d+)")

SHORT_LINK_RE = re.compile(r"https?://(?:www\.)?(maps\.app\.goo\.gl|goo\.gl)/\S+", re.IGNORECASE)

# Google Plus Code (Open Location Code) -- e.g. "XV3W+VC8 Kiganjo Road, Ruiru"
# or the globally-unique long form "6GCRMQQH+W7". Requires a Geocoding API
# call since a short code alone (without the locality text) isn't decodable
# offline -- unlike raw coordinates, this always costs one API call.
PLUS_CODE_RE = re.compile(r"\b[23456789CFGHJMPQRVWX]{4,8}\+[23456789CFGHJMPQRVWX]{2,3}\b", re.IGNORECASE)

GEOCODE_TIMEOUT_SECONDS = 10


class LocationParseError(ValueError):
    """Raised when input can't be confidently resolved to a valid Kenyan-region coordinate pair."""


def _in_bounds(lat, lng):
    return KENYA_LAT_BOUNDS[0] <= lat <= KENYA_LAT_BOUNDS[1] and KENYA_LNG_BOUNDS[0] <= lng <= KENYA_LNG_BOUNDS[1]


def _validated_pair(lat_str, lng_str):
    try:
        lat, lng = float(lat_str), float(lng_str)
    except (TypeError, ValueError):
        raise LocationParseError("Coordinates could not be parsed as numbers.")

    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        raise LocationParseError("Coordinates are outside valid Earth ranges — likely swapped or corrupted.")

    if not _in_bounds(lat, lng):
        raise LocationParseError(
            f"Coordinates ({lat}, {lng}) fall outside the supported region. "
            "If this is a genuine Kenyan location, please report it."
        )

    return round(lat, 7), round(lng, 7)


def _resolve_short_link(url):
    """
    Follows a maps.app.goo.gl / goo.gl redirect server-side to get the real
    Google Maps URL. Refuses to follow to anywhere outside a small allowlist
    of Google hosts — this endpoint should never become a generic "fetch any
    URL the user gives us" proxy.
    """
    try:
        resp = requests.head(
            url, allow_redirects=True, timeout=SHORT_LINK_TIMEOUT_SECONDS
        )
    except requests.RequestException as exc:
        logger.warning("Short link resolution failed for %s: %s", url, exc)
        raise LocationParseError("Could not resolve the shortened Maps link — it may be invalid or expired.")

    final_url = resp.url
    from urllib.parse import urlparse
    host = urlparse(final_url).netloc.lower()

    if not any(host == h or host.endswith("." + h) for h in TRUSTED_RESOLVED_HOSTS):
        logger.warning("Short link resolved to untrusted host: %s (from %s)", host, url)
        raise LocationParseError("This link did not resolve to a Google Maps location.")

    if len(resp.history) > MAX_REDIRECTS:
        raise LocationParseError("Too many redirects — link looks malformed.")

    return final_url


def _resolve_plus_code(full_text):
    """
    Resolves a Plus Code (with its locality text, e.g. the whole
    "XV3W+VC8 Kiganjo Road, Ruiru" string) via the Geocoding API -- a plus
    code alone can't be decoded offline without the area context Google
    supplies. Logged as a real geocoding API call (see google_client.py's
    ESTIMATED_COST_USD) since, unlike raw coordinates, this always costs
    something to resolve.
    """
    from .google_client import GOOGLE_API_KEY, _log_call

    params = {"address": full_text, "region": "ke", "key": GOOGLE_API_KEY}
    try:
        resp = requests.get(
            "https://maps.googleapis.com/maps/api/geocode/json",
            params=params, timeout=GEOCODE_TIMEOUT_SECONDS,
        )
        data = resp.json()
        succeeded = resp.status_code == 200 and data.get("status") == "OK"
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Plus Code geocoding failed for %r: %s", full_text, exc)
        _log_call("geocoding", None, {"address": full_text}, None, False)
        raise LocationParseError("Could not resolve that Plus Code — please check it or try a different format.")

    _log_call("geocoding", None, {"address": full_text}, resp.status_code, succeeded)

    if not succeeded or not data.get("results"):
        raise LocationParseError("Could not resolve that Plus Code — please check it or try a different format.")

    loc = data["results"][0]["geometry"]["location"]
    return _validated_pair(loc["lat"], loc["lng"])


def parse_location_input(raw_input):
    """
    Main entry point. Takes whatever the broker pasted and returns
    (latitude, longitude, input_type). Raises LocationParseError with a
    user-facing message on anything it can't confidently resolve.

    Order matters: cheapest/most specific checks first, so a raw coordinate
    pair never has to go through URL parsing or a network call.
    """
    if not raw_input or not raw_input.strip():
        raise LocationParseError("Please paste a location — a pin, coordinates, or a Maps link.")

    text = raw_input.strip()

    # 1. Raw "lat,lng" — the whole input is just coordinates
    m = RAW_COORDS_RE.match(text)
    if m:
        lat, lng = _validated_pair(m.group(1), m.group(2))
        return lat, lng, "coordinates"

    # 2. Shortened Google Maps link — resolve first, then re-run parsing on the real URL
    short_match = SHORT_LINK_RE.search(text)
    if short_match:
        resolved_url = _resolve_short_link(short_match.group(0))
        at_match = GOOGLE_MAPS_AT_RE.search(resolved_url)
        if at_match:
            lat, lng = _validated_pair(at_match.group(1), at_match.group(2))
            return lat, lng, "google_maps_short_link"
        query_match = GOOGLE_MAPS_QUERY_RE.search(resolved_url)
        if query_match:
            lat, lng = _validated_pair(query_match.group(1), query_match.group(2))
            return lat, lng, "google_maps_short_link"
        raise LocationParseError("The Maps link resolved, but no coordinates could be found in it.")

    # 3. Full Google Maps URL — @lat,lng or ?q=lat,lng
    at_match = GOOGLE_MAPS_AT_RE.search(text)
    if at_match:
        lat, lng = _validated_pair(at_match.group(1), at_match.group(2))
        return lat, lng, "google_maps_link"

    query_match = GOOGLE_MAPS_QUERY_RE.search(text)
    if query_match:
        lat, lng = _validated_pair(query_match.group(1), query_match.group(2))
        return lat, lng, "google_maps_link"

    # 3b. Plus Code (Open Location Code) -- e.g. "XV3W+VC8 Kiganjo Road, Ruiru".
    #     Checked after full Maps URLs (a URL could coincidentally contain a
    #     plus-shaped substring) but before the last-resort embedded-digits
    #     fallback, since a plus code's locality text could otherwise get
    #     misread as containing raw coordinates.
    if PLUS_CODE_RE.search(text):
        lat, lng = _resolve_plus_code(text)
        return lat, lng, "plus_code"

    # 4. WhatsApp forwards / captions with coordinates embedded in free text.
    #    WhatsApp's "Live Location" / "Send Location" share almost always
    #    comes through as a maps.google.com link (caught above) or, when
    #    copy-pasted as text, as raw coordinates somewhere in the message —
    #    caught here as a last resort, deliberately AFTER the stricter
    #    patterns above so we don't misfire on, say, a phone number.
    embedded_match = EMBEDDED_COORDS_RE.search(text)
    if embedded_match:
        try:
            lat, lng = _validated_pair(embedded_match.group(1), embedded_match.group(2))
            return lat, lng, "whatsapp_location"
        except LocationParseError:
            pass  # fall through to final rejection

    raise LocationParseError(
        "Couldn't find a location in that. Try pasting a Google Maps link, "
        "a WhatsApp shared location, or coordinates like '-1.1534, 36.9642'."
    )


# ---------------------------------------------------------------------------
# Cache orchestration
# ---------------------------------------------------------------------------

def get_or_create_location_cell(latitude, longitude):
    """
    Looks up the geohash cell for these coordinates. Returns
    (cell, was_newly_created). If the cell already existed, bumps
    times_reused so cache ROI is visible in admin without extra queries.
    """
    cell_geohash = compute_geohash(latitude, longitude)

    cell, created = LocationCell.objects.get_or_create(
        geohash=cell_geohash,
        defaults={
            "center_latitude": latitude,
            "center_longitude": longitude,
        },
    )

    if not created:
        LocationCell.objects.filter(pk=cell.pk).update(times_reused=F("times_reused") + 1)
        cell.refresh_from_db(fields=["times_reused"])

    return cell, created


def needs_enrichment(cell):
    """
    True if this cell has never been fully enriched, or its data has gone
    stale (see LocationCell.is_stale). This is the single gate that decides
    whether google_client.py gets called at all for a given pin.
    """
    return (not cell.has_complete_data) or cell.is_stale


def create_pin(raw_input, broker, submitted_by=""):
    """
    High-level entry point for the API view: parse the broker's input,
    resolve (or create) its LocationCell, and save a PropertyPin recording
    exactly what was asked for and whether it was a cache hit.

    Does NOT trigger Google enrichment itself — that's the caller's job
    (typically a view or a background task), using needs_enrichment(cell)
    to decide, and google_client.py to actually do it.

    NOTE: signature takes `broker` (a Broker instance) now, not a free
    `submitted_by` string — PropertyPin.broker is a required FK, so the
    pin has to be attached to a real Broker record at creation time, not
    left dangling for a view to patch on afterwards.
    """
    latitude, longitude, input_type = parse_location_input(raw_input)
    cell, cell_created = get_or_create_location_cell(latitude, longitude)

    was_cache_hit = (not cell_created) and cell.has_complete_data and not cell.is_stale

    pin = PropertyPin.objects.create(
        raw_input=raw_input,
        input_type=input_type,
        latitude=latitude,
        longitude=longitude,
        location_cell=cell,
        was_cache_hit=was_cache_hit,
        broker=broker,
    )

    logger.info(
        "Pin created: %s (%s, %s) → cell %s [%s]",
        pin.id, latitude, longitude, cell.geohash,
        "CACHE HIT" if was_cache_hit else "NEEDS ENRICHMENT",
    )

    return pin, cell


# ---------------------------------------------------------------------------
# Anonymous → authenticated identity bridging
# ---------------------------------------------------------------------------

def merge_brokers_for_user(user):
    """
    Attaches any anonymous Broker record(s) matching this user's email to
    their auth User account. Called on every successful LOGIN (not signup —
    see why below).

    Why login, not signup:
        At signup, a person has only TYPED an email — that proves nothing.
        Merging there would let anyone claim a stranger's report history
        (and anything derived from it, e.g. a wallet credit tied to a paid
        report) just by entering that person's email on the signup form.
        At login, a correct password has already been verified, which is
        real proof of ownership. So this must run post-authenticate(),
        never post-signup-form-submit.

    Why this can't be abused to farm extra free reports:
        This function ONLY reassigns Broker.user. It never touches
        DeviceFingerprint.free_reports_remaining, never creates a Broker,
        and never resets any fraud/suspicion state. The free-tier gate
        lives entirely on DeviceFingerprint, keyed by fingerprint_hash —
        logging in on ten different emails does not grant a single extra
        free report anywhere. This function is purely cosmetic/UX
        (history visibility), not an allowance mechanism.

    Why `user__isnull=True` matters:
        Broker.email and the auth User's email/username are both unique,
        so under normal operation at most one Broker can ever match an
        email, and it can only ever belong to nobody or to this exact
        user already. The filter is still here as defense in depth: it
        guarantees this function can NEVER reassign a Broker away from a
        different, already-linked user, even if some future code path
        breaks that uniqueness assumption.

    Idempotent and cheap: safe to call unconditionally on every login,
    not just the first one after signup (covers "signed up on desktop
    months ago, but only just started using mobile anonymously before
    logging in there too").
    """
    updated = Broker.objects.filter(
        email__iexact=user.email, user__isnull=True
    ).update(user=user)

    if updated:
        logger.info(
            "Login merge: attached %d anonymous Broker record(s) to user %s (%s)",
            updated, user.pk, user.email,
        )

    return updated