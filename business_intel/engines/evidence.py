"""
business_intel/engines/evidence.py

Engine 23 -- Evidence Engine
Per business_location_intelligence_engines_data_models_v2.md, Section 23.

NOTE ON THE SPEC FILE: same situation as every other engine in this
codebase -- business_location_intelligence_engines_data_models_v2.md is
referenced but not present on disk. Built to match Engine 2/3/4/5/6/7/8/9
for structure, schema contract, and citation discipline. The stub this
replaced gave only a purpose line: "Show exactly what evidence produced
a conclusion."

=============================================================================
SCOPE BOUNDARY VS. ENGINE 24 (read this before extending)
=============================================================================
Engine 24 (confidence.py, not yet built) will ask "how much should a
person TRUST this conclusion" -- a calibration question that can
legitimately fold in things this engine deliberately does not touch:
historical accuracy of this engine suite's past predictions, the
business cost of being wrong, and user-facing communication of
uncertainty.

This engine asks a narrower, prior question: "what evidence actually
produced this conclusion, how complete/traceable/corroborated is that
evidence, and has anything about it changed unexpectedly since it was
last computed?" That is evidentiary bookkeeping and audit, not a trust
judgment -- Engine 23 characterizes the evidence; Engine 24 (once built)
is expected to consume Engine 23's per-feature Evidence Strength Index
as one input among several, not to duplicate it. Same "deliberate, not
accidental" scope-boundary convention as Engine 6 vs. Engine 7's split.

=============================================================================
WHAT THIS ENGINE ACTUALLY MEASURES
=============================================================================
Four real, separately-cited questions, computed entirely from this
codebase's own already-written FeatureObservation and EngineRun rows
(no external API calls -- this engine's whole job is to look at what the
other 26 engines already produced and wrote to the database, not to
fetch anything new):

  1. HOW STRONG is the evidence behind each individual feature? --
     Evidence Strength Index (ESI), a weighted GEOMETRIC mean (not
     arithmetic -- see rationale below) of five real, cited components:
     source-class reliability, derivation-level distance from raw
     observation, recency, sample-size adequacy, and spatial/temporal
     coverage.

  2. HAS ANYTHING DRIFTED? -- for any feature with enough prior versions
     on record (FeatureObservation keeps full version history;
     write_latest never deletes old rows, it only flips is_latest), a
     robust, median-based outlier test flags a LATEST value that has
     moved suspiciously far from that feature's own recent history -- a
     real statistical check for "did this number just jump for a reason
     worth looking at," not an eyeballed before/after diff.

  3. HOW DIVERSE ARE THE SOURCES backing this location, overall? --
     Shannon entropy (same toolkit Engine 6 already applies to
     business-category mix, reused here for source_class mix) over the
     distribution of source classes contributing evidence to this
     location -- a cell whose entire evidence base is one source class
     is structurally weaker than one corroborated across several,
     independent of how "good" any single source is individually.

  4. IS THE EVIDENCE CHAIN INTACT? -- an audit of this query's EngineRun
     rows (which of the other 22 primary engines actually ran, which
     failed/degraded and why) plus a check for "orphaned"
     business_conclusion-level features: a conclusion with zero
     lower-derivation-level features on record from the SAME engine is
     flagged -- a conclusion should be traceable back to at least one
     calculation/observation from its own engine, not standing on
     nothing.

A fifth mechanism -- CROSS-ENGINE corroboration via Bland-Altman limits
of agreement between two independent estimates of a conceptually
equivalent quantity (e.g. the kind of KM-vs-Nelson-Aalen convergence
check Engine 7's own docstring already describes as a design PRINCIPLE)
is implemented as a reusable, generic function (bland_altman_agreement
below) and wired to an explicit CROSS_CHECK_FEATURE_PAIRS registry --
see that registry's own comment for why it ships empty rather than
guessing at feature_name pairs this module cannot verify are actually
being written yet.

=============================================================================
REAL METHODS USED (researched and cited, not invented)
=============================================================================
1. Weighted geometric mean of sub-indicator scores
   United Nations Development Programme (2010). Human Development
   Report 2010 -- The Real Wealth of Nations: Pathways to Human
   Development, Technical Note 1. UNDP switched the Human Development
   Index from an arithmetic to a geometric mean of its three sub-indices
   specifically because an arithmetic mean lets a very poor score on ONE
   dimension be masked by a strong score on another -- exactly the
   failure mode this engine must avoid (a feature with an excellent
   source but a genuinely tiny, undocumented sample size should NOT
   read as "high evidence strength"). Same rationale applied here across
   five evidentiary sub-components instead of HDI's three.

2. Error propagation across derivation steps
   Ku, H.H. (1966). "Notes on the Use of Propagation of Error Formulas."
   Journal of Research of the National Bureau of Standards, Section C:
   Engineering and Instrumentation, 70C(4), 263-273. Standard reference
   for why uncertainty compounds as a quantity passes through more
   computed/modeled steps -- grounds this engine's
   DERIVATION_LEVEL_WEIGHT ordering (observation > calculation >
   model_estimate > prediction): each additional layer of computation
   between a raw observation and a reported feature is itself a source
   of propagated, non-zero error, even when every individual step is
   correct.

3. Shannon entropy (source-class diversity)
   Shannon, C.E. (1948). "A Mathematical Theory of Communication." Bell
   System Technical Journal, 27(3), 379-423.
   H' = -sum(p_i * ln(p_i)) over the share p_i of evidence contributed
   by each distinct source_class present -- identical formula and
   rationale to Engine 6's category-diversity use, applied here to
   evidentiary source mix instead of business-category mix (same
   "genuinely different application of an identical mathematical form"
   note Engine 6's own docstring makes about HHI vs. Simpson).

4. Median Absolute Deviation (MAD) modified z-score for drift detection
   Iglewicz, B., Hoaglin, D.C. (1993). "How to Detect and Handle
   Outliers." The ASQC Basic References in Quality Control: Statistical
   Techniques, Vol. 16. ASQC Quality Press.
   M_i = 0.6745 * (x_i - median(x)) / MAD(x), |M_i| > 3.5 flagged as an
   outlier -- robust to the small, non-normal sample sizes a feature's
   own version history typically has (a handful of report re-runs),
   unlike a mean/standard-deviation z-score, which a single early
   outlier can itself distort.

5. Bland-Altman limits of agreement (cross-engine corroboration)
   Bland, J.M., Altman, D.G. (1986). "Statistical methods for assessing
   agreement between two methods of clinical measurement." Lancet,
   327(8476), 307-310.
   Bias = mean(a_i - b_i); 95% limits of agreement = bias +/- 1.96 *
   SD(a_i - b_i) -- the standard method for asking "do two independent
   ways of estimating the same underlying quantity actually agree,"
   reused here (outside its original clinical-measurement context, same
   kind of cross-domain borrowing as Engine 6's ecological-diversity-to-
   urban-economics move) for any registered pair of features meant to
   independently estimate a related real-world quantity.

6. Exponential recency decay ("information half-life")
   Standard exponential decay, half-life = the feature's own
   staleness_window_days (STALENESS_DAYS_BY_SOURCE_CLASS, already
   defined in models.py): recency_weight = 0.5 ** (age_days /
   staleness_window_days). A feature exactly at its own staleness
   boundary is scored at 50% recency strength, decaying continuously
   before and after that boundary rather than the underlying boolean
   is_stale flag's hard cutoff -- deliberately smoother than a yes/no
   cliff so two features one day apart in age don't get radically
   different scores.

7. Saturating-return sample-size weighting
   sample_weight = 1 - exp(-n / SAMPLE_SIZE_HALF_SATURATION) -- standard
   diminishing-returns functional form for "how much does one more
   observation still add," structurally identical to species-
   accumulation / sampling-effort curves in ecological survey design
   (Colwell, R.K., Coddington, J.A. (1994). "Estimating terrestrial
   biodiversity through extrapolation." Philosophical Transactions of
   the Royal Society B, 345(1311), 101-118) -- reused here so that going
   from n=5 to n=10 moves the score far more than going from n=500 to
   n=505, rather than a naive linear sample-size score that would treat
   both jumps identically. SAMPLE_SIZE_HALF_SATURATION = 30 uses the
   widely-cited (if informal) n=30 rule-of-thumb sample size as the
   point at which sample-size weight crosses 1 - 1/e ~ 0.632 -- a
   calibration choice of this module, not a claim that n=30 has any
   special theoretical status.

=============================================================================
WHAT'S ACTUALLY AVAILABLE VS. WHAT ISN'T
=============================================================================
Real data used: models.FeatureObservation (ALL versions for the cell,
not just is_latest -- drift detection specifically needs the full
history) scoped to query.location_cell, and models.EngineRun scoped to
THIS query (EngineRun is per-report-run, not per-cell, so "which engines
ran for this report" is correctly a query-scoped question while "what
evidence has this cell accumulated" is correctly a cell-scoped one --
these are deliberately two different scopes, not an inconsistency). No
external API calls. No dependency on any single OTHER engine's output
being present -- this engine is explicitly designed to run against
however many of the other 26 engines have actually executed, including
zero (see "zero evidence" handling below).

What is explicitly an approximation, flagged as such:
  - SOURCE_CLASS_RELIABILITY_WEIGHT below is this module's OWN
    calibration -- a domain-specific adaptation of the general
    hierarchy-of-evidence PRINCIPLE used in evidence-grading frameworks
    such as GRADE (Guyatt, G. et al. (2008). "GRADE: an emerging
    consensus on rating quality of evidence and strength of
    recommendations." BMJ, 336(7650), 924-926.), NOT a claim that
    GRADE's own weights (built for randomized-trial evidence in
    clinical medicine) apply unchanged to spatial/economic data. The
    borrowing is the METHODOLOGY -- grade evidence quality by source
    rigor, be explicit and consistent about the ranking -- not GRADE's
    specific numbers.
  - DERIVATION_LEVEL_WEIGHT's placement of "business_conclusion" is a
    genuine judgment call, not a strict "further = weaker" ladder: a
    business_conclusion feature is usually a relabeling of an
    underlying calculation/model_estimate for human consumption rather
    than an additional independent inferential step, so it is weighted
    close to (not below) model_estimate rather than at the bottom near
    "prediction" -- documented here rather than left as an unexplained
    number.
  - Cross-engine corroboration (Bland-Altman) only fires for pairs
    explicitly registered in CROSS_CHECK_FEATURE_PAIRS, which ships
    EMPTY. See that registry's own comment.

=============================================================================
EDGE CASES THIS MODULE HANDLES EXPLICITLY
=============================================================================
- Zero FeatureObservation rows for this cell at all (no other engine has
  run yet) -> every aggregate is None/empty, not 0-as-if-computed; same
  "data gap, not evidence of nothing" principle as Engine 6/7.
- A feature with source_class not in SOURCE_CLASS_RELIABILITY_WEIGHT
  (schema drift / a new choice added later) -> falls back to the same
  "unknown" weight the model layer itself uses as its conservative
  default, flagged rather than KeyError.
- A feature with derivation_level not in DERIVATION_LEVEL_WEIGHT -> falls
  back to model_estimate's weight (a conservative middle value), flagged.
- sample_size or coverage_pct is None -> that component is scored at a
  NEUTRAL (0.5) weight rather than 0 (missing metadata is not the same
  claim as "known to be small/unrepresentative") and the feature is
  flagged sample_size_undocumented / coverage_undocumented so the gap
  itself is visible in the record, not silently smoothed over.
- age_days is None (should not happen given auto_now, defensive only) ->
  recency component scored NEUTRAL (0.5), flagged.
- Drift detection: fewer than MIN_VERSIONS_FOR_DRIFT versions on record
  for a feature identity -> skipped entirely (a modified z-score on 1-2
  points is not a meaningful statistic), not computed and reported as
  "no drift."
- Drift detection: MAD == 0 (every prior version had the identical
  value) -> any change at all is reported as a qualitative
  "value_changed_from_constant_history" flag rather than a
  divide-by-zero or a fabricated infinite z-score.
- EngineRun query has zero rows for one or more of engine_numbers 1-22
  -> reported as "not_yet_run", a distinct status from "failed", since
  an engine simply not having executed yet is not itself an evidence
  problem.
- Cross-engine registry pair references a feature_name that does not
  exist for this cell -> that pair is silently skipped (not every
  registered pair is expected to be populated for every query), not an
  error.
- FeatureObservation.value_type != "numeric" for a feature otherwise
  eligible for drift/agreement checks -> excluded from BOTH drift
  detection and Bland-Altman agreement (categorical/JSON values have no
  meaningful numeric distance), never coerced.
- Fewer than 2 shared (catchment, business_category) contexts between a
  registered pair's two feature_names -> that pair's agreement is
  skipped (Bland-Altman needs at least 2 paired observations for a
  meaningful SD of differences).

Scope boundary
--------------
Depends on business_intel.models only (BusinessQuery, EngineRun,
FeatureObservation, STALENESS_DAYS_BY_SOURCE_CLASS). No dependency on
any specific OTHER engine's output being present, no external API calls
-- purely a read-and-audit layer over whatever this codebase's own
accumulated FeatureObservation/EngineRun history already contains.
"""

