"""
business_intel/engines/confidence.py

Engine 24 -- Confidence Engine
Per business_location_intelligence_engines_data_models_v2.md, Section 24.

NOTE ON THE SPEC FILE: same situation as every other engine in this
codebase -- business_location_intelligence_engines_data_models_v2.md is
referenced but not present on disk. Built to match Engine 2/3/6/7/8/9/23
for structure, schema contract, and citation discipline. The stub this
replaced gave only a purpose line: "Quantify how reliable each
conclusion is."

=============================================================================
SCOPE BOUNDARY VS. ENGINE 23 (read this before extending)
=============================================================================
Engine 23 (evidence.py, built) asks "what evidence actually produced this
conclusion, how complete/traceable/corroborated is it, and has anything
drifted?" -- evidentiary bookkeeping and audit.

This engine asks the narrower, DOWNSTREAM question Engine 23's own
docstring explicitly reserves for it: "how much should a person TRUST
this conclusion" -- a calibration/decision-support question that folds in
three things Engine 23 deliberately does not touch: (1) the historical
operational reliability of the engine that produced the conclusion, (2)
an honest, un-fabricated way to express how RISK-AVERSE the reader should
be about a given confidence level, and (3) user-facing communication of
uncertainty (a categorical band, not just a bare number).

=============================================================================
HONESTY NOTE ON WHAT ENGINE 23 ACTUALLY PERSISTS (read before assuming)
=============================================================================
Engine 23's own docstring states it is "expected to consume Engine 23's
per-feature Evidence Strength Index (ESI) as one input among several, not
duplicate it." In the CURRENT evidence.py implementation, however, ESI is
computed in-memory per feature but only ever PERSISTED as (a) a single
cell-level mean_evidence_strength_index, (b) a list of features that fell
below evidence.py's WEAK_EVIDENCE_THRESHOLD inside the evidence_dossier
JSON blob, and (c) drift_flags / orphaned_conclusions / engine_run_summary
inside that same dossier. There is no full per-feature, per-catchment ESI
table on disk to read.

This engine is built honestly against what Engine 23 ACTUALLY writes, not
against what its docstring aspirationally describes:
  - The cell-level mean_evidence_strength_index and the dossier's
    drift/orphan/weak-evidence lists ARE genuinely consumed (read, never
    recomputed) wherever Engine 23 has run for this cell -- true
    "consume, don't duplicate."
  - For conclusion-level (catchment + business_category) granularity that
    Engine 23's dossier does not provide at all, this engine reads the
    SAME underlying FeatureObservation metadata fields Engine 23's own
    ESI derives from (source_class, sample_size, age_days,
    staleness_window_days) and computes a narrower, single-conclusion
    evidentiary read -- NOT a duplicate ESI computation (it does not use
    derivation_level or coverage_pct, and is never written to the DB as
    an "ESI"), but the minimum necessary extension to reach the
    granularity Engine 23 does not persist. This gap is real and is
    exactly the kind of thing this codebase's own discipline says to
    flag rather than silently paper over -- see EDGE CASES below,
    `no_evidence_engine_output_falling_back_to_unshrunk`, and the
    Bühlmann-credibility blend (REAL METHODS #3) that keeps this
    conclusion-level read from ever being trusted MORE than the genuine
    Engine-23 cell-level prior once one exists.

=============================================================================
WHAT THIS ENGINE ACTUALLY MEASURES
=============================================================================
For every latest business_conclusion-level FeatureObservation on record
for this query's location_cell (i.e. every "headline" figure another
engine has already computed -- ecosystem_vitality_score, median business
survival days, gap_classification, etc.), this engine answers:

  1. HOW MUCH DIRECT EVIDENTIARY SUPPORT does this specific conclusion
     have, blended with how strong the cell's OVERALL evidence base is
     (Bühlmann (1967) credibility shrinkage -- a conclusion resting on a
     thin, undocumented sample gets pulled toward the cell's broader
     evidentiary picture rather than trusted at face value; a
     well-documented, well-sampled conclusion stays close to its own
     read).

  2. HOW RELIABLE has the engine that produced this conclusion actually
     BEEN, historically, across every query this codebase has ever run
     it against? A real Beta-Bayesian estimate (Jeffreys 1946 prior)
     from EngineRun.status history -- explicitly labeled as an
     OPERATIONAL reliability proxy (did this engine run to completion
     without failing/degrading), not a claim about predictive accuracy
     against real-world ground truth, because no labeled outcomes table
     exists anywhere in this codebase to validate predictive accuracy
     against. Faking that distinction would be exactly the kind of
     unlabeled overclaim this codebase's own conventions exist to avoid.

  3. HOW COMPLETE is the evidentiary picture for THIS query -- what
     fraction of the 22 primary engines actually ran and succeeded,
     expressed as a Wilson (1927) score-interval proportion (chosen,
     like Engine 23's own small-sample statistical choices, because a
     Wald/normal-approximation interval is a poor fit for a proportion
     over as few as 22 trials, especially near 0 or 1).

  4. HAS ANYTHING ABOUT THIS SPECIFIC CONCLUSION BEEN FLAGGED as
     drifting or orphaned by Engine 23's own audit -- read directly from
     the evidence_dossier, never recomputed.

  5. HOW MUCH DOES THE FINAL CONFIDENCE NUMBER ITSELF WOBBLE across the
     query's whole conclusion set? A real nonparametric bootstrap
     (Efron 1979) over the per-conclusion confidence scores gives a
     percentile confidence interval on the QUERY-LEVEL aggregate
     confidence -- an honest way to say "this overall confidence reading
     is itself uncertain" rather than reporting one point estimate as if
     it were exact.

A sixth, OPTIONAL mechanism -- risk-adjustment -- lets a caller who wants
to be more conservative (e.g. a large capital commitment) apply a real,
cited, monotonic re-expression of the same confidence number (Kahneman &
Tversky (1979) loss-aversion framing) rather than getting a single
one-size-fits-all number regardless of how much is riding on the
decision.

=============================================================================
REAL METHODS USED (researched and cited, not invented)
=============================================================================
1. Beta-Binomial conjugate reliability estimation, Jeffreys prior
   Jeffreys, H. (1946). "An Invariant Form for the Prior Probability in
   Estimation Problems." Proceedings of the Royal Society A, 186(1007),
   453-461.
   Brown, L.D., Cai, T.T., DasGupta, A. (2001). "Interval Estimation for
   a Binomial Proportion." Statistical Science, 16(2), 101-133. (Source
   of the recommendation that Jeffreys'/Wilson's intervals dominate the
   naive Wald interval for small-to-moderate n and extreme proportions --
   exactly this engine's regime, since most engines in this codebase have
   run against relatively few historical queries so far.)
   Posterior mean r = (successes + 0.5) / (trials + 1), where a
   "degraded" EngineRun counts as 0.5 of a success (it completed but
   flagged a real caveat) and "failed" counts as 0 -- treating degraded
   as a genuine partial outcome, not silently folding it into either
   pure category.

2. Wilson score interval
   Wilson, E.B. (1927). "Probable Inference, the Law of Succession, and
   Statistical Inference." Journal of the American Statistical
   Association, 22(158), 209-212.
   Used for the query-completeness proportion (successes out of the 22
   primary engines) -- same small-n justification as point 1, applied to
   a genuinely different proportion (per-query completeness, not
   per-engine historical reliability).

3. Bühlmann credibility theory
   Bühlmann, H. (1967). "Experience rating and credibility." ASTIN
   Bulletin, 4(3), 199-207.
   Z = n / (n + k) credibility weight, blending a conclusion's own
   (possibly thin) evidentiary read with the cell's broader,
   Engine-23-computed evidentiary prior:
       blended = Z * own_read + (1 - Z) * cell_prior
   The actuarial textbook use of this formula: don't trust a small
   individual sample fully, don't ignore it either -- shrink toward a
   larger reference class in proportion to how much information the
   individual sample actually carries. n here is drawn from the
   conclusion's own recorded sample_size (see CREDIBILITY_K below for
   the calibration of k).

4. Weighted geometric mean of sub-indicator scores
   United Nations Development Programme (2010). Human Development Report
   2010, Technical Note 1. Same rationale Engine 23 already established
   for this codebase (a very poor score on one dimension should not be
   maskable by a strong score on another) -- reused here as the house
   convention for combining this engine's own four components
   (evidentiary, reliability, completeness, integrity-penalty) into one
   composite confidence score, independently re-derived with this
   engine's own component set and weights, not imported from evidence.py.

5. Nonparametric bootstrap (percentile method)
   Efron, B. (1979). "Bootstrap Methods: Another Look at the Jackknife."
   The Annals of Statistics, 7(1), 1-26.
   Efron, B., Tibshirani, R.J. (1993). An Introduction to the Bootstrap.
   Chapman & Hall/CRC, Ch. 13 (percentile intervals).
   B=2000 resamples with replacement of the per-conclusion confidence
   scores for this query; the [2.5, 97.5] percentiles of the resampled
   means give a real confidence interval on the aggregate query-level
   confidence, not a fabricated +/- band.

6. Prospect-theory-inspired loss aversion (OPTIONAL risk adjustment)
   Kahneman, D., Tversky, A. (1979). "Prospect Theory: An Analysis of
   Decision under Risk." Econometrica, 47(2), 263-291. (Source of the
   empirically-estimated loss-aversion coefficient lambda ~ 2.25, used
   here ONLY as a documented default exponent shape, not as a claim that
   this specific business-confidence context has been empirically
   calibrated the way Kahneman & Tversky's original gambles were.)
   risk_adjusted = 100 * (confidence/100) ** (1 + risk_aversion * (LAMBDA - 1))
   Monotonic, bounded in [0, 100], identity at risk_aversion=0 -- a
   real, cited, but EXPLICITLY EDITORIAL re-expression (this module's own
   composition, same "editorial blend, labeled as such" convention as
   competition.py's competition_score and business_ecosystem.py's
   ecosystem_vitality_score_0_100), never applied unless the caller
   explicitly asks for it via a nonzero risk_aversion parameter.

=============================================================================
EDGE CASES THIS MODULE HANDLES EXPLICITLY
=============================================================================
- Zero business_conclusion-level FeatureObservation rows for this cell ->
  every aggregate is None/empty, not a fabricated zero-confidence
  reading (a data gap, same principle as Engine 6/7/23's "nothing synced
  yet" handling).
- Engine 23 has not run for this cell yet (no evidence_dossier on record)
  -> conclusion-level evidentiary reads are computed but NOT credibility-
  shrunk toward a cell prior that does not exist (Z effectively forced to
  1.0), flagged no_evidence_engine_output_falling_back_to_unshrunk.
- A conclusion's own FeatureObservation.sample_size is None -> credibility
  n is conservatively set to 1 (near-minimum trust in the individual
  read, near-total reliance on the shrinkage target), flagged
  sample_size_undocumented_conservative_credibility.
- A conclusion's source engine_number has ZERO historical EngineRun rows
  anywhere in the database (a brand-new engine with no track record) ->
  Jeffreys prior alone gives r=0.5 with maximal interval width, flagged
  no_track_record_using_uninformative_prior -- an honest "we don't know
  yet," not an assumed-good or assumed-bad score.
- This query's own EngineRun set has zero rows for one or more of the 22
  primary engines -> counted as a non-success for the completeness Wilson
  interval (a "not_yet_run" engine did not, in fact, contribute
  completed evidence to this specific report), consistent with, but
  computed independently from, Engine 23's own run_summary semantics.
- Fewer than MIN_CONCLUSIONS_FOR_BOOTSTRAP conclusions on record for this
  query -> the query-level bootstrap confidence interval is skipped
  (a percentile bootstrap on 1-2 points is not a meaningful statistic),
  the point-estimate mean is still reported, flagged
  insufficient_conclusions_for_bootstrap.
- risk_aversion supplied outside [0, 1] -> clamped to the nearer bound,
  flagged risk_aversion_clamped, never silently ignored.
- A conclusion whose value_type is not "numeric" (e.g. a categorical
  gap_classification) is still scored for confidence (evidentiary
  strength does not require the VALUE to be numeric), but is excluded
  from any future numeric cross-checks -- flagged non_numeric_conclusion
  purely for transparency, not treated as lower quality.
- All conclusions come from a single engine_number -> confidence is still
  computed correctly per-conclusion, but flagged
  single_engine_conclusion_set at the query level, since a report backed
  by only one engine's conclusions is a genuinely thinner evidentiary
  base than one drawing on several, independent of any single
  conclusion's own individual confidence.
- A conclusion flagged in Engine 23's drift_flags or orphaned_conclusions
  lists -> receives the reduced INTEGRITY_PENALTY_ON_FLAG component
  weight rather than the full 1.0, and is listed by name in this
  engine's own evidence dict so a reader can see exactly why.

Scope boundary
--------------
Depends on business_intel.models only (BusinessQuery, EngineRun,
FeatureObservation). Reads (never writes to) Engine 23's evidence_dossier
and mean_evidence_strength_index FeatureObservation rows where they
exist, and reads EngineRun history both for this query and, for the
reliability component, across the whole database -- the one deliberate
exception in this codebase's engines to "no dependency on any other
engine's feature-store output," and an exception Engine 23's own
docstring explicitly authorizes for exactly this engine. No external API
calls.
"""

