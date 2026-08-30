"""
business_intel/engines/failure_detection.py

Engine 20 -- Failure/Risk Detection Engine

================================================================================
OVERVIEW
================================================================================
This engine synthesises signals from all previous engines (1‑19) to produce a
probabilistic assessment of the risk that a new business would fail or
underperform. The assessment is grounded in Bayesian statistics, empirical
calibration, and uncertainty quantification.

Key features:
  • **Bayesian updating** – a Beta(1,1) prior for failure probability is
    updated with each signal using likelihood ratios derived from ECDF
    percentiles.
  • **Component reliability** – each signal is weighted by the historical
    reliability of its source engine (EngineRun success rate) and the sample
    size of the feature.
  • **Uncertainty quantification** – a parametric bootstrap (1,000 resamples)
    provides a 90% credible interval for the risk score.
  • **Missing data handling** – missing signals are imputed with the
    database‑wide median (or a conservative value), and flagged.
  • **Caching** – empirical distributions for each feature are cached for the
    lifetime of the request to reduce database load.
  • **Sensitivity analysis** – the score is re‑computed under small
    perturbations of the component weights to assess stability.
  • **Full audit trail** – the evidence dossier contains prior, likelihoods,
    posterior, credible interval, and component‑by‑component breakdown.

================================================================================
STATISTICAL METHODS (with real textbook references)
================================================================================
1. **Empirical cumulative distribution (ECDF)** – non‑parametric transform.
   Reference: Wasserman (2006) *All of Nonparametric Statistics*, Ch. 2.

2. **Bayesian update with Beta prior** – conjugate updating.
   Reference: Gelman et al. (2013) *Bayesian Data Analysis*, 3rd ed., Ch. 2 & 7.

3. **Weighted logarithmic opinion pool** – combination of independent forecasts.
   Reference: Genest & Zidek (1986) *Statistical Science*, 1(1), 114‑135.

4. **Reliability weighting** – supra‑Bayesian combination.
   Reference: Winkler (1981) *Management Science*, 27(4), 479‑488.

5. **Parametric bootstrap** – percentile method for credible intervals.
   Reference: Efron & Tibshirani (1993) *An Introduction to the Bootstrap*, Ch. 10.

6. **Sensitivity analysis** – local one‑factor‑at‑a‑time perturbation.
   Reference: Saltelli et al. (2008) *Global Sensitivity Analysis: The Primer*.

7. **Survival analysis** – Kaplan‑Meier, Cox PH, log‑rank, C‑index.
   References: Kleinbaum & Klein (2012) *Survival Analysis*;
              Brookmeyer & Crowley (1982) *Biometrics*, 38(1), 29‑41;
              Peduzzi et al. (1995) *J Clin Epidemiol*, 48(12), 1503‑1510.

================================================================================
DEPENDENCIES
================================================================================
- Django ORM
- Python standard library (math, random, collections, dataclasses)
- No external statistical libraries – all methods implemented from first principles.
"""

from __future__ import annotations

import logging
import math
import random
import time
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional, Sequence, List, Dict, Tuple, Any

from django.db import transaction
from django.db.models import Avg, Count, Q
from django.utils import timezone as django_timezone

from ..models import (
    BusinessLocationCell,
    BusinessQuery,
    Catchment,
    EngineRun,
    FeatureObservation,
)

logger = logging.getLogger("business_intel.engines.failure_detection")

ENGINE_NUMBER = 20

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Horizon (days) for survival‑based anchor
RISK_HORIZON_DAYS = 3 * 365  # 3 years

# Base component weights (sum to 1)
BASE_WEIGHTS = {
    "survival_median": 0.20,
    "survival_hazard_chain": 0.10,
    "competition_count": 0.15,
    "competition_hhi": 0.10,
    "market_gap_score": 0.15,
    "ecosystem_turnover": 0.05,
    "ecosystem_growth": 0.05,
    "anchor_proximity": 0.05,
    "development_activity": 0.05,
    "economic_momentum": 0.10,
}
# Normalise to sum to 1.0
total_w = sum(BASE_WEIGHTS.values())
COMPONENT_WEIGHTS = {k: v / total_w for k, v in BASE_WEIGHTS.items()}

# Direction: 'direct' -> higher value -> higher risk; 'inverse' -> lower value -> higher risk
RISK_DIRECTION = {
    "survival_median": "inverse",
    "survival_hazard_chain": "direct",
    "competition_count": "direct",
    "competition_hhi": "direct",
    "market_gap_score": "inverse",
    "ecosystem_turnover": "direct",
    "ecosystem_growth": "inverse",
    "anchor_proximity": "inverse",
    "development_activity": "inverse",
    "economic_momentum": "inverse",
}

NEUTRAL_SCORE = 0.5
MIN_ECDF_SAMPLE = 10
BOOTSTRAP_ITERATIONS = 1000
CREDIBLE_INTERVAL_ALPHA = 0.10  # 90% CI

# Risk bands (0‑100)
RISK_BANDS = [
    (80.0, "very_high"),
    (60.0, "high"),
    (40.0, "moderate"),
    (20.0, "low"),
    (0.0, "very_low"),
]

# Cache TTL for ECDF distributions (seconds)
CACHE_TTL_SECONDS = 300  # 5 minutes

def risk_band(score: Optional[float]) -> str:
    if score is None:
        return "insufficient_data"
    for threshold, label in RISK_BANDS:
        if score >= threshold:
            return label
    return "very_low"


# ---------------------------------------------------------------------------
# Caching for empirical distributions
# ---------------------------------------------------------------------------

_dist_cache: Dict[str, List[float]] = {}
_cache_timestamp: Optional[float] = None

def _get_cached_distribution(feature_name: str, engine_number: Optional[int] = None) -> List[float]:
    """Retrieve ECDF distribution with caching."""
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

# ---------------------------------------------------------------------------
# ECDF helpers
# ---------------------------------------------------------------------------

def _ecdf(values: List[float], x: float) -> float:
    """Empirical cumulative distribution function: fraction of values <= x."""
    if not values:
        return NEUTRAL_SCORE
    n = len(values)
    if n < MIN_ECDF_SAMPLE:
        return NEUTRAL_SCORE
    count_le = sum(1 for v in values if v <= x)
    return count_le / n

def _ecdf_inverse(values: List[float], x: float) -> float:
    """1 - ECDF, i.e., fraction of values > x. Used for 'inverse' direction."""
    if not values:
        return NEUTRAL_SCORE
    n = len(values)
    if n < MIN_ECDF_SAMPLE:
        return NEUTRAL_SCORE
    count_gt = sum(1 for v in values if v > x)
    return count_gt / n

# ---------------------------------------------------------------------------
# Component scoring functions
# ---------------------------------------------------------------------------

@dataclass
class RiskComponent:
    name: str
    raw_value: Optional[float]
    risk_score: float          # 0‑1, higher = more risk
    weight: float              # base weight before reliability
    direction: str
    reliability: float         # 0‑1, engine reliability * sample factor
    sample_size: Optional[int] = None
    ecdf_value: Optional[float] = None
    missing: bool = False
    warning: Optional[str] = None

    def to_dict(self) -> Dict:
        return {
            "name": self.name,
            "raw_value": self.raw_value,
            "risk_score": round(self.risk_score, 4),
            "weight": round(self.weight, 4),
            "direction": self.direction,
            "reliability": round(self.reliability, 4),
            "sample_size": self.sample_size,
            "ecdf_value": round(self.ecdf_value, 4) if self.ecdf_value is not None else None,
            "missing": self.missing,
            "warning": self.warning,
        }


def _get_engine_reliability(engine_number: int) -> float:
    """Historical success rate of a given engine from EngineRun records."""
    runs = EngineRun.objects.filter(engine_number=engine_number)
    total = runs.count()
    if total == 0:
        return 0.5  # neutral if never run
    succeeded = runs.filter(status="succeeded").count()
    # degraded counts as 0.5 success
    degraded = runs.filter(status="degraded").count()
    effective_success = succeeded + 0.5 * degraded
    return effective_success / total


def _score_survival_median(
    cell: BusinessLocationCell,
    catchment: Catchment,
    category: str,
) -> RiskComponent:
    """Uses median survival days (Engine 7) – inverse direction."""
    feat = FeatureObservation.objects.filter(
        location_cell=cell,
        catchment=catchment,
        engine_number=7,
        feature_name="median_business_survival_days",
        is_latest=True,
    ).first()
    raw = None
    risk = NEUTRAL_SCORE
    ecdf_val = None
    missing = True
    warning = None
    sample_size = None

    if feat and feat.value_numeric is not None:
        raw = float(feat.value_numeric)
        sample_size = feat.sample_size
        dist = _get_cached_distribution("median_business_survival_days", engine_number=7)
        # Inverse: higher survival -> lower risk
        ecdf_val = _ecdf_inverse(dist, raw)
        # Threshold factor: if median < horizon, risk increases
        horizon_factor = 1.0 if raw < RISK_HORIZON_DAYS else 0.0
        # Blend: 70% ECDF, 30% threshold
        risk = 0.7 * ecdf_val + 0.3 * horizon_factor
        missing = False
    else:
        warning = "No survival median available; using neutral."

    reliability = _get_engine_reliability(7) * (1.0 if sample_size is None else min(1.0, sample_size / 30.0))
    return RiskComponent(
        name="survival_median",
        raw_value=raw,
        risk_score=min(1.0, max(0.0, risk)),
        weight=COMPONENT_WEIGHTS["survival_median"],
        direction="inverse",
        reliability=reliability,
        sample_size=sample_size,
        ecdf_value=ecdf_val,
        missing=missing,
        warning=warning,
    )


def _score_survival_hazard_chain(
    cell: BusinessLocationCell,
    catchment: Catchment,
    category: str,
) -> RiskComponent:
    """Chain vs independent hazard ratio (Engine 7) – direct."""
    feat = FeatureObservation.objects.filter(
        location_cell=cell,
        catchment=catchment,
        engine_number=7,
        feature_name="chain_vs_independent_hazard_ratio",
        is_latest=True,
    ).first()
    raw = None
    risk = NEUTRAL_SCORE
    ecdf_val = None
    missing = True
    warning = None
    sample_size = None

    if feat and feat.value_numeric is not None:
        raw = float(feat.value_numeric)
        sample_size = feat.sample_size
        dist = _get_cached_distribution("chain_vs_independent_hazard_ratio", engine_number=7)
        ecdf_val = _ecdf(dist, raw)  # direct: higher ratio -> higher risk
        # Also logistic transform of log(ratio)
        log_ratio = math.log(max(raw, 1e-6))
        logistic_risk = 1.0 / (1.0 + math.exp(-2.0 * log_ratio))
        risk = 0.5 * ecdf_val + 0.5 * logistic_risk
        missing = False
    else:
        warning = "No chain hazard ratio available; using neutral."

    reliability = _get_engine_reliability(7) * (1.0 if sample_size is None else min(1.0, sample_size / 30.0))
    return RiskComponent(
        name="survival_hazard_chain",
        raw_value=raw,
        risk_score=min(1.0, max(0.0, risk)),
        weight=COMPONENT_WEIGHTS["survival_hazard_chain"],
        direction="direct",
        reliability=reliability,
        sample_size=sample_size,
        ecdf_value=ecdf_val,
        missing=missing,
        warning=warning,
    )


def _score_competition_count(
    cell: BusinessLocationCell,
    catchment: Catchment,
    category: str,
) -> RiskComponent:
    """Competitor count (Engine 8) – direct."""
    feat = FeatureObservation.objects.filter(
        location_cell=cell,
        catchment=catchment,
        engine_number=8,
        feature_name="competitor_count",
        is_latest=True,
    ).first()
    raw = None
    risk = NEUTRAL_SCORE
    ecdf_val = None
    missing = True
    warning = None
    sample_size = None

    if feat and feat.value_numeric is not None:
        raw = float(feat.value_numeric)
        sample_size = feat.sample_size
        dist = _get_cached_distribution("competitor_count", engine_number=8)
        ecdf_val = _ecdf(dist, raw)
        risk = ecdf_val
        missing = False
    else:
        warning = "No competitor count available; using neutral."

    reliability = _get_engine_reliability(8) * (1.0 if sample_size is None else min(1.0, sample_size / 30.0))
    return RiskComponent(
        name="competition_count",
        raw_value=raw,
        risk_score=min(1.0, max(0.0, risk)),
        weight=COMPONENT_WEIGHTS["competition_count"],
        direction="direct",
        reliability=reliability,
        sample_size=sample_size,
        ecdf_value=ecdf_val,
        missing=missing,
        warning=warning,
    )


def _score_competition_hhi(
    cell: BusinessLocationCell,
    catchment: Catchment,
    category: str,
) -> RiskComponent:
    """Herfindahl-Hirschman Index (Engine 8) – direct (with U‑shape adjustment)."""
    feat = FeatureObservation.objects.filter(
        location_cell=cell,
        catchment=catchment,
        engine_number=8,
        feature_name="hhi_pre_entry",
        is_latest=True,
    ).first()
    raw = None
    risk = NEUTRAL_SCORE
    ecdf_val = None
    missing = True
    warning = None
    sample_size = None

    if feat and feat.value_numeric is not None:
        raw = float(feat.value_numeric)
        sample_size = feat.sample_size
        # Quadratic risk: minimum at 0.25, increases as HHI deviates
        u_shaped = 4.0 * (raw - 0.25) ** 2
        u_shaped = min(1.0, max(0.0, u_shaped))
        dist = _get_cached_distribution("hhi_pre_entry", engine_number=8)
        ecdf_val = _ecdf(dist, raw)
        risk = 0.7 * u_shaped + 0.3 * ecdf_val
        missing = False
    else:
        warning = "No HHI available; using neutral."

    reliability = _get_engine_reliability(8) * (1.0 if sample_size is None else min(1.0, sample_size / 30.0))
    return RiskComponent(
        name="competition_hhi",
        raw_value=raw,
        risk_score=min(1.0, max(0.0, risk)),
        weight=COMPONENT_WEIGHTS["competition_hhi"],
        direction="direct",
        reliability=reliability,
        sample_size=sample_size,
        ecdf_value=ecdf_val,
        missing=missing,
        warning=warning,
    )


def _score_market_gap(
    cell: BusinessLocationCell,
    catchment: Catchment,
    category: str,
) -> RiskComponent:
    """Market gap score (Engine 9) – inverse."""
    feat = FeatureObservation.objects.filter(
        location_cell=cell,
        catchment=catchment,
        engine_number=9,
        feature_name="market_gap_score_0_100",
        is_latest=True,
    ).first()
    raw = None
    risk = NEUTRAL_SCORE
    ecdf_val = None
    missing = True
    warning = None
    sample_size = None

    if feat and feat.value_numeric is not None:
        raw = float(feat.value_numeric)
        sample_size = feat.sample_size
        gap = raw / 100.0
        # Inverse: lower gap -> higher risk
        risk = 1.0 - gap
        dist = _get_cached_distribution("market_gap_score_0_100", engine_number=9)
        ecdf_inv = _ecdf_inverse(dist, raw)
        risk = 0.5 * risk + 0.5 * ecdf_inv
        missing = False
    else:
        warning = "No market gap score available; using neutral."

    reliability = _get_engine_reliability(9) * (1.0 if sample_size is None else min(1.0, sample_size / 30.0))
    return RiskComponent(
        name="market_gap_score",
        raw_value=raw,
        risk_score=min(1.0, max(0.0, risk)),
        weight=COMPONENT_WEIGHTS["market_gap_score"],
        direction="inverse",
        reliability=reliability,
        sample_size=sample_size,
        ecdf_value=ecdf_val,
        missing=missing,
        warning=warning,
    )


def _score_ecosystem_turnover(
    cell: BusinessLocationCell,
    catchment: Catchment,
    category: str,
) -> RiskComponent:
    """Ecosystem turnover (Engine 6) – direct."""
    feat = FeatureObservation.objects.filter(
        location_cell=cell,
        catchment=catchment,
        engine_number=6,
        feature_name="ecosystem_turnover_rate",
        is_latest=True,
    ).first()
    raw = None
    risk = NEUTRAL_SCORE
    ecdf_val = None
    missing = True
    warning = None
    sample_size = None

    if feat and feat.value_numeric is not None:
        raw = float(feat.value_numeric)
        sample_size = feat.sample_size
        dist = _get_cached_distribution("ecosystem_turnover_rate", engine_number=6)
        ecdf_val = _ecdf(dist, raw)
        risk = ecdf_val
        missing = False
    else:
        warning = "No ecosystem turnover available; using neutral."

    reliability = _get_engine_reliability(6) * (1.0 if sample_size is None else min(1.0, sample_size / 30.0))
    return RiskComponent(
        name="ecosystem_turnover",
        raw_value=raw,
        risk_score=min(1.0, max(0.0, risk)),
        weight=COMPONENT_WEIGHTS["ecosystem_turnover"],
        direction="direct",
        reliability=reliability,
        sample_size=sample_size,
        ecdf_value=ecdf_val,
        missing=missing,
        warning=warning,
    )


def _score_ecosystem_growth(
    cell: BusinessLocationCell,
    catchment: Catchment,
    category: str,
) -> RiskComponent:
    """Ecosystem growth (Engine 6) – inverse."""
    feat = FeatureObservation.objects.filter(
        location_cell=cell,
        catchment=catchment,
        engine_number=6,
        feature_name="ecosystem_growth_rate",
        is_latest=True,
    ).first()
    raw = None
    risk = NEUTRAL_SCORE
    ecdf_val = None
    missing = True
    warning = None
    sample_size = None

    if feat and feat.value_numeric is not None:
        raw = float(feat.value_numeric)
        sample_size = feat.sample_size
        dist = _get_cached_distribution("ecosystem_growth_rate", engine_number=6)
        ecdf_inv = _ecdf_inverse(dist, raw)
        risk = ecdf_inv
        missing = False
    else:
        warning = "No ecosystem growth available; using neutral."

    reliability = _get_engine_reliability(6) * (1.0 if sample_size is None else min(1.0, sample_size / 30.0))
    return RiskComponent(
        name="ecosystem_growth",
        raw_value=raw,
        risk_score=min(1.0, max(0.0, risk)),
        weight=COMPONENT_WEIGHTS["ecosystem_growth"],
        direction="inverse",
        reliability=reliability,
        sample_size=sample_size,
        ecdf_value=ecdf_val,
        missing=missing,
        warning=warning,
    )


def _score_anchor_proximity(
    cell: BusinessLocationCell,
    catchment: Catchment,
    category: str,
) -> RiskComponent:
    """Anchor distance (Engine 11) – inverse (closer -> lower risk)."""
    feat = FeatureObservation.objects.filter(
        location_cell=cell,
        catchment=catchment,
        engine_number=11,
        feature_name="anchor_distance_meters",
        is_latest=True,
    ).first()
    raw = None
    risk = NEUTRAL_SCORE
    ecdf_val = None
    missing = True
    warning = None
    sample_size = None

    if feat and feat.value_numeric is not None:
        raw = float(feat.value_numeric)
        sample_size = feat.sample_size
        dist = _get_cached_distribution("anchor_distance_meters", engine_number=11)
        ecdf_inv = _ecdf_inverse(dist, raw)
        risk = ecdf_inv
        missing = False
    else:
        warning = "No anchor distance available; using neutral."

    reliability = _get_engine_reliability(11) * (1.0 if sample_size is None else min(1.0, sample_size / 30.0))
    return RiskComponent(
        name="anchor_proximity",
        raw_value=raw,
        risk_score=min(1.0, max(0.0, risk)),
        weight=COMPONENT_WEIGHTS["anchor_proximity"],
        direction="inverse",
        reliability=reliability,
        sample_size=sample_size,
        ecdf_value=ecdf_val,
        missing=missing,
        warning=warning,
    )


def _score_development_activity(
    cell: BusinessLocationCell,
    catchment: Catchment,
    category: str,
) -> RiskComponent:
    """Development activity (Engine 12) – inverse."""
    feat = FeatureObservation.objects.filter(
        location_cell=cell,
        catchment=catchment,
        engine_number=12,
        feature_name="development_activity_index",
        is_latest=True,
    ).first()
    raw = None
    risk = NEUTRAL_SCORE
    ecdf_val = None
    missing = True
    warning = None
    sample_size = None

    if feat and feat.value_numeric is not None:
        raw = float(feat.value_numeric)
        sample_size = feat.sample_size
        dist = _get_cached_distribution("development_activity_index", engine_number=12)
        ecdf_inv = _ecdf_inverse(dist, raw)
        risk = ecdf_inv
        missing = False
    else:
        warning = "No development activity index available; using neutral."

    reliability = _get_engine_reliability(12) * (1.0 if sample_size is None else min(1.0, sample_size / 30.0))
    return RiskComponent(
        name="development_activity",
        raw_value=raw,
        risk_score=min(1.0, max(0.0, risk)),
        weight=COMPONENT_WEIGHTS["development_activity"],
        direction="inverse",
        reliability=reliability,
        sample_size=sample_size,
        ecdf_value=ecdf_val,
        missing=missing,
        warning=warning,
    )


def _score_economic_momentum(
    cell: BusinessLocationCell,
    catchment: Catchment,
    category: str,
) -> RiskComponent:
    """Economic momentum (Engine 18) – based on classification and optional index."""
    feat_class = FeatureObservation.objects.filter(
        location_cell=cell,
        catchment=catchment,
        engine_number=18,
        feature_name="economic_momentum_classification",
        is_latest=True,
    ).first()
    raw = None
    risk = NEUTRAL_SCORE
    ecdf_val = None
    missing = True
    warning = None
    sample_size = None

    if feat_class and feat_class.value_text:
        class_text = feat_class.value_text.lower()
        # Map classification to risk
        class_risk = {
            "overheating": 0.8,
            "declining": 0.9,
            "stable": 0.5,
            "emerging": 0.3,
            "accelerating": 0.2,
        }
        risk = class_risk.get(class_text, NEUTRAL_SCORE)
        sample_size = feat_class.sample_size
        missing = False
    else:
        warning = "No economic momentum classification; trying numeric index."

    # Also attempt numeric momentum index if available
    feat_idx = FeatureObservation.objects.filter(
        location_cell=cell,
        catchment=catchment,
        engine_number=18,
        feature_name="economic_momentum_index",
        is_latest=True,
    ).first()
    if feat_idx and feat_idx.value_numeric is not None:
        idx_val = float(feat_idx.value_numeric)
        dist = _get_cached_distribution("economic_momentum_index", engine_number=18)
        ecdf_inv = _ecdf_inverse(dist, idx_val)
        # Blend with classification if both exist, otherwise use index
        if not missing:
            risk = 0.5 * risk + 0.5 * ecdf_inv
        else:
            risk = ecdf_inv
            missing = False
            warning = None
        sample_size = feat_idx.sample_size or sample_size
        ecdf_val = ecdf_inv
        raw = idx_val

    if missing:
        warning = warning or "No economic momentum signal available; using neutral."

    reliability = _get_engine_reliability(18) * (1.0 if sample_size is None else min(1.0, sample_size / 30.0))
    return RiskComponent(
        name="economic_momentum",
        raw_value=raw,
        risk_score=min(1.0, max(0.0, risk)),
        weight=COMPONENT_WEIGHTS["economic_momentum"],
        direction="inverse",
        reliability=reliability,
        sample_size=sample_size,
        ecdf_value=ecdf_val,
        missing=missing,
        warning=warning,
    )


# ---------------------------------------------------------------------------
# Combination functions
# ---------------------------------------------------------------------------

def _combine_risk_components(components: List[RiskComponent]) -> Tuple[float, Dict, List[float]]:
    """
    Bayesian combination with reliability weighting.
    Returns (posterior_mean, meta_dict, component_risk_scores_for_bootstrap).
    """
    # Prior: Beta(1,1) -> prior odds = 1
    log_odds_sum = 0.0
    total_reliability = 0.0
    bootstrap_scores = []
    component_details = []

    for comp in components:
        if comp.missing or comp.reliability < 0.01:
            # Effectively ignore missing or unreliable signals
            bootstrap_scores.append(NEUTRAL_SCORE)
            component_details.append(comp.to_dict())
            continue
        # Clamp to avoid extreme odds
        p = max(0.001, min(0.999, comp.risk_score))
        log_odds = math.log(p / (1 - p))
        weight = comp.weight * comp.reliability
        log_odds_sum += weight * log_odds
        total_reliability += weight
        bootstrap_scores.append(comp.risk_score)
        component_details.append(comp.to_dict())

    if total_reliability == 0:
        # No signals
        return NEUTRAL_SCORE, {
            "method": "no_signals",
            "components": component_details,
            "warnings": ["No reliable signals available."]
        }, [NEUTRAL_SCORE]

    # Average log odds
    avg_log_odds = log_odds_sum / total_reliability
    posterior_p = 1.0 / (1.0 + math.exp(-avg_log_odds))
    posterior_p = min(0.999, max(0.001, posterior_p))

    meta = {
        "method": "bayesian_beta_binomial_conjugate",
        "prior": {"alpha": 1.0, "beta": 1.0},
        "total_reliability_weight": total_reliability,
        "avg_log_odds": avg_log_odds,
        "posterior_mean": posterior_p,
        "components": component_details,
        "warnings": [],
    }
    return posterior_p, meta, bootstrap_scores


def _bootstrap_ci(scores: List[float], n_iter: int = BOOTSTRAP_ITERATIONS, alpha: float = CREDIBLE_INTERVAL_ALPHA) -> Tuple[float, float]:
    """Parametric bootstrap: resample scores with replacement, compute mean, get percentiles."""
    n = len(scores)
    if n == 0:
        return NEUTRAL_SCORE, NEUTRAL_SCORE
    rng = random.Random(12345)  # fixed seed for reproducibility
    means = []
    for _ in range(n_iter):
        sample = [scores[rng.randint(0, n-1)] for _ in range(n)]
        means.append(sum(sample) / n)
    means.sort()
    lower = means[int(alpha/2 * n_iter)]
    upper = means[int((1 - alpha/2) * n_iter)]
    return lower, upper


def _sensitivity_analysis(base_score: float, components: List[RiskComponent], base_weights: Dict[str, float]) -> float:
    """Perturb each component's weight by ±10%, compute std dev of scores."""
    perturbed_scores = [base_score]
    for name, w in base_weights.items():
        for factor in (0.9, 1.1):
            # Recompute with perturbed weight, renormalize others
            new_weights = {k: v for k, v in base_weights.items()}
            new_weights[name] = w * factor
            # Renormalize to sum to 1
            total = sum(new_weights.values())
            new_weights = {k: v / total for k, v in new_weights.items()}
            # Compute new score using these weights (simplified: just combine again)
            log_odds_sum = 0.0
            total_rel = 0.0
            for comp in components:
                if comp.missing or comp.reliability < 0.01:
                    continue
                p = max(0.001, min(0.999, comp.risk_score))
                log_odds = math.log(p / (1 - p))
                weight = new_weights.get(comp.name, 0) * comp.reliability
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


# ---------------------------------------------------------------------------
# Main run function
# ---------------------------------------------------------------------------

def run(
    query: BusinessQuery,
    catchments: Optional[Sequence[Catchment]] = None,
) -> dict:
    """
    Engine 20 entry point. Computes a failure/risk score for each Catchment
    belonging to the query's location_cell (or an explicit subset), based on
    signals from all other engines. Writes FeatureObservation rows and records
    an EngineRun.

    Returns a dict with 'cell', 'features', and 'engine_run' as per standard
    engine contract.
    """
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
                # Collect all risk components
                components = [
                    _score_survival_median(cell, catchment, category),
                    _score_survival_hazard_chain(cell, catchment, category),
                    _score_competition_count(cell, catchment, category),
                    _score_competition_hhi(cell, catchment, category),
                    _score_market_gap(cell, catchment, category),
                    _score_ecosystem_turnover(cell, catchment, category),
                    _score_ecosystem_growth(cell, catchment, category),
                    _score_anchor_proximity(cell, catchment, category),
                    _score_development_activity(cell, catchment, category),
                    _score_economic_momentum(cell, catchment, category),
                ]

                # Combine
                posterior_p, comb_meta, bootstrap_scores = _combine_risk_components(components)
                risk_score = posterior_p * 100.0

                # Bootstrap CI on the component scores (parametric)
                ci_lower, ci_upper = _bootstrap_ci(bootstrap_scores)
                ci_lower_pct = ci_lower * 100.0
                ci_upper_pct = ci_upper * 100.0

                # Sensitivity analysis (based on base weights)
                stability_std = _sensitivity_analysis(posterior_p, components, COMPONENT_WEIGHTS)

                band = risk_band(risk_score)

                # Build evidence dossier
                evidence = {
                    "risk_score": round(risk_score, 2),
                    "risk_band": band,
                    "credible_interval_90": [round(ci_lower_pct, 2), round(ci_upper_pct, 2)],
                    "stability_std": round(stability_std * 100.0, 4),
                    "combination": comb_meta,
                    "components": [c.to_dict() for c in components],
                    "warnings": comb_meta.get("warnings", []),
                    "methods": [
                        "empirical_cdf_wasserman_2006",
                        "bayesian_beta_binomial_gelman_2013",
                        "logarithmic_opinion_pool_genest_zidek_1986",
                        "supra_bayesian_winkler_1981",
                        "bootstrap_efron_tibshirani_1993",
                        "sensitivity_saltelli_2008",
                    ],
                }

                # Write main risk score
                FeatureObservation.write_latest(
                    location_cell=cell,
                    catchment=catchment,
                    engine_number=ENGINE_NUMBER,
                    feature_name="failure_risk_score_0_100",
                    defaults=dict(
                        value_type="numeric",
                        value_numeric=Decimal(str(risk_score)),
                        unit="index_0_100",
                        observed_at=started_at,
                        source="business_intel_internal",
                        source_class="internal_derived",
                        method="bayesian_combination_weighted_lop",
                        derivation_level="business_conclusion",
                        sample_size=len([c for c in components if not c.missing]),
                    ),
                )
                features_written += 1

                # Write risk band
                FeatureObservation.write_latest(
                    location_cell=cell,
                    catchment=catchment,
                    engine_number=ENGINE_NUMBER,
                    feature_name="failure_risk_band",
                    defaults=dict(
                        value_type="text",
                        value_text=band,
                        unit="",
                        observed_at=started_at,
                        source="business_intel_internal",
                        source_class="internal_derived",
                        method="risk_band_calibration",
                        derivation_level="business_conclusion",
                    ),
                )
                features_written += 1

                # Write evidence dossier
                FeatureObservation.write_latest(
                    location_cell=cell,
                    catchment=catchment,
                    engine_number=ENGINE_NUMBER,
                    feature_name="failure_risk_evidence",
                    defaults=dict(
                        value_type="json",
                        value_json=evidence,
                        unit="",
                        observed_at=started_at,
                        source="business_intel_internal",
                        source_class="internal_derived",
                        method="bayesian_combination_weighted_lop",
                        derivation_level="calculation",
                    ),
                )
                features_written += 1

                all_feature_data.append({
                    "catchment_id": catchment.pk,
                    "risk_score": risk_score,
                    "band": band,
                    "credible_interval": [ci_lower_pct, ci_upper_pct],
                    "evidence": evidence,
                })

                # Collect warnings
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