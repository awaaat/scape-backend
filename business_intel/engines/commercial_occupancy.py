"""
business_intel/engines/commercial_occupancy.py

Engine 14 -- Commercial Occupancy Engine
Per business_location_intelligence_engines_data_models_v2.md, Section 14.

NOTE ON THE SPEC FILE: same situation as every other engine in this
codebase -- business_location_intelligence_engines_data_models_v2.md is
referenced but not present on disk. Built to match Engine 6/7's structure,
schema contract, and citation discipline (both extracted and read in full
before writing this module).

=============================================================================
SCOPE BOUNDARY VS. ENGINE 6 AND ENGINE 7 (read this before extending)
=============================================================================
Engine 6 (business_ecosystem.py) asks "is the catchment's business
POPULATION, as a whole, growing, shrinking, or churning" -- an aggregate
entry/exit RATE computed from BusinessLifecycleEvent counts, using
business identity (opened/closed_confirmed/censored_last_seen) as the
unit of analysis.

Engine 7 (business_survival.py) asks "how long does a GIVEN business tend
to last, and which traits predict it" -- individual-level time-to-event
modeling (Kaplan-Meier, Cox PH) over the same BusinessLifecycleEvent
history, business-identity-scoped.

This engine asks a third, genuinely different question: "is the
commercial SPACE in this catchment being absorbed or left vacant, and
how fast does it cycle between occupied and vacant?" That is a
space-utilization question, not a business-identity question -- and it
is deliberately built on a DIFFERENT primary data source:
models.BusinessSnapshot, not models.BusinessLifecycleEvent.

Why BusinessSnapshot and not BusinessLifecycleEvent: a BusinessSnapshot
row is written on every report run via services.sync_places_to_
business_entities() (status/rating/review_count/observed_at), so it is
a dense, regularly-sampled time series per business -- exactly the
repeated-measures structure needed to detect a STATUS TRANSITION
(operational -> closed_temporarily -> operational again, or ->
closed_permanently). BusinessLifecycleEvent rows, by contrast, are
sparse birth/death markers (Engine 6/7's unit of analysis) and do not
capture temporary-closure-and-reopening cycles at all -- there is no
"reopened" event_type in BusinessLifecycleEvent.EVENT_TYPE_CHOICES.
Vacancy cycling is only visible in the snapshot history.

A real, honestly-documented limitation follows directly from this
codebase's data model: a BusinessEntity is anchored to one Google
Places place_id (see BusinessEntity.place_id, unique=True). If a
physical retail unit closes and a genuinely NEW tenant opens in the
same space, Places assigns a new place_id and this system sees a new
BusinessEntity with its own first_observed_at -- there is no
address-level "commercial space" identity distinct from the business
occupying it. So "occupancy" here is necessarily measured two ways that
do NOT require space-level identity:
  (a) a cross-sectional VACANCY RATE (what fraction of tracked
      addresses are non-operational right now), and
  (b) TURNOVER INTENSITY / temporary-vacancy-spell duration measured
      per BUSINESS (does a given tenant's status flip between
      operational and closed_temporarily, and for how long) --
      genuine re-tenanting by a different business at the same
      physical address is invisible to this engine and is flagged as
      a known simplification, not silently assumed away.

=============================================================================
WHAT THIS ENGINE ACTUALLY MEASURES
=============================================================================
Four real, separately-cited questions:

  1. HOW MUCH of the catchment's tracked commercial space is vacant
     RIGHT NOW? -- a cross-sectional proportion (closed_temporarily +
     closed_permanently, over all businesses with a known status) with
     a Wilson (1927) score confidence interval, same interval family
     development_activity.py uses for the same small-n-proportion
     reason (Wald intervals misbehave at the sample sizes typical of a
     single catchment).

  2. HOW FAST does occupancy CYCLE? -- an occurrence/exposure turnover
     intensity (Elandt-Johnson & Johnson 1980; standard demographic
     rate estimator): count of real observed status transitions in the
     snapshot history, divided by total business-time-at-risk observed
     (person-years, in the demographic-rate sense) in the window. This
     is a rate, not a proportion, and is reported per business-year so
     catchments with different numbers of tracked businesses and
     different snapshot histories are comparable.

  3. WHEN a business goes into closed_temporarily, HOW LONG before it
     reopens (is re-absorbed), if ever? -- a cause-specific Kaplan-
     Meier (Kaplan & Meier 1958; cause-specific hazard convention per
     Prentice et al. 1978 / Putter et al. 2007 for competing risks)
     product-limit estimator over "vacancy spells" (onset =
     operational -> closed_temporarily transition, event of interest =
     reopening, competing event = closed_permanently conversion,
     right-censoring = still closed_temporarily as of the observation
     cutoff). Reports median reabsorption time, or "not reached" if
     fewer than half the tracked spells have resolved by re-opening --
     the same honest non-collapse behavior business_survival.py uses
     for median survival time.

  4. DOES VACANCY CLUSTER SPATIALLY, or is it scattered at random
     across the catchment? -- a Cliff & Ord (1981) binary join-count
     test (BB joins, "black-black" = vacant-vacant), computed over a
     symmetrized k-nearest-neighbor contiguity graph (this codebase has
     point locations, not polygon adjacency, so k-NN symmetric
     contiguity is the standard substitute -- same "points, not
     polygons" situation business_ecosystem.py's Clark-Evans test is
     already built for). A positive, significant z-score means vacant
     units are physically clustered together (a real "dead zone"
     forming, not just an elevated rate scattered evenly); a
     non-significant result means the same vacancy rate is spread
     evenly through the catchment -- two catchments with an identical
     vacancy_rate can have very different implications depending on
     this result, which is exactly why it is reported as its own
     feature and not folded silently into the rate.

A composite occupancy_stability_score_0_100 blends (1), (2), and (4) --
same weighted-blend-with-renormalization-on-missing-components
convention as business_ecosystem.py's ecosystem_vitality_score_0_100 --
into a single business_conclusion-level signal.

=============================================================================
WHAT'S ACTUALLY AVAILABLE VS. WHAT ISN'T
=============================================================================
Real data used: models.BusinessEntity (category, brand, status, lat/lon)
and models.BusinessSnapshot (status/rating/review_count/observed_at,
one row per report run per business -- see BusinessSnapshot's own
docstring: "one observation of a BusinessEntity at one point in time.
Append-only."). No external API calls in this module -- purely a real
read across data this codebase's own report runs have already
accumulated, same "genuinely independent, DB-only scope" convention as
Engine 6/7.

Real, documented gaps (reported as warnings on the affected feature, per
this codebase's evidence-dict convention -- not silently dropped):
  - Snapshot cadence is tied to report-run frequency, not a fixed
    sampling interval. A business observed only twice, six months
    apart, contributes one very coarse transition-or-not data point --
    a real status flip that happened between those two snapshots (e.g.
    closed and reopened within the gap) is invisible. This under-counts
    turnover_intensity and vacancy-spell counts; both are honestly
    reported as lower bounds, same "lower/lagged bound" framing
    business_ecosystem.py already uses for its own observation-derived
    entry_rate.
  - No address-level "commercial space" identity independent of
    BusinessEntity/place_id -- see the SCOPE BOUNDARY section above.
    Genuine re-tenanting reads as one business's closed_permanently
    event plus, if the new tenant is ever Places-indexed nearby, an
    unrelated new BusinessEntity -- not a linked "space re-occupied"
    event. This engine does not attempt to infer that link.
  - The competing-risks estimator here is CAUSE-SPECIFIC (treats the
    competing event as censoring), not the full Aalen-Johansen
    cumulative-incidence estimator -- a standard, named simplification
    (Putter et al. 2007 discuss exactly this tradeoff), not an
    implementation shortcut hidden from the reader.

Depends on business_intel.models only (BusinessEntity, BusinessSnapshot,
BusinessLocationCell, Catchment, BusinessQuery, EngineRun,
FeatureObservation). No dependency on any other engine's feature-store
output or any external API.
"""

