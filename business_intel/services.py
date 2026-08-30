"""
business_intel/services.py

Two responsibilities, kept together because they're always used
back-to-back: parsing whatever the requester pasted into (lat, lng), and
deciding whether the resulting cell needs fresh Places/isochrone data or
can be served from cache. Actual API calls live in google_client.py --
this file decides WHEN, not HOW.
"""
import re
import logging
from decimal import Decimal, ROUND_HALF_UP

from django.db.models import F

from .models import BusinessLocationCell, BusinessQuery, Requester, compute_geohash

logger = logging.getLogger("business_intel")

KENYA_LAT_BOUNDS = (-5.5, 5.5)
KENYA_LNG_BOUNDS = (33.0, 42.5)

RAW_COORDS_RE = re.compile(r"^\s*(-?\d{1,3}\.\d+)\s*,\s*(-?\d{1,3}\.\d+)\s*$")
GOOGLE_MAPS_AT_RE = re.compile(r"@(-?\d{1,3}\.\d+),(-?\d{1,3}\.\d+)")
GOOGLE_MAPS_QUERY_RE = re.compile(r"[?&]q=(-?\d{1,3}\.\d+),(-?\d{1,3}\.\d+)")

SEVEN_DP = Decimal("0.0000001")


class LocationParseError(ValueError):
    pass


def _in_bounds(lat, lng):
    return KENYA_LAT_BOUNDS[0] <= lat <= KENYA_LAT_BOUNDS[1] and KENYA_LNG_BOUNDS[0] <= lng <= KENYA_LNG_BOUNDS[1]


def _validated_pair(lat_str, lng_str):
    try:
        lat, lng = float(lat_str), float(lng_str)
    except (TypeError, ValueError):
        raise LocationParseError("Coordinates could not be parsed as numbers.")
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        raise LocationParseError("Coordinates are outside valid Earth ranges.")
    if not _in_bounds(lat, lng):
        raise LocationParseError(f"Coordinates ({lat}, {lng}) fall outside the supported region.")

    lat_dec = Decimal(str(lat)).quantize(SEVEN_DP, rounding=ROUND_HALF_UP)
    lng_dec = Decimal(str(lng)).quantize(SEVEN_DP, rounding=ROUND_HALF_UP)
    return lat_dec, lng_dec


def parse_location_input(raw_input):
    """Minimal v1 parser: raw coordinates or a full (non-shortened) Google
    Maps URL."""
    if not raw_input or not raw_input.strip():
        raise LocationParseError("No location provided.")
    text = raw_input.strip()

    m = RAW_COORDS_RE.match(text)
    if m:
        lat, lng = _validated_pair(m.group(1), m.group(2))
        return lat, lng

    at_match = GOOGLE_MAPS_AT_RE.search(text)
    if at_match:
        lat, lng = _validated_pair(at_match.group(1), at_match.group(2))
        return lat, lng

    query_match = GOOGLE_MAPS_QUERY_RE.search(text)
    if query_match:
        lat, lng = _validated_pair(query_match.group(1), query_match.group(2))
        return lat, lng

    raise LocationParseError(
        "Couldn't find a location in that. Try coordinates like '-1.1534, 36.9642' or a Google Maps link."
    )


def get_or_create_location_cell(latitude, longitude):
    cell_geohash = compute_geohash(latitude, longitude)
    cell, created = BusinessLocationCell.objects.get_or_create(
        geohash=cell_geohash,
        defaults={"center_latitude": latitude, "center_longitude": longitude},
    )
    if not created:
        BusinessLocationCell.objects.filter(pk=cell.pk).update(times_reused=F("times_reused") + 1)
        cell.refresh_from_db(fields=["times_reused"])
    return cell, created


def needs_category_enrichment(cell, category):
    return (not cell.has_category_data(category)) or cell.is_stale


def needs_isochrone_enrichment(cell):
    return (not cell.has_isochrones) or cell.is_stale


def get_or_create_requester(email, user=None):
    """Every caller is now an authenticated request (see views.py) --
    this links Requester.user the moment it's created, and backfills it
    if an older row somehow predates the login requirement."""
    requester, _ = Requester.objects.get_or_create(email=email.lower().strip())
    if user is not None and requester.user_id != user.pk:
        requester.user = user
        requester.save(update_fields=["user"])
    return requester


def create_query(raw_location_input, business_category, requester, **extra_fields):
    """High-level entry point for the API view. Does NOT trigger
    enrichment or scoring itself -- that's tasks.py's job."""
    latitude, longitude = parse_location_input(raw_location_input)
    cell, _ = get_or_create_location_cell(latitude, longitude)

    query = BusinessQuery.objects.create(
        raw_location_input=raw_location_input,
        latitude=latitude,
        longitude=longitude,
        location_cell=cell,
        business_category=business_category,
        requester=requester,
        **extra_fields,
    )
    logger.info("BusinessQuery created: %s (%s) -> cell %s", query.id, business_category, cell.geohash)
    return query, cell
