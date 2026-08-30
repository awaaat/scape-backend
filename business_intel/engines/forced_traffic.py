"""
business_intel/engines/forced_traffic.py

Engine 5 -- Forced-Traffic / Captive Flow Engine
Per business_location_intelligence_engines_data_models_v2.md, Section 5.

NOTE ON THE SPEC FILE: same situation as every other engine in this
codebase -- business_location_intelligence_engines_data_models_v2.md is
referenced but not present on disk. Built to match Engine 2/3/4/8/9 for
structure, schema contract, and citation discipline. The stub this
replaced gave only a purpose line: "Identify places where movement is
concentrated because alternatives are limited."

=============================================================================
WHAT "FORCED TRAFFIC" ACTUALLY MEANS HERE (read this before extending)
=============================================================================
"Forced" / "captive" traffic is a real, well-studied idea across three
separate literatures, and this module builds on all three rather than
inventing a single ad hoc heuristic:

  1. Trip mandatoriness. Not all destinations pull traffic the same way.
     A hospital or school generates trips people cannot simply skip or
     defer; a mall or bar generates trips people can. Activity-based
     travel-demand research classifies trip purposes exactly this way
     (mandatory/maintenance/discretionary) -- see MANDATORY_TRIP_WEIGHT_
     BY_ANCHOR_TYPE below.

  2. Route redundancy. A destination reachable by only one practical
     road is a structural bottleneck: everyone going there funnels
     through the same corridor. A destination with several
     comparably-fast routes is not. Transportation-network-science calls
     this circuity / route redundancy, and it is directly measurable
     from real Google Routes API alternative-route responses (no
     network graph needed) -- see the route-redundancy section below.

  3. Pass-by capture. Some sites do not need to BE the destination to
     receive forced traffic -- they sit on the path between an actual
     origin and destination and catch trips that were already happening
     for another reason. Traffic engineers have a name and a whole
     methodology for exactly this: the "pass-by trip." The Institute of
     Transportation Engineers' Trip Generation Handbook defines pass-by
     trips and gives the standard estimation logic; this module's
     `waypoint_detour_ratio` implements the geographic core of that idea
     (does routing through this cell cost the traveler anything?) with
     real Routes API waypoint queries -- see the pass-by section below.

=============================================================================
WHAT'S ACTUALLY AVAILABLE VS. WHAT ISN'T
=============================================================================
Real, currently-public capabilities used here (same credentials as
Engine 4, google_client.py's GOOGLE_API_KEY):
  - Routes API `computeRoutes` with `computeAlternativeRoutes: true` --
    real alternative-route enumeration between two points. Not
    previously used anywhere in this codebase; the two new functions
    below (fetch_routes_with_alternatives, fetch_route_via_waypoint) are
    flagged, same as Engine 4's fetch_free_flow_duration, as ideally
    belonging in google_client.py alongside their siblings.
  - Routes API `intermediates` -- real waypoint-constrained routing,
    used for the pass-by detour calculation.
  - AnchorFacility rows (already synced by anchor.py / search_anchors)
    for anchor identity, type, location, and mass.

Deliberately NOT used, for the same reason Engine 4 documents:
  - No public "actual traffic volume" or "actual trip count" API exists
    from Google or anywhere else this codebase has credentials for.
    Nothing here claims to measure real vehicle counts -- every score
    is a structural PROXY (mandatoriness x redundancy x pass-by
    geometry), clearly labeled as such throughout.
  - No road-network graph (OSMnx) is available yet -- see Engine 4's
    RoadNetworkCentralityProvider for the identical, still-open gap.
    True betweenness centrality would strengthen the pass-by component
    below; the waypoint-detour approach here is a real, defensible,
    Google-API-only substitute, not presented as network centrality
    itself.

Per the explicit "don't limit yourself, fetch what you need" direction
this engine was commissioned under: MAX_ANCHORS_FOR_CAPTIVITY,
MAX_ANCHOR_PAIRS_FOR_PASSBY, and ROUTES_ALTERNATIVES_PER_ANCHOR below are
all set generously and exist as named module constants specifically so
they can be raised further, not as a hidden ceiling.

=============================================================================
REAL METHODS USED (researched and cited, not invented)
=============================================================================
1. Mandatory vs. maintenance vs. discretionary trip-purpose taxonomy
   Adler, T., Ben-Akiva, M. (1979). "A Theoretical and Empirical Model
   of Trip Chaining Behavior." Transportation Research Part B, 13(3),
   243-257.
   The foundational activity-based-travel-demand distinction between
   trips a traveler cannot easily forgo (work, school, medical) and
   trips that are schedulable or skippable (shopping, social,
   recreational). MANDATORY_TRIP_WEIGHT_BY_ANCHOR_TYPE assigns each
   AnchorFacility.anchor_type a weight on this spectrum -- an editorial
   classification calibrated to that taxonomy, not a measured constant
   (same "classification, labeled as such" convention as
   purchasing_power.py's CATEGORY_SPENDING_TIER and mobility_traffic.py's
   REPRESENTATIVE_DEPARTURE_WINDOWS).

2. Hansen gravity accessibility (Hansen 1959; Handy & Niemeier 1997) --
   REUSED, not reimplemented, from engines.mobility_traffic, weighted
   here by mandatory-trip weight instead of raw mass. Same formula
   family already shared by competition.py's Huff model and
   market_gap.py's gravity-Poisson; this is its third real use in this
   codebase, all citing the same root idea consistently.

3. Route redundancy / circuity
   Levinson, D., El-Geneidy, A. (2009). "The minimum circuity frontier
   and the journey to work." Journal of Transport Geography, 17(6),
   491-499.
   Scott, D.M., Novak, D.C., Aultman-Hall, L., Guo, Z. (2006). "Network
   Robustness Index: A new method for identifying critical links and
   evaluating the performance of transportation networks." Journal of
   Transport Geography, 14(3), 215-227.
   Levinson & El-Geneidy is the standard citation for network circuity
   (ratio of network distance to straight-line distance) and documents
   that route-time ratios across a road network are well approximated
   by a log-normal distribution -- the justification for the log-normal
   MLE fit in `fit_lognormal_detour_distribution` below. Scott et al.'s
   Network Robustness Index is the standard citation for measuring a
   link/node's importance by the cost increase when the best option is
   unavailable -- the same logic this module applies at anchor-corridor
   scale using real Routes API alternative-route responses instead of a
   full network-removal simulation (which would need OSMnx, not
   available here -- see the gap note above).

4. Pass-by trip capture
   Institute of Transportation Engineers (ITE). Trip Generation
   Handbook, 3rd Edition (2017). Washington, DC: ITE.
   Defines "pass-by trips" as trips already on the road network for
   another primary purpose that divert to a site with little or no
   extra travel cost, and gives the standard estimation approach:
   compare travel cost via the site against the traveler's original
   direct path. `waypoint_detour_ratio` below implements exactly that
   comparison with real Routes API `intermediates` queries between
   pairs of real AnchorFacility rows (an origin-type anchor and a
   destination-type anchor, chosen by mandatory-trip weight -- see
   ORIGIN_LIKE_ANCHOR_TYPES / DESTINATION_LIKE_ANCHOR_TYPES) routed
   through the subject cell.

5. Log-normal maximum-likelihood fit (closed-form)
   Aitchison, J., Brown, J.A.C. (1957). The Lognormal Distribution.
   Cambridge University Press.
   For a variable known/assumed log-normal (per Levinson & El-Geneidy,
   point 3), the MLE of (mu, sigma) is simply the mean and sample
   standard deviation of the log-transformed data -- a real, closed-form
   fitting procedure (not scipy-dependent, consistent with this
   codebase's numpy-only environment, same gap already flagged in
   population.py and market_gap.py). Used to convert a raw detour ratio
   into a calibrated percentile/z-score against the cell's OWN sampled
   anchor-corridor distribution, rather than comparing it to an
   arbitrary fixed threshold.

6. Bootstrap confidence intervals (Efron 1979) -- REUSED from
   engines.mobility_traffic._bootstrap_ci (same nonparametric resampling
   procedure, not reimplemented) to put an honest interval around the
   aggregate captivity score built from a handful of real anchor-corridor
   samples.

Both statistical layers (Hansen-with-mandatory-weighting, and the
route-redundancy / pass-by geometry) are built from genuinely
independent real Routes API measurements taken THIS run against this
query's actual synced AnchorFacility rows -- dynamic, syncs with your
state, same discipline as Engine 4.

=============================================================================
EDGE CASES THIS MODULE HANDLES EXPLICITLY
=============================================================================
- No AnchorFacility rows synced for this cell -> every component is
  None, not 0 (a data gap, not evidence of an open, unforced market).
- Fewer than 2 anchors -> no anchor PAIRS exist, so the pass-by
  component is withheld (flagged) even if the anchor-captivity
  component (which only needs 1 anchor) still runs.
- No anchors fall into ORIGIN_LIKE_ANCHOR_TYPES or
  DESTINATION_LIKE_ANCHOR_TYPES specifically -> pass-by pairing falls
  back to the top-N anchors by mandatory-weight*mass generally (flagged
  low_taxonomy_coverage), rather than silently returning nothing.
- Routes API returns a single route with computeAlternativeRoutes=true
  (no real alternative exists) -> treated as the STRONGEST possible
  redundancy-scarcity signal for that corridor (maximally captive), not
  as a failed call -- this is real information, not missing data.
- Routes API call itself fails (network/HTTP error) -> that
  anchor/pair sample is dropped and counted, not zeroed or treated as
  "no alternative" (a failed call is missing data; a successful call
  returning one route is a real captivity signal -- these are kept
  distinct throughout).
- Fewer than MIN_SAMPLES_FOR_LOGNORMAL_FIT successful detour-ratio
  samples -> the log-normal MLE fit and its derived z-score/percentile
  are skipped; the raw mean detour ratio is still reported, flagged as
  unfit (mirrors mobility_traffic's MIN_SAMPLES_FOR_PERCENTILES pattern
  exactly).
- AnchorFacility.size_value missing -> unweighted mass of 1.0 used, per
  the identical documented convention already established in
  mobility_traffic.hansen_accessibility_index and competition.py.
- Waypoint route legitimately fails to route (e.g. subject cell is not
  plausibly reachable between the two anchors, or one leg is
  unroutable) -> that pair is dropped from the pass-by sample and
  recorded as such, not scored as "no detour" (which would fabricate a
  perfect pass-by capture score from a routing failure).

Scope boundary
--------------
Depends on business_intel.models, business_intel.google_client, and
engines.mobility_traffic (reused for AnchorObservation, _anchors_for_cell,
prefilter_nearest_anchors, haversine_m, _bootstrap_ci, KENYA_TZ,
_next_occurrence_rfc3339 -- genuine shared utility/plumbing, not
Hansen-specific business logic, so importing rather than duplicating is
the right call here, unlike competition.py's haversine_m duplication for
independence). No dependency on the feature-store output of any other
engine (unlike market_gap.py) -- this engine's inputs are raw Google API
responses and raw AnchorFacility rows, like Engine 4.
"""

