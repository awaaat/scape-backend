import re, sys, pathlib

ROOT = pathlib.Path(".")

def patch(path, old, new, label):
    p = ROOT / path
    text = p.read_text()
    count = text.count(old)
    if count != 1:
        print(f"FAIL [{label}]: expected 1 match in {path}, found {count}. File has drifted -- paste current block.")
        sys.exit(1)
    p.write_text(text.replace(old, new))
    print(f"OK   [{label}]: patched {path}")

# ---------------------------------------------------------------------
# Fix 1: property_intel/google_client.py -- guarantee nearest major road
# ---------------------------------------------------------------------
old_roads = '''def _query_osm_nearby_roads(lat0, lng0, cell, top_n=NEARBY_ROADS_COUNT):
    """
    Queries Overpass for real, named roads within an expanding radius.
    Returns a list of up to `top_n` dicts {"name": str, "distance_m": int},
    nearest first, deduped by name (a long road can have many way segments
    -- only its closest segment counts). Returns [] if OSM has no coverage
    in range or every request fails -- callers must fall back to the
    Google-based path in that case, never invent a distance.
    """
    highway_filter = "|".join(OSM_ROAD_HIGHWAY_TAGS)
    for radius_m in OSM_SEARCH_RADII_M:
        query = (
            f'[out:json][timeout:{OSM_REQUEST_TIMEOUT_SECONDS}];'
            f'way(around:{radius_m},{lat0},{lng0})["highway"~"^({highway_filter})$"];'
            f'out geom;'
        )
        try:
            resp = requests.post(
                OSM_OVERPASS_URL, data={"data": query}, timeout=OSM_REQUEST_TIMEOUT_SECONDS,
            )
            succeeded = resp.status_code == 200
            data = resp.json() if succeeded else {}
        except (requests.RequestException, ValueError) as exc:
            logger.warning("OSM Overpass query failed for cell %s at %sm: %s", cell.geohash, radius_m, exc)
            _log_call("osm_overpass", cell, {"radius_m": radius_m}, None, False)
            continue
        else:
            _log_call("osm_overpass", cell, {"radius_m": radius_m}, resp.status_code, succeeded)

        if not succeeded:
            continue

        elements = data.get("elements", [])
        if not elements:
            continue  # nothing at this radius -- widen and try again

        closest_by_name = {}  # name -> nearest distance_m seen for that name
        for way in elements:
            geometry = way.get("geometry") or []
            if not geometry:
                continue
            tags = way.get("tags", {})
            name = tags.get("name") or tags.get("ref")
            if not name:
                continue
            distance_m = _point_to_polyline_distance_m(lat0, lng0, geometry)
            if name not in closest_by_name or distance_m < closest_by_name[name]:
                closest_by_name[name] = distance_m

        if closest_by_name:
            ranked = sorted(closest_by_name.items(), key=lambda kv: kv[1])[:top_n]
            return [{"name": name, "distance_m": int(distance_m)} for name, distance_m in ranked]

    return []'''

new_roads = '''MAJOR_ROAD_TAGS = ("motorway", "trunk", "primary")


def _query_osm_nearby_roads(lat0, lng0, cell, top_n=NEARBY_ROADS_COUNT):
    """
    Queries Overpass for real, named roads within an expanding radius.
    Returns a list of up to `top_n` dicts {"name": str, "distance_m": int},
    nearest first, deduped by name (a long road can have many way segments
    -- only its closest segment counts). Returns [] if OSM has no coverage
    in range or every request fails -- callers must fall back to the
    Google-based path in that case, never invent a distance.

    Guarantees a slot for the nearest motorway/trunk/primary road if one
    exists in range, even when it isn't among the top_n nearest by raw
    distance -- a highway 800m out is a stronger selling point than a
    fourth residential street 50m out, and pure-distance ranking was
    silently dropping every major road once enough closer minor streets
    existed.
    """
    highway_filter = "|".join(OSM_ROAD_HIGHWAY_TAGS)
    for radius_m in OSM_SEARCH_RADII_M:
        query = (
            f'[out:json][timeout:{OSM_REQUEST_TIMEOUT_SECONDS}];'
            f'way(around:{radius_m},{lat0},{lng0})["highway"~"^({highway_filter})$"];'
            f'out geom;'
        )
        try:
            resp = requests.post(
                OSM_OVERPASS_URL, data={"data": query}, timeout=OSM_REQUEST_TIMEOUT_SECONDS,
            )
            succeeded = resp.status_code == 200
            data = resp.json() if succeeded else {}
        except (requests.RequestException, ValueError) as exc:
            logger.warning("OSM Overpass query failed for cell %s at %sm: %s", cell.geohash, radius_m, exc)
            _log_call("osm_overpass", cell, {"radius_m": radius_m}, None, False)
            continue
        else:
            _log_call("osm_overpass", cell, {"radius_m": radius_m}, resp.status_code, succeeded)

        if not succeeded:
            continue

        elements = data.get("elements", [])
        if not elements:
            continue  # nothing at this radius -- widen and try again

        closest_by_name = {}  # name -> (nearest distance_m, highway tag) seen for that name
        for way in elements:
            geometry = way.get("geometry") or []
            if not geometry:
                continue
            tags = way.get("tags", {})
            name = tags.get("name") or tags.get("ref")
            if not name:
                continue
            highway_tag = tags.get("highway", "")
            distance_m = _point_to_polyline_distance_m(lat0, lng0, geometry)
            if name not in closest_by_name or distance_m < closest_by_name[name][0]:
                closest_by_name[name] = (distance_m, highway_tag)

        if closest_by_name:
            ranked = sorted(closest_by_name.items(), key=lambda kv: kv[1][0])

            nearest_major = next(
                ((name, dist) for name, (dist, tag) in ranked if tag in MAJOR_ROAD_TAGS),
                None,
            )

            top = ranked[:top_n]
            result = [{"name": name, "distance_m": int(dist)} for name, (dist, _tag) in top]

            if nearest_major and nearest_major[0] not in {r["name"] for r in result}:
                if len(result) >= top_n:
                    result = result[:top_n - 1]
                result.append({"name": nearest_major[0], "distance_m": int(nearest_major[1])})
                result.sort(key=lambda r: r["distance_m"])

            return result

    return []'''

