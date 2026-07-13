"""
property_intel/google_client.py

All actual Google Maps Platform calls live here — nothing else in the app
talks to Google directly. Every call:
  1. Logs an APICallLog row FIRST (billing note: Google charges per request
     received, success or not — so failures are logged too, not swallowed).
  2. Writes results onto the LocationCell.
  3. Never lets one failing API break the whole enrichment run — each
     fetch_* function catches its own exceptions so a broker still gets a
     partial report (e.g. missing air quality) rather than nothing.

Image handling: Google's Static Maps / Street View URLs embed your API key
as a query param. We NEVER store or hand back a raw Google URL — that would
leak the key to anyone who opens the PDF or inspects network traffic. We
fetch the image bytes server-side and re-upload to our own storage
(property_intel/storage.py), then store OUR url on the cell.
"""
import logging
from datetime import timedelta

import requests
from django.conf import settings
from django.utils import timezone

from .models import APICallLog, LocationCell

logger = logging.getLogger("property_intel")

GOOGLE_API_KEY = getattr(settings, "GOOGLE_MAPS_API_KEY", "")
REQUEST_TIMEOUT_SECONDS = 10
AQI_GOOD_THRESHOLD_FOR_STREAK = 50

# Nairobi CBD is kept as a deliberate, fixed national-relevance anchor
# (used directly in accessibility scoring and the report summary) -- this
# is NOT the "nearest town" logic, which is now fully dynamic (see
# kenya_towns.py / fetch_nearest_towns below) rather than a fixed list.
NAIROBI_CBD_LAT, NAIROBI_CBD_LNG = -1.286389, 36.817223
MAX_NEAREST_TOWNS = 5

# Estimated per-call costs (USD) for internal cost tracking only — these are
# NOT authoritative billing figures. Google Cloud's own billing dashboard is
# the source of truth; this is just what lets APICallLog answer "roughly
# how much did this report cost us" without a manual lookup every time.
ESTIMATED_COST_USD = {
    "geocoding": 0.005,
    "maps_static": 0.002,
    "street_view_metadata": 0.0,  # metadata check is free — always call before billing for the image
    "street_view_static": 0.007,
    "places_nearby": 0.032,
    "routes": 0.005,
    "air_quality": 0.005,
    "air_quality_history": 0.005,
    "air_quality_forecast": 0.005,
    "elevation": 0.005,
    "routes_transit": 0.005,
    "roads": 0.005,
    "places_text": 0.032,
    "places_photo": 0.007,
}

PLACE_CATEGORIES = {
    "nearby_schools": "school",
    "nearby_universities": "university",
    "nearby_hospitals": "hospital",
    "nearby_banks": "bank",
    "nearby_petrol_stations": "gas_station",
    "nearby_shopping": "shopping_mall",
    "nearby_supermarkets": "supermarket",
    "nearby_restaurants": "restaurant",
    "nearby_police_stations": "police",
    "nearby_fire_stations": "fire_station",
    "nearby_pharmacies": "pharmacy",
    "nearby_transit_stops": "transit_station",
    "nearby_parks": "park",
    "nearby_ev_charging": "electric_vehicle_charging_station",
}


def _log_call(api, location_cell, request_params, status_code, succeeded):
    APICallLog.objects.create(
        api=api,
        location_cell=location_cell,
        request_params=request_params,
        response_status_code=status_code,
        succeeded=succeeded,
        estimated_cost_usd=ESTIMATED_COST_USD.get(api, 0),
    )


class EnrichmentStepFailed(Exception):
    """Raised internally, always caught by the orchestrator — lets each
    fetch_* function fail loudly in its own scope without a bare except."""


# ---------------------------------------------------------------------------
# Geocoding
# ---------------------------------------------------------------------------

def geocode_cell(cell: LocationCell):
    params = {
        "latlng": f"{cell.center_latitude},{cell.center_longitude}",
        "key": GOOGLE_API_KEY,
    }
    try:
        resp = requests.get(
            "https://maps.googleapis.com/maps/api/geocode/json",
            params=params, timeout=REQUEST_TIMEOUT_SECONDS,
        )
        data = resp.json()
        succeeded = resp.status_code == 200 and data.get("status") == "OK"
    except requests.RequestException as exc:
        logger.warning("Geocoding failed for cell %s: %s", cell.geohash, exc)
        _log_call("geocoding", cell, {"latlng": params["latlng"]}, None, False)
        raise EnrichmentStepFailed("geocoding") from exc
    except ValueError as exc:
        # resp.json() raised — Google returned something that isn't JSON
        # (quota-exceeded HTML pages are the usual culprit). Treated the
        # same as a network failure: this step failed, others still run.
        logger.warning("Geocoding returned non-JSON for cell %s: %s", cell.geohash, exc)
        _log_call("geocoding", cell, {"latlng": params["latlng"]}, getattr(resp, "status_code", None), False)
        raise EnrichmentStepFailed("geocoding: non-JSON response") from exc

    _log_call("geocoding", cell, {"latlng": params["latlng"]}, resp.status_code, succeeded)

    if not succeeded:
        raise EnrichmentStepFailed(f"geocoding returned status={data.get('status')}")

    result = data["results"][0]
    cell.formatted_address = result.get("formatted_address", "")
    cell.geocode_raw_response = data
    cell.save(update_fields=["formatted_address", "geocode_raw_response"])
    return cell