from __future__ import annotations

import logging
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Optional, Sequence

from django.db import transaction
from django.utils import timezone as django_timezone

from ..models import (
    BusinessEntity, BusinessSnapshot, BusinessLocationCell,
    Catchment, BusinessQuery, EngineRun, FeatureObservation,
)

logger = logging.getLogger("business_intel.engines.commercial_occupancy")

ENGINE_NUMBER = 14

VACANT_STATUSES = frozenset({"closed_temporarily", "closed_permanently"})
KNOWN_STATUSES = frozenset({"operational", "closed_temporarily", "closed_permanently"})

MIN_N_FOR_VACANCY_RATE = 5          # below this, a Wilson interval on the rate is too wide to be worth reporting
MIN_BUSINESS_YEARS_FOR_TURNOVER = 1.0  # below this, an occurrence/exposure rate has almost no exposure to divide by
MIN_SPELLS_FOR_REABSORPTION_KM = 5     # same small-sample discipline as business_survival's MIN_N_FOR_KAPLAN_MEIER
MIN_N_FOR_JOIN_COUNT = 8               # same order as business_ecosystem's MIN_N_FOR_CLARK_EVANS
K_NEIGHBORS_FOR_CONTIGUITY = 4         # standard default k for k-NN spatial-weights construction (Cliff & Ord practice)

Z_975 = 1.959963985                # standard normal 97.5th percentile (hardcoded, no scipy -- same as business_survival.py)

# Snapshots are cheap (no per-call API cost, produced as a byproduct of
# every report run) so this can be generous, same reasoning
# business_survival.py gives for its 5-year DEFAULT_OBSERVATION_WINDOW_DAYS.
DEFAULT_OBSERVATION_WINDOW_DAYS = 2 * 365


# ============================================================================
# Numerically-independent helpers. Duplicated (not imported) from
# business_survival.py/business_ecosystem.py per this codebase's established
# per-engine independent-footprint convention.
# ============================================================================

def _standard_normal_cdf(z: float) -> float:
    """Phi(z) via the error function -- no scipy in this environment."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters."""
    earth_radius_m = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * earth_radius_m * math.asin(math.sqrt(a))


def wilson_score_interval(successes: int, n: int, z: float = Z_975) -> tuple:
    """Wilson (1927) score interval for a binomial proportion. Same choice,
    same justification, as development_activity.py's identical function
    (Wald intervals misbehave in the small-n regime this engine operates
    in) -- duplicated locally rather than imported, per convention."""
    if n <= 0:
        return None, None
    p = successes / n
    denom = 1 + (z ** 2) / n
    center = (p + (z ** 2) / (2 * n)) / denom
    margin = (z / denom) * math.sqrt((p * (1 - p) / n) + (z ** 2) / (4 * n ** 2))
    return max(0.0, center - margin), min(1.0, center + margin)


def _as_date(value) -> date:
    return value.date() if isinstance(value, datetime) else value


# ============================================================================
# Pure-math layer 1: cross-sectional vacancy rate.
# ============================================================================

@dataclass
class VacancyRateResult:
    n_known_status: int
    n_vacant: int
    n_operational: int
    n_unknown_excluded: int
    vacancy_rate: float
    ci_lower: Optional[float]
    ci_upper: Optional[float]
    warnings: list = field(default_factory=list)