from __future__ import annotations

import logging
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional, Sequence

from django.db import transaction
from django.utils import timezone as django_timezone

from ..models import (
    STALENESS_DAYS_BY_SOURCE_CLASS,
    BusinessQuery,
    EngineRun,
    FeatureObservation,
)

logger = logging.getLogger("business_intel.engines.evidence")

ENGINE_NUMBER = 23

# Engines 23-27 are meta/synthesis engines that consume other engines'
# output rather than producing independent primary evidence -- excluded
# from the "which primary engines ran" audit below so Evidence auditing
# itself (or Confidence/Scenario/Decision/Cross-Engine once built) never
# gets flagged as missing evidence against itself.
META_ENGINE_NUMBERS = {23, 24, 25, 26, 27}
PRIMARY_ENGINE_NUMBERS = [n for n in range(1, 28) if n not in META_ENGINE_NUMBERS]

# ---------------------------------------------------------------------------
# Component weighting -- see "REAL METHODS USED" #1 for the geometric-mean
# rationale and #2/#6/#7 for why each component below is scored as it is.
# ---------------------------------------------------------------------------

SOURCE_CLASS_RELIABILITY_WEIGHT = {
    "government_survey": 0.95,
    "gridded_estimate": 0.80,
    "realtime_api": 0.75,
    "satellite": 0.70,
    "manual_entry": 0.65,
    "internal_derived": 0.60,
    "scraped_listing": 0.55,
    "unknown": 0.30,
}

