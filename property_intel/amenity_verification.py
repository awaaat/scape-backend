"""
property_intel/amenity_verification.py

Cascade for amenity entries that share an identical/near-identical
lat-lng across unrelated categories (Google's own listing data --
businesses geocoded to a shared reference point instead of their real
location; see the "3m cluster" incident on the Eldoret cell, confirmed
via raw DB inspection, not guessed).

Tier 3 -- OSM cross-check: query Overpass for a real, named node/way of
    the matching type near the property. If a confident name match is
    found, replace the entry's lat/lng/distance with OSM's real values.
Tier 1 -- Suppress: OSM found nothing confident. distance_m is set to
    None and the entry is flagged 'location_unverified'. NEVER rendered
    as "Unknown" -- pdf.py's _discover_amenity_fields drops any entry
    with distance_m is None before it reaches any report section.
Tier 2 -- Drop: the category has no OSM tag equivalent at all (student
    housing, gated communities -- OSM has no reliable tag for these in
    Kenya), so verification was never attemptable. Entry is removed
    from the JSON list entirely, not just hidden.

Cost control: this only runs Overpass queries for entries actually
caught in a detected coincident-coordinate cluster, not on every
amenity in every report -- and only once per LocationCell (cached).
"""
import difflib
import logging
import re

import requests

from .google_client import OSM_OVERPASS_URL, _haversine_m, _log_call

logger = logging.getLogger("property_intel")

# Buckets entries by rounded (lat, lng) to detect coincident geocodes.
# 4 decimal places ~= 11m at the equator -- loose enough to catch
# near-duplicates, tight enough not to bucket genuinely distinct nearby
# amenities together.
CLUSTER_ROUND_PRECISION = 4
MIN_CLUSTER_SIZE = 2

OSM_VERIFY_SEARCH_RADII_M = [500, 1500, 3000, 5000]
OSM_VERIFY_TIMEOUT_SECONDS = 15
NAME_SIMILARITY_THRESHOLD = 0.6

# Character-level similarity alone misses cases like "Moi Girls Eldoret"
# (Google's name) vs "Moi Girls High School" (OSM's name) -- same place,
# but the town-name suffix vs the official-designation suffix drag the
# SequenceMatcher ratio below threshold even though the core name matches.
# Word-overlap catches this: how much of the SMALLER name's word set is
# also in the other name.
TOKEN_OVERLAP_THRESHOLD = 0.6

# Category -> OSM tag filter(s). Categories absent from this map have no
# reliable OSM equivalent in Kenya and go straight to tier 2 (drop) when
# caught in a cluster, since tier 3 verification isn't attemptable.
CATEGORY_OSM_TAGS = {
    "nearby_schools": ['"amenity"="school"'],
    "nearby_universities": ['"amenity"="university"', '"amenity"="college"'],
    "nearby_hospitals": ['"amenity"="hospital"'],
    "nearby_banks": ['"amenity"="bank"'],
    "nearby_petrol_stations": ['"amenity"="fuel"'],
    "nearby_shopping": ['"shop"="mall"', '"amenity"="marketplace"'],
    "nearby_supermarkets": ['"shop"="supermarket"'],
    "nearby_restaurants": ['"amenity"="restaurant"'],
    "nearby_police_stations": ['"amenity"="police"'],
    "nearby_fire_stations": ['"amenity"="fire_station"'],
    "nearby_pharmacies": ['"amenity"="pharmacy"'],
    "nearby_transit_stops": ['"highway"="bus_stop"', '"amenity"="bus_station"'],
    "nearby_parks": ['"leisure"="park"'],
    "nearby_ev_charging": ['"amenity"="charging_station"'],
}