def compute_vacancy_rate(businesses: Sequence) -> Optional[VacancyRateResult]:
    """businesses: BusinessEntity rows already scoped to one catchment.
    Uses BusinessEntity.current_status (the latest-known status; a real,
    already-maintained field, not re-derived from snapshots here) as the
    cross-sectional observation. 'unknown' (not re-observed recently) is
    excluded from the denominator -- it is a real missing-data state, not
    evidence either way, same discipline BusinessSnapshot/
    CategoryEnrichmentLog use to distinguish "checked, none found" from
    "never checked"."""
    total = len(businesses)
    if total == 0:
        return None

    warnings = []
    status_counts = Counter(b.current_status for b in businesses)
    n_unknown = status_counts.get("unknown", 0)
    n_vacant = sum(status_counts.get(s, 0) for s in VACANT_STATUSES)
    n_operational = status_counts.get("operational", 0)
    n_known = n_vacant + n_operational

    if n_known == 0:
        warnings.append("Every tracked business in this catchment has current_status='unknown' -- vacancy rate withheld (no known-status denominator).")
        return VacancyRateResult(
            n_known_status=0, n_vacant=0, n_operational=0, n_unknown_excluded=n_unknown,
            vacancy_rate=None, ci_lower=None, ci_upper=None, warnings=warnings,
        )

    rate = n_vacant / n_known
    ci_lower, ci_upper = (None, None)
    if n_known < MIN_N_FOR_VACANCY_RATE:
        warnings.append(f"Only {n_known} businesses with a known status (< {MIN_N_FOR_VACANCY_RATE}) -- vacancy_rate reported but its Wilson interval is withheld as too unstable to be useful.")
    else:
        ci_lower, ci_upper = wilson_score_interval(n_vacant, n_known)

    if n_unknown:
        frac_unknown = n_unknown / total
        if frac_unknown >= 0.25:
            warnings.append(f"{n_unknown} of {total} tracked businesses ({frac_unknown:.0%}) have status 'unknown' (stale, not re-observed recently) and are excluded from the rate -- the true current vacancy rate could differ materially from what's reported here.")

    return VacancyRateResult(
        n_known_status=n_known, n_vacant=n_vacant, n_operational=n_operational,
        n_unknown_excluded=n_unknown, vacancy_rate=round(rate, 6),
        ci_lower=round(ci_lower, 6) if ci_lower is not None else None,
        ci_upper=round(ci_upper, 6) if ci_upper is not None else None,
        warnings=warnings,
    )


# ============================================================================
# Pure-math layer 2: snapshot-derived status transitions and turnover
# intensity (occurrence/exposure rate).
# ============================================================================

@dataclass
class SnapshotTransition:
    business_id: object
    from_status: str
    to_status: str
    approx_date: date   # midpoint of the (from_snapshot, to_snapshot) gap -- the true flip date is unobserved


def _build_snapshot_transitions(snapshots_by_business: dict) -> list:
    """One SnapshotTransition per adjacent pair of snapshots (already
    ordered by observed_at) whose status differs. A business with only
    one snapshot in the window contributes no transitions -- it isn't
    excluded from exposure time, just from the transition count (see
    compute_turnover_intensity)."""
    transitions = []
    for business_id, snaps in snapshots_by_business.items():
        ordered = sorted(snaps, key=lambda s: s.observed_at)
        for prev, curr in zip(ordered, ordered[1:]):
            if prev.status != curr.status and prev.status in KNOWN_STATUSES and curr.status in KNOWN_STATUSES:
                gap = curr.observed_at - prev.observed_at
                approx_date = _as_date(prev.observed_at + gap / 2)
                transitions.append(SnapshotTransition(
                    business_id=business_id, from_status=prev.status, to_status=curr.status, approx_date=approx_date,
                ))
    return transitions


@dataclass
class TurnoverIntensityResult:
    businesses_with_snapshots: int
    total_business_years_observed: float
    total_transitions: int
    vacancy_onsets: int          # operational -> closed_temporarily
    reabsorptions: int           # closed_temporarily -> operational
    permanent_conversions: int   # any -> closed_permanently
    turnover_intensity_per_business_year: Optional[float]
    vacancy_onset_rate_per_business_year: Optional[float]
    warnings: list = field(default_factory=list)


def compute_turnover_intensity(
    snapshots_by_business: dict, window_start: date, as_of: date,
) -> Optional[TurnoverIntensityResult]:
    """Occurrence/exposure rate (Elandt-Johnson & Johnson 1980): total
    observed transitions divided by total business-time-at-risk. Exposure
    for a business is measured from its first snapshot in the window (or
    window_start, if its first snapshot predates the window) to its last
    snapshot in the window (or as_of) -- the real span over which this
    system could have observed a transition for that business, whether
    or not one actually occurred."""
    if not snapshots_by_business:
        return None

    warnings = []
    total_days_observed = 0.0
    businesses_with_multi_snapshots = 0

    for business_id, snaps in snapshots_by_business.items():
        ordered = sorted(snaps, key=lambda s: s.observed_at)
        first_dt = max(_as_date(ordered[0].observed_at), window_start)
        last_dt = min(_as_date(ordered[-1].observed_at), as_of)
        span_days = (last_dt - first_dt).days
        if span_days > 0:
            total_days_observed += span_days
        if len(ordered) >= 2:
            businesses_with_multi_snapshots += 1

    total_business_years = total_days_observed / 365.0

    transitions = _build_snapshot_transitions(snapshots_by_business)
    total_transitions = len(transitions)
    vacancy_onsets = sum(1 for t in transitions if t.from_status == "operational" and t.to_status == "closed_temporarily")
    reabsorptions = sum(1 for t in transitions if t.from_status == "closed_temporarily" and t.to_status == "operational")
    permanent_conversions = sum(1 for t in transitions if t.to_status == "closed_permanently")

    turnover_rate = None
    onset_rate = None
    if total_business_years < MIN_BUSINESS_YEARS_FOR_TURNOVER:
        warnings.append(f"Only {total_business_years:.2f} business-years of snapshot exposure observed (< {MIN_BUSINESS_YEARS_FOR_TURNOVER}) -- turnover intensity withheld as too little exposure time to trust a rate.")
    else:
        turnover_rate = round(total_transitions / total_business_years, 4)
        onset_rate = round(vacancy_onsets / total_business_years, 4)

    if businesses_with_multi_snapshots < len(snapshots_by_business):
        n_single = len(snapshots_by_business) - businesses_with_multi_snapshots
        warnings.append(f"{n_single} of {len(snapshots_by_business)} tracked businesses have only one snapshot in the window -- they contribute exposure time but cannot contribute an observed transition, biasing the rate downward (a lower bound, not the true rate).")

    return TurnoverIntensityResult(
        businesses_with_snapshots=len(snapshots_by_business),
        total_business_years_observed=round(total_business_years, 4),
        total_transitions=total_transitions, vacancy_onsets=vacancy_onsets,
        reabsorptions=reabsorptions, permanent_conversions=permanent_conversions,
        turnover_intensity_per_business_year=turnover_rate,
        vacancy_onset_rate_per_business_year=onset_rate,
        warnings=warnings,
    )


