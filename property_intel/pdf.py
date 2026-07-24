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
    "Low-Medium": 2,
    "Low": 1,
}


def _top_suitability_rows(cell, max_rows=4):
    """Best-suited-for section shows only the rows that read as a selling
    point -- ranked by fit, capped at max_rows, and 'Low' rows are dropped
    entirely unless nothing else qualifies. Includes the Residential Home
    row, which the underlying scoring table doesn't produce on its own --
    see _residential_home_row()."""
    all_rows = _development_suitability_table(cell) + [_residential_home_row(cell), _agriculture_land_banking_row(cell)]
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


RURAL_TOWN_DISTANCE_KM_THRESHOLD = 8  # beyond this, "peripheral" reads as honest, not a stretch
LOW_DENSITY_CATEGORY_COUNT = 2  # at most this many amenity categories present at all

# Broad, commonly-cited ranges for rain-fed agriculture (not specific to any
# one crop). Deliberately conservative and wide -- this is informational
# context from a global 250m soil model and a 30-year climate normal, not a
# site-specific agronomic assessment, so it's only ever used to add a real
# citation, never to declare a plot "fertile."
AGRICULTURE_PH_RANGE = (5.5, 7.5)
AGRICULTURE_MIN_RAINFALL_MM = 600


def _agriculture_land_banking_row(cell):
    """'Agriculture / Land Banking' suitability row.

    Two tiers of evidence, both real:

    1. Soil (ISRIC SoilGrids, cell.soil_ph) and climate (NASA POWER,
       cell.avg_annual_rainfall_mm) -- fetched by google_client.py's
       fetch_soil_data()/fetch_climate_data(). When both fall inside a
       broad, commonly-cited workable range for rain-fed agriculture, this
       function cites the actual numbers directly, always with an explicit
       on-site-testing caveat. When the numbers exist but fall OUTSIDE that
       range, this function does NOT print a negative soil/rainfall claim --
       consistent with how _air_quality_sentence() only ever surfaces good
       air-quality data -- it just falls through to tier 2 instead.

    2. Rurality proxy (distance from nearest resolved town, amenity
       density, road paving) -- used whenever soil/climate data isn't
       available or isn't itself favorable. This NEVER claims soil quality;
       it only describes how peripheral/undeveloped the area reads as.

    If you want a real, crop-specific fertility verdict, that's a
    genuinely harder problem than an API call (drainage, existing land use,
    and local knowledge all matter) and belongs with a licensed agronomist,
    not this report.
    """
    amenity_fields = _discover_amenity_fields(cell)
    categories_present = len(amenity_fields)

    nearest_town = _nearest_town_summary(cell)
    km = nearest_town[2] if nearest_town else None
    on_paved = getattr(cell, "on_paved_road", None)
    town_label = nearest_town[0] if nearest_town else "the nearest town"

    is_peripheral = km is not None and km >= RURAL_TOWN_DISTANCE_KM_THRESHOLD
    is_low_density = categories_present <= LOW_DENSITY_CATEGORY_COUNT

    soil_ph = getattr(cell, "soil_ph", None)
    rainfall_mm = getattr(cell, "avg_annual_rainfall_mm", None)
    ph_favorable = soil_ph is not None and AGRICULTURE_PH_RANGE[0] <= soil_ph <= AGRICULTURE_PH_RANGE[1]
    rainfall_favorable = rainfall_mm is not None and rainfall_mm >= AGRICULTURE_MIN_RAINFALL_MM

    if ph_favorable and rainfall_favorable:
        rationale = (
            f"Topsoil pH of {soil_ph} and average annual rainfall of {int(rainfall_mm)}mm "
            "(30-year normal) both fall within ranges commonly considered workable for "
            "rain-fed agriculture, though on-site soil testing is still recommended before "
            "a farming-based purchase decision"
        )
        level = "High" if (is_peripheral or is_low_density) else "Medium-High"
        return ("Agriculture / Land Banking", level, rationale)

    if ph_favorable or rainfall_favorable:
        if ph_favorable:
            fact = f"Topsoil pH of {soil_ph} falls within a workable range for most crops"
        else:
            fact = f"Average annual rainfall of {int(rainfall_mm)}mm (30-year normal) supports rain-fed agriculture"
        rationale = f"{fact}, though on-site soil testing is still recommended before a farming-based purchase decision"
        level = "Medium-High" if (is_peripheral or is_low_density) else "Medium"
        return ("Agriculture / Land Banking", level, rationale)

    if is_peripheral and is_low_density:
        rationale = (
            f"Sits {km} kilometers from {town_label} with few competing developments nearby, "
            "the kind of quiet, undeveloped setting often used for agriculture or long-term land banking "
            "(soil quality itself is not verified by this report)"
        )
        return ("Agriculture / Land Banking", "Medium", rationale)

    if is_peripheral:
        rationale = (
            f"Sits {km} kilometers from {town_label}, peripheral enough to suit land banking, "
            "though the surrounding area already has some development "
            "(soil quality itself is not verified by this report)"
        )
        return ("Agriculture / Land Banking", "Low-Medium", rationale)

    if on_paved is False:
        rationale = (
            "Not on a paved road, consistent with an undeveloped or agricultural plot "
            "(soil quality itself is not verified by this report)"
        )
        return ("Agriculture / Land Banking", "Low-Medium", rationale)

    return (
        "Agriculture / Land Banking",
        "Low",
        "Proximity to town and existing development make this a better fit for residential or "
        "commercial use than agriculture",
    )


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
    "Pharmacies",
    "Universities",
    "Banks",
    "Supermarkets",
    "Gated Communities",
    "Petrol Stations",
    "Police Stations",
    "Fire Stations",
    "EV Charging",
    "Transit Stops",
    "Restaurants",  # notable ones only
    "Shopping",  # malls/marketplaces only -- nearby_shopping never fetches generic small shops
    "Parks",
)
# NOTE ON PREVIOUSLY-DROPPED DATA: nearby_pharmacies and nearby_transit_stops
# are fetched and stored on every LocationCell (see models.py) and were
# already being picked up by _discover_amenity_fields(), but this priority
# tuple never listed them, so _collect_evidence_points() silently discarded
# both categories on every single report. They're real, named, distance-
# bearing data points like any other amenity, so they're restored here
# rather than left excluded.