# ---------------------------------------------------------------------------
# Imagery — Satellite (Maps Static API)
# ---------------------------------------------------------------------------

def fetch_satellite_image(cell: LocationCell):
    from . import storage  # local import: storage.py is the next file, keeps this module importable before it exists

    params = {
        "center": f"{cell.center_latitude},{cell.center_longitude}",
        "zoom": "17",
        "size": "640x400",
        "maptype": "satellite",
        "key": GOOGLE_API_KEY,
    }
    try:
        resp = requests.get(
            "https://maps.googleapis.com/maps/api/staticmap",
            params=params, timeout=REQUEST_TIMEOUT_SECONDS,
        )
        succeeded = resp.status_code == 200 and resp.headers.get("Content-Type", "").startswith("image/")
    except requests.RequestException as exc:
        logger.warning("Satellite image fetch failed for cell %s: %s", cell.geohash, exc)
        _log_call("maps_static", cell, {"center": params["center"]}, None, False)
        raise EnrichmentStepFailed("maps_static") from exc

    _log_call("maps_static", cell, {"center": params["center"]}, resp.status_code, succeeded)

    if not succeeded:
        raise EnrichmentStepFailed(f"maps_static returned status={resp.status_code}")

    stored_url = storage.upload_image_bytes(
        resp.content, path=f"location-cells/{cell.geohash}/satellite.jpg", content_type="image/jpeg"
    )
    cell.satellite_image_url = stored_url
    cell.satellite_image_fetched_at = timezone.now()
    cell.save(update_fields=["satellite_image_url", "satellite_image_fetched_at"])
    return cell


# ---------------------------------------------------------------------------
# Imagery — Street View (metadata check first — it's free — THEN the image)
# ---------------------------------------------------------------------------

def check_street_view_availability(cell: LocationCell):
    params = {
        "location": f"{cell.center_latitude},{cell.center_longitude}",
        "key": GOOGLE_API_KEY,
    }
    try:
        resp = requests.get(
            "https://maps.googleapis.com/maps/api/streetview/metadata",
            params=params, timeout=REQUEST_TIMEOUT_SECONDS,
        )
        data = resp.json()
    except requests.RequestException as exc:
        logger.warning("Street View metadata check failed for cell %s: %s", cell.geohash, exc)
        _log_call("street_view_metadata", cell, {"location": params["location"]}, None, False)
        raise EnrichmentStepFailed("street_view_metadata") from exc
    except ValueError as exc:
        logger.warning("Street View metadata returned non-JSON for cell %s: %s", cell.geohash, exc)
        _log_call("street_view_metadata", cell, {"location": params["location"]}, getattr(resp, "status_code", None), False)
        raise EnrichmentStepFailed("street_view_metadata: non-JSON response") from exc

    _log_call("street_view_metadata", cell, {"location": params["location"]}, resp.status_code, True)

    available = data.get("status") == "OK"
    cell.street_view_available = available
    if available:
        cell.street_view_pano_id = data.get("pano_id", "")
    cell.save(update_fields=["street_view_available", "street_view_pano_id"])
    return available


