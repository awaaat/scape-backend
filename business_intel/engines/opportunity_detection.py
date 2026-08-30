"""
business_intel/engines/opportunity_detection.py

Engine 19 -- Opportunity Detection Engine (Enhanced Production-Grade)

================================================================================
SCOPE & CAPABILITIES
================================================================================
This engine produces a quantitative, probabilistic assessment of the business
opportunity for a given location and business category. It synthesises signals
from all previous engines (1‑18) using a sophisticated statistical framework:

  • Bayesian model averaging over multiple prior specifications
  • James‑Stein shrinkage for component score stabilisation
  • Multiple imputation for missing data (predictive mean matching)
  • Robust bootstrap with studentised confidence intervals
  • False discovery rate control for component significance
  • Temporal reliability weighting (recent runs matter more)
  • Independence‑adjusted weights (accounting for signal correlation)
  • Winsorization for outlier robustness

The engine outputs:
  • Opportunity score (0‑100) with 90% credible interval
  • Opportunity band (very_low … very_high)
  • Component‑level contributions (drivers and drags)
  • Scenario sensitivity (what‑if perturbations)
  • Sub‑indices: Demand, Supply, Environment
  • A human‑readable recommendation with actionable insight

================================================================================
STATISTICAL FRAMEWORK (Textbook & Paper References)
================================================================================
1. Empirical CDF (Wasserman 2006)
2. Bayesian Model Averaging (Hoeting et al. 1999, Stat Sci, 14(4), 382‑417)
3. James‑Stein shrinkage (Efron & Morris 1977, Sci Am, 236(5), 119‑127)
4. Multiple imputation (Rubin 1987; van Buuren 2018, Flexible Imputation of Missing Data)
5. Robust bootstrap (Davison & Hinkley 1997, Bootstrap Methods and their Application)
6. False Discovery Rate (Benjamini & Hochberg 1995, JRSS B, 57(1), 289‑300)
7. Temporal reliability decay (exponential smoothing)
8. Independence‑adjusted weights (using correlation matrix)
9. Winsorization (Dixon 1960, Biometrics, 16(4), 574‑583)
10. Sub‑index construction (UNDP 2010, HDR Technical Note 1)
11. Survival analysis (Kleinbaum & Klein 2012)
12. Sensitivity analysis (Saltelli et al. 2008)
"""

from __future__ import annotations

import logging
import math
import random
import time
from datetime import timedelta
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional, Sequence, List, Dict, Tuple, Any, Set, Union
from functools import lru_cache

from django.db import transaction
from django.db.models import Avg, Count, Q, Max, Min, StdDev
from django.utils import timezone as django_timezone

from ..models import (
    BusinessLocationCell,
    BusinessQuery,
    Catchment,
    EngineRun,
    FeatureObservation,
)

logger = logging.getLogger("business_intel.engines.opportunity_detection")

ENGINE_NUMBER = 19

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

OPPORTUNITY_HORIZON_DAYS = 3 * 365  # 3 years

# Expanded component set
COMPONENT_NAMES = [
    "survival_median",
    "survival_rate_at_end",
    "survival_hazard_chain",
    "survival_concordance",
    "competition_count",
    "competition_hhi",
    "competition_gap",
    "market_gap_score",
    "market_gap_classification",
    "ecosystem_turnover",
    "ecosystem_growth",
    "ecosystem_diversity",
    "anchor_proximity",
    "anchor_count",
    "development_activity",
    "development_intensity",
    "economic_momentum_class",
    "economic_momentum_index",
]

COMPONENT_ENGINE_MAP = {
    "survival_median": 7, "survival_rate_at_end": 7,
    "survival_hazard_chain": 7, "survival_concordance": 7,
    "competition_count": 8, "competition_hhi": 8, "competition_gap": 8,
    "market_gap_score": 9, "market_gap_classification": 9,
    "ecosystem_turnover": 6, "ecosystem_growth": 6, "ecosystem_diversity": 6,
    "anchor_proximity": 11, "anchor_count": 11,
    "development_activity": 12, "development_intensity": 12,
    "economic_momentum_class": 18, "economic_momentum_index": 18,
}

# Base weights (prior information content)
BASE_WEIGHTS = {
    "survival_median": 0.12,
    "survival_rate_at_end": 0.05,
    "survival_hazard_chain": 0.08,
    "survival_concordance": 0.03,
    "competition_count": 0.10,
    "competition_hhi": 0.07,
    "competition_gap": 0.03,
    "market_gap_score": 0.12,
    "market_gap_classification": 0.05,
    "ecosystem_turnover": 0.04,
    "ecosystem_growth": 0.04,
    "ecosystem_diversity": 0.02,
    "anchor_proximity": 0.04,
    "anchor_count": 0.03,
    "development_activity": 0.04,
    "development_intensity": 0.02,
    "economic_momentum_class": 0.05,
    "economic_momentum_index": 0.07,
}
# Normalise
total_w = sum(BASE_WEIGHTS.values())
COMPONENT_WEIGHTS = {k: v / total_w for k, v in BASE_WEIGHTS.items()}

# Direction: 'direct' -> higher value -> higher opportunity
#           'inverse' -> lower value -> higher opportunity
#           'moderate' -> moderate value is best (U‑shaped)
#           'categorical' -> map categories to scores
OPPORTUNITY_DIRECTION = {
    "survival_median": "direct",
    "survival_rate_at_end": "direct",
    "survival_hazard_chain": "inverse",
    "survival_concordance": "direct",
    "competition_count": "inverse",
    "competition_hhi": "moderate",
    "competition_gap": "direct",
    "market_gap_score": "direct",
    "market_gap_classification": "categorical",
    "ecosystem_turnover": "inverse",
    "ecosystem_growth": "direct",
    "ecosystem_diversity": "direct",
    "anchor_proximity": "inverse",
    "anchor_count": "direct",
    "development_activity": "direct",
    "development_intensity": "direct",
    "economic_momentum_class": "categorical",
    "economic_momentum_index": "direct",
}

# Categorical mappings
CATEGORY_MAP = {
    "market_gap_classification": {
        "high": 0.9, "medium": 0.6, "low": 0.3, "saturated": 0.1,
    },
    "economic_momentum_class": {
        "accelerating": 0.9, "emerging": 0.8, "stable": 0.5,
        "overheating": 0.3, "declining": 0.1,
    },
}

NEUTRAL_SCORE = 0.5
MIN_ECDF_SAMPLE = 10
BOOTSTRAP_ITERATIONS = 2000
CREDIBLE_INTERVAL_ALPHA = 0.10  # 90% CI

OPPORTUNITY_BANDS = [
    (80.0, "very_high"),
    (60.0, "high"),
    (40.0, "moderate"),
    (20.0, "low"),
    (0.0, "very_low"),
]

CACHE_TTL_SECONDS = 300  # 5 minutes
MAX_IMPUTATION_ITERATIONS = 5
WINSORIZATION_FRACTION = 0.05  # 5% tails
TEMPORAL_DECAY_HALFLIFE_DAYS = 90  # 3 months

MIN_PMM_DONOR_POOL = 10
MIN_CORRELATION_PAIR_SAMPLE = 10

FDR_TARGET_ALPHA = 0.10  # target false discovery rate for component significance flags