def _normalize_name(name):
    if not name:
        return ""
    n = name.lower()
    n = re.sub(r"[^a-z0-9\s]", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def _names_match(google_name, candidate_name, candidate_brand=None):
    g = _normalize_name(google_name)
    if not g:
        return False
    g_tokens = set(g.split())
    for cand in filter(None, [candidate_name, candidate_brand]):
        c = _normalize_name(cand)
        if not c:
            continue
        if c in g or g in c:
            return True
        if difflib.SequenceMatcher(None, g, c).ratio() >= NAME_SIMILARITY_THRESHOLD:
            return True
        c_tokens = set(c.split())
        if g_tokens and c_tokens:
            overlap = g_tokens & c_tokens
            smaller = min(len(g_tokens), len(c_tokens))
            if smaller and len(overlap) / smaller >= TOKEN_OVERLAP_THRESHOLD:
                return True
    return False


def _best_osm_match(google_name, candidates):
    best, best_ratio = None, 0.0
    g = _normalize_name(google_name)
    if not g:
        return None
    for cand in candidates:
        if not _names_match(google_name, cand.get("name"), cand.get("brand")):
            continue
        ratio = max(
            difflib.SequenceMatcher(None, g, _normalize_name(cand.get("name") or "")).ratio(),
            difflib.SequenceMatcher(None, g, _normalize_name(cand.get("brand") or "")).ratio(),
        )
        if ratio > best_ratio:
            best_ratio, best = ratio, cand
    return best


def _query_osm_candidates(lat0, lng0, tag_filters, cell):
    """Expanding-radius Overpass search for named nodes/ways matching any
    of tag_filters. Returns [] if OSM has no coverage here or every
    request fails -- caller must treat that as 'verification inconclusive',
    not as proof the Google entry is wrong."""
    for radius_m in OSM_VERIFY_SEARCH_RADII_M:
        clauses = "".join(
            f'node(around:{radius_m},{lat0},{lng0})[{tf}];'
            f'way(around:{radius_m},{lat0},{lng0})[{tf}];'
            for tf in tag_filters
        )
        query = f'[out:json][timeout:{OSM_VERIFY_TIMEOUT_SECONDS}];({clauses});out center tags;'
        try:
            resp = requests.post(OSM_OVERPASS_URL, data={"data": query}, timeout=OSM_VERIFY_TIMEOUT_SECONDS)
            succeeded = resp.status_code == 200
            data = resp.json() if succeeded else {}
        except (requests.RequestException, ValueError) as exc:
            logger.warning("OSM amenity-verify query failed for cell %s at %sm: %s", cell.geohash, radius_m, exc)
            _log_call("osm_overpass", cell, {"purpose": "amenity_verify", "radius_m": radius_m}, None, False)
            continue

        _log_call("osm_overpass", cell, {"purpose": "amenity_verify", "radius_m": radius_m}, resp.status_code, succeeded)
        if not succeeded:
            continue

        candidates = []
        for el in data.get("elements", []):
            tags = el.get("tags", {}) or {}
            name, brand = tags.get("name"), tags.get("brand")
            if not name and not brand:
                continue
            if el.get("type") == "node":
                lat, lng = el.get("lat"), el.get("lon")
            else:
                center = el.get("center") or {}
                lat, lng = center.get("lat"), center.get("lon")
            if lat is None or lng is None:
                continue
            candidates.append({"name": name, "brand": brand, "lat": lat, "lng": lng})

        if candidates:
            return candidates
    return []


def detect_coincident_clusters(cell):
    """Groups amenity entries (across ALL categories) that share a
    near-identical lat/lng. A group of 2+ is suspect -- real amenities of
    different kinds don't occupy the same few square meters."""
    field_names = [f.name for f in cell._meta.get_fields() if getattr(f, "name", "").startswith("nearby_")]
    buckets = {}
    for field_name in field_names:
        for entry in getattr(cell, field_name, None) or []:
            lat, lng = entry.get("lat"), entry.get("lng")
            if lat is None or lng is None:
                continue
            key = (round(lat, CLUSTER_ROUND_PRECISION), round(lng, CLUSTER_ROUND_PRECISION))
            buckets.setdefault(key, []).append((field_name, entry))
    return [group for group in buckets.values() if len(group) >= MIN_CLUSTER_SIZE]


def resolve_suspect_amenities(cell):
    clusters = detect_coincident_clusters(cell)
    if not clusters:
        return cell

    center_lat, center_lng = float(cell.center_latitude), float(cell.center_longitude)
    changed_fields = set()
    verified, suppressed, dropped = 0, 0, 0

    for cluster in clusters:
        cluster_lat, cluster_lng = cluster[0][1]["lat"], cluster[0][1]["lng"]
        needed_tags = set()
        for field_name, _entry in cluster:
            needed_tags.update(CATEGORY_OSM_TAGS.get(field_name, []))

        osm_candidates = _query_osm_candidates(cluster_lat, cluster_lng, sorted(needed_tags), cell) if needed_tags else []

        for field_name, entry in cluster:
            tag_filters = CATEGORY_OSM_TAGS.get(field_name)
            if not tag_filters:
                entry["_drop"] = True
                dropped += 1
                changed_fields.add(field_name)
                continue

            match = _best_osm_match(entry.get("name", ""), osm_candidates)
            if match:
                entry["lat"], entry["lng"] = match["lat"], match["lng"]
                entry["distance_m"] = _haversine_m(center_lat, center_lng, match["lat"], match["lng"])
                entry["verified_via"] = "osm"
                entry.pop("location_unverified", None)
                verified += 1
            else:
                entry["distance_m"] = None
                entry["location_unverified"] = True
                suppressed += 1
            changed_fields.add(field_name)

    for field_name in changed_fields:
        entries = [e for e in (getattr(cell, field_name, None) or []) if not e.get("_drop")]
        entries.sort(key=lambda e: e.get("distance_m") if e.get("distance_m") is not None else float("inf"))
        setattr(cell, field_name, entries)

    if changed_fields:
        cell.save(update_fields=list(changed_fields))
        logger.info(
            "Cell %s: amenity verification -- %s verified via OSM, %s suppressed (no confident OSM match), %s dropped (no OSM coverage for category)",
            cell.geohash, verified, suppressed, dropped,
        )
    return cell