def fetch_street_view_image(cell: LocationCell):
    from . import storage

    if not cell.street_view_available:
        # Caller should have run check_street_view_availability first — this
        # guards against accidentally billing for an image that doesn't exist.
        return cell

    params = {
        "location": f"{cell.center_latitude},{cell.center_longitude}",
        "size": "640x400",
        "fov": "90",
        "key": GOOGLE_API_KEY,
    }
    try:
        resp = requests.get(
            "https://maps.googleapis.com/maps/api/streetview",
            params=params, timeout=REQUEST_TIMEOUT_SECONDS,
        )
        succeeded = resp.status_code == 200 and resp.headers.get("Content-Type", "").startswith("image/")
    except requests.RequestException as exc:
        logger.warning("Street View image fetch failed for cell %s: %s", cell.geohash, exc)
        _log_call("street_view_static", cell, {"location": params["location"]}, None, False)
        raise EnrichmentStepFailed("street_view_static") from exc

    _log_call("street_view_static", cell, {"location": params["location"]}, resp.status_code, succeeded)

    if not succeeded:
        raise EnrichmentStepFailed(f"street_view_static returned status={resp.status_code}")

    stored_url = storage.upload_image_bytes(
        resp.content, path=f"location-cells/{cell.geohash}/street_view.jpg", content_type="image/jpeg"
    )
    cell.street_view_image_url = stored_url
    cell.street_view_fetched_at = timezone.now()
    cell.save(update_fields=["street_view_image_url", "street_view_fetched_at"])
    return cell


# ---------------------------------------------------------------------------
# Nearby amenities — Places API (New)
# ---------------------------------------------------------------------------

PLACES_FIELD_MASK = (
    "places.displayName,places.location,places.rating,places.id,"
    "places.userRatingCount,places.priceLevel,places.regularOpeningHours,places.businessStatus,"
    "places.photos"
)


def _search_nearby(cell: LocationCell, included_type):
    body = {
        "includedTypes": [included_type],
        "maxResultCount": 20,
        "locationRestriction": {
            "circle": {
                "center": {"latitude": float(cell.center_latitude), "longitude": float(cell.center_longitude)},
                "radius": 5000.0,
            }
        },
    }
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": GOOGLE_API_KEY,
        "X-Goog-FieldMask": PLACES_FIELD_MASK,
    }
    try:
        resp = requests.post(
            "https://places.googleapis.com/v1/places:searchNearby",
            json=body, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS,
        )
        succeeded = resp.status_code == 200
        data = resp.json() if succeeded else {}
    except requests.RequestException as exc:
        logger.warning("Places nearby search failed for cell %s (%s): %s", cell.geohash, included_type, exc)
        _log_call("places_nearby", cell, {"type": included_type}, None, False)
        raise EnrichmentStepFailed("places_nearby") from exc

    _log_call("places_nearby", cell, {"type": included_type}, resp.status_code, succeeded)

    if not succeeded:
        raise EnrichmentStepFailed(f"places_nearby ({included_type}) returned status={resp.status_code}")

    results = []
    center_lat, center_lng = float(cell.center_latitude), float(cell.center_longitude)
    for place in data.get("places", []):
        loc = place.get("location", {})
        hours = place.get("regularOpeningHours", {}) or {}
        results.append({
            "name": place.get("displayName", {}).get("text", ""),
            "place_id": place.get("id", ""),
            "lat": loc.get("latitude"),
            "lng": loc.get("longitude"),
            "rating": place.get("rating"),
            "user_rating_count": place.get("userRatingCount"),
            "price_level": place.get("priceLevel"),
            "business_status": place.get("businessStatus", ""),
            "open_now": hours.get("openNow"),
            "photo_name": (place.get("photos") or [{}])[0].get("name"),
            "distance_m": _haversine_m(center_lat, center_lng, loc.get("latitude"), loc.get("longitude")),
        })
    results.sort(key=lambda r: r["distance_m"] if r["distance_m"] is not None else float("inf"))
    return results


def fetch_nearby_amenities(cell: LocationCell):
    update_fields = []
    for field_name, place_type in PLACE_CATEGORIES.items():
        try:
            setattr(cell, field_name, _search_nearby(cell, place_type))
            update_fields.append(field_name)
        except EnrichmentStepFailed:
            # One category failing (e.g. no banks nearby is a valid empty
            # result, but a genuine API error) shouldn't block the others.
            continue

    cell.amenities_fetched_at = timezone.now()
    update_fields.append("amenities_fetched_at")
    cell.save(update_fields=update_fields)
    return cell


