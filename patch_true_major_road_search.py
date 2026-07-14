"""
Run this from ~/scape_backend:  python3 patch_true_major_road_search.py

Fixes the real remaining bug: fetch_major_road_context() only ever looked
at roads that happened to lie along driving routes to the 5 nearest towns.
It never actually looked near the property. In Bungoma that meant it
missed the real nearby highway entirely and fell back to a same-name
segment of that highway ~40km away, resolved from a route to a different
town (Webuye/Chwele/etc) -- which is also why the name came back as
"Kisumu-Kakamega Road" instead of the Bungoma-Kakamega stretch actually
nearby: it's the same physical road, just resolved somewhere else.

THE FIX: search in expanding rings directly around the property (same
technique fetch_road_context() already uses for the single nearest road
point -- Roads API nearestRoads + Place Details), batched into ONE Roads
API call, then pick the nearest result that classifies as major -- or the
nearest named road if none classify as major. The old route-based signal
is kept only as a last-resort fallback if the ring search finds nothing.

Anchors below were copied verbatim from your current file -- if any
doesn't match, the script aborts with no changes written.
"""
path = "property_intel/google_client.py"
with open(path, "r") as f:
    content = f.read()


def apply(label, old, new, content):
    if old not in content:
        raise SystemExit(f"ERROR: anchor not found for [{label}] -- aborting without changes.")
    if content.count(old) > 1:
        raise SystemExit(f"ERROR: anchor for [{label}] is not unique -- aborting without changes.")
    return content.replace(old, new, 1)


# ── 1. Need `math` for the ring-point bearing/distance offset calc ──────────
content = apply(
    "math import",
    '''import logging
import re
from datetime import timedelta''',
    '''import logging
import math
import re
from datetime import timedelta''',
    content,
)

# ── 2. Replace fetch_major_road_context with the ring-search version ────────
old_fn = '''# ---------------------------------------------------------------------------
# Major roads -- distance to the real arterial/highway a property actually
# connects to. Derived from the driving routes already computed in
# fetch_nearest_towns (see _major_road_from_step_records above) rather
# than matching against a fixed, inevitably-incomplete list of named
# Kenyan roads. Works for any location, and makes zero extra API calls of
# its own -- it just reads what fetch_nearest_towns (which MUST run first
# -- see enrich_location_cell's step order) already stored, checking
# every nearby town's route rather than only the nearest one.
# ---------------------------------------------------------------------------

def fetch_major_road_context(cell: LocationCell):
    towns = cell.nearest_towns or []
    candidates = [
        (t["major_road_name"], t["major_road_distance_m"], t.get("major_road_tier", "named"))
        for t in towns
        if t.get("major_road_name") and t.get("major_road_distance_m") is not None
    ]
    # Prefer a genuinely classified major/trunk road across EVERY nearby
    # town's route -- not just the nearest town's, since that one alone
    # is often too short to ever cross a real highway. Fall back to the
    # closest candidate of any tier only if nothing classified as major.
    major_only = [c for c in candidates if c[2] == "major"]
    pool = major_only or candidates
    best_name, best_distance, _tier = min(pool, key=lambda c: c[1]) if pool else (None, None, None)

    cell.nearest_major_road_name = best_name
    cell.nearest_major_road_distance_m = best_distance
    cell.major_road_context_fetched_at = timezone.now()
    cell.save(update_fields=[
        "nearest_major_road_name", "nearest_major_road_distance_m", "major_road_context_fetched_at",
    ])
    return cell'''