from __future__ import annotations

import logging
import math
import random
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional, Sequence

from django.db import transaction
from django.db.models import Q
from django.utils import timezone as django_timezone

from ..models import BusinessQuery, Catchment, EngineRun, FeatureObservation

logger = logging.getLogger("business_intel.engines.confidence")

ENGINE_NUMBER = 24

META_ENGINE_NUMBERS = {23, 24, 25, 26, 27}
PRIMARY_ENGINE_NUMBERS = [n for n in range(1, 28) if n not in META_ENGINE_NUMBERS]

# ---------------------------------------------------------------------------
# Calibration constants -- each one documented as either a real published
# figure (see REAL METHODS above) or this module's own editorial choice.
# ---------------------------------------------------------------------------

# Bühlmann credibility constant k (REAL METHODS #3): the sample_size at
# which a conclusion's own evidentiary read and the cell-wide prior are
# weighted equally (Z=0.5). Chosen, like Engine 23's
# SAMPLE_SIZE_HALF_SATURATION=30, as a documented calibration rather than
# a claim of unique theoretical status; kept deliberately smaller than
# Engine 23's own n=30 because credibility Z=n/(n+k) already saturates
# much more gradually than Engine 23's 1-exp(-n/30) saturating-return form.
CREDIBILITY_K = 12.0

