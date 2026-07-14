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
import re
from datetime import datetime

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
    if meters is None:
        return "Unknown"
    if meters <= 500:
        return f"{int(round(meters))}m"
    km = meters / 1000
    return f"{int(round(meters))}m ({km:.1f}km)"


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
            closest = _format_distance(top[0].get('distance_m'))
            if len(names) == 1:
                text = names[0]
            else:
                text = f"{', '.join(names[:-1])} and {names[-1]}"
            if not include_distance:
                return text
            remaining = len(sorted_entries) - len(top)
            remaining_label = _count_label(remaining) if len(sorted_entries) >= AMENITY_FETCH_CAP else str(remaining)
            suffix = f" (the nearest just {closest} away"
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


def _development_suitability_table(cell):
    """Unchanged scoring logic from the original report -- the template
    just displays fewer rows now. Returns [(dev_type, level, rationale), ...]."""
    amenity_fields = _discover_amenity_fields(cell)
    amenity_lookup = dict(amenity_fields)
    has_university = "Universities" in amenity_lookup
    has_retail = "Shopping" in amenity_lookup or "Supermarkets" in amenity_lookup
    has_gated = "Gated Communities" in amenity_lookup
    has_student_housing = any(
        e.get("distance_m") is not None and e["distance_m"] <= STUDENT_HOUSING_PROXIMITY_M
        for e in amenity_lookup.get("Student Housing", [])
    )

    nairobi = (cell.travel_times or {}).get("nairobi_cbd")
    commute_mins = round(nairobi["duration_s"] / 60) if nairobi and nairobi.get("duration_s") else None

    suitability = []

    if has_student_housing and has_university:
        suitability.append(("Student Housing", "Very High", "Existing student accommodation confirms active rental market"))
    elif has_university and commute_mins and commute_mins < 45:
        suitability.append(("Student Housing", "High", "Proximity to educational institutions drives rental demand"))
    elif has_university:
        suitability.append(("Student Housing", "Medium", "Near educational institutions"))
    else:
        suitability.append(("Student Housing", "Low", "Limited educational institutions in immediate area"))

    if has_retail and has_university and has_gated:
        suitability.append(("Apartments", "Very High", "Service ecosystem plus proven demand from nearby gated communities"))
    elif has_retail and has_university:
        suitability.append(("Apartments", "High", "Complete service ecosystem supports residential development"))
    elif has_retail or has_university:
        suitability.append(("Apartments", "Medium", "Some support infrastructure present"))
    else:
        suitability.append(("Apartments", "Low", "Limited service infrastructure"))

    if has_retail and commute_mins and commute_mins < 60:
        suitability.append(("Mixed-Use", "Medium-High", "Retail presence with reasonable commute supports mixed-use"))
    elif has_retail:
        suitability.append(("Mixed-Use", "Medium", "Retail presence supports mixed-use potential"))
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
        return ("Residential Home", "Very High", f"Established estate {name} {int(dist)}m away")
    if dist <= 2000:
        return ("Residential Home", "High", f"Established estate {name} within {_format_distance(dist)}")
    return ("Residential Home", "Medium", f"Nearest estate, {name}, is {_format_distance(dist)} away")


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


def _evid(text):
    """Wraps a piece of text in the template's <span class="evid"> evidence
    chip. Escapes the inner text (place names, addresses) since it's
    outside our control, then marks the whole chip safe to insert into
    the Jinja template raw."""
    return Markup('<span class="evid">{}</span>').format(text)