def _haversine_m(lat1, lng1, lat2, lng2):
    """Straight-line distance in meters — good enough for report display
    and sorting; NOT a substitute for Routes API driving distance."""
    if lat2 is None or lng2 is None:
        return None
    from math import radians, sin, cos, sqrt, atan2
    R = 6371000
    phi1, phi2 = radians(lat1), radians(lat2)
    dphi = radians(lat2 - lat1)
    dlambda = radians(lng2 - lng1)
    a = sin(dphi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(dlambda / 2) ** 2
    return round(R * 2 * atan2(sqrt(a), sqrt(1 - a)))


# ---------------------------------------------------------------------------
# Air Quality
# ---------------------------------------------------------------------------

def fetch_air_quality(cell: LocationCell):
    body = {
        "location": {"latitude": float(cell.center_latitude), "longitude": float(cell.center_longitude)},
    }
    headers = {"Content-Type": "application/json", "X-Goog-Api-Key": GOOGLE_API_KEY}
    try:
        resp = requests.post(
            "https://airquality.googleapis.com/v1/currentConditions:lookup",
            json=body, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS,
        )
        succeeded = resp.status_code == 200
        data = resp.json() if succeeded else {}
    except requests.RequestException as exc:
        logger.warning("Air quality fetch failed for cell %s: %s", cell.geohash, exc)
        _log_call("air_quality", cell, {}, None, False)
        raise EnrichmentStepFailed("air_quality") from exc

    _log_call("air_quality", cell, {}, resp.status_code, succeeded)

    if not succeeded:
        raise EnrichmentStepFailed(f"air_quality returned status={resp.status_code}")

    indexes = data.get("indexes", [])
    primary = indexes[0] if indexes else {}

    cell.air_quality_raw_response = data
    cell.air_quality_index = primary.get("aqi")
    cell.air_quality_category = primary.get("category", "")
    cell.air_quality_fetched_at = timezone.now()
    cell.save(update_fields=["air_quality_raw_response", "air_quality_index", "air_quality_category", "air_quality_fetched_at"])
    return cell


def fetch_air_quality_history(cell: LocationCell):
    """Up to 30 days of daily AQI -- turns 'AQI is 42 today' (one lucky
    reading) into 'AQI has stayed good for the last N days' (a trend)."""
    body = {
        "location": {"latitude": float(cell.center_latitude), "longitude": float(cell.center_longitude)},
        "period": {"startTime": (timezone.now() - timedelta(days=30)).isoformat(), "endTime": timezone.now().isoformat()},
        "pageSize": 30,
    }
    headers = {"Content-Type": "application/json", "X-Goog-Api-Key": GOOGLE_API_KEY}
    try:
        resp = requests.post(
            "https://airquality.googleapis.com/v1/history:lookup",
            json=body, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS,
        )
        succeeded = resp.status_code == 200
        data = resp.json() if succeeded else {}
    except requests.RequestException as exc:
        logger.warning("Air quality history fetch failed for cell %s: %s", cell.geohash, exc)
        _log_call("air_quality_history", cell, {}, None, False)
        raise EnrichmentStepFailed("air_quality_history") from exc

    _log_call("air_quality_history", cell, {}, resp.status_code, succeeded)
    if not succeeded:
        raise EnrichmentStepFailed(f"air_quality_history returned status={resp.status_code}")

    good_streak = 0
    for hour_info in data.get("hoursInfo", []):
        indexes = hour_info.get("indexes", [])
        aqi = indexes[0].get("aqi") if indexes else None
        if aqi is not None and aqi <= AQI_GOOD_THRESHOLD_FOR_STREAK:
            good_streak += 1
        else:
            break

    cell.air_quality_history_raw = data
    cell.air_quality_good_days_streak = good_streak // 24 if good_streak else 0
    cell.air_quality_history_fetched_at = timezone.now()
    cell.save(update_fields=["air_quality_history_raw", "air_quality_good_days_streak", "air_quality_history_fetched_at"])
    return cell


def fetch_air_quality_forecast(cell: LocationCell):
    """Next 96h forecast -- lets the report say conditions are staying
    stable/improving rather than only reporting a single past snapshot."""
    body = {
        "location": {"latitude": float(cell.center_latitude), "longitude": float(cell.center_longitude)},
        "period": {"startTime": timezone.now().isoformat(), "endTime": (timezone.now() + timedelta(hours=96)).isoformat()},
        "pageSize": 96,
    }
    headers = {"Content-Type": "application/json", "X-Goog-Api-Key": GOOGLE_API_KEY}
    try:
        resp = requests.post(
            "https://airquality.googleapis.com/v1/forecast:lookup",
            json=body, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS,
        )
        succeeded = resp.status_code == 200
        data = resp.json() if succeeded else {}
    except requests.RequestException as exc:
        logger.warning("Air quality forecast fetch failed for cell %s: %s", cell.geohash, exc)
        _log_call("air_quality_forecast", cell, {}, None, False)
        raise EnrichmentStepFailed("air_quality_forecast") from exc

    _log_call("air_quality_forecast", cell, {}, resp.status_code, succeeded)
    if not succeeded:
        raise EnrichmentStepFailed(f"air_quality_forecast returned status={resp.status_code}")

    cell.air_quality_forecast_raw = data
    cell.air_quality_forecast_fetched_at = timezone.now()
    cell.save(update_fields=["air_quality_forecast_raw", "air_quality_forecast_fetched_at"])
    return cell


# ---------------------------------------------------------------------------
# Travel times — Routes API
# ---------------------------------------------------------------------------

def fetch_travel_times(cell: LocationCell):
    travel_times = dict(cell.travel_times or {})

    body = {
        "origin": {"location": {"latLng": {"latitude": float(cell.center_latitude), "longitude": float(cell.center_longitude)}}},
        "destination": {"location": {"latLng": {"latitude": NAIROBI_CBD_LAT, "longitude": NAIROBI_CBD_LNG}}},
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_AWARE",
    }
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": GOOGLE_API_KEY,
        "X-Goog-FieldMask": "routes.duration,routes.distanceMeters",
    }
    try:
        resp = requests.post(
            "https://routes.googleapis.com/directions/v2:computeRoutes",
            json=body, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS,
        )
        succeeded = resp.status_code == 200
        data = resp.json() if succeeded else {}
    except requests.RequestException as exc:
        logger.warning("Routes fetch failed for cell %s → nairobi_cbd: %s", cell.geohash, exc)
        _log_call("routes", cell, {"destination": "nairobi_cbd"}, None, False)
        data, succeeded = {}, False
    else:
        _log_call("routes", cell, {"destination": "nairobi_cbd"}, resp.status_code, succeeded)

    if succeeded and data.get("routes"):
        route = data["routes"][0]
        duration_s = int(str(route.get("duration", "0s")).rstrip("s"))
        travel_times["nairobi_cbd"] = {
            "duration_s": duration_s,
            "distance_m": route.get("distanceMeters"),
        }

    # One extra TRANSIT-mode call, CBD only -- "has public transit access"
    # is sellable but not worth extra Routes calls for towns nobody asks about.
    transit_body = {
        "origin": {"location": {"latLng": {"latitude": float(cell.center_latitude), "longitude": float(cell.center_longitude)}}},
        "destination": {"location": {"latLng": {"latitude": NAIROBI_CBD_LAT, "longitude": NAIROBI_CBD_LNG}}},
        "travelMode": "TRANSIT",
    }
    transit_headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": GOOGLE_API_KEY,
        "X-Goog-FieldMask": "routes.duration,routes.distanceMeters",
    }
    try:
        resp = requests.post(
            "https://routes.googleapis.com/directions/v2:computeRoutes",
            json=transit_body, headers=transit_headers, timeout=REQUEST_TIMEOUT_SECONDS,
        )
        succeeded = resp.status_code == 200
        data = resp.json() if succeeded else {}
    except requests.RequestException as exc:
        logger.warning("Transit routes fetch failed for cell %s: %s", cell.geohash, exc)
        _log_call("routes_transit", cell, {"destination": "nairobi_cbd"}, None, False)
        succeeded, data = False, {}
    else:
        _log_call("routes_transit", cell, {"destination": "nairobi_cbd"}, resp.status_code, succeeded)

    if succeeded and data.get("routes"):
        route = data["routes"][0]
        duration_s = int(str(route.get("duration", "0s")).rstrip("s"))
        travel_times.setdefault("nairobi_cbd", {})["transit_duration_s"] = duration_s
        travel_times["nairobi_cbd"]["has_transit"] = True
    else:
        travel_times.setdefault("nairobi_cbd", {})["has_transit"] = False

    cell.travel_times = travel_times
    cell.travel_times_fetched_at = timezone.now()
    cell.save(update_fields=["travel_times", "travel_times_fetched_at"])
    return cell