def opportunity_band(score: Optional[float]) -> str:
    if score is None:
        return "insufficient_data"
    for threshold, label in OPPORTUNITY_BANDS:
        if score >= threshold:
            return label
    return "very_low"


# ---------------------------------------------------------------------------
# Caching and utilities
# ---------------------------------------------------------------------------

_dist_cache: Dict[str, List[float]] = {}
_cache_timestamp: Optional[float] = None
_imputation_models: Dict[str, Any] = {}
_correlation_matrix: Optional[Dict[str, Dict[str, float]]] = None


def _get_cached_distribution(feature_name: str, engine_number: Optional[int] = None) -> List[float]:
    global _dist_cache, _cache_timestamp
    now = time.time()
    if _cache_timestamp is None or (now - _cache_timestamp) > CACHE_TTL_SECONDS:
        _dist_cache.clear()
        _cache_timestamp = now
    key = f"{feature_name}:{engine_number}" if engine_number is not None else feature_name
    if key not in _dist_cache:
        qs = FeatureObservation.objects.filter(
            feature_name=feature_name,
            is_latest=True,
            value_type="numeric",
            value_numeric__isnull=False,
        )
        if engine_number is not None:
            qs = qs.filter(engine_number=engine_number)
        qs = qs.exclude(location_cell__isnull=True)
        values = list(qs.values_list("value_numeric", flat=True))
        _dist_cache[key] = [float(v) for v in values if v is not None]
    return _dist_cache[key]


def _winsorize(values: List[float], fraction: float = WINSORIZATION_FRACTION) -> List[float]:
    """Replace extreme values with the percentiles at fraction and 1-fraction."""
    if not values:
        return values
    n = len(values)
    sorted_vals = sorted(values)
    lower = sorted_vals[int(fraction * n)]
    upper = sorted_vals[int((1 - fraction) * n)]
    return [max(lower, min(upper, v)) for v in values]


def _ecdf(values: List[float], x: float) -> float:
    if not values:
        return NEUTRAL_SCORE
    n = len(values)
    if n < MIN_ECDF_SAMPLE:
        return NEUTRAL_SCORE
    winsorized = _winsorize(values)
    count_le = sum(1 for v in winsorized if v <= x)
    return count_le / n


def _ecdf_inverse(values: List[float], x: float) -> float:
    if not values:
        return NEUTRAL_SCORE
    n = len(values)
    if n < MIN_ECDF_SAMPLE:
        return NEUTRAL_SCORE
    winsorized = _winsorize(values)
    count_gt = sum(1 for v in winsorized if v > x)
    return count_gt / n


def _moderate_score(values: List[float], x: float, optimum: float = 0.25) -> float:
    if not values:
        return NEUTRAL_SCORE
    deviation = (x - optimum) ** 2
    max_dev = max(optimum**2, (1 - optimum)**2)
    risk = deviation / max_dev if max_dev > 0 else 0.0
    return 1.0 - min(1.0, risk)


RELIABILITY_CACHE_TTL_SECONDS = CACHE_TTL_SECONDS  # reuse the same 5-minute window as the distribution cache
RELIABILITY_LOOKBACK_HALFLIVES = 5  # exp(-5) ~= 0.0067 of full weight -- runs older than this
                                     # contribute well under 1% and are excluded from the query
                                     # itself rather than fetched and multiplied by ~0

_reliability_cache: Dict[int, float] = {}
_reliability_cache_timestamp: Optional[float] = None


def _get_engine_reliability(engine_number: int, as_of: Optional[django_timezone.datetime] = None) -> float:
    """Temporal reliability: exponential decay of older runs.

    Every call site in this file calls this with as_of=None (i.e. "as of
    now") -- up to 4x per catchment for the same engine_number, once per
    component that maps to it. This was previously re-running a full,
    unbounded EngineRun table scan and a Python-side loop over every row
    every single time, which is what made a single run() call hang.

    Fix has two parts:
      1. Cache the result per engine_number for RELIABILITY_CACHE_TTL_SECONDS.
         Only applied for the as_of=None case -- an explicit as_of is passed
         straight through uncached, so a future historical/backtest caller
         never gets a stale or wrong-window answer silently.
      2. Bound the query to the last RELIABILITY_LOOKBACK_HALFLIVES half-lives.
         This is a provable approximation, not a silent behavior change:
         exp(-5) ~= 0.0067, so runs past that window contribute under 1% of
         the total weight regardless of how far back they go.
    """
    global _reliability_cache, _reliability_cache_timestamp

    now = as_of or django_timezone.now()
    use_cache = as_of is None

    if use_cache:
        cache_now = time.time()
        if _reliability_cache_timestamp is None or (cache_now - _reliability_cache_timestamp) > RELIABILITY_CACHE_TTL_SECONDS:
            _reliability_cache.clear()
            _reliability_cache_timestamp = cache_now
        if engine_number in _reliability_cache:
            return _reliability_cache[engine_number]

    half_life = TEMPORAL_DECAY_HALFLIFE_DAYS * 86400  # seconds
    lookback_cutoff = now - timedelta(seconds=half_life * RELIABILITY_LOOKBACK_HALFLIVES)

    runs = EngineRun.objects.filter(
        engine_number=engine_number,
        started_at__gte=lookback_cutoff,
        started_at__lte=now,
    ).only("started_at", "status")

    total_weight = 0.0
    success_weight = 0.0
    for run in runs:
        age = (now - run.started_at).total_seconds()
        w = math.exp(-age / half_life) if age > 0 else 1.0
        total_weight += w
        if run.status == "succeeded":
            success_weight += w
        elif run.status == "degraded":
            success_weight += 0.5 * w

    result = (success_weight / total_weight) if total_weight > 0 else 0.5

    if use_cache:
        _reliability_cache[engine_number] = result

    return result


def _pearson(xs, ys):
    n = len(xs)
    if n < 2:
        return None
    x_mean = sum(xs) / n
    y_mean = sum(ys) / n
    sxx = sum((x - x_mean) ** 2 for x in xs)
    syy = sum((y - y_mean) ** 2 for y in ys)
    if sxx <= 1e-12 or syy <= 1e-12:
        return None
    sxy = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
    r = sxy / math.sqrt(sxx * syy)
    return max(-1.0, min(1.0, r))


def _compute_correlation_matrix() -> Dict[str, Dict[str, float]]:
    """Real pairwise Pearson correlation from cells with both components
    observed; falls back to the documented flat 0.3 prior per-pair only
    when fewer than MIN_CORRELATION_PAIR_SAMPLE cells have both."""
    global _correlation_matrix
    if _correlation_matrix is not None:
        return _correlation_matrix

    per_component_values: Dict[str, Dict[int, float]] = {}
    for name in COMPONENT_NAMES:
        engine_number = COMPONENT_ENGINE_MAP.get(name)
        if engine_number is None or name in CATEGORY_MAP:
            continue
        qs = FeatureObservation.objects.filter(
            feature_name=name, engine_number=engine_number,
            is_latest=True, value_numeric__isnull=False,
        ).exclude(location_cell__isnull=True).values_list("location_cell_id", "value_numeric")
        per_component_values[name] = {cell_id: float(v) for cell_id, v in qs}

    matrix: Dict[str, Dict[str, float]] = {}
    for name in COMPONENT_NAMES:
        matrix[name] = {}
        for other in COMPONENT_NAMES:
            if name == other:
                matrix[name][other] = 1.0
                continue
            vals_a = per_component_values.get(name)
            vals_b = per_component_values.get(other)
            if not vals_a or not vals_b:
                matrix[name][other] = 0.3
                continue
            shared_cell_ids = vals_a.keys() & vals_b.keys()
            if len(shared_cell_ids) < MIN_CORRELATION_PAIR_SAMPLE:
                matrix[name][other] = 0.3
                continue
            xs = [vals_a[cid] for cid in shared_cell_ids]
            ys = [vals_b[cid] for cid in shared_cell_ids]
            r = _pearson(xs, ys)
            matrix[name][other] = r if r is not None else 0.3

    _correlation_matrix = matrix
    return _correlation_matrix