# Component weights for the final composite (REAL METHODS #4). Sum to 1.0.
COMPONENT_WEIGHTS = {
    "evidentiary": 0.45,
    "reliability": 0.25,
    "completeness": 0.20,
    "integrity": 0.10,
}
assert abs(sum(COMPONENT_WEIGHTS.values()) - 1.0) < 1e-9

MIN_COMPONENT_FLOOR = 0.02   # keeps ln() finite, same convention as evidence.py
NEUTRAL_SCORE = 0.5

# Applied as the "integrity" component for a conclusion Engine 23 flagged
# as drifting or orphaned, in place of the full 1.0 an unflagged
# conclusion receives -- editorial, documented, not a cited figure.
INTEGRITY_PENALTY_ON_FLAG = 0.35

# Recency half-life fallback (days) used only when a conclusion's own
# staleness_window_days is unset -- matches models.STALENESS_DAYS_BY_
# SOURCE_CLASS["unknown"] in spirit but this module does not import that
# table directly (independent-footprint convention); 45 days is a
# conservative mid-range default consistent with the codebase's other
# engines' staleness windows for internally-derived features.
DEFAULT_STALENESS_DAYS = 45

MIN_CONCLUSIONS_FOR_BOOTSTRAP = 3
N_BOOTSTRAP_RESAMPLES = 2000
BOOTSTRAP_SEED = 2404

