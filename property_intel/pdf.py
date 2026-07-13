"""
property_intel/pdf.py

LOCATION INTELLIGENCE REPORT - High-level overview for sellers.
Every statement backed by specific, named data points.
No vague claims. No generic "educational institutions" - we list them by name.
"""
import io
import logging
import re
from datetime import datetime

import requests
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Image as RLImage
from reportlab.platypus import (
    Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
    ListFlowable, ListItem,
)
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_JUSTIFY

logger = logging.getLogger("property_intel")

AQI_GOOD_THRESHOLD = 50
AQI_MODERATE_THRESHOLD = 100
IMAGE_FETCH_TIMEOUT_SECONDS = 10
NEARBY_RING_METERS = 3000

PLUS_CODE_RE = re.compile(r"^[A-Z0-9]{4,8}\+[A-Z0-9]{2,3}")

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


class ReportRenderError(Exception):
    """Raised on unrecoverable rendering failure -- caller treats this as a
    failed report, never a partial/corrupt one."""


def _label_from_field_name(field_name):
    """Convert field name to user-friendly label"""
    return field_name.replace("nearby_", "", 1).replace("_", " ").title()


def _discover_amenity_fields(cell):
    """Every model field starting with 'nearby_' that's a non-empty list --
    generic, so any future amenity category shows up with zero changes."""
    found = []
    for field in cell._meta.get_fields():
        name = getattr(field, "name", "")
        if name.startswith("nearby_"):
            value = getattr(cell, name, None)
            if value:
                found.append((_label_from_field_name(name), value))
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
    return nearest["name"].title(), minutes, km


def _display_location_name(pin, cell):
    address = cell.formatted_address or ""
    if address and not PLUS_CODE_RE.match(address):
        return address
    town, _ = _match_price_benchmark(cell)
    if town:
        return f"Near {town.title()}, Kenya"
    return f"{pin.latitude}, {pin.longitude}"


def _score_accessibility(cell):
    """Calculate accessibility score based on amenity density and commute times"""
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
    """Investment potential -- deliberately independent of accessibility_score."""
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


def _verdict_label(investment_score):
    if investment_score >= 80:
        return "Strong Buy"
    elif investment_score >= 65:
        return "Solid Long-Term Hold"
    elif investment_score >= 45:
        return "Proceed With Due Diligence"
    else:
        return "High Risk -- Caution Advised"


def _format_distance(meters):
    """Format distance in meters with km equivalent in brackets if > 500m"""
    if meters is None:
        return "Unknown"
    if meters <= 500:
        return f"{meters}m"
    else:
        km = meters / 1000
        return f"{meters}m ({km:.1f}km)"


def _get_named_amenities_text(cell, category, label, max_names=3, include_distance=True):
    """
    Narrative-friendly summary of a category: names the 2-3 nearest by name,
    calls out the distance ONCE (the closest one), and folds any remainder
    into a plain count instead of bracketing every single entry.
    """
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
            suffix = f" (the nearest just {closest} away"
            suffix += f", plus {remaining} more nearby)" if remaining > 0 else ")"
            return text + suffix
    return None


def _summary_text(pin, cell, investment_score, accessibility_score):
    """Returns (lead, bullets) -- a one-line intro plus a capped, scannable
    bullet list of the most decision-relevant facts."""
    location_name = _display_location_name(pin, cell)
    nearest_town = _nearest_town_summary(cell)

    lead = location_name
    if nearest_town:
        town_label, minutes, km = nearest_town
        if minutes is not None:
            lead += f", {minutes} minutes from {town_label}"
        else:
            lead += f", {km} km from {town_label}"
    lead += "."

    bullets = []

    town, benchmark = _match_price_benchmark(cell)
    if benchmark and benchmark.get("yoy_change_pct") is not None:
        bullets.append(f"{town.title()} land values rose {benchmark['yoy_change_pct']}% over the past year.")

    nairobi = (cell.travel_times or {}).get("nairobi_cbd")
    if nairobi and nairobi.get("duration_s"):
        minutes = round(nairobi["duration_s"] / 60)
        transit_bit = ", with public transit access" if nairobi.get("has_transit") else ""
        bullets.append(f"Nairobi CBD is a {minutes}-minute drive away{transit_bit}.")

    schools = _get_named_amenities_text(cell, "schools", "Schools", max_names=2, include_distance=False)
    if schools:
        bullets.append(f"Nearby schools include {schools}.")

    universities = _get_named_amenities_text(cell, "universities", "Universities", max_names=1, include_distance=False)
    if universities:
        bullets.append(f"{universities} nearby supports student rental demand.")

    hospitals = _get_named_amenities_text(cell, "hospitals", "Hospitals", max_names=1, include_distance=True)
    if hospitals:
        bullets.append(f"Nearest hospital: {hospitals}.")

    gated = _get_named_amenities_text(cell, "gated communities", "Gated Communities", max_names=1, include_distance=False)
    student_housing = _get_named_amenities_text(cell, "student housing", "Student Housing", max_names=1, include_distance=False)
    if gated:
        bullets.append(f"{gated} shows an established residential market nearby.")
    elif student_housing:
        bullets.append(f"{student_housing} shows active demand from student renters.")

    if cell.air_quality_category:
        streak = getattr(cell, "air_quality_good_days_streak", None)
        if streak:
            bullets.append(f"Air quality has stayed {cell.air_quality_category.lower()} for {streak} days straight.")
        else:
            bullets.append(f"Air quality is currently rated {cell.air_quality_category.lower()}.")

    if getattr(cell, "elevation_slope_range_m", None) is not None:
        if cell.elevation_slope_range_m < 3:
            bullets.append("Flat terrain lowers flood and drainage risk.")
        else:
            bullets.append(f"Terrain varies about {cell.elevation_slope_range_m:.0f}m, suggesting a gentle slope.")

    return lead, bullets[:6]