new_fn = '''# ---------------------------------------------------------------------------
# Major roads -- distance to the real arterial/highway a property actually
# connects to. Searches directly around the property in expanding rings
# (same Roads API + Place Details technique fetch_road_context() already
# uses for the single nearest road point), rather than inferring from
# routes to the 5 nearest towns -- route-inference has a hard blind spot:
# a major road not on the way to any of those 5 towns was never found,
# no matter how physically close it actually was. One batched Roads API
# call per property; only distinct placeIds get resolved/classified.
# ---------------------------------------------------------------------------

# Ring radii (metres) and bearings (degrees, 0=N) sampled around the
# property. 6 rings x 12 bearings + the center point = 73 points, well
# under the Roads API's 100-point-per-request limit.
MAJOR_ROAD_SEARCH_RADII_M = [500, 1000, 2000, 4000, 8000, 15000]
MAJOR_ROAD_SEARCH_BEARINGS = list(range(0, 360, 30))


def _offset_latlng(lat, lng, distance_m, bearing_deg):
    """Returns (lat, lng) offset from (lat, lng) by distance_m along
    bearing_deg (0=N, 90=E), using the standard spherical-earth
    destination-point formula."""
    R = 6371000.0
    bearing = math.radians(bearing_deg)
    lat1 = math.radians(lat)
    lng1 = math.radians(lng)
    ang = distance_m / R
    lat2 = math.asin(
        math.sin(lat1) * math.cos(ang) + math.cos(lat1) * math.sin(ang) * math.cos(bearing)
    )
    lng2 = lng1 + math.atan2(
        math.sin(bearing) * math.sin(ang) * math.cos(lat1),
        math.cos(ang) - math.sin(lat1) * math.sin(lat2),
    )
    return math.degrees(lat2), math.degrees(lng2)


def fetch_major_road_context(cell: LocationCell):
    lat0 = float(cell.center_latitude)
    lng0 = float(cell.center_longitude)

    points = [(lat0, lng0)]
    for radius_m in MAJOR_ROAD_SEARCH_RADII_M:
        for bearing in MAJOR_ROAD_SEARCH_BEARINGS:
            points.append(_offset_latlng(lat0, lng0, radius_m, bearing))
    points = points[:100]  # Roads API hard limit

    succeeded = False
    data = {}
    try:
        resp = requests.get(
            "https://roads.googleapis.com/v1/nearestRoads",
            params={"points": "|".join(f"{lat},{lng}" for lat, lng in points), "key": GOOGLE_API_KEY},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        succeeded = resp.status_code == 200
        data = resp.json() if succeeded else {}
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Major-road ring search failed for cell %s: %s", cell.geohash, exc)
        _log_call("roads", cell, {"points": len(points)}, None, False)
    else:
        _log_call("roads", cell, {"points": len(points)}, resp.status_code, succeeded)

    best_major = None   # (name, distance_m)
    best_named = None   # (name, distance_m) -- fallback if no major found

    if succeeded:
        # Dedupe by placeId first -- many ring points snap to the same
        # segment -- then walk candidates nearest-first so the FIRST major
        # match really is the nearest one.
        distance_by_place_id = {}
        for snapped in data.get("snappedPoints", []):
            place_id = snapped.get("placeId")
            if not place_id:
                continue
            loc = snapped.get("location", {})
            d = _haversine_m(lat0, lng0, loc.get("latitude"), loc.get("longitude"))
            if place_id not in distance_by_place_id or d < distance_by_place_id[place_id]:
                distance_by_place_id[place_id] = d

        for place_id, distance_m in sorted(distance_by_place_id.items(), key=lambda kv: kv[1]):
            name = _resolve_road_name(place_id, cell)
            tier = _road_tier(name)
            if tier == "major":
                best_major = (name, distance_m)
                break
            if tier == "named" and best_named is None:
                best_named = (name, distance_m)

    if best_major:
        best_name, best_distance = best_major
    elif best_named:
        best_name, best_distance = best_named
    else:
        # Last-resort fallback: the old route-inferred signal from
        # fetch_nearest_towns, in case the ring search found nothing
        # (e.g. Roads API failure, or a genuinely road-sparse area).
        towns = cell.nearest_towns or []
        candidates = [
            (t["major_road_name"], t["major_road_distance_m"], t.get("major_road_tier", "named"))
            for t in towns
            if t.get("major_road_name") and t.get("major_road_distance_m") is not None
        ]
        major_only = [c for c in candidates if c[2] == "major"]
        pool = major_only or candidates
        best_name, best_distance, _tier = min(pool, key=lambda c: c[1]) if pool else (None, None, None)

    cell.nearest_major_road_name = best_name
    cell.nearest_major_road_distance_m = best_distance
    cell.major_road_context_fetched_at = timezone.now()
    cell.save(update_fields=[
        "nearest_major_road_name", "nearest_major_road_distance_m", "major_road_context_fetched_at",
    ])
    return cell'''

content = apply("fetch_major_road_context ring-search rewrite", old_fn, new_fn, content)

with open(path, "w") as f:
    f.write(content)

print("google_client.py patched successfully:")
print(" - fetch_major_road_context now searches ~70 points in rings directly around the")
print("   property (same Roads API + Place Details technique fetch_road_context already")
print("   uses), instead of only ever looking at roads along routes to the 5 nearest towns.")
print(" - Picks the NEAREST classified-major road by real distance; falls back to the")
print("   nearest named road, then to the old route-inferred signal, only if the ring")
print("   search finds nothing.")