patch("property_intel/google_client.py", old_roads, new_roads, "major road guarantee")

# ---------------------------------------------------------------------
# Fix 2: property_intel/pdf.py -- notable-mall filter + guaranteed slot
# ---------------------------------------------------------------------
old_notable_restaurant = '''def _is_notable_restaurant(name):
    n = (name or "").lower()
    return any(kw in n for kw in _NOTABLE_RESTAURANT_KEYWORDS)'''

new_notable_restaurant = '''def _is_notable_restaurant(name):
    n = (name or "").lower()
    return any(kw in n for kw in _NOTABLE_RESTAURANT_KEYWORDS)


NOTABLE_SHOPPING_MIN_RATINGS = 50


def _is_notable_shopping(entry):
    """A mall worth naming in a listing, not just whichever shopping_mall-
    tagged point Google happened to return closest. Google's shopping_mall
    type also catches small plazas and arcades; user_rating_count is the
    only signal available at fetch time for 'this is actually a landmark.'
    """
    count = entry.get("user_rating_count")
    return isinstance(count, int) and count >= NOTABLE_SHOPPING_MIN_RATINGS'''

patch("property_intel/pdf.py", old_notable_restaurant, new_notable_restaurant, "notable shopping filter")

old_collect = '''def _collect_evidence_points(cell, max_points=6):
    """Nearest named entry per priority category, sorted by distance,
    capped at max_points. Returns [(label, name, distance_m), ...].

    Restaurants is restricted to notable establishments (hotels, resorts,
    lodges, inns -- via _is_notable_restaurant) so the report never cites
    an arbitrary roadside eatery as evidence.
    """
    amenity_fields = dict(_discover_amenity_fields(cell))
    points = []
    for label in EVIDENCE_CATEGORY_PRIORITY:
        entries = amenity_fields.get(label)
        if not entries:
            continue
        if label == "Restaurants":
            entries = [e for e in entries if _is_notable_restaurant(e.get("name"))]
            if not entries:
                continue
        nearest = min(entries, key=lambda e: e.get("distance_m", float("inf")))
        name = nearest.get("name")
        distance_m = nearest.get("distance_m")
        if not name or distance_m is None:
            continue
        points.append((label, name, distance_m))
    points.sort(key=lambda p: p[2])
    return points[:max_points]'''

new_collect = '''def _collect_evidence_points(cell, max_points=6):
    """Nearest named entry per priority category, sorted by distance,
    capped at max_points. Returns [(label, name, distance_m), ...].

    Restaurants is restricted to notable establishments (hotels, resorts,
    lodges, inns -- via _is_notable_restaurant) so the report never cites
    an arbitrary roadside eatery as evidence.

    Shopping prefers a notable mall (_is_notable_shopping) over the
    literal nearest shopping_mall-tagged point, and that notable mall is
    guaranteed a slot in the final capped list even if closer amenities
    in other categories would otherwise crowd it out -- a real mall is a
    stronger selling point than a fourth or fifth nearby pharmacy/bank/
    petrol station, and pure-distance ranking was silently dropping it
    from the report entirely.
    """
    amenity_fields = dict(_discover_amenity_fields(cell))
    points = []
    for label in EVIDENCE_CATEGORY_PRIORITY:
        entries = amenity_fields.get(label)
        if not entries:
            continue
        if label == "Restaurants":
            entries = [e for e in entries if _is_notable_restaurant(e.get("name"))]
            if not entries:
                continue
        if label == "Shopping":
            notable = [e for e in entries if _is_notable_shopping(e)]
            entries = notable or entries
        nearest = min(entries, key=lambda e: e.get("distance_m", float("inf")))
        name = nearest.get("name")
        distance_m = nearest.get("distance_m")
        if not name or distance_m is None:
            continue
        points.append((label, name, distance_m))

    points.sort(key=lambda p: p[2])

    shopping_point = next((p for p in points if p[0] == "Shopping"), None)
    top = points[:max_points]
    if shopping_point and shopping_point not in top:
        top = top[:max_points - 1] + [shopping_point]
        top.sort(key=lambda p: p[2])

    return top'''

patch("property_intel/pdf.py", old_collect, new_collect, "guaranteed shopping slot")

print("\nAll patches applied.")