# ============================================================================
# Pure-math layer 3: vacancy-spell reabsorption -- cause-specific
# Kaplan-Meier product-limit estimator over closed_temporarily spells.
# ============================================================================

@dataclass
class VacancySpell:
    business_id: object
    onset_date: date
    end_date: date
    duration_days: float
    outcome: str   # "reabsorbed" | "converted_permanent" | "censored"


def _build_vacancy_spells(snapshots_by_business: dict, as_of: date) -> list:
    """A spell begins at the approximate date of an operational ->
    closed_temporarily transition and ends at the first subsequent
    transition out of closed_temporarily (to operational =
    "reabsorbed", to closed_permanently = "converted_permanent"), or is
    right-censored at as_of if the business is still closed_temporarily
    with no further snapshot. Businesses can contribute more than one
    spell (e.g. closes, reopens, closes again) -- each is a real,
    separate observation of vacancy duration, not double-counting the
    same event."""
    spells = []
    for business_id, snaps in snapshots_by_business.items():
        ordered = sorted(snaps, key=lambda s: s.observed_at)
        open_spell_start = None
        for prev, curr in zip(ordered, ordered[1:]):
            if prev.status == "operational" and curr.status == "closed_temporarily":
                gap = curr.observed_at - prev.observed_at
                open_spell_start = _as_date(prev.observed_at + gap / 2)
            elif open_spell_start is not None and prev.status == "closed_temporarily" and curr.status == "operational":
                gap = curr.observed_at - prev.observed_at
                end_date = _as_date(prev.observed_at + gap / 2)
                duration = (end_date - open_spell_start).days
                if duration > 0:
                    spells.append(VacancySpell(business_id, open_spell_start, end_date, float(duration), "reabsorbed"))
                open_spell_start = None
            elif open_spell_start is not None and prev.status == "closed_temporarily" and curr.status == "closed_permanently":
                gap = curr.observed_at - prev.observed_at
                end_date = _as_date(prev.observed_at + gap / 2)
                duration = (end_date - open_spell_start).days
                if duration > 0:
                    spells.append(VacancySpell(business_id, open_spell_start, end_date, float(duration), "converted_permanent"))
                open_spell_start = None

        if open_spell_start is not None:
            last_snap_date = _as_date(ordered[-1].observed_at)
            if ordered[-1].status == "closed_temporarily":
                duration = (as_of - open_spell_start).days
                if duration > 0:
                    spells.append(VacancySpell(business_id, open_spell_start, as_of, float(duration), "censored"))

    return spells


@dataclass
class KMPoint:
    time_days: float
    at_risk: int
    events: int
    survival: float   # here: probability of NOT YET being reabsorbed by this time


@dataclass
class ReabsorptionKMResult:
    n_spells: int
    n_reabsorbed: int
    n_converted_permanent: int
    n_censored: int
    median_reabsorption_days: Optional[float]
    median_reached: bool
    curve: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


def cause_specific_reabsorption_km(spells: Sequence) -> Optional[ReabsorptionKMResult]:
    """Kaplan-Meier product-limit estimator (Kaplan & Meier 1958) of
    time-to-reabsorption, cause-specific convention for the competing
    risk (Prentice et al. 1978): a converted_permanent spell is treated
    as censored AT ITS END DATE for this estimator, same as a spell
    still open at as_of -- both simply stop contributing "still vacant,
    not yet reabsorbed" information from that point on. This estimates
    the cause-specific survival function, not the cumulative-incidence
    function a full competing-risks (Aalen-Johansen) analysis would
    give -- see module docstring's documented-simplification note."""
    n = len(spells)
    if n < MIN_SPELLS_FOR_REABSORPTION_KM:
        return None

    durations = [s.duration_days for s in spells]
    events = [1 if s.outcome == "reabsorbed" else 0 for s in spells]
    n_reabsorbed = sum(events)
    n_converted = sum(1 for s in spells if s.outcome == "converted_permanent")
    n_censored = sum(1 for s in spells if s.outcome == "censored")

    warnings = []
    order = sorted(range(n), key=lambda i: durations[i])
    unique_times = sorted(set(durations[i] for i in order if events[i] == 1))

    survival = 1.0
    curve = []
    at_risk = n
    remaining = sorted(range(n), key=lambda i: durations[i])
    idx = 0
    for t in unique_times:
        while idx < n and durations[remaining[idx]] < t:
            idx += 1
        at_risk_t = sum(1 for i in range(n) if durations[i] >= t)
        events_t = sum(1 for i in range(n) if durations[i] == t and events[i] == 1)
        if at_risk_t == 0:
            continue
        survival *= (1.0 - events_t / at_risk_t)
        curve.append(KMPoint(time_days=t, at_risk=at_risk_t, events=events_t, survival=round(survival, 6)))

    median_days = None
    median_reached = False
    for point in curve:
        if point.survival <= 0.5:
            median_days = point.time_days
            median_reached = True
            break

    if not median_reached:
        warnings.append("Fewer than half of tracked vacancy spells have been reabsorbed by the end of the observation window -- median_reabsorption_days is 'not reached', a real reported outcome, not a missing value.")
    if n_converted:
        warnings.append(f"{n_converted} vacancy spell(s) ended in permanent closure rather than reabsorption -- treated as censored at their conversion date for this cause-specific estimate (see module docstring on the competing-risks simplification).")

    return ReabsorptionKMResult(
        n_spells=n, n_reabsorbed=n_reabsorbed, n_converted_permanent=n_converted, n_censored=n_censored,
        median_reabsorption_days=median_days, median_reached=median_reached, curve=curve, warnings=warnings,
    )