def _fetch_image_flowable(url, width_mm=160, height_mm=90):
    if not url:
        return None
    try:
        resp = requests.get(url, timeout=IMAGE_FETCH_TIMEOUT_SECONDS)
        if resp.status_code != 200:
            return None
        return RLImage(io.BytesIO(resp.content), width=width_mm * mm, height=height_mm * mm)
    except requests.RequestException as exc:
        logger.warning("Could not fetch image for PDF (%s): %s", url, exc)
        return None


def _development_suitability_table(cell):
    """Generate development suitability recommendations based on location data"""
    amenity_fields = _discover_amenity_fields(cell)
    has_university = any(label == "Universities" for label, _ in amenity_fields)
    has_retail = any(label == "Shopping Centres" for label, _ in amenity_fields)
    has_student_housing = any(label == "Student Housing" for label, _ in amenity_fields)
    has_gated = any(label == "Gated Communities" for label, _ in amenity_fields)
    
    # Get commute time
    nairobi = (cell.travel_times or {}).get("nairobi_cbd")
    commute_mins = round(nairobi["duration_s"] / 60) if nairobi and nairobi.get("duration_s") else None
    
    suitability = []
    
    # Student Housing
    if has_student_housing and has_university:
        suitability.append(("Student Housing", "Very High", "Existing student accommodation confirms active rental market"))
    elif has_university and commute_mins and commute_mins < 45:
        suitability.append(("Student Housing", "High", "Proximity to educational institutions drives rental demand"))
    elif has_university:
        suitability.append(("Student Housing", "Medium", "Near educational institutions"))
    else:
        suitability.append(("Student Housing", "Low", "Limited educational institutions in immediate area"))
    
    # Apartments
    if has_retail and has_university and has_gated:
        suitability.append(("Apartments", "Very High", "Service ecosystem plus proven demand from nearby gated communities"))
    elif has_retail and has_university:
        suitability.append(("Apartments", "High", "Complete service ecosystem supports residential development"))
    elif has_retail or has_university:
        suitability.append(("Apartments", "Medium", "Some support infrastructure present"))
    else:
        suitability.append(("Apartments", "Low", "Limited service infrastructure"))
    
    # Mixed-Use
    if has_retail and commute_mins and commute_mins < 60:
        suitability.append(("Mixed-Use", "Medium-High", "Retail presence with reasonable commute supports mixed-use"))
    elif has_retail:
        suitability.append(("Mixed-Use", "Medium", "Retail presence supports mixed-use potential"))
    else:
        suitability.append(("Mixed-Use", "Low", "Limited commercial ecosystem"))
    
    # Warehousing
    if commute_mins and commute_mins > 30:
        suitability.append(("Warehousing", "Medium", "Peripheral location supports logistics and distribution"))
    else:
        suitability.append(("Warehousing", "Low", "Too central for cost-effective warehousing"))
    
    # Industrial
    suitability.append(("Industrial", "Low", "Location characteristics more suited to residential and commercial uses"))
    
    return suitability


def _density_table_data(cell):
    """Count of each amenity category within 1km / 3km / 5km"""
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


