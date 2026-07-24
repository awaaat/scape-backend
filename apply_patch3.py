import sys, pathlib

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
# Fix A: roads -- drop the major/minor split, just search a straight
# 5km radius and take the nearest top_n named roads, whatever class
# they are. No more early-return-at-1000m, no separate major query.
# ---------------------------------------------------------------------
old_a = '''OSM_SEARCH_RADII_M = [1000, 5000, 10000, 20000]
OSM_REQUEST_TIMEOUT_SECONDS = 15
NEARBY_ROADS_COUNT = 3


def _point_to_polyline_distance_m(lat0, lng0, nodes):
    """nodes: list of {'lat':..,'lon':..} dicts describing a way's geometry
    (as returned by Overpass 'out geom'). Approximates point-to-polyline
    distance as the minimum distance to any vertex -- adequate at these
    search radii since OSM way segments are typically short relative to
    the search rings; a true point-to-segment projection would be
    marginally more precise but isn't worth the complexity here."""
    return min(_haversine_m(lat0, lng0, n["lat"], n["lon"]) for n in nodes if "lat" in n and "lon" in n)


MAJOR_ROAD_TAGS = ("motorway", "trunk", "primary")


def _query_major_road(lat0, lng0, cell):
    """Searches expanding radii for the nearest motorway/trunk/primary
    road specifically, independent of whatever minor roads exist close
    in. This has to be its own widening search: the general road query
    below returns as soon as it finds ANY named road (almost always true
    at 1000m, since most properties front something), so it never gets
    a chance to look further out for a highway that's genuinely close
    but not the closest thing overall. Returns {"name", "distance_m"}
    or None if nothing major turns up within OSM_SEARCH_RADII_M.
    """
    major_filter = "|".join(MAJOR_ROAD_TAGS)
    for radius_m in OSM_SEARCH_RADII_M:
        query = (
            f'[out:json][timeout:{OSM_REQUEST_TIMEOUT_SECONDS}];'
            f'way(around:{radius_m},{lat0},{lng0})["highway"~"^({major_filter})$"];'
            f'out geom;'
        )
        try:
            resp = requests.post(
                OSM_OVERPASS_URL, data={"data": query}, timeout=OSM_REQUEST_TIMEOUT_SECONDS,
            )
            succeeded = resp.status_code == 200
            data = resp.json() if succeeded else {}
        except (requests.RequestException, ValueError) as exc:
            logger.warning("OSM major-road query failed for cell %s at %sm: %s", cell.geohash, radius_m, exc)
            _log_call("osm_overpass_major", cell, {"radius_m": radius_m}, None, False)
            continue
        else:
            _log_call("osm_overpass_major", cell, {"radius_m": radius_m}, resp.status_code, succeeded)

        if not succeeded:
            continue

        elements = data.get("elements", [])
        if not elements:
            continue  # no major road at this radius yet -- widen and try again

        best_name, best_dist = None, None
        for way in elements:
            geometry = way.get("geometry") or []
            if not geometry:
                continue
            tags = way.get("tags", {})
            name = tags.get("name") or tags.get("ref")
            if not name:
                continue
            distance_m = _point_to_polyline_distance_m(lat0, lng0, geometry)
            if best_dist is None or distance_m < best_dist:
                best_name, best_dist = name, distance_m

        if best_name:
            return {"name": best_name, "distance_m": int(best_dist)}

    return None


def _query_osm_nearby_roads(lat0, lng0, cell, top_n=NEARBY_ROADS_COUNT):
    """
    Queries Overpass for real, named roads within an expanding radius.
    Returns a list of up to `top_n` dicts {"name": str, "distance_m": int},
    nearest first, deduped by name (a long road can have many way segments
    -- only its closest segment counts). Returns [] if OSM has no coverage
    in range or every request fails -- callers must fall back to the
    Google-based path in that case, never invent a distance.

    Guarantees a slot for the nearest motorway/trunk/primary road via
    _query_major_road() (see that function for why it has to be a
    separate, independently-widening search), even when it isn't among
    the top_n nearest by raw distance -- a highway 800m out is a
    stronger selling point than a fourth residential street 50m out.
    """
    highway_filter = "|".join(OSM_ROAD_HIGHWAY_TAGS)
    result = []
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
            result = [{"name": name, "distance_m": int(distance_m)} for name, distance_m in ranked]
            break

    major = _query_major_road(lat0, lng0, cell)
    if major and major["name"] not in {r["name"] for r in result}:
        if len(result) >= top_n:
            result = result[:top_n - 1]
        result.append(major)
        result.sort(key=lambda r: r["distance_m"])

    return result'''