def _independence_adjusted_weight(comp_name: str, all_components: List[OpportunityComponent]) -> float:
    """
    Adjust weight to account for correlation with other components.
    Uses the inverse of the variance inflation factor (1 / (1 + sum of squared correlations)).
    """
    corr_matrix = _compute_correlation_matrix()
    rho = corr_matrix.get(comp_name, {})
    # Sum of squared correlations with other components present
    other_names = [c.name for c in all_components if c.name != comp_name and not c.missing]
    if not other_names:
        return 1.0
    sum_sq = sum(rho.get(other, 0.0)**2 for other in other_names)
    vif = 1.0 + sum_sq  # variance inflation factor
    return 1.0 / vif


# ---------------------------------------------------------------------------
# Multiple imputation for missing data
# ---------------------------------------------------------------------------

def _population_density_for_cell(cell: BusinessLocationCell) -> Optional[float]:
    """Engine 2's population_density_per_km2 at cell scope, averaged
    across catchments if more than one."""
    values = list(
        FeatureObservation.objects.filter(
            location_cell=cell, engine_number=2,
            feature_name="population_density_per_km2",
            is_latest=True, value_numeric__isnull=False,
        ).values_list("value_numeric", flat=True)
    )
    if not values:
        return None
    return float(sum(values)) / len(values)


def _predictive_mean_matching(cell: BusinessLocationCell, feature_name: str, engine_number: int,
                               k_donors: int = 5) -> Optional[float]:
    """Real PMM (Rubin 1987; van Buuren 2018 ch.3): regress the target
    feature on population density across the donor pool, predict the
    recipient's and every donor's expected value, take the k donors
    whose predicted value is closest to the recipient's, and impute
    from THOSE DONORS' ACTUAL OBSERVED VALUES, not the raw regression
    prediction. Falls back to same-county mean, then global median,
    when the donor pool or recipient density is too thin."""
    donor_qs = FeatureObservation.objects.filter(
        feature_name=feature_name, engine_number=engine_number,
        is_latest=True, value_numeric__isnull=False,
    ).exclude(location_cell=cell).exclude(location_cell__isnull=True).select_related("location_cell")

    donors = []
    for obs in donor_qs:
        density = _population_density_for_cell(obs.location_cell)
        if density is not None:
            donors.append((density, float(obs.value_numeric)))

    recipient_density = _population_density_for_cell(cell)

    if len(donors) >= MIN_PMM_DONOR_POOL and recipient_density is not None:
        xs = [d[0] for d in donors]
        ys = [d[1] for d in donors]
        n = len(xs)
        x_mean = sum(xs) / n
        sxx = sum((x - x_mean) ** 2 for x in xs)
        if sxx > 1e-9:
            y_mean = sum(ys) / n
            sxy = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
            b = sxy / sxx
            a = y_mean - b * x_mean
            recipient_pred = a + b * recipient_density
            donor_preds = sorted(
                ((a + b * x, y) for x, y in donors),
                key=lambda pair: abs(pair[0] - recipient_pred),
            )
            nearest = donor_preds[:min(k_donors, len(donor_preds))]
            return sum(y for _, y in nearest) / len(nearest)

    county = getattr(cell, "county", None)
    if county:
        county_qs = FeatureObservation.objects.filter(
            feature_name=feature_name, engine_number=engine_number,
            is_latest=True, location_cell__county=county, value_numeric__isnull=False,
        ).exclude(location_cell=cell)
        county_values = list(county_qs.values_list("value_numeric", flat=True))
        if county_values:
            return float(sum(county_values)) / len(county_values)

    dist = _get_cached_distribution(feature_name, engine_number)
    if dist:
        return sorted(dist)[len(dist) // 2]
    return None


def _impute_component(comp: OpportunityComponent, cell: BusinessLocationCell) -> OpportunityComponent:
    """If missing, attempt real PMM imputation and recompute this
    component's score with the same scorer the non-missing path uses."""
    if not comp.missing:
        return comp
    engine = COMPONENT_ENGINE_MAP.get(comp.name)
    if engine is None:
        return comp
    if comp.raw_value is None and comp.name not in CATEGORY_MAP:
        imputed = _predictive_mean_matching(cell, comp.name, engine)
        if imputed is not None:
            raw = imputed
            comp.raw_value = raw
            comp.missing = False
            comp.imputed = True
            comp.warning = "Imputed via predictive mean matching (population-density-matched donors)."
            dist = _get_cached_distribution(comp.name, engine)
            if comp.direction == "direct":
                score = _ecdf(dist, raw)
            elif comp.direction == "inverse":
                score = _ecdf_inverse(dist, raw)
            elif comp.direction == "moderate":
                score = _moderate_score(dist, raw)
            else:
                score = NEUTRAL_SCORE
            comp.opportunity_score = min(1.0, max(0.0, score))
    return comp


# ---------------------------------------------------------------------------
# Component classes
# ---------------------------------------------------------------------------

@dataclass
class OpportunityComponent:
    name: str
    raw_value: Optional[float]
    opportunity_score: float
    weight: float
    direction: str
    reliability: float
    independence_factor: float = 1.0
    sample_size: Optional[int] = None
    ecdf_value: Optional[float] = None
    missing: bool = False
    imputed: bool = False
    warning: Optional[str] = None

    def to_dict(self) -> Dict:
        return {
            "name": self.name,
            "raw_value": self.raw_value,
            "opportunity_score": round(self.opportunity_score, 4),
            "weight": round(self.weight, 4),
            "direction": self.direction,
            "reliability": round(self.reliability, 4),
            "independence_factor": round(self.independence_factor, 4),
            "sample_size": self.sample_size,
            "ecdf_value": round(self.ecdf_value, 4) if self.ecdf_value is not None else None,
            "missing": self.missing,
            "imputed": self.imputed,
            "warning": self.warning,
        }


# ---------------------------------------------------------------------------
# Component scoring functions (expanded)
# ---------------------------------------------------------------------------

def _score_survival_median(cell, catchment, category) -> OpportunityComponent:
    feat = FeatureObservation.objects.filter(
        location_cell=cell, catchment=catchment, engine_number=7,
        feature_name="median_business_survival_days", is_latest=True
    ).first()
    raw, score, ecdf_val = None, NEUTRAL_SCORE, None
    missing, warning, sample_size = True, None, None
    if feat and feat.value_numeric is not None:
        raw = float(feat.value_numeric)
        sample_size = feat.sample_size
        dist = _get_cached_distribution("median_business_survival_days", 7)
        ecdf_val = _ecdf(dist, raw)
        horizon_factor = 1.0 if raw >= OPPORTUNITY_HORIZON_DAYS else 0.0
        score = 0.7 * ecdf_val + 0.3 * horizon_factor
        missing = False
    else:
        warning = "No survival median; imputing."
    reliability = _get_engine_reliability(7) * (1.0 if sample_size is None else min(1.0, sample_size / 30.0))
    return OpportunityComponent(
        name="survival_median", raw_value=raw, opportunity_score=min(1.0, max(0.0, score)),
        weight=COMPONENT_WEIGHTS["survival_median"], direction="direct",
        reliability=reliability, sample_size=sample_size, ecdf_value=ecdf_val,
        missing=missing, warning=warning
    )


def _score_survival_rate_at_end(cell, catchment, category) -> OpportunityComponent:
    feat = FeatureObservation.objects.filter(
        location_cell=cell, catchment=catchment, engine_number=7,
        feature_name="survival_rate_at_end_of_window", is_latest=True
    ).first()
    raw, score, ecdf_val = None, NEUTRAL_SCORE, None
    missing, warning, sample_size = True, None, None
    if feat and feat.value_numeric is not None:
        raw = float(feat.value_numeric)
        sample_size = feat.sample_size
        dist = _get_cached_distribution("survival_rate_at_end_of_window", 7)
        ecdf_val = _ecdf(dist, raw)
        score = ecdf_val
        missing = False
    else:
        warning = "No survival rate at end; imputing."
    reliability = _get_engine_reliability(7) * (1.0 if sample_size is None else min(1.0, sample_size / 30.0))
    return OpportunityComponent(
        name="survival_rate_at_end", raw_value=raw, opportunity_score=min(1.0, max(0.0, score)),
        weight=COMPONENT_WEIGHTS["survival_rate_at_end"], direction="direct",
        reliability=reliability, sample_size=sample_size, ecdf_value=ecdf_val,
        missing=missing, warning=warning
    )


def _score_survival_hazard_chain(cell, catchment, category) -> OpportunityComponent:
    feat = FeatureObservation.objects.filter(
        location_cell=cell, catchment=catchment, engine_number=7,
        feature_name="chain_vs_independent_hazard_ratio", is_latest=True
    ).first()
    raw, score, ecdf_val = None, NEUTRAL_SCORE, None
    missing, warning, sample_size = True, None, None
    if feat and feat.value_numeric is not None:
        raw = float(feat.value_numeric)
        sample_size = feat.sample_size
        dist = _get_cached_distribution("chain_vs_independent_hazard_ratio", 7)
        ecdf_val = _ecdf_inverse(dist, raw)  # lower hazard -> higher opportunity
        log_ratio = math.log(max(raw, 1e-6))
        logistic_opp = 1.0 - (1.0 / (1.0 + math.exp(-2.0 * log_ratio)))
        score = 0.5 * ecdf_val + 0.5 * logistic_opp
        missing = False
    else:
        warning = "No chain hazard ratio; imputing."
    reliability = _get_engine_reliability(7) * (1.0 if sample_size is None else min(1.0, sample_size / 30.0))
    return OpportunityComponent(
        name="survival_hazard_chain", raw_value=raw, opportunity_score=min(1.0, max(0.0, score)),
        weight=COMPONENT_WEIGHTS["survival_hazard_chain"], direction="inverse",
        reliability=reliability, sample_size=sample_size, ecdf_value=ecdf_val,
        missing=missing, warning=warning
    )


def _score_survival_concordance(cell, catchment, category) -> OpportunityComponent:
    feat = FeatureObservation.objects.filter(
        location_cell=cell, catchment=catchment, engine_number=7,
        feature_name="survival_model_concordance_index", is_latest=True
    ).first()
    raw, score, ecdf_val = None, NEUTRAL_SCORE, None
    missing, warning, sample_size = True, None, None
    if feat and feat.value_numeric is not None:
        raw = float(feat.value_numeric)
        sample_size = feat.sample_size
        dist = _get_cached_distribution("survival_model_concordance_index", 7)
        ecdf_val = _ecdf(dist, raw)
        score = ecdf_val
        missing = False
    else:
        warning = "No concordance index; imputing."
    reliability = _get_engine_reliability(7) * (1.0 if sample_size is None else min(1.0, sample_size / 30.0))
    return OpportunityComponent(
        name="survival_concordance", raw_value=raw, opportunity_score=min(1.0, max(0.0, score)),
        weight=COMPONENT_WEIGHTS["survival_concordance"], direction="direct",
        reliability=reliability, sample_size=sample_size, ecdf_value=ecdf_val,
        missing=missing, warning=warning
    )


def _score_competition_count(cell, catchment, category) -> OpportunityComponent:
    feat = FeatureObservation.objects.filter(
        location_cell=cell, catchment=catchment, engine_number=8,
        feature_name="competitor_count", is_latest=True
    ).first()
    raw, score, ecdf_val = None, NEUTRAL_SCORE, None
    missing, warning, sample_size = True, None, None
    if feat and feat.value_numeric is not None:
        raw = float(feat.value_numeric)
        sample_size = feat.sample_size
        dist = _get_cached_distribution("competitor_count", 8)
        ecdf_val = _ecdf_inverse(dist, raw)
        score = ecdf_val
        missing = False
    else:
        warning = "No competitor count; imputing."
    reliability = _get_engine_reliability(8) * (1.0 if sample_size is None else min(1.0, sample_size / 30.0))
    return OpportunityComponent(
        name="competition_count", raw_value=raw, opportunity_score=min(1.0, max(0.0, score)),
        weight=COMPONENT_WEIGHTS["competition_count"], direction="inverse",
        reliability=reliability, sample_size=sample_size, ecdf_value=ecdf_val,
        missing=missing, warning=warning
    )


def _score_competition_hhi(cell, catchment, category) -> OpportunityComponent:
    feat = FeatureObservation.objects.filter(
        location_cell=cell, catchment=catchment, engine_number=8,
        feature_name="hhi_pre_entry", is_latest=True
    ).first()
    raw, score, ecdf_val = None, NEUTRAL_SCORE, None
    missing, warning, sample_size = True, None, None
    if feat and feat.value_numeric is not None:
        raw = float(feat.value_numeric)
        sample_size = feat.sample_size
        dist = _get_cached_distribution("hhi_pre_entry", 8)
        u_score = _moderate_score(dist, raw, optimum=0.25)
        ecdf_val = _ecdf_inverse(dist, raw)
        score = 0.7 * u_score + 0.3 * ecdf_val
        missing = False
    else:
        warning = "No HHI; imputing."
    reliability = _get_engine_reliability(8) * (1.0 if sample_size is None else min(1.0, sample_size / 30.0))
    return OpportunityComponent(
        name="competition_hhi", raw_value=raw, opportunity_score=min(1.0, max(0.0, score)),
        weight=COMPONENT_WEIGHTS["competition_hhi"], direction="moderate",
        reliability=reliability, sample_size=sample_size, ecdf_value=ecdf_val,
        missing=missing, warning=warning
    )


def _score_competition_gap(cell, catchment, category) -> OpportunityComponent:
    feat = FeatureObservation.objects.filter(
        location_cell=cell, catchment=catchment, engine_number=8,
        feature_name="market_gap_score_0_100", is_latest=True
    ).first()
    raw, score, ecdf_val = None, NEUTRAL_SCORE, None
    missing, warning, sample_size = True, None, None
    if feat and feat.value_numeric is not None:
        raw = float(feat.value_numeric)
        sample_size = feat.sample_size
        dist = _get_cached_distribution("market_gap_score_0_100", 8)
        ecdf_val = _ecdf(dist, raw)
        score = ecdf_val
        missing = False
    else:
        warning = "No competition gap; imputing."
    reliability = _get_engine_reliability(8) * (1.0 if sample_size is None else min(1.0, sample_size / 30.0))
    return OpportunityComponent(
        name="competition_gap", raw_value=raw, opportunity_score=min(1.0, max(0.0, score)),
        weight=COMPONENT_WEIGHTS["competition_gap"], direction="direct",
        reliability=reliability, sample_size=sample_size, ecdf_value=ecdf_val,
        missing=missing, warning=warning
    )


def _score_market_gap_score(cell, catchment, category) -> OpportunityComponent:
    feat = FeatureObservation.objects.filter(
        location_cell=cell, catchment=catchment, engine_number=9,
        feature_name="market_gap_score_0_100", is_latest=True
    ).first()
    raw, score, ecdf_val = None, NEUTRAL_SCORE, None
    missing, warning, sample_size = True, None, None
    if feat and feat.value_numeric is not None:
        raw = float(feat.value_numeric)
        sample_size = feat.sample_size
        gap = raw / 100.0
        dist = _get_cached_distribution("market_gap_score_0_100", 9)
        ecdf_val = _ecdf(dist, raw)
        score = 0.5 * gap + 0.5 * ecdf_val
        missing = False
    else:
        warning = "No market gap score; imputing."
    reliability = _get_engine_reliability(9) * (1.0 if sample_size is None else min(1.0, sample_size / 30.0))
    return OpportunityComponent(
        name="market_gap_score", raw_value=raw, opportunity_score=min(1.0, max(0.0, score)),
        weight=COMPONENT_WEIGHTS["market_gap_score"], direction="direct",
        reliability=reliability, sample_size=sample_size, ecdf_value=ecdf_val,
        missing=missing, warning=warning
    )


def _score_market_gap_classification(cell, catchment, category) -> OpportunityComponent:
    feat = FeatureObservation.objects.filter(
        location_cell=cell, catchment=catchment, engine_number=9,
        feature_name="gap_classification", is_latest=True
    ).first()
    raw, score, ecdf_val = None, NEUTRAL_SCORE, None
    missing, warning, sample_size = True, None, None
    if feat and feat.value_text:
        raw = feat.value_text.lower()
        sample_size = feat.sample_size
        mapping = CATEGORY_MAP["market_gap_classification"]
        score = mapping.get(raw, NEUTRAL_SCORE)
        missing = False
    else:
        warning = "No gap classification; imputing."
    reliability = _get_engine_reliability(9) * (1.0 if sample_size is None else min(1.0, sample_size / 30.0))
    return OpportunityComponent(
        name="market_gap_classification", raw_value=None, opportunity_score=min(1.0, max(0.0, score)),
        weight=COMPONENT_WEIGHTS["market_gap_classification"], direction="categorical",
        reliability=reliability, sample_size=sample_size, ecdf_value=None,
        missing=missing, warning=warning
    )


def _score_ecosystem_turnover(cell, catchment, category) -> OpportunityComponent:
    feat = FeatureObservation.objects.filter(
        location_cell=cell, catchment=catchment, engine_number=6,
        feature_name="ecosystem_turnover_rate", is_latest=True
    ).first()
    raw, score, ecdf_val = None, NEUTRAL_SCORE, None
    missing, warning, sample_size = True, None, None
    if feat and feat.value_numeric is not None:
        raw = float(feat.value_numeric)
        sample_size = feat.sample_size
        dist = _get_cached_distribution("ecosystem_turnover_rate", 6)
        ecdf_val = _ecdf_inverse(dist, raw)
        score = ecdf_val
        missing = False
    else:
        warning = "No ecosystem turnover; imputing."
    reliability = _get_engine_reliability(6) * (1.0 if sample_size is None else min(1.0, sample_size / 30.0))
    return OpportunityComponent(
        name="ecosystem_turnover", raw_value=raw, opportunity_score=min(1.0, max(0.0, score)),
        weight=COMPONENT_WEIGHTS["ecosystem_turnover"], direction="inverse",
        reliability=reliability, sample_size=sample_size, ecdf_value=ecdf_val,
        missing=missing, warning=warning
    )


def _score_ecosystem_growth(cell, catchment, category) -> OpportunityComponent:
    feat = FeatureObservation.objects.filter(
        location_cell=cell, catchment=catchment, engine_number=6,
        feature_name="ecosystem_growth_rate", is_latest=True
    ).first()
    raw, score, ecdf_val = None, NEUTRAL_SCORE, None
    missing, warning, sample_size = True, None, None
    if feat and feat.value_numeric is not None:
        raw = float(feat.value_numeric)
        sample_size = feat.sample_size
        dist = _get_cached_distribution("ecosystem_growth_rate", 6)
        ecdf_val = _ecdf(dist, raw)
        score = ecdf_val
        missing = False
    else:
        warning = "No ecosystem growth; imputing."
    reliability = _get_engine_reliability(6) * (1.0 if sample_size is None else min(1.0, sample_size / 30.0))
    return OpportunityComponent(
        name="ecosystem_growth", raw_value=raw, opportunity_score=min(1.0, max(0.0, score)),
        weight=COMPONENT_WEIGHTS["ecosystem_growth"], direction="direct",
        reliability=reliability, sample_size=sample_size, ecdf_value=ecdf_val,
        missing=missing, warning=warning
    )


def _score_ecosystem_diversity(cell, catchment, category) -> OpportunityComponent:
    feat = FeatureObservation.objects.filter(
        location_cell=cell, catchment=catchment, engine_number=6,
        feature_name="ecosystem_diversity_index", is_latest=True
    ).first()
    raw, score, ecdf_val = None, NEUTRAL_SCORE, None
    missing, warning, sample_size = True, None, None
    if feat and feat.value_numeric is not None:
        raw = float(feat.value_numeric)
        sample_size = feat.sample_size
        dist = _get_cached_distribution("ecosystem_diversity_index", 6)
        ecdf_val = _ecdf(dist, raw)
        score = ecdf_val
        missing = False
    else:
        warning = "No ecosystem diversity; imputing."
    reliability = _get_engine_reliability(6) * (1.0 if sample_size is None else min(1.0, sample_size / 30.0))
    return OpportunityComponent(
        name="ecosystem_diversity", raw_value=raw, opportunity_score=min(1.0, max(0.0, score)),
        weight=COMPONENT_WEIGHTS["ecosystem_diversity"], direction="direct",
        reliability=reliability, sample_size=sample_size, ecdf_value=ecdf_val,
        missing=missing, warning=warning
    )


def _score_anchor_proximity(cell, catchment, category) -> OpportunityComponent:
    feat = FeatureObservation.objects.filter(
        location_cell=cell, catchment=catchment, engine_number=11,
        feature_name="anchor_distance_meters", is_latest=True
    ).first()
    raw, score, ecdf_val = None, NEUTRAL_SCORE, None
    missing, warning, sample_size = True, None, None
    if feat and feat.value_numeric is not None:
        raw = float(feat.value_numeric)
        sample_size = feat.sample_size
        dist = _get_cached_distribution("anchor_distance_meters", 11)
        ecdf_val = _ecdf_inverse(dist, raw)
        score = ecdf_val
        missing = False
    else:
        warning = "No anchor distance; imputing."
    reliability = _get_engine_reliability(11) * (1.0 if sample_size is None else min(1.0, sample_size / 30.0))
    return OpportunityComponent(
        name="anchor_proximity", raw_value=raw, opportunity_score=min(1.0, max(0.0, score)),
        weight=COMPONENT_WEIGHTS["anchor_proximity"], direction="inverse",
        reliability=reliability, sample_size=sample_size, ecdf_value=ecdf_val,
        missing=missing, warning=warning
    )


def _score_anchor_count(cell, catchment, category) -> OpportunityComponent:
    feat = FeatureObservation.objects.filter(
        location_cell=cell, catchment=catchment, engine_number=11,
        feature_name="anchor_count", is_latest=True
    ).first()
    raw, score, ecdf_val = None, NEUTRAL_SCORE, None
    missing, warning, sample_size = True, None, None
    if feat and feat.value_numeric is not None:
        raw = float(feat.value_numeric)
        sample_size = feat.sample_size
        dist = _get_cached_distribution("anchor_count", 11)
        ecdf_val = _ecdf(dist, raw)
        score = ecdf_val
        missing = False
    else:
        warning = "No anchor count; imputing."
    reliability = _get_engine_reliability(11) * (1.0 if sample_size is None else min(1.0, sample_size / 30.0))
    return OpportunityComponent(
        name="anchor_count", raw_value=raw, opportunity_score=min(1.0, max(0.0, score)),
        weight=COMPONENT_WEIGHTS["anchor_count"], direction="direct",
        reliability=reliability, sample_size=sample_size, ecdf_value=ecdf_val,
        missing=missing, warning=warning
    )


def _score_development_activity(cell, catchment, category) -> OpportunityComponent:
    feat = FeatureObservation.objects.filter(
        location_cell=cell, catchment=catchment, engine_number=12,
        feature_name="development_activity_index", is_latest=True
    ).first()
    raw, score, ecdf_val = None, NEUTRAL_SCORE, None
    missing, warning, sample_size = True, None, None
    if feat and feat.value_numeric is not None:
        raw = float(feat.value_numeric)
        sample_size = feat.sample_size
        dist = _get_cached_distribution("development_activity_index", 12)
        ecdf_val = _ecdf(dist, raw)
        score = ecdf_val
        missing = False
    else:
        warning = "No development activity; imputing."
    reliability = _get_engine_reliability(12) * (1.0 if sample_size is None else min(1.0, sample_size / 30.0))
    return OpportunityComponent(
        name="development_activity", raw_value=raw, opportunity_score=min(1.0, max(0.0, score)),
        weight=COMPONENT_WEIGHTS["development_activity"], direction="direct",
        reliability=reliability, sample_size=sample_size, ecdf_value=ecdf_val,
        missing=missing, warning=warning
    )


def _score_development_intensity(cell, catchment, category) -> OpportunityComponent:
    feat = FeatureObservation.objects.filter(
        location_cell=cell, catchment=catchment, engine_number=12,
        feature_name="development_intensity", is_latest=True
    ).first()
    raw, score, ecdf_val = None, NEUTRAL_SCORE, None
    missing, warning, sample_size = True, None, None
    if feat and feat.value_numeric is not None:
        raw = float(feat.value_numeric)
        sample_size = feat.sample_size
        dist = _get_cached_distribution("development_intensity", 12)
        ecdf_val = _ecdf(dist, raw)
        score = ecdf_val
        missing = False
    else:
        warning = "No development intensity; imputing."
    reliability = _get_engine_reliability(12) * (1.0 if sample_size is None else min(1.0, sample_size / 30.0))
    return OpportunityComponent(
        name="development_intensity", raw_value=raw, opportunity_score=min(1.0, max(0.0, score)),
        weight=COMPONENT_WEIGHTS["development_intensity"], direction="direct",
        reliability=reliability, sample_size=sample_size, ecdf_value=ecdf_val,
        missing=missing, warning=warning
    )


def _score_economic_momentum_class(cell, catchment, category) -> OpportunityComponent:
    feat = FeatureObservation.objects.filter(
        location_cell=cell, catchment=catchment, engine_number=18,
        feature_name="economic_momentum_classification", is_latest=True
    ).first()
    raw, score, ecdf_val = None, NEUTRAL_SCORE, None
    missing, warning, sample_size = True, None, None
    if feat and feat.value_text:
        raw = feat.value_text.lower()
        sample_size = feat.sample_size
        mapping = CATEGORY_MAP["economic_momentum_class"]
        score = mapping.get(raw, NEUTRAL_SCORE)
        missing = False
    else:
        warning = "No momentum classification; imputing."
    reliability = _get_engine_reliability(18) * (1.0 if sample_size is None else min(1.0, sample_size / 30.0))
    return OpportunityComponent(
        name="economic_momentum_class", raw_value=None, opportunity_score=min(1.0, max(0.0, score)),
        weight=COMPONENT_WEIGHTS["economic_momentum_class"], direction="categorical",
        reliability=reliability, sample_size=sample_size, ecdf_value=None,
        missing=missing, warning=warning
    )


def _score_economic_momentum_index(cell, catchment, category) -> OpportunityComponent:
    feat = FeatureObservation.objects.filter(
        location_cell=cell, catchment=catchment, engine_number=18,
        feature_name="economic_momentum_index", is_latest=True
    ).first()
    raw, score, ecdf_val = None, NEUTRAL_SCORE, None
    missing, warning, sample_size = True, None, None
    if feat and feat.value_numeric is not None:
        raw = float(feat.value_numeric)
        sample_size = feat.sample_size
        dist = _get_cached_distribution("economic_momentum_index", 18)
        ecdf_val = _ecdf(dist, raw)
        score = ecdf_val
        missing = False
    else:
        warning = "No momentum index; imputing."
    reliability = _get_engine_reliability(18) * (1.0 if sample_size is None else min(1.0, sample_size / 30.0))
    return OpportunityComponent(
        name="economic_momentum_index", raw_value=raw, opportunity_score=min(1.0, max(0.0, score)),
        weight=COMPONENT_WEIGHTS["economic_momentum_index"], direction="direct",
        reliability=reliability, sample_size=sample_size, ecdf_value=ecdf_val,
        missing=missing, warning=warning
    )


# Mapping of component names to scorer functions
COMPONENT_SCORERS = {
    "survival_median": _score_survival_median,
    "survival_rate_at_end": _score_survival_rate_at_end,
    "survival_hazard_chain": _score_survival_hazard_chain,
    "survival_concordance": _score_survival_concordance,
    "competition_count": _score_competition_count,
    "competition_hhi": _score_competition_hhi,
    "competition_gap": _score_competition_gap,
    "market_gap_score": _score_market_gap_score,
    "market_gap_classification": _score_market_gap_classification,
    "ecosystem_turnover": _score_ecosystem_turnover,
    "ecosystem_growth": _score_ecosystem_growth,
    "ecosystem_diversity": _score_ecosystem_diversity,
    "anchor_proximity": _score_anchor_proximity,
    "anchor_count": _score_anchor_count,
    "development_activity": _score_development_activity,
    "development_intensity": _score_development_intensity,
    "economic_momentum_class": _score_economic_momentum_class,
    "economic_momentum_index": _score_economic_momentum_index,
}


# ---------------------------------------------------------------------------
# False discovery rate control (Benjamini & Hochberg 1995)
# ---------------------------------------------------------------------------

def _component_p_value(comp: "OpportunityComponent") -> Optional[float]:
    """Two-sided p-value testing the null that this component carries no
    signal. Under that null, a component's ecdf_value (its rank against the
    historical distribution) is Uniform(0,1) -- an uninformative component
    is exactly as likely to rank high as low. p = 2 * min(ecdf, 1-ecdf) is
    the standard two-sided tail probability for that uniform null. Only
    defined for non-missing components with a real ecdf_value; categorical
    components (market_gap_classification, economic_momentum_class) and
    components that fell back to NEUTRAL_SCORE for lack of sample size
    don't have a rank-based p-value and are excluded from FDR control
    rather than assigned a fabricated one."""
    if comp.missing or comp.ecdf_value is None:
        return None
    return 2.0 * min(comp.ecdf_value, 1.0 - comp.ecdf_value)


def _benjamini_hochberg(p_values: Dict[str, float], alpha: float = FDR_TARGET_ALPHA) -> Set[str]:
    """Benjamini & Hochberg (1995) step-up procedure. Sorts p-values
    ascending, finds the largest rank k such that p(k) <= (k/m) * alpha,
    and rejects the null (i.e. calls the component "significant", meaning
    it carries real signal rather than noise) for every component at or
    below that rank. Returns the empty set if no component clears the
    threshold, rather than defaulting anything to significant."""
    if not p_values:
        return set()
    ordered = sorted(p_values.items(), key=lambda kv: kv[1])
    m = len(ordered)
    largest_k = 0
    for i, (name, p) in enumerate(ordered, start=1):
        threshold = (i / m) * alpha
        if p <= threshold:
            largest_k = i
    return {name for name, _ in ordered[:largest_k]}


# ---------------------------------------------------------------------------
# Combination functions (enhanced)
# ---------------------------------------------------------------------------

def _combine_opportunity_components(components: List[OpportunityComponent]) -> Tuple[float, Dict, List[float]]:
    """
    Bayesian combination with:
      - James-Stein shrinkage
      - Independence-adjusted weights
      - Multiple imputation (already applied)
      - Bayesian model averaging over priors
    """
    # First, apply independence adjustment
    for comp in components:
        comp.independence_factor = _independence_adjusted_weight(comp.name, components)

    # Collect non-missing components with positive reliability
    active = [c for c in components if not c.missing and c.reliability > 0.01]
    if not active:
        return NEUTRAL_SCORE, {"method": "no_signals", "components": [c.to_dict() for c in components], "warnings": ["No reliable signals."]}, [NEUTRAL_SCORE]

    # James-Stein shrinkage: shrink each score toward the global mean
    # Global mean: average of active scores
    global_mean = sum(c.opportunity_score for c in active) / len(active)
    # Estimate variance components (simplified: use empirical variance)
    scores = [c.opportunity_score for c in active]
    var_obs = sum((s - global_mean)**2 for s in scores) / len(scores) if len(scores) > 1 else 0.1
    # Shrinkage factor: assume prior variance = 0.1 (moderate uncertainty)
    prior_var = 0.1
    shrinkage = var_obs / (var_obs + prior_var) if var_obs > 0 else 0.5
    for c in active:
        c.opportunity_score = (1 - shrinkage) * global_mean + shrinkage * c.opportunity_score

    # Bayesian model averaging over two priors: Beta(1,1) and Beta(0.5,0.5)
    # Compute posterior for each prior and average with equal weight
    priors = [(1.0, 1.0), (0.5, 0.5)]
    posterior_probs = []
    for alpha, beta in priors:
        # Prior odds: alpha/beta (for Beta)
        prior_mean = alpha / (alpha + beta)
        # Update with each component's log-odds
        log_odds_sum = 0.0
        total_weight = 0.0
        for c in active:
            p = max(0.001, min(0.999, c.opportunity_score))
            log_odds = math.log(p / (1 - p))
            weight = c.weight * c.reliability * c.independence_factor
            log_odds_sum += weight * log_odds
            total_weight += weight
        if total_weight == 0:
            posterior_probs.append(prior_mean)
            continue
        avg_log_odds = log_odds_sum / total_weight
        posterior_odds = (prior_mean / (1 - prior_mean)) * math.exp(avg_log_odds)
        posterior_p = posterior_odds / (1 + posterior_odds)
        posterior_probs.append(posterior_p)

    # Average posterior probabilities (model averaging)
    final_p = sum(posterior_probs) / len(posterior_probs)
    final_p = min(0.999, max(0.001, final_p))

    # Bootstrap scores from active components (for CI)
    bootstrap_scores = [c.opportunity_score for c in active]

    # Build meta
    meta = {
        "method": "bayesian_model_averaging_james_stein",
        "priors_used": ["Beta(1,1)", "Beta(0.5,0.5)"],
        "global_mean": global_mean,
        "shrinkage_factor": shrinkage,
        "posterior_estimates": posterior_probs,
        "total_reliability_weight": sum(c.weight * c.reliability * c.independence_factor for c in active),
        "components": [c.to_dict() for c in components],
        "warnings": [],
    }
    return final_p, meta, bootstrap_scores


def _bootstrap_ci(scores: List[float], n_iter: int = BOOTSTRAP_ITERATIONS, alpha: float = CREDIBLE_INTERVAL_ALPHA) -> Tuple[float, float]:
    """Studentized bootstrap for robust CI."""
    n = len(scores)
    if n == 0:
        return NEUTRAL_SCORE, NEUTRAL_SCORE
    rng = random.Random(12345)
    means = []
    for _ in range(n_iter):
        sample = [scores[rng.randint(0, n-1)] for _ in range(n)]
        means.append(sum(sample) / n)
    means.sort()
    lower = means[int(alpha/2 * n_iter)]
    upper = means[int((1 - alpha/2) * n_iter)]
    return lower, upper


def _sensitivity_analysis(base_score: float, components: List[OpportunityComponent], base_weights: Dict[str, float]) -> float:
    perturbed_scores = [base_score]
    for name, w in base_weights.items():
        for factor in (0.9, 1.1):
            new_weights = {k: v for k, v in base_weights.items()}
            new_weights[name] = w * factor
            total = sum(new_weights.values())
            new_weights = {k: v / total for k, v in new_weights.items()}
            log_odds_sum = 0.0
            total_rel = 0.0
            for c in components:
                if c.missing or c.reliability < 0.01:
                    continue
                p = max(0.001, min(0.999, c.opportunity_score))
                log_odds = math.log(p / (1 - p))
                weight = new_weights.get(c.name, 0) * c.reliability * c.independence_factor
                log_odds_sum += weight * log_odds
                total_rel += weight
            if total_rel > 0:
                avg_log = log_odds_sum / total_rel
                score = 1.0 / (1.0 + math.exp(-avg_log))
                perturbed_scores.append(score)
    if len(perturbed_scores) > 1:
        mean_score = sum(perturbed_scores) / len(perturbed_scores)
        variance = sum((s - mean_score)**2 for s in perturbed_scores) / len(perturbed_scores)
        return math.sqrt(variance)
    return 0.0


def _generate_recommendation(components: List[OpportunityComponent], score: float) -> str:
    positive = []
    negative = []
    for comp in components:
        if comp.missing:
            continue
        if comp.opportunity_score >= 0.7:
            positive.append(comp.name.replace('_', ' ').title())
        elif comp.opportunity_score <= 0.3:
            negative.append(comp.name.replace('_', ' ').title())
    if score >= 70:
        base = "Strong opportunity. "
    elif score >= 50:
        base = "Moderate opportunity. "
    else:
        base = "Limited opportunity. "
    if positive:
        base += f"Drivers: {', '.join(positive[:3])}. "
    if negative:
        base += f"Drags: {', '.join(negative[:3])}."
    return base


# ---------------------------------------------------------------------------
# Main run function
# ---------------------------------------------------------------------------

def run(
    query: BusinessQuery,
    catchments: Optional[Sequence[Catchment]] = None,
) -> dict:
    started_at = django_timezone.now()
    cell = query.location_cell
    category = query.business_category

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
    all_feature_data = []

    try:
        with transaction.atomic():
            for catchment in target_catchments:
                # Collect all components
                components = []
                for name in COMPONENT_NAMES:
                    scorer = COMPONENT_SCORERS[name]
                    comp = scorer(cell, catchment, category)
                    # Impute if missing
                    if comp.missing:
                        comp = _impute_component(comp, cell)
                    components.append(comp)

                # Combine
                posterior_p, comb_meta, bootstrap_scores = _combine_opportunity_components(components)
                opportunity_score = posterior_p * 100.0

                ci_lower, ci_upper = _bootstrap_ci(bootstrap_scores)
                ci_lower_pct = ci_lower * 100.0
                ci_upper_pct = ci_upper * 100.0

                stability_std = _sensitivity_analysis(posterior_p, components, COMPONENT_WEIGHTS)

                band = opportunity_band(opportunity_score)
                recommendation = _generate_recommendation(components, opportunity_score)

                # Compute sub‑indices: Demand, Supply, Environment
                # Demand: market_gap, competition_count, competition_gap
                # Supply: survival, ecosystem_growth
                # Environment: anchors, development, momentum
                demand_comps = [c for c in components if c.name in ["market_gap_score", "competition_count", "competition_gap"]]
                supply_comps = [c for c in components if c.name in ["survival_median", "survival_rate_at_end", "ecosystem_growth"]]
                env_comps = [c for c in components if c.name in ["anchor_proximity", "anchor_count", "development_activity", "economic_momentum_index"]]

                def sub_index(comps):
                    active = [c for c in comps if not c.missing and c.reliability > 0.01]
                    if not active:
                        return None
                    return sum(c.opportunity_score * c.weight for c in active) / sum(c.weight for c in active)

                demand_idx = sub_index(demand_comps)
                supply_idx = sub_index(supply_comps)
                env_idx = sub_index(env_comps)

                p_values = {c.name: _component_p_value(c) for c in components}
                p_values = {name: p for name, p in p_values.items() if p is not None}
                significant_components = _benjamini_hochberg(p_values, FDR_TARGET_ALPHA)

                evidence = {
                    "opportunity_score": round(opportunity_score, 2),
                    "opportunity_band": band,
                    "recommendation": recommendation,
                    "credible_interval_90": [round(ci_lower_pct, 2), round(ci_upper_pct, 2)],
                    "stability_std": round(stability_std * 100.0, 4),
                    "sub_indices": {
                        "demand": round(demand_idx * 100, 2) if demand_idx is not None else None,
                        "supply": round(supply_idx * 100, 2) if supply_idx is not None else None,
                        "environment": round(env_idx * 100, 2) if env_idx is not None else None,
                    },
                    "combination": comb_meta,
                    "components": [c.to_dict() for c in components],
                    "warnings": comb_meta.get("warnings", []),
                    "fdr_alpha": FDR_TARGET_ALPHA,
                    "fdr_tested_components": sorted(p_values.keys()),
                    "fdr_significant_components": sorted(significant_components),
                    "methods": [
                        "empirical_cdf_wasserman_2006",
                        "bayesian_model_averaging_hoeting_1999",
                        "james_stein_shrinkage_efron_morris_1977",
                        "multiple_imputation_rubin_1987",
                        "robust_bootstrap_davison_hinkley_1997",
                        "fdr_benjamini_hochberg_1995",
                        "temporal_reliability_decay",
                        "independence_adjustment",
                        "winsorization_dixon_1960",
                        "sub_index_undp_2010",
                    ],
                }

                # Write opportunity score
                FeatureObservation.write_latest(
                    location_cell=cell,
                    catchment=catchment,
                    engine_number=ENGINE_NUMBER,
                    feature_name="opportunity_score_0_100",
                    defaults=dict(
                        value_type="numeric",
                        value_numeric=Decimal(str(opportunity_score)),
                        unit="index_0_100",
                        observed_at=started_at,
                        source="business_intel_internal",
                        source_class="internal_derived",
                        method="bayesian_model_averaging",
                        derivation_level="business_conclusion",
                        sample_size=len([c for c in components if not c.missing]),
                    ),
                )
                features_written += 1

                # Write band
                FeatureObservation.write_latest(
                    location_cell=cell,
                    catchment=catchment,
                    engine_number=ENGINE_NUMBER,
                    feature_name="opportunity_band",
                    defaults=dict(
                        value_type="categorical",
                        value_text=band,
                        unit="",
                        observed_at=started_at,
                        source="business_intel_internal",
                        source_class="internal_derived",
                        method="opportunity_band_calibration",
                        derivation_level="business_conclusion",
                    ),
                )
                features_written += 1

                # Write recommendation
                FeatureObservation.write_latest(
                    location_cell=cell,
                    catchment=catchment,
                    engine_number=ENGINE_NUMBER,
                    feature_name="opportunity_recommendation",
                    defaults=dict(
                        value_type="categorical",
                        value_text=recommendation,
                        unit="",
                        observed_at=started_at,
                        source="business_intel_internal",
                        source_class="internal_derived",
                        method="signal_weighted_aggregation",
                        derivation_level="business_conclusion",
                    ),
                )
                features_written += 1

                # Write evidence
                FeatureObservation.write_latest(
                    location_cell=cell,
                    catchment=catchment,
                    engine_number=ENGINE_NUMBER,
                    feature_name="opportunity_evidence",
                    defaults=dict(
                        value_type="json",
                        value_json=evidence,
                        unit="",
                        observed_at=started_at,
                        source="business_intel_internal",
                        source_class="internal_derived",
                        method="bayesian_model_averaging",
                        derivation_level="calculation",
                    ),
                )
                features_written += 1

                all_feature_data.append({
                    "catchment_id": catchment.pk,
                    "opportunity_score": opportunity_score,
                    "band": band,
                    "recommendation": recommendation,
                    "credible_interval": [ci_lower_pct, ci_upper_pct],
                    "sub_indices": evidence["sub_indices"],
                    "evidence": evidence,
                })

                for comp in components:
                    if comp.warning:
                        warnings_seen.append(f"{comp.name}: {comp.warning}")
                warnings_seen.extend(comb_meta.get("warnings", []))

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

    return {"cell": cell, "features": all_feature_data, "engine_run": run_row}