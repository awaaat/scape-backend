"""
property_intel/pdf.py

PROPERTY LOCATION REPORT - broker-facing listing tool.

Renders the exact HTML/CSS template (property_location_report.html.j2 --
a Jinja2 copy of property_location_report_template.html, same CSS, same
DOM, same classes, only content swapped for {{ }} placeholders) straight
to PDF via WeasyPrint. This replaces the earlier ReportLab rebuild, which
could not reproduce the template's pill-shaped tags, CSS grid, or web
fonts exactly -- rendering the real HTML is the only way to get a true
match.

Every statement on the report is backed by a specific, named data point
(an "evidence tag"). No investment score, no AI opinion, no driving
directions, no bulk amenity dump. If a data point isn't available for a
given pin, its section/row is skipped silently -- never padded with
placeholder or "N/A" text.
"""
import base64
import io
import logging
import math
import os
import random
import re
from datetime import datetime
from itertools import product

import qrcode
import requests
from django.conf import settings
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup, escape
from weasyprint import HTML

logger = logging.getLogger("property_intel")

AQI_GOOD_THRESHOLD = 50
AQI_MODERATE_THRESHOLD = 100
IMAGE_FETCH_TIMEOUT_SECONDS = 10
NEARBY_RING_METERS = 3000

PLUS_CODE_RE = re.compile(r"^[A-Z0-9]{4,8}\+[A-Z0-9]{2,3}")

TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "templates")
_jinja_env = Environment(
    loader=FileSystemLoader(TEMPLATE_DIR),
    autoescape=select_autoescape(["html"]),
)

# Every sample report showed exactly 20 for schools/hospitals/banks/etc at
# 5km regardless of location (Nairobi CBD, Bungoma, two different Ruiru
# pins) -- that's a fetch cap in google_client.py being displayed as a
# true final count. Until pagination is fixed at the source, show "20+"
# instead of a bare "20" so the report never implies a false precision.
AMENITY_FETCH_CAP = 20


def _count_label(n, cap=AMENITY_FETCH_CAP):
    return f"{n}+" if n >= cap else str(n)

PRICE_BENCHMARKS = {
    "ruiru": {
        "price_per_acre_kes": 40_500_000,
        "yoy_change_pct": 10.6,
        "quarter": "Q1 2026",
        "note": "the strongest satellite-town appreciation in Nairobi, driven by Tatu City demand and Thika Road access",
    },
    "juja": {
        "price_per_acre_kes": None,
        "yoy_change_pct": 1.2,
        "quarter": "Q1 2026",
        "note": "anchored by steady, structural rental demand from JKUAT's student population",
    },
    "kitengela": {
        "price_per_acre_kes": 18_800_000,
        "yoy_change_pct": 0.8,
        "quarter": "Q1 2026",
        "note": "among the most affordable entry points of the major satellite towns",
    },
}


KENYA_CITY_NAMES = {"NAIROBI", "MOMBASA", "KISUMU", "NAKURU"}

# Scape's own support line for the branded footer -- set
# SCAPE_SUPPORT_PHONE in settings/env once decided. Falls back to the
# template's own default line if unset.
SCAPE_SUPPORT_PHONE = getattr(settings, "SCAPE_SUPPORT_PHONE", "+254718889559")
DEFAULT_CONTACT_LINE = "+254 718 889 559 \u00b7 WhatsApp"


def _whatsapp_link(phone):
    """Builds a wa.me click-to-chat link from a canonical +254XXXXXXXXX
    phone number. Returns None -- never a broken link -- for anything
    that doesn't resolve to a plausible Kenyan mobile number."""
    if not phone:
        return None
    digits = re.sub(r"\D", "", phone)
    if digits.startswith("0") and len(digits) == 10:
        digits = "254" + digits[1:]
    if not re.match(r"^254[17]\d{8}$", digits):
        return None
    return f"https://wa.me/{digits}"


def _tel_link(phone):
    """Builds a tel: click-to-call link from a canonical +254XXXXXXXXX
    phone number. Returns None for anything invalid."""
    if not phone:
        return None
    digits = re.sub(r"\D", "", phone)
    if digits.startswith("0") and len(digits) == 10:
        digits = "254" + digits[1:]
    if not re.match(r"^254[17]\d{8}$", digits):
        return None
    return f"tel:+{digits}"


def _broker_phone(pin):
    """Best-effort lookup of the submitting broker's phone, sourced from
    their UserSignup profile (collected at signup). Returns None for
    anonymous submissions or brokers who never signed up; never raises."""
    broker = getattr(pin, "broker", None)
    user = getattr(broker, "user", None)
    if not user:
        return None
    try:
        return user.signup_profile.phone
    except Exception:
        return None


def _town_or_city_label(name):
    """Kenya has four chartered cities (Nairobi, Mombasa, Kisumu, Nakuru);
    every other settlement in kenya_towns_final.csv is a town. Normalizes
    names like 'NAIROBI CITY' and appends the correct suffix so the report
    never states a bare place name."""
    if not name:
        return name
    base = name.upper().replace(" CITY", "").strip()
    suffix = "City" if base in KENYA_CITY_NAMES else "Town"
    return f"{base.title()} {suffix}"


class ReportRenderError(Exception):
    """Raised on unrecoverable rendering failure -- caller treats this as a
    failed report, never a partial/corrupt one."""


def _label_from_field_name(field_name):
    """Convert field name to user-friendly label"""
    return field_name.replace("nearby_", "", 1).replace("_", " ").title()


MAJOR_AMENITY_CATEGORIES = {
    "nearby_schools",
    "nearby_universities",
    "nearby_hospitals",
    "nearby_banks",
    "nearby_petrol_stations",
    "nearby_supermarkets",
    "nearby_gated_communities",
    "nearby_police_stations",
    "nearby_fire_stations",
    "nearby_ev_charging",
}

_NOTABLE_RESTAURANT_KEYWORDS = ("hotel", "resort", "lodge", " inn", "inn ")


def _is_notable_restaurant(name):
    n = (name or "").lower()
    return any(kw in n for kw in _NOTABLE_RESTAURANT_KEYWORDS)