# Human-friendly noun for each category, used only when weaving the
# "putting X, Y and Z within a short walk" clause of the description.
_CATEGORY_BENEFIT_NOUN = {
    "Schools": "schooling",
    "Universities": "education",
    "Hospitals": "healthcare",
    "Pharmacies": "everyday healthcare",
    "Banks": "banking",
    "Supermarkets": "shopping",
    "Gated Communities": "established housing",
    "Petrol Stations": "fuel access",
    "Police Stations": "security",
    "Fire Stations": "safety",
    "EV Charging": "electric vehicle charging",
    "Transit Stops": "public transport",
    "Restaurants": "dining",
    "Shopping": "retail",
    "Parks": "green space",
}


def _collect_evidence_points(cell, max_points=6):
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
# NEW: Category-specific marketing sentence templates (12-15 words each)
# ===========================================================================

# Each category has a list of templates using {name} and {dist} placeholders.
# {dist} is the full distance phrase like "1.2 kilometers away" or "right in the area".
# Templates are designed to sell the benefit of that amenity.

CATEGORY_SENTENCE_TEMPLATES = {
    "Schools": [
        "{name} is {dist}, offering quality schooling for families.",
        "With {name} {dist}, children enjoy a short trip to school.",
        "The nearby {name} ({dist}) makes school runs quick and easy.",
        "Parents will love {name} just {dist}, a great school close by.",
        "Schooling is convenient with {name} only {dist} from home.",
        "{name} {dist} means less time commuting and more family time.",
        "A top school, {name}, is {dist}, ideal for growing families.",
        "Families already send their children to {name}, only {dist}.",
        "{name} is {dist}, one less logistics headache for parents.",
        "The area's school runs are simple, with {name} just {dist}.",
        "{name}, {dist}, gives young families a reason to put down roots here.",
        "A confirmed school, {name}, sits {dist}, backing up the area's family appeal.",
        "For anyone raising a family, {name} {dist} is a genuine, everyday convenience.",
    ],
    "Universities": [
        "{name} {dist} provides higher education within easy reach.",
        "University students benefit from {name} just {dist}.",
        "{name} is {dist}, opening up tertiary education opportunities.",
        "With {name} {dist}, campus life is always close at hand.",
        "The university {name} is {dist}, perfect for students and staff.",
        "Higher learning at {name} is accessible, only {dist} from here.",
        "{name} {dist} makes attending lectures and events effortless.",
        "Staff and students at {name} already commute from this area, {dist}.",
        "{name}, {dist}, is exactly the kind of proximity student tenants look for.",
        "A confirmed university, {name}, is {dist}, a strong signal for rental demand.",
        "Lecturers and students alike value being {dist} from {name}.",
        "With {name} just {dist}, campus commutes stop being a daily hassle.",
    ],
    "Hospitals": [
        "Healthcare is close with {name} only {dist} from the property.",
        "{name} {dist} ensures medical care is always within quick reach.",
        "Residents appreciate {name} just {dist} for peace of mind.",
        "The hospital {name} is {dist}, offering reliable healthcare access.",
        "For medical emergencies, {name} is {dist}, reassuringly close.",
        "{name} {dist} provides quality healthcare without the long drive.",
        "With {name} {dist}, you're never far from medical services.",
        "Neighbours already rely on {name}, {dist}, for their care.",
        "A verified hospital, {name}, sits {dist}, real reassurance for any household.",
        "{name}, {dist}, means a health scare never has to mean a long drive.",
        "Having {name} {dist} is the kind of safety net most buyers look for.",
    ],
    "Pharmacies": [
        "{name} is {dist}, keeping everyday medicine and check-ups close by.",
        "With {name} {dist}, a prescription is never far away.",
        "The nearby pharmacy {name} ({dist}) covers day-to-day health needs.",
        "{name}, only {dist}, makes routine medication runs simple.",
        "Residents can reach {name} in {dist}, no need to plan ahead.",
        "A confirmed pharmacy, {name}, is {dist}, useful for the whole household.",
        "{name} {dist} rounds out the area's everyday healthcare access.",
    ],
    "Banks": [
        "Banking is easy with {name} only {dist} for your financial needs.",
        "{name} {dist} puts everyday banking right on your doorstep.",
        "Financial services at {name} are just {dist}, very convenient.",
        "Manage your money with ease, {name} is {dist} from home.",
        "{name} {dist} means shorter queues and simpler banking.",
        "The nearest bank, {name}, is {dist}, saving you valuable time.",
        "With {name} {dist}, you can handle banking errands in minutes.",
        "{name}, {dist}, is the kind of established institution serious buyers check for.",
        "A confirmed branch of {name} sits {dist}, real financial infrastructure nearby.",
        "Households here already bank at {name}, only {dist}.",
    ],
    "Supermarkets": [
        "Grocery shopping is a breeze with {name} only {dist}.",
        "{name} {dist} ensures you can pick up fresh food quickly.",
        "The supermarket {name} is {dist}, making daily shopping effortless.",
        "For everyday needs, {name} is {dist}, incredibly handy.",
        "With {name} {dist}, you'll never run out of essentials.",
        "Shopping at {name} is convenient, it's just {dist} from here.",
        "Stock up with ease, {name} is only {dist} for all your groceries.",
        "Neighbours already do their weekly shop at {name}, {dist}.",
        "{name}, {dist}, is a small but real daily-life advantage worth factoring in.",
        "A confirmed supermarket, {name}, at {dist}, keeps errands short and simple.",
    ],
    "Gated Communities": [
        "The established estate {name} is {dist}, offering secure living.",
        "{name} {dist} provides a sought-after neighbourhood setting.",
        "With {name} just {dist}, you benefit from a prestigious address.",
        "The gated community {name} is {dist}, ensuring safety and comfort.",
        "Residents of {name} enjoy a secure environment, only {dist}.",
        "{name} {dist} adds to the area's desirability for homebuyers.",
        "A peaceful estate, {name}, is {dist}, perfect for families.",
        "The presence of {name}, {dist}, shows this area already attracts serious homeowners.",
        "Buyers looking for company will find {name}, {dist}, already home to an established community.",
        "{name}, {dist}, is proof this location has already passed other buyers' due diligence.",
    ],
    "Petrol Stations": [
        "Fuel up quickly with {name} only {dist} for your vehicle.",
        "{name} {dist} makes refuelling convenient and stress-free.",
        "The petrol station {name} is {dist}, saving you time on the road.",
        "With {name} {dist}, you'll never worry about running out of fuel.",
        "Petrol is easily accessible, {name} is just {dist}.",
        "Fill up at {name} {dist}, ideal for busy commuters.",
        "{name} {dist} keeps your journeys moving without detours.",
        "A confirmed fuel stop, {name}, is {dist}, useful for daily commuters.",
    ],
    "Police Stations": [
        "Security is enhanced with {name} only {dist} from the property.",
        "The police station {name} is {dist}, providing added peace of mind.",
        "With {name} {dist}, law enforcement is always close at hand.",
        "Residents feel safer knowing {name} is just {dist}.",
        "A police station, {name}, is {dist}, ensuring quick response times.",
        "Neighbourhood safety is a priority, {name} is only {dist} from here.",
        "{name} {dist} adds an extra layer of security for your family.",
        "A verified police post, {name}, sits {dist}, real reassurance for any household.",
    ],
    "Fire Stations": [
        "Fire safety is covered with {name} only {dist} from the site.",
        "The fire station {name} is {dist}, ensuring rapid emergency response.",
        "With {name} {dist}, you're protected against fire hazards.",
        "Emergency services are near, {name} is just {dist}.",
        "A fire station, {name}, is {dist}, crucial for safety.",
        "Residents benefit from {name} {dist}, reducing fire risk.",
        "{name} {dist} means professional help is minutes away.",
    ],
    "EV Charging": [
        "Electric vehicle owners will find {name} only {dist} for charging.",
        "{name} {dist} makes owning an EV practical and convenient.",
        "With {name} {dist}, you can charge your car without hassle.",
        "The EV charging station {name} is {dist}, future-ready infrastructure.",
        "Sustainable driving is easy, {name} is just {dist}.",
        "Charge up at {name} {dist}, ideal for eco-conscious buyers.",
        "{name} is only {dist}, useful reassurance for EV owners.",
    ],
    "Transit Stops": [
        "{name} is {dist}, keeping the property connected without a car.",
        "With {name} just {dist}, commuting without a car is realistic here.",
        "The transit stop {name} ({dist}) links the property to the wider city.",
        "{name}, only {dist}, is useful for staff, tenants, or visitors without a vehicle.",
        "Public transport is close, {name} sits {dist} from the property.",
        "A confirmed stop, {name}, at {dist}, adds genuine transport flexibility.",
    ],
    "Restaurants": [
        "Dining out is easy with {name} only {dist} from home.",
        "{name} {dist} offers a well-known option just a short walk away.",
        "Enjoy a meal at {name}, it's {dist}, a nice option nearby.",
        "The venue {name} is {dist}, adding to the local flavour.",
        "With {name} {dist}, you can treat yourself without the drive.",
        "A known name in the area, {name}, is {dist}.",
        "{name} {dist} means a familiar meal spot is always close at hand.",
    ],
    # Fallback for any unknown category
    "default": [
        "{name} is {dist}, adding convenience to your daily life.",
        "With {name} {dist}, you have essential services within reach.",
        "{name} is only {dist}, a practical benefit for residents.",
        "The nearby {name} ({dist}) makes everything more accessible.",
    ]
}


