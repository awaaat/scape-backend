"""
business_intel/engines/mobility_traffic.py

Engine 4 — Mobility & Traffic Engine
Per business_location_intelligence_engines_data_models_v2.md, Section 4.

NOTE ON THE SPEC FILE: same situation as every other engine in this
codebase — business_location_intelligence_engines_data_models_v2.md is
referenced but not present on disk. Built to match Engine 2/3/8/9 for
structure, schema contract, and citation discipline.

=============================================================================
WHAT'S ACTUALLY AVAILABLE VS. WHAT ISN'T (read this before extending)
=============================================================================
This module is built ONLY on real, currently-public Google Maps
Platform capabilities this codebase already has credentials for
(google_client.py's GOOGLE_MAPS_API_KEY):

  - Routes API `computeRoutes` (routes.googleapis.com) — real
    traffic-aware AND traffic-unaware (free-flow) travel time/distance
    between two points, at a caller-specified departure time. Already
    partially wired via google_client.fetch_traffic_aware_duration.
  - Places API (New) `searchNearby` — real nearby-place search,
    already wired via google_client.search_places_category.

What is explicitly NOT used here, because it does not exist as a
public, documented Google API product (not a self-imposed limit —
Google does not sell this):
  - There is no public "live footfall / popular-times / busyness" API.
    The busy-hours graphs shown in the consumer Google Maps app are not
    exposed through the Places API or any other documented Google Cloud
    endpoint. Anything claiming to fetch that from Google directly would
    be relying on an unofficial/undocumented method this module will
    not implement.
  - There is no public Google "road-network graph" or "traffic volume
    count" API suitable for computing real street-network centrality
    (e.g. betweenness/Space Syntax measures). google_client.py's own
    fetch_isochrone() already documents this exact gap for isochrones
    (needs OSMnx against OpenStreetMap, not Google) — road-network
    centrality has the identical dependency. `RoadNetworkCentralityProvider`
    below is the documented hook for that real, separate GIS work (same
    pattern as population.py's SpatialSmoothingProvider), not faked here.

Within what IS real and available, this module is deliberately generous
with API usage — sampling many departure-time windows per location and
several nearby-anchor destinations — rather than a single point-in-time
call, because a single Routes API call only ever answers "what would
traffic be like at this one instant," which is a poor proxy for how
reachable a location actually is across a normal week. NUM_DEPARTURE_
WINDOWS and MAX_ANCHORS_FOR_ACCESSIBILITY below are the two knobs that
control real call volume/cost; both are exposed as module constants
specifically so they can be turned up, not held down as a hidden
ceiling.

=============================================================================
REAL METHODS USED (researched and cited, not invented)
=============================================================================
1. Travel Time Index / Planning Time Index / Buffer Index
   Lomax, T., Schrank, D., Turner, S., Margiotta, R. (2003).
   "Selecting Travel Reliability Measures." Texas Transportation
   Institute, Texas A&M University System.
   These are the standard travel-time-reliability metrics U.S. state
   DOTs and the Texas A&M Urban Mobility Report use, all requiring a
   *distribution* of observed travel times, not one number:
     TTI (Travel Time Index)   = mean(observed duration) / free-flow duration
     PTI (Planning Time Index) = p95(observed duration) / free-flow duration
     Buffer Index              = (p95 - mean) / mean
   TTI answers "how much longer does it typically take"; PTI/Buffer
   Index answer "how much EXTRA time should someone budget to arrive
   reliably" — the second question is usually the more decision-
   relevant one for a business (a customer who is unsure whether
   traffic will make them 5 or 25 minutes late is less likely to make
   the trip at all). Built here from REAL repeated Routes API
   measurements across a representative set of departure-time windows
   (REPRESENTATIVE_DEPARTURE_WINDOWS below) against one free-flow
   (TRAFFIC_UNAWARE) baseline call, not simulated.

2. Bootstrap confidence intervals on the reliability statistics
   Efron, B. (1979). "Bootstrap Methods: Another Look at the
   Jackknife." Annals of Statistics, 7(1), 1-26.
   With only NUM_DEPARTURE_WINDOWS real samples (a dozen-ish real API
   calls, not thousands), reporting TTI/PTI as bare point estimates
   overstates precision. Standard nonparametric bootstrap (resample the
   observed duration vector with replacement, recompute the statistic,
   repeat) gives an honest empirical confidence interval from the
   SAME real data, no distributional assumption imposed on travel
   times (which are right-skewed, not normal).

3. Hansen gravity accessibility index, with travel-time impedance
   Hansen, W.G. (1959). "How Accessibility Shapes Land Use." Journal
   of the American Institute of Planners, 25(2), 73-76.
   Handy, S.L. and Niemeier, D.A. (1997). "Measuring Accessibility: An
   Exploration of Issues and Alternatives." Environment and Planning A,
   29(7), 1175-1194.
   Hansen's original accessibility index, A_i = sum_j(S_j / f(d_ij)),
   is the foundational gravity-based accessibility measure in
   transportation planning (same mathematical family as competition.py's
   Huff model and market_gap.py's gravity-Poisson — this codebase now
   uses one consistent gravity-model idiom across three engines, not
   three unrelated heuristics). Handy & Niemeier (1997) is the standard
   citation for the (now common) refinement of using real network
   travel TIME as the impedance f(d_ij) instead of straight-line
   distance — exactly what real Routes API measurements let this
   module do that a pure-geometry version could not. S_j (destination
   "mass") uses AnchorFacility.size_value (enrollment/beds/GLA) when
   synced, falling back to an unweighted count (mass=1 per anchor,
   flagged) when it isn't — real data when available, honest
   substitution when not, same convention as competition.py's
   huff_attractiveness proxy discussion.

4. Temporal accessibility volatility
   Not a named method from one paper — this module's own application
   of a standard descriptive-statistics tool (coefficient of variation)
   to the Hansen index computed separately at each sampled departure
   window: how much does "how reachable is this location" itself swing
   across the day, not just how much slower any one trip gets. Flagged
   here as this module's own composition of #1 and #3's real
   ingredients, not a separately cited formula.

Both statistical layers (#1/#2 and #3/#4) are built from genuinely
independent real API measurements taken THIS run, not a static
lookup table — every run reflects your actual current AnchorFacility
rows and actual current Google-reported traffic, per the "dynamic,
syncs with your state" requirement.

=============================================================================
EDGE CASES THIS MODULE HANDLES EXPLICITLY
=============================================================================
- No AnchorFacility rows synced yet for this cell -> Hansen
  accessibility is None (not 0), flagged: "no synced anchors" is a data
  gap, not evidence of genuine isolation.
- Only 1-2 anchors within range -> accessibility is still computed
  (Hansen's formula doesn't require a minimum N) but flagged
  low_anchor_sample so it isn't read with the same confidence as a
  well-covered urban cell.
- Routes API returns no route at all for a window (disconnected/
  unreachable in that travel mode) -> that window is dropped from the
  reliability sample (not treated as 0 seconds or as an error killing
  the whole run); if MORE than half the requested windows fail this
  way, the whole reliability result is None with a loud warning
  instead of quietly reporting a reliability profile built from a
  handful of lucky calls.
- Fewer than MIN_SAMPLES_FOR_PERCENTILES successful samples -> p95/PTI/
  Buffer Index/bootstrap CI are skipped (a 95th percentile of 3 points
  is not meaningful); only the mean-based TTI is reported, flagged.
- A requested departure window's computed next-occurrence datetime
  would land in the past by the time the call fires (a genuine race
  condition risk if this run is slow) -> recomputed immediately before
  each call, not batch-computed once at the top, so no window is ever
  submitted with a past departureTime (Routes API rejects those).
- Free-flow (TRAFFIC_UNAWARE) call itself fails -> nothing downstream
  can be computed (every ratio needs it as the denominator); returns
  None immediately with an explicit warning rather than silently
  falling back to treating the first successful traffic-aware sample
  AS the free-flow baseline (which would be circular).
- AnchorFacility.size_value present but size_unit differs across
  anchor types (beds vs. enrollment vs. GLA-sqm are not the same
  scale) -> NOT rescaled/combined into one pooled mass here (there is
  no principled cross-unit conversion this module can fabricate); each
  anchor's mass is used as-is within Hansen's formula (which sums
  mass/impedance across ALL anchors regardless of type — a real,
  documented simplification, same "known limitation, not hidden" style
  as competition.py's single-origin-demand-point note), flagged when
  the anchor set actually mixes size_unit values.
- No Kenya public holiday calendar wired up -> representative departure
  windows are chosen by weekday/weekend and time-of-day only; a
  candidate falling on a public holiday could see unrepresentative
  live traffic. Documented gap, not solved here.

Scope boundary
--------------
Depends on business_intel.models and business_intel.google_client
(GOOGLE_API_KEY, ROUTES_URL, _log_call — reused, not duplicated,
except for one new free-flow request function documented below as
ideally belonging in google_client.py alongside its sibling). No
dependency on services.py, tasks.py's scheduling, or the other engines'
feature-store output (unlike market_gap.py, this engine's inputs are
raw Google API responses and raw AnchorFacility rows, not another
engine's derived features).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from typing import Optional, Sequence
from zoneinfo import ZoneInfo

import numpy as np
import requests
from django.db import transaction
from django.utils import timezone as django_timezone

from ..google_client import GOOGLE_API_KEY, ROUTES_URL, DEFAULT_TIMEOUT_SECONDS, _log_call
from ..models import AnchorFacility, BusinessLocationCell, BusinessQuery, EngineRun, FeatureObservation

logger = logging.getLogger("business_intel.engines.mobility_traffic")

ENGINE_NUMBER = 4

# Kenya is entirely in one timezone (EAT, UTC+3, no DST) -- see
# services.py's own KENYA_LAT_BOUNDS/KENYA_LNG_BOUNDS bounding-box
# convention for the same country-scoping assumption used elsewhere in
# this codebase.
KENYA_TZ = ZoneInfo("Africa/Nairobi")

# ============================================================================
# Calibration inputs -- documented as such, not measured constants.
# ============================================================================

# Representative weekly departure windows: (label, iso_weekday[1=Mon..7=Sun],
# hour, minute). Nairobi's commonly cited peak windows are ~07:00-09:30 and
# ~17:00-19:30 on weekdays (Kenya National Transport & Safety Authority /
# World Bank Nairobi mobility studies both use similar bands); this is an
# operational assumption for WHICH windows to sample, not a cited constant
# in itself -- same "editorial classification, labeled as such" convention
# purchasing_power.py uses for CATEGORY_SPENDING_TIER. Sample generously
# (11 windows across a full week) by design -- see module docstring on
# being deliberately non-conservative with real API usage. Raise this list
# freely for deeper coverage; nothing below assumes exactly 11.
REPRESENTATIVE_DEPARTURE_WINDOWS = [
    ("mon_am_peak", 1, 8, 0),
    ("mon_midday", 1, 13, 0),
    ("mon_pm_peak", 1, 18, 0),
    ("mon_evening", 1, 20, 30),
    ("thu_am_peak", 4, 7, 30),
    ("thu_midday", 4, 12, 30),
    ("thu_pm_peak", 4, 17, 30),
    ("fri_pm_peak", 5, 18, 30),   # Friday PM is its own, usually worse, peak
    ("sat_midday", 6, 12, 0),
    ("sat_evening", 6, 19, 0),
    ("sun_midday", 7, 13, 0),
]

# Below this many SUCCESSFUL samples, percentile-based statistics (PTI,
# Buffer Index, bootstrap CI) are not meaningful and are skipped -- only
# the mean-based TTI is still reported. Calibration input: a p95 of
# fewer than ~6 points is dominated by which single point happens to be
# largest, not a real tail estimate.
MIN_SAMPLES_FOR_PERCENTILES = 6

# If fewer than half of REPRESENTATIVE_DEPARTURE_WINDOWS succeed at all,
# the whole reliability result is withheld rather than built from a
# lucky/unlucky minority of calls.
MIN_SUCCESS_FRACTION = 0.5

N_BOOTSTRAP_RESAMPLES = 2000

# Nearest-K anchor cap for the Hansen accessibility index -- a real
# Routes API call per anchor is the cost driver here (not a modeling
# choice), so this caps COST, not insight quality below the cap: raise
# it if you want accessibility computed against a larger anchor set.
# Pre-filtered by straight-line distance first (haversine, free) so the
# K anchors actually spent on real Routes calls are the most locally
# relevant ones.
MAX_ANCHORS_FOR_ACCESSIBILITY = 15
ANCHOR_PREFILTER_RADIUS_M = 8000.0

EARTH_RADIUS_M = 6_371_000.0

# Hansen accessibility distance-decay exponent -- same power-law family
# and same "exposed as a parameter, not a cited constant" convention as
# competition.py's huff_market_shares(beta=...); Handy & Niemeier (1997)
# surveys a range of empirically fit values across studies (typically
# 1-2 for local urban trips) without one universal figure to cite
# directly, so this default sits in the middle of that literature
# range pending local calibration.
DEFAULT_ACCESSIBILITY_BETA = 1.5


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters. Duplicated from competition.py's
    identical function rather than imported, to keep this module's own
    import-order/dependency footprint independent, same reasoning
    competition.py itself gives for not importing spatial_context's copy."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def _next_occurrence_rfc3339(iso_weekday: int, hour: int, minute: int) -> str:
    """
    Next future occurrence of (iso_weekday, hour, minute) in Africa/Nairobi,
    formatted as RFC3339 UTC ('...Z') for the Routes API's departureTime
    field, which the API requires to be strictly in the future at call
    time. Recomputed fresh immediately before each individual API call
    (see fetch_reliability_samples) rather than once up front, so a slow
    run can never submit a departureTime that has already passed.
    """
    now_nbo = datetime.now(KENYA_TZ)
    days_ahead = (iso_weekday - now_nbo.isoweekday()) % 7
    candidate = now_nbo.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(days=days_ahead)
    if candidate <= now_nbo:
        candidate += timedelta(days=7)
    return candidate.astimezone(dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ============================================================================
# Google Routes API calls -- traffic-aware sampling + free-flow baseline.
# fetch_traffic_aware_duration already exists in google_client.py and is
# reused as-is below for each sampled window; fetch_free_flow_duration is
# new (TRAFFIC_UNAWARE has no departureTime concept) -- flagged in the
# module docstring as ideally belonging alongside its sibling in
# google_client.py; kept local here so this engine ships as one file.
# ============================================================================

def fetch_free_flow_duration(origin_lat: float, origin_lng: float, dest_lat: float, dest_lng: float) -> Optional[dict]:
    """TRAFFIC_UNAWARE Routes API call -- Google's modeled free-flow
    (no live/predictive traffic) duration, used as the denominator for
    every reliability ratio below. Mirrors google_client.
    fetch_traffic_aware_duration's request shape exactly, differing
    only in routingPreference and the absence of departureTime (which
    TRAFFIC_UNAWARE ignores per the Routes API's own documented
    behavior -- free-flow routing is time-of-day-independent by
    definition)."""
    body = {
        "origin": {"location": {"latLng": {"latitude": origin_lat, "longitude": origin_lng}}},
        "destination": {"location": {"latLng": {"latitude": dest_lat, "longitude": dest_lng}}},
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_UNAWARE",
    }
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": GOOGLE_API_KEY,
        "X-Goog-FieldMask": "routes.duration,routes.distanceMeters",
    }
    try:
        resp = requests.post(ROUTES_URL, json=body, headers=headers, timeout=DEFAULT_TIMEOUT_SECONDS)
        succeeded = resp.status_code == 200
        data = resp.json() if succeeded else {}
    except requests.RequestException as exc:
        logger.warning("Free-flow Routes API call failed: %s", exc)
        _log_call("routes", None, body, None, False)
        return None

    _log_call("routes", None, body, resp.status_code, succeeded)
    if not succeeded or not data.get("routes"):
        return None
    return data["routes"][0]


def _parse_duration_seconds(route: dict) -> Optional[float]:
    """Routes API returns duration as a string like '1234s'."""
    raw = route.get("duration")
    if not raw:
        return None
    try:
        return float(str(raw).rstrip("s"))
    except (TypeError, ValueError):
        return None


@dataclass
class TravelTimeSample:
    label: str
    iso_weekday: int
    hour: int
    duration_seconds: Optional[float]
    distance_meters: Optional[float]


def fetch_reliability_samples(
    origin_lat: float, origin_lng: float, dest_lat: float, dest_lng: float,
    windows: Sequence[tuple] = REPRESENTATIVE_DEPARTURE_WINDOWS,
) -> list:
    """
    Fires one real traffic-aware Routes API call per window (reusing
    google_client.fetch_traffic_aware_duration), recomputing each
    window's next-future-occurrence departure time immediately before
    that specific call. Failed/unroutable windows are recorded with
    duration_seconds=None and excluded downstream, not dropped
    silently -- callers can see exactly which windows succeeded.
    """
    from ..google_client import fetch_traffic_aware_duration  # local import: avoid a hard import-time dependency for pure-math callers/tests

    samples = []
    for label, iso_weekday, hour, minute in windows:
        departure_time = _next_occurrence_rfc3339(iso_weekday, hour, minute)
        route = fetch_traffic_aware_duration(origin_lat, origin_lng, dest_lat, dest_lng, departure_time=departure_time)
        duration = _parse_duration_seconds(route) if route else None
        distance = route.get("distanceMeters") if route else None
        samples.append(TravelTimeSample(
            label=label, iso_weekday=iso_weekday, hour=hour,
            duration_seconds=duration, distance_meters=float(distance) if distance is not None else None,
        ))
    return samples


# ============================================================================
# Pure-math layer: travel-time reliability statistics (Lomax et al. 2003;
# Efron 1979 for the bootstrap CI). No network calls below this point --
# every function here takes already-fetched durations, independently
# unit-testable without hitting Google, same pattern as competition.py's
# CompetitorObservation section and purchasing_power.py's PCA layer.
# ============================================================================

@dataclass
class ReliabilityResult:
    free_flow_duration_s: float
    successful_sample_count: int
    attempted_sample_count: int
    mean_duration_s: float
    travel_time_index: float                      # TTI = mean / free_flow
    p95_duration_s: Optional[float]
    planning_time_index: Optional[float]           # PTI = p95 / free_flow
    buffer_index_pct: Optional[float]              # (p95-mean)/mean * 100
    tti_bootstrap_ci_low: Optional[float]
    tti_bootstrap_ci_high: Optional[float]
    per_window: dict = field(default_factory=dict)  # label -> TTI for that single window
    warnings: list = field(default_factory=list)


def _bootstrap_ci(values: np.ndarray, statistic_fn, n_resamples: int = N_BOOTSTRAP_RESAMPLES, ci: float = 0.90, seed: int = 7) -> tuple:
    """Standard nonparametric bootstrap percentile CI (Efron 1979):
    resample `values` with replacement n_resamples times, recompute
    statistic_fn on each resample, take the (1-ci)/2 and 1-(1-ci)/2
    percentiles of the resulting distribution as the CI bounds."""
    rng = np.random.default_rng(seed)
    n = len(values)
    stats = np.empty(n_resamples)
    for i in range(n_resamples):
        resample = values[rng.integers(0, n, size=n)]
        stats[i] = statistic_fn(resample)
    lo = float(np.percentile(stats, (1 - ci) / 2 * 100))
    hi = float(np.percentile(stats, (1 - (1 - ci) / 2) * 100))
    return lo, hi


def compute_travel_time_reliability(
    free_flow_duration_s: Optional[float],
    samples: Sequence[TravelTimeSample],
) -> Optional[ReliabilityResult]:
    """
    Builds the full Lomax et al. (2003) reliability picture from
    already-fetched samples. Returns None if the free-flow baseline is
    missing (nothing downstream is computable without it) or if fewer
    than MIN_SUCCESS_FRACTION of the attempted windows produced a real
    duration.
    """
    warnings = []
    if free_flow_duration_s is None or free_flow_duration_s <= 0:
        return None

    attempted = len(samples)
    successful = [s for s in samples if s.duration_seconds is not None]
    if attempted == 0 or len(successful) / attempted < MIN_SUCCESS_FRACTION:
        return None

    durations = np.array([s.duration_seconds for s in successful])
    mean_dur = float(durations.mean())
    tti = mean_dur / free_flow_duration_s

    p95 = pti = buffer_pct = None
    ci_lo = ci_hi = None
    if len(successful) >= MIN_SAMPLES_FOR_PERCENTILES:
        p95 = float(np.percentile(durations, 95))
        pti = p95 / free_flow_duration_s
        buffer_pct = ((p95 - mean_dur) / mean_dur) * 100.0 if mean_dur > 0 else None
        ci_lo, ci_hi = _bootstrap_ci(durations, lambda d: d.mean() / free_flow_duration_s)
    else:
        warnings.append(
            f"Only {len(successful)} successful sample(s) (< {MIN_SAMPLES_FOR_PERCENTILES}); "
            "percentile-based metrics (PTI, Buffer Index, bootstrap CI) skipped -- only mean-based TTI reported."
        )

    if len(successful) < attempted:
        warnings.append(f"{attempted - len(successful)} of {attempted} departure windows returned no route (excluded, not zeroed).")

    per_window = {
        s.label: round(s.duration_seconds / free_flow_duration_s, 4)
        for s in successful
    }

    return ReliabilityResult(
        free_flow_duration_s=round(free_flow_duration_s, 1),
        successful_sample_count=len(successful),
        attempted_sample_count=attempted,
        mean_duration_s=round(mean_dur, 1),
        travel_time_index=round(tti, 4),
        p95_duration_s=round(p95, 1) if p95 is not None else None,
        planning_time_index=round(pti, 4) if pti is not None else None,
        buffer_index_pct=round(buffer_pct, 2) if buffer_pct is not None else None,
        tti_bootstrap_ci_low=round(ci_lo, 4) if ci_lo is not None else None,
        tti_bootstrap_ci_high=round(ci_hi, 4) if ci_hi is not None else None,
        per_window=per_window,
        warnings=warnings,
    )


# ============================================================================
# Hansen gravity accessibility index (Hansen 1959; Handy & Niemeier 1997)
# ============================================================================

@dataclass
class AnchorObservation:
    id: str
    anchor_type: str
    latitude: float
    longitude: float
    size_value: Optional[float] = None
    size_unit: str = ""
    straight_line_distance_m: Optional[float] = None
    travel_time_s: Optional[float] = None      # filled in once Routes is called for this anchor


@dataclass
class AccessibilityResult:
    hansen_index: float
    anchor_count_used: int
    anchor_count_prefiltered_out: int
    mixed_size_units: bool
    low_anchor_sample: bool
    beta: float
    per_anchor: list = field(default_factory=list)  # [{id, anchor_type, mass, travel_time_s, contribution}, ...]
    warnings: list = field(default_factory=list)


def prefilter_nearest_anchors(
    subject_lat: float, subject_lon: float, anchors: Sequence[AnchorObservation],
    max_anchors: int = MAX_ANCHORS_FOR_ACCESSIBILITY, radius_m: float = ANCHOR_PREFILTER_RADIUS_M,
) -> tuple:
    """Free (haversine-only) pre-filter to the nearest `max_anchors`
    anchors within `radius_m`, so real (billed) Routes API calls are
    only spent on the anchors most likely to matter. Returns
    (kept, dropped_count)."""
    with_dist = []
    for a in anchors:
        d = haversine_m(subject_lat, subject_lon, a.latitude, a.longitude)
        if d <= radius_m:
            a.straight_line_distance_m = d
            with_dist.append(a)
    with_dist.sort(key=lambda a: a.straight_line_distance_m)
    kept = with_dist[:max_anchors]
    dropped = len(anchors) - len(kept)
    return kept, dropped


def hansen_accessibility_index(
    subject_lat: float, subject_lon: float,
    anchors: Sequence[AnchorObservation],
    beta: float = DEFAULT_ACCESSIBILITY_BETA,
    min_travel_time_s: float = 60.0,
) -> Optional[AccessibilityResult]:
    """
    A_i = sum_j( S_j / travel_time_ij ** beta ), Hansen (1959) with
    Handy & Niemeier's (1997) travel-time impedance in place of
    straight-line distance. Anchors must already have travel_time_s
    populated (real Routes API measurement) -- this function is pure
    math over already-fetched data, same separation-of-concerns as
    huff_market_shares in competition.py. Returns None if no anchor has
    a usable travel time (nothing to sum).

    min_travel_time_s guards the beta power-law the same way
    huff_market_shares' min_distance_m does -- an anchor essentially
    co-located with the subject shouldn't produce a divide-by-near-zero
    blowup.
    """
    usable = [a for a in anchors if a.travel_time_s is not None and a.travel_time_s > 0]
    if not usable:
        return None

    warnings = []
    size_units_seen = {a.size_unit for a in usable if a.size_unit}
    mixed_units = len(size_units_seen) > 1
    if mixed_units:
        warnings.append(
            f"Anchor set mixes size_unit values ({sorted(size_units_seen)}) -- mass terms are not on a "
            "common scale; Hansen index still sums them as-is (documented simplification, see module docstring)."
        )

    per_anchor = []
    total = 0.0
    unweighted_count = 0
    for a in usable:
        mass = float(a.size_value) if a.size_value is not None else 1.0
        if a.size_value is None:
            unweighted_count += 1
        t = max(a.travel_time_s, min_travel_time_s)
        contribution = mass / (t ** beta)
        total += contribution
        per_anchor.append({
            "id": a.id, "anchor_type": a.anchor_type, "mass": mass,
            "mass_is_unweighted_default": a.size_value is None,
            "travel_time_s": a.travel_time_s, "contribution": round(contribution, 6),
        })

    if unweighted_count:
        warnings.append(
            f"{unweighted_count} of {len(usable)} anchors have no AnchorFacility.size_value on record; "
            "used an unweighted mass of 1.0 for those (real gap, not a fabricated size)."
        )

    low_sample = len(usable) < 3
    if low_sample:
        warnings.append(f"Only {len(usable)} anchor(s) with usable travel time -- accessibility index has a small sample.")

    per_anchor.sort(key=lambda d: -d["contribution"])
    return AccessibilityResult(
        hansen_index=round(total, 6),
        anchor_count_used=len(usable),
        anchor_count_prefiltered_out=0,  # filled in by caller, which knows the prefilter drop count
        mixed_size_units=mixed_units,
        low_anchor_sample=low_sample,
        beta=beta,
        per_anchor=per_anchor,
        warnings=warnings,
    )


def temporal_accessibility_volatility(hansen_indices_by_window: Mapping) -> Optional[dict]:
    """
    Coefficient of variation of the Hansen accessibility index across
    departure-time windows -- this module's own descriptive-statistics
    composition of #1 (real per-window travel times) and #3 (Hansen
    index), not a separately cited formula (see module docstring point
    4). Requires accessibility to have been computed separately per
    window (a real cost multiplier -- see run()'s docstring on why this
    is opt-in, not automatic, for every anchor).
    """
    vals = [v for v in hansen_indices_by_window.values() if v is not None]
    if len(vals) < 2:
        return None
    arr = np.array(vals)
    mean = float(arr.mean())
    if mean == 0:
        return None
    cv = float(arr.std(ddof=1) / mean)
    return {
        "coefficient_of_variation": round(cv, 4),
        "min_window_index": round(float(arr.min()), 6),
        "max_window_index": round(float(arr.max()), 6),
        "n_windows": len(vals),
    }


# ============================================================================
# Composite score + evidence dict
# ============================================================================

def mobility_score_0_100(reliability: Optional[ReliabilityResult], accessibility: Optional[AccessibilityResult],
                          corpus_accessibility_reference: Optional[float] = None) -> Optional[float]:
    """
    Blends reliability (inverted -- lower TTI/Buffer Index is better)
    and accessibility (higher Hansen index is better, rescaled against
    corpus_accessibility_reference -- e.g. the fit corpus's own median
    Hansen index across catchments already computed this run/cached,
    since Hansen's index has no natural 0-100 bound on its own) into
    one 0-100 figure. This blend is this module's own presentation
    layer on top of the two sourced statistics (same "not claimed as an
    external formula" convention as competition.py's competition_score
    / market_gap.py's market_gap_score_0_100). Returns None if neither
    input is available.

    Weights: 55% reliability (30% TTI-based, 25% Buffer-Index-based,
    since a merely-slower trip and an UNRELIABLY-timed trip are
    different frictions for a customer), 45% accessibility.
    """
    if reliability is None and accessibility is None:
        return None

    reliability_component = None
    if reliability is not None:
        # TTI of 1.0 (no slowdown) -> 100; TTI of 2.0 (double free-flow) -> 0; clipped.
        tti_term = max(0.0, min(1.0, (2.0 - reliability.travel_time_index) / 1.0)) * 100.0
        if reliability.buffer_index_pct is not None:
            # Buffer Index of 0% -> 100; 100%+ extra padding needed -> 0.
            buffer_term = max(0.0, min(1.0, (100.0 - reliability.buffer_index_pct) / 100.0)) * 100.0
            reliability_component = 0.55 * tti_term + 0.45 * buffer_term
        else:
            reliability_component = tti_term

    accessibility_component = None
    if accessibility is not None:
        if corpus_accessibility_reference and corpus_accessibility_reference > 0:
            ratio = accessibility.hansen_index / corpus_accessibility_reference
            accessibility_component = 100.0 / (1.0 + math.exp(-(ratio - 1.0)))  # logistic centered on the reference
        else:
            # No corpus reference available -- report a coarse, clearly-flagged
            # log-scaled figure rather than a 0-100 claim with no real anchor point.
            accessibility_component = min(100.0, 20.0 * math.log1p(accessibility.hansen_index))

    if reliability_component is not None and accessibility_component is not None:
        return round(0.55 * reliability_component + 0.45 * accessibility_component, 2)
    return round(reliability_component if reliability_component is not None else accessibility_component, 2)


def mobility_evidence(reliability: Optional[ReliabilityResult], accessibility: Optional[AccessibilityResult],
                       volatility: Optional[dict]) -> dict:
    """Structured evidence dict, same convention as competition.py's
    competition_evidence -- every figure a human or engines.decision
    would need to see WHY the score came out the way it did."""
    return {
        "reliability": {
            "travel_time_index": reliability.travel_time_index if reliability else None,
            "planning_time_index": reliability.planning_time_index if reliability else None,
            "buffer_index_pct": reliability.buffer_index_pct if reliability else None,
            "tti_90pct_ci": (
                [reliability.tti_bootstrap_ci_low, reliability.tti_bootstrap_ci_high] if reliability else None
            ),
            "successful_samples": f"{reliability.successful_sample_count}/{reliability.attempted_sample_count}" if reliability else None,
            "per_window_tti": reliability.per_window if reliability else None,
            "warnings": reliability.warnings if reliability else [],
        } if reliability is not None else None,
        "accessibility": {
            "hansen_index": accessibility.hansen_index if accessibility else None,
            "anchor_count_used": accessibility.anchor_count_used if accessibility else None,
            "beta": accessibility.beta if accessibility else None,
            "top_anchors": accessibility.per_anchor[:5] if accessibility else None,
            "warnings": accessibility.warnings if accessibility else [],
        } if accessibility is not None else None,
        "temporal_accessibility_volatility": volatility,
        "methods": ["travel_time_index", "planning_time_index", "buffer_index", "hansen_accessibility_index", "bootstrap_ci"],
    }


# ============================================================================
# Road-network centrality hook -- documented, deliberately unimplemented
# (see module docstring: not a Google-API-backed capability at all).
# ============================================================================

class RoadNetworkCentralityProvider:
    """
    Hook for real street-network betweenness/closeness centrality
    (Freeman, L.C. 1977. "A Set of Measures of Centrality Based on
    Betweenness." Sociometry, 40(1), 35-41; or the Space Syntax
    tradition -- Hillier, B. and Hanson, J. 1984. The Social Logic of
    Space. Cambridge University Press) against an OSMnx-built road
    graph. Requires the same OSMnx dependency google_client.py's own
    fetch_isochrone() already documents as unwired -- deliberately left
    unimplemented here rather than approximated with Google data that
    doesn't actually measure network structure. A concrete
    implementation would build G = ox.graph_from_point(...), compute
    nx.betweenness_centrality on the edge/node nearest the subject, and
    return it here.
    """

    def centrality(self, latitude: float, longitude: float) -> Optional[float]:
        raise NotImplementedError(
            "Real street-network centrality needs an OSMnx road graph, which this codebase "
            "doesn't build yet (see google_client.fetch_isochrone's identical documented gap). "
            "Not implemented with Google data because Google does not publish a road-network-graph "
            "API this could be built from without fabricating a proxy."
        )


# ============================================================================
# Django-facing entry points
# ============================================================================

def _anchors_for_cell(cell: BusinessLocationCell) -> list:
    qs = AnchorFacility.objects.filter(location_cell=cell)
    return [
        AnchorObservation(
            id=str(a.id), anchor_type=a.anchor_type,
            latitude=float(a.latitude), longitude=float(a.longitude),
            size_value=float(a.size_value) if a.size_value is not None else None,
            size_unit=a.size_unit or "",
        )
        for a in qs
    ]


@dataclass
class MobilityFeatures:
    location_cell: BusinessLocationCell
    reliability: Optional[ReliabilityResult] = None
    accessibility: Optional[AccessibilityResult] = None
    temporal_volatility: Optional[dict] = None
    mobility_score: Optional[float] = None
    reference_anchor_id: Optional[str] = None
    source: str = "business_intel.engines.mobility_traffic"
    source_class: str = "internal_derived"
    warnings: list = field(default_factory=list)


def compute_mobility_traffic(
    cell: BusinessLocationCell,
    compute_temporal_volatility: bool = False,
    corpus_accessibility_reference: Optional[float] = None,
) -> MobilityFeatures:
    """
    Full Section-4 calculation for one BusinessLocationCell. This is a
    CELL-level (not catchment-level) engine -- travel time to a fixed
    destination doesn't change with catchment radius, per
    FeatureObservation.catchment's own documented "null for cell-level
    features" convention -- so, unlike Engine 2/3/9, this has no
    per-catchment loop.

    compute_temporal_volatility=True multiplies the anchor-side Routes
    API cost by len(REPRESENTATIVE_DEPARTURE_WINDOWS) (recomputing
    Hansen accessibility separately at each window's travel times
    instead of once) -- opt-in, not automatic, since that's a real,
    substantial jump in call volume even under this module's generally
    generous-with-cost stance; left off by default so a caller
    consciously opts into that spend rather than it firing on every run.
    """
    subject_lat, subject_lon = float(cell.center_latitude), float(cell.center_longitude)
    warnings = []

    all_anchors = _anchors_for_cell(cell)
    kept_anchors, dropped = prefilter_nearest_anchors(subject_lat, subject_lon, all_anchors)
    if not all_anchors:
        warnings.append("No AnchorFacility rows synced for this cell yet -- run anchor sync before this engine for a real accessibility figure.")

    reliability = None
    reference_anchor_id = None
    if kept_anchors:
        reference = kept_anchors[0]  # nearest anchor by straight-line distance -- documented default, see module docstring
        reference_anchor_id = reference.id
        free_flow = fetch_free_flow_duration(subject_lat, subject_lon, reference.latitude, reference.longitude)
        free_flow_s = _parse_duration_seconds(free_flow) if free_flow else None
        if free_flow_s is None:
            warnings.append("Free-flow Routes API baseline call failed; travel-time reliability cannot be computed this run.")
        else:
            samples = fetch_reliability_samples(subject_lat, subject_lon, reference.latitude, reference.longitude)
            reliability = compute_travel_time_reliability(free_flow_s, samples)
            if reliability is None:
                warnings.append(
                    f"Fewer than {int(MIN_SUCCESS_FRACTION*100)}% of departure windows returned a route to the "
                    "reference anchor; reliability withheld rather than built from a minority of samples."
                )
    else:
        warnings.append("No anchors within ANCHOR_PREFILTER_RADIUS_M -- no reference destination for a reliability profile.")

    # Fetch a SINGLE representative (weekday PM peak) travel time to
    # each kept anchor for the Hansen accessibility index -- a full
    # weekly profile per anchor would multiply cost by
    # len(REPRESENTATIVE_DEPARTURE_WINDOWS) for every anchor, which is
    # what compute_temporal_volatility's opt-in path is for instead.
    accessibility = None
    temporal_volatility = None
    if kept_anchors:
        peak_departure = _next_occurrence_rfc3339(*REPRESENTATIVE_DEPARTURE_WINDOWS[2][1:])  # mon_pm_peak window
        from ..google_client import fetch_traffic_aware_duration
        for a in kept_anchors:
            route = fetch_traffic_aware_duration(subject_lat, subject_lon, a.latitude, a.longitude, departure_time=peak_departure)
            a.travel_time_s = _parse_duration_seconds(route) if route else None
        accessibility = hansen_accessibility_index(subject_lat, subject_lon, kept_anchors)
        if accessibility is not None:
            accessibility.anchor_count_prefiltered_out = dropped
            if dropped:
                accessibility.warnings.append(f"{dropped} anchor(s) outside ANCHOR_PREFILTER_RADIUS_M were not queried (cost control, see module docstring).")
        else:
            warnings.append("No kept anchor returned a usable travel time; accessibility index withheld.")

        if compute_temporal_volatility and accessibility is not None:
            per_window_index = {}
            for label, iso_wd, hour, minute in REPRESENTATIVE_DEPARTURE_WINDOWS:
                dep = _next_occurrence_rfc3339(iso_wd, hour, minute)
                for a in kept_anchors:
                    route = fetch_traffic_aware_duration(subject_lat, subject_lon, a.latitude, a.longitude, departure_time=dep)
                    a.travel_time_s = _parse_duration_seconds(route) if route else None
                window_result = hansen_accessibility_index(subject_lat, subject_lon, kept_anchors)
                per_window_index[label] = window_result.hansen_index if window_result else None
            temporal_volatility = temporal_accessibility_volatility(per_window_index)

    score = mobility_score_0_100(reliability, accessibility, corpus_accessibility_reference)

    return MobilityFeatures(
        location_cell=cell, reliability=reliability, accessibility=accessibility,
        temporal_volatility=temporal_volatility, mobility_score=score,
        reference_anchor_id=reference_anchor_id, warnings=warnings,
    )


# ============================================================================
# Orchestration / EngineRun integration
# ============================================================================

def run(query: BusinessQuery, compute_temporal_volatility: bool = False) -> dict:
    """
    Engine 4 entry point. Computes mobility/traffic features for
    query.location_cell (cell-level, catchment=None -- see
    compute_mobility_traffic's docstring), writes FeatureObservation
    rows, and records an EngineRun -- same contract shape as every
    other engine in this codebase.
    """
    started_at = django_timezone.now()
    cell = query.location_cell

    run_row = EngineRun.objects.create(
        query=query, engine_number=ENGINE_NUMBER, status="succeeded", started_at=started_at,
    )

    try:
        with transaction.atomic():
            features = compute_mobility_traffic(cell, compute_temporal_volatility=compute_temporal_volatility)
            evidence = mobility_evidence(features.reliability, features.accessibility, features.temporal_volatility)

            features_written = 0
            if features.reliability is not None:
                for field_name, value in (
                    ("travel_time_index", features.reliability.travel_time_index),
                    ("planning_time_index", features.reliability.planning_time_index),
                    ("buffer_index_pct", features.reliability.buffer_index_pct),
                ):
                    if value is None:
                        continue
                    FeatureObservation.write_latest(
                        location_cell=cell, catchment=None, engine_number=ENGINE_NUMBER, feature_name=field_name,
                        defaults=dict(
                            value_type="numeric", value_numeric=Decimal(str(round(value, 6))),
                            unit="ratio" if "index" in field_name else "%",
                            observed_at=started_at, source=features.source, source_class=features.source_class,
                            method="travel_time_reliability_lomax_2003", derivation_level="calculation",
                            sample_size=features.reliability.successful_sample_count,
                        ),
                    )
                    features_written += 1

            if features.accessibility is not None:
                FeatureObservation.write_latest(
                    location_cell=cell, catchment=None, engine_number=ENGINE_NUMBER, feature_name="hansen_accessibility_index",
                    defaults=dict(
                        value_type="numeric", value_numeric=Decimal(str(round(features.accessibility.hansen_index, 6))),
                        unit="mass_per_second^beta", observed_at=started_at, source=features.source, source_class=features.source_class,
                        method="hansen_accessibility_1959", derivation_level="calculation",
                        sample_size=features.accessibility.anchor_count_used,
                    ),
                )
                features_written += 1

            if features.mobility_score is not None:
                FeatureObservation.write_latest(
                    location_cell=cell, catchment=None, engine_number=ENGINE_NUMBER, feature_name="mobility_score",
                    defaults=dict(
                        value_type="numeric", value_numeric=Decimal(str(features.mobility_score)),
                        unit="index_0_100", observed_at=started_at, source=features.source, source_class=features.source_class,
                        method="mobility_traffic_engine_blend", derivation_level="business_conclusion",
                    ),
                )
                features_written += 1

            FeatureObservation.write_latest(
                location_cell=cell, catchment=None, engine_number=ENGINE_NUMBER, feature_name="mobility_evidence",
                defaults=dict(
                    value_type="json", value_json=evidence, unit="",
                    observed_at=started_at, source=features.source, source_class=features.source_class,
                    method="mobility_traffic_engine", derivation_level="calculation",
                ),
            )
            features_written += 1

    except Exception as exc:
        logger.exception("Engine %s failed for query %s", ENGINE_NUMBER, query.id)
        run_row.status = "failed"
        run_row.error_message = str(exc)
        run_row.finished_at = django_timezone.now()
        run_row.save(update_fields=["status", "error_message", "finished_at"])
        raise

    finished_at = django_timezone.now()
    run_row.finished_at = finished_at
    run_row.duration_ms = int((finished_at - started_at).total_seconds() * 1000)
    run_row.features_written = features_written
    if features.warnings:
        run_row.status = "degraded"
        run_row.degraded_reason = "; ".join(sorted(set(features.warnings)))[:255]
    run_row.save(update_fields=["status", "finished_at", "duration_ms", "features_written", "degraded_reason"])

    return {"cell": cell, "features": features, "evidence": evidence, "engine_run": run_row}