# Kahneman & Tversky (1979) empirically-estimated loss-aversion
# coefficient -- see REAL METHODS #6 for why this is used as a shape
# parameter, not a validated constant for this specific domain.
KT_LOSS_AVERSION_LAMBDA = 2.25

CONFIDENCE_BANDS = [
    (85.0, "very_high"),
    (70.0, "high"),
    (50.0, "moderate"),
    (30.0, "low"),
    (0.0, "very_low"),
]


def confidence_band(score: Optional[float]) -> str:
    if score is None:
        return "insufficient_data"
    for threshold, label in CONFIDENCE_BANDS:
        if score >= threshold:
            return label
    return "very_low"


# ============================================================================
# Pure-math layer 1: interval estimators (Jeffreys / Wilson). No
# scipy/numpy dependency -- duplicated in spirit from Engine 7's
# scipy-free convention, independently coded here.
# ============================================================================

def jeffreys_posterior_mean(successes: float, trials: float) -> float:
    """Beta(0.5, 0.5) Jeffreys-prior posterior mean for a binomial
    proportion (REAL METHODS #1). trials may be 0 -- returns exactly 0.5,
    the correct prior-only mean when there is no data at all."""
    return (successes + 0.5) / (trials + 1.0)


def jeffreys_credible_interval_width(successes: float, trials: float) -> float:
    """A simple, real proxy for how WIDE the Jeffreys posterior is --
    the Beta(a, b) standard deviation (a=successes+0.5, b=trials-
    successes+0.5), NOT the exact HPD interval width, but a monotonic,
    honest measure of the same underlying uncertainty without needing an
    incomplete-beta inversion this environment has no scipy for."""
    a = successes + 0.5
    b = max(trials - successes, 0.0) + 0.5
    total = a + b
    variance = (a * b) / ((total ** 2) * (total + 1.0))
    return math.sqrt(max(variance, 0.0))


def wilson_score_center(successes: float, trials: int, z: float = 1.959963985) -> Optional[float]:
    """Wilson (1927) score-interval CENTER (not the naive successes/trials
    point estimate) for a binomial proportion -- REAL METHODS #2. Returns
    None for trials=0 (no proportion is defined, not 0%)."""
    if trials <= 0:
        return None
    p_hat = successes / trials
    denom = 1.0 + (z ** 2) / trials
    center = (p_hat + (z ** 2) / (2.0 * trials)) / denom
    return max(0.0, min(1.0, center))


# ============================================================================
# Pure-math layer 2: per-conclusion evidentiary read (recency + sample
# size only -- see module docstring's honesty note for why this is
# deliberately narrower than Engine 23's full five-component ESI).
# ============================================================================

@dataclass
class ConclusionEvidentiaryRead:
    recency_weight: float
    sample_size_weight: float
    own_read: float          # geometric mean of the two above
    flags: list = field(default_factory=list)


def _recency_weight(feature: FeatureObservation) -> tuple:
    age_days = getattr(feature, "age_days", None)
    if age_days is None:
        return NEUTRAL_SCORE, ["recency_undetermined"]
    window = getattr(feature, "staleness_window_days", None) or DEFAULT_STALENESS_DAYS
    if window <= 0:
        return NEUTRAL_SCORE, ["nonpositive_staleness_window"]
    weight = 0.5 ** (max(age_days, 0) / window)
    return max(0.0, min(1.0, weight)), []