def _get_category_sentence(category, name, distance_m, rng):
    """Return a marketing sentence for a given category, using its templates."""
    templates = CATEGORY_SENTENCE_TEMPLATES.get(category)
    if not templates:
        templates = CATEGORY_SENTENCE_TEMPLATES["default"]
    dist_phrase = _format_distance_away(distance_m)
    template = rng.choice(templates)
    # Ensure sentence is 12-15 words; we trust our templates.
    sentence = template.format(name=escape(name), dist=dist_phrase)
    # Ensure it ends with a period.
    if not sentence.endswith("."):
        sentence += "."
    return sentence


# ===========================================================================
# OPENING SENTENCE POOLS -- distance, frontage, and closing are always
# built as separate, self-contained sentences (never glued together with
# "and"), which is what previously caused dangling-modifier sentences
# like "...which works out to a 7-minute drive from Ruiru Town and
# enjoying frontage on C65, this property offers...".
# ===========================================================================

def _generate_openers():
    intro_verbs = [
        "Located", "Situated", "Positioned", "Set", "Found", "Sited", "Nestled",
    ]

    town_with_drive = []
    for verb in intro_verbs:
        for phrasing in [
            "{verb} just {{dist_m}} metres from {{town}}, roughly {{drive_phrase}}",
            "{verb} {{dist_m}} metres from {{town}}, about {{drive_phrase}}",
            "{verb} only {{dist_m}} metres from {{town}}, which works out to {{drive_phrase}}",
            "{verb} a mere {{dist_m}} metres from {{town}}, around {{drive_phrase}}",
            "{verb} {{dist_away}} from {{town}}, roughly {{drive_phrase}}",
            "{verb} {{dist_away}} from {{town}}, which is about {{drive_phrase}}",
        ]:
            town_with_drive.append(phrasing.format(verb=verb) + ".")

    town_without_drive = []
    for verb in intro_verbs:
        for phrasing in [
            "{verb} {{dist_m}} metres from {{town}}",
            "{verb} just {{dist_m}} metres from {{town}}",
            "{verb} {{dist_away}} from {{town}}",
            "{verb} {{dist_away}} from the heart of {{town}}",
        ]:
            town_without_drive.append(phrasing.format(verb=verb) + ".")

    fallback = [
        "{location_line} offers strong development potential for residential or commercial use.",
        "{location_line} presents an accessible opportunity for residential or commercial development.",
        "{location_line} is a prime candidate for residential or commercial development.",
        "{location_line} enjoys a strategic location with strong development upside.",
        "{location_line} is conveniently located for residential or commercial purposes.",
    ]

    return {
        "town_with_drive": town_with_drive,
        "town_without_drive": town_without_drive,
        "fallback": fallback,
    }