# ---------------------------------------------------------------------------
# Nearest towns — dynamic, Kenya-wide (see property_intel/kenya_towns.py).
# Replaces the old fixed satellite-town list: works correctly anywhere in
# Kenya, not just Nairobi's commuter belt.
# ---------------------------------------------------------------------------

def fetch_nearest_towns(cell: LocationCell):
    """
    Finds up to MAX_NEAREST_TOWNS nearest towns via haversine (free, pure
    math against kenya_towns.py's reference list), then confirms real drive
    time/distance to each with ONE Routes API call per town. A single
    town's Routes call failing doesn't drop it from the list -- it's kept
    with haversine distance only, and drive_duration_s left None so pdf.py
    can word it honestly instead of overclaiming a time we don't have.
    """
    from .kenya_towns import find_nearest_towns

    candidates = find_nearest_towns(
        float(cell.center_latitude), float(cell.center_longitude), _haversine_m, n=MAX_NEAREST_TOWNS
    )

    results = []
    for town in candidates:
        entry = {
            "name": town["name"],
            "county": town["county"],
            "rank": town["rank"],
            "distance_m": town["distance_m"],
            "drive_duration_s": None,
            "drive_distance_m": None,
        }

        body = {
            "origin": {"location": {"latLng": {"latitude": float(cell.center_latitude), "longitude": float(cell.center_longitude)}}},
            "destination": {"location": {"latLng": {"latitude": town["lat"], "longitude": town["lng"]}}},
            "travelMode": "DRIVE",
            "routingPreference": "TRAFFIC_AWARE",
        }
        headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": GOOGLE_API_KEY,
            "X-Goog-FieldMask": "routes.duration,routes.distanceMeters,routes.legs.steps.navigationInstruction.instructions",
        }
        try:
            resp = requests.post(
                "https://routes.googleapis.com/directions/v2:computeRoutes",
                json=body, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS,
            )
            succeeded = resp.status_code == 200
            data = resp.json() if succeeded else {}
        except requests.RequestException as exc:
            logger.warning("Routes fetch failed for cell %s → %s: %s", cell.geohash, town["name"], exc)
            _log_call("routes", cell, {"destination": town["name"]}, None, False)
            results.append(entry)
            continue

        _log_call("routes", cell, {"destination": town["name"]}, resp.status_code, succeeded)

        if succeeded and data.get("routes"):
            route = data["routes"][0]
            entry["drive_duration_s"] = int(str(route.get("duration", "0s")).rstrip("s"))
            entry["drive_distance_m"] = route.get("distanceMeters")
            # Turn-by-turn text is only worth keeping for the single nearest
            # town -- that's the one pdf.py uses for the Access section, and
            # it keeps the stored JSON small.
            if town["rank"] == 1:
                steps = []
                for leg in route.get("legs", []):
                    for step in leg.get("steps", []):
                        instruction = step.get("navigationInstruction", {}).get("instructions")
                        if instruction:
                            steps.append(instruction)
                if steps:
                    entry["directions_steps"] = steps

        results.append(entry)

    cell.nearest_towns = results
    cell.nearest_towns_fetched_at = timezone.now()
    cell.save(update_fields=["nearest_towns", "nearest_towns_fetched_at"])
    return cell