from __future__ import annotations

import itertools
import logging
import math
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional, Sequence

import numpy as np
import requests
from django.db import transaction
from django.utils import timezone as django_timezone

from ..google_client import GOOGLE_API_KEY, ROUTES_URL, DEFAULT_TIMEOUT_SECONDS, _log_call
from ..models import BusinessLocationCell, BusinessQuery, EngineRun, FeatureObservation
from .mobility_traffic import (
    AnchorObservation,
    _anchors_for_cell,
    prefilter_nearest_anchors,
    haversine_m,
    _bootstrap_ci,
    KENYA_TZ,
    _next_occurrence_rfc3339,
    REPRESENTATIVE_DEPARTURE_WINDOWS,
)

logger = logging.getLogger("business_intel.engines.forced_traffic")

ENGINE_NUMBER = 5

# ============================================================================
# Calibration inputs -- documented as editorial classification, not
# measured constants (see module docstring, point 1).
# ============================================================================

# Adler & Ben-Akiva (1979) mandatory/maintenance/discretionary spectrum,
# mapped onto this codebase's real ANCHOR_TYPE_CHOICES. 1.0 = as
# non-discretionary as this taxonomy gets (you cannot simply not go to
# the hospital); values trend down toward genuinely discretionary
# destinations. Anchor types not covering demand ORIGINS (e.g.
# residential_development) are still given a weight below, used only
# when they participate as an endpoint of a pass-by pair (see
# ORIGIN_LIKE_ANCHOR_TYPES).
MANDATORY_TRIP_WEIGHT_BY_ANCHOR_TYPE = {
    "hospital": 1.00,
    "clinic_anchor": 0.90,
    "school": 0.95,
    "primary_school": 0.95,
    "secondary_school": 0.95,
    "university": 0.80,
    "government_facility": 0.80,
    "transit_hub": 0.85,
    "major_employer": 0.90,
    "industrial_park": 0.80,
    "market": 0.55,           # maintenance-type: recurring but schedulable
    "shopping_mall": 0.35,    # discretionary
    "religious_community": 0.40,  # recurring, time-windowed, but voluntary
    "residential_development": 0.60,  # demand ORIGIN, not a pull destination
}
DEFAULT_MANDATORY_WEIGHT = 0.5  # unclassified anchor_type: honest neutral prior