_OPENER_POOLS = _generate_openers()

# Always its own sentence -- never appended to the distance clause with "and".
_FRONTAGE_SENTENCES = [
    "The property fronts {frontage}.",
    "It enjoys direct frontage on {frontage}.",
    "The plot has frontage onto {frontage}.",
    "It sits with frontage on {frontage}.",
    "Direct frontage on {frontage} is confirmed for this plot.",
    "This plot fronts directly onto {frontage}.",
]

_OPENER_END_SENTENCES = [
    "This property offers excellent accessibility for residential or commercial development.",
    "This property offers outstanding accessibility for residential or commercial development.",
    "This property presents an accessible opportunity for residential or commercial development.",
    "This property combines convenience with strong development potential.",
    "This property enjoys a highly accessible location suitable for residential or commercial development.",
    "This property is strategically positioned for residential or commercial development.",
    "This property offers a practical, well-connected setting for residential or commercial development.",
    "This property delivers excellent accessibility for residential or commercial development.",
    "This property provides exceptional connectivity for residential or commercial use.",
    "This property is ideally placed for residential or commercial development.",
    "This property affords strong accessibility for a range of residential or commercial uses.",
    "This property gives buyers a genuinely well-connected base for residential or commercial plans.",
    "This property's location already does much of the work for a future residential or commercial build.",
]

# Services templates -- many variations
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

