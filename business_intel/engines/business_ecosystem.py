"""
business_intel/engines/business_ecosystem.py

Engine 6 -- Business Ecosystem Engine
Per business_location_intelligence_engines_data_models_v2.md, Section 6.

NOTE ON THE SPEC FILE: same situation as every other engine in this
codebase -- business_location_intelligence_engines_data_models_v2.md is
referenced but not present on disk. Built to match Engine 2/3/4/5/8/9
for structure, schema contract, and citation discipline. The stub this
replaced gave only a purpose line: "Create a spatial and temporal
representation of the business environment."

=============================================================================
WHAT THIS ENGINE ACTUALLY MEASURES (read this before extending)
=============================================================================
"Business ecosystem" is treated here as three real, separately-cited
questions, not one invented score:

  1. COMPOSITION -- what MIX of business categories exists in this
     catchment, and how balanced is that mix? Borrowed directly from
     community ecology's species-diversity toolkit (Shannon entropy,
     Simpson's index, Pielou evenness), which is exactly the toolkit
     urban economists reach for when formalizing Jane Jacobs' claim
     that categorical DIVERSITY (not just density) drives urban vitality
     -- see Quigley (1998), a direct citation for this exact
     borrowing.

  2. SPATIAL PATTERN -- are businesses in this catchment CLUSTERED
     together (agglomeration) or DISPERSED, and is that pattern
     statistically distinguishable from a random spatial arrangement?
     Clark & Evans' (1954) nearest-neighbor index is the classic,
     directly-testable point-pattern statistic for exactly this
     question, with a known sampling distribution (a real z-test, not
     an eyeballed threshold).

  3. TEMPORAL DYNAMICS -- is this catchment's business population
     growing, shrinking, or churning, using real BusinessLifecycleEvent
     rows (not simulated). Deliberately scoped to ecosystem-level
     entry/exit RATES here, not individual-business survival modeling
     -- that is Engine 7's job (Kaplan-Meier / hazard modeling on
     individual BusinessLifecycleEvent histories). This engine asks "is
     the population as a whole growing," Engine 7 asks "how long does a
     given business tend to last." Scope boundary is deliberate, not
     accidental overlap.

A fourth diagnostic, Location Quotient (category specialization
relative to the wider admin area), is reported but NOT folded into the
composite score -- see the composite-score section for why.

=============================================================================
WHAT'S ACTUALLY AVAILABLE VS. WHAT ISN'T
=============================================================================
Real data used: models.BusinessEntity (category, brand, status, lat/lon,
first_observed_at) and models.BusinessLifecycleEvent (real
opened/closed_confirmed/censored_last_seen rows, already distinguishing
confirmed deaths from right-censored ones -- see
BusinessLifecycleEvent's own docstring). No external API calls in this
engine at all -- everything here is computed from businesses this
codebase has already synced via google_client.sync_places_to_business_
entities (Engine 8's dependency) plus whatever BRS/Gazette-sourced
BusinessLifecycleEvent rows exist.

What is explicitly an approximation, flagged as such:
  - Location Quotient's reference region (the wider county/sub-county)
    is computed from THIS DATABASE's own accumulated business records,
    not an authoritative external census of businesses (Kenya does not
    publish one at this granularity) -- same "within-corpus reference,
    documented as approximation" convention already used by
    market_gap.py's corpus_rate_per_km2 and purchasing_power.py's
    admin-unit averaging. Reliable only once enough of the reference
    area has actually been queried/synced; MIN_REFERENCE_SAMPLE below
    flags when it hasn't.
  - BusinessLifecycleEvent.source matters for entry-rate reliability:
    'google_places'-sourced "opened" events only mean "first time OUR
    sync saw this place," not a verified true opening date, whereas
    'kenya_gazette'/'brs'-sourced events carry real registration dates.
    Entry/exit rates below are computed from whatever's on record but
    the source mix is reported in warnings so this distinction isn't
    hidden.

=============================================================================
REAL METHODS USED (researched and cited, not invented)
=============================================================================
1. Shannon-Wiener diversity index & Pielou evenness
   Shannon, C.E. (1948). "A Mathematical Theory of Communication." Bell
   System Technical Journal, 27(3), 379-423.
   Pielou, E.C. (1966). "The measurement of diversity in different
   types of biological collections." Journal of Theoretical Biology,
   13, 131-144.
   Quigley, J.M. (1998). "Urban Diversity and Economic Growth." Journal
   of Economic Perspectives, 12(2), 127-138. (Direct citation for
   applying ecological diversity indices to urban business-category
   mix, formalizing Jacobs' (1969) diversity argument.)
   H' = -sum(p_i * ln(p_i)) over category shares p_i; Pielou evenness
   J' = H' / ln(S) where S = category richness (number of distinct
   categories present). J' isolates BALANCE from richness -- a
   catchment with 2 categories split 50/50 has the same evenness as one
   with 10 categories split evenly, which raw H' alone would not show.

2. Simpson's Diversity Index
   Simpson, E.H. (1949). "Measurement of Diversity." Nature, 163, 688.
   D = 1 - sum(p_i^2) -- the probability that two businesses drawn at
   random from the catchment are of DIFFERENT categories. Reported
   alongside Shannon/Pielou because it weights toward dominant
   categories differently (more sensitive to a single category's
   dominance than Shannon is), giving two independent cross-checks on
   the same underlying share vector rather than one figure taken on
   faith. NOTE: same "sum of squared shares" structure as competition.py's
   HHI, but computed here across ALL business CATEGORIES present in a
   catchment, not one category's BRAND shares -- a genuinely different
   application of the identical mathematical form, not a duplicate
   feature.

3. Clark & Evans nearest-neighbor index (point-pattern analysis)
   Clark, P.J., Evans, F.C. (1954). "Distance to Nearest Neighbor as a
   Measure of Spatial Relationships in Populations." Ecology, 35(4),
   445-453.
   R = mean(observed nearest-neighbor distance) / mean(expected
   nearest-neighbor distance under Complete Spatial Randomness), where
   the CSR expectation is 1/(2*sqrt(density)), density = n/area. R < 1
   indicates clustering, R > 1 indicates dispersion/regularity, R = 1
   is consistent with a random (Poisson) spatial process. The
   accompanying z-score uses Clark & Evans' own standard-error formula,
   SE = 0.26136 / sqrt(n * density), giving a real, testable
   significance level (two-tailed, via the standard normal CDF) rather
   than treating any R != 1 as meaningful. KNOWN, DOCUMENTED
   SIMPLIFICATION: this implementation does not apply Donnelly's (1978)
   edge-correction for points near the catchment boundary (Donnelly,
   C. (1978). "Simulations to determine the variance and edge-effect of
   total nearest neighbour distance." In Simulation Methods in
   Archaeology) -- a real, citable refinement left as a documented gap
   here (same "known limitation, not hidden" convention as Engine 4's
   mixed-size-unit gap), not faked with an arbitrary correction factor.

4. Location Quotient
   Isard, W. (1960). Methods of Regional Analysis: An Introduction to
   Regional Science. MIT Press. (Standard regional-economics origin of
   LQ as a specialization/concentration measure.)
   LQ_i = (local_count_i / local_total) / (reference_count_i /
   reference_total). LQ_i > 1 means this catchment is MORE
   concentrated in category i than the wider reference area; LQ_i < 1
   means less. Reported per category as a diagnostic (see composite
   score section for why it's not blended in).

5. Ecosystem entry/exit rates from real BusinessLifecycleEvent data
   Not a single named external formula -- straightforward real-data
   rate computation (entries and confirmed exits per year, over the
   actual observed window), but genuinely grounded in this codebase's
   own real censoring-aware lifecycle model (see
   BusinessLifecycleEvent's own docstring on why censored vs confirmed
   deaths matter) rather than fabricated. This module's own composition
   of real inputs, flagged as such -- same convention as Engine 4's
   temporal_accessibility_volatility.

=============================================================================
COMPOSITE SCORE: WHY LOCATION QUOTIENT AND RAW CLARK-EVANS SIGN ARE
DELIBERATELY EXCLUDED
=============================================================================
Diversity (more balanced, more categories present) and positive net
growth both have an unambiguous "more is healthier" direction for an
ecosystem-vitality reading, so they're blended into
ecosystem_vitality_score_0_100 below. Spatial clustering and category
specialization do NOT have an unambiguous direction: clustering can
reflect genuine Marshallian agglomeration economies (positive) or
oversaturation/excess competition in a narrow strip (negative,
overlapping with what competition.py's HHI already flags); high LQ
specialization can mean a strong, differentiated local economy or a
risky monoculture. Forcing either into a single "higher = better" blend
would misrepresent a genuinely ambiguous real-world signal as settled --
both are reported in full as evidence/diagnostics instead, consistent
with this codebase's standing rule (see e.g. mobility_traffic.py's
temporal_accessibility_volatility) that ambiguous-sign statistics stay
descriptive rather than getting forced into a directional score.

=============================================================================
EDGE CASES THIS MODULE HANDLES EXPLICITLY
=============================================================================
- Zero businesses in the catchment -> every component is None, not 0
  (an unsynced/never-searched catchment is a data gap, not evidence of
  an empty ecosystem -- same principle as
  BusinessLocationCell.has_category_data's documented distinction).
- Exactly one category present -> Shannon H'=0, Pielou evenness
  undefined (ln(1)=0 denominator) -> reported as 0.0 with a flag rather
  than a divide-by-zero, since a single-category catchment IS the
  minimum-evenness case by definition.
- Fewer than MIN_N_FOR_CLARK_EVANS businesses -> Clark-Evans withheld
  entirely (a nearest-neighbor STATISTIC needs enough neighbors to be
  meaningful; Clark & Evans' own SE formula also becomes unstable at
  very small n).
- Co-located businesses (distance = 0, e.g. multiple units in the same
  mall/plaza sharing near-identical coordinates from Places) -> real,
  not an error; included in the nearest-neighbor calculation as-is
  (they genuinely pull R toward 0/clustering, which is an honest
  reflection of true co-location), but flagged in warnings so a
  surprisingly low R can be sanity-checked against duplicate records.
- Reference region (for Location Quotient) has fewer than
  MIN_REFERENCE_SAMPLE businesses on record -> LQ still computed but
  flagged low_reference_sample, since a thin reference denominator
  makes the ratio unstable.
- No BusinessLifecycleEvent rows at all for this catchment's businesses
  -> temporal churn component is None, not "0% growth" (never having
  tracked entries/exits is a data gap, not evidence of a static
  ecosystem).
- Catchment is an isochrone type (minutes-based, no fixed meter radius)
  -> business-mix retrieval here only supports meter-radius catchments
  (radius/network_radius); isochrone catchments are skipped with an
  explicit flag (needs real point-in-polygon geometry_geojson lookup,
  same documented, unimplemented gap google_client.fetch_isochrone
  itself already flags) rather than approximated with a fake radius.

Scope boundary
--------------
Depends on business_intel.models only (BusinessEntity,
BusinessLifecycleEvent, BusinessLocationCell, Catchment, BusinessQuery,
EngineRun, FeatureObservation). No dependency on any other engine's
feature-store output or any external API -- purely a real-data read
across this codebase's own accumulated BusinessEntity/
BusinessLifecycleEvent history, same "genuinely independent" scope as
Engine 4/5's raw-API-only design, just DB-only instead of API-only here.
"""

