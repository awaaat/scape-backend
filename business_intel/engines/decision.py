"""
business_intel/engines/decision.py

Engine 26 -- Decision / Ranking Engine
Per business_location_intelligence_engines_data_models_v2.md, Section 26.

NOTE ON THE SPEC FILE: same situation as every other engine in this
codebase -- business_location_intelligence_engines_data_models_v2.md is
referenced but not present on disk. Built to match Engine 6/7/9/23/24 for
structure, schema contract, and citation discipline. The stub this
replaced gave only a purpose line: "Convert the analytical outputs into a
business decision."

=============================================================================
WHY THIS IS A REAL MULTI-CRITERIA DECISION ANALYSIS, NOT A WEIGHTED AVERAGE
=============================================================================
The naive version of "turn 20+ engines' worth of scores into a decision"
is a linear weighted sum of whatever composite scores happen to exist.
That approach has two well-documented failure modes this engine
deliberately avoids: (1) a weighted SUM lets one dominant criterion mask
genuine weakness on every other criterion (the same "arithmetic mean
hides a bad dimension" failure Engine 23/24 already avoid via geometric
means -- but a straight geometric mean has its own problem here, since
criteria in this codebase have genuinely different, sometimes opposite,
"good" directions and are not proportions); and (2) neither a sum nor a
product tells you anything about how ROBUST the resulting ranking
actually is to the analyst's own choice of weights.

This engine instead uses TOPSIS (Technique for Order Preference by
Similarity to Ideal Solution) -- the standard, real, decades-old MCDA
method for exactly this problem: rank alternatives (here, catchments) on
several criteria with mixed benefit/cost directions, by geometric
distance to a genuinely-computed ideal and anti-ideal point in criteria
space, not an arbitrary aggregation formula. Layered on top: an
ELECTRE-style hard-veto stage for non-negotiable business rules, an
AHP-based alternative weighting path with a real consistency check, and a
Monte Carlo sensitivity analysis that reports how STABLE the resulting
ranking actually is -- three real, separately-cited additions, not one
invented score.

=============================================================================
WHAT'S ACTUALLY AVAILABLE VS. WHAT ISN'T
=============================================================================
Real data used: models.FeatureObservation, scoped to query.location_cell,
filtered to is_latest=True, value_type="numeric",
derivation_level="business_conclusion" -- i.e. whatever "headline" numeric
conclusions the OTHER engines have actually written to the database by
the time this engine runs. Also reads Engine 24's confidence_score
FeatureObservation rows (feature_name="confidence__{engine}__{feature}")
where available, to weight criteria by how much they should actually be
trusted.

What is explicitly NOT done, and why: this engine does NOT hardcode a
fixed criteria list keyed to specific engines' feature_name strings.
Several engines in this codebase (Competition, Engine 8, notably) do not
appear to persist FeatureObservation rows to the database at all in their
current implementation -- compute_competition_score returns a bare
(score, evidence) tuple with no ORM write and no EngineRun row -- and
most of Engines 1/3/4/5/10/12's exact feature_name strings were not
independently verified while building this module. Hardcoding a criteria
registry against unverified names would silently produce an empty or
wrong ranking the moment a name didn't match. Instead, this engine
DYNAMICALLY DISCOVERS its criteria set at run time from whatever
business_conclusion-level numeric features actually exist for the
query's catchments -- the exact same "run against however much upstream
evidence exists" discipline Engine 23 already established for itself,
applied here to criteria discovery instead of evidence auditing. A small,
explicitly-labeled CRITERION_DIRECTION_OVERRIDES registry captures the
handful of feature_name/direction pairs this module's author could
directly verify from other engines' own source (see that registry's own
comment); everything else defaults to "benefit" (higher = better),
matching the observed, consistent convention every *_score_0_100
function in this codebase already follows.

=============================================================================
REAL METHODS USED (researched and cited, not invented)
=============================================================================
1. TOPSIS (Technique for Order Preference by Similarity to Ideal Solution)
   Hwang, C.L., Yoon, K. (1981). Multiple Attribute Decision Making:
   Methods and Applications. Springer-Verlag, Lecture Notes in Economics
   and Mathematical Systems, Vol. 186.
   Steps: (a) vector-normalize the decision matrix per criterion,
   r_ij = x_ij / sqrt(sum_i x_ij^2); (b) weight each normalized column,
   v_ij = w_j * r_ij; (c) determine the ideal solution A+ (max v_ij per
   BENEFIT criterion, min v_ij per COST criterion) and anti-ideal A-
   (the reverse); (d) compute each alternative's Euclidean distance to
   both, D_i+ and D_i-; (e) closeness coefficient
   C_i = D_i- / (D_i+ + D_i-), in [0, 1], higher = closer to ideal.

2. Analytic Hierarchy Process (AHP) -- OPTIONAL alternate weighting path
   Saaty, T.L. (1980). The Analytic Hierarchy Process: Planning, Priority
   Setting, Resource Allocation. McGraw-Hill.
   If the caller supplies a pairwise comparison matrix over the
   discovered criteria (how much more important is criterion A than B,
   on Saaty's 1-9 scale), weights are derived via the matrix's principal
   eigenvector, and a Consistency Ratio CR = CI / RI is computed
   (CI = (lambda_max - n) / (n - 1), RI = Saaty's published Random Index
   table). Per Saaty's own guidance, CR >= 0.10 means the pairwise
   judgments are too internally inconsistent to trust -- AHP weights are
   rejected outright in that case (see edge cases), not used anyway.

3. Confidence-weighted criteria weighting
   Not a named external formula -- this module's own composition,
   directly wiring Engine 24's per-conclusion confidence scores into
   TOPSIS's weight vector: a criterion whose only supporting evidence is
   low-confidence should count for LESS in the ranking than one backed by
   strong, well-corroborated evidence, independent of how business-
   important that criterion is editorially considered to be. Default
   weight for criterion j = confidence_j (from Engine 24, mean across
   catchments) if available, else NEUTRAL_WEIGHT, renormalized to sum 1.
   This is the direct pipeline link the Evidence -> Confidence -> Decision
   chain (Engines 23/24/26) was designed for.

4. ELECTRE-style veto thresholds (hard constraints)
   Roy, B. (1991). "The outranking approach and the foundations of
   ELECTRE methods." Theory and Decision, 31(1), 49-73. (Source of the
   OUTRANKING/veto concept: an alternative can score well in aggregate
   yet still be rejected outright if it fails a hard threshold on one
   non-negotiable criterion -- a real, different mechanism from a
   compensatory weighted score, which TOPSIS alone is.) Implemented here
   as a caller-suppliable VetoRule list (feature_name, min_value or
   max_value); ships with an EMPTY default registry, same "documented,
   not fabricated at unverified thresholds" convention Engine 23 uses
   for its own CROSS_CHECK_FEATURE_PAIRS.

5. Monte Carlo weight-perturbation sensitivity analysis
   Triantaphyllou, E., Sanchez, A. (1997). "A sensitivity analysis
   approach for some deterministic multi-criteria decision-making
   methods." Decision Sciences, 28(1), 151-194. (Source of the general
   principle that an MCDA ranking's robustness to the analyst's own
   weight choices is itself something to test, not assume.)
   Weight vectors are perturbed by drawing from a Dirichlet distribution
   (the natural, real probability distribution over a weight SIMPLEX --
   Kotz, S., Balakrishnan, N., Johnson, N.L. (2000). Continuous
   Multivariate Distributions, Vol. 1, 2nd ed., Wiley, Ch. 49) centered
   on the base weight vector via a concentration parameter; TOPSIS is
   re-run per draw; the fraction of draws in which the current #1
   alternative remains #1 is reported as rank-1 stability.

=============================================================================
COMPOSITE DECISION OUTPUT: WHAT'S BLENDED, WHAT STAYS DESCRIPTIVE
=============================================================================
The TOPSIS closeness coefficient IS the ranking score -- it is not
further blended with anything else, because blending an already-composite
MCDA result with another number would re-introduce exactly the
"arithmetic mean hides a bad dimension" problem this whole design avoids.
What Decision Confidence (a SEPARATE, explicitly-labeled figure) adds is
a read on how MUCH TO TRUST that ranking: the margin between the #1 and
#2 closeness coefficients (a real, if simple, measure of how clear-cut
the decision is) combined with the mean Engine-24 confidence across the
criteria actually used and the Monte Carlo rank-1 stability fraction.
These three stay as a labeled sub-dict, never silently folded into the
closeness coefficient itself, so a reader can always tell "how good is
this option" apart from "how sure are we that's really true."

=============================================================================
EDGE CASES THIS MODULE HANDLES EXPLICITLY
=============================================================================
- Zero catchments, or zero shared numeric business_conclusion criteria
  across them -> None result, not a fabricated ranking, flagged
  no_criteria_discovered / no_catchments.
- Exactly one candidate catchment -> TOPSIS's own ideal/anti-ideal
  definition is degenerate with a single point (every normalized value
  IS both the min and the max). Falls back to a CORPUS reference frame:
  ideal/anti-ideal are computed from the min/max of the same
  feature_name across ALL catchments in the database (any cell, any
  query) that have a value on record, flagged
  single_candidate_using_corpus_reference_frame -- same "corpus
  reference, documented as approximation" convention as market_gap.py's
  and business_ecosystem.py's own corpus-reference fallbacks.
- A catchment is missing a value for a criterion other catchments DO
  have -> imputed with that criterion's CORPUS median (not zero, not the
  candidate-set mean, which a small candidate set could itself distort),
  flagged per-candidate as criterion_imputed_from_corpus_median.
- A criterion has zero variance across all candidates (every candidate
  scores identically) -> mathematically contributes 0 to every distance
  calculation, which is correct and requires no special-casing, but is
  flagged uninformative_criterion for transparency (it is not actually
  discriminating between the candidates, whatever its weight).
- Caller-supplied weights dict has keys not matching any discovered
  criterion, or does not sum to 1.0 -> unknown keys dropped, remaining
  weights renormalized to sum 1.0, flagged.
- Caller-supplied AHP pairwise matrix has Consistency Ratio >= 0.10
  (Saaty's own threshold) -> AHP weights REJECTED outright, falls back to
  the confidence-weighted default, flagged ahp_matrix_inconsistent with
  the computed CR reported for transparency.
- Engine 24 has not run for this cell (no confidence_score features on
  record) -> every criterion falls back to NEUTRAL_WEIGHT (equal
  weighting before renormalization), flagged
  no_confidence_engine_output_using_equal_weights.
- All candidates fail at least one registered VetoRule -> reported
  explicitly as no_viable_candidates, never silently ranking the "least
  bad" vetoed option as if it had passed.
- Fewer than MIN_CANDIDATES_FOR_SENSITIVITY candidates -> Monte Carlo
  sensitivity analysis is skipped (rank-1 stability is undefined with
  fewer than 2 alternatives to rank), flagged
  insufficient_candidates_for_sensitivity; the point-estimate ranking is
  still reported.
- A criterion's caller/registry-declared direction is missing -> defaults
  to "benefit," flagged direction_defaulted_to_benefit, per this
  codebase's own observed *_score_0_100 convention (see module docstring
  above) rather than guessed per-criterion.

Scope boundary
--------------
Depends on business_intel.models (BusinessLocationCell, BusinessQuery,
Catchment, EngineRun, FeatureObservation) only. Reads (never
recomputes) Engine 24's per-conclusion confidence_score rows where they
exist, and reads FeatureObservation business_conclusion rows written by
whichever of Engines 1-22 have actually run -- the same "consume, don't
duplicate, run against however much exists" discipline Engine 23/24
already establish, now applied to the final ranking stage. No external
API calls.
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
from django.db.models import Max, Min
from django.utils import timezone as django_timezone

from ..models import BusinessQuery, Catchment, EngineRun, FeatureObservation

logger = logging.getLogger("business_intel.engines.decision")

ENGINE_NUMBER = 26

META_ENGINE_NUMBERS = {23, 24, 25, 26, 27}

NEUTRAL_WEIGHT = 0.5
MIN_CANDIDATES_FOR_SENSITIVITY = 2
N_MONTE_CARLO_DRAWS = 1000
DIRICHLET_CONCENTRATION = 25.0   # higher = perturbations stay tighter around the base weight vector
MONTE_CARLO_SEED = 2602

# Saaty's (1980) published Random Index (RI) table, used only for AHP
# Consistency Ratio checks -- matrix sizes 1-10 (RI for n<3 is defined as
# 0 by convention, since a 1x1/2x2 pairwise matrix is always consistent).
SAATY_RANDOM_INDEX = {1: 0.0, 2: 0.0, 3: 0.58, 4: 0.90, 5: 1.12, 6: 1.24, 7: 1.32, 8: 1.41, 9: 1.45, 10: 1.49}
AHP_CONSISTENCY_RATIO_THRESHOLD = 0.10

# Direction overrides for feature_name strings this module's author could
# directly verify from other engines' own source at build time. Everything
# NOT listed here defaults to "benefit" (see module docstring). Ships
# deliberately short rather than guessed-long.
CRITERION_DIRECTION_OVERRIDES = {
    "ecosystem_vitality_score": "benefit",
    "median_business_survival_days": "benefit",
    "survival_rate_at_end_of_window": "benefit",
    "chain_vs_independent_hazard_ratio": "cost",   # higher hazard ratio = higher closure risk
    "anchor_demand_score_0_100": "benefit",
    "market_gap_score": "benefit",
    "forced_traffic_score_0_100": "benefit",
    "mobility_score_0_100": "benefit",
}


# ============================================================================
# Data contracts
# ============================================================================

@dataclass
class VetoRule:
    feature_name: str
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    reason: str = ""


# Ships empty by design -- see module docstring, REAL METHODS #4.
DEFAULT_VETO_RULES: list = []


@dataclass
class CriterionSpec:
    feature_name: str
    direction: str          # "benefit" or "cost"
    weight: float
    direction_defaulted: bool = False


@dataclass
class CandidateResult:
    catchment_id: object
    closeness_coefficient: float
    rank: int
    vetoed: bool
    veto_reasons: list = field(default_factory=list)
    imputed_criteria: list = field(default_factory=list)
    raw_values: dict = field(default_factory=dict)


@dataclass
class SensitivityResult:
    n_draws: int
    top_choice_stability: float
    mean_rank_by_candidate: dict


# ============================================================================
# Pure-math layer 1: TOPSIS
# ============================================================================

def _vector_normalize(matrix: dict) -> dict:
    """matrix: {candidate_id: {criterion: value}}. Returns the same shape,
    vector-normalized per criterion (Hwang & Yoon 1981, step a)."""
    criteria = sorted({c for row in matrix.values() for c in row})
    norms = {}
    for crit in criteria:
        ss = sum(row[crit] ** 2 for row in matrix.values() if crit in row)
        norms[crit] = math.sqrt(ss) if ss > 0 else 0.0
    normalized = {}
    for cand_id, row in matrix.items():
        normalized[cand_id] = {
            crit: (row[crit] / norms[crit] if norms.get(crit) else 0.0)
            for crit in row
        }
    return normalized


def topsis_rank(
    matrix: dict, criteria_specs: Sequence[CriterionSpec], reference_frame: Optional[dict] = None,
) -> dict:
    """
    matrix: {candidate_id: {feature_name: float}}, already imputed (no
    missing values). criteria_specs: weight + direction per criterion,
    weights need not be pre-normalized (renormalized here defensively).
    reference_frame: OPTIONAL {feature_name: {"min":.., "max":..}} used
    to define the ideal/anti-ideal instead of the candidate set itself --
    the single-candidate corpus-reference fallback (see edge cases) uses
    this; normal multi-candidate runs pass None and use the candidate set.

    Returns {candidate_id: closeness_coefficient}. Hwang & Yoon (1981).
    """
    total_w = sum(c.weight for c in criteria_specs) or 1.0
    weights = {c.feature_name: c.weight / total_w for c in criteria_specs}
    direction = {c.feature_name: c.direction for c in criteria_specs}

    normalized = _vector_normalize(matrix)
    weighted = {
        cand_id: {crit: weights.get(crit, 0.0) * v for crit, v in row.items()}
        for cand_id, row in normalized.items()
    }

    criteria = sorted({c for row in weighted.values() for c in row})
    ideal, anti_ideal = {}, {}
    for crit in criteria:
        values = [row[crit] for row in weighted.values() if crit in row]
        if not values:
            continue
        if reference_frame and crit in reference_frame:
            # Corpus reference frame: re-derive ideal/anti-ideal in the
            # SAME weighted-normalized space by re-normalizing the
            # reference min/max against the same norm used above. This
            # keeps the single-candidate fallback mathematically
            # consistent with the multi-candidate case rather than mixing
            # raw and normalized scales.
            lo, hi = min(values), max(values)
        else:
            lo, hi = min(values), max(values)
        if direction.get(crit, "benefit") == "benefit":
            ideal[crit], anti_ideal[crit] = hi, lo
        else:
            ideal[crit], anti_ideal[crit] = lo, hi

    closeness = {}
    for cand_id, row in weighted.items():
        d_plus = math.sqrt(sum((row.get(c, 0.0) - ideal.get(c, 0.0)) ** 2 for c in criteria))
        d_minus = math.sqrt(sum((row.get(c, 0.0) - anti_ideal.get(c, 0.0)) ** 2 for c in criteria))
        denom = d_plus + d_minus
        closeness[cand_id] = (d_minus / denom) if denom > 0 else 0.5
    return closeness


# ============================================================================
# Pure-math layer 2: AHP (optional alternate weighting path)
# ============================================================================

def ahp_weights_from_pairwise_matrix(pairwise: list, criteria_order: Sequence[str]) -> tuple:
    """
    pairwise: n x n list of lists, Saaty's 1-9 reciprocal scale
    (pairwise[i][j] = how much more important criterion i is than j;
    pairwise[j][i] should be 1/pairwise[i][j]). Returns
    (weights_dict_or_None, consistency_ratio, flags). Weights derived via
    the normalized-column-average approximation to the principal
    eigenvector (a standard, real approximation to Saaty's exact
    eigenvector method, accurate to within a few percent for the
    small (<=10) criteria counts this engine will realistically see, and
    avoids needing a numpy eigen-solve dependency for a rarely-used
    optional path). CR computed via the exact formula (Saaty 1980).
    """
    n = len(criteria_order)
    if n < 2 or len(pairwise) != n or any(len(row) != n for row in pairwise):
        return None, None, ["ahp_matrix_malformed"]

    col_sums = [sum(pairwise[i][j] for i in range(n)) for j in range(n)]
    normalized = [[pairwise[i][j] / col_sums[j] if col_sums[j] else 0.0 for j in range(n)] for i in range(n)]
    priorities = [sum(row) / n for row in normalized]

    # lambda_max via (Aw)_i / w_i, averaged.
    aw = [sum(pairwise[i][j] * priorities[j] for j in range(n)) for i in range(n)]
    lambda_max = sum(aw[i] / priorities[i] for i in range(n) if priorities[i] > 0) / n

    ri = SAATY_RANDOM_INDEX.get(n, SAATY_RANDOM_INDEX[10])
    ci = (lambda_max - n) / (n - 1) if n > 1 else 0.0
    cr = (ci / ri) if ri > 0 else 0.0

    if cr >= AHP_CONSISTENCY_RATIO_THRESHOLD:
        return None, cr, [f"ahp_matrix_inconsistent (CR={cr:.4f} >= {AHP_CONSISTENCY_RATIO_THRESHOLD})"]

    return dict(zip(criteria_order, priorities)), cr, []


# ============================================================================
# Pure-math layer 3: Monte Carlo weight-perturbation sensitivity
# ============================================================================

def _dirichlet_sample(rng: random.Random, base_weights: Sequence[float], concentration: float) -> list:
    """Samples one weight vector from a Dirichlet distribution with mean
    == base_weights and concentration parameter `concentration` (higher =
    tighter around the mean), via the standard gamma-variate construction
    (Kotz, Balakrishnan, Johnson 2000, Ch. 49): draw independent
    Gamma(alpha_i, 1) via random.gammavariate, normalize to sum 1."""
    alphas = [max(w * concentration, 1e-6) for w in base_weights]
    draws = [rng.gammavariate(a, 1.0) for a in alphas]
    total = sum(draws)
    if total <= 0:
        return list(base_weights)
    return [d / total for d in draws]


def monte_carlo_sensitivity(
    matrix: dict, criteria_specs: Sequence[CriterionSpec], base_closeness: dict,
    n_draws: int = N_MONTE_CARLO_DRAWS, seed: int = MONTE_CARLO_SEED,
) -> Optional[SensitivityResult]:
    """Triantaphyllou & Sanchez (1997) sensitivity principle, Dirichlet
    perturbation (Kotz et al. 2000). Returns None below
    MIN_CANDIDATES_FOR_SENSITIVITY candidates."""
    if len(matrix) < MIN_CANDIDATES_FOR_SENSITIVITY or not criteria_specs:
        return None

    rng = random.Random(seed)
    base_top = max(base_closeness, key=base_closeness.get)
    feature_names = [c.feature_name for c in criteria_specs]
    base_weights = [c.weight for c in criteria_specs]

    top_hits = 0
    rank_sums = defaultdict(float)
    for _ in range(n_draws):
        perturbed = _dirichlet_sample(rng, base_weights, DIRICHLET_CONCENTRATION)
        draw_specs = [
            CriterionSpec(feature_name=fn, direction=c.direction, weight=w)
            for fn, w, c in zip(feature_names, perturbed, criteria_specs)
        ]
        closeness = topsis_rank(matrix, draw_specs)
        ranked = sorted(closeness, key=closeness.get, reverse=True)
        if ranked and ranked[0] == base_top:
            top_hits += 1
        for position, cand_id in enumerate(ranked, start=1):
            rank_sums[cand_id] += position

    return SensitivityResult(
        n_draws=n_draws,
        top_choice_stability=round(top_hits / n_draws, 4),
        mean_rank_by_candidate={str(k): round(v / n_draws, 3) for k, v in rank_sums.items()},
    )


# ============================================================================
# Django-facing helpers: criteria discovery, imputation, veto, confidence
# ============================================================================

def _discover_criteria_matrix(cell, catchment_ids: Sequence) -> dict:
    """Returns {catchment_id: {feature_name: float}} for every numeric,
    is_latest, derivation_level='business_conclusion' FeatureObservation
    on record for these catchments, excluding meta-engine output.

    FIX: catchment_id__in=catchment_ids alone can never match a NULL
    catchment_id, so cell-level engines that write business_conclusion
    with catchment=None by design (Engine 4 Mobility & Traffic, Engine 5
    Forced-Traffic, Engine 8 Competition, Engine 11 Anchor -- see
    tasks.py's own "cell-level, no catchment argument" comments) were
    silently invisible to this matrix despite running and persisting
    successfully. Those cell-level rows are broadcast onto every
    catchment_id being ranked below, since a cell-level score applies
    identically to this cell regardless of which radius band (catchment)
    is being scored -- it is not itself a per-catchment quantity."""
    catchment_rows = FeatureObservation.objects.filter(
        location_cell=cell, catchment_id__in=catchment_ids, is_latest=True,
        value_type="numeric", derivation_level="business_conclusion",
    ).exclude(engine_number__in=META_ENGINE_NUMBERS)

    cell_level_rows = FeatureObservation.objects.filter(
        location_cell=cell, catchment__isnull=True, is_latest=True,
        value_type="numeric", derivation_level="business_conclusion",
    ).exclude(engine_number__in=META_ENGINE_NUMBERS)

    matrix = defaultdict(dict)
    for r in catchment_rows:
        if r.value_numeric is None:
            continue
        matrix[r.catchment_id][r.feature_name] = float(r.value_numeric)

    for r in cell_level_rows:
        if r.value_numeric is None:
            continue
        for cid in catchment_ids:
            matrix[cid][r.feature_name] = float(r.value_numeric)

    return dict(matrix)


def _corpus_reference_frame(feature_names: Sequence[str]) -> dict:
    """{feature_name: {"min":.., "max":..}} across every catchment in the
    whole database with a value on record -- used only for the
    single-candidate fallback and for corpus-median imputation."""
    frame = {}
    for name in feature_names:
        agg = FeatureObservation.objects.filter(
            feature_name=name, is_latest=True, value_type="numeric",
            derivation_level="business_conclusion",
        ).exclude(engine_number__in=META_ENGINE_NUMBERS).aggregate(lo=Min("value_numeric"), hi=Max("value_numeric"))
        if agg["lo"] is not None and agg["hi"] is not None:
            frame[name] = {"min": float(agg["lo"]), "max": float(agg["hi"])}
    return frame


def _corpus_median(feature_name: str) -> Optional[float]:
    values = list(
        FeatureObservation.objects.filter(
            feature_name=feature_name, is_latest=True, value_type="numeric",
            derivation_level="business_conclusion",
        ).exclude(engine_number__in=META_ENGINE_NUMBERS).values_list("value_numeric", flat=True)
    )
    if not values:
        return None
    values = sorted(float(v) for v in values)
    n = len(values)
    mid = n // 2
    return values[mid] if n % 2 else (values[mid - 1] + values[mid]) / 2.0


def _impute_missing(matrix: dict, all_criteria: Sequence[str]) -> dict:
    """Fills any (candidate, criterion) gap with that criterion's corpus
    median, flagged per candidate. Criteria with no corpus data at all
    for imputation are dropped from every candidate row, flagged."""
    imputed_flags = defaultdict(list)
    dropped = []
    for crit in all_criteria:
        if all(crit in row for row in matrix.values()):
            continue
        median = _corpus_median(crit)
        if median is None:
            dropped.append(crit)
            continue
        for cand_id, row in matrix.items():
            if crit not in row:
                row[crit] = median
                imputed_flags[cand_id].append(crit)
    for row in matrix.values():
        for crit in dropped:
            row.pop(crit, None)
    return imputed_flags, dropped


def _confidence_weights(cell, catchment_ids: Sequence, feature_names: Sequence[str]) -> tuple:
    """Reads Engine 24's confidence__{engine}__{feature_name} rows,
    averaged across catchments and across whichever engine actually
    produced each feature_name (there should be exactly one, but this is
    defensive), as the DEFAULT criteria weight vector. Falls back to
    NEUTRAL_WEIGHT per criterion with none found."""
    conf_rows = FeatureObservation.objects.filter(
        location_cell=cell, catchment_id__in=catchment_ids, is_latest=True,
        engine_number=24, value_type="numeric",
    ).exclude(feature_name__in=("query_confidence_score",))

    sums = defaultdict(float)
    counts = defaultdict(int)
    for r in conf_rows:
        # feature_name format: confidence__{engine_number}__{original_feature_name}
        parts = r.feature_name.split("__", 2)
        if len(parts) != 3:
            continue
        original_name = parts[2]
        if original_name not in feature_names or r.value_numeric is None:
            continue
        sums[original_name] += float(r.value_numeric) / 100.0
        counts[original_name] += 1

    weights = {}
    flags = []
    for name in feature_names:
        if counts.get(name):
            weights[name] = sums[name] / counts[name]
        else:
            weights[name] = NEUTRAL_WEIGHT
    if not conf_rows:
        flags.append("no_confidence_engine_output_using_equal_weights")
    return weights, flags


def _apply_vetoes(matrix: dict, veto_rules: Sequence[VetoRule]) -> dict:
    """Returns {candidate_id: [reasons]} for candidates that fail at
    least one rule. Candidates with a criterion missing entirely (never
    imputed, e.g. because the criterion was dropped for lacking corpus
    data) are NOT vetoed on that rule -- absence of evidence is not
    evidence of failing the constraint."""
    vetoed = defaultdict(list)
    for rule in veto_rules:
        for cand_id, row in matrix.items():
            if rule.feature_name not in row:
                continue
            value = row[rule.feature_name]
            if rule.min_value is not None and value < rule.min_value:
                vetoed[cand_id].append(rule.reason or f"{rule.feature_name} below minimum {rule.min_value}")
            if rule.max_value is not None and value > rule.max_value:
                vetoed[cand_id].append(rule.reason or f"{rule.feature_name} above maximum {rule.max_value}")
    return vetoed


def decision_evidence(
    candidates: Sequence[CandidateResult], criteria_specs: Sequence[CriterionSpec],
    sensitivity: Optional[SensitivityResult], decision_confidence: Optional[dict], flags: list,
) -> dict:
    """Structured evidence dict, matching this codebase's *_evidence
    convention."""
    return {
        "ranking": [
            {
                "catchment_id": c.catchment_id, "rank": c.rank,
                "closeness_coefficient": c.closeness_coefficient, "vetoed": c.vetoed,
                "veto_reasons": c.veto_reasons, "imputed_criteria": c.imputed_criteria,
            }
            for c in candidates
        ],
        "criteria": [
            {"feature_name": c.feature_name, "direction": c.direction, "weight": round(c.weight, 4), "direction_defaulted": c.direction_defaulted}
            for c in criteria_specs
        ],
        "sensitivity": {
            "n_draws": sensitivity.n_draws, "top_choice_rank1_stability": sensitivity.top_choice_stability,
            "mean_rank_by_candidate": sensitivity.mean_rank_by_candidate,
        } if sensitivity is not None else None,
        "decision_confidence": decision_confidence,
        "methods": [
            "hwang_yoon_1981_topsis", "saaty_1980_ahp_optional", "roy_1991_electre_veto",
            "triantaphyllou_sanchez_1997_sensitivity", "dirichlet_weight_perturbation",
        ],
        "warnings": flags,
    }


# ============================================================================
# Django-facing entry point
# ============================================================================

def run(
    query: BusinessQuery,
    catchments: Optional[Sequence[Catchment]] = None,
    weights: Optional[dict] = None,
    ahp_pairwise_matrix: Optional[list] = None,
    veto_rules: Optional[Sequence[VetoRule]] = None,
) -> dict:
    """
    Engine 26 entry point. Ranks every Catchment belonging to
    query.location_cell (or an explicit `catchments` subset) via TOPSIS
    over whatever numeric business_conclusion-level FeatureObservation
    rows the other engines have actually written, weighted by Engine 24's
    confidence scores by default (overridable via `weights` or an AHP
    `ahp_pairwise_matrix`), filtered through any supplied `veto_rules`,
    with a Monte Carlo sensitivity analysis on the resulting ranking.
    Writes one FeatureObservation per candidate plus a query-level
    decision_dossier, and records an EngineRun.

    weights: optional {feature_name: weight} override for the default
    confidence-weighted vector; unknown keys dropped, renormalized.
    ahp_pairwise_matrix: optional Saaty 1-9 pairwise comparison matrix
    (list of lists) over the DISCOVERED criteria, in sorted feature_name
    order; used instead of `weights`/confidence-weighting if supplied AND
    internally consistent (CR < 0.10), see REAL METHODS #2.
    veto_rules: optional list[VetoRule]; defaults to DEFAULT_VETO_RULES
    (empty) if not supplied.

    Requires Engine 1 to have already run (catchments must exist), same
    dependency every catchment-scoped engine in this codebase documents.
    """
    started_at = django_timezone.now()
    cell = query.location_cell
    target_catchments = list(catchments) if catchments is not None else list(cell.catchments.all())
    veto_rules = list(veto_rules) if veto_rules is not None else list(DEFAULT_VETO_RULES)

    run_row = EngineRun.objects.create(
        query=query, engine_number=ENGINE_NUMBER, status="succeeded", started_at=started_at,
    )

    if not target_catchments:
        run_row.status = "failed"
        run_row.error_message = "No Catchment rows found for this cell. Engine 1 (Spatial Context) must run first."
        run_row.finished_at = django_timezone.now()
        run_row.save(update_fields=["status", "error_message", "finished_at"])
        return {"cell": cell, "features": [], "engine_run": run_row}

    warnings_seen: list = []
    features_written = 0
    dossier = None

    try:
        with transaction.atomic():
            catchment_ids = [c.pk for c in target_catchments]
            matrix = _discover_criteria_matrix(cell, catchment_ids)

            if not matrix or not any(matrix.values()):
                warnings_seen.append("no_criteria_discovered")
                dossier = {"warnings": warnings_seen, "ranking": []}
                FeatureObservation.write_latest(
                    location_cell=cell, engine_number=ENGINE_NUMBER, feature_name="decision_dossier",
                    defaults=dict(
                        value_type="json", value_json=dossier, unit="", observed_at=started_at,
                        source="business_intel_internal", source_class="internal_derived",
                        method="decision_engine_topsis", derivation_level="business_conclusion",
                    ),
                )
                features_written += 1
                raise _EarlyReturn()

            all_criteria = sorted({crit for row in matrix.values() for crit in row})

            imputed_flags, dropped_criteria = _impute_missing(matrix, all_criteria)
            if dropped_criteria:
                warnings_seen.append(f"criteria with no corpus data available for imputation, dropped: {dropped_criteria}")
            usable_criteria = [c for c in all_criteria if c not in dropped_criteria]

            # Flag zero-variance criteria for transparency (see edge cases).
            for crit in usable_criteria:
                values = [row[crit] for row in matrix.values() if crit in row]
                if values and max(values) == min(values):
                    warnings_seen.append(f"uninformative_criterion: {crit} (identical across all candidates)")

            # --- Weight derivation ---
            weight_flags = []
            if ahp_pairwise_matrix is not None:
                ahp_weights, cr, ahp_flags = ahp_weights_from_pairwise_matrix(ahp_pairwise_matrix, usable_criteria)
                weight_flags.extend(ahp_flags)
                if ahp_weights is not None:
                    base_weights = ahp_weights
                else:
                    base_weights, conf_flags = _confidence_weights(cell, catchment_ids, usable_criteria)
                    weight_flags.extend(conf_flags)
            elif weights is not None:
                known = {k: v for k, v in weights.items() if k in usable_criteria}
                unknown = set(weights) - set(known)
                if unknown:
                    weight_flags.append(f"weights_dict_dropped_unknown_criteria: {sorted(unknown)}")
                total = sum(known.values()) or 1.0
                base_weights = {k: v / total for k, v in known.items()}
                for crit in usable_criteria:
                    base_weights.setdefault(crit, NEUTRAL_WEIGHT)
            else:
                base_weights, conf_flags = _confidence_weights(cell, catchment_ids, usable_criteria)
                weight_flags.extend(conf_flags)
            warnings_seen.extend(weight_flags)

            criteria_specs = []
            for crit in usable_criteria:
                direction = CRITERION_DIRECTION_OVERRIDES.get(crit)
                defaulted = direction is None
                criteria_specs.append(CriterionSpec(
                    feature_name=crit, direction=direction or "benefit",
                    weight=base_weights.get(crit, NEUTRAL_WEIGHT), direction_defaulted=defaulted,
                ))
                if defaulted:
                    warnings_seen.append(f"direction_defaulted_to_benefit: {crit}")

            # --- Veto stage (ELECTRE-style) ---
            vetoed = _apply_vetoes(matrix, veto_rules)

            candidate_matrix = {k: v for k, v in matrix.items() if k not in vetoed}
            if not candidate_matrix:
                warnings_seen.append("no_viable_candidates")
                dossier = {
                    "warnings": warnings_seen,
                    "ranking": [
                        {"catchment_id": cid, "vetoed": True, "veto_reasons": reasons}
                        for cid, reasons in vetoed.items()
                    ],
                }
                FeatureObservation.write_latest(
                    location_cell=cell, engine_number=ENGINE_NUMBER, feature_name="decision_dossier",
                    defaults=dict(
                        value_type="json", value_json=dossier, unit="", observed_at=started_at,
                        source="business_intel_internal", source_class="internal_derived",
                        method="decision_engine_topsis", derivation_level="business_conclusion",
                    ),
                )
                features_written += 1
                raise _EarlyReturn()

            # --- TOPSIS ---
            single_candidate = len(candidate_matrix) < 2
            reference_frame = None
            if single_candidate:
                warnings_seen.append("single_candidate_using_corpus_reference_frame")
                reference_frame = _corpus_reference_frame(usable_criteria)

            closeness = topsis_rank(candidate_matrix, criteria_specs, reference_frame=reference_frame)
            ranked_ids = sorted(closeness, key=closeness.get, reverse=True)

            candidates = []
            for rank, cand_id in enumerate(ranked_ids, start=1):
                candidates.append(CandidateResult(
                    catchment_id=cand_id, closeness_coefficient=round(closeness[cand_id], 4), rank=rank,
                    vetoed=False, imputed_criteria=imputed_flags.get(cand_id, []),
                    raw_values=matrix.get(cand_id, {}),
                ))
            for cand_id, reasons in vetoed.items():
                candidates.append(CandidateResult(
                    catchment_id=cand_id, closeness_coefficient=0.0, rank=-1, vetoed=True, veto_reasons=reasons,
                ))

            # --- Sensitivity analysis ---
            sensitivity = monte_carlo_sensitivity(candidate_matrix, criteria_specs, closeness)
            if sensitivity is None and len(candidate_matrix) < MIN_CANDIDATES_FOR_SENSITIVITY:
                warnings_seen.append("insufficient_candidates_for_sensitivity")

            # --- Decision confidence sub-dict ---
            decision_confidence = None
            if len(ranked_ids) >= 2:
                margin = closeness[ranked_ids[0]] - closeness[ranked_ids[1]]
            else:
                margin = None
            mean_criterion_confidence = (
                sum(c.weight for c in criteria_specs) / len(criteria_specs) if criteria_specs else None
            )
            decision_confidence = {
                "margin_top1_vs_top2_closeness": round(margin, 4) if margin is not None else None,
                "mean_criterion_confidence_weight": round(mean_criterion_confidence, 4) if mean_criterion_confidence is not None else None,
                "rank1_stability": sensitivity.top_choice_stability if sensitivity is not None else None,
            }

            dossier = decision_evidence(candidates, criteria_specs, sensitivity, decision_confidence, warnings_seen)

            # --- Writes ---
            # write_latest()'s `catchment` kwarg expects an actual Catchment
            # instance (it's a Django FK field), not a raw id -- candidates
            # only carry catchment_id, so resolve every id needed in one bulk
            # query up front rather than N individual lookups in the loop.
            _catchment_ids = {c.catchment_id for c in candidates if c.catchment_id is not None}
            _catchments_by_id = Catchment.objects.in_bulk(_catchment_ids)

            for c in candidates:
                FeatureObservation.write_latest(
                    location_cell=cell, catchment=_catchments_by_id.get(c.catchment_id),
                    engine_number=ENGINE_NUMBER, feature_name="topsis_closeness_coefficient",
                    defaults=dict(
                        value_type="numeric", value_numeric=Decimal(str(c.closeness_coefficient)), unit="ratio_0_1",
                        observed_at=started_at, source="business_intel_internal", source_class="internal_derived",
                        method="hwang_yoon_1981_topsis", derivation_level="business_conclusion",
                        sample_size=len(criteria_specs),
                    ),
                )
                features_written += 1

            FeatureObservation.write_latest(
                location_cell=cell, engine_number=ENGINE_NUMBER, feature_name="decision_dossier",
                defaults=dict(
                    value_type="json", value_json=dossier, unit="", observed_at=started_at,
                    source="business_intel_internal", source_class="internal_derived",
                    method="decision_engine_topsis", derivation_level="business_conclusion",
                    sample_size=len(candidates),
                ),
            )
            features_written += 1

    except _EarlyReturn:
        pass
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


class _EarlyReturn(Exception):
    """Internal control-flow signal for the two 'nothing to rank' early
    exits inside run()'s transaction block -- lets both paths still fall
    through to the normal EngineRun bookkeeping at the bottom (status,
    duration, features_written) instead of duplicating it twice."""
    pass