# NEW: Short service sentences (12-15 words) -- used alongside the longer ones.
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

# Short estate sentences (kept)
_SHORT_CLOSING_ESTATE = [
    "The area is anchored by {estate}.",
    "The neighbourhood is built around {estate}.",
    "The location benefits from {estate}.",
    "An established estate, {estate}, is nearby.",
]

def _service_connector_phrase(dist_m, rng):
    """Picks a colloquial distance qualifier consistent with the actual
    number -- the old flat pool let "practically on the doorstep" land on
    entries 400-900m away, which reads as contradictory."""
    if dist_m is None:
        return ""
    if dist_m < 50:
        return rng.choice(["right in the area", "immediately adjacent", "right next to the property"])
    d = _format_distance_away(dist_m)
    if dist_m < 300:
        templates = ["just {d}", "only {d}", "{d}, right by the property", "{d}, a short walk from the site"]
    elif dist_m < 800:
        templates = ["{d}", "{d}, a few minutes' walk from the site", "{d}, close to the site", "{d}, easily accessible"]
    elif dist_m < 1500:
        templates = ["{d}", "{d}, a short drive from the site", "{d}, within easy reach"]
    else:
        templates = ["{d}", "{d} from the site", "{d}, a short drive from the property"]
    return rng.choice(templates).format(d=d)


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
        connector = _service_connector_phrase(dist_m, rng)
        pieces.append(f"{escape(name)} {connector}".rstrip())
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


def _get_nearest_named(evidence_points, label):
    """Return (name, distance_m) for the nearest entry with given label, or None."""
    for lbl, name, dist in evidence_points:
        if lbl == label:
            return name, dist
    return None


def _named_density_points_for_report(cell, evidence_points, estate, max_per_category=2):
    """Nearest 2 named Banks and 2 named Supermarkets within 5km that
    have NOT already been cited elsewhere in the description (the
    evidence-point sentences or the estate line) -- so the closing
    financial/retail sentence always introduces new names rather than
    repeating ones already mentioned. Returns {label: [(name, dist_m), ...]}.
    """
    exclude = {name for _label, name, _dist in evidence_points}
    if estate:
        exclude.add(estate[0])
    amenity_fields = dict(_discover_amenity_fields(cell))
    result = {}
    for label in ("Banks", "Supermarkets"):
        entries = amenity_fields.get(label) or []
        candidates = [
            (e.get("name"), e.get("distance_m"))
            for e in entries
            if e.get("name")
            and e.get("distance_m") is not None
            and e["distance_m"] <= 5000
            and e.get("name") not in exclude
        ]
        candidates.sort(key=lambda p: p[1])
        if candidates:
            result[label] = candidates[:max_per_category]
    return result


_DENSITY_CATEGORY_NOUN = {
    "Banks": "financial institutions",
    "Supermarkets": "supermarkets and malls",
}


def _format_named_density_sentence(named_density):
    """Turns {'Banks': [(name,dist),...], 'Supermarkets': [...]} into a
    sentence naming the actual nearby banks and supermarkets/malls with
    distances. Returns None if there's nothing new to report."""
    if not named_density:
        return None
    chunks = []
    for label in ("Banks", "Supermarkets"):
        entries = named_density.get(label)
        if not entries:
            continue
        noun = _DENSITY_CATEGORY_NOUN[label]
        names = " and ".join(
            f"{escape(name)} ({_format_distance_away(dist)})" for name, dist in entries
        )
        chunks.append(f"{noun} like {names}")
    if not chunks:
        return None
    return "The area also offers " + ", plus ".join(chunks) + ", a strong service base for buyers."


# ---------------------------------------------------------------------------
# Previously-fetched-but-unused data, restored as their own honest sentences.
#
# PRICE_BENCHMARKS is real, sourced market data (see the dict definition
# near the top of this file, with its quarter and note fields) that was
# only ever used to pick a fallback location label or to compute the
# internal (unprinted) investment score. It never appeared as a sentence
# in the actual report, even though it's exactly the kind of concrete,
# named, dated evidence this report is built around. Restoring it also
# doubles as an honest Cialdini "authority" cue (named market data, not an
# AI opinion) and, where yoy growth is genuinely positive, a truthful
# "scarcity of opportunity" cue -- real appreciation trends, not an
# invented countdown.
# ---------------------------------------------------------------------------

_PRICE_BENCHMARK_TEMPLATES_POSITIVE = [
    "Land in {town} appreciated {yoy}% year-on-year as of {quarter}, {note}.",
    "Published market data puts {town} land appreciation at {yoy}% year-on-year ({quarter}), {note}.",
    "{town} land values rose {yoy}% year-on-year in {quarter}, {note}.",
    "According to {quarter} market data, {town} has seen {yoy}% year-on-year appreciation, {note}.",
]

_PRICE_BENCHMARK_TEMPLATES_FLAT = [
    "{town} land values were broadly stable in {quarter}, {note}.",
    "Market data shows {town} land prices held steady through {quarter}, {note}.",
]