DERIVATION_LEVEL_WEIGHT = {
    "observation": 1.00,
    "calculation": 0.90,
    "business_conclusion": 0.78,
    "model_estimate": 0.72,
    "prediction": 0.55,
}

# Sum to 1.0 -- see UNDP (2010) geometric-mean rationale above for why
# these combine multiplicatively rather than as a weighted sum.
COMPONENT_WEIGHTS = {
    "source": 0.30,
    "derivation": 0.25,
    "recency": 0.20,
    "sample_size": 0.15,
    "coverage": 0.10,
}
assert abs(sum(COMPONENT_WEIGHTS.values()) - 1.0) < 1e-9

NEUTRAL_COMPONENT_SCORE = 0.5    # used when a component's underlying metadata is undocumented
MIN_COMPONENT_FLOOR = 0.02       # keeps ln() finite for a genuinely worst-case component instead of -inf
SAMPLE_SIZE_HALF_SATURATION = 30

WEAK_EVIDENCE_THRESHOLD = 35.0   # ESI (0-100) at/below which a feature is flagged weak_evidence

MIN_VERSIONS_FOR_DRIFT = 3
DRIFT_MODIFIED_Z_THRESHOLD = 3.5  # Iglewicz & Hoaglin (1993)'s own recommended cutoff