new_a = '''OSM_SEARCH_RADII_M = [5000, 10000, 20000]
OSM_REQUEST_TIMEOUT_SECONDS = 15
NEARBY_ROADS_COUNT = 3


def _point_to_polyline_distance_m(lat0, lng0, nodes):
    """nodes: list of {'lat':..,'lon':..} dicts describing a way's geometry
    (as returned by Overpass 'out geom'). Approximates point-to-polyline
    distance as the minimum distance to any vertex -- adequate at these
    search radii since OSM way segments are typically short relative to
    the search rings; a true point-to-segment projection would be
    marginally more precise but isn't worth the complexity here."""
    return min(_haversine_m(lat0, lng0, n["lat"], n["lon"]) for n in nodes if "lat" in n and "lon" in n)


def _query_osm_nearby_roads(lat0, lng0, cell, top_n=NEARBY_ROADS_COUNT):
    """
    Queries Overpass for real, named roads within 5km (widening to 10km/
    20km only if nothing at all turns up that close). Returns a list of
    up to `top_n` dicts {"name": str, "distance_m": int}, nearest first,
    deduped by name -- no major/minor classification, no special-casing
    by road tag: whatever real, named road is physically closest wins,
    motorway or residential street alike. Returns [] if OSM has no
    coverage in range or every request fails -- callers must fall back
    to the Google-based path in that case, never invent a distance.
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

patch("property_intel/google_client.py", old_a, new_a, "roads: straight 5km nearest, no major/minor split")

# ---------------------------------------------------------------------
# Fix B: malls -- widen the Places search radius to 10km specifically
# for nearby_shopping (every other category stays at 5km). No rating
# filter (already removed) -- every shopping_mall Google returns within
# 10km is a selling point on its own.
# ---------------------------------------------------------------------
old_b1 = '''def _search_nearby(cell: LocationCell, included_type):
    body = {
        "includedTypes": [included_type],
        "maxResultCount": 20,
        "locationRestriction": {
            "circle": {
                "center": {"latitude": float(cell.center_latitude), "longitude": float(cell.center_longitude)},
                "radius": 5000.0,
            }
        },
    }'''

new_b1 = '''def _search_nearby(cell: LocationCell, included_type, radius_m=5000.0):
    body = {
        "includedTypes": [included_type],
        "maxResultCount": 20,
        "locationRestriction": {
            "circle": {
                "center": {"latitude": float(cell.center_latitude), "longitude": float(cell.center_longitude)},
                "radius": radius_m,
            }
        },
    }'''

patch("property_intel/google_client.py", old_b1, new_b1, "malls: parameterize search radius")

old_b2 = '''def fetch_nearby_amenities(cell: LocationCell):
    update_fields = []
    for field_name, place_type in PLACE_CATEGORIES.items():
        try:
            setattr(cell, field_name, _search_nearby(cell, place_type))
            update_fields.append(field_name)
        except EnrichmentStepFailed:
            # One category failing (e.g. no banks nearby is a valid empty
            # result, but a genuine API error) shouldn't block the others.
            continue'''

new_b2 = '''# Shopping malls get a wider net than every other category -- a mall is
# a selling point regardless of a few extra km, and buyers/brokers think
# of "nearest mall" on a different scale than "nearest pharmacy".
PLACE_CATEGORY_RADII_M = {
    "nearby_shopping": 10000.0,
}


def fetch_nearby_amenities(cell: LocationCell):
    update_fields = []
    for field_name, place_type in PLACE_CATEGORIES.items():
        radius_m = PLACE_CATEGORY_RADII_M.get(field_name, 5000.0)
        try:
            setattr(cell, field_name, _search_nearby(cell, place_type, radius_m=radius_m))
            update_fields.append(field_name)
        except EnrichmentStepFailed:
            # One category failing (e.g. no banks nearby is a valid empty
            # result, but a genuine API error) shouldn't block the others.
            continue'''

patch("property_intel/google_client.py", old_b2, new_b2, "malls: 10km radius for nearby_shopping")

print("\nAll patches applied.")