from __future__ import annotations

import logging
import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional, Sequence

from django.db import transaction
from django.utils import timezone as django_timezone

from ..models import (
    BusinessEntity, BusinessLifecycleEvent, BusinessLocationCell,
    Catchment, BusinessQuery, EngineRun, FeatureObservation,
)

logger = logging.getLogger("business_intel.engines.business_ecosystem")

ENGINE_NUMBER = 6

EARTH_RADIUS_M = 6_371_000.0

MIN_N_FOR_CLARK_EVANS = 8       # below this, a nearest-neighbor SE isn't stable
MIN_REFERENCE_SAMPLE = 30       # below this, LQ's denominator is flagged unstable
MIN_CATEGORIES_FOR_EVENNESS = 2  # Pielou evenness is undefined for S<2

# Default lookback window for ecosystem entry/exit rates -- generous by
# design (this module has no per-call API cost, unlike Engine 4/5, so
# there's no reason to shrink this): 3 years gives a real multi-year
# trend without reaching back before most of this codebase's own
# tracking began. Exposed as a parameter, not hardcoded, so callers with
# a longer real Gazette-sourced history can widen it freely.
DEFAULT_CHURN_LOOKBACK_DAYS = 3 * 365


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters. Duplicated (not imported) from
    competition.py/mobility_traffic.py's identical function, per this
    codebase's own established convention of keeping each engine's
    import-order/dependency footprint independent."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def _standard_normal_cdf(z: float) -> float:
    """Phi(z) via the error function -- no scipy in this environment
    (same numpy-free, closed-form convention used for the exact same
    purpose in engines.forced_traffic)."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


# ============================================================================
# Pure-math layer 1: category-mix diversity (Shannon, Pielou, Simpson).
# No DB access below this point -- every function here takes an
# already-fetched category-count dict, independently unit-testable, same
# pattern as every other engine's pure-math layer.
# ============================================================================

@dataclass
class DiversityResult:
    richness: int
    total_count: int
    shannon_h: float
    shannon_h_max: float
    pielou_evenness: Optional[float]
    simpson_d: float
    category_shares: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)


def category_diversity(category_counts: dict) -> Optional[DiversityResult]:
    """
    category_counts: {category_name: count}, already filtered to
    operational businesses in the catchment. Shannon H' (point 1),
    Pielou evenness J'=H'/ln(S) (point 1), Simpson's D (point 2).
    Returns None for an empty catchment (see module docstring edge
    cases) -- 0 businesses is not the same as 0 diversity.
    """
    total = sum(category_counts.values())
    if total == 0:
        return None

    warnings = []
    richness = len(category_counts)
    shares = {cat: count / total for cat, count in category_counts.items()}

    shannon_h = -sum(p * math.log(p) for p in shares.values() if p > 0)
    shannon_h_max = math.log(richness) if richness > 1 else 0.0

    if richness < MIN_CATEGORIES_FOR_EVENNESS:
        pielou = 0.0
        warnings.append(f"Only {richness} category present -- Pielou evenness is minimally defined (0.0), not computed from ln(1).")
    else:
        pielou = round(shannon_h / shannon_h_max, 6) if shannon_h_max > 0 else 0.0

    simpson_d = 1.0 - sum(p ** 2 for p in shares.values())

    return DiversityResult(
        richness=richness, total_count=total,
        shannon_h=round(shannon_h, 6), shannon_h_max=round(shannon_h_max, 6),
        pielou_evenness=pielou, simpson_d=round(simpson_d, 6),
        category_shares={k: round(v, 4) for k, v in shares.items()},
        warnings=warnings,
    )


# ============================================================================
# Pure-math layer 2: Clark & Evans nearest-neighbor spatial-pattern test.
# ============================================================================

@dataclass
class ClarkEvansResult:
    n: int
    area_km2: float
    density_per_km2: float
    observed_mean_nn_distance_m: float
    expected_mean_nn_distance_m: float
    r_statistic: float
    z_score: float
    p_value_two_tailed: float
    interpretation: str  # "clustered" | "dispersed" | "not distinguishable from random"
    co_located_pairs: int
    warnings: list = field(default_factory=list)


def nearest_neighbor_index(
    points: Sequence[tuple], area_km2: float, alpha: float = 0.05,
) -> Optional[ClarkEvansResult]:
    """
    points: sequence of (lat, lon). Full O(n^2) pairwise haversine --
    fine at catchment scale (typically well under a few hundred real
    businesses); no spatial index needed at this n. Withholds the
    result below MIN_N_FOR_CLARK_EVANS (point 3, module docstring).
    """
    n = len(points)
    if n < MIN_N_FOR_CLARK_EVANS or area_km2 <= 0:
        return None

    warnings = []
    area_m2 = area_km2 * 1_000_000.0
    density = n / area_m2  # points per m^2

    nn_distances = []
    co_located = 0
    for i in range(n):
        best = None
        for j in range(n):
            if i == j:
                continue
            d = haversine_m(points[i][0], points[i][1], points[j][0], points[j][1])
            if best is None or d < best:
                best = d
        nn_distances.append(best)
        if best == 0.0:
            co_located += 1

    observed_mean = sum(nn_distances) / n
    expected_mean = 1.0 / (2.0 * math.sqrt(density))
    r = observed_mean / expected_mean if expected_mean > 0 else None
    if r is None:
        return None

    se = 0.26136 / math.sqrt(n * density)
    z = (observed_mean - expected_mean) / se if se > 0 else 0.0
    p_two_tailed = 2.0 * (1.0 - _standard_normal_cdf(abs(z)))

    if p_two_tailed < alpha:
        interpretation = "clustered" if r < 1.0 else "dispersed"
    else:
        interpretation = "not distinguishable from random (CSR)"

    if co_located:
        warnings.append(
            f"{co_located} of {n} businesses have a nearest-neighbor distance of exactly 0m (co-located coordinates) "
            "-- could be real co-location (e.g. mall units sharing one Places pin) or duplicate records; "
            "pulls R toward 0 either way, worth spot-checking if this reading is surprising."
        )
    warnings.append(
        "No Donnelly (1978) edge-effect correction applied -- points near the catchment boundary may have their "
        "true nearest neighbor outside it, biasing observed_mean_nn_distance upward. Documented simplification, "
        "see module docstring point 3."
    )

    return ClarkEvansResult(
        n=n, area_km2=round(area_km2, 4), density_per_km2=round(density * 1_000_000.0, 4),
        observed_mean_nn_distance_m=round(observed_mean, 2), expected_mean_nn_distance_m=round(expected_mean, 2),
        r_statistic=round(r, 4), z_score=round(z, 4), p_value_two_tailed=round(p_two_tailed, 6),
        interpretation=interpretation, co_located_pairs=co_located, warnings=warnings,
    )


# ============================================================================
# Pure-math layer 3: Location Quotient.
# ============================================================================

@dataclass
class LocationQuotientEntry:
    category: str
    local_share: float
    reference_share: float
    lq: float


@dataclass
class LocationQuotientResult:
    entries: list
    reference_total: int
    low_reference_sample: bool
    warnings: list = field(default_factory=list)


def location_quotients(local_counts: dict, reference_counts: dict) -> Optional[LocationQuotientResult]:
    """LQ_i (point 4). reference_counts should include the local area's
    own counts (a standard-region-includes-subregion LQ setup) -- the
    caller is expected to pass the full admin-unit aggregate, which by
    construction already contains the catchment's own businesses."""
    local_total = sum(local_counts.values())
    reference_total = sum(reference_counts.values())
    if local_total == 0 or reference_total == 0:
        return None

    warnings = []
    low_ref = reference_total < MIN_REFERENCE_SAMPLE
    if low_ref:
        warnings.append(
            f"Reference region has only {reference_total} businesses on record (< {MIN_REFERENCE_SAMPLE}) -- "
            "Location Quotient denominators are unstable at this sample size; treat LQ values as indicative only."
        )

    entries = []
    for cat, local_count in local_counts.items():
        local_share = local_count / local_total
        ref_share = reference_counts.get(cat, 0) / reference_total
        if ref_share == 0:
            # Category present locally but absent from the reference region
            # entirely -- LQ is mathematically infinite; report as such
            # rather than a fabricated large finite number.
            entries.append(LocationQuotientEntry(category=cat, local_share=round(local_share, 4), reference_share=0.0, lq=float("inf")))
            continue
        entries.append(LocationQuotientEntry(
            category=cat, local_share=round(local_share, 4), reference_share=round(ref_share, 4),
            lq=round(local_share / ref_share, 4),
        ))

    entries.sort(key=lambda e: -(e.lq if e.lq != float("inf") else 1e18))
    return LocationQuotientResult(entries=entries, reference_total=reference_total, low_reference_sample=low_ref, warnings=warnings)