# Which anchor types plausibly act as trip ORIGINS vs DESTINATIONS for
# pass-by pairing (point 4). Not mutually exclusive by design --
# transit_hub and major_employer are legitimately both, depending on
# time of day -- this taxonomy only determines which pairs get sampled
# first when anchors are abundant (a prioritization, not a hard filter;
# see the low_taxonomy_coverage fallback in the module docstring).
ORIGIN_LIKE_ANCHOR_TYPES = {"residential_development", "transit_hub"}
DESTINATION_LIKE_ANCHOR_TYPES = {
    "hospital", "clinic_anchor", "school", "primary_school", "secondary_school",
    "university", "major_employer", "industrial_park", "government_facility",
    "market", "shopping_mall",
}

# Cost/insight knobs. Per the explicit "fetch greedily" direction this
# engine was built under, these are set generously and are meant to be
# raised further, not treated as a hidden ceiling -- see module docstring.
MAX_ANCHORS_FOR_CAPTIVITY = 20
ANCHOR_PREFILTER_RADIUS_M = 8000.0
ROUTES_ALTERNATIVES_PER_ANCHOR_CAP = 20   # top-N anchors sampled for alternative routes
MAX_ANCHOR_PAIRS_FOR_PASSBY = 25          # top-N anchor PAIRS sampled for waypoint detour

MIN_SAMPLES_FOR_LOGNORMAL_FIT = 5
N_BOOTSTRAP_RESAMPLES = 2000

# Hansen accessibility distance-decay exponent -- same convention and
# same default as mobility_traffic.DEFAULT_ACCESSIBILITY_BETA (Handy &
# Niemeier 1997's literature range, 1-2 for local urban trips).
DEFAULT_ACCESSIBILITY_BETA = 1.5

# Peak departure window used for all real-time-sensitive Routes calls
# below (redundancy sampling, pass-by detour) -- Monday PM peak, same
# window mobility_traffic.py uses as its single-call representative
# peak (index [2] of REPRESENTATIVE_DEPARTURE_WINDOWS: "mon_pm_peak").
# Forced/captive-flow patterns are most policy-relevant exactly when
# traffic is heaviest -- an alternative that looks fine off-peak can
# disappear (or become genuinely worse) at peak, which is the scenario
# this engine exists to catch.
_PEAK_WINDOW = REPRESENTATIVE_DEPARTURE_WINDOWS[2]
assert _PEAK_WINDOW[0] == "mon_pm_peak"


# ============================================================================
# Google Routes API calls -- alternative-route enumeration + waypoint
# detour queries. Neither exists elsewhere in this codebase; flagged in
# the module docstring as ideally belonging in google_client.py
# alongside fetch_traffic_aware_duration / fetch_free_flow_duration.
# ============================================================================

def _parse_duration_seconds(route: dict) -> Optional[float]:
    raw = route.get("duration")
    if not raw:
        return None
    try:
        return float(str(raw).rstrip("s"))
    except (TypeError, ValueError):
        return None


def fetch_routes_with_alternatives(
    origin_lat: float, origin_lng: float, dest_lat: float, dest_lng: float,
    departure_time: Optional[str] = None,
) -> list:
    """
    Real Routes API call with computeAlternativeRoutes=true. Returns a
    list of {duration_s, distance_m} dicts, one per route Google
    returned, sorted ascending by duration (fastest first) -- empty list
    if the call failed or returned nothing routable.
    """
    body = {
        "origin": {"location": {"latLng": {"latitude": origin_lat, "longitude": origin_lng}}},
        "destination": {"location": {"latLng": {"latitude": dest_lat, "longitude": dest_lng}}},
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_AWARE",
        "computeAlternativeRoutes": True,
    }
    if departure_time:
        body["departureTime"] = departure_time
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
        logger.warning("Alternative-routes Routes API call failed: %s", exc)
        _log_call("routes", None, body, None, False)
        return []

    _log_call("routes", None, body, resp.status_code, succeeded)
    if not succeeded:
        return []

    parsed = []
    for route in data.get("routes", []):
        dur = _parse_duration_seconds(route)
        if dur is not None:
            parsed.append({"duration_s": dur, "distance_m": route.get("distanceMeters")})
    parsed.sort(key=lambda r: r["duration_s"])
    return parsed


def fetch_route_via_waypoint(
    origin_lat: float, origin_lng: float,
    waypoint_lat: float, waypoint_lng: float,
    dest_lat: float, dest_lng: float,
    departure_time: Optional[str] = None,
) -> Optional[float]:
    """
    Real Routes API call constrained through one intermediate waypoint
    (the subject cell). Returns total travel-time seconds for
    origin -> waypoint -> destination, or None if unroutable/failed.
    Used for the ITE pass-by detour comparison (point 4).
    """
    body = {
        "origin": {"location": {"latLng": {"latitude": origin_lat, "longitude": origin_lng}}},
        "destination": {"location": {"latLng": {"latitude": dest_lat, "longitude": dest_lng}}},
        "intermediates": [{"location": {"latLng": {"latitude": waypoint_lat, "longitude": waypoint_lng}}}],
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_AWARE",
    }
    if departure_time:
        body["departureTime"] = departure_time
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
        logger.warning("Waypoint-detour Routes API call failed: %s", exc)
        _log_call("routes", None, body, None, False)
        return None

    _log_call("routes", None, body, resp.status_code, succeeded)
    if not succeeded or not data.get("routes"):
        return None
    return _parse_duration_seconds(data["routes"][0])