# Cross-check registry: pairs of (feature_name_a, feature_name_b) that
# some OTHER engine's own docstring already describes as intentionally
# independent estimators of a related quantity (e.g. Engine 7's
# Kaplan-Meier vs. Nelson-Aalen). Ships EMPTY: this module cannot safely
# assert that any specific feature_name pair is actually being WRITTEN as
# a FeatureObservation row by another engine without reading that
# engine's current run() implementation, and most of Engines 1-22 are, at
# time of writing, still evolving. Populate as engines mature and a
# pair's exact feature_name strings are confirmed -- the
# bland_altman_agreement() machinery below is fully implemented and ready
# the moment a pair is added; nothing else needs to change.
CROSS_CHECK_FEATURE_PAIRS: list[tuple[str, str]] = []

Z_975 = 1.959963985  # standard normal 97.5th percentile (matches Engine 7's own hardcoded constant, no scipy)


# ============================================================================
# Small numeric helpers (median / MAD). No numpy/scipy in this environment,
# same convention as Engine 7 -- duplicated rather than imported from
# elsewhere per this codebase's established per-engine independent-footprint
# convention.
# ============================================================================

def _median(values: Sequence[float]) -> float:
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def _modified_z_scores(values: Sequence[float]):
    """Iglewicz & Hoaglin (1993) modified z-score: 0.6745*(x-median)/MAD.
    Returns (z_scores, median, mad). z_scores is a list of Nones (never
    raised) wherever MAD == 0 -- caller decides what a zero-MAD history
    means (see detect_feature_drift)."""
    if not values:
        return [], 0.0, 0.0
    median = _median(values)
    mad = _median([abs(v - median) for v in values])
    if mad == 0:
        return [None] * len(values), median, 0.0
    return [0.6745 * (v - median) / mad for v in values], median, mad