# ============================================================================
# Pure-math layer 4: ecosystem entry/exit rates from real
# BusinessLifecycleEvent data.
# ============================================================================

@dataclass
class EcosystemChurnResult:
    window_days: int
    businesses_tracked: int
    entries: int
    confirmed_exits: int
    censored_last_seen: int
    entry_rate_per_year: float
    exit_rate_per_year: float
    net_growth_rate_pct_per_year: Optional[float]
    source_mix: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)


def compute_ecosystem_churn(
    events: Sequence, businesses_tracked: int, window_days: int = DEFAULT_CHURN_LOOKBACK_DAYS,
) -> Optional[EcosystemChurnResult]:
    """
    events: BusinessLifecycleEvent rows (already filtered to this
    catchment's businesses and the lookback window by the caller).
    businesses_tracked: current operational count, used as the base for
    net_growth_rate_pct (point 5). Returns None if there are no events
    at all (a real data gap, not 0% growth -- see module docstring).
    """
    if not events:
        return None

    warnings = []
    entries = sum(1 for e in events if e.event_type == "opened")
    confirmed_exits = sum(1 for e in events if e.event_type == "closed_confirmed")
    censored = sum(1 for e in events if e.event_type == "censored_last_seen")

    years = max(window_days / 365.0, 1e-9)
    entry_rate = entries / years
    exit_rate = confirmed_exits / years

    net_growth_pct = None
    if businesses_tracked > 0:
        net_growth_pct = round(((entries - confirmed_exits) / businesses_tracked) / years * 100.0, 4)
    else:
        warnings.append("businesses_tracked=0 -- net_growth_rate_pct_per_year withheld (no base to compute a rate against).")

    source_counts = Counter(e.source for e in events)
    non_gazette_opened = sum(1 for e in events if e.event_type == "opened" and e.source not in ("kenya_gazette", "brs"))
    if entries and non_gazette_opened == entries:
        warnings.append(
            "All 'opened' events are sourced from observation (not kenya_gazette/brs) -- entry dates reflect when "
            "this system FIRST SAW each business, not verified true opening dates. Real trend direction is still "
            "meaningful; exact entry_rate_per_year should be read as a lower/lagged bound."
        )
    if censored > confirmed_exits:
        warnings.append(
            f"{censored} right-censored ('last seen, not confirmed closed') events vs {confirmed_exits} confirmed "
            "closures -- true exit_rate could be higher than reported; censored events are correctly excluded from "
            "confirmed_exits per BusinessLifecycleEvent's own censoring discipline, not undercounted by mistake."
        )

    return EcosystemChurnResult(
        window_days=window_days, businesses_tracked=businesses_tracked,
        entries=entries, confirmed_exits=confirmed_exits, censored_last_seen=censored,
        entry_rate_per_year=round(entry_rate, 4), exit_rate_per_year=round(exit_rate, 4),
        net_growth_rate_pct_per_year=net_growth_pct, source_mix=dict(source_counts), warnings=warnings,
    )