# ============================================================================
# Pure-math layer 4: Cliff & Ord (1981) binary join-count test for
# spatial clustering of vacancy, over a symmetrized k-NN contiguity graph.
# ============================================================================

@dataclass
class JoinCountResult:
    n: int
    n_vacant: int
    n_occupied: int
    k_neighbors: int
    total_joins: int
    observed_bb_joins: int
    expected_bb_joins: float
    variance_bb_joins: Optional[float]
    z_score: Optional[float]
    p_value_two_tailed: Optional[float]
    interpretation: str  # "clustered" | "dispersed" | "not distinguishable from random"
    warnings: list = field(default_factory=list)


def _knn_symmetric_contiguity(points: Sequence, k: int) -> list:
    """Returns an n x n binary adjacency matrix (list of lists):
    w[i][j] = 1 if i and j are contiguous. Symmetrized k-NN graph (i,j
    adjacent if either is among the other's k nearest neighbors) --
    the standard substitute for polygon adjacency when only point
    locations are available, same situation business_ecosystem.py's
    Clark-Evans test is already built for. O(n^2) haversine, fine at
    catchment scale."""
    n = len(points)
    dist = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            d = _haversine_m(points[i][0], points[i][1], points[j][0], points[j][1])
            dist[i][j] = d
            dist[j][i] = d

    w = [[0] * n for _ in range(n)]
    for i in range(n):
        order = sorted((j for j in range(n) if j != i), key=lambda j: dist[i][j])
        for j in order[:k]:
            w[i][j] = 1
            w[j][i] = 1
    return w


def join_count_statistic(points: Sequence, is_vacant: Sequence, k: int = K_NEIGHBORS_FOR_CONTIGUITY) -> Optional[JoinCountResult]:
    """Cliff & Ord (1981) BB (black-black, here vacant-vacant) join-count
    test under non-free sampling (the m vacant labels are a uniformly
    random size-m subset of the n locations, fixed contiguity graph).
    Moments derived directly by exact combinatorics rather than quoted
    from a remembered closed form: for a fixed edge e, P(both endpoints
    vacant) = m(m-1)/(n(n-1)) (a hypergeometric probability); X = number
    of BB edges is a sum of these J dependent Bernoulli indicators, so
    Var(X) = sum_e Var(I_e) + 2*sum_{e<f} Cov(I_e,I_f), split into edge
    pairs sharing one vertex (covariance via the 3-vertex-all-vacant
    hypergeometric probability) vs. sharing no vertex (via the
    4-vertex-all-vacant probability) -- see Q1/Q2 below. Verified
    against a Monte Carlo permutation simulation during development
    (not trusted from memory alone -- same discipline development_
    activity.py's module docstring describes for its own Poisson CI).
    Withheld below MIN_N_FOR_JOIN_COUNT, same discipline as Clark-Evans."""
    n = len(points)
    if n < MIN_N_FOR_JOIN_COUNT:
        return None
    n_vacant = sum(1 for v in is_vacant if v)
    n_occupied = n - n_vacant
    if n_vacant < 2 or n_occupied < 2:
        return None

    k_eff = min(k, n - 1)
    w = _knn_symmetric_contiguity(points, k_eff)

    degrees = [sum(row) for row in w]
    total_joins = sum(degrees) // 2
    if total_joins == 0:
        return None

    observed_bb = 0
    for i in range(n):
        for j in range(i + 1, n):
            if w[i][j] and is_vacant[i] and is_vacant[j]:
                observed_bb += 1

    m = float(n_vacant)
    N = float(n)
    J = float(total_joins)

    p = m * (m - 1) / (N * (N - 1))
    expected_bb = J * p

    variance_bb = None
    z = None
    p_two_tailed = None
    warnings = []

    if n >= 4:
        p3 = m * (m - 1) * (m - 2) / (N * (N - 1) * (N - 2))
        p4 = m * (m - 1) * (m - 2) * (m - 3) / (N * (N - 1) * (N - 2) * (N - 3))
        # Q1 = # unordered edge-pairs sharing exactly one vertex = sum_i C(degree_i, 2)
        # Q2 = # unordered edge-pairs sharing no vertex = C(J,2) - Q1
        Q1 = sum(deg * (deg - 1) / 2.0 for deg in degrees)
        Q2 = (J * (J - 1) / 2.0) - Q1
        variance_bb = J * p * (1 - p) + 2 * Q1 * (p3 - p ** 2) + 2 * Q2 * (p4 - p ** 2)
        if variance_bb > 0:
            z = (observed_bb - expected_bb) / math.sqrt(variance_bb)
            p_two_tailed = 2.0 * (1.0 - _standard_normal_cdf(abs(z)))
        else:
            warnings.append("Computed join-count variance was non-positive (degenerate contiguity structure for this n/k) -- z-score and p-value withheld.")
    else:
        warnings.append("n < 4 -- the join-count variance formula's higher-order terms are undefined; z-score and p-value withheld.")

    if z is not None and p_two_tailed is not None:
        if p_two_tailed < 0.05:
            interpretation = "clustered" if observed_bb > expected_bb else "dispersed"
        else:
            interpretation = "not distinguishable from random"
    else:
        interpretation = "not distinguishable from random"

    return JoinCountResult(
        n=n, n_vacant=n_vacant, n_occupied=n_occupied, k_neighbors=k_eff, total_joins=int(total_joins),
        observed_bb_joins=observed_bb, expected_bb_joins=round(expected_bb, 4),
        variance_bb_joins=round(variance_bb, 6) if variance_bb is not None else None,
        z_score=round(z, 4) if z is not None else None,
        p_value_two_tailed=round(p_two_tailed, 6) if p_two_tailed is not None else None,
        interpretation=interpretation, warnings=warnings,
    )