def shannon_entropy(counts: Sequence[int]) -> Optional[float]:
    """H' = -sum(p_i * ln(p_i)). Same formula/rationale as Engine 6's
    category-diversity use (Shannon 1948), applied here to source_class
    mix. Returns None for zero total count (no evidence at all -- a data
    gap, not a real entropy value of 0); a single category present
    correctly returns 0.0 (that IS the real entropy of a degenerate
    distribution, not a data gap)."""
    total = sum(counts)
    if total <= 0:
        return None
    h = 0.0
    for c in counts:
        if c <= 0:
            continue
        p = c / total
        h -= p * math.log(p)
    return h


# ============================================================================
# 1. Evidence Strength Index (per feature)
# ============================================================================

@dataclass
class EvidenceStrengthResult:
    feature_id: object
    feature_name: str
    engine_number: int
    esi: float
    components: dict
    flags: list = field(default_factory=list)


def _reliability_weight(source_class: str):
    weight = SOURCE_CLASS_RELIABILITY_WEIGHT.get(source_class)
    if weight is None:
        return SOURCE_CLASS_RELIABILITY_WEIGHT["unknown"], [f"unrecognized_source_class:{source_class}"]
    return weight, []


def _derivation_weight(derivation_level: str):
    weight = DERIVATION_LEVEL_WEIGHT.get(derivation_level)
    if weight is None:
        return DERIVATION_LEVEL_WEIGHT["model_estimate"], [f"unrecognized_derivation_level:{derivation_level}"]
    return weight, []


def _recency_weight(feature: FeatureObservation):
    age_days = feature.age_days
    if age_days is None:
        return NEUTRAL_COMPONENT_SCORE, ["recency_undetermined"]
    window = feature.staleness_window_days or STALENESS_DAYS_BY_SOURCE_CLASS["unknown"]
    if window <= 0:
        return NEUTRAL_COMPONENT_SCORE, ["nonpositive_staleness_window"]
    weight = 0.5 ** (max(age_days, 0) / window)
    return max(0.0, min(1.0, weight)), []


def _sample_size_weight(sample_size: Optional[int]):
    if sample_size is None:
        return NEUTRAL_COMPONENT_SCORE, ["sample_size_undocumented"]
    if sample_size <= 0:
        return MIN_COMPONENT_FLOOR, ["nonpositive_sample_size"]
    weight = 1.0 - math.exp(-sample_size / SAMPLE_SIZE_HALF_SATURATION)
    return max(0.0, min(1.0, weight)), []


def _coverage_weight(coverage_pct):
    if coverage_pct is None:
        return NEUTRAL_COMPONENT_SCORE, ["coverage_undocumented"]
    weight = float(coverage_pct) / 100.0
    return max(0.0, min(1.0, weight)), []


def compute_evidence_strength(feature: FeatureObservation) -> EvidenceStrengthResult:
    """Weighted geometric mean of five components (UNDP 2010 rationale --
    see module docstring #1). Each component is floored at
    MIN_COMPONENT_FLOOR before the log so a genuinely worst-case
    dimension drags the score toward, not to, zero."""
    flags: list = []

    source_w, f1 = _reliability_weight(feature.source_class)
    deriv_w, f2 = _derivation_weight(feature.derivation_level)
    recency_w, f3 = _recency_weight(feature)
    sample_w, f4 = _sample_size_weight(feature.sample_size)
    coverage_w, f5 = _coverage_weight(feature.coverage_pct)
    for fl in (f1, f2, f3, f4, f5):
        flags.extend(fl)

    components = {
        "source": source_w, "derivation": deriv_w, "recency": recency_w,
        "sample_size": sample_w, "coverage": coverage_w,
    }
    log_sum = sum(
        COMPONENT_WEIGHTS[name] * math.log(max(value, MIN_COMPONENT_FLOOR))
        for name, value in components.items()
    )
    esi = max(0.0, min(100.0, math.exp(log_sum) * 100.0))

    if esi <= WEAK_EVIDENCE_THRESHOLD:
        flags.append("weak_evidence")

    return EvidenceStrengthResult(
        feature_id=feature.pk, feature_name=feature.feature_name,
        engine_number=feature.engine_number, esi=esi, components=components, flags=flags,
    )