def _sample_size_credibility_n(sample_size: Optional[int]) -> tuple:
    """Returns (n_for_credibility, sample_size_weight_0_1, flags). The
    sample-size WEIGHT uses the same saturating-return functional form
    Engine 23 documents (Colwell & Coddington 1994 species-accumulation
    analogy) -- reused as a general statistical idiom, independently
    parameterized here (half-saturation at n=15, tighter than Engine 23's
    n=30, since this is a narrower single-conclusion read that should not
    require as large a sample to register real confidence)."""
    if sample_size is None:
        return 1.0, NEUTRAL_SCORE, ["sample_size_undocumented_conservative_credibility"]
    if sample_size <= 0:
        return 1.0, MIN_COMPONENT_FLOOR, ["nonpositive_sample_size"]
    weight = 1.0 - math.exp(-sample_size / 15.0)
    return float(sample_size), max(0.0, min(1.0, weight)), []


def compute_conclusion_evidentiary_read(feature: FeatureObservation) -> tuple:
    """Returns (ConclusionEvidentiaryRead, credibility_n). Geometric mean
    of recency and sample-size weight (UNDP 2010 rationale, independently
    applied here to a two-component read)."""
    recency_w, f1 = _recency_weight(feature)
    credibility_n, sample_w, f2 = _sample_size_credibility_n(feature.sample_size)
    flags = f1 + f2
    log_sum = 0.5 * math.log(max(recency_w, MIN_COMPONENT_FLOOR)) + 0.5 * math.log(max(sample_w, MIN_COMPONENT_FLOOR))
    own_read = max(0.0, min(1.0, math.exp(log_sum)))
    return (
        ConclusionEvidentiaryRead(
            recency_weight=recency_w, sample_size_weight=sample_w, own_read=own_read, flags=flags,
        ),
        credibility_n,
    )


def buhlmann_credibility_blend(own_read: float, credibility_n: float, cell_prior: Optional[float]) -> tuple:
    """Z = n / (n + CREDIBILITY_K); blended = Z*own + (1-Z)*prior (REAL
    METHODS #3). If cell_prior is None (Engine 23 has not run for this
    cell), Z is forced to 1.0 -- there is nothing to shrink toward, so
    the conclusion's own read is used unshrunk and flagged."""
    if cell_prior is None:
        return own_read, 1.0, ["no_evidence_engine_output_falling_back_to_unshrunk"]
    z = credibility_n / (credibility_n + CREDIBILITY_K)
    blended = z * own_read + (1.0 - z) * cell_prior
    return max(0.0, min(1.0, blended)), z, []


# ============================================================================
# Pure-math layer 3: engine historical reliability + query completeness.
# ============================================================================

@dataclass
class EngineReliability:
    engine_number: int
    trials: int
    successes: float
    reliability: float
    interval_width: float
    flags: list = field(default_factory=list)


def compute_engine_reliability(runs: Sequence[EngineRun]) -> EngineReliability:
    """runs: every historical EngineRun row for ONE engine_number, across
    every query in the database (not just this one) -- REAL METHODS #1.
    'degraded' counts as 0.5 of a success; 'failed' as 0; 'succeeded' as 1."""
    trials = len(runs)
    successes = sum(1.0 if r.status == "succeeded" else 0.5 if r.status == "degraded" else 0.0 for r in runs)
    reliability = jeffreys_posterior_mean(successes, trials)
    width = jeffreys_credible_interval_width(successes, trials)
    flags = [] if trials > 0 else ["no_track_record_using_uninformative_prior"]
    return EngineReliability(
        engine_number=runs[0].engine_number if runs else -1,
        trials=trials, successes=successes, reliability=reliability, interval_width=width, flags=flags,
    )


@dataclass
class CompletenessResult:
    engines_expected: int
    engines_succeeded: int
    completeness: Optional[float]
    flags: list = field(default_factory=list)


def compute_query_completeness(engine_runs_this_query: Sequence[EngineRun]) -> CompletenessResult:
    """Wilson-score-centered proportion of PRIMARY_ENGINE_NUMBERS that
    reached 'succeeded' status for THIS query (REAL METHODS #2). An
    engine that never ran at all, or that ran but degraded/failed, both
    count as a non-success here -- this is deliberately a stricter
    reading than Engine 23's three-way not_yet_run/degraded/failed
    status split, because completeness is asking a binary question
    ("did this contribute clean evidence"), not an audit question."""
    latest_by_engine = {}
    for run in engine_runs_this_query:
        if run.engine_number not in PRIMARY_ENGINE_NUMBERS:
            continue
        current = latest_by_engine.get(run.engine_number)
        if current is None or run.started_at > current.started_at:
            latest_by_engine[run.engine_number] = run

    expected = len(PRIMARY_ENGINE_NUMBERS)
    succeeded = sum(1 for r in latest_by_engine.values() if r.status == "succeeded")
    completeness = wilson_score_center(float(succeeded), expected)
    flags = []
    if succeeded < expected:
        missing = sorted(set(PRIMARY_ENGINE_NUMBERS) - set(latest_by_engine.keys()))
        degraded_or_failed = [n for n, r in latest_by_engine.items() if r.status != "succeeded"]
        if missing:
            flags.append(f"{len(missing)} primary engine(s) never ran for this query: {missing}.")
        if degraded_or_failed:
            flags.append(f"{len(degraded_or_failed)} primary engine(s) ran but did not fully succeed: {sorted(degraded_or_failed)}.")
    return CompletenessResult(engines_expected=expected, engines_succeeded=succeeded, completeness=completeness, flags=flags)