def _price_benchmark_sentence(cell, rng):
    """Sentence built from PRICE_BENCHMARKS, only when this pin actually
    matches one of the named towns in that dict (via _match_price_benchmark).
    Never invents a percentage or quarter; both come straight from the
    benchmark entry."""
    town, benchmark = _match_price_benchmark(cell)
    if not benchmark or benchmark.get("yoy_change_pct") is None or not benchmark.get("note"):
        return None
    yoy = benchmark["yoy_change_pct"]
    quarter = benchmark.get("quarter", "the most recent quarter")
    town_label = town.title()
    if yoy > 0:
        template = rng.choice(_PRICE_BENCHMARK_TEMPLATES_POSITIVE)
    else:
        template = rng.choice(_PRICE_BENCHMARK_TEMPLATES_FLAT)
    return template.format(town=escape(town_label), yoy=yoy, quarter=escape(quarter), note=benchmark["note"])


# Air Quality history/category is fetched (air_quality_category,
# air_quality_good_days_streak) but never printed anywhere in the report --
# only air_quality_index feeds the unprinted internal score. Restored here
# as its own short, honest sentence, only when the data is genuinely good
# news; a bad-air-quality pin simply gets no sentence rather than a
# negative one, consistent with this report's "sell only what's true and
# positive, skip what isn't" design.
_AIR_QUALITY_TEMPLATES = [
    "Air quality here is rated {category}, with {streak} consecutive good-air days recorded.",
    "The area has logged {streak} straight good-air-quality days, rated {category} overall.",
    "Air quality monitoring rates this location {category}, with a {streak}-day good-air streak on record.",
]

_AIR_QUALITY_TEMPLATES_NO_STREAK = [
    "Air quality here is rated {category}.",
    "The area's air quality is monitored and currently rated {category}.",
]


def _air_quality_sentence(cell, rng):
    aqi = getattr(cell, "air_quality_index", None)
    category = (getattr(cell, "air_quality_category", "") or "").strip()
    streak = getattr(cell, "air_quality_good_days_streak", None)
    if aqi is None or aqi > AQI_GOOD_THRESHOLD or not category:
        return None
    if streak:
        template = rng.choice(_AIR_QUALITY_TEMPLATES)
        return template.format(category=escape(category), streak=streak)
    template = rng.choice(_AIR_QUALITY_TEMPLATES_NO_STREAK)
    return template.format(category=escape(category))


# elevation_slope_range_m (max-min elevation across a 5-point sample grid)
# is fetched but never printed. A small range is a genuine, checkable
# signal that the site is flat -- useful for construction cost and,
# honestly, for farming too, without ever claiming anything about the soil
# itself.
FLAT_SITE_SLOPE_RANGE_M = 3.0

_FLAT_SITE_TEMPLATES = [
    "Elevation across the site varies by only {slope} meters, indicating flat, easy-to-build terrain.",
    "An elevation survey of the site shows just {slope} meters of variation, consistent with flat, level ground.",
    "The site is notably flat, with elevation varying by only {slope} meters across the plot.",
]


def _flat_site_sentence(cell, rng):
    slope = getattr(cell, "elevation_slope_range_m", None)
    if slope is None or slope > FLAT_SITE_SLOPE_RANGE_M:
        return None
    template = rng.choice(_FLAT_SITE_TEMPLATES)
    return template.format(slope=round(slope, 1))


# Soil (ISRIC SoilGrids) / climate (NASA POWER) data restored as its own
# sentence, gated on the same AGRICULTURE_PH_RANGE / AGRICULTURE_MIN_RAINFALL_MM
# thresholds as _agriculture_land_banking_row(), so the description and the
# suitability table never contradict each other. Only fires when at least
# one of the two figures is genuinely favorable; an unfavorable pH/rainfall
# reading is simply omitted rather than reported negatively.
_SOIL_CLIMATE_TEMPLATES_BOTH = [
    "Soil data shows a topsoil pH of {ph}, and climate records show {rainfall}mm of average annual rainfall, both workable for rain-fed agriculture (on-site testing is still advised before any farming investment).",
    "This location has a topsoil pH of {ph} and an average annual rainfall of {rainfall}mm, a combination generally considered workable for rain-fed farming.",
]

_SOIL_CLIMATE_TEMPLATES_PH_ONLY = [
    "Soil data for this location shows a topsoil pH of {ph}, within a workable range for most crops (on-site testing is still advised before any farming investment).",
]

_SOIL_CLIMATE_TEMPLATES_RAIN_ONLY = [
    "Climate records show {rainfall}mm of average annual rainfall here, a 30-year normal supportive of rain-fed agriculture.",
]