def fetch_direct_route_duration(
    origin_lat: float, origin_lng: float, dest_lat: float, dest_lng: float,
    departure_time: Optional[str] = None,
) -> Optional[float]:
    """Single-route (no waypoint, no alternatives) traffic-aware duration,
    used as the denominator for the pass-by detour ratio. Reuses
    mobility_traffic.fetch_free_flow_duration's request shape but stays
    TRAFFIC_AWARE (pass-by detour is compared like-for-like at the SAME
    departure time as the waypoint route -- mixing a free-flow baseline
    with a traffic-aware waypoint route would conflate two different
    effects: congestion and detour distance)."""
    body = {
        "origin": {"location": {"latLng": {"latitude": origin_lat, "longitude": origin_lng}}},
        "destination": {"location": {"latLng": {"latitude": dest_lat, "longitude": dest_lng}}},
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_AWARE",
    }
    if departure_time:
        body["departureTime"] = departure_time
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
        logger.warning("Direct-route Routes API call failed: %s", exc)
        _log_call("routes", None, body, None, False)
        return None

    _log_call("routes", None, body, resp.status_code, succeeded)
    if not succeeded or not data.get("routes"):
        return None
    return _parse_duration_seconds(data["routes"][0])


# ============================================================================
# Pure-math layer 1: mandatory-weighted Hansen accessibility (anchor
# captivity). No network calls below this point.
# ============================================================================

def mandatory_weight(anchor_type: str) -> float:
    return MANDATORY_TRIP_WEIGHT_BY_ANCHOR_TYPE.get(anchor_type, DEFAULT_MANDATORY_WEIGHT)


@dataclass
class AnchorCaptivityResult:
    weighted_hansen_index: float
    anchor_count_used: int
    mean_mandatory_weight: float
    per_anchor: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


def anchor_captivity_index(
    anchors: Sequence[AnchorObservation],
    beta: float = DEFAULT_ACCESSIBILITY_BETA,
    min_travel_time_s: float = 60.0,
) -> Optional[AnchorCaptivityResult]:
    """
    A_i = sum_j( w_j * S_j / travel_time_ij ** beta ), Hansen (1959) with
    Handy & Niemeier's (1997) travel-time impedance, EXTENDED here with
    w_j = mandatory_weight(anchor_type) -- point 1/2 of the module
    docstring combined. Anchors must already have travel_time_s
    populated. Pure math over already-fetched data -- same
    separation-of-concerns as mobility_traffic.hansen_accessibility_index,
    which this deliberately does not call directly (that function's
    formula excludes the mandatory-weight term; duplicating the ~10-line
    loop here with the extra term is clearer than parameterizing that
    function for a term specific to this engine).
    """
    usable = [a for a in anchors if a.travel_time_s is not None and a.travel_time_s > 0]
    if not usable:
        return None

    warnings = []
    per_anchor = []
    total = 0.0
    unweighted_count = 0
    weights_used = []
    for a in usable:
        mass = float(a.size_value) if a.size_value is not None else 1.0
        if a.size_value is None:
            unweighted_count += 1
        w = mandatory_weight(a.anchor_type)
        weights_used.append(w)
        t = max(a.travel_time_s, min_travel_time_s)
        contribution = w * mass / (t ** beta)
        total += contribution
        per_anchor.append({
            "id": a.id, "anchor_type": a.anchor_type, "mandatory_weight": w,
            "mass": mass, "mass_is_unweighted_default": a.size_value is None,
            "travel_time_s": a.travel_time_s, "contribution": round(contribution, 6),
        })

    if unweighted_count:
        warnings.append(
            f"{unweighted_count} of {len(usable)} anchors have no AnchorFacility.size_value on record; "
            "used an unweighted mass of 1.0 for those (real gap, not a fabricated size)."
        )

    per_anchor.sort(key=lambda d: -d["contribution"])
    return AnchorCaptivityResult(
        weighted_hansen_index=round(total, 6),
        anchor_count_used=len(usable),
        mean_mandatory_weight=round(float(np.mean(weights_used)), 4),
        per_anchor=per_anchor,
        warnings=warnings,
    )


# ============================================================================
# Pure-math layer 2: route redundancy / circuity (Levinson & El-Geneidy
# 2009; Scott et al. 2006), with a real log-normal MLE fit (Aitchison &
# Brown 1957).
# ============================================================================

@dataclass
class RedundancySample:
    anchor_id: str
    anchor_type: str
    mandatory_weight: float
    alt_count: int                 # number of distinct routes Google returned (>=1 if any)
    best_duration_s: Optional[float]
    detour_ratio: Optional[float]  # second_best / best; None if alt_count < 2
    no_alternative_found: bool     # True if alt_count == 1 (strongest scarcity signal)


def sample_route_redundancy(
    subject_lat: float, subject_lon: float,
    anchors: Sequence[AnchorObservation],
    departure_time: str,
) -> list:
    """
    Fires one real Routes API call per anchor with
    computeAlternativeRoutes=true (point 3). Anchors here should already
    be the priority-ranked, capped subset the caller wants to spend real
    calls on (see run()'s use of ROUTES_ALTERNATIVES_PER_ANCHOR_CAP).
    """
    samples = []
    for a in anchors:
        routes = fetch_routes_with_alternatives(subject_lat, subject_lon, a.latitude, a.longitude, departure_time)
        if not routes:
            # Call failed or nothing routable at all -- missing data, not
            # a captivity signal (see module docstring edge cases).
            continue
        alt_count = len(routes)
        best = routes[0]["duration_s"]
        detour_ratio = (routes[1]["duration_s"] / best) if alt_count >= 2 and best > 0 else None
        samples.append(RedundancySample(
            anchor_id=a.id, anchor_type=a.anchor_type, mandatory_weight=mandatory_weight(a.anchor_type),
            alt_count=alt_count, best_duration_s=best, detour_ratio=detour_ratio,
            no_alternative_found=(alt_count == 1),
        ))
    return samples


@dataclass
class LognormalFit:
    mu: float
    sigma: float
    n: int