# ============================================================================
# 2. Version-over-version drift detection
# ============================================================================

@dataclass
class DriftFlag:
    feature_name: str
    latest_value: float
    prior_median: float
    mad: float
    modified_z: Optional[float]
    note: str


def detect_feature_drift(history: Sequence[FeatureObservation]) -> Optional[DriftFlag]:
    """history: ALL versions of one (location_cell, catchment,
    business_category, engine_number, feature_name) identity, numeric
    value_type only, any order. Flags the LATEST version if it is a
    modified-z outlier relative to the full history including itself
    (Iglewicz & Hoaglin recommend including the point under test in the
    reference set for small-n outlier screening)."""
    numeric_history = sorted(
        (f for f in history if f.value_type == "numeric" and f.value_numeric is not None),
        key=lambda f: f.version,
    )
    if len(numeric_history) < MIN_VERSIONS_FOR_DRIFT:
        return None

    values = [float(f.value_numeric) for f in numeric_history]
    latest = numeric_history[-1]
    z_scores, median, mad = _modified_z_scores(values)
    latest_z = z_scores[-1]

    if latest_z is None:
        # MAD == 0: every prior version identical. Any change from that
        # constant is itself the signal -- report qualitatively rather
        # than divide by zero or fabricate an infinite z-score.
        if values[-1] != values[0]:
            return DriftFlag(
                feature_name=latest.feature_name, latest_value=values[-1], prior_median=median,
                mad=0.0, modified_z=None, note="value_changed_from_constant_history",
            )
        return None

    if abs(latest_z) > DRIFT_MODIFIED_Z_THRESHOLD:
        return DriftFlag(
            feature_name=latest.feature_name, latest_value=values[-1], prior_median=median,
            mad=mad, modified_z=latest_z, note="latest_value_is_robust_outlier_vs_own_history",
        )
    return None


# ============================================================================
# 3. Cross-engine corroboration (Bland-Altman limits of agreement)
# ============================================================================

@dataclass
class AgreementResult:
    feature_a: str
    feature_b: str
    n_pairs: int
    bias: float
    loa_lower: float
    loa_upper: float
    coefficient_of_variation: Optional[float]


def bland_altman_agreement(pairs: Sequence[tuple]) -> Optional[AgreementResult]:
    """Bland & Altman (1986). pairs: list of (value_a, value_b), each
    pair from the SAME (catchment, business_category) context, for two
    feature_names registered as independent estimators of a related
    quantity. Returns None for fewer than 2 pairs (no meaningful SD of
    differences with n=1)."""
    if len(pairs) < 2:
        return None
    diffs = [a - b for a, b in pairs]
    n = len(diffs)
    mean_diff = sum(diffs) / n
    variance = sum((d - mean_diff) ** 2 for d in diffs) / (n - 1)
    sd = math.sqrt(variance)
    loa_lower = mean_diff - Z_975 * sd
    loa_upper = mean_diff + Z_975 * sd

    means = [(a + b) / 2 for a, b in pairs]
    grand_mean = sum(means) / n
    cv = (sd / abs(grand_mean)) if grand_mean != 0 else None

    return AgreementResult(
        feature_a="", feature_b="", n_pairs=n, bias=mean_diff,
        loa_lower=loa_lower, loa_upper=loa_upper, coefficient_of_variation=cv,
    )


# ============================================================================
# 4. Engine-run audit + orphaned-conclusion detection
# ============================================================================

