"""
business_intel/engines/infrastructure_change.py

Engine 13 — Infrastructure Change Engine
Per business_location_intelligence_engines_data_models_v2.md, Section 13.

NOTE ON THE SPEC FILE: same situation as every other engine stub in this
codebase (development_activity.py, industry_translation.py, population.py,
purchasing_power.py, competition.py) already documents for itself --
business_location_intelligence_engines_data_models_v2.md is referenced but
does not exist on disk. Built without it, matching those engines'
structure/schema contract and citation discipline. Anywhere this would
otherwise need to guess at the missing spec, it says so instead of
guessing silently.

=============================================================================
PURPOSE, AND HOW THIS DIFFERS FROM ENGINE 12
=============================================================================
"Measure how infrastructure investment changes future accessibility and
commercial conditions." models.InfrastructureProject's own docstring says
it "covers both development activity and infrastructure change" -- i.e.
Engine 12 and Engine 13 are an explicitly designed pair sharing one table,
split by question, not by data source availability:

  - Engine 12 (development_activity.py) asks "is something being built
    HERE, right now" -- answered from OSM Overpass, a live snapshot of
    what physically exists on the ground today. It does not read
    InfrastructureProject at all (confirmed: no engine in this codebase
    reads that model before this file).
  - Engine 13 (this file) asks "how does a PLANNED/COMMITTED infrastructure
    project change future accessibility" -- answered from
    InfrastructureProject, the one real, already-migrated table built for
    exactly this forward-looking question (PROJECT_TYPE_CHOICES,
    STATUS_CHOICES from proposed through completed/stalled, completion_pct,
    start_date/end_date, budget_kes). Overpass cannot answer this: it sees
    what is physically present now, not what is approved-but-not-yet-built.

Scope boundary (documented, not incidental): InfrastructureProject's
PROJECT_TYPE_CHOICES also includes residential/commercial/industrial
"development" types. Those describe buildings, not infrastructure, and are
Engine 12's conceptual territory even though Engine 12 currently sources
its own signal from OSM rather than this table -- duplicating "is a
building going up" here would violate the one-engine-one-question
discipline market_gap.py/business_adjacency.py's own docstrings already
insist on for themselves. INFRASTRUCTURE_PROJECT_TYPES below is therefore
a real subset: road, bridge, interchange, rail, brt, water, sewerage,
electricity, fibre -- physical infrastructure with an accessibility or
serviceability effect, not development stock.

=============================================================================
THE REAL STATISTICAL PROBLEM
=============================================================================
Three genuinely separate problems, each handled with a named, real method
rather than a single invented formula:

1. SPATIAL DECAY OF IMPACT -- a project's effect on commercial conditions
   attenuates with distance from the query cell, and different
   infrastructure classes plausibly attenuate at different rates. This
   module borrows the accessibility-as-decayed-opportunity framing of
   Hansen, W. G. (1959). "How Accessibility Shapes Land Use." Journal of
   the American Institute of Planners, 25(2), 73-76 -- the foundational
   gravity-style accessibility measure (opportunity weighted by a
   decreasing function of distance/cost), rather than a hard-cutoff
   catchment. Two decay scales are used, not one, because "reach" is not
   uniform across this table's project types:
     - ROAD_RAIL_DECAY_M for road/bridge/interchange/rail/brt: corridor-
       and network-scale mobility infrastructure, whose accessibility
       effect plausibly extends well beyond an immediate walkshed.
     - UTILITY_DECAY_M for water/sewerage/electricity/fibre: serviceability
       infrastructure whose commercial relevance is local (is this specific
       cell now served), not a network-reach effect. Its scale is anchored
       (not validated -- see below) to the ~800m transit-catchment radius
       Guerra, E., Cervero, R., & Tischler, D. (2012). "The Half-Mile
       Circle: Does It Best Represent Transit Station Catchments?"
       Transportation Research Record, 2276(1), 101-109 report as the
       de facto U.S. standard population catchment for a transit stop
       (their study also finds 0.25 miles/~400m fits employment catchments
       better than population ones -- both distances real, reused here only
       as an order-of-magnitude reference point for a "locally relevant"
       radius, not because that paper measured Kenyan utility infrastructure).
   REACH_MULTIPLIER_BY_TYPE (below) additionally spreads decay scale WITHIN
   the mobility group (an interchange plausibly reaches further than a
   single road segment) -- an ordinal engineering judgment, flagged as
   such, not a second citation.

2. WHETHER A PLANNED PROJECT WILL ACTUALLY DELIVER -- a "proposed" project
   and a "completed" one are not the same evidence, and treating every row
   in the table as equally likely to materialize would be dishonest. This
   is exactly the megaproject delivery-risk problem Flyvbjerg has measured
   at scale: Flyvbjerg, B. (2014). "What You Should Know About Megaprojects
   and Why: An Overview." Project Management Journal, 45(2), 6-19 --
   documents cost overruns in roughly nine of ten megaprojects, delays
   averaging ~45% of planned duration for dams, and overruns/shortfalls
   that are systematic rather than occasional across seven decades of
   international data. That paper does not hand this module a Kenya-
   calibrated "P(delivery | status)" table (no such published table exists
   for this system to cite), so STATUS_DELIVERY_PROBABILITY below is an
   explicit, documented, ORDINALLY-grounded-but-uncalibrated engineering
   estimate -- proposed and stalled projects are weighted low (Flyvbjerg's
   findings are exactly why "proposed" should not be treated as "as good as
   built," and a project already flagged stalled is empirical evidence of
   exactly the failure mode that literature documents), under_construction
   weighted high without being 1.0 (Flyvbjerg's overrun/delay evidence
   applies to projects already underway, not only to ones on paper). Same
   "conservative placeholder, not measured" status development_activity.py's
   COLD_START_PRIOR_CONSTRUCTION_INTENSITY and purchasing_power.py's own
   priors already carry for themselves in this codebase.

3. CONSTRUCTION-PHASE DISRUPTION IS REAL AND DOCUMENTED, NOT JUST A
   THEORETICAL OFFSET -- an under_construction mobility project near a cell
   is not purely good news for that cell's near-term commercial conditions.
   Ray, R. S. (2017). "Open for business? Effects of Los Angeles Metro Rail
   construction on adjacent businesses." Journal of Transport and Land Use,
   10(1), 725-742 finds business survival was significantly lower within
   400m of stations undergoing cut-and-cover construction. This module uses
   that 400m figure for DISRUPTION_RADIUS_M and flags (does not silently
   fold into the signed score -- see point below) nearby under_construction
   mobility projects as a near-term disruption risk, consistent with this
   codebase's established "report a real but uncalibrated signed effect in
   evidence rather than netting it into one uncalibrated number" convention
   (development_activity.py's net-decline handling is the direct precedent).

=============================================================================
AGGREGATION: A REAL EXPECTATION AND VARIANCE, NOT A POINT GUESS
=============================================================================
Each nearby, non-completed, non-cancelled project i contributes a random
"delivered impact" modeled as a Bernoulli-scaled magnitude:
    contribution_i = magnitude_i * X_i,   X_i ~ Bernoulli(p_i)
where magnitude_i = REACH_MULTIPLIER_BY_TYPE[type_i] * exp(-distance_i /
decay_m[type_i]) (the Hansen-style decayed impact IF the project delivers)
and p_i = effective_delivery_probability_i (status-based, see point 2,
optionally nudged upward by completion_pct for under_construction projects
already partway done -- see effective_delivery_probability()). Treating
each project's delivery as an independent Bernoulli trial and summing is
ordinary probability theory (not a citation-requiring method choice; the
INTERPRETATION of p_i is what point 2 above grounds):
    E[sum] = sum_i magnitude_i * p_i
    Var[sum] = sum_i magnitude_i^2 * p_i * (1 - p_i)     (independence assumed)
A normal-approximation 95% interval is built from that mean/variance
(explicitly flagged as an approximation, weakest when few projects are
nearby -- see MIN_PROJECTS_FOR_NORMAL_APPROX) -- honest propagation of the
one real uncertainty this module has (will each project actually deliver),
not a fabricated confidence figure.

=============================================================================
FRAMING THE 0-100 SCORE HONESTLY
=============================================================================
Same status this codebase already applies to every uncalibrated composite:
infrastructure_change_score is a monotonic, explicitly UNCALIBRATED
saturating transform of the expected decayed impact above -- more real,
better-evidenced, more-likely-to-deliver nearby infrastructure investment
always scores higher, never lower, but no published Kenya-calibrated
threshold exists mapping a specific expected-impact value to a specific
real accessibility outcome. Construction-phase disruption (point 3) is
computed and reported but deliberately NOT subtracted from the score with
any specific weight, for the same reason development_activity.py declines
to net its own signed decline signal into one axis: doing so would need
calibration data this system does not have. Documented, not implemented,
gap.

=============================================================================
WHAT'S ACTUALLY AVAILABLE VS. WHAT ISN'T
=============================================================================
Real data used: business_intel.models.InfrastructureProject rows already
in this system's own database (populated by whatever ETL/ingestion process
feeds that table -- out of scope for this engine, which only reads it).
No live network call is made by this engine (unlike Engine 12's Overpass
call) -- it is a query over already-migrated internal data, so there is no
per-run API cost or cache-staleness question of the Engine-12 kind, only
the StaleAwareModel staleness each InfrastructureProject row already
carries via its own source_class/last_refreshed_at, which this engine
reads and flags (not silently trusts) per project.

What is explicitly NOT available and is flagged, not faked:
  - No independent, Kenya-specific measurement of P(delivery | status) --
    see point 2 above. STATUS_DELIVERY_PROBABILITY is an engineering
    default, always reported alongside the raw status so a reader can
    apply their own judgment, exactly like development_activity.py's own
    uncalibrated priors are reported.
  - No routed/network travel-time accessibility (Engine 4/mobility_traffic
    territory) -- straight-line (haversine) distance is used for the decay
    calculation, same simplification competition.py and development_activity.py
    already make for their own distance calculations in this codebase, not
    a network-shortest-path accessibility estimate.
  - Projects with completion_pct set are used only to nudge
    effective_delivery_probability for under_construction projects
    specifically (see point 2) -- start_date/end_date are surfaced in
    evidence as months_to_expected_completion for context but are NOT used
    to weight the score, since a published end_date is exactly the figure
    Flyvbjerg's delay literature (point 2) shows is least reliable.
    Documented, not implemented, extension.

KNOWN UPSTREAM GAP (flagged like every other engine flags gaps it depends
on but doesn't own): tasks.py's generate_business_report does not
currently call Engine 13 (or Engine 12) at all, and BusinessReport has no
infrastructure_change_score field -- the same "wiring is a separate,
out-of-scope task" gap development_activity.py's own module docstring
already documents for itself.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import List, Optional, Sequence, Tuple

from scipy.stats import norm

logger = logging.getLogger("business_intel")

# =============================================================================
# Constants
# =============================================================================

ENGINE_NUMBER = 13

# Real subset of InfrastructureProject.PROJECT_TYPE_CHOICES -- physical
# infrastructure with an accessibility/serviceability effect. Excludes
# residential/commercial/industrial "development" types, which are Engine
# 12's conceptual territory (see module docstring's "scope boundary" note).
MOBILITY_TYPES = ("road", "bridge", "interchange", "rail", "brt")
UTILITY_TYPES = ("water", "sewerage", "electricity", "fibre")
INFRASTRUCTURE_PROJECT_TYPES = MOBILITY_TYPES + UTILITY_TYPES

# Statuses that represent a still-uncertain FUTURE change in accessibility.
# "completed" is excluded from the forward-looking score -- a completed
# project is already reflected in the cell's current, actually-observed
# accessibility (which this engine does not itself measure -- Engine 4/
# mobility_traffic and Engine 16/visibility_access own that), so scoring it
# here would double-count a change that has already happened. "cancelled"
# is excluded outright (zero probability of ever delivering).
FORWARD_LOOKING_STATUSES = ("proposed", "approved", "under_construction", "stalled")

# Widest search radius -- matches models.DISTANCE_BAND_METERS' widest band,
# same "don't invent a new spatial constant when a codebase one already
# exists at this scale" convention as development_activity.py's radii.
SEARCH_RADIUS_M = 10_000

# Hansen (1959)-style exponential distance decay scales, in meters, per
# module docstring point 1. Distinct scales for mobility (corridor/network
# reach) vs. utility (local serviceability) infrastructure.
ROAD_RAIL_DECAY_M = 3_000
# Anchored to Guerra, Cervero & Tischler (2012)'s ~800m population-catchment
# figure; not a validated utility-specific measurement, see module docstring.
UTILITY_DECAY_M = 800

# Ordinal, documented-uncalibrated reach multiplier within the mobility
# group -- an interchange/rail investment plausibly has broader commercial
# reach than a single road segment; a bridge sits between the two. Utility
# types are given a flat-ish multiplier since this table does not
# distinguish e.g. a small local water main from a trunk line. Engineering
# judgment, not a citable magnitude -- same status as
# development_activity.py's own SCORE_SATURATION_SCALE.
REACH_MULTIPLIER_BY_TYPE = {
    "interchange": 1.0,
    "rail": 0.9,
    "brt": 0.75,
    "bridge": 0.7,
    "road": 0.55,
    "electricity": 0.5,
    "water": 0.4,
    "fibre": 0.35,
    "sewerage": 0.3,
}

# Per module docstring point 2 -- documented, ordinally-grounded-in-
# Flyvbjerg-but-uncalibrated status-based delivery probabilities.
STATUS_DELIVERY_PROBABILITY = {
    "proposed": 0.20,
    "approved": 0.45,
    "under_construction": 0.80,
    "stalled": 0.15,
}

# For under_construction projects only: completion_pct nudges
# effective_delivery_probability upward toward 1.0, since a project 90%
# complete is closer to certain delivery than one just broken ground even
# though both share the same status label. Blend weight is an engineering
# choice (monotonic, bounded in [status_prob, 1.0]), not a citable constant.
COMPLETION_PCT_BLEND_WEIGHT = 0.5

# Ray (2017)'s measured radius within which LA Metro construction
# significantly reduced adjacent business survival -- reused directly as
# this module's disruption-flag radius (module docstring point 3), not
# re-derived.
DISRUPTION_RADIUS_M = 400

# Below this many nearby forward-looking projects, the normal-approximation
# 95% interval on the sum (module docstring's "AGGREGATION" section) is
# flagged as a weak approximation rather than trusted at face value --
# engineering threshold, same role as development_activity.py's
# MIN_BANDS_FOR_GRADIENT_FIT.
MIN_PROJECTS_FOR_NORMAL_APPROX = 5

# scipy.stats.norm.ppf(0.975), i.e. the exact two-sided 95% z-value --
# computed once at import time rather than hardcoded to a rounded literal
# (same convention as development_activity.py's WILSON_Z_95).
Z_95 = float(norm.ppf(0.975))

# Saturating-curve scale for the uncalibrated 0-100 score transform -- see
# module docstring's "FRAMING THE 0-100 SCORE HONESTLY". Larger SCALE = a
# given expected impact maps to a lower score (curve saturates more slowly).
SCORE_SATURATION_SCALE = 1.4

FEATURE_NAME_EXPECTED_IMPACT = "raw_expected_accessibility_change"


# =============================================================================
# Pure data structures -- no Django/network import at module scope, so this
# file's math is unit-testable without a DB, matching competition.py's and
# development_activity.py's "pure-math layer above without a DB" split.
# =============================================================================

@dataclass
class ProjectObservation:
    """One InfrastructureProject, already resolved to a straight-line
    distance from the query cell, ready for pure-math scoring."""
    project_id: str
    name: str
    project_type: str
    status: str
    distance_m: float
    completion_pct: Optional[float] = None
    end_date: Optional[date] = None
    is_stale: bool = False

    @property
    def decay_m(self) -> float:
        return ROAD_RAIL_DECAY_M if self.project_type in MOBILITY_TYPES else UTILITY_DECAY_M

    @property
    def reach_multiplier(self) -> float:
        return REACH_MULTIPLIER_BY_TYPE.get(self.project_type, 0.3)

    @property
    def magnitude_if_delivered(self) -> float:
        """Hansen (1959)-style decayed impact, conditional on the project
        actually delivering (see effective_delivery_probability for
        P(deliver))."""
        return self.reach_multiplier * math.exp(-self.distance_m / self.decay_m)

    @property
    def is_disruption_risk(self) -> bool:
        """Per Ray (2017), module docstring point 3: an under_construction
        mobility-type project within DISRUPTION_RADIUS_M is a documented
        near-term risk to adjacent business survival, independent of its
        long-run positive accessibility effect."""
        return (
            self.status == "under_construction"
            and self.project_type in MOBILITY_TYPES
            and self.distance_m <= DISRUPTION_RADIUS_M
        )


@dataclass
class ProjectContribution:
    observation: ProjectObservation
    effective_delivery_probability: float
    magnitude_if_delivered: float

    @property
    def expected_contribution(self) -> float:
        return self.magnitude_if_delivered * self.effective_delivery_probability

    @property
    def contribution_variance(self) -> float:
        p = self.effective_delivery_probability
        return (self.magnitude_if_delivered ** 2) * p * (1 - p)


@dataclass
class InfrastructureChangeResult:
    contributions: List[ProjectContribution]
    n_forward_looking_projects: int
    n_stale_projects: int
    raw_expected_accessibility_change: Optional[float]
    expected_change_variance: float
    expected_change_low: Optional[float]
    expected_change_high: Optional[float]
    normal_approx_reliable: bool
    infrastructure_change_score: Optional[float]
    infrastructure_change_score_low: Optional[float]
    infrastructure_change_score_high: Optional[float]
    disruption_risk_projects: List[ProjectObservation]
    dominant_project: Optional[ProjectObservation]
    recently_completed_count: int
    warnings: List[str] = field(default_factory=list)


# =============================================================================
# Real, cited statistical primitives
# =============================================================================

def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Duplicated locally rather than imported -- same 'import for
    plumbing, duplicate for independent statistics' convention
    development_activity.py/competition.py already establish for this
    exact function in this codebase."""
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def effective_delivery_probability(status: str, completion_pct: Optional[float]) -> float:
    """P(this project's impact actually materializes), per module docstring
    point 2. For under_construction projects, completion_pct blends the
    status-level base probability upward toward 1.0 (monotonic, bounded) --
    a project 90% complete is closer to certain delivery than one just
    broken ground, even though both carry the same status label. All other
    statuses use the flat STATUS_DELIVERY_PROBABILITY (no completion_pct
    signal is meaningful for e.g. 'proposed')."""
    base = STATUS_DELIVERY_PROBABILITY.get(status)
    if base is None:
        raise ValueError(f"'{status}' is not a forward-looking status with a defined delivery probability.")
    if status == "under_construction" and completion_pct is not None:
        frac = max(0.0, min(100.0, float(completion_pct))) / 100.0
        return base + (1.0 - base) * frac * COMPLETION_PCT_BLEND_WEIGHT
    return base