# ---------------------------------------------------------------------------
# Elevation — flood/drainage risk proxy
# ---------------------------------------------------------------------------

def _grid_points(lat, lng, offset_deg=0.00135):
    """~150m N/E/S/W offsets around center (0.00135deg ~= 150m at this latitude)."""
    return [
        (lat, lng),
        (lat + offset_deg, lng),
        (lat - offset_deg, lng),
        (lat, lng + offset_deg),
        (lat, lng - offset_deg),
    ]


def fetch_elevation(cell: LocationCell):
    points = _grid_points(float(cell.center_latitude), float(cell.center_longitude))
    locations_param = "|".join(f"{lat},{lng}" for lat, lng in points)
    params = {"locations": locations_param, "key": GOOGLE_API_KEY}
    try:
        resp = requests.get(
            "https://maps.googleapis.com/maps/api/elevation/json",
            params=params, timeout=REQUEST_TIMEOUT_SECONDS,
        )
        data = resp.json()
        succeeded = resp.status_code == 200 and data.get("status") == "OK"
    except requests.RequestException as exc:
        logger.warning("Elevation fetch failed for cell %s: %s", cell.geohash, exc)
        _log_call("elevation", cell, {"locations": locations_param}, None, False)
        raise EnrichmentStepFailed("elevation") from exc
    except ValueError as exc:
        logger.warning("Elevation returned non-JSON for cell %s: %s", cell.geohash, exc)
        _log_call("elevation", cell, {"locations": locations_param}, getattr(resp, "status_code", None), False)
        raise EnrichmentStepFailed("elevation: non-JSON response") from exc

    _log_call("elevation", cell, {"locations": locations_param}, resp.status_code, succeeded)

    if not succeeded:
        raise EnrichmentStepFailed(f"elevation returned status={data.get('status')}")

    results = data["results"]
    grid = [{"lat": r["location"]["lat"], "lng": r["location"]["lng"], "elevation": r.get("elevation")} for r in results]
    elevations = [g["elevation"] for g in grid if g["elevation"] is not None]

    cell.elevation_meters = grid[0]["elevation"] if grid else None
    cell.elevation_grid = grid
    cell.elevation_slope_range_m = (max(elevations) - min(elevations)) if len(elevations) >= 2 else None
    cell.elevation_fetched_at = timezone.now()
    cell.save(update_fields=["elevation_meters", "elevation_grid", "elevation_slope_range_m", "elevation_fetched_at"])
    return cell