# ============================================================================
# Composite score + evidence dict
# ============================================================================

def occupancy_stability_score_0_100(
    vacancy: Optional[VacancyRateResult],
    turnover: Optional[TurnoverIntensityResult],
    join_count: Optional[JoinCountResult],
) -> Optional[float]:
    """Blends the three unambiguous-direction components -- low vacancy,
    low turnover, and no significant spatial clustering of vacancy all
    mean MORE stable occupancy -- 45% vacancy rate (inverted), 35%
    turnover intensity (inverted), 20% spatial clustering penalty.
    Missing components are dropped and remaining weights renormalized,
    same convention as business_ecosystem.py's ecosystem_vitality_
    score_0_100 (not treated as 0)."""
    components = []

    if vacancy is not None and vacancy.vacancy_rate is not None:
        vacancy_component = (1.0 - vacancy.vacancy_rate) * 100.0
        components.append((0.45, vacancy_component))

    if turnover is not None and turnover.turnover_intensity_per_business_year is not None:
        # Logistic centered so ~2 transitions/business-year (a real, fast-
        # cycling catchment) sits near the logistic's steep region.
        t = turnover.turnover_intensity_per_business_year
        turnover_component = 100.0 / (1.0 + math.exp((t - 1.0) / 0.6))
        components.append((0.35, turnover_component))

    if join_count is not None and join_count.z_score is not None:
        # z <= 0 (no clustering or dispersed vacancy) -> full 100;
        # increasingly clustered (higher positive z) pulls this toward 0.
        z = join_count.z_score
        clustering_component = 100.0 / (1.0 + math.exp((z - 1.5) / 0.75)) if z > 0 else 100.0
        components.append((0.20, clustering_component))

    if not components:
        return None

    weight_sum = sum(wt for wt, _ in components)
    return round(sum(wt * v for wt, v in components) / weight_sum, 2)


def commercial_occupancy_evidence(
    vacancy: Optional[VacancyRateResult], turnover: Optional[TurnoverIntensityResult],
    reabsorption: Optional[ReabsorptionKMResult], join_count: Optional[JoinCountResult],
) -> dict:
    """Structured evidence dict, same convention as every other engine's
    *_evidence function in this codebase."""
    return {
        "vacancy_rate": {
            "n_known_status": vacancy.n_known_status if vacancy else None,
            "n_vacant": vacancy.n_vacant if vacancy else None,
            "n_operational": vacancy.n_operational if vacancy else None,
            "n_unknown_excluded": vacancy.n_unknown_excluded if vacancy else None,
            "vacancy_rate": vacancy.vacancy_rate if vacancy else None,
            "ci_lower": vacancy.ci_lower if vacancy else None,
            "ci_upper": vacancy.ci_upper if vacancy else None,
            "warnings": vacancy.warnings if vacancy else [],
        } if vacancy is not None else None,
        "turnover_intensity": {
            "businesses_with_snapshots": turnover.businesses_with_snapshots if turnover else None,
            "total_business_years_observed": turnover.total_business_years_observed if turnover else None,
            "total_transitions": turnover.total_transitions if turnover else None,
            "vacancy_onsets": turnover.vacancy_onsets if turnover else None,
            "reabsorptions": turnover.reabsorptions if turnover else None,
            "permanent_conversions": turnover.permanent_conversions if turnover else None,
            "turnover_intensity_per_business_year": turnover.turnover_intensity_per_business_year if turnover else None,
            "vacancy_onset_rate_per_business_year": turnover.vacancy_onset_rate_per_business_year if turnover else None,
            "warnings": turnover.warnings if turnover else [],
        } if turnover is not None else None,
        "vacancy_reabsorption": {
            "n_spells": reabsorption.n_spells if reabsorption else None,
            "n_reabsorbed": reabsorption.n_reabsorbed if reabsorption else None,
            "n_converted_permanent": reabsorption.n_converted_permanent if reabsorption else None,
            "n_censored": reabsorption.n_censored if reabsorption else None,
            "median_reabsorption_days": reabsorption.median_reabsorption_days if reabsorption else None,
            "median_reached": reabsorption.median_reached if reabsorption else None,
            "warnings": reabsorption.warnings if reabsorption else [],
        } if reabsorption is not None else None,
        "spatial_clustering_join_count": {
            "n": join_count.n if join_count else None,
            "n_vacant": join_count.n_vacant if join_count else None,
            "k_neighbors": join_count.k_neighbors if join_count else None,
            "observed_bb_joins": join_count.observed_bb_joins if join_count else None,
            "expected_bb_joins": join_count.expected_bb_joins if join_count else None,
            "z_score": join_count.z_score if join_count else None,
            "p_value_two_tailed": join_count.p_value_two_tailed if join_count else None,
            "interpretation": join_count.interpretation if join_count else None,
            "warnings": join_count.warnings if join_count else [],
        } if join_count is not None else None,
        "methods": [
            "wilson_1927_score_interval", "elandt_johnson_johnson_1980_occurrence_exposure_rate",
            "kaplan_meier_1958_product_limit", "prentice_et_al_1978_cause_specific_competing_risk",
            "cliff_ord_1981_join_count_bb", "knn_symmetric_contiguity",
        ],
        "known_simplifications": [
            "Transition dates are the midpoint between two consecutive snapshots, not the true (unobserved) flip date -- see module docstring on snapshot cadence.",
            "Cause-specific Kaplan-Meier for reabsorption treats permanent-closure conversions as censored, not a full Aalen-Johansen cumulative-incidence competing-risks estimate (Putter et al. 2007) -- a documented, not implemented, gap.",
            "No address-level 'commercial space' identity independent of BusinessEntity/place_id -- genuine re-tenanting by a different business at the same physical location is not linked or detected (see module docstring's SCOPE BOUNDARY section).",
        ],
    }