# ============================================================================
# Composite score + evidence dict
# ============================================================================

def ecosystem_vitality_score_0_100(
    diversity: Optional[DiversityResult],
    churn: Optional[EcosystemChurnResult],
    corpus_richness_reference: Optional[float] = None,
) -> Optional[float]:
    """
    Blends ONLY the two unambiguous-direction components (see module
    docstring's composite-score section): 55% diversity (evenness and
    richness combined), 45% temporal growth momentum. Missing components
    are dropped and remaining weights renormalized (same convention as
    engines.forced_traffic.forced_traffic_score_0_100), not treated as 0.

    Diversity sub-blend: 60% Pielou evenness (already 0-1, scaled to
    0-100 directly) + 40% richness, rescaled against
    corpus_richness_reference (median richness across catchments already
    computed this run/cached, same corpus-logistic-or-log1p-fallback
    convention as mobility_traffic.mobility_score_0_100's accessibility
    rescaling) when available, else a coarse log1p scaling.

    Growth momentum: net_growth_rate_pct_per_year run through a logistic
    centered at 0 (0%/yr growth -> 50; strongly positive -> approaches
    100; strongly negative -> approaches 0), scale factor chosen so
    +/-10%/yr (a real, substantial catchment-level swing) sits near the
    logistic's steep region.
    """
    components = []  # (weight, value)

    if diversity is not None and diversity.pielou_evenness is not None:
        evenness_term = diversity.pielou_evenness * 100.0
        if corpus_richness_reference and corpus_richness_reference > 0:
            ratio = diversity.richness / corpus_richness_reference
            richness_term = 100.0 / (1.0 + math.exp(-(ratio - 1.0)))
        else:
            richness_term = min(100.0, 25.0 * math.log1p(diversity.richness))
        diversity_component = 0.60 * evenness_term + 0.40 * richness_term
        components.append((0.55, diversity_component))

    if churn is not None and churn.net_growth_rate_pct_per_year is not None:
        g = churn.net_growth_rate_pct_per_year
        growth_component = 100.0 / (1.0 + math.exp(-g / 5.0))
        components.append((0.45, growth_component))

    if not components:
        return None

    weight_sum = sum(w for w, _ in components)
    return round(sum(w * v for w, v in components) / weight_sum, 2)


def business_ecosystem_evidence(
    diversity: Optional[DiversityResult],
    clark_evans: Optional[ClarkEvansResult],
    location_quotient: Optional[LocationQuotientResult],
    churn: Optional[EcosystemChurnResult],
) -> dict:
    """Structured evidence dict, same convention as every other engine's
    *_evidence function in this codebase."""
    return {
        "composition": {
            "richness": diversity.richness if diversity else None,
            "shannon_h": diversity.shannon_h if diversity else None,
            "pielou_evenness": diversity.pielou_evenness if diversity else None,
            "simpson_d": diversity.simpson_d if diversity else None,
            "category_shares": diversity.category_shares if diversity else None,
            "warnings": diversity.warnings if diversity else [],
        } if diversity is not None else None,
        "spatial_pattern": {
            "n": clark_evans.n if clark_evans else None,
            "r_statistic": clark_evans.r_statistic if clark_evans else None,
            "z_score": clark_evans.z_score if clark_evans else None,
            "p_value_two_tailed": clark_evans.p_value_two_tailed if clark_evans else None,
            "interpretation": clark_evans.interpretation if clark_evans else None,
            "density_per_km2": clark_evans.density_per_km2 if clark_evans else None,
            "warnings": clark_evans.warnings if clark_evans else [],
        } if clark_evans is not None else None,
        "specialization_location_quotient": {
            "top_specializations": [
                {"category": e.category, "lq": (e.lq if e.lq != float("inf") else "inf"), "local_share": e.local_share}
                for e in (location_quotient.entries[:5] if location_quotient else [])
            ],
            "reference_total": location_quotient.reference_total if location_quotient else None,
            "low_reference_sample": location_quotient.low_reference_sample if location_quotient else None,
            "warnings": location_quotient.warnings if location_quotient else [],
        } if location_quotient is not None else None,
        "temporal_churn": {
            "entries": churn.entries if churn else None,
            "confirmed_exits": churn.confirmed_exits if churn else None,
            "censored_last_seen": churn.censored_last_seen if churn else None,
            "entry_rate_per_year": churn.entry_rate_per_year if churn else None,
            "exit_rate_per_year": churn.exit_rate_per_year if churn else None,
            "net_growth_rate_pct_per_year": churn.net_growth_rate_pct_per_year if churn else None,
            "source_mix": churn.source_mix if churn else None,
            "warnings": churn.warnings if churn else [],
        } if churn is not None else None,
        "methods": [
            "shannon_wiener_diversity", "pielou_evenness", "simpson_diversity_index",
            "clark_evans_nearest_neighbor", "location_quotient", "lifecycle_event_churn_rate",
        ],
    }


# ============================================================================
# Django-facing entry points
# ============================================================================

DEFAULT_ECOSYSTEM_SEARCH_RADIUS_FALLBACK_M = 1500.0


def _operational_businesses_in_radius(subject_lat: float, subject_lon: float, radius_m: float) -> list:
    """Same bbox-prefilter + exact-haversine pattern as
    competition.py's compute_competition_score, but across ALL
    categories (this engine's whole point is the MIX, not one
    category)."""
    lat_pad = radius_m / 111_000.0
    lon_pad = radius_m / (111_000.0 * max(math.cos(math.radians(subject_lat)), 0.01))
    bbox_filter = dict(
        current_status="operational",
        latitude__gte=subject_lat - lat_pad, latitude__lte=subject_lat + lat_pad,
        longitude__gte=subject_lon - lon_pad, longitude__lte=subject_lon + lon_pad,
    )
    qs = BusinessEntity.objects.filter(**bbox_filter)
    found = []
    for biz in qs:
        d = haversine_m(subject_lat, subject_lon, float(biz.latitude), float(biz.longitude))
        if d <= radius_m:
            found.append(biz)
    return found


def _category_counts_for_admin_unit(county: str, subcounty: Optional[str] = None) -> dict:
    """Reference-region category counts for Location Quotient (point
    4). Same admin-unit aggregation pattern as purchasing_power.py's/
    market_gap.py's identically-shaped helpers -- kept local per this
    codebase's established convention (see forced_traffic.py's/
    market_gap.py's own notes on why these small helpers are duplicated,
    not imported, across engines)."""
    cell_filter = {"county": county}
    if subcounty:
        cell_filter["subcounty"] = subcounty
    cell_ids = BusinessLocationCell.objects.filter(**cell_filter).values_list("id", flat=True)
    qs = BusinessEntity.objects.filter(location_cell_id__in=cell_ids, current_status="operational")
    return dict(Counter(qs.values_list("category", flat=True)))


@dataclass
class CatchmentEcosystemFeatures:
    catchment: Catchment
    diversity: Optional[DiversityResult] = None
    clark_evans: Optional[ClarkEvansResult] = None
    location_quotient: Optional[LocationQuotientResult] = None
    churn: Optional[EcosystemChurnResult] = None
    vitality_score: Optional[float] = None
    source: str = "business_intel.engines.business_ecosystem"
    source_class: str = "internal_derived"
    warnings: list = field(default_factory=list)


def compute_catchment_ecosystem(
    cell: BusinessLocationCell,
    catchment: Catchment,
    churn_lookback_days: int = DEFAULT_CHURN_LOOKBACK_DAYS,
    corpus_richness_reference: Optional[float] = None,
) -> Optional[CatchmentEcosystemFeatures]:
    """Full Section-6 calculation for one Catchment. Returns None (not
    a features object with everything empty) for isochrone-type
    catchments this engine doesn't support yet -- see module docstring
    edge cases -- so the caller can skip them cleanly."""
    if catchment.catchment_size_unit != "meters":
        logger.info(
            "Skipping catchment %s: business-ecosystem mix currently only supports meter-radius catchments "
            "(radius/network_radius), not isochrone geometry -- see module docstring.", catchment.id,
        )
        return None

    subject_lat, subject_lon = float(cell.center_latitude), float(cell.center_longitude)
    radius_m = float(catchment.catchment_size_value)
    warnings = []

    businesses = _operational_businesses_in_radius(subject_lat, subject_lon, radius_m)
    if not businesses:
        warnings.append(f"No operational businesses found within {radius_m:.0f}m -- catchment may be genuinely empty or not yet Places-searched.")
        return CatchmentEcosystemFeatures(catchment=catchment, warnings=warnings)

    category_counts = dict(Counter(b.category for b in businesses))
    diversity = category_diversity(category_counts)

    area_km2 = float(catchment.area_km2) if catchment.area_km2 is not None else (math.pi * (radius_m / 1000.0) ** 2)
    if catchment.area_km2 is None:
        warnings.append("Catchment.area_km2 not set -- used a circular-buffer area approximation (pi*r^2) instead, flagged as such.")
    points = [(float(b.latitude), float(b.longitude)) for b in businesses]
    clark_evans = nearest_neighbor_index(points, area_km2)
    if clark_evans is None and len(businesses) < MIN_N_FOR_CLARK_EVANS:
        warnings.append(f"Only {len(businesses)} businesses (< {MIN_N_FOR_CLARK_EVANS}) -- Clark-Evans spatial-pattern test withheld.")

    location_quotient = None
    if cell.county:
        reference_counts = _category_counts_for_admin_unit(cell.county, cell.subcounty or None)
        location_quotient = location_quotients(category_counts, reference_counts)
    else:
        warnings.append("BusinessLocationCell.county is blank -- Location Quotient reference region withheld.")

    window_start = date.today() - timedelta(days=churn_lookback_days)
    business_ids = [b.id for b in businesses]
    events = list(
        BusinessLifecycleEvent.objects.filter(business_id__in=business_ids, event_date__gte=window_start)
    )
    churn = compute_ecosystem_churn(events, businesses_tracked=len(businesses), window_days=churn_lookback_days)
    if churn is None:
        warnings.append("No BusinessLifecycleEvent rows on record for these businesses in the lookback window -- temporal churn component withheld.")

    score = ecosystem_vitality_score_0_100(diversity, churn, corpus_richness_reference)

    return CatchmentEcosystemFeatures(
        catchment=catchment, diversity=diversity, clark_evans=clark_evans,
        location_quotient=location_quotient, churn=churn, vitality_score=score, warnings=warnings,
    )


def run(
    query: BusinessQuery,
    catchments: Optional[Sequence[Catchment]] = None,
    churn_lookback_days: int = DEFAULT_CHURN_LOOKBACK_DAYS,
    corpus_richness_reference: Optional[float] = None,
) -> dict:
    """
    Engine 6 entry point. Computes business-ecosystem features for
    every Catchment belonging to query.location_cell (or an explicit
    `catchments` subset), writes FeatureObservation rows, and records
    an EngineRun -- same contract shape as every other engine.

    Requires Engine 1 to have already run (catchments must exist), same
    dependency population.py's run() documents.
    """
    started_at = django_timezone.now()
    cell = query.location_cell
    target_catchments = list(catchments) if catchments is not None else list(cell.catchments.all())

    run_row = EngineRun.objects.create(
        query=query, engine_number=ENGINE_NUMBER, status="succeeded", started_at=started_at,
    )

    if not target_catchments:
        run_row.status = "failed"
        run_row.error_message = "No Catchment rows found for this cell. Engine 1 (Spatial Context) must run first."
        run_row.finished_at = django_timezone.now()
        run_row.save(update_fields=["status", "error_message", "finished_at"])
        return {"cell": cell, "features": [], "engine_run": run_row}

    features_written = 0
    warnings_seen = []
    all_features = []

    try:
        with transaction.atomic():
            for catchment in target_catchments:
                catch_features = compute_catchment_ecosystem(
                    cell, catchment, churn_lookback_days=churn_lookback_days,
                    corpus_richness_reference=corpus_richness_reference,
                )
                if catch_features is None:
                    continue  # unsupported (isochrone) catchment type -- see module docstring
                all_features.append(catch_features)
                warnings_seen.extend(catch_features.warnings)
                evidence = business_ecosystem_evidence(
                    catch_features.diversity, catch_features.clark_evans,
                    catch_features.location_quotient, catch_features.churn,
                )

                if catch_features.diversity is not None:
                    for field_name, value, unit in (
                        ("shannon_diversity_index", catch_features.diversity.shannon_h, "nats"),
                        ("pielou_evenness", catch_features.diversity.pielou_evenness, "ratio_0_1"),
                        ("simpson_diversity_index", catch_features.diversity.simpson_d, "ratio_0_1"),
                        ("category_richness", float(catch_features.diversity.richness), "count"),
                    ):
                        if value is None:
                            continue
                        FeatureObservation.write_latest(
                            location_cell=cell, catchment=catchment, engine_number=ENGINE_NUMBER, feature_name=field_name,
                            defaults=dict(
                                value_type="numeric", value_numeric=Decimal(str(round(value, 6))), unit=unit,
                                observed_at=started_at, source=catch_features.source, source_class=catch_features.source_class,
                                method="shannon_pielou_simpson_diversity", derivation_level="calculation",
                                sample_size=catch_features.diversity.total_count,
                            ),
                        )
                        features_written += 1

                if catch_features.clark_evans is not None:
                    FeatureObservation.write_latest(
                        location_cell=cell, catchment=catchment, engine_number=ENGINE_NUMBER, feature_name="clark_evans_r_statistic",
                        defaults=dict(
                            value_type="numeric", value_numeric=Decimal(str(catch_features.clark_evans.r_statistic)),
                            unit="ratio", observed_at=started_at, source=catch_features.source, source_class=catch_features.source_class,
                            method="clark_evans_1954_nearest_neighbor", derivation_level="calculation",
                            sample_size=catch_features.clark_evans.n,
                            uncertainty=Decimal(str(catch_features.clark_evans.p_value_two_tailed)),
                        ),
                    )
                    features_written += 1

                if catch_features.churn is not None and catch_features.churn.net_growth_rate_pct_per_year is not None:
                    FeatureObservation.write_latest(
                        location_cell=cell, catchment=catchment, engine_number=ENGINE_NUMBER, feature_name="net_business_growth_rate_pct",
                        defaults=dict(
                            value_type="numeric", value_numeric=Decimal(str(catch_features.churn.net_growth_rate_pct_per_year)),
                            unit="%/yr", observed_at=started_at, source=catch_features.source, source_class=catch_features.source_class,
                            method="lifecycle_event_churn_rate", derivation_level="calculation",
                            sample_size=catch_features.churn.businesses_tracked,
                        ),
                    )
                    features_written += 1

                if catch_features.vitality_score is not None:
                    FeatureObservation.write_latest(
                        location_cell=cell, catchment=catchment, engine_number=ENGINE_NUMBER, feature_name="ecosystem_vitality_score",
                        defaults=dict(
                            value_type="numeric", value_numeric=Decimal(str(catch_features.vitality_score)), unit="index_0_100",
                            observed_at=started_at, source=catch_features.source, source_class=catch_features.source_class,
                            method="business_ecosystem_engine_blend", derivation_level="business_conclusion",
                        ),
                    )
                    features_written += 1

                FeatureObservation.write_latest(
                    location_cell=cell, catchment=catchment, engine_number=ENGINE_NUMBER, feature_name="business_ecosystem_evidence",
                    defaults=dict(
                        value_type="json", value_json=evidence, unit="",
                        observed_at=started_at, source=catch_features.source, source_class=catch_features.source_class,
                        method="business_ecosystem_engine", derivation_level="calculation",
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
    if warnings_seen:
        run_row.status = "degraded"
        run_row.degraded_reason = "; ".join(sorted(set(warnings_seen)))[:255]
    run_row.save(update_fields=["status", "finished_at", "duration_ms", "features_written", "degraded_reason"])

    return {"cell": cell, "features": all_features, "engine_run": run_row}