# ---------------------------------------------------------------------------
# Roads — is the parcel actually on/near a mapped (usually paved) road?
# ---------------------------------------------------------------------------

def fetch_road_context(cell: LocationCell):
    params = {
        "points": f"{cell.center_latitude},{cell.center_longitude}",
        "key": GOOGLE_API_KEY,
    }
    try:
        resp = requests.get(
            "https://roads.googleapis.com/v1/nearestRoads",
            params=params, timeout=REQUEST_TIMEOUT_SECONDS,
        )
        data = resp.json()
        succeeded = resp.status_code == 200
    except requests.RequestException as exc:
        logger.warning("Roads API fetch failed for cell %s: %s", cell.geohash, exc)
        _log_call("roads", cell, {"points": params["points"]}, None, False)
        raise EnrichmentStepFailed("roads") from exc
    except ValueError as exc:
        logger.warning("Roads API returned non-JSON for cell %s: %s", cell.geohash, exc)
        _log_call("roads", cell, {"points": params["points"]}, getattr(resp, "status_code", None), False)
        raise EnrichmentStepFailed("roads: non-JSON response") from exc

    _log_call("roads", cell, {"points": params["points"]}, resp.status_code, succeeded)

    if not succeeded:
        raise EnrichmentStepFailed(f"roads returned status={resp.status_code}")

    snapped = data.get("snappedPoints", [])
    cell.on_paved_road = bool(snapped)
    if snapped:
        snap_lat = snapped[0]["location"]["latitude"]
        snap_lng = snapped[0]["location"]["longitude"]
        cell.nearest_road_distance_m = _haversine_m(
            float(cell.center_latitude), float(cell.center_longitude), snap_lat, snap_lng
        )
    else:
        cell.nearest_road_distance_m = None

    cell.road_context_fetched_at = timezone.now()
    cell.save(update_fields=["on_paved_road", "nearest_road_distance_m", "road_context_fetched_at"])
    return cell


# ---------------------------------------------------------------------------
# Text Search — catches categories Nearby Search's fixed types miss
# (student housing, gated communities) via free-text queries.
# ---------------------------------------------------------------------------

TEXT_SEARCH_CATEGORIES = {
    "nearby_student_housing": "student hostels and accommodation",
    "nearby_gated_communities": "gated community estate",
}

TEXT_SEARCH_FIELD_MASK = "places.displayName,places.location,places.rating,places.id,places.businessStatus,places.photos"


def _search_text(cell: LocationCell, text_query):
    body = {
        "textQuery": text_query,
        "locationBias": {
            "circle": {
                "center": {"latitude": float(cell.center_latitude), "longitude": float(cell.center_longitude)},
                "radius": 5000.0,
            }
        },
        "maxResultCount": 20,
    }
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": GOOGLE_API_KEY,
        "X-Goog-FieldMask": TEXT_SEARCH_FIELD_MASK,
    }
    try:
        resp = requests.post(
            "https://places.googleapis.com/v1/places:searchText",
            json=body, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS,
        )
        succeeded = resp.status_code == 200
        data = resp.json() if succeeded else {}
    except requests.RequestException as exc:
        logger.warning("Places text search failed for cell %s (%s): %s", cell.geohash, text_query, exc)
        _log_call("places_text", cell, {"query": text_query}, None, False)
        raise EnrichmentStepFailed("places_text") from exc

    _log_call("places_text", cell, {"query": text_query}, resp.status_code, succeeded)

    if not succeeded:
        raise EnrichmentStepFailed(f"places_text ({text_query}) returned status={resp.status_code}")

    results = []
    center_lat, center_lng = float(cell.center_latitude), float(cell.center_longitude)
    for place in data.get("places", []):
        loc = place.get("location", {})
        results.append({
            "name": place.get("displayName", {}).get("text", ""),
            "place_id": place.get("id", ""),
            "lat": loc.get("latitude"),
            "lng": loc.get("longitude"),
            "rating": place.get("rating"),
            "business_status": place.get("businessStatus", ""),
            "photo_name": (place.get("photos") or [{}])[0].get("name"),
            "distance_m": _haversine_m(center_lat, center_lng, loc.get("latitude"), loc.get("longitude")),
        })
    results.sort(key=lambda r: r["distance_m"] if r["distance_m"] is not None else float("inf"))
    return results


def fetch_text_search_amenities(cell: LocationCell):
    update_fields = []
    for field_name, query_text in TEXT_SEARCH_CATEGORIES.items():
        try:
            query = f"{query_text} near {cell.center_latitude},{cell.center_longitude}"
            setattr(cell, field_name, _search_text(cell, query))
            update_fields.append(field_name)
        except EnrichmentStepFailed:
            continue

    if update_fields:
        cell.save(update_fields=update_fields)
    return cell