# ============================================================================
# Django-facing entry points
# ============================================================================

def _businesses_in_radius(subject_lat: float, subject_lon: float, radius_m: float) -> list:
    """Deliberately does NOT filter on current_status -- vacancy rate
    needs closed businesses too. Same bbox-prefilter + exact-haversine
    pattern as business_survival.py's/business_ecosystem.py's identical
    helpers, per convention duplicated locally rather than imported."""
    lat_pad = radius_m / 111_000.0
    lon_pad = radius_m / (111_000.0 * max(math.cos(math.radians(subject_lat)), 0.01))
    qs = BusinessEntity.objects.filter(
        latitude__gte=subject_lat - lat_pad, latitude__lte=subject_lat + lat_pad,
        longitude__gte=subject_lon - lon_pad, longitude__lte=subject_lon + lon_pad,
    )
    found = []
    for biz in qs:
        d = _haversine_m(subject_lat, subject_lon, float(biz.latitude), float(biz.longitude))
        if d <= radius_m:
            found.append(biz)
    return found


def _snapshots_by_business(business_ids: list, window_start: date) -> dict:
    qs = BusinessSnapshot.objects.filter(
        business_id__in=business_ids, observed_at__gte=window_start,
    ).order_by("business_id", "observed_at")
    grouped = defaultdict(list)
    for snap in qs:
        grouped[snap.business_id].append(snap)
    return dict(grouped)


@dataclass
class CatchmentOccupancyFeatures:
    catchment: Catchment
    vacancy: Optional[VacancyRateResult] = None
    turnover: Optional[TurnoverIntensityResult] = None
    reabsorption: Optional[ReabsorptionKMResult] = None
    spatial_clustering: Optional[JoinCountResult] = None
    stability_score: Optional[float] = None
    source: str = "business_intel.engines.commercial_occupancy"
    source_class: str = "internal_derived"
    warnings: list = field(default_factory=list)


def compute_catchment_occupancy(
    cell: BusinessLocationCell, catchment: Catchment, as_of: Optional[date] = None,
    observation_window_days: int = DEFAULT_OBSERVATION_WINDOW_DAYS,
) -> Optional[CatchmentOccupancyFeatures]:
    """Full Section-14 calculation for one Catchment. Returns None for
    isochrone-type catchments this engine doesn't support yet -- same
    limitation, same reasoning, as business_survival.py/
    business_ecosystem.py."""
    if catchment.catchment_size_unit != "meters":
        logger.info(
            "Skipping catchment %s: commercial-occupancy modeling currently only supports meter-radius catchments "
            "(radius/network_radius), not isochrone geometry -- see module docstring.", catchment.id,
        )
        return None

    as_of = as_of or django_timezone.now().date()
    subject_lat, subject_lon = float(cell.center_latitude), float(cell.center_longitude)
    radius_m = float(catchment.catchment_size_value)
    warnings = []

    businesses = _businesses_in_radius(subject_lat, subject_lon, radius_m)
    if not businesses:
        warnings.append(f"No businesses (any status) found within {radius_m:.0f}m -- catchment may be genuinely empty or not yet Places-searched.")
        return CatchmentOccupancyFeatures(catchment=catchment, warnings=warnings)

    vacancy = compute_vacancy_rate(businesses)
    if vacancy:
        warnings.extend(vacancy.warnings)

    window_start = as_of - timedelta(days=observation_window_days)
    business_ids = [b.id for b in businesses]
    snapshots_by_business = _snapshots_by_business(business_ids, window_start)

    turnover = None
    reabsorption = None
    if not snapshots_by_business:
        warnings.append(f"No BusinessSnapshot rows found in the last {observation_window_days} days for these businesses -- turnover intensity and vacancy-reabsorption withheld (need at least 2 report runs' worth of history).")
    else:
        turnover = compute_turnover_intensity(snapshots_by_business, window_start, as_of)
        if turnover:
            warnings.extend(turnover.warnings)

        spells = _build_vacancy_spells(snapshots_by_business, as_of)
        if len(spells) < MIN_SPELLS_FOR_REABSORPTION_KM:
            warnings.append(f"Only {len(spells)} vacancy spell(s) observed (< {MIN_SPELLS_FOR_REABSORPTION_KM}) -- reabsorption Kaplan-Meier withheld.")
        else:
            reabsorption = cause_specific_reabsorption_km(spells)
            if reabsorption:
                warnings.extend(reabsorption.warnings)

    join_count = None
    if vacancy and vacancy.n_known_status >= MIN_N_FOR_JOIN_COUNT:
        known_businesses = [b for b in businesses if b.current_status in KNOWN_STATUSES]
        points = [(float(b.latitude), float(b.longitude)) for b in known_businesses]
        is_vacant = [b.current_status in VACANT_STATUSES for b in known_businesses]
        join_count = join_count_statistic(points, is_vacant)
        if join_count:
            warnings.extend(join_count.warnings)
    elif vacancy:
        warnings.append(f"Only {vacancy.n_known_status} businesses with a known status (< {MIN_N_FOR_JOIN_COUNT}) -- spatial join-count clustering test withheld.")

    score = occupancy_stability_score_0_100(vacancy, turnover, join_count)

    return CatchmentOccupancyFeatures(
        catchment=catchment, vacancy=vacancy, turnover=turnover, reabsorption=reabsorption,
        spatial_clustering=join_count, stability_score=score, warnings=warnings,
    )


