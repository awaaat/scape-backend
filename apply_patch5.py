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


# ===========================================================================
# pdf.py -- import _road_tier so we can tell which fetched road (if any)
# is the arterial/highway.
# ===========================================================================
old_import = '''from weasyprint import HTML

logger = logging.getLogger("property_intel")'''

new_import = '''from weasyprint import HTML

from .google_client import _road_tier

logger = logging.getLogger("property_intel")'''

patch("property_intel/pdf.py", old_import, new_import, "import _road_tier")


# ===========================================================================
# pdf.py -- local-name alias table + helper, display-layer only.
# ===========================================================================
old_town_qualify_def = '''def _town_qualified_road_name(cell, road_name):
    """Appends the nearest resolved town/city to a road name so it's
    unambiguous in the report -- 'Moi Avenue' exists in multiple Kenyan
    towns, and a bare name reads as more specific than it actually is."""
    if not road_name:
        return road_name
    nearest_town = _nearest_town_summary(cell)
    if nearest_town:
        town_label = nearest_town[0]
        if town_label.split()[0].upper() not in road_name.upper():
            return f"{road_name}, {town_label}"
    return road_name'''

new_town_qualify_def = '''# Some OSM ways are tagged by their official destination-to-destination
# name rather than the name Kenyans actually use day to day (e.g. the
# Thika Superhighway shows up in OSM as "Embu - Nairobi Highway" on the
# stretch past Thika, since that's where the route officially continues
# to). This map translates known cases so the report reads the way a
# local buyer/broker would immediately recognize -- add to it as new
# mismatches turn up rather than guessing ahead of time. Keyed by a
# normalized (lowercased, punctuation-stripped) form of the source name.
_LOCAL_ROAD_NAME_ALIASES = {
    "embu nairobi highway": "Thika Superhighway",
    "nairobi embu highway": "Thika Superhighway",
    "nairobi embu road": "Thika Superhighway",
    "nairobi thika highway": "Thika Superhighway",
    "thika nairobi highway": "Thika Superhighway",
    "thika road": "Thika Superhighway",
}


def _local_road_name(name):
    """Returns the locally-recognized name for a road if we know of one
    (see _LOCAL_ROAD_NAME_ALIASES), otherwise the original name
    unchanged. Applied at display time only -- the raw OSM/Google name
    is left untouched in the stored cell data for debugging."""
    if not name:
        return name
    key = re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()
    return _LOCAL_ROAD_NAME_ALIASES.get(key, name)


def _town_qualified_road_name(cell, road_name):
    """Appends the nearest resolved town/city to a road name so it's
    unambiguous in the report -- 'Moi Avenue' exists in multiple Kenyan
    towns, and a bare name reads as more specific than it actually is."""
    if not road_name:
        return road_name
    road_name = _local_road_name(road_name)
    nearest_town = _nearest_town_summary(cell)
    if nearest_town:
        town_label = nearest_town[0]
        if town_label.split()[0].upper() not in road_name.upper():
            return f"{road_name}, {town_label}"
    return road_name'''

patch("property_intel/pdf.py", old_town_qualify_def, new_town_qualify_def, "local road name alias")


# ===========================================================================
# pdf.py -- surface a nearby arterial/highway as its own highlight,
# separate from frontage. Previously nearby_roads[1:] were fetched but
# never shown anywhere in the report.
# ===========================================================================
old_frontage = '''        frontage_name = _town_qualified_road_name(cell, getattr(cell, "nearest_road_name", None))
        frontage_dist = getattr(cell, "nearest_road_distance_m", None)
        if not frontage_name:
            nearby_roads = getattr(cell, "nearby_roads", None) or []
            if nearby_roads:
                frontage_name = _town_qualified_road_name(cell, nearby_roads[0].get("name"))
                frontage_dist = nearby_roads[0].get("distance_m")'''

new_frontage = '''        frontage_name = _town_qualified_road_name(cell, getattr(cell, "nearest_road_name", None))
        frontage_dist = getattr(cell, "nearest_road_distance_m", None)
        nearby_roads = getattr(cell, "nearby_roads", None) or []
        if not frontage_name and nearby_roads:
            frontage_name = _town_qualified_road_name(cell, nearby_roads[0].get("name"))
            frontage_dist = nearby_roads[0].get("distance_m")

        # A nearby arterial/highway is a selling point in its own right,
        # separate from whatever road the plot actually fronts -- e.g.
        # fronting a residential street 36m away while sitting 4km from
        # the Thika Superhighway. nearby_roads holds up to 3 nearest
        # named roads with no major/minor filtering in the ranking, so
        # this only picks the nearest one that _road_tier calls "major"
        # and that isn't already the frontage road -- it doesn't change
        # which roads get fetched or how they're ranked.
        major_road_name, major_road_dist = None, None
        frontage_short = frontage_name.split(",")[0] if frontage_name else None
        for road in nearby_roads:
            candidate = _local_road_name(road.get("name"))
            if not candidate or _road_tier(candidate) != "major":
                continue
            if frontage_short and candidate == frontage_short:
                continue
            major_road_name, major_road_dist = candidate, road.get("distance_m")
            break'''

patch("property_intel/pdf.py", old_frontage, new_frontage, "surface nearby highway")


# ===========================================================================
# pdf.py -- add to quick facts strip.
# ===========================================================================
old_facts = '''        if frontage_name:
            facts.append({"label": "Frontage", "value": frontage_name.split(",")[0]})
        if county:'''

new_facts = '''        if frontage_name:
            facts.append({"label": "Frontage", "value": frontage_name.split(",")[0]})
        if major_road_name and major_road_dist is not None:
            facts.append({
                "label": "Nearest Highway",
                "value": f"{major_road_name} ({_format_distance_away(major_road_dist)})",
            })
        if county:'''

patch("property_intel/pdf.py", old_facts, new_facts, "highway quick fact")


# ===========================================================================
# pdf.py -- add to Location Highlights checklist.
# ===========================================================================
old_highlight = '''        if frontage_name and frontage_dist is not None and frontage_dist >= ROAD_DISTANCE_ALONG_THRESHOLD_M:
            short_name = frontage_name.split(",")[0]
            highlights.append({"text": f"Fronts {short_name}", "dist": _format_distance_away(frontage_dist)})'''

new_highlight = '''        if frontage_name and frontage_dist is not None and frontage_dist >= ROAD_DISTANCE_ALONG_THRESHOLD_M:
            short_name = frontage_name.split(",")[0]
            highlights.append({"text": f"Fronts {short_name}", "dist": _format_distance_away(frontage_dist)})
        if major_road_name and major_road_dist is not None:
            highlights.append({
                "text": f"{major_road_name} access",
                "dist": _format_distance_away(major_road_dist),
            })'''

patch("property_intel/pdf.py", old_highlight, new_highlight, "highway location highlight")

print("\nAll patches applied.")