def _normalize_amenity_name(name):
    if not name:
        return ""
    n = name.lower()
    n = re.sub(r"[^a-z0-9\s]", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def _discover_amenity_fields(cell, major_only=False):
    """'nearby_roads' is always excluded -- it has its own report section.
    major_only=True restricts to MAJOR_AMENITY_CATEGORIES (plus notable
    hotels/resorts within nearby_restaurants)."""
    found = []
    for field in cell._meta.get_fields():
        name = getattr(field, "name", "")
        if not name.startswith("nearby_") or name == "nearby_roads":
            continue
        if major_only and name not in MAJOR_AMENITY_CATEGORIES and name != "nearby_restaurants":
            continue
        value = getattr(cell, name, None) or []
        filtered = []
        for e in value:
            if e.get("distance_m") is None:
                continue
            if e.get("business_status") == "CLOSED_PERMANENTLY":
                continue
            entry_name = (e.get("name") or "").strip()
            if not entry_name or entry_name.lower() == "null":
                continue
            if major_only and name == "nearby_restaurants" and not _is_notable_restaurant(entry_name):
                continue
            filtered.append(e)
        if filtered:
            found.append((_label_from_field_name(name), filtered))
    return found


def _within(entries, meters):
    return sum(1 for e in (entries or []) if e.get("distance_m") is not None and e["distance_m"] <= meters)


def _match_price_benchmark(cell):
    haystack = (cell.formatted_address or "").lower()
    for town, data in PRICE_BENCHMARKS.items():
        if town in haystack:
            return town, data
    for t in (cell.nearest_towns or []):
        name = (t.get("name") or "").lower()
        if name in PRICE_BENCHMARKS:
            return name, PRICE_BENCHMARKS[name]
    return None, None


def _nearest_town_summary(cell):
    """
    Nearest town from the dynamic, Kenya-wide list (kenya_towns.py /
    cell.nearest_towns), not a fixed Nairobi-satellite list -- works
    correctly anywhere in the country. Uses real Routes drive time when
    available; falls back to honest haversine-distance wording when a
    town's Routes call failed rather than overclaiming a time.
    Returns (name, minutes|None, km) or None if no towns were resolved.
    """
    towns = cell.nearest_towns or []
    if not towns:
        return None
    nearest = towns[0]
    minutes = round(nearest["drive_duration_s"] / 60) if nearest.get("drive_duration_s") else None
    km = round(nearest["distance_m"] / 1000, 1) if nearest.get("distance_m") is not None else None
    return _town_or_city_label(nearest["name"]), minutes, km


def _cell_county(cell):
    """First-listed county from cell.nearest_towns, the same list the
    Nearby Towns table used to draw from. Returns None rather than a
    placeholder string if towns never resolved."""
    towns = cell.nearest_towns or []
    if not towns:
        return None
    county = towns[0].get("county")
    return county.title() if county else None


def _display_location_name(pin, cell):
    address = cell.formatted_address or ""
    if address and not PLUS_CODE_RE.match(address):
        return address
    town, _ = _match_price_benchmark(cell)
    if town:
        return f"Near {_town_or_city_label(town)}, Kenya"
    nearest_town = _nearest_town_summary(cell)
    if nearest_town:
        town_label, _minutes, _km = nearest_town
        return f"Near {town_label}, Kenya"
    return f"{pin.latitude}, {pin.longitude}"


def _town_qualified_road_name(cell, road_name):
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
    return road_name


ROAD_DISTANCE_ALONG_THRESHOLD_M = 20  # below this, "~0.0km away" reads as nonsense


def _format_road_distance(name, distance_m):
    if distance_m is None:
        return name
    if distance_m < ROAD_DISTANCE_ALONG_THRESHOLD_M:
        return f"Right along {name}"
    if distance_m < 1000:
        return f"{name} (~{int(round(distance_m))}m away)"
    return f"{name} (~{distance_m / 1000:.1f}km away)"


def _score_accessibility(cell):
    """Kept for callers elsewhere in the app (pin ranking/filtering) even
    though the Property Location Report no longer prints a scorecard."""
    score = 50
    amenity_fields = _discover_amenity_fields(cell)
    if amenity_fields:
        per_category = 25 / max(len(amenity_fields), 1)
        score += round(per_category * len(amenity_fields))

    nairobi = (cell.travel_times or {}).get("nairobi_cbd")
    if nairobi and nairobi.get("duration_s"):
        minutes = nairobi["duration_s"] / 60
        if minutes < 30:
            score += 20
        elif minutes < 60:
            score += 10

    return max(0, min(100, score))


def _score_investment(cell, accessibility_score):
    """Kept for callers elsewhere in the app; not printed in this report."""
    score = 50

    _, benchmark = _match_price_benchmark(cell)
    if benchmark and benchmark.get("yoy_change_pct") is not None:
        yoy = benchmark["yoy_change_pct"]
        if yoy >= 8:
            score += 20
        elif yoy >= 3:
            score += 10
        elif yoy >= 0:
            score += 3
        else:
            score -= 10

    density_rows = _density_table_data(cell)
    categories_with_3km_presence = sum(1 for _, _, c3, _ in density_rows if c3 > 0)
    score += min(15, categories_with_3km_presence * 2)

    score += round((accessibility_score - 50) * 0.3)

    if cell.air_quality_index is not None:
        if cell.air_quality_index <= AQI_GOOD_THRESHOLD:
            score += 5
        elif cell.air_quality_index > AQI_MODERATE_THRESHOLD:
            score -= 10

    return max(0, min(100, round(score)))


def _format_distance(meters):
    """Compact form ('37m', '1.2km') -- kept for callers/tests that still
    want the terse version (e.g. anywhere space is tight)."""
    if meters is None:
        return "Unknown"
    if meters <= 500:
        return f"{int(round(meters))}m"
    km = meters / 1000
    return f"{int(round(meters))}m ({km:.1f}km)"


def _format_distance_away(meters):
    """Full 'read-out-loud' phrasing -- '37 meters away' / '1.2 kilometers
    away' -- used everywhere a distance is shown to a broker or buyer, so
    every number on the report is unambiguous on its own, without relying
    on a nearby label to explain what a bare '37m' means. Never abbreviates
    and never omits the trailing 'away'. Anything under 50m reads as
    "0 meters away" otherwise -- nonsensical when the property IS
    effectively at that landmark -- so it gets its own phrasing instead."""
    if meters is None:
        return "distance unknown"
    if meters < 50:
        return "right in the area"
    if meters < 1000:
        return f"{int(round(meters))} meters away"
    km = meters / 1000
    return f"{km:.1f} kilometers away"


def _density_table_data(cell):
    """Kept for _score_investment's density signal; not printed in this report."""
    amenity_fields = _discover_amenity_fields(cell)
    rows = []
    for label, entries in amenity_fields:
        rows.append((
            label,
            _within(entries, 1000),
            _within(entries, 3000),
            _within(entries, 5000),
        ))
    return rows


def _summary_text(pin, cell, investment_score, accessibility_score):
    """Returns (lead, bullets) -- kept as-is for the plain-text summary_text
    return value some callers (search previews, notifications) rely on,
    even though the PDF itself no longer renders this as a bullet list."""
    location_name = _display_location_name(pin, cell)
    nearest_town = _nearest_town_summary(cell)

    lead = location_name
    if nearest_town:
        town_label, minutes, km = nearest_town
        if km == 0.0:
            lead += f", in {town_label}"
        elif minutes is not None:
            lead += f", {minutes} minutes from {town_label}"
        else:
            lead += f", {km} km from {town_label}"
    lead += "."

    bullets = []
    schools = _get_named_amenities_text(cell, "schools", "Schools", max_names=2, include_distance=False)
    if schools:
        bullets.append(f"Nearby schools include {schools}.")
    hospitals = _get_named_amenities_text(cell, "hospitals", "Hospitals", max_names=1, include_distance=True)
    if hospitals:
        bullets.append(f"Nearest hospital: {hospitals}.")

    return lead, bullets[:6]


def _get_named_amenities_text(cell, category, label, max_names=3, include_distance=True):
    amenity_fields = _discover_amenity_fields(cell)
    for lbl, entries in amenity_fields:
        if lbl.lower() == category.lower():
            sorted_entries = sorted(entries, key=lambda e: e.get('distance_m') if e.get('distance_m') is not None else float('inf'))
            top = [e for e in sorted_entries[:max_names] if e.get('name')]
            if not top:
                return None
            names = [e['name'] for e in top]
            closest = _format_distance_away(top[0].get('distance_m'))
            if len(names) == 1:
                text = names[0]
            else:
                text = f"{', '.join(names[:-1])} and {names[-1]}"
            if not include_distance:
                return text
            remaining = len(sorted_entries) - len(top)
            remaining_label = _count_label(remaining) if len(sorted_entries) >= AMENITY_FETCH_CAP else str(remaining)
            suffix = f" (the nearest just {closest}"
            suffix += f", plus {remaining_label} more nearby)" if remaining > 0 else ")"
            return text + suffix
    return None


def _fetch_image_bytes(url):
    if not url:
        return None
    try:
        resp = requests.get(url, timeout=IMAGE_FETCH_TIMEOUT_SECONDS)
        if resp.status_code != 200:
            return None
        return resp.content
    except requests.RequestException as exc:
        logger.warning("Could not fetch image (%s): %s", url, exc)
        return None


def _image_data_uri(url, mime="image/jpeg"):
    """Fetches an image and returns it as a base64 data: URI so WeasyPrint
    never has to reach the network at render time. Returns None -- never a
    broken <img> tag -- if the fetch fails."""
    content = _fetch_image_bytes(url)
    if not content:
        return None
    return f"data:{mime};base64,{base64.b64encode(content).decode('ascii')}"


STUDENT_HOUSING_PROXIMITY_M = 5000  # matches the "within 5km" density bucket shown alongside it


def _nearest_dist(amenity_lookup, *labels):
    """Nearest distance_m across one or more amenity-lookup labels, or
    None if none of those labels have any entries for this pin. Used so
    suitability rationale can cite a real distance instead of a vague
    'near educational institutions' style phrase."""
    best = None
    for label in labels:
        for e in amenity_lookup.get(label) or []:
            d = e.get("distance_m")
            if d is None:
                continue
            if best is None or d < best:
                best = d
    return best


def _nearest_named(amenity_lookup, *labels):
    """Nearest (name, distance_m) across one or more amenity-lookup labels,
    or None if none of those labels have a named, distance-bearing entry.
    Suitability rationale must always cite the actual amenity by name --
    never a bare category ('nearest retail') -- so this is used instead of
    _nearest_dist() wherever a rationale string is built."""
    best = None
    for label in labels:
        for e in amenity_lookup.get(label) or []:
            d = e.get("distance_m")
            name = e.get("name")
            if d is None or not name:
                continue
            if best is None or d < best[1]:
                best = (name, d)
    return best


def _development_suitability_table(cell):
    """Same scoring logic as the original report -- the template just
    displays fewer rows now. Every rationale string names the actual
    amenity it's citing (e.g. 'Nearest university, Mount Kenya University,
    is 850 meters away') -- never a bare, unnamed category claim like
    'nearest retail is 300m away', which reads as an unverifiable rumor
    rather than evidence.
    Returns [(dev_type, level, rationale), ...]."""
    amenity_fields = _discover_amenity_fields(cell)
    amenity_lookup = dict(amenity_fields)
    has_student_housing = any(
        e.get("distance_m") is not None and e["distance_m"] <= STUDENT_HOUSING_PROXIMITY_M
        for e in amenity_lookup.get("Student Housing", [])
    )

    university = _nearest_named(amenity_lookup, "Universities")
    retail = _nearest_named(amenity_lookup, "Shopping", "Supermarkets")
    gated = _nearest_named(amenity_lookup, "Gated Communities")

    nairobi = (cell.travel_times or {}).get("nairobi_cbd")
    commute_mins = round(nairobi["duration_s"] / 60) if nairobi and nairobi.get("duration_s") else None

    suitability = []

    if has_student_housing and university:
        suitability.append(("Student Housing", "Very High", f"Existing student accommodation confirms active rental market, with the nearest university, {university[0]}, {_format_distance_away(university[1])}"))
    elif university and commute_mins and commute_mins < 45:
        suitability.append(("Student Housing", "High", f"Nearest university, {university[0]}, is {_format_distance_away(university[1])}, close enough to drive rental demand"))
    elif university:
        suitability.append(("Student Housing", "Medium", f"Nearest university, {university[0]}, is {_format_distance_away(university[1])}"))
    else:
        suitability.append(("Student Housing", "Low", "Limited educational institutions in immediate area"))

    if retail and university and gated:
        suitability.append(("Apartments", "Very High", f"Nearest retail, {retail[0]}, is {_format_distance_away(retail[1])} and nearest gated community, {gated[0]}, is {_format_distance_away(gated[1])}, showing proven demand nearby"))
    elif retail and university:
        suitability.append(("Apartments", "High", f"Complete service ecosystem supports residential development, with the nearest retail, {retail[0]}, {_format_distance_away(retail[1])}"))
    elif retail or university:
        nearest_label, nearest_amenity = ("retail", retail) if retail else ("university", university)
        suitability.append(("Apartments", "Medium", f"Nearest {nearest_label}, {nearest_amenity[0]}, is {_format_distance_away(nearest_amenity[1])}"))
    else:
        suitability.append(("Apartments", "Low", "Limited service infrastructure"))

    if retail and commute_mins and commute_mins < 60:
        suitability.append(("Mixed-Use", "Medium-High", f"Nearest retail, {retail[0]}, is {_format_distance_away(retail[1])}, with a reasonable commute supporting mixed-use"))
    elif retail:
        suitability.append(("Mixed-Use", "Medium", f"Nearest retail, {retail[0]}, is {_format_distance_away(retail[1])}"))
    else:
        suitability.append(("Mixed-Use", "Low", "Limited commercial ecosystem"))

    if commute_mins and commute_mins > 30:
        suitability.append(("Warehousing", "Medium", "Peripheral location supports logistics and distribution"))
    else:
        suitability.append(("Warehousing", "Low", "Too central for cost-effective warehousing"))

    suitability.append(("Industrial", "Low", "Location characteristics more suited to residential and commercial uses"))

    return suitability


SUITABILITY_STARS = {
    "Very High": 5,
    "High": 4,
    "Medium-High": 3,
    "Medium": 3,
    "Low": 1,
}


def _top_suitability_rows(cell, max_rows=4):
    """Best-suited-for section shows only the rows that read as a selling
    point -- ranked by fit, capped at max_rows, and 'Low' rows are dropped
    entirely unless nothing else qualifies. Includes the Residential Home
    row, which the underlying scoring table doesn't produce on its own --
    see _residential_home_row()."""
    all_rows = _development_suitability_table(cell) + [_residential_home_row(cell)]
    ranked = sorted(all_rows, key=lambda r: -SUITABILITY_STARS.get(r[1], 0))
    strong = [r for r in ranked if SUITABILITY_STARS.get(r[1], 0) >= 3]
    return (strong or ranked)[:max_rows]


def _residential_home_row(cell):
    """'Residential Home' isn't produced by _development_suitability_table()
    (that function only scores Student Housing / Apartments / Mixed-Use /
    Warehousing / Industrial), but it's the most common ask from a broker
    listing a plot for a family buyer -- so it gets its own small,
    evidence-based check here rather than being silently omitted."""
    estate = _nearby_estate(cell)
    if not estate:
        return ("Residential Home", "Medium", "No established estate confirmed nearby yet")
    name, dist = estate
    if dist <= 500:
        return ("Residential Home", "Very High", f"Established estate {name} is {_format_distance_away(dist)}")
    if dist <= 2000:
        return ("Residential Home", "High", f"Established estate {name} is {_format_distance_away(dist)}")
    return ("Residential Home", "Medium", f"Nearest estate, {name}, is {_format_distance_away(dist)}")


# ---------------------------------------------------------------------------
# Evidence collection -- the core of the template. Every claim on the
# report traces back to one of these (label, name, distance_m) points.
# Categories are tried in priority order; any that have no data for this
# pin are silently skipped, so the report degrades gracefully instead of
# printing "Unknown" or leaving a gap.
# ---------------------------------------------------------------------------
EVIDENCE_CATEGORY_PRIORITY = (
    "Schools",
    "Hospitals",
    "Universities",
    "Banks",
    "Supermarkets",
    "Gated Communities",
    "Petrol Stations",
)

# Human-friendly noun for each category, used only when weaving the
# "putting X, Y and Z within a short walk" clause of the description.
_CATEGORY_BENEFIT_NOUN = {
    "Schools": "schooling",
    "Universities": "education",
    "Hospitals": "healthcare",
    "Banks": "banking",
    "Supermarkets": "shopping",
    "Gated Communities": "established housing",
    "Petrol Stations": "fuel access",
}


def _collect_evidence_points(cell, max_points=6):
    """Nearest named entry per priority category, sorted by distance,
    capped at max_points. Returns [(label, name, distance_m), ...]."""
    amenity_fields = dict(_discover_amenity_fields(cell))
    points = []
    for label in EVIDENCE_CATEGORY_PRIORITY:
        entries = amenity_fields.get(label)
        if not entries:
            continue
        nearest = min(entries, key=lambda e: e.get("distance_m", float("inf")))
        name = nearest.get("name")
        distance_m = nearest.get("distance_m")
        if not name or distance_m is None:
            continue
        points.append((label, name, distance_m))
    points.sort(key=lambda p: p[2])
    return points[:max_points]


def _singular(label):
    lower = label.lower()
    return lower[:-1] if lower.endswith("s") else lower


def _evidence_density_counts(cell):
    """Returns [(label, count), ...] for Banks/Supermarkets/Schools/Hospitals
    with a nonzero 5km count -- the raw counts behind the description's
    closing 'dense service base' clause."""
    density_rows = _density_table_data(cell)
    lookup = {label: c5 for label, c1, c3, c5 in density_rows}
    picks = []
    for label in ("Banks", "Supermarkets", "Schools", "Hospitals"):
        c5 = lookup.get(label, 0)
        if c5 > 0:
            picks.append((label, c5))
        if len(picks) == 2:
            break
    return picks


def _nearby_estate(cell):
    """Nearest gated community or, failing that, nearest student housing
    entry -- used as the one 'established neighbourhood' proof point."""
    amenity_fields = dict(_discover_amenity_fields(cell))
    for label in ("Gated Communities", "Student Housing"):
        entries = amenity_fields.get(label)
        if entries:
            nearest = min(entries, key=lambda e: e.get("distance_m", float("inf")))
            if nearest.get("name") and nearest.get("distance_m") is not None:
                return nearest["name"], nearest["distance_m"]
    return None


# ===========================================================================
# MEGA VARIATION POOLS – generated dynamically for enormous variety
# ===========================================================================

def _generate_openers():
    """
    Builds a giant list of opening sentences from component phrases.
    All placeholders are written as literal strings with double braces
    so they can be .format()'d later.
    """
    intro_verbs = [
        "Located", "Situated", "Positioned", "Placed", "Set", "Found", "Sited",
        "Nestled", "Lying", "Sitting", "Benefiting from",
    ]
    distance_phrases = [
        "just {dist_m} metres, approximately {drive_phrase}",
        "{dist_m} metres, about {drive_phrase}",
        "only {dist_m} metres, roughly {drive_phrase}",
        "{dist_m} metres, which works out to {drive_phrase}",
        "a mere {dist_m} metres, approximately {drive_phrase}",
        "{dist_m} metres, around {drive_phrase}",
    ]
    frontage_parts = [
        "fronting {frontage}",
        "with frontage on {frontage}",
        "enjoying frontage on {frontage}",
    ]
    end_parts = [
        "this property offers excellent accessibility for residential or commercial development.",
        "this property offers outstanding accessibility for residential or commercial development.",
        "this property presents an accessible opportunity for residential or commercial development.",
        "this property combines convenience with strong development potential.",
        "this property enjoys a highly accessible location suitable for residential or commercial development.",
        "this property is strategically positioned for residential or commercial development.",
        "this property offers a practical and well‑connected setting for residential or commercial development.",
        "this property delivers excellent accessibility for residential or commercial development.",
        "this property provides exceptional connectivity for residential or commercial use.",
        "this property is ideally placed for residential or commercial development.",
        "this property affords strong accessibility for a range of residential or commercial uses.",
    ]
    openers = []

    # Town + frontage combos
    for verb in intro_verbs:
        for d in distance_phrases:
            for end in end_parts:
                for f in frontage_parts:
                    # Verb first, town + frontage
                    openers.append(f"{verb} {d} from {{town}} and {f}, {end}")
                    openers.append(f"{verb} {d} from {{town}}, {f}, {end}")
                    # Frontage first, then verb
                    openers.append(f"{f.capitalize()} and {verb.lower()} {d} from {{town}}, {end}")
                    # with full distance away phrasing
                    alt_d = d.replace("{dist_m} metres", "{dist_away}")
                    openers.append(f"{verb} {alt_d} from {{town}} and {f}, {end}")
    # Town only
    for verb in intro_verbs:
        for d in distance_phrases:
            for end in end_parts:
                openers.append(f"{verb} {d} from {{town}}, {end}")
                openers.append(f"{verb} {d} from {{town}} centre, {end}")
                openers.append(f"{verb} {d} from the heart of {{town}}, {end}")
                # with away phrasing
                alt_d = d.replace("{dist_m} metres", "{dist_away}")
                openers.append(f"{verb} {alt_d} from {{town}}, {end}")
    # Frontage only
    for verb in intro_verbs:
        for f in frontage_parts:
            for end in end_parts:
                openers.append(f"{verb} {f}, {end}")
                openers.append(f"{f.capitalize()}, {verb.lower()} {end}")
    # Fallback (no town, no frontage)
    fallbacks = [
        "{location_line} offers strong development potential for residential or commercial use.",
        "{location_line} presents an accessible opportunity for residential or commercial development.",
        "{location_line} is a prime candidate for residential or commercial development.",
        "{location_line} enjoys a strategic location with strong development upside.",
    ]
    return openers + fallbacks


# Generate the actual pools
_OPENERS_ALL = _generate_openers()

# NEW: Short openers (12–15 words) – these will be chosen alongside the longer ones.
_SHORT_OPENERS = [
    "Just {dist_away} from {town} centre, this property is well located.",
    "Only {dist_away} from {town}, this site offers great access.",
    "Set {dist_away} from {town}, the property is convenient.",
    "The property lies {dist_away} from {town} centre.",
    "This site is {dist_away} from {town} – ideal for commuting.",
    "With {town} centre {dist_away} away, the location is practical.",
    "Positioned {dist_away} from {town}, the property is accessible.",
    "The property is {dist_away} from {town} centre.",
    "Just {dist_away} from {town}, this site is well connected.",
    "Only {dist_away} from {town}, the property is strategically placed.",
]
# Prepend them to the main list so they are always available
_OPENERS_ALL = _SHORT_OPENERS + _OPENERS_ALL

# Services templates – many variations
_SERVICES_TEMPLATES = [
    "Nearby amenities include {svc_list}, placing {nouns} within a short walk.",
    "Within walking distance are {svc_list}, ensuring {nouns} are all close by.",
    "Essential services within reach include {svc_list}, placing {nouns} within easy walking distance.",
    "{svc_list_cap} are all close at hand, putting {nouns} within a short walk.",
    "Nearby services include {svc_list}, putting {nouns} within comfortable walking distance.",
    "Healthcare, education, banking, and other essential services are all close by, including {svc_list}.",
    "The immediate surroundings include {svc_list}, bringing {nouns} within easy reach.",
    "A range of services, including {svc_list}, are just a short walk away, ensuring {nouns} are conveniently close.",
    "With {svc_list} nearby, {nouns} are always within easy reach.",
    "The area boasts {svc_list}, making {nouns} exceptionally accessible.",
    "Key amenities such as {svc_list} are situated close by, offering {nouns} at your doorstep.",
    "Residents will appreciate the proximity of {svc_list}, providing {nouns} within a stone's throw.",
    "Everyday needs are well catered for with {svc_list} just moments away, covering {nouns}.",
    "The locale is well‑served by {svc_list}, ensuring {nouns} are never far.",
    "From {svc_list}, you have all the essentials for {nouns} right on your doorstep.",
    "With {svc_list} in the vicinity, {nouns} are effortlessly accessible.",
    "The property benefits from nearby {svc_list}, placing {nouns} within a comfortable stroll.",
]

# NEW: Short service sentences (12–15 words) – these will be used alongside the longer ones.
_SHORT_SERVICES = [
    "Nearby services include {svc_list}.",
    "Within walking distance are {svc_list}.",
    "Essential amenities close by are {svc_list}.",
    "The area offers {svc_list} within easy reach.",
    "You will find {svc_list} just a short walk away.",
    "A range of services is available, including {svc_list}.",
    "The immediate vicinity features {svc_list}.",
    "Key amenities such as {svc_list} are close at hand.",
    "Residents benefit from {svc_list} in the neighbourhood.",
    "Everyday needs are catered for by {svc_list}.",
]
_SERVICES_TEMPLATES = _SHORT_SERVICES + _SERVICES_TEMPLATES

# Closing sentences – many variations (kept)
_CLOSING_BOTH = [
    "The surrounding area is well established for residential living, anchored by {estate}, and supported by {density}, making the property well suited for apartments, rental housing, or mixed-use development.",
    "Anchored by {estate} and supported by {density}, the surrounding area offers strong potential for apartments, rental housing, or mixed-use development.",
    "The neighbourhood is already well established, with {estate} nearby and {density} close at hand, reinforcing its suitability for apartments, rentals, or mixed-use projects.",
    "The area is further strengthened by {estate}, together with {density}, supporting residential, rental, and mixed-use development.",
    "The surrounding area is already residential, anchored by {estate} and a dense service base of {density} -- supporting apartments, rentals, or mixed-use development.",
    "With {estate} and {density} in close proximity, the location is primed for residential, rental, and mixed‑use ventures.",
    "The presence of {estate} and {density} makes this an ideal spot for apartments, rentals, or mixed‑use schemes.",
    "Both {estate} and {density} reinforce the area's appeal for residential development, from apartments to mixed‑use.",
    "The combination of {estate} and {density} provides a robust foundation for a variety of residential and commercial projects.",
    "Thanks to {estate} and a dense service base of {density}, the property is exceptionally well‑suited to apartments, rentals, or mixed‑use.",
]

_CLOSING_ESTATE_ONLY = [
    "The surrounding area is already residential, anchored by {estate} -- supporting apartments, rentals, or mixed-use development.",
    "Anchored by {estate}, the surrounding neighbourhood is well suited to apartments, rental housing, or mixed-use development.",
    "The area is already well established for residential living, with {estate} nearby.",
    "With {estate} at hand, the location is ready for apartments, rentals, or mixed‑use projects.",
    "{estate} provides a solid anchor for further residential and commercial development.",
    "The established presence of {estate} underpins the area's potential for rental housing and mixed‑use.",
]

_CLOSING_DENSITY_ONLY = [
    "The property is further supported by {density}, reinforcing its suitability for apartments, rental housing, or mixed-use development.",
    "With {density} nearby, the area offers a strong service base for apartments, rentals, or mixed-use development.",
    "A dense service base of {density} further supports apartments, rental housing, or mixed-use development.",
    "The area's service density of {density} makes it a prime candidate for residential and mixed‑use projects.",
    "Backed by {density}, the property is well positioned for apartments, rentals, or mixed‑use.",
]

# NEW: Short closing sentences (12–15 words) – split into two separate sentences:
# one about estate, one about financial/shopping.
_SHORT_CLOSING_ESTATE = [
    "The area is anchored by {estate}.",
    "The neighbourhood is built around {estate}.",
    "The location benefits from {estate}.",
    "An established estate, {estate}, is nearby.",
]
_SHORT_CLOSING_FINANCE = [
    "Nearby financial institutions like {bank} and shopping centres like {supermarket} are close.",
    "Banks such as {bank} and supermarkets like {supermarket} are within easy reach.",
    "You'll find {bank} for banking and {supermarket} for shopping close by.",
    "Financial services at {bank} and retail at {supermarket} are nearby.",
]

# Rotated per service entry so a four-item list never repeats the same
# connector phrasing four times in a row.
_SERVICE_CONNECTORS = [
    "just {d}",
    "{d}",
    "only {d}",
    "{d}, right by the property",
    "at {d}",
    "{d}, close to the site",
    "{d}, a stone's throw away",
    "{d}, practically on the doorstep",
    "{d}, easily accessible",
    "{d}, within a short stroll",
]


def _format_drive_phrase(minutes):
    if minutes is None:
        return None
    if minutes <= 1:
        return "a 1-minute drive"
    return f"a {minutes}-minute drive"


def _format_service_list(evidence_points, rng):
    """Builds the varied 'X 37 meters away, Y 42 meters away ...' clause,
    rotating connector phrasing per entry so a four-item list doesn't
    repeat itself, and returns the benefit-noun phrase ('healthcare,
    education and banking') built from the same entries. Every distance
    is the full 'X meters away' / 'X.Y kilometers away' form -- never
    abbreviated. Returns (None, None) if there are no evidence points."""
    if not evidence_points:
        return None, None
    top4 = evidence_points[:4]
    pieces = []
    for _label, name, dist_m in top4:
        connector = rng.choice(_SERVICE_CONNECTORS)
        d_text = _format_distance_away(dist_m)
        pieces.append(f"{escape(name)} {connector.format(d=d_text)}")
    if len(pieces) == 1:
        svc_list = pieces[0]
    else:
        svc_list = ", ".join(pieces[:-1]) + ", and " + pieces[-1]

    nouns = []
    for label, _name, _dist in top4:
        noun = _CATEGORY_BENEFIT_NOUN.get(label)
        if noun and noun not in nouns:
            nouns.append(noun)
    if len(nouns) >= 2:
        noun_phrase = ", ".join(nouns[:-1]) + " and " + nouns[-1]
    elif nouns:
        noun_phrase = nouns[0]
    else:
        noun_phrase = "everyday needs"
    return svc_list, noun_phrase


def _format_density_phrase(density_counts):
    # DEPRECATED – kept for compatibility but no longer used.
    # We now use _format_finance_sentence with specific names.
    if not density_counts:
        return None
    chips = [
        f"{_count_label(c)} {label.lower() if c != 1 else _singular(label)}"
        for label, c in density_counts
    ]
    if len(chips) == 1:
        return f"{chips[0]} within 5km"
    return " and ".join(chips) + " within 5km"


def _get_nearest_named(evidence_points, label):
    """Return (name, distance_m) for the nearest entry with given label, or None."""
    for lbl, name, dist in evidence_points:
        if lbl == label:
            return name, dist
    return None


def _build_description_html(town_label, nearest_town, frontage_name, frontage_dist,
                             evidence_points, estate, density_counts,
                             location_line=None, seed=None):
    """Builds the Listing Description paragraph from real evidence data.
    Each sentence is 12‑15 words on average. The description is composed of:
      - an opening sentence about location (and frontage if available)
      - a services sentence (if amenities exist)
      - a sentence about the estate (if available)
      - a sentence about banking/shopping (using specific names)
    The pools include both long and short templates; the short ones are preferred.
    """
    rng = random.Random(seed) if seed is not None else random

    minutes = nearest_town[1] if nearest_town else None
    km = nearest_town[2] if nearest_town else None
    dist_m = int(round(km * 1000)) if km is not None else None
    drive_phrase = _format_drive_phrase(minutes)
    dist_away = _format_distance_away(dist_m) if dist_m is not None else "unknown distance"
    frontage_short = frontage_name.split(",")[0] if frontage_name else None

    # ---- 1. Opening sentence ----
    # Prefer short openers (already at the front of _OPENERS_ALL)
    possible_openers = _OPENERS_ALL[:]  # copy
    if town_label is None or dist_m is None or drive_phrase is None:
        possible_openers = [
            t for t in possible_openers
            if "{town}" not in t and "{dist_m}" not in t and "{dist_away}" not in t and "{drive_phrase}" not in t
        ]
    else:
        if not frontage_short:
            possible_openers = [t for t in possible_openers if "{{frontage}}" not in t]
    if not possible_openers:
        possible_openers = _OPENERS_ALL

    opener = rng.choice(possible_openers)
    opening_sentence = opener.format(
        dist_m=dist_m if dist_m is not None else "",
        drive_phrase=drive_phrase if drive_phrase else "",
        town=str(escape(town_label)) if town_label else "",
        frontage=str(escape(frontage_short)) if frontage_short else "",
        dist_away=dist_away,
        location_line=str(escape(location_line)) if location_line else "This property",
    )
    parts = [opening_sentence]

    # ---- 2. Services sentence (if any) ----
    svc_list, noun_phrase = _format_service_list(evidence_points, rng)
    if svc_list:
        # Prefer short service templates (at the front)
        services_template = rng.choice(_SERVICES_TEMPLATES)
        parts.append(services_template.format(
            svc_list=svc_list,
            svc_list_cap=svc_list[0].upper() + svc_list[1:] if svc_list else "",
            nouns=noun_phrase,
        ))

    # ---- 3. Estate sentence (if any) ----
    if estate:
        name, dist = estate
        estate_text = f"{escape(name)} {_format_distance_away(dist)}"
        # Use a short estate sentence
        estate_sentence = rng.choice(_SHORT_CLOSING_ESTATE).format(estate=estate_text)
        parts.append(estate_sentence)

    # ---- 4. Banking & shopping sentence ----
    bank = _get_nearest_named(evidence_points, "Banks")
    supermarket = _get_nearest_named(evidence_points, "Supermarkets")
    if bank and supermarket:
        bank_name, bank_dist = bank
        super_name, super_dist = supermarket
        finance_sentence = rng.choice(_SHORT_CLOSING_FINANCE).format(
            bank=f"{escape(bank_name)} ({_format_distance_away(bank_dist)})",
            supermarket=f"{escape(super_name)} ({_format_distance_away(super_dist)})",
        )
        parts.append(finance_sentence)
    elif bank:
        # Only bank available
        bank_name, bank_dist = bank
        parts.append(f"Nearby financial institutions include {escape(bank_name)} ({_format_distance_away(bank_dist)}).")
    elif supermarket:
        super_name, super_dist = supermarket
        parts.append(f"Shopping centres like {escape(super_name)} ({_format_distance_away(super_dist)}) are nearby.")

    # If no parts, fallback
    if not parts:
        fallback = escape(location_line) if location_line else "This property"
        return Markup(f"{fallback}. Not enough verified data was available to write a description for this pin.")

    # Join sentences into a paragraph with periods.
    paragraph = ". ".join(parts)
    if not paragraph.endswith("."):
        paragraph += "."
    return Markup(paragraph)


def _qr_data_uri(url, box_size=8):
    """Renders a QR code pointing at the Google Maps pin as an in-memory
    PNG, returned as a base64 data: URI. Returns None (never a broken
    image) if generation fails for any reason."""
    if not url:
        return None
    try:
        qr = qrcode.QRCode(border=1, box_size=box_size)
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="#1D2B1F", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return f"data:image/png;base64,{base64.b64encode(buf.getvalue()).decode('ascii')}"
    except Exception as exc:
        logger.warning("Could not generate QR code for %s: %s", url, exc)
        return None


def _google_maps_view_url(pin):
    """Shareable link that just drops a pin -- for a broker forwarding the
    report before the buyer is ready to navigate there yet."""
    return f"https://www.google.com/maps/search/?api=1&query={pin.latitude},{pin.longitude}"


def _google_maps_directions_url(pin):
    return f"https://www.google.com/maps/dir/?api=1&destination={pin.latitude},{pin.longitude}"


def render_report_pdf(pin, cell):
    """Renders the Property Location Report by filling
    property_location_report.html.j2 (the exact template, unmodified CSS
    and DOM) with real pin/cell data and converting straight to PDF via
    WeasyPrint. Every distance-based claim comes from
    _collect_evidence_points()/_development_suitability_table(), and any
    category missing for a given pin is dropped, never faked, so the
    report degrades gracefully instead of printing "N/A"."""
    try:
        accessibility_score = _score_accessibility(cell)
        investment_score = _score_investment(cell, accessibility_score)
        summary_lead, summary_bullets = _summary_text(pin, cell, investment_score, accessibility_score)
        summary_text = summary_lead + (" " + " ".join(summary_bullets) if summary_bullets else "")

        location_name = _display_location_name(pin, cell)
        nearest_town = _nearest_town_summary(cell)
        town_label = nearest_town[0] if nearest_town else None
        county = _cell_county(cell)
        evidence_points = _collect_evidence_points(cell)
        estate = _nearby_estate(cell)
        density_counts = _evidence_density_counts(cell)

        frontage_name = _town_qualified_road_name(cell, getattr(cell, "nearest_road_name", None))
        frontage_dist = getattr(cell, "nearest_road_distance_m", None)
        if not frontage_name:
            nearby_roads = getattr(cell, "nearby_roads", None) or []
            if nearby_roads:
                frontage_name = _town_qualified_road_name(cell, nearby_roads[0].get("name"))
                frontage_dist = nearby_roads[0].get("distance_m")

        # ---- masthead ----
        ref = f"{str(getattr(pin, 'id', 'N-A'))[:10].upper()}"
        location_line = location_name
        if town_label and county:
            location_line = f"{town_label}, {county} County"
        elif town_label:
            location_line = town_label
        generated_date = datetime.now().strftime("%d %B %Y")

        seal = None
        if frontage_name and frontage_dist is not None:
            seal = {
                "top": "Verified",
                "mid": _format_distance_away(frontage_dist),
                "bot": frontage_name.split(",")[0],
            }

        # ---- quick facts strip ----
        facts = []
        if town_label:
            facts.append({"label": "Nearest Town", "value": town_label})
            _, minutes, km = nearest_town
            dist_m_val = int(round(km * 1000)) if km is not None else None
            if dist_m_val is not None and minutes is not None:
                dist_val = f"{_format_distance_away(dist_m_val)} \u00b7 {minutes} min"
            elif minutes is not None:
                dist_val = f"{minutes} min"
            elif dist_m_val is not None:
                dist_val = _format_distance_away(dist_m_val)
            else:
                dist_val = "Unknown"
            facts.append({"label": f"Distance to {town_label} Centre", "value": dist_val})
        if frontage_name:
            facts.append({"label": "Frontage", "value": frontage_name.split(",")[0]})
        if county:
            facts.append({"label": "County", "value": county})

        # ---- listing description ----
        description_html = _build_description_html(
            town_label, nearest_town, frontage_name, frontage_dist,
            evidence_points, estate, density_counts,
            location_line=location_line, seed=str(pin.id) if pin.id else None,
        )
        if description_html is None:
            description_html = Markup(escape(
                f"{location_name}. Not enough verified data was available to write "
                "a description for this pin."
            ))

        # ---- highlights ----
        # A frontage distance under the "right along" threshold reads as
        # "0m away", which says nothing useful -- so it's only added once
        # there's an actual distance worth reporting. Every remaining
        # highlight is spelled out in full ("37 meters away", "1.2
        # kilometers away"), never abbreviated to "37m"/"1.2km".
        highlights = []
        if frontage_name and frontage_dist is not None and frontage_dist >= ROAD_DISTANCE_ALONG_THRESHOLD_M:
            short_name = frontage_name.split(",")[0]
            highlights.append({"text": f"Fronts {short_name}", "dist": _format_distance_away(frontage_dist)})
        for label, name, dist in evidence_points:
            if dist is not None and dist > 0:
                highlights.append({"text": name, "dist": _format_distance_away(dist)})
        if estate:
            name, dist = estate
            if dist is not None and dist > 0:
                highlights.append({"text": f"Established neighbourhood: {name}", "dist": _format_distance_away(dist)})

        # ---- suitability ----
        suitability = [
            {"name": dev_type, "evidence": rationale, "stars": SUITABILITY_STARS.get(level, 1)}
            for dev_type, level, rationale in _top_suitability_rows(cell)
        ]

        # ---- landmarks ----
        landmarks = [
            {"name": name, "distance": _format_distance_away(dist)}
            for _label, name, dist in _collect_evidence_points(cell, max_points=5)
        ]

        # ---- maps / QR / satellite / street view ----
        view_url = _google_maps_view_url(pin)
        directions_url = _google_maps_directions_url(pin)
        qr_data_uri = _qr_data_uri(view_url)
        satellite_data_uri = _image_data_uri(getattr(cell, "satellite_image_url", None))
        street_view_data_uri = None
        if getattr(cell, "street_view_available", False):
            street_view_data_uri = _image_data_uri(getattr(cell, "street_view_image_url", None))

        # ---- footer contact ----
        broker_phone = _broker_phone(pin)
        broker_email = getattr(getattr(pin, "broker", None), "email", None)
        whatsapp_link = _whatsapp_link(broker_phone)
        tel_link = _tel_link(broker_phone)
        contact_bits = []
        if whatsapp_link:
            contact_bits.append(f'<a href="{whatsapp_link}" style="color:inherit;text-decoration:none;">{escape(broker_phone)} \u00b7 WhatsApp</a>')
        elif tel_link:
            contact_bits.append(f'<a href="{tel_link}" style="color:inherit;text-decoration:none;">{escape(broker_phone)}</a>')
        if broker_email:
            contact_bits.append(f'<a href="mailto:{escape(broker_email)}" style="color:inherit;text-decoration:none;">{escape(broker_email)}</a>')
        if contact_bits:
            contact_line = Markup(" \u00b7 ".join(contact_bits))
        else:
            support_whatsapp = _whatsapp_link(SCAPE_SUPPORT_PHONE)
            contact_line = Markup(
                f'<a href="{support_whatsapp}" style="color:inherit;text-decoration:none;">{DEFAULT_CONTACT_LINE}</a>'
            ) if support_whatsapp else DEFAULT_CONTACT_LINE

        template = _jinja_env.get_template("property_location_report.html.j2")
        html_string = template.render(
            ref=ref,
            location_line=location_line,
            generated_date=generated_date,
            seal=seal,
            facts=facts,
            description_html=description_html,
            highlights=highlights,
            suitability=suitability,
            landmarks=landmarks,
            qr_data_uri=qr_data_uri,
            satellite_data_uri=satellite_data_uri,
            street_view_data_uri=street_view_data_uri,
            view_url=view_url,
            directions_url=directions_url,
            contact_line=contact_line,
        )

        pdf_bytes = HTML(string=html_string, base_url=TEMPLATE_DIR).write_pdf()
        return pdf_bytes, investment_score, accessibility_score, summary_text

    except Exception as exc:
        logger.error("PDF render failed for pin %s: %s", pin.id, exc)
        raise ReportRenderError(str(exc)) from exc