def audit_engine_runs(engine_runs: Sequence[EngineRun]) -> dict:
    """Which of the 22 primary engines (see PRIMARY_ENGINE_NUMBERS) have
    run for THIS query, and with what status. 'not_yet_run' is its own
    status, distinct from 'failed' -- an engine simply not having
    executed yet is not itself an evidence problem."""
    latest_by_engine: dict = {}
    for run in engine_runs:
        current = latest_by_engine.get(run.engine_number)
        if current is None or run.started_at > current.started_at:
            latest_by_engine[run.engine_number] = run

    summary = {}
    for n in PRIMARY_ENGINE_NUMBERS:
        run = latest_by_engine.get(n)
        if run is None:
            summary[n] = {"status": "not_yet_run"}
            continue
        entry = {"status": run.status}
        if run.status == "failed":
            entry["error_message"] = run.error_message
        if run.status == "degraded":
            entry["degraded_reason"] = run.degraded_reason
        summary[n] = entry
    return summary


def detect_orphaned_conclusions(features_by_engine: dict) -> list:
    """A business_conclusion-level feature from engine E is 'orphaned' if
    engine E wrote NO calculation/model_estimate/observation-level
    feature at all (for the same latest-feature set) -- i.e. the
    conclusion is on record with nothing beneath it from its own engine
    to point to."""
    orphans = []
    for engine_number, feats in features_by_engine.items():
        conclusions = [f for f in feats if f.derivation_level == "business_conclusion"]
        if not conclusions:
            continue
        supporting = [f for f in feats if f.derivation_level in ("observation", "calculation", "model_estimate")]
        if not supporting:
            orphans.extend(f"engine_{engine_number}:{c.feature_name}" for c in conclusions)
    return orphans


# ============================================================================
# Engine entry point
# ============================================================================