def run(
    query: BusinessQuery,
    catchments: Optional[Sequence[Catchment]] = None,
    as_of: Optional[date] = None,
    observation_window_days: int = DEFAULT_OBSERVATION_WINDOW_DAYS,
) -> dict:
    """
    Engine 14 entry point. Computes commercial-occupancy features for
    every Catchment belonging to query.location_cell (or an explicit
    `catchments` subset), writes FeatureObservation rows, and records an
    EngineRun -- same contract shape as every other engine.

    Requires Engine 1 to have already run (catchments must exist), and
    benefits from at least two prior report runs having populated
    BusinessSnapshot history (turnover/reabsorption are withheld,
    vacancy_rate/join-count still compute, on a single-snapshot cell) --
    same dependency documentation convention as business_survival.py's/
    business_ecosystem.py's run().
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
                catch_features = compute_catchment_occupancy(
                    cell, catchment, as_of=as_of, observation_window_days=observation_window_days,
                )
                if catch_features is None:
                    continue  # unsupported (isochrone) catchment type -- see module docstring
                all_features.append(catch_features)
                warnings_seen.extend(catch_features.warnings)
                evidence = commercial_occupancy_evidence(
                    catch_features.vacancy, catch_features.turnover,
                    catch_features.reabsorption, catch_features.spatial_clustering,
                )

                if catch_features.vacancy is not None and catch_features.vacancy.vacancy_rate is not None:
                    v = catch_features.vacancy
                    FeatureObservation.write_latest(
                        location_cell=cell, catchment=catchment, engine_number=ENGINE_NUMBER, feature_name="commercial_vacancy_rate",
                        defaults=dict(
                            value_type="numeric", value_numeric=Decimal(str(v.vacancy_rate)), unit="ratio_0_1",
                            observed_at=started_at, source=catch_features.source, source_class=catch_features.source_class,
                            method="wilson_1927_score_interval", derivation_level="calculation",
                            sample_size=v.n_known_status,
                            uncertainty=Decimal(str(round(v.ci_upper - v.ci_lower, 6))) if (v.ci_lower is not None and v.ci_upper is not None) else None,
                        ),
                    )
                    features_written += 1

                if catch_features.turnover is not None and catch_features.turnover.turnover_intensity_per_business_year is not None:
                    t = catch_features.turnover
                    FeatureObservation.write_latest(
                        location_cell=cell, catchment=catchment, engine_number=ENGINE_NUMBER, feature_name="occupancy_turnover_intensity_per_year",
                        defaults=dict(
                            value_type="numeric", value_numeric=Decimal(str(t.turnover_intensity_per_business_year)), unit="transitions_per_business_year",
                            observed_at=started_at, source=catch_features.source, source_class=catch_features.source_class,
                            method="elandt_johnson_johnson_1980_occurrence_exposure_rate", derivation_level="calculation",
                            sample_size=t.businesses_with_snapshots,
                        ),
                    )
                    features_written += 1

                if catch_features.reabsorption is not None and catch_features.reabsorption.median_reabsorption_days is not None:
                    r = catch_features.reabsorption
                    FeatureObservation.write_latest(
                        location_cell=cell, catchment=catchment, engine_number=ENGINE_NUMBER, feature_name="median_vacancy_reabsorption_days",
                        defaults=dict(
                            value_type="numeric", value_numeric=Decimal(str(r.median_reabsorption_days)), unit="days",
                            observed_at=started_at, source=catch_features.source, source_class=catch_features.source_class,
                            method="kaplan_meier_1958_cause_specific_median", derivation_level="business_conclusion",
                            sample_size=r.n_spells,
                        ),
                    )
                    features_written += 1

                if catch_features.spatial_clustering is not None and catch_features.spatial_clustering.z_score is not None:
                    j = catch_features.spatial_clustering
                    FeatureObservation.write_latest(
                        location_cell=cell, catchment=catchment, engine_number=ENGINE_NUMBER, feature_name="vacancy_spatial_clustering_z",
                        defaults=dict(
                            value_type="numeric", value_numeric=Decimal(str(j.z_score)), unit="z_score",
                            observed_at=started_at, source=catch_features.source, source_class=catch_features.source_class,
                            method="cliff_ord_1981_join_count_bb", derivation_level="calculation",
                            sample_size=j.n, uncertainty=Decimal(str(j.p_value_two_tailed)) if j.p_value_two_tailed is not None else None,
                        ),
                    )
                    features_written += 1

                if catch_features.stability_score is not None:
                    FeatureObservation.write_latest(
                        location_cell=cell, catchment=catchment, engine_number=ENGINE_NUMBER, feature_name="occupancy_stability_score",
                        defaults=dict(
                            value_type="numeric", value_numeric=Decimal(str(catch_features.stability_score)), unit="score_0_100",
                            observed_at=started_at, source=catch_features.source, source_class=catch_features.source_class,
                            method="commercial_occupancy_composite", derivation_level="business_conclusion",
                        ),
                    )
                    features_written += 1

                FeatureObservation.write_latest(
                    location_cell=cell, catchment=catchment, engine_number=ENGINE_NUMBER, feature_name="commercial_occupancy_evidence",
                    defaults=dict(
                        value_type="json", value_json=evidence, unit="",
                        observed_at=started_at, source=catch_features.source, source_class=catch_features.source_class,
                        method="commercial_occupancy_engine", derivation_level="calculation",
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