def _soil_climate_sentence(cell, rng):
    soil_ph = getattr(cell, "soil_ph", None)
    rainfall_mm = getattr(cell, "avg_annual_rainfall_mm", None)
    ph_favorable = soil_ph is not None and AGRICULTURE_PH_RANGE[0] <= soil_ph <= AGRICULTURE_PH_RANGE[1]
    rainfall_favorable = rainfall_mm is not None and rainfall_mm >= AGRICULTURE_MIN_RAINFALL_MM

    if ph_favorable and rainfall_favorable:
        template = rng.choice(_SOIL_CLIMATE_TEMPLATES_BOTH)
        return template.format(ph=soil_ph, rainfall=int(rainfall_mm))
    if ph_favorable:
        return rng.choice(_SOIL_CLIMATE_TEMPLATES_PH_ONLY).format(ph=soil_ph)
    if rainfall_favorable:
        return rng.choice(_SOIL_CLIMATE_TEMPLATES_RAIN_ONLY).format(rainfall=int(rainfall_mm))
    return None


# ---------------------------------------------------------------------------
# "Combined intelligence": synthesis sentences that don't cite a single
# amenity but instead name two or more real, already-cited evidence points
# together and state what buyer profile they jointly support. Each check
# below requires at least two genuine, distinct data points to be present
# before it fires -- this is the "given this and this, this area is
# suitable for X" behaviour, built strictly from real evidence rather than
# invented reasoning.
# ---------------------------------------------------------------------------

_FAMILY_FIT_TEMPLATES = [
    "With both {school} and {hospital} nearby, this location covers two of the biggest priorities for families.",
    "Having {school} and {hospital} both close by makes this a practical, low-stress base for raising a family.",
    "{school} and {hospital}, both within reach, are exactly the combination family buyers look for.",
]

_STUDENT_RENTAL_TEMPLATES = [
    "With {university} nearby and everyday services like {support} already established, this reads as a strong student-rental location.",
    "{university} plus existing {support} nearby gives this plot genuine student-housing rental potential.",
    "The combination of {university} and nearby {support} is exactly what drives consistent student rental demand.",
]

_INVESTMENT_GRADE_TEMPLATES = [
    "With an established estate, {estate}, and real banking infrastructure like {bank} both nearby, this area already reads as investment-grade.",
    "{estate} and {bank}, both confirmed nearby, are the kind of established-neighbourhood signals serious investors look for.",
    "The presence of both {estate} and {bank} nearby suggests a maturing, bankable neighbourhood rather than speculative land.",
]

_COMMERCIAL_FIT_TEMPLATES = [
    "With {retail} and {fuel} both nearby, and direct road frontage, this plot suits commercial or mixed-use development well.",
    "{retail} and {fuel} nearby, combined with road frontage, point toward strong commercial or mixed-use potential.",
]


def _combined_intelligence_sentences(cell, evidence_points, estate, frontage_name, rng, max_sentences=2):
    """Builds 0-2 synthesis sentences from combinations of already-verified
    evidence points. Every named entry here (school/hospital/university/
    bank/estate/retail/fuel) is looked up fresh from the real data via
    _nearest_named on the full amenity lookup, not just the trimmed
    evidence_points list, so a combination can fire even if one of its two
    amenities didn't make the top-6 evidence cut. Distances are
    deliberately omitted here (they're already stated elsewhere in the
    description) to keep each sentence focused on the "what this combo
    means for you" framing rather than repeating numbers."""
    amenity_fields = _discover_amenity_fields(cell)
    amenity_lookup = dict(amenity_fields)
    already_named = {name for _label, name, _dist in evidence_points}
    if estate:
        already_named.add(estate[0])

    candidates = []

    school = _nearest_named(amenity_lookup, "Schools")
    hospital = _nearest_named(amenity_lookup, "Hospitals")
    if school and hospital:
        candidates.append(rng.choice(_FAMILY_FIT_TEMPLATES).format(
            school=escape(school[0]), hospital=escape(hospital[0]),
        ))

    university = _nearest_named(amenity_lookup, "Universities")
    support = _nearest_named(amenity_lookup, "Supermarkets", "Restaurants", "Transit Stops")
    if university and support and support[0] != (school[0] if school else None):
        candidates.append(rng.choice(_STUDENT_RENTAL_TEMPLATES).format(
            university=escape(university[0]), support=escape(support[0]),
        ))

    bank = _nearest_named(amenity_lookup, "Banks")
    if estate and bank and bank[0] != estate[0]:
        candidates.append(rng.choice(_INVESTMENT_GRADE_TEMPLATES).format(
            estate=escape(estate[0]), bank=escape(bank[0]),
        ))

    retail = _nearest_named(amenity_lookup, "Shopping", "Supermarkets")
    fuel = _nearest_named(amenity_lookup, "Petrol Stations")
    if retail and fuel and frontage_name and retail[0] != fuel[0]:
        candidates.append(rng.choice(_COMMERCIAL_FIT_TEMPLATES).format(
            retail=escape(retail[0]), fuel=escape(fuel[0]),
        ))

    rng.shuffle(candidates)
    return candidates[:max_sentences]