def fit_lognormal_detour_distribution(detour_ratios: Sequence[float]) -> Optional[LognormalFit]:
    """
    Closed-form MLE of a log-normal distribution (Aitchison & Brown
    1957): mu_hat = mean(log(x)), sigma_hat = sample std of log(x).
    Justified by Levinson & El-Geneidy (2009), who document that
    network route-time/circuity ratios are well approximated by a
    log-normal distribution. Requires detour_ratios > 0 (true by
    construction -- ratios of positive durations) and
    MIN_SAMPLES_FOR_LOGNORMAL_FIT points; below that the fit is not
    reported (see compute_forced_traffic's handling).
    """
    vals = np.array([d for d in detour_ratios if d and d > 0], dtype=float)
    if len(vals) < MIN_SAMPLES_FOR_LOGNORMAL_FIT:
        return None
    log_vals = np.log(vals)
    mu_hat = float(log_vals.mean())
    sigma_hat = float(log_vals.std(ddof=1))
    if sigma_hat <= 0:
        return None
    return LognormalFit(mu=round(mu_hat, 6), sigma=round(sigma_hat, 6), n=len(vals))


def _standard_normal_cdf(z: float) -> float:
    """Phi(z) via the error function -- no scipy in this environment
    (same numpy-only constraint documented in population.py and
    market_gap.py)."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def lognormal_percentile(fit: LognormalFit, value: float) -> Optional[float]:
    """Percentile (0-100) of `value` under the fitted log-normal --
    i.e. how unusual this cell's OWN detour ratio is relative to its
    OWN sampled anchor-corridor distribution (a within-cell reference,
    since no cross-cell corpus is assumed available -- see run()'s
    corpus_redundancy_reference for the opt-in cross-cell version)."""
    if value is None or value <= 0:
        return None
    z = (math.log(value) - fit.mu) / fit.sigma
    return round(_standard_normal_cdf(z) * 100.0, 2)


@dataclass
class RedundancyResult:
    sample_count: int
    no_alternative_count: int
    mean_detour_ratio: Optional[float]
    detour_ratio_ci_low: Optional[float]
    detour_ratio_ci_high: Optional[float]
    lognormal_fit: Optional[LognormalFit]
    scarcity_score_0_100: float   # this module's own 0-100 conversion, see docstring
    per_anchor: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


def summarize_route_redundancy(samples: Sequence[RedundancySample]) -> Optional[RedundancyResult]:
    """
    Converts raw per-anchor RedundancySamples into one weighted
    redundancy-scarcity picture. `no_alternative_found` anchors (no real
    second route at all) are the strongest possible captivity evidence
    and are folded into the score at the maximum severity rather than
    excluded as missing data -- see module docstring edge cases.
    scarcity_score_0_100 = 100 * (1 - 1/detour_ratio) for anchors with a
    real alternative (detour_ratio=1 -> 0, i.e. genuinely redundant;
    detour_ratio->inf -> 100), and a flat 100 for anchors with no
    alternative at all. This 0-100 conversion is this module's own
    presentation layer on the real detour_ratio statistic (same
    "not claimed as an external formula" convention as
    mobility_traffic.mobility_score_0_100 / competition.py's
    competition_score), weighted by each anchor's mandatory_weight so a
    captive-but-discretionary destination (e.g. a mall on a single
    access road) doesn't dominate a captive-and-mandatory one (e.g. the
    only hospital access road) equally.
    """
    if not samples:
        return None

    warnings = []
    detour_ratios = [s.detour_ratio for s in samples if s.detour_ratio is not None]
    no_alt_count = sum(1 for s in samples if s.no_alternative_found)

    per_anchor_scores = []
    weight_sum = 0.0
    weighted_score_sum = 0.0
    for s in samples:
        if s.no_alternative_found:
            score = 100.0
        elif s.detour_ratio is not None:
            score = max(0.0, min(100.0, 100.0 * (1.0 - 1.0 / s.detour_ratio)))
        else:
            continue
        w = max(s.mandatory_weight, 0.05)  # floor so a zero-weight anchor doesn't erase its own signal
        weighted_score_sum += w * score
        weight_sum += w
        per_anchor_scores.append({
            "anchor_id": s.anchor_id, "anchor_type": s.anchor_type,
            "mandatory_weight": s.mandatory_weight, "alt_count": s.alt_count,
            "detour_ratio": round(s.detour_ratio, 4) if s.detour_ratio is not None else None,
            "no_alternative_found": s.no_alternative_found, "scarcity_score": round(score, 2),
        })

    if weight_sum == 0:
        return None

    scarcity_score = weighted_score_sum / weight_sum

    fit = fit_lognormal_detour_distribution(detour_ratios)
    ci_lo = ci_hi = mean_ratio = None
    if detour_ratios:
        mean_ratio = round(float(np.mean(detour_ratios)), 4)
        if len(detour_ratios) >= MIN_SAMPLES_FOR_LOGNORMAL_FIT:
            arr = np.array(detour_ratios)
            ci_lo, ci_hi = _bootstrap_ci(arr, lambda d: float(d.mean()), n_resamples=N_BOOTSTRAP_RESAMPLES, seed=11)
            ci_lo, ci_hi = round(ci_lo, 4), round(ci_hi, 4)
        else:
            warnings.append(
                f"Only {len(detour_ratios)} anchor(s) had a real second route (< {MIN_SAMPLES_FOR_LOGNORMAL_FIT}); "
                "log-normal fit and bootstrap CI on detour ratio skipped -- raw mean still reported."
            )
    if no_alt_count:
        warnings.append(
            f"{no_alt_count} of {len(samples)} anchor(s) had NO alternative route returned by Routes API at all "
            "-- treated as maximal captivity for those anchors, not as missing data."
        )

    per_anchor_scores.sort(key=lambda d: -d["scarcity_score"])
    return RedundancyResult(
        sample_count=len(samples), no_alternative_count=no_alt_count,
        mean_detour_ratio=mean_ratio, detour_ratio_ci_low=ci_lo, detour_ratio_ci_high=ci_hi,
        lognormal_fit=fit, scarcity_score_0_100=round(scarcity_score, 2),
        per_anchor=per_anchor_scores, warnings=warnings,
    )


# ============================================================================
# Pure-math layer 3: pass-by capture via waypoint detour (ITE Trip
# Generation Handbook, 3rd Ed. 2017).
# ============================================================================

@dataclass
class PassBySample:
    origin_anchor_id: str
    dest_anchor_id: str
    origin_type: str
    dest_type: str
    pair_weight: float             # combined mandatory-weight*mass priority used to select this pair
    direct_duration_s: Optional[float]
    via_cell_duration_s: Optional[float]
    detour_ratio: Optional[float]  # via_cell / direct; 1.0 = zero-cost detour (perfect pass-by capture)


def select_passby_pairs(
    anchors: Sequence[AnchorObservation], max_pairs: int = MAX_ANCHOR_PAIRS_FOR_PASSBY,
) -> tuple:
    """
    Builds candidate (origin_anchor, destination_anchor) pairs for the
    pass-by detour test, prioritized by ORIGIN_LIKE_ANCHOR_TYPES x
    DESTINATION_LIKE_ANCHOR_TYPES combinations (point 4's real
    origin/destination framing) weighted by mandatory_weight * mass,
    highest-priority first, capped at max_pairs. Falls back to
    highest-weight anchors generally (all pairwise combinations, same
    priority ordering) if the anchor set doesn't have enough
    taxonomy-typed origins/destinations to fill max_pairs -- flagged
    low_taxonomy_coverage so the caller knows the fallback fired.
    """
    def pair_weight(a, b):
        mass_a = float(a.size_value) if a.size_value is not None else 1.0
        mass_b = float(b.size_value) if b.size_value is not None else 1.0
        return mandatory_weight(a.anchor_type) * mass_a * mandatory_weight(b.anchor_type) * mass_b

    origins = [a for a in anchors if a.anchor_type in ORIGIN_LIKE_ANCHOR_TYPES]
    destinations = [a for a in anchors if a.anchor_type in DESTINATION_LIKE_ANCHOR_TYPES]

    taxonomy_pairs = []
    for o, d in itertools.product(origins, destinations):
        if o.id == d.id:
            continue
        taxonomy_pairs.append((o, d, pair_weight(o, d)))
    taxonomy_pairs.sort(key=lambda t: -t[2])

    # low_taxonomy_coverage only means "no real origin/destination-typed
    # pairs existed at all" -- NOT "fewer than max_pairs exist", since
    # max_pairs is a generous cost cap, not an expected anchor count.
    # Filling remaining slots (up to the cap) with the next-best generic
    # pairs is normal, expected behavior for a small/typical anchor set
    # and should not itself be flagged as a data-quality gap.
    low_taxonomy_coverage = len(taxonomy_pairs) == 0
    pairs = taxonomy_pairs[:max_pairs]
    if len(pairs) < max_pairs:
        seen = {(p[0].id, p[1].id) for p in pairs}
        all_pairs = []
        for a, b in itertools.combinations(anchors, 2):
            if (a.id, b.id) in seen or (b.id, a.id) in seen:
                continue
            all_pairs.append((a, b, pair_weight(a, b)))
        all_pairs.sort(key=lambda t: -t[2])
        pairs.extend(all_pairs[: max_pairs - len(pairs)])

    return pairs, low_taxonomy_coverage


def sample_passby_detour(
    subject_lat: float, subject_lon: float,
    pairs: Sequence[tuple], departure_time: str,
) -> list:
    """Fires two real Routes API calls per candidate pair: the direct
    origin->destination route, and the origin->subject_cell->destination
    route (point 4). Both at the SAME departure_time so the comparison
    isolates detour distance, not traffic-timing differences."""
    samples = []
    for origin, dest, w in pairs:
        direct = fetch_direct_route_duration(origin.latitude, origin.longitude, dest.latitude, dest.longitude, departure_time)
        if direct is None:
            continue
        via_cell = fetch_route_via_waypoint(
            origin.latitude, origin.longitude, subject_lat, subject_lon,
            dest.latitude, dest.longitude, departure_time,
        )
        if via_cell is None:
            continue
        ratio = (via_cell / direct) if direct > 0 else None
        samples.append(PassBySample(
            origin_anchor_id=origin.id, dest_anchor_id=dest.id,
            origin_type=origin.anchor_type, dest_type=dest.anchor_type, pair_weight=w,
            direct_duration_s=direct, via_cell_duration_s=via_cell, detour_ratio=ratio,
        ))
    return samples


@dataclass
class PassByResult:
    sample_count: int
    mean_detour_ratio: Optional[float]
    passby_capture_score_0_100: float
    per_pair: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


def summarize_passby_capture(samples: Sequence[PassBySample]) -> Optional[PassByResult]:
    """
    Converts raw PassBySamples into a weighted pass-by capture score.
    passby_capture_score_0_100 = 100 / detour_ratio, clipped to [0,100]
    -- a detour_ratio of 1.0 (routing through the subject cell costs
    NOTHING extra) scores 100 (maximum pass-by capture potential, per
    ITE's own "little or no extra travel cost" definition of a pass-by
    site); a detour_ratio of 2.0 (routing through doubles travel time)
    scores 50; large detours approach 0. Own presentation layer on the
    real detour_ratio statistic, same convention as the redundancy
    component above.
    """
    usable = [s for s in samples if s.detour_ratio is not None and s.detour_ratio > 0]
    if not usable:
        return None

    weight_sum = sum(s.pair_weight for s in usable) or len(usable)
    weighted_score_sum = 0.0
    per_pair = []
    for s in usable:
        score = max(0.0, min(100.0, 100.0 / s.detour_ratio))
        w = s.pair_weight if s.pair_weight > 0 else 1.0
        weighted_score_sum += w * score
        per_pair.append({
            "origin_anchor_id": s.origin_anchor_id, "dest_anchor_id": s.dest_anchor_id,
            "origin_type": s.origin_type, "dest_type": s.dest_type,
            "direct_duration_s": round(s.direct_duration_s, 1),
            "via_cell_duration_s": round(s.via_cell_duration_s, 1),
            "detour_ratio": round(s.detour_ratio, 4), "passby_score": round(score, 2),
        })

    per_pair.sort(key=lambda d: -d["passby_score"])
    ratios = [s.detour_ratio for s in usable]
    warnings = []
    if len(samples) > len(usable):
        warnings.append(f"{len(samples) - len(usable)} of {len(samples)} candidate pair(s) failed to route and were dropped.")

    return PassByResult(
        sample_count=len(usable), mean_detour_ratio=round(float(np.mean(ratios)), 4),
        passby_capture_score_0_100=round(weighted_score_sum / weight_sum, 2),
        per_pair=per_pair[:10], warnings=warnings,
    )


# ============================================================================
# Composite score + evidence dict
# ============================================================================

def forced_traffic_score_0_100(
    captivity: Optional[AnchorCaptivityResult],
    redundancy: Optional[RedundancyResult],
    passby: Optional[PassByResult],
    corpus_captivity_reference: Optional[float] = None,
) -> Optional[float]:
    """
    Blends the three real components into one 0-100 figure. Weights:
    40% anchor captivity (mandatory-weighted Hansen index, rescaled
    against corpus_captivity_reference the same logistic/log1p-fallback
    way mobility_traffic.mobility_score_0_100 rescales its accessibility
    component -- Hansen-family indices have no natural 0-100 bound),
    35% route-redundancy scarcity, 25% pass-by capture. This blend is
    this module's own composition of three independently-sourced
    statistics, not an external formula (same disclosure convention as
    every composite score elsewhere in this codebase). Any missing
    component is dropped and the remaining weights renormalized, rather
    than treated as 0 (missing is not the same as "not captive").
    """
    components = []  # (weight, value)

    if captivity is not None:
        if corpus_captivity_reference and corpus_captivity_reference > 0:
            ratio = captivity.weighted_hansen_index / corpus_captivity_reference
            captivity_component = 100.0 / (1.0 + math.exp(-(ratio - 1.0)))
        else:
            captivity_component = min(100.0, 20.0 * math.log1p(captivity.weighted_hansen_index))
        components.append((0.40, captivity_component))

    if redundancy is not None:
        components.append((0.35, redundancy.scarcity_score_0_100))

    if passby is not None:
        components.append((0.25, passby.passby_capture_score_0_100))

    if not components:
        return None

    weight_sum = sum(w for w, _ in components)
    return round(sum(w * v for w, v in components) / weight_sum, 2)


def forced_traffic_evidence(
    captivity: Optional[AnchorCaptivityResult],
    redundancy: Optional[RedundancyResult],
    passby: Optional[PassByResult],
) -> dict:
    """Structured evidence dict, same convention as competition.py's
    competition_evidence / mobility_traffic.mobility_evidence."""
    return {
        "anchor_captivity": {
            "weighted_hansen_index": captivity.weighted_hansen_index if captivity else None,
            "anchor_count_used": captivity.anchor_count_used if captivity else None,
            "mean_mandatory_weight": captivity.mean_mandatory_weight if captivity else None,
            "top_anchors": captivity.per_anchor[:5] if captivity else None,
            "warnings": captivity.warnings if captivity else [],
        } if captivity is not None else None,
        "route_redundancy": {
            "sample_count": redundancy.sample_count if redundancy else None,
            "no_alternative_count": redundancy.no_alternative_count if redundancy else None,
            "mean_detour_ratio": redundancy.mean_detour_ratio if redundancy else None,
            "detour_ratio_90pct_ci": (
                [redundancy.detour_ratio_ci_low, redundancy.detour_ratio_ci_high] if redundancy else None
            ),
            "lognormal_fit": (
                {"mu": redundancy.lognormal_fit.mu, "sigma": redundancy.lognormal_fit.sigma, "n": redundancy.lognormal_fit.n}
                if redundancy and redundancy.lognormal_fit else None
            ),
            "scarcity_score_0_100": redundancy.scarcity_score_0_100 if redundancy else None,
            "most_captive_anchors": redundancy.per_anchor[:5] if redundancy else None,
            "warnings": redundancy.warnings if redundancy else [],
        } if redundancy is not None else None,
        "passby_capture": {
            "sample_count": passby.sample_count if passby else None,
            "mean_detour_ratio": passby.mean_detour_ratio if passby else None,
            "passby_capture_score_0_100": passby.passby_capture_score_0_100 if passby else None,
            "top_pairs": passby.per_pair[:5] if passby else None,
            "warnings": passby.warnings if passby else [],
        } if passby is not None else None,
        "methods": [
            "mandatory_trip_weighted_hansen_index", "route_alternative_scarcity",
            "lognormal_mle_detour_fit", "ite_passby_waypoint_detour", "bootstrap_ci",
        ],
    }


# ============================================================================
# Django-facing entry points
# ============================================================================

@dataclass
class ForcedTrafficFeatures:
    location_cell: BusinessLocationCell
    captivity: Optional[AnchorCaptivityResult] = None
    redundancy: Optional[RedundancyResult] = None
    passby: Optional[PassByResult] = None
    forced_traffic_score: Optional[float] = None
    source: str = "business_intel.engines.forced_traffic"
    source_class: str = "internal_derived"
    warnings: list = field(default_factory=list)


def compute_forced_traffic(
    cell: BusinessLocationCell,
    corpus_captivity_reference: Optional[float] = None,
) -> ForcedTrafficFeatures:
    """
    Full Section-5 calculation for one BusinessLocationCell. Cell-level
    (not catchment-level), same reasoning as Engine 4:
    origin/destination travel geometry doesn't change with catchment
    radius.
    """
    subject_lat, subject_lon = float(cell.center_latitude), float(cell.center_longitude)
    warnings = []

    all_anchors = _anchors_for_cell(cell)
    if not all_anchors:
        warnings.append("No AnchorFacility rows synced for this cell yet -- run anchor sync before this engine for a real captivity figure.")
        return ForcedTrafficFeatures(location_cell=cell, warnings=warnings)

    kept_anchors, dropped = prefilter_nearest_anchors(
        subject_lat, subject_lon, all_anchors,
        max_anchors=MAX_ANCHORS_FOR_CAPTIVITY, radius_m=ANCHOR_PREFILTER_RADIUS_M,
    )
    if not kept_anchors:
        warnings.append("No anchors within ANCHOR_PREFILTER_RADIUS_M -- no captivity figure possible.")
        return ForcedTrafficFeatures(location_cell=cell, warnings=warnings)

    peak_departure = _next_occurrence_rfc3339(*_PEAK_WINDOW[1:])

    # --- Component 1: anchor captivity (needs real travel times, peak window) ---
    from ..google_client import fetch_traffic_aware_duration
    for a in kept_anchors:
        route = fetch_traffic_aware_duration(subject_lat, subject_lon, a.latitude, a.longitude, departure_time=peak_departure)
        a.travel_time_s = _parse_duration_seconds(route) if route else None
    captivity = anchor_captivity_index(kept_anchors)
    if captivity is None:
        warnings.append("No kept anchor returned a usable travel time; anchor-captivity index withheld.")

    # --- Component 2: route redundancy (priority subset by mandatory weight * mass) ---
    def _priority(a: AnchorObservation) -> float:
        mass = float(a.size_value) if a.size_value is not None else 1.0
        return mandatory_weight(a.anchor_type) * mass

    priority_anchors = sorted(kept_anchors, key=_priority, reverse=True)[:ROUTES_ALTERNATIVES_PER_ANCHOR_CAP]
    redundancy_samples = sample_route_redundancy(subject_lat, subject_lon, priority_anchors, peak_departure)
    redundancy = summarize_route_redundancy(redundancy_samples)
    if redundancy is None:
        warnings.append("No anchor produced a usable alternative-route sample; route-redundancy component withheld.")

    # --- Component 3: pass-by capture (needs >= 2 anchors to form a pair) ---
    passby = None
    if len(kept_anchors) >= 2:
        pairs, low_taxonomy_coverage = select_passby_pairs(kept_anchors, max_pairs=MAX_ANCHOR_PAIRS_FOR_PASSBY)
        if low_taxonomy_coverage:
            warnings.append(
                "Anchor set has few/no anchors typed as clear origins or destinations for pass-by pairing "
                "(see ORIGIN_LIKE_ANCHOR_TYPES/DESTINATION_LIKE_ANCHOR_TYPES) -- fell back to highest-weighted "
                "generic anchor pairs."
            )
        passby_samples = sample_passby_detour(subject_lat, subject_lon, pairs, peak_departure)
        passby = summarize_passby_capture(passby_samples)
        if passby is None:
            warnings.append("No candidate anchor pair produced a usable waypoint-detour sample; pass-by component withheld.")
    else:
        warnings.append(f"Only {len(kept_anchors)} anchor(s) within range -- fewer than 2, so no pass-by pair exists; component withheld.")

    if dropped:
        warnings.append(f"{dropped} anchor(s) outside ANCHOR_PREFILTER_RADIUS_M were not queried (cost control, see module docstring).")

    score = forced_traffic_score_0_100(captivity, redundancy, passby, corpus_captivity_reference)

    return ForcedTrafficFeatures(
        location_cell=cell, captivity=captivity, redundancy=redundancy, passby=passby,
        forced_traffic_score=score, warnings=warnings,
    )


def run(query: BusinessQuery, corpus_captivity_reference: Optional[float] = None) -> dict:
    """
    Engine 5 entry point. Computes forced-traffic/captive-flow features
    for query.location_cell (cell-level, catchment=None), writes
    FeatureObservation rows, and records an EngineRun -- same contract
    shape as every other engine in this codebase.
    """
    started_at = django_timezone.now()
    cell = query.location_cell

    run_row = EngineRun.objects.create(
        query=query, engine_number=ENGINE_NUMBER, status="succeeded", started_at=started_at,
    )

    try:
        with transaction.atomic():
            features = compute_forced_traffic(cell, corpus_captivity_reference=corpus_captivity_reference)
            evidence = forced_traffic_evidence(features.captivity, features.redundancy, features.passby)

            features_written = 0

            if features.captivity is not None:
                FeatureObservation.write_latest(
                    location_cell=cell, catchment=None, engine_number=ENGINE_NUMBER,
                    feature_name="mandatory_weighted_hansen_index",
                    defaults=dict(
                        value_type="numeric", value_numeric=Decimal(str(round(features.captivity.weighted_hansen_index, 6))),
                        unit="mass_per_second^beta", observed_at=started_at, source=features.source, source_class=features.source_class,
                        method="hansen_1959_mandatory_weighted", derivation_level="calculation",
                        sample_size=features.captivity.anchor_count_used,
                    ),
                )
                features_written += 1

            if features.redundancy is not None:
                FeatureObservation.write_latest(
                    location_cell=cell, catchment=None, engine_number=ENGINE_NUMBER,
                    feature_name="route_redundancy_scarcity_score",
                    defaults=dict(
                        value_type="numeric", value_numeric=Decimal(str(features.redundancy.scarcity_score_0_100)),
                        unit="index_0_100", observed_at=started_at, source=features.source, source_class=features.source_class,
                        method="route_alternative_scarcity_lognormal_fit", derivation_level="calculation",
                        sample_size=features.redundancy.sample_count,
                    ),
                )
                features_written += 1

            if features.passby is not None:
                FeatureObservation.write_latest(
                    location_cell=cell, catchment=None, engine_number=ENGINE_NUMBER,
                    feature_name="passby_capture_score",
                    defaults=dict(
                        value_type="numeric", value_numeric=Decimal(str(features.passby.passby_capture_score_0_100)),
                        unit="index_0_100", observed_at=started_at, source=features.source, source_class=features.source_class,
                        method="ite_passby_waypoint_detour", derivation_level="calculation",
                        sample_size=features.passby.sample_count,
                    ),
                )
                features_written += 1

            if features.forced_traffic_score is not None:
                FeatureObservation.write_latest(
                    location_cell=cell, catchment=None, engine_number=ENGINE_NUMBER,
                    feature_name="forced_traffic_score",
                    defaults=dict(
                        value_type="numeric", value_numeric=Decimal(str(features.forced_traffic_score)),
                        unit="index_0_100", observed_at=started_at, source=features.source, source_class=features.source_class,
                        method="forced_traffic_engine_blend", derivation_level="business_conclusion",
                    ),
                )
                features_written += 1

            FeatureObservation.write_latest(
                location_cell=cell, catchment=None, engine_number=ENGINE_NUMBER, feature_name="forced_traffic_evidence",
                defaults=dict(
                    value_type="json", value_json=evidence, unit="",
                    observed_at=started_at, source=features.source, source_class=features.source_class,
                    method="forced_traffic_engine", derivation_level="calculation",
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