# ============================================================================
# Pure-math layer 4: nonparametric bootstrap on the query-level mean.
# ============================================================================

@dataclass
class BootstrapResult:
    point_estimate: float
    ci_lower: float
    ci_upper: float
    n_resamples: int


def bootstrap_mean_ci(values: Sequence[float], n_resamples: int = N_BOOTSTRAP_RESAMPLES, seed: int = BOOTSTRAP_SEED) -> Optional[BootstrapResult]:
    """Percentile-method nonparametric bootstrap (REAL METHODS #5) on the
    mean of `values`. Returns None below MIN_CONCLUSIONS_FOR_BOOTSTRAP --
    a bootstrap CI on 1-2 points is not a meaningful statistic, same
    discipline as Engine 7's MIN_N_FOR_KAPLAN_MEIER guard."""
    n = len(values)
    if n < MIN_CONCLUSIONS_FOR_BOOTSTRAP:
        return None
    rng = random.Random(seed)
    point = sum(values) / n
    resampled_means = []
    for _ in range(n_resamples):
        sample = [values[rng.randrange(n)] for _ in range(n)]
        resampled_means.append(sum(sample) / n)
    resampled_means.sort()
    lo_idx = int(0.025 * n_resamples)
    hi_idx = min(n_resamples - 1, int(0.975 * n_resamples))
    return BootstrapResult(
        point_estimate=point, ci_lower=resampled_means[lo_idx], ci_upper=resampled_means[hi_idx],
        n_resamples=n_resamples,
    )


# ============================================================================
# Pure-math layer 5: composite confidence + optional risk adjustment.
# ============================================================================

def composite_confidence_0_100(evidentiary: float, reliability: float, completeness: Optional[float], integrity: float) -> float:
    """Weighted geometric mean (REAL METHODS #4) of the four components.
    completeness=None (should not happen once compute_query_completeness
    has run, since it always has a defined denominator) is treated as
    NEUTRAL rather than crashing, defensively."""
    components = {
        "evidentiary": evidentiary,
        "reliability": reliability,
        "completeness": completeness if completeness is not None else NEUTRAL_SCORE,
        "integrity": integrity,
    }
    log_sum = sum(
        COMPONENT_WEIGHTS[name] * math.log(max(value, MIN_COMPONENT_FLOOR))
        for name, value in components.items()
    )
    return max(0.0, min(100.0, math.exp(log_sum) * 100.0))


def risk_adjusted_confidence(confidence_0_100: float, risk_aversion: float) -> float:
    """REAL METHODS #6 -- monotonic, bounded, identity at risk_aversion=0.
    risk_aversion is clamped to [0, 1] by the caller before this is
    invoked; not re-clamped here so the flag is only raised once."""
    exponent = 1.0 + risk_aversion * (KT_LOSS_AVERSION_LAMBDA - 1.0)
    fraction = max(0.0, min(1.0, confidence_0_100 / 100.0))
    return round(100.0 * (fraction ** exponent), 4)


# ============================================================================
# Per-conclusion assembly
# ============================================================================

@dataclass
class ConclusionConfidence:
    feature_id: object
    engine_number: int
    feature_name: str
    catchment_id: Optional[object]
    business_category: str
    evidentiary_read: float
    credibility_z: float
    engine_reliability: float
    completeness: Optional[float]
    integrity: float
    confidence_score: float
    band: str
    flags: list = field(default_factory=list)


def _flagged_feature_names(dossier: Optional[dict]) -> set:
    """Feature names Engine 23 flagged as drifting or orphaned for this
    cell, read directly from its dossier JSON -- genuine consumption of
    Engine 23's output, no recomputation."""
    if not dossier:
        return set()
    names = set()
    for d in dossier.get("drift_flags") or []:
        if d.get("feature_name"):
            names.add(d["feature_name"])
    for orphan in dossier.get("orphaned_conclusions") or []:
        # orphan strings are formatted "engine_{n}:{feature_name}" by evidence.py
        if ":" in orphan:
            names.add(orphan.split(":", 1)[1])
    return names