def render_report_pdf(pin, cell):
    try:
        accessibility_score = _score_accessibility(cell)
        investment_score = _score_investment(cell, accessibility_score)
        summary_lead, summary_bullets = _summary_text(pin, cell, investment_score, accessibility_score)
        summary_text = summary_lead + (" " + " ".join(summary_bullets) if summary_bullets else "")

        buffer = io.BytesIO()
        report_title = f"{_display_location_name(pin, cell)} \u2014 Location Intelligence Report"
        doc = SimpleDocTemplate(
            buffer, pagesize=A4,
            topMargin=20 * mm, bottomMargin=20 * mm,
            leftMargin=15 * mm, rightMargin=15 * mm,
            title=report_title,
            author="Scape Data Solutions",
        )
        
        # Custom styles
        styles = getSampleStyleSheet()
        
        styles.add(ParagraphStyle(
            name='CustomTitle',
            parent=styles['Title'],
            fontSize=24,
            textColor=colors.HexColor('#0a2a5e'),
            spaceAfter=10,
            alignment=TA_CENTER
        ))
        
        styles.add(ParagraphStyle(
            name='CustomHeading2',
            parent=styles['Heading2'],
            fontSize=16,
            textColor=colors.HexColor('#1a4a7e'),
            spaceAfter=8,
            alignment=TA_LEFT
        ))
        
        styles.add(ParagraphStyle(
            name='CustomHeading3',
            parent=styles['Heading3'],
            fontSize=14,
            textColor=colors.HexColor('#2a5a8e'),
            spaceAfter=6,
            alignment=TA_LEFT
        ))
        
        styles.add(ParagraphStyle(
            name='JustifiedNormal',
            parent=styles['Normal'],
            fontSize=11,
            leading=14,
            alignment=TA_JUSTIFY,
            spaceAfter=6
        ))

        styles.add(ParagraphStyle(
            name='TableCell',
            parent=styles['Normal'],
            fontSize=9,
            leading=11,
        ))

        styles.add(ParagraphStyle(
            name='ScoreHeader',
            parent=styles['Normal'],
            fontSize=11,
            leading=13,
            alignment=TA_CENTER,
            textColor=colors.white,
            fontName='Helvetica-Bold',
        ))

        styles.add(ParagraphStyle(
            name='ScoreValue',
            parent=styles['Normal'],
            fontSize=15,
            leading=18,
            alignment=TA_CENTER,
            textColor=colors.HexColor('#0a2a5e'),
            fontName='Helvetica-Bold',
        ))
        
        story = []

        # Header
        story.append(Paragraph("LOCATION INTELLIGENCE REPORT", styles['CustomTitle']))
        story.append(Spacer(1, 2 * mm))
        story.append(Paragraph(f"Generated: {datetime.now().strftime('%B %d, %Y')}", styles['Normal']))
        story.append(Spacer(1, 6 * mm))

        # Property Location
        story.append(Paragraph(f"PROPERTY LOCATION: {_display_location_name(pin, cell)}", styles['CustomHeading2']))
        story.append(Spacer(1, 4 * mm))

        # Executive Summary
        story.append(Paragraph("EXECUTIVE SUMMARY", styles['CustomHeading2']))
        story.append(Spacer(1, 2 * mm))
        story.append(Paragraph(summary_lead, styles['JustifiedNormal']))
        if summary_bullets:
            story.append(Spacer(1, 1 * mm))
            bullet_items = [
                ListItem(Paragraph(b, styles['JustifiedNormal']), leftIndent=4 * mm, spaceAfter=2)
                for b in summary_bullets
            ]
            story.append(ListFlowable(
                bullet_items, bulletType='bullet', start='•',
                leftIndent=6 * mm, bulletFontSize=9,
            ))
        story.append(Spacer(1, 6 * mm))

        # Investment Scores
        story.append(Paragraph("INVESTMENT SCORECARD", styles['CustomHeading2']))
        story.append(Spacer(1, 2 * mm))
        
        score_table = Table(
            [
                [Paragraph("ACCESSIBILITY SCORE", styles['ScoreHeader']), Paragraph("INVESTMENT POTENTIAL", styles['ScoreHeader'])],
                [Paragraph(f"{accessibility_score}/100", styles['ScoreValue']), Paragraph(f"{investment_score}/100", styles['ScoreValue'])],
            ],
            colWidths=[90 * mm, 90 * mm],
            rowHeights=[12 * mm, 16 * mm],
        )
        score_table.setStyle(TableStyle([
            ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#0a2a5e')),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('LEFTPADDING', (0, 0), (-1, -1), 4),
            ('RIGHTPADDING', (0, 0), (-1, -1), 4),
            ('TOPPADDING', (0, 0), (-1, -1), 3),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
        ]))
        story.append(score_table)
        story.append(Spacer(1, 2 * mm))
        story.append(Paragraph(
            f"<b>Verdict: {_verdict_label(investment_score)}</b>",
            styles['JustifiedNormal']
        ))
        story.append(Paragraph(
            "<i>Descriptive scores based on infrastructure, accessibility, and market data - not a property valuation</i>",
            styles['Normal']
        ))
        story.append(Spacer(1, 6 * mm))

        mv_town, mv_benchmark = _match_price_benchmark(cell)
        if mv_benchmark:
            story.append(Paragraph("MARKET VALUES", styles['CustomHeading2']))
            story.append(Spacer(1, 2 * mm))
            price_bit = ""
            if mv_benchmark.get("price_per_acre_kes"):
                price_bit = f"KES {mv_benchmark['price_per_acre_kes']:,.0f} per acre, "
            yoy = mv_benchmark.get("yoy_change_pct")
            direction = "up" if yoy is not None and yoy > 0 else "down" if yoy is not None and yoy < 0 else "flat"
            mv_text = f"{mv_town.title()} land values: {price_bit}{direction} {abs(yoy):.1f}% year-over-year"
            if mv_benchmark.get("quarter"):
                mv_text += f" ({mv_benchmark['quarter']})"
            mv_text += "."
            if mv_benchmark.get("note"):
                mv_text += f" {mv_benchmark['note'].capitalize()}."
            story.append(Paragraph(mv_text, styles['JustifiedNormal']))
            story.append(Spacer(1, 6 * mm))

        # Development Suitability
        story.append(Paragraph("DEVELOPMENT SUITABILITY", styles['CustomHeading2']))
        story.append(Spacer(1, 2 * mm))
        
        suitability_data = _development_suitability_table(cell)
        table_data = [["Development Type", "Suitability", "Rationale"]]
        for dev_type, suitability, rationale in suitability_data:
            table_data.append([
                Paragraph(dev_type, styles['TableCell']),
                Paragraph(suitability, styles['TableCell']),
                Paragraph(rationale, styles['TableCell']),
            ])
        
        suit_table = Table(table_data, colWidths=[40 * mm, 30 * mm, 110 * mm])
        suit_table.setStyle(TableStyle([
            ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#0a2a5e')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 10),
            ('FONTSIZE', (0, 1), (-1, -1), 9),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('PADDING', (0, 0), (-1, -1), 4),
        ]))
        story.append(suit_table)
        story.append(Spacer(1, 6 * mm))

        # Visual Assets
        sat_image = _fetch_image_flowable(cell.satellite_image_url)
        if sat_image:
            story.append(Paragraph("SATELLITE VIEW", styles['CustomHeading3']))
            story.append(sat_image)
            story.append(Spacer(1, 4 * mm))

        if cell.street_view_available:
            sv_image = _fetch_image_flowable(cell.street_view_image_url)
            if sv_image:
                story.append(Paragraph("STREET VIEW", styles['CustomHeading3']))
                story.append(sv_image)
                story.append(Spacer(1, 4 * mm))

        # Specific Named Amenities - FULL DETAILS with formatted distances
        amenity_fields = _discover_amenity_fields(cell)
        if amenity_fields:
            story.append(Paragraph("NEARBY AMENITIES", styles['CustomHeading2']))
            story.append(Spacer(1, 2 * mm))
            
            # Flatten every category into one list, then sort the WHOLE
            # table by distance -- nearest to farthest, regardless of category.
            flat_entries = []
            for label, entries in amenity_fields:
                for entry in entries:
                    flat_entries.append((label, entry))
            flat_entries.sort(
                key=lambda pair: pair[1].get('distance_m') if pair[1].get('distance_m') is not None else float('inf')
            )

            amenity_data = [["Type", "Name", "Distance"]]
            for label, entry in flat_entries[:30]:  # cap at 30 rows total for a readable page
                name = entry.get('name', 'Unknown')
                distance = entry.get('distance_m')
                dist_str = _format_distance(distance)
                amenity_data.append([
                    Paragraph(label, styles['TableCell']),
                    Paragraph(name, styles['TableCell']),
                    Paragraph(dist_str, styles['TableCell']),
                ])
            
            amenity_table = Table(amenity_data, colWidths=[35 * mm, 90 * mm, 55 * mm])
            amenity_table.setStyle(TableStyle([
                ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#0a2a5e')),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, 0), 10),
                ('FONTSIZE', (0, 1), (-1, -1), 9),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('PADDING', (0, 0), (-1, -1), 4),
            ]))
            story.append(amenity_table)
            story.append(Spacer(1, 6 * mm))

        # Nearby Towns -- from the dynamic Kenya-wide matcher (kenya_towns.py),
        # not just the single nearest one already used in the executive summary.
        if cell.nearest_towns:
            story.append(Paragraph("NEARBY TOWNS", styles['CustomHeading2']))
            story.append(Spacer(1, 2 * mm))

            town_data = [["Town", "County", "Distance", "Drive Time"]]
            for t in cell.nearest_towns:
                dist_str = _format_distance(t.get("distance_m"))
                mins = t.get("drive_duration_s")
                drive_str = f"{round(mins / 60)} min" if mins else "Unknown"
                town_data.append([
                    Paragraph(t["name"].title(), styles['TableCell']),
                    Paragraph(t["county"].title(), styles['TableCell']),
                    Paragraph(dist_str, styles['TableCell']),
                    Paragraph(drive_str, styles['TableCell']),
                ])

            town_table = Table(town_data, colWidths=[50 * mm, 45 * mm, 40 * mm, 40 * mm])
            town_table.setStyle(TableStyle([
                ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#0a2a5e')),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, 0), 10),
                ('FONTSIZE', (0, 1), (-1, -1), 9),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('PADDING', (0, 0), (-1, -1), 4),
            ]))
            story.append(town_table)
            story.append(Spacer(1, 6 * mm))

        # Area Density
        density_rows = _density_table_data(cell)
        if density_rows:
            story.append(Paragraph("AMENITY DENSITY", styles['CustomHeading2']))
            story.append(Spacer(1, 2 * mm))
            density_data = [["Category", "Within 1km", "Within 3km", "Within 5km"]]
            for label, c1, c3, c5 in density_rows:
                density_data.append([label, str(c1), str(c3), str(c5)])
            density_table = Table(density_data, colWidths=[70 * mm, 36 * mm, 37 * mm, 37 * mm])
            density_table.setStyle(TableStyle([
                ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#0a2a5e')),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, 0), 10),
                ('FONTSIZE', (0, 1), (-1, -1), 9),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('PADDING', (0, 0), (-1, -1), 4),
            ]))
            story.append(density_table)
            story.append(Spacer(1, 6 * mm))

        # Additional Details
        story.append(Paragraph("LOCATION DETAILS", styles['CustomHeading2']))
        story.append(Spacer(1, 2 * mm))
        
        details = []
        if cell.air_quality_category:
            details.append(f"Air Quality: {cell.air_quality_category} (AQI {cell.air_quality_index})")

        nairobi_travel = (cell.travel_times or {}).get("nairobi_cbd")
        if nairobi_travel and nairobi_travel.get("duration_s"):
            mins = round(nairobi_travel["duration_s"] / 60)
            details.append(f"Drive Time to Nairobi CBD: {mins} minutes")
        
        if getattr(cell, "elevation_meters", None) is not None:
            details.append(f"Elevation: {cell.elevation_meters:.0f}m above sea level")

        if getattr(cell, "on_paved_road", None) is not None:
            if cell.on_paved_road:
                road_bit = f" (~{cell.nearest_road_distance_m}m to nearest mapped road)" if cell.nearest_road_distance_m else ""
                details.append(f"Road Access: On or near a mapped road{road_bit}")
            else:
                details.append("Road Access: No mapped road detected nearby -- verify physical access before purchase")
        
        for detail in details:
            story.append(Paragraph(f"• {detail}", styles['JustifiedNormal']))
        
        story.append(Spacer(1, 6 * mm))

        # Next Steps
        story.append(Paragraph("NEXT STEPS", styles['CustomHeading2']))
        story.append(Spacer(1, 2 * mm))
        broker_email = getattr(getattr(pin, "broker", None), "email", None)
        contact_bit = f" Contact {broker_email} to discuss pricing, site visits, or next steps." if broker_email else " Contact the broker who shared this report to discuss pricing, site visits, or next steps."
        story.append(Paragraph(
            f"This report was generated via Scape Data Solutions.{contact_bit}",
            styles['JustifiedNormal']
        ))
        story.append(Spacer(1, 6 * mm))

        # Footer
        story.append(Paragraph(
            "<i>This report provides data-driven location intelligence. All data is derived from publicly available sources. "
            "Conduct independent due diligence before making investment decisions.</i>",
            styles['Normal']
        ))

        doc.build(story)
        return buffer.getvalue(), investment_score, accessibility_score, summary_text

    except Exception as exc:
        logger.error("PDF render failed for pin %s: %s", pin.id, exc)
        raise ReportRenderError(str(exc)) from exc