# ---------------------------------------------------------------------------
# Amenity Photos — Places Photo Media. Downloads a photo for a handful of
# the nearest, most sellable amenities (schools/hospitals/universities/
# shopping/gated communities) and re-uploads to OUR storage, same reasoning
# as satellite/street view: never hand back a raw Google URL with the key
# embedded. photo_url is written directly onto the existing JSON entry
# already stored on the cell (nearby_schools, etc.) — no new DB columns,
# no migration needed.
# ---------------------------------------------------------------------------

PHOTO_CATEGORIES = (
    "nearby_schools", "nearby_hospitals", "nearby_universities",
    "nearby_shopping", "nearby_gated_communities",
)
MAX_AMENITY_PHOTOS = 4


def fetch_amenity_photos(cell: LocationCell):
    from . import storage

    update_fields = []
    photos_fetched = 0

    for field_name in PHOTO_CATEGORIES:
        if photos_fetched >= MAX_AMENITY_PHOTOS:
            break
        entries = getattr(cell, field_name, None) or []
        candidate = next((e for e in entries if e.get("photo_name") and not e.get("photo_url")), None)
        if not candidate:
            continue

        photo_name = candidate["photo_name"]
        params = {"maxWidthPx": 400, "key": GOOGLE_API_KEY}
        try:
            resp = requests.get(
                f"https://places.googleapis.com/v1/{photo_name}/media",
                params=params, timeout=REQUEST_TIMEOUT_SECONDS,
            )
            succeeded = resp.status_code == 200 and resp.headers.get("Content-Type", "").startswith("image/")
        except requests.RequestException as exc:
            logger.warning("Amenity photo fetch failed for cell %s (%s): %s", cell.geohash, photo_name, exc)
            _log_call("places_photo", cell, {"photo_name": photo_name}, None, False)
            continue

        _log_call("places_photo", cell, {"photo_name": photo_name}, resp.status_code, succeeded)
        if not succeeded:
            continue

        safe_id = (candidate.get("place_id") or "")[:40] or f"photo{photos_fetched}"
        stored_url = storage.upload_image_bytes(
            resp.content,
            path=f"location-cells/{cell.geohash}/amenities/{safe_id}.jpg",
            content_type="image/jpeg",
        )
        candidate["photo_url"] = stored_url
        setattr(cell, field_name, entries)
        update_fields.append(field_name)
        photos_fetched += 1

    if update_fields:
        cell.save(update_fields=list(set(update_fields)))
    return cell


# ---------------------------------------------------------------------------
# Orchestrator — runs everything, tolerates partial failure
# ---------------------------------------------------------------------------

def enrich_location_cell(cell: LocationCell):
    """
    Runs the full enrichment pipeline for a cell. Each step is independent —
    one failing (e.g. Air Quality API down) doesn't stop the others, so a
    broker still gets a mostly-complete report instead of a hard failure.
    Steps that succeed are saved immediately (each fetch_* function saves
    its own fields), so a partial run isn't lost if a later step fails.
    """
    steps = [
        ("geocoding", geocode_cell),
        ("satellite image", fetch_satellite_image),
        ("street view availability", check_street_view_availability),
        ("nearby amenities", fetch_nearby_amenities),
        ("air quality", fetch_air_quality),
        ("air quality history", fetch_air_quality_history),
        ("air quality forecast", fetch_air_quality_forecast),
        ("travel times", fetch_travel_times),
        ("nearest towns", fetch_nearest_towns),
        ("elevation", fetch_elevation),
        ("road context", fetch_road_context),
        ("text search amenities", fetch_text_search_amenities),
        ("amenity photos", fetch_amenity_photos),
    ]

    failures = []
    for label, fn in steps:
        try:
            fn(cell)
        except EnrichmentStepFailed as exc:
            logger.warning("Enrichment step '%s' failed for cell %s: %s", label, cell.geohash, exc)
            failures.append(label)

    # Street View image only makes sense after availability is confirmed —
    # run it separately, after the loop, and only if the check succeeded.
    if cell.street_view_available:
        try:
            fetch_street_view_image(cell)
        except EnrichmentStepFailed as exc:
            logger.warning("Street View image fetch failed for cell %s: %s", cell.geohash, exc)
            failures.append("street view image")

    if failures:
        logger.info("Cell %s enriched with partial failures: %s", cell.geohash, ", ".join(failures))

    return cell, failures