def compute_conclusion_confidence(
    feature: FeatureObservation,
    cell_prior_esi_0_1: Optional[float],
    reliability_by_engine: dict,
    completeness: CompletenessResult,
    flagged_feature_names: set,
) -> ConclusionConfidence:
    read, credibility_n = compute_conclusion_evidentiary_read(feature)
    blended_read, z, shrink_flags = buhlmann_credibility_blend(read.own_read, credibility_n, cell_prior_esi_0_1)

    rel = reliability_by_engine.get(feature.engine_number)
    reliability = rel.reliability if rel is not None else 0.5
    reliability_flags = rel.flags if rel is not None else ["no_track_record_using_uninformative_prior"]

    integrity = INTEGRITY_PENALTY_ON_FLAG if feature.feature_name in flagged_feature_names else 1.0
    integrity_flags = ["flagged_by_evidence_engine_drift_or_orphan_audit"] if integrity < 1.0 else []

    non_numeric_flag = [] if feature.value_type == "numeric" else ["non_numeric_conclusion"]

    score = composite_confidence_0_100(blended_read, reliability, completeness.completeness, integrity)

    all_flags = read.flags + shrink_flags + reliability_flags + integrity_flags + non_numeric_flag
    return ConclusionConfidence(
        feature_id=feature.pk, engine_number=feature.engine_number, feature_name=feature.feature_name,
        catchment_id=feature.catchment_id, business_category=feature.business_category or "",
        evidentiary_read=round(blended_read, 4), credibility_z=round(z, 4),
        engine_reliability=round(reliability, 4), completeness=completeness.completeness,
        integrity=integrity, confidence_score=round(score, 2), band=confidence_band(score),
        flags=all_flags,
    )


def confidence_evidence(
    conclusions: Sequence[ConclusionConfidence],
    query_bootstrap: Optional[BootstrapResult],
    completeness: CompletenessResult,
    reliability_by_engine: dict,
    single_engine_flag: bool,
) -> dict:
    """Structured evidence dict, same convention as every other engine's
    *_evidence function in this codebase."""
    return {
        "conclusions": [
            {
                "engine_number": c.engine_number, "feature_name": c.feature_name,
                "catchment_id": c.catchment_id, "business_category": c.business_category,
                "confidence_score": c.confidence_score, "band": c.band,
                "evidentiary_read": c.evidentiary_read, "credibility_z": c.credibility_z,
                "engine_reliability": c.engine_reliability, "flags": c.flags,
            }
            for c in conclusions
        ],
        "query_level": {
            "mean_confidence": round(sum(c.confidence_score for c in conclusions) / len(conclusions), 2) if conclusions else None,
            "bootstrap_ci_lower": round(query_bootstrap.ci_lower, 2) if query_bootstrap else None,
            "bootstrap_ci_upper": round(query_bootstrap.ci_upper, 2) if query_bootstrap else None,
            "bootstrap_n_resamples": query_bootstrap.n_resamples if query_bootstrap else None,
            "single_engine_conclusion_set": single_engine_flag,
        },
        "completeness": {
            "engines_expected": completeness.engines_expected,
            "engines_succeeded": completeness.engines_succeeded,
            "completeness_wilson_center": round(completeness.completeness, 4) if completeness.completeness is not None else None,
            "flags": completeness.flags,
        },
        "engine_reliability": {
            str(n): {
                "trials": r.trials, "reliability": round(r.reliability, 4),
                "interval_width": round(r.interval_width, 4), "flags": r.flags,
            }
            for n, r in reliability_by_engine.items()
        },
        "methods": [
            "jeffreys_1946_beta_binomial", "wilson_1927_score_interval",
            "buhlmann_1967_credibility", "undp_2010_weighted_geometric_mean",
            "efron_1979_bootstrap", "kahneman_tversky_1979_loss_aversion_optional",
        ],
    }


# ============================================================================
# Django-facing entry point
# ============================================================================