def run(query: BusinessQuery) -> dict:
    """
    Engine 23 entry point. Audits every FeatureObservation on record for
    query.location_cell (all versions) and every EngineRun on record for
    THIS query, computes an Evidence Strength Index for each latest
    feature, checks version history for drift, audits which primary
    engines have run, checks for orphaned conclusions, and (where
    CROSS_CHECK_FEATURE_PAIRS is populated) checks cross-engine
    agreement -- then writes a cell-level evidence_dossier FeatureObservation
    plus several individual summary numeric features. Same contract shape
    as every other engine.

    Unlike Engines 2-22, this engine has NO upstream dependency
    requirement -- it runs against however much evidence already exists,
    including none (see 'zero evidence' handling in the module
    docstring).
    """
    started_at = django_timezone.now()
    cell = query.location_cell

    run_row = EngineRun.objects.create(
        query=query, engine_number=ENGINE_NUMBER, status="succeeded", started_at=started_at,
    )

    warnings_seen: list = []
    features_written = 0
    dossier = None

    try:
        with transaction.atomic():
            all_features = list(
                FeatureObservation.objects.filter(location_cell=cell).exclude(engine_number=ENGINE_NUMBER)
            )
            latest_features = [f for f in all_features if f.is_latest]
            engine_runs = list(EngineRun.objects.filter(query=query).exclude(engine_number=ENGINE_NUMBER))

            if not all_features:
                warnings_seen.append("no_feature_observations_found_for_this_cell -- no other engine has run yet.")

            # --- 1. Evidence Strength Index per latest feature ---
            esi_results = [compute_evidence_strength(f) for f in latest_features]
            weak = [r for r in esi_results if "weak_evidence" in r.flags]
            mean_esi = (sum(r.esi for r in esi_results) / len(esi_results)) if esi_results else None

            # --- 2. Drift detection across full version history ---
            history_by_identity = defaultdict(list)
            for f in all_features:
                key = (f.catchment_id, f.business_category, f.engine_number, f.feature_name)
                history_by_identity[key].append(f)
            drift_flags = [
                flag for flag in (detect_feature_drift(h) for h in history_by_identity.values())
                if flag is not None
            ]

            # --- 3. Source-class diversity (Shannon entropy) ---
            source_counts = Counter(f.source_class for f in latest_features)
            entropy = shannon_entropy(list(source_counts.values()))

            # --- 4. Engine-run audit + orphaned conclusions ---
            run_summary = audit_engine_runs(engine_runs)
            features_by_engine = defaultdict(list)
            for f in latest_features:
                features_by_engine[f.engine_number].append(f)
            orphans = detect_orphaned_conclusions(features_by_engine)

            # --- 5. Cross-engine corroboration (registry-driven, ships empty) ---
            agreement_results = []
            if CROSS_CHECK_FEATURE_PAIRS:
                by_name = defaultdict(list)
                for f in latest_features:
                    if f.value_type == "numeric" and f.value_numeric is not None:
                        by_name[f.feature_name].append(f)
                for name_a, name_b in CROSS_CHECK_FEATURE_PAIRS:
                    a_by_ctx = {(f.catchment_id, f.business_category): float(f.value_numeric) for f in by_name.get(name_a, [])}
                    b_by_ctx = {(f.catchment_id, f.business_category): float(f.value_numeric) for f in by_name.get(name_b, [])}
                    shared = set(a_by_ctx) & set(b_by_ctx)
                    if not shared:
                        continue
                    result = bland_altman_agreement([(a_by_ctx[k], b_by_ctx[k]) for k in shared])
                    if result is not None:
                        result.feature_a, result.feature_b = name_a, name_b
                        agreement_results.append(result)

            dossier = {
                "cell_geohash": cell.geohash,
                "total_evidence_count": len(latest_features),
                "mean_evidence_strength_index": round(mean_esi, 2) if mean_esi is not None else None,
                "weak_evidence_count": len(weak),
                "weak_evidence_features": [
                    {"engine_number": r.engine_number, "feature_name": r.feature_name, "esi": round(r.esi, 2)}
                    for r in weak
                ],
                "drift_flags": [
                    {
                        "feature_name": d.feature_name, "latest_value": d.latest_value,
                        "prior_median": d.prior_median,
                        "modified_z": round(d.modified_z, 2) if d.modified_z is not None else None,
                        "note": d.note,
                    }
                    for d in drift_flags
                ],
                "source_class_distribution": dict(source_counts),
                "source_diversity_entropy": round(entropy, 4) if entropy is not None else None,
                "engine_run_summary": run_summary,
                "orphaned_conclusions": orphans,
                "cross_engine_agreement": [
                    {
                        "feature_a": a.feature_a, "feature_b": a.feature_b, "n_pairs": a.n_pairs,
                        "bias": round(a.bias, 4), "loa_lower": round(a.loa_lower, 4), "loa_upper": round(a.loa_upper, 4),
                        "coefficient_of_variation": round(a.coefficient_of_variation, 4) if a.coefficient_of_variation is not None else None,
                    }
                    for a in agreement_results
                ],
                "warnings": warnings_seen,
            }

            FeatureObservation.write_latest(
                location_cell=cell, engine_number=ENGINE_NUMBER, feature_name="evidence_dossier",
                defaults=dict(
                    value_type="json", value_json=dossier, unit="",
                    observed_at=started_at, source="business_intel_internal", source_class="internal_derived",
                    method="evidence_engine_composite_audit", derivation_level="calculation",
                    sample_size=len(latest_features),
                ),
            )
            features_written += 1

            if mean_esi is not None:
                FeatureObservation.write_latest(
                    location_cell=cell, engine_number=ENGINE_NUMBER, feature_name="mean_evidence_strength_index",
                    defaults=dict(
                        value_type="numeric", value_numeric=Decimal(str(round(mean_esi, 4))), unit="index_0_100",
                        observed_at=started_at, source="business_intel_internal", source_class="internal_derived",
                        method="undp_2010_weighted_geometric_mean", derivation_level="calculation",
                        sample_size=len(esi_results),
                    ),
                )
                features_written += 1

            if entropy is not None:
                FeatureObservation.write_latest(
                    location_cell=cell, engine_number=ENGINE_NUMBER, feature_name="source_diversity_entropy",
                    defaults=dict(
                        value_type="numeric", value_numeric=Decimal(str(round(entropy, 6))), unit="nats",
                        observed_at=started_at, source="business_intel_internal", source_class="internal_derived",
                        method="shannon_1948", derivation_level="calculation",
                        sample_size=len(latest_features),
                    ),
                )
                features_written += 1

            FeatureObservation.write_latest(
                location_cell=cell, engine_number=ENGINE_NUMBER, feature_name="weak_evidence_feature_count",
                defaults=dict(
                    value_type="numeric", value_numeric=Decimal(len(weak)), unit="count",
                    observed_at=started_at, source="business_intel_internal", source_class="internal_derived",
                    method="evidence_engine_threshold_35", derivation_level="calculation",
                ),
            )
            features_written += 1

            if drift_flags:
                warnings_seen.append(f"{len(drift_flags)} feature(s) flagged for version-over-version drift.")
            if orphans:
                warnings_seen.append(
                    f"{len(orphans)} business_conclusion feature(s) have no supporting lower-level evidence from their own engine."
                )

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