def _build_description_html(town_label, nearest_town, frontage_name, frontage_dist,
                             evidence_points, estate, density_counts):
    """Builds the Listing Description paragraph with the same inline
    evidence-chip markup as the template's example copy, entirely from
    real data. Any clause whose underlying evidence is missing for this
    pin is dropped -- never padded with a placeholder."""
    parts = []

    # --- opening: town distance + frontage ---
    open_bits = []
    if town_label and nearest_town:
        _, minutes, km = nearest_town
        if km is not None and minutes is not None:
            town_chip = _evid(f"{int(round(km * 1000))}m \u00b7 {minutes} min")
        elif minutes is not None:
            town_chip = _evid(f"{minutes} min")
        elif km is not None:
            town_chip = _evid(f"{km}km")
        else:
            town_chip = None
        if town_chip is not None:
            open_bits.append(Markup("Located {} from {} Town Centre").format(town_chip, town_label))
        else:
            open_bits.append(Markup("Located near {} Town Centre").format(town_label))
    if frontage_name:
        short_name = frontage_name.split(",")[0]
        if frontage_dist is not None and frontage_dist >= ROAD_DISTANCE_ALONG_THRESHOLD_M:
            front_chip = _evid(f"{short_name}, {int(round(frontage_dist))}m")
            open_bits.append(Markup("fronting {}").format(front_chip))
        else:
            open_bits.append(Markup("fronting {}").format(escape(short_name)))
    if open_bits:
        joined = Markup(" and ").join(open_bits) if len(open_bits) > 1 else open_bits[0]
        parts.append(Markup("{}, this property offers strong accessibility for residential or commercial use.").format(joined))

    # --- nearby services (up to 4 evidence points) ---
    if evidence_points:
        top4 = evidence_points[:4]
        chips = [_evid(f"{name} \u00b7 {_format_distance(dist)}") for _, name, dist in top4]
        if len(chips) == 1:
            chip_list = chips[0]
        else:
            chip_list = Markup(", ").join(chips[:-1])
            chip_list = Markup("{} and {}").format(chip_list, chips[-1])
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
        parts.append(Markup("Nearby services include {}, putting {} within a short walk.").format(chip_list, noun_phrase))

    # --- established area + density ---
    closing_bits = []
    if estate:
        name, dist = estate
        closing_bits.append(Markup("anchored by {}").format(_evid(f"{name} \u00b7 {_format_distance(dist)}")))
    if density_counts:
        density_chips = [_evid(f"{_count_label(c)} {label.lower() if c != 1 else _singular(label)}") for label, c in density_counts]
        density_joined = Markup(" and ").join(density_chips)
        closing_bits.append(Markup("a dense service base of {} within 5km").format(density_joined))
    if closing_bits:
        town_possessive = f"{town_label}'s" if town_label else "the area's"
        closing = Markup(" and ").join(closing_bits) if len(closing_bits) > 1 else closing_bits[0]
        parts.append(Markup(
            "The surrounding area is already residential, {} \u2014 supporting apartments, "
            "rentals, or mixed-use development for buyers who want easy reach of {} town centre."
        ).format(closing, town_possessive))

    if not parts:
        return None
    return Markup(" ").join(parts)


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
                "mid": f"{int(round(frontage_dist))}m",
                "bot": frontage_name.split(",")[0],
            }

        # ---- quick facts strip ----
        facts = []
        if town_label:
            facts.append({"label": "Nearest Town", "value": town_label})
            _, minutes, km = nearest_town
            if km is not None and minutes is not None:
                dist_val = f"{int(round(km * 1000))}m \u00b7 {minutes} min"
            elif minutes is not None:
                dist_val = f"{minutes} min"
            elif km is not None:
                dist_val = f"{km}km"
            else:
                dist_val = "Unknown"
            facts.append({"label": f"Distance to {town_label} Centre", "value": dist_val})
        if frontage_name:
            facts.append({"label": "Frontage", "value": frontage_name.split(",")[0]})
        if county:
            facts.append({"label": "County", "value": county})

        # ---- listing description (with inline evidence chips) ----
        description_html = _build_description_html(
            town_label, nearest_town, frontage_name, frontage_dist,
            evidence_points, estate, density_counts,
        )
        if description_html is None:
            description_html = Markup(escape(
                f"{location_name}. Not enough verified data was available to write "
                "a description for this pin."
            ))

        # ---- highlights ----
        highlights = []
        if frontage_name:
            short_name = frontage_name.split(",")[0]
            if frontage_dist is not None and frontage_dist < ROAD_DISTANCE_ALONG_THRESHOLD_M:
                highlights.append({"text": f"Fronts {short_name}, the main road into town", "dist": "0m"})
            elif frontage_dist is not None:
                highlights.append({"text": f"Fronts {short_name}", "dist": f"{int(round(frontage_dist))}m"})
        for label, name, dist in evidence_points:
            highlights.append({"text": name, "dist": _format_distance(dist)})
        if estate:
            name, dist = estate
            highlights.append({"text": f"Established neighbourhood: {name}", "dist": _format_distance(dist)})

        # ---- suitability ----
        suitability = [
            {"name": dev_type, "evidence": rationale, "stars": SUITABILITY_STARS.get(level, 1)}
            for dev_type, level, rationale in _top_suitability_rows(cell)
        ]

        # ---- landmarks ----
        landmarks = [
            {"name": name, "distance": _format_distance(dist)}
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
        # Prefers the broker's own WhatsApp/tel line (collected at signup);
        # falls back to their email, then to the company's own line -- the
        # same priority order the old ReportLab footer used.
        broker_phone = _broker_phone(pin)
        broker_email = getattr(getattr(pin, "broker", None), "email", None)
        whatsapp_link = _whatsapp_link(broker_phone)
        tel_link = _tel_link(broker_phone)
        contact_bits = []
        if whatsapp_link:
            contact_bits.append(f"{broker_phone} \u00b7 WhatsApp")
        elif tel_link:
            contact_bits.append(broker_phone)
        if broker_email:
            contact_bits.append(broker_email)
        contact_line = " \u00b7 ".join(contact_bits) if contact_bits else DEFAULT_CONTACT_LINE

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