def run(query: BusinessQuery, risk_aversion: float = 0.0) -> dict:
    """
    Engine 24 entry point. For every latest business_conclusion-level
    FeatureObservation on record for query.location_cell, computes a
    per-conclusion confidence score (0-100) blending direct evidentiary
    support, this codebase's own historical operational reliability for
    the producing engine, this query's completeness, and Engine 23's
    drift/orphan audit -- then a query-level aggregate with a bootstrap
    confidence interval. Writes one FeatureObservation per conclusion
    plus a query-level confidence_dossier, and records an EngineRun.

    risk_aversion (0-1, default 0 = no adjustment): optionally re-expresses
    every confidence score via a documented, monotonic loss-aversion
    transform (see REAL METHODS #6) for callers who want a more
    conservative reading. Clamped to [0, 1] and flagged if out of range.

    Unlike Engines 2-22, this engine has no strict upstream requirement --
    it will happily run with zero conclusions on record (reporting an
    empty result, not an error) and works whether or not Engine 23 has
    run yet for this cell (see module docstring's honesty note).
    """
    started_at = django_timezone.now()
    cell = query.location_cell

    risk_flags = []
    if not (0.0 <= risk_aversion <= 1.0):
        risk_flags.append("risk_aversion_clamped")
        risk_aversion = max(0.0, min(1.0, risk_aversion))

    run_row = EngineRun.objects.create(
        query=query, engine_number=ENGINE_NUMBER, status="succeeded", started_at=started_at,
    )

    warnings_seen: list = list(risk_flags)
    features_written = 0
    dossier = None

    try:
        with transaction.atomic():
            conclusion_features = list(
                FeatureObservation.objects.filter(
                    location_cell=cell, is_latest=True, derivation_level="business_conclusion",
                ).exclude(engine_number__in=META_ENGINE_NUMBERS)
            )

            if not conclusion_features:
                warnings_seen.append("no_business_conclusion_feature_observations_found_for_this_cell")

            # --- Engine 23's dossier (genuine consumption, never recomputed) ---
            dossier_row = FeatureObservation.objects.filter(
                location_cell=cell, engine_number=23, feature_name="evidence_dossier", is_latest=True,
            ).first()
            evidence_dossier = dossier_row.value_json if dossier_row is not None else None
            if evidence_dossier is None:
                warnings_seen.append("no_evidence_engine_output_falling_back_to_unshrunk")

            mean_esi_row = FeatureObservation.objects.filter(
                location_cell=cell, engine_number=23, feature_name="mean_evidence_strength_index", is_latest=True,
            ).first()
            cell_prior_esi_0_1 = (
                float(mean_esi_row.value_numeric) / 100.0
                if mean_esi_row is not None and mean_esi_row.value_numeric is not None
                else None
            )

            flagged_feature_names = _flagged_feature_names(evidence_dossier)

            # --- Query completeness ---
            engine_runs_this_query = list(EngineRun.objects.filter(query=query).exclude(engine_number__in=META_ENGINE_NUMBERS))
            completeness = compute_query_completeness(engine_runs_this_query)
            warnings_seen.extend(completeness.flags)

            # --- Historical engine reliability, per DISTINCT engine_number present in conclusions ---
            distinct_engine_numbers = sorted(set(f.engine_number for f in conclusion_features))
            reliability_by_engine = {}
            for n in distinct_engine_numbers:
                historical_runs = list(EngineRun.objects.filter(engine_number=n))
                result = compute_engine_reliability(historical_runs)
                result.engine_number = n
                reliability_by_engine[n] = result
                warnings_seen.extend(f"engine_{n}: {flag}" for flag in result.flags)

            single_engine_flag = len(distinct_engine_numbers) == 1 and len(conclusion_features) > 0
            if single_engine_flag:
                warnings_seen.append("single_engine_conclusion_set")

            # --- Per-conclusion confidence ---
            conclusions = [
                compute_conclusion_confidence(
                    f, cell_prior_esi_0_1, reliability_by_engine, completeness, flagged_feature_names,
                )
                for f in conclusion_features
            ]

            raw_scores = [c.confidence_score for c in conclusions]
            query_bootstrap = bootstrap_mean_ci(raw_scores) if raw_scores else None
            if raw_scores and query_bootstrap is None:
                warnings_seen.append("insufficient_conclusions_for_bootstrap")

            if risk_aversion > 0.0:
                for c in conclusions:
                    c.confidence_score = risk_adjusted_confidence(c.confidence_score, risk_aversion)
                    c.band = confidence_band(c.confidence_score)

            dossier = confidence_evidence(conclusions, query_bootstrap, completeness, reliability_by_engine, single_engine_flag)
            dossier["risk_aversion_applied"] = risk_aversion
            dossier["warnings"] = warnings_seen

            # --- Writes ---
            # write_latest()'s `catchment` kwarg expects an actual Catchment
            # instance (it's a Django FK field), not a raw id -- ConclusionConfidence
            # only carries catchment_id, so resolve every id needed in one bulk
            # query up front rather than N individual lookups in the loop.
            _catchment_ids = {c.catchment_id for c in conclusions if c.catchment_id is not None}
            _catchments_by_id = Catchment.objects.in_bulk(_catchment_ids)

            for c in conclusions:
                FeatureObservation.write_latest(
                    location_cell=cell, catchment=_catchments_by_id.get(c.catchment_id),
                    engine_number=ENGINE_NUMBER, feature_name=f"confidence__{c.engine_number}__{c.feature_name}",
                    business_category=c.business_category,
                    defaults=dict(
                        value_type="numeric", value_numeric=Decimal(str(c.confidence_score)), unit="index_0_100",
                        observed_at=started_at, source="business_intel_internal", source_class="internal_derived",
                        method="confidence_engine_composite", derivation_level="business_conclusion",
                    ),
                )
                features_written += 1

            if raw_scores:
                mean_confidence = sum(raw_scores) / len(raw_scores)
                FeatureObservation.write_latest(
                    location_cell=cell, engine_number=ENGINE_NUMBER, feature_name="query_confidence_score",
                    defaults=dict(
                        value_type="numeric", value_numeric=Decimal(str(round(mean_confidence, 4))), unit="index_0_100",
                        observed_at=started_at, source="business_intel_internal", source_class="internal_derived",
                        method="confidence_engine_query_aggregate", derivation_level="business_conclusion",
                        sample_size=len(conclusions),
                    ),
                )
                features_written += 1

            FeatureObservation.write_latest(
                location_cell=cell, engine_number=ENGINE_NUMBER, feature_name="confidence_dossier",
                defaults=dict(
                    value_type="json", value_json=dossier, unit="",
                    observed_at=started_at, source="business_intel_internal", source_class="internal_derived",
                    method="confidence_engine_composite_audit", derivation_level="calculation",
                    sample_size=len(conclusions),
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

    return {"cell": cell, "dossier": dossier, "engine_run": run_row}