def _build_description_html(town_label, nearest_town, frontage_name, frontage_dist,
                             evidence_points, estate, named_density,
                             location_line=None, seed=None, cell=None):
    """
    Builds the Listing Description as separate, self-contained sentences:
    distance, frontage (own sentence -- never glued to the distance
    clause), a closing accessibility line, a general services summary
    (top 4 nearest evidence points), category-specific sentences ONLY for
    evidence points beyond those top 4 (so nothing is cited twice), an
    estate sentence, a service-density sentence, and -- when `cell` is
    passed -- up to two "combined intelligence" synthesis sentences plus
    any of the previously-unused price benchmark / air quality / flat-site
    sentences that have real data behind them.

    The estate's own name is excluded from the evidence points used here
    so it isn't named in the services summary AND in its own estate
    sentence.
    """
    rng = random.Random(seed) if seed is not None else random

    if estate:
        evidence_points = [p for p in evidence_points if p[1] != estate[0]]

    minutes = nearest_town[1] if nearest_town else None
    km = nearest_town[2] if nearest_town else None
    dist_m = int(round(km * 1000)) if km is not None else None
    drive_phrase = _format_drive_phrase(minutes)
    dist_away = _format_distance_away(dist_m) if dist_m is not None else "unknown distance"
    frontage_short = frontage_name.split(",")[0] if frontage_name else None

    # ---- 1. Distance sentence ----
    if town_label and dist_m is not None:
        pool = _OPENER_POOLS["town_with_drive"] if drive_phrase else _OPENER_POOLS["town_without_drive"]
        distance_sentence = rng.choice(pool).format(
            dist_m=dist_m,
            drive_phrase=drive_phrase or "",
            town=str(escape(town_label)),
            dist_away=dist_away,
        )
    else:
        distance_sentence = rng.choice(_OPENER_POOLS["fallback"]).format(
            location_line=str(escape(location_line)) if location_line else "This property",
        )
    sentences = [distance_sentence]

    # ---- 2. Frontage -- its own sentence, never glued to the distance clause ----
    if frontage_short:
        sentences.append(rng.choice(_FRONTAGE_SENTENCES).format(frontage=str(escape(frontage_short))))

    # ---- 3. Closing accessibility line ----
    sentences.append(rng.choice(_OPENER_END_SENTENCES))

    # ---- 4. General services summary (top 4 nearest evidence points) ----
    top4 = evidence_points[:4]
    svc_list, noun_phrase = _format_service_list(top4, rng)
    if svc_list:
        services_template = rng.choice(_SERVICES_TEMPLATES)
        svc_sentence = services_template.format(
            svc_list=svc_list,
            svc_list_cap=svc_list[0].upper() + svc_list[1:] if svc_list else "",
            nouns=noun_phrase,
        )
        if not svc_sentence.endswith("."):
            svc_sentence += "."
        sentences.append(svc_sentence)

    # ---- 5. Category-specific sentences -- ONLY for points beyond the
    # top 4 already named above, so nothing is cited twice ----
    for label, name, dist in evidence_points[4:6]:
        if label == "Gated Communities":
            continue
        sentences.append(_get_category_sentence(label, name, dist, rng))

    # ---- 6. Estate sentence ----
    if estate:
        name, dist = estate
        estate_text = f"{escape(name)} {_format_distance_away(dist)}"
        estate_sentence = rng.choice(_SHORT_CLOSING_ESTATE).format(estate=estate_text)
        if not estate_sentence.endswith("."):
            estate_sentence += "."
        sentences.append(estate_sentence)

    # ---- 7. Named financial/retail density sentence ----
    density_sentence = _format_named_density_sentence(named_density)
    if density_sentence:
        sentences.append(density_sentence)

    if cell is not None:
        # ---- 8. Combined intelligence -- synthesis across 2+ real data points ----
        sentences.extend(_combined_intelligence_sentences(cell, evidence_points, estate, frontage_name, rng))

        # ---- 9. Previously-fetched-but-unused data, restored ----
        price_sentence = _price_benchmark_sentence(cell, rng)
        if price_sentence:
            sentences.append(price_sentence)

        flat_sentence = _flat_site_sentence(cell, rng)
        if flat_sentence:
            sentences.append(flat_sentence)

        soil_sentence = _soil_climate_sentence(cell, rng)
        if soil_sentence:
            sentences.append(soil_sentence)

        air_sentence = _air_quality_sentence(cell, rng)
        if air_sentence:
            sentences.append(air_sentence)

    if not sentences:
        fallback = escape(location_line) if location_line else "This property"
        return Markup(f"{fallback}. Not enough verified data was available to write a description for this pin.")

    # Every sentence already ends with "." -- join with a single space,
    # never ". ", which was producing ".." throughout the paragraph.
    sentences = [s if s.endswith(".") else s + "." for s in sentences]
    paragraph = " ".join(sentences)
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
        named_density = _named_density_points_for_report(cell, evidence_points, estate)

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
            evidence_points, estate, named_density,
            location_line=location_line, seed=str(pin.id) if pin.id else None,
            cell=cell,
        )
        if description_html is None:
            description_html = Markup(escape(
                f"{location_name}. Not enough verified data was available to write "
                "a description for this pin."
            ))

        # ---- highlights ----
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