def score_from_expected_impact(expected_impact: Optional[float]) -> Optional[float]:
    """Monotonic, explicitly uncalibrated saturating transform to 0-100 --
    see module docstring's 'FRAMING THE 0-100 SCORE HONESTLY' section."""
    if expected_impact is None:
        return None
    return round(100.0 * (1.0 - math.exp(-max(0.0, expected_impact) / SCORE_SATURATION_SCALE)), 2)


# =============================================================================
# Assembly: pure-math result builder (no Django import -- unit-testable
# directly, matching competition.py's / development_activity.py's
# compute_*_result / compute_*_score split)
# =============================================================================

def compute_infrastructure_change_result(
    observations: Sequence[ProjectObservation],
    recently_completed_count: int = 0,
) -> InfrastructureChangeResult:
    warnings: List[str] = []

    forward_looking = [o for o in observations if o.status in FORWARD_LOOKING_STATUSES]
    n_stale = sum(1 for o in forward_looking if o.is_stale)

    if n_stale:
        warnings.append(
            f"{n_stale} of {len(forward_looking)} nearby forward-looking project record(s) are stale "
            "per their own StaleAwareModel staleness window -- included in scoring (this is internal "
            "government/administrative data, not a live feed, so staleness here means 'not recently "
            "re-verified,' not 'known wrong'), but reported so a reader can weigh that."
        )

    if not forward_looking:
        warnings.append(
            f"No proposed/approved/under_construction/stalled InfrastructureProject records found "
            f"within {SEARCH_RADIUS_M}m -- this may reflect genuinely no planned infrastructure "
            "investment nearby, or simply that this system's InfrastructureProject table has no "
            "records for this area yet. Treat a low/undefined score as low-confidence, not as "
            "evidence of stagnation, same caveat development_activity.py applies to its own sparse-"
            "data case."
        )
        return InfrastructureChangeResult(
            contributions=[], n_forward_looking_projects=0, n_stale_projects=n_stale,
            raw_expected_accessibility_change=None, expected_change_variance=0.0,
            expected_change_low=None, expected_change_high=None, normal_approx_reliable=False,
            infrastructure_change_score=None, infrastructure_change_score_low=None,
            infrastructure_change_score_high=None, disruption_risk_projects=[], dominant_project=None,
            recently_completed_count=recently_completed_count, warnings=warnings,
        )

    contributions = [
        ProjectContribution(
            observation=o,
            effective_delivery_probability=effective_delivery_probability(o.status, o.completion_pct),
            magnitude_if_delivered=o.magnitude_if_delivered,
        )
        for o in forward_looking
    ]

    expected = sum(c.expected_contribution for c in contributions)
    variance = sum(c.contribution_variance for c in contributions)
    std = math.sqrt(variance)
    low = max(0.0, expected - Z_95 * std)
    high = expected + Z_95 * std

    normal_approx_reliable = len(contributions) >= MIN_PROJECTS_FOR_NORMAL_APPROX
    if not normal_approx_reliable:
        warnings.append(
            f"Only {len(contributions)} forward-looking project(s) nearby (< "
            f"{MIN_PROJECTS_FOR_NORMAL_APPROX}) -- the normal-approximation 95% interval on expected "
            "accessibility change is a weak approximation at this sample size (the Central Limit "
            "Theorem it relies on describes behavior as the number of independent contributions "
            "grows), not a precise interval. Reported anyway, but treat the width as indicative, "
            "not exact."
        )

    disruption_risk_projects = [o for o in forward_looking if o.is_disruption_risk]
    if disruption_risk_projects:
        names = ", ".join(o.name for o in disruption_risk_projects[:5])
        warnings.append(
            f"{len(disruption_risk_projects)} under_construction mobility-infrastructure project(s) "
            f"within {DISRUPTION_RADIUS_M}m ({names}) -- Ray (2017, Journal of Transport and Land Use) "
            "found significantly lower business survival within this radius during comparable transit "
            "construction. This is a real near-term risk signal, reported here but NOT subtracted from "
            "infrastructure_change_score (see module docstring's 'FRAMING THE 0-100 SCORE HONESTLY') "
            "since no calibrated magnitude exists to net it against the long-run positive effect."
        )

    dominant_project = (
        max(contributions, key=lambda c: c.expected_contribution).observation if contributions else None
    )

    score = score_from_expected_impact(expected)
    score_low = score_from_expected_impact(low)
    score_high = score_from_expected_impact(high)

    return InfrastructureChangeResult(
        contributions=contributions,
        n_forward_looking_projects=len(forward_looking),
        n_stale_projects=n_stale,
        raw_expected_accessibility_change=expected,
        expected_change_variance=variance,
        expected_change_low=low,
        expected_change_high=high,
        normal_approx_reliable=normal_approx_reliable,
        infrastructure_change_score=score,
        infrastructure_change_score_low=score_low,
        infrastructure_change_score_high=score_high,
        disruption_risk_projects=disruption_risk_projects,
        dominant_project=dominant_project,
        recently_completed_count=recently_completed_count,
        warnings=warnings,
    )


def infrastructure_change_evidence(result: InfrastructureChangeResult, query_date: Optional[date] = None) -> dict:
    """Structured evidence dict, matching the shape FeatureObservation.value_json
    expects and the same field-naming style as development_activity_evidence()
    / competition_evidence(): every number a human (or the Decision engine)
    would need to see WHY the score came out the way it did."""
    query_date = query_date or date.today()

    def _months_out(end_date: Optional[date]) -> Optional[int]:
        if end_date is None:
            return None
        return max(0, (end_date.year - query_date.year) * 12 + (end_date.month - query_date.month))

    return {
        "n_forward_looking_projects": result.n_forward_looking_projects,
        "n_stale_projects": result.n_stale_projects,
        "recently_completed_count": result.recently_completed_count,
        "raw_expected_accessibility_change": (
            round(result.raw_expected_accessibility_change, 4)
            if result.raw_expected_accessibility_change is not None else None
        ),
        "expected_accessibility_change_95ci": [
            round(result.expected_change_low, 4) if result.expected_change_low is not None else None,
            round(result.expected_change_high, 4) if result.expected_change_high is not None else None,
        ],
        "normal_approx_reliable": result.normal_approx_reliable,
        "infrastructure_change_score_0_100": result.infrastructure_change_score,
        "infrastructure_change_score_95ci": [
            result.infrastructure_change_score_low, result.infrastructure_change_score_high,
        ],
        "dominant_project": (
            {
                "name": result.dominant_project.name,
                "project_type": result.dominant_project.project_type,
                "status": result.dominant_project.status,
                "distance_m": round(result.dominant_project.distance_m, 1),
                "months_to_expected_completion": _months_out(result.dominant_project.end_date),
            } if result.dominant_project else None
        ),
        "disruption_risk_projects": [
            {"name": o.name, "project_type": o.project_type, "distance_m": round(o.distance_m, 1)}
            for o in result.disruption_risk_projects
        ],
        "projects": [
            {
                "name": c.observation.name,
                "project_type": c.observation.project_type,
                "status": c.observation.status,
                "distance_m": round(c.observation.distance_m, 1),
                "completion_pct": (
                    float(c.observation.completion_pct) if c.observation.completion_pct is not None else None
                ),
                "months_to_expected_completion": _months_out(c.observation.end_date),
                "effective_delivery_probability": round(c.effective_delivery_probability, 3),
                "magnitude_if_delivered": round(c.magnitude_if_delivered, 4),
                "expected_contribution": round(c.expected_contribution, 4),
                "is_disruption_risk": c.observation.is_disruption_risk,
                "is_stale": c.observation.is_stale,
            }
            for c in result.contributions
        ],
        "methods": [
            "hansen_1959_exponential_distance_decay",
            "status_based_bernoulli_delivery_probability",
            "independent_bernoulli_sum_expectation_and_variance",
            "normal_approximation_interval",
        ],
        "warnings": result.warnings,
    }


# =============================================================================
# Django-facing entry point
# =============================================================================

METRIC_FIELDS = [
    (FEATURE_NAME_EXPECTED_IMPACT, "decayed_impact_units", "numeric"),
    ("infrastructure_change_score", "index_0_100", "numeric"),
]


def _load_project_observations(cell, radius_m: int = SEARCH_RADIUS_M) -> Tuple[List[ProjectObservation], int]:
    """Real Django-facing query over InfrastructureProject, resolved to
    straight-line distance from the cell (see module docstring's
    'no routed accessibility' note). Local import keeps the pure-math layer
    above DB-import-free, matching this module's established split."""
    from ..models import InfrastructureProject

    lat, lng = float(cell.center_latitude), float(cell.center_longitude)

    # A generous bounding box first (cheap index-friendly prefilter), exact
    # haversine distance second -- same two-step pattern this codebase's
    # other radius-based engines use rather than a full-table scan.
    deg_pad = radius_m / 111_000.0 + 0.05
    candidates = InfrastructureProject.objects.filter(
        project_type__in=INFRASTRUCTURE_PROJECT_TYPES,
        latitude__isnull=False,
        longitude__isnull=False,
        latitude__gte=Decimal(str(lat - deg_pad)),
        latitude__lte=Decimal(str(lat + deg_pad)),
        longitude__gte=Decimal(str(lng - deg_pad)),
        longitude__lte=Decimal(str(lng + deg_pad)),
    ).exclude(status="cancelled")

    observations: List[ProjectObservation] = []
    recently_completed_count = 0
    for p in candidates:
        distance_m = _haversine_m(lat, lng, float(p.latitude), float(p.longitude))
        if distance_m > radius_m:
            continue
        if p.status == "completed":
            recently_completed_count += 1
            continue
        observations.append(ProjectObservation(
            project_id=str(p.id),
            name=p.name,
            project_type=p.project_type,
            status=p.status,
            distance_m=distance_m,
            completion_pct=float(p.completion_pct) if p.completion_pct is not None else None,
            end_date=p.end_date,
            is_stale=bool(getattr(p, "is_stale", False)),
        ))
    return observations, recently_completed_count


def compute_infrastructure_change_score(query, radius_m: int = SEARCH_RADIUS_M) -> tuple:
    """Real Django-facing Engine 13 entry point, matching the (score,
    evidence) tuple shape compute_competition_score / compute_development_activity_score
    already return. Writes its own FeatureObservation rows and an EngineRun
    row, matching those engines' run() contract."""
    from ..models import EngineRun, FeatureObservation
    from django.utils import timezone as _timezone

    started_at = _timezone.now()
    cell = query.location_cell

    run_row = EngineRun.objects.create(
        query=query, engine_number=ENGINE_NUMBER, status="succeeded", started_at=started_at,
    )

    try:
        observations, recently_completed_count = _load_project_observations(cell, radius_m=radius_m)
    except Exception as exc:  # pragma: no cover - defensive, mirrors other engines' failure handling
        msg = f"Failed to load InfrastructureProject records: {exc}"
        logger.warning("Infrastructure Change engine: %s (cell=%s)", msg, cell.geohash)
        run_row.status = "failed"
        run_row.error_message = msg
        run_row.finished_at = _timezone.now()
        run_row.save(update_fields=["status", "error_message", "finished_at"])
        return None, {"warnings": [msg], "engine_run_status": "failed"}

    result = compute_infrastructure_change_result(observations, recently_completed_count=recently_completed_count)
    evidence = infrastructure_change_evidence(result, query_date=started_at.date())
    evidence["engine_run_status"] = (
        "ok" if result.raw_expected_accessibility_change is not None else "ok_no_nearby_projects"
    )

    features_written = 0
    for field_name, unit, value_type in METRIC_FIELDS:
        value = getattr(result, field_name)
        if value is None:
            continue
        uncertainty = None
        if field_name == "infrastructure_change_score" and result.infrastructure_change_score_low is not None:
            uncertainty = (result.infrastructure_change_score_high - result.infrastructure_change_score_low) / 2.0
        FeatureObservation.write_latest(
            location_cell=cell, engine_number=ENGINE_NUMBER, feature_name=field_name,
            defaults=dict(
                value_type=value_type,
                value_numeric=Decimal(str(round(value, 6))),
                unit=unit,
                observed_at=started_at,
                source="business_intel.engines.infrastructure_change",
                source_class="internal_derived",
                method=(
                    "hansen_decay_bernoulli_expectation" if field_name == FEATURE_NAME_EXPECTED_IMPACT
                    else "saturating_transform_of_expected_impact"
                ),
                derivation_level="calculation" if field_name == FEATURE_NAME_EXPECTED_IMPACT else "model_estimate",
                sample_size=result.n_forward_looking_projects,
                uncertainty=Decimal(str(round(uncertainty, 6))) if uncertainty is not None else None,
            ),
        )
        features_written += 1

    finished_at = _timezone.now()
    run_row.finished_at = finished_at
    run_row.duration_ms = int((finished_at - started_at).total_seconds() * 1000)
    run_row.features_written = features_written
    if result.warnings:
        run_row.status = "degraded"
        run_row.degraded_reason = "; ".join(sorted(set(result.warnings)))[:255]
    run_row.save(update_fields=["status", "finished_at", "duration_ms", "features_written", "degraded_reason"])

    return result.infrastructure_change_score, evidence


def run(query, radius_m: int = SEARCH_RADIUS_M) -> dict:
    """Engine 13 entry point. Thin wrapper around
    compute_infrastructure_change_score(query, radius_m) -- see that
    function's docstring for what it actually computes/persists. Returns a
    dict shaped like every other wired engine's run() (score, evidence,
    engine_run), so tasks.py's _run_engine() can call it interchangeably
    with competition.run() / development_activity.run() / etc. (Not
    currently called by tasks.py -- see module docstring's 'KNOWN UPSTREAM
    GAP'.)"""
    from ..models import EngineRun

    cell = query.location_cell
    score, evidence = compute_infrastructure_change_score(query, radius_m=radius_m)

    run_row = (
        EngineRun.objects.filter(query=query, engine_number=ENGINE_NUMBER)
        .order_by("-started_at")
        .first()
    )

    return {"cell": cell, "score": score, "evidence": evidence, "engine_run": run_row}