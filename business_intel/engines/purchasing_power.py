"""
business_intel/engines/purchasing_power.py

Engine 3 — Purchasing Power & Economic Capacity Engine
Per business_location_intelligence_engines_data_models_v2.md, Section 3.

NOTE ON THE SPEC FILE: business_location_intelligence_engines_data_models_v2.md
is referenced by every engine stub in this codebase but does not exist on
disk (confirmed via `find`). Built without it, matching Engine 2
(population.py) and Engine 8 (competition.py) for structure/schema
contract and citation discipline. Anywhere this would otherwise need to
guess at the missing spec, it says so instead of guessing silently.

=============================================================================
WHY THIS IS A REAL STATISTICS/ML ENGINE, NOT A FLAT WEIGHTED AVERAGE
=============================================================================
v1 of this module blended two signals with a fixed 50/50 average and an
ad hoc shrinkage weight alpha = n/(n+k) with k hand-picked. That is not
defensible as "pure data science" -- it has no basis in how a composite
index's *own data* says the signals should be weighted, and no basis in
how much local vs. prior evidence should count. This version replaces
both pieces with real, cited, textbook methods:

1. PCA-constructed wealth/purchasing-power index (unsupervised, no label
   data required)
   Filmer, D. and Pritchett, L.H. (2001). "Estimating Wealth Effects
   Without Expenditure Data -- Or Tears: An Application to Educational
   Enrollments in States of India." Demography, 38(1), 115-132.
   Rutstein, S.O. and Johnson, K. (2004). "The DHS Wealth Index." DHS
   Comparative Reports No. 6. ORC Macro.
   This is the actual method national Demographic and Health Surveys
   (DHS) and the World Bank use to build a socioeconomic-status index
   from several correlated observed indicators when no ground-truth
   income/expenditure data exists (exactly this system's situation --
   Kenya's KIHBS microdata isn't available here either): standardize
   each indicator, take the first principal component of their
   correlation matrix as the composite index. The first PC is, by
   construction, the linear combination of the indicators that explains
   the most shared variance -- i.e. the data itself determines the
   weights, not an editorial 50/50 guess.

2. Empirical Bayes (method-of-moments) shrinkage toward a prior
   Same statistical family as this codebase's own Fay-Herriot use in
   population.py (Fay, R.E. and Herriot, R.A. 1979, JASA 74(366),
   269-277) -- but the simpler normal-normal Empirical Bayes case
   (Efron, B. and Morris, C. 1975. "Data Analysis Using Stein's
   Estimator and Its Generalizations." JASA, 70(350), 311-319 -- the
   James-Stein / Empirical Bayes estimator), since Section 3 has no
   fitted variance-components model the way population.py's
   FittedFayHerriotModel does. The between-catchment variance A is
   estimated FROM THE DATA via method-of-moments (same technique
   population.py already uses for Fay-Herriot's own A), not hand-picked
   -- so how much to trust a local PCA score vs. the county prior is
   itself a data-driven answer, area by area.

Both methods are genuinely unsupervised / require no labeled income
data this system doesn't have -- unlike a supervised regression (which
would need real ground-truth income figures to fit against, which this
codebase does not have and this module will not fabricate).

=============================================================================
ADMINISTRATIVE-UNIT NARROWING
=============================================================================
A caller can request purchasing power resolved at catchment level (the
default -- one small radius/isochrone around a candidate point), or at
county / sub-county level directly (`compute_purchasing_power_for_admin_unit`),
reusing the SAME real AdministrativeHierarchyProvider /
AdminUnit hierarchy population.py already implements (imported, not
re-invented) -- county -> sub-county -> sub-location, per KNBS KPHC
Volume II. When a catchment's own business sample is too thin to trust
(see MIN_DIRECT_SAMPLE_SIZE), `compute_purchasing_power` automatically
widens to the next coarsest admin unit that has enough data, and records
which level it actually resolved to (`resolved_admin_level`) -- the same
"finest available, not silently substituted" discipline
resolve_population_source_unit() already applies to population.

=============================================================================
EDGE CASES THIS MODULE HANDLES EXPLICITLY (not just tries to avoid)
=============================================================================
- Zero businesses in a catchment -> all signals None, PCA/EB skipped,
  warnings recorded, falls back up the admin hierarchy automatically.
- Fewer catchments system-wide than MIN_PCA_FIT_SAMPLE to fit PCA at all
  -> falls back to a simple mean-of-standardized-indicators index
  (still real, just lower-powered), flagged in warnings and
  `wealth_index_method`.
- An indicator with zero variance across the fit corpus (e.g. every
  catchment has an identical price tier) -> that column is dropped
  before fitting rather than producing a divide-by-zero.
- A single-indicator catchment (only one of the 5 indicators observed)
  -> PCA scoring falls back to using the corpus mean for missing
  indicators (standard PCA-with-missing-data practice: impute at the
  mean, which contributes zero to the standardized/centered score),
  and `indicators_imputed` records exactly which ones were faked-neutral
  so nothing downstream mistakes an imputed value for an observation.
- Between-area variance estimated as negative (a real possibility with
  method-of-moments on small samples) -> clipped to 0 per the standard
  Fay-Herriot/Empirical-Bayes convention (same clip population.py's own
  Fay-Herriot fitting would need), meaning full shrinkage to the prior.
- Requested admin_level narrower than any data exists for (e.g. a ward
  with zero synced businesses) -> walks UP the hierarchy (ward/sub-
  location -> sub-county -> county) same direction and same honesty
  rule as population.py's own fallback, never silently invents a
  number at the requested level.
- Candidate point near an administrative border -> inherits the same
  known limitation population.py already documents (resolution is by
  the cell centroid's polygon membership, not the catchment's full
  footprint) -- not solved here, just not hidden.
- SyntheticCountyEconomicPriorProvider or SyntheticAdminHierarchyProvider
  in use -> engine run is forced to `degraded` status with an explicit
  reason, same as population.py enforces for its own synthetic provider.

Scope boundary
--------------
Depends on business_intel.models and, for the real admin hierarchy, on
business_intel.engines.population's AdministrativeHierarchyProvider /
AdminUnit (imported, not duplicated). No dependency on services.py or
views.py.
"""

from __future__ import annotations

import logging
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Mapping, Optional, Sequence

import numpy as np
from django.db import transaction
from django.db.models import Avg, Count
from django.utils import timezone

from ..models import (
    BusinessEntity,
    BusinessLocationCell,
    BusinessQuery,
    Catchment,
    EngineRun,
    FeatureObservation,
)
from .population import AdministrativeHierarchyProvider, AdminUnit, SyntheticAdminHierarchyProvider

logger = logging.getLogger("business_intel.engines.purchasing_power")

ENGINE_NUMBER = 3

# ============================================================================
# Google Places priceLevel -> ordinal tier (real Places API enum values)
# ============================================================================

PRICE_LEVEL_ORDINAL: Mapping[str, int] = {
    "PRICE_LEVEL_FREE": 0,
    "PRICE_LEVEL_INEXPENSIVE": 1,
    "PRICE_LEVEL_MODERATE": 2,
    "PRICE_LEVEL_EXPENSIVE": 3,
    "PRICE_LEVEL_VERY_EXPENSIVE": 4,
}
PRICE_LEVEL_MAX_ORDINAL = 4

# ============================================================================
# Discretionary vs. necessity taxonomy (Engel's Law application) --
# editorial classification over models.BUSINESS_CATEGORY_CHOICES, labeled
# as such, not a measured constant. See module docstring for the two
# judgment calls worth a second opinion (clinic, gym).
# ============================================================================

CATEGORY_SPENDING_TIER: Mapping[str, str] = {
    "pharmacy": "necessity",
    "supermarket": "necessity",
    "clinic": "necessity",
    "hardware_store": "necessity",
    "bank_agent": "necessity",
    "restaurant": "discretionary",
    "salon_barbershop": "discretionary",
    "electronics_shop": "discretionary",
    "gym": "discretionary",
    "boutique_clothing": "discretionary",
    "bar": "discretionary",
    "bakery": "discretionary",
    "other": "unclassified",
}

# ============================================================================
# Indicator set for the PCA wealth index. Each is a real, directly
# computable per-catchment quantity -- no fabricated inputs.
# log1p on review_count per standard practice for heavily right-skewed
# count data (review counts span orders of magnitude; untransformed they
# would dominate the correlation matrix on scale alone).
# ============================================================================

INDICATOR_NAMES = (
    "price_tier_mean",
    "discretionary_share_pct",
    "business_density_per_km2",
    "avg_rating",
    "log_avg_review_count",
)

# Comrey, A.L. and Lee, H.B. (1992). A First Course in Factor Analysis
# (2nd ed.). Erlbaum -- widely cited rule-of-thumb minimum of ~5
# observations per variable for a stable factor/PCA solution. 5
# indicators * 5 = 25; rounded up to a flat 30 as the floor below which
# this module refuses to fit its own PCA loadings and instead falls
# back to a simpler (still real, just lower-powered) standardized-mean
# index -- a calibration input, documented as such.
MIN_PCA_FIT_SAMPLE = 30

# Below this many synced operational businesses, a catchment's own
# direct signal is not trusted enough to report on its own -- the
# engine walks up the administrative hierarchy instead. Calibration
# input, not a measured constant.
MIN_DIRECT_SAMPLE_SIZE = 5


# ============================================================================
# County-level economic prior provider (the Empirical-Bayes prior term)
# ============================================================================

@dataclass(frozen=True)
class CountyEconomicPrior:
    """A county-level prior for local economic capacity, 0-100 scale."""
    index_0_100: float
    source: str
    source_class: str  # "survey_derived" | "synthetic"


class CountyEconomicPriorProvider(ABC):
    """
    Abstract hook for a county-level economic-capacity prior.
    Deliberately left unimplemented in this module -- same documented
    decision population.py makes for SpatialSmoothingProvider. Real
    implementation requires sourcing and transcribing actual KNBS KIHBS
    2015/16 (or County Gross Product) figures, the way
    kenya_counties_2019_census.py transcribed real KPHC population
    figures -- genuine separate work, not fabricated here.
    """

    @abstractmethod
    def get_prior(self, county: str, subcounty: Optional[str] = None) -> Optional[CountyEconomicPrior]:
        ...


class SyntheticCountyEconomicPriorProvider(CountyEconomicPriorProvider):
    """Fixed neutral (50.0) prior for exercising the math only. NOT real
    data -- must never back a production request. Forces the EngineRun
    to 'degraded' (see run())."""

    def get_prior(self, county: str, subcounty: Optional[str] = None) -> Optional[CountyEconomicPrior]:
        return CountyEconomicPrior(
            index_0_100=50.0,
            source="SyntheticCountyEconomicPriorProvider (fixed neutral prior, NOT real data)",
            source_class="synthetic",
        )


# ============================================================================
# Direct observations
# ============================================================================

@dataclass
class PriceLevelObservation:
    """
    One business's Google Places priceLevel. BusinessEntity does not
    persist price_level today (google_client.py fetches it, then drops
    it before storage) -- callers must build these from the raw dicts
    search_places_category() already returns until that gap is closed.
    """
    place_id: str
    price_level: Optional[str] = None


# ============================================================================
# Pure-math layer (DB-import-free where practical)
# ============================================================================

def price_tier_composite(observations: Sequence[PriceLevelObservation]) -> tuple[Optional[float], int]:
    tiers = [PRICE_LEVEL_ORDINAL[o.price_level] for o in observations if o.price_level in PRICE_LEVEL_ORDINAL]
    if not tiers:
        return None, 0
    return sum(tiers) / len(tiers), len(tiers)


def category_mix_shares(categories: Sequence[str]) -> tuple[Optional[float], Optional[float], int]:
    """(discretionary_pct, necessity_pct, n)."""
    if not categories:
        return None, None, 0
    n = len(categories)
    disc = sum(1 for c in categories if CATEGORY_SPENDING_TIER.get(c) == "discretionary")
    nec = sum(1 for c in categories if CATEGORY_SPENDING_TIER.get(c) == "necessity")
    return (100.0 * disc / n), (100.0 * nec / n), n


@dataclass
class RawIndicators:
    """One row of the PCA input matrix -- a catchment's (or admin
    unit's) raw, untransformed indicator values before standardization.
    None means genuinely unobserved, not zero."""
    price_tier_mean: Optional[float] = None
    discretionary_share_pct: Optional[float] = None
    business_density_per_km2: Optional[float] = None
    avg_rating: Optional[float] = None
    avg_review_count: Optional[float] = None

    def as_vector(self) -> dict:
        return {
            "price_tier_mean": self.price_tier_mean,
            "discretionary_share_pct": self.discretionary_share_pct,
            "business_density_per_km2": self.business_density_per_km2,
            "avg_rating": self.avg_rating,
            "log_avg_review_count": (
                math.log1p(self.avg_review_count) if self.avg_review_count is not None else None
            ),
        }


# ============================================================================
# PCA wealth-index model (Filmer & Pritchett 2001 / Rutstein & Johnson 2004)
# ============================================================================

@dataclass
class PCAWealthIndexModel:
    """
    Fitted first-principal-component model for turning several
    standardized indicators into one composite index. Analogous to a
    fitted DHS Wealth Index -- meant to be fit ONCE across a broad
    corpus of catchments and then reused to *score* new, individual
    catchments (out-of-sample projection), exactly like a real DHS
    Wealth Index is fit nationally and then applied per household.

    Persisting a fitted instance of this across requests (rather than
    refitting from whatever catchments happen to be in the DB at
    request time) is real, separate infra work -- e.g. a periodic batch
    job that calls `fit()` and caches loadings/means/stds -- and is
    intentionally left as a caller's responsibility rather than
    silently refit-per-request here, the same way population.py leaves
    SpatialSmoothingProvider as a documented hook rather than faking a
    fit. `fit_from_catchments()` below is the honest live-fit fallback
    for when no cached model exists yet.
    """
    indicator_names: tuple
    means: np.ndarray
    stds: np.ndarray
    loadings: np.ndarray            # first-PC eigenvector, one weight per surviving indicator
    explained_variance_ratio: float
    score_mean: float               # for rescaling raw PC1 scores to a 0-100 index
    score_std: float
    fit_n: int

    @classmethod
    def fit(cls, matrix: np.ndarray, indicator_names: Sequence[str]) -> Optional["PCAWealthIndexModel"]:
        """
        matrix: (n_samples, n_indicators), no NaNs (caller must have
        already dropped or imputed rows before fitting -- fitting on
        imputed rows silently would bias the loadings, so this method
        requires a clean matrix and raises on NaN rather than guessing).
        Zero-variance columns are dropped (can't standardize them --
        division by zero) with a warning-worthy explained_variance_ratio
        of None returned via the caller's own check on `n_kept` vs
        len(indicator_names) if that distinction matters to them.
        Returns None (not an exception) if fewer than MIN_PCA_FIT_SAMPLE
        rows are available -- an honest "can't fit yet", matching this
        codebase's own preference for a recorded gap over a guess.
        """
        n = matrix.shape[0]
        if n < MIN_PCA_FIT_SAMPLE:
            return None
        if np.isnan(matrix).any():
            raise ValueError("PCAWealthIndexModel.fit() requires a NaN-free matrix; impute or drop rows first.")

        stds = matrix.std(axis=0, ddof=1)
        keep = stds > 1e-9
        if keep.sum() < 2:
            # Fewer than 2 indicators with real variance -- PCA is
            # meaningless (a single varying indicator has only itself
            # to be "first component" of). Caller should fall back to
            # the simpler standardized-mean index in this case.
            return None

        kept_names = tuple(name for name, k in zip(indicator_names, keep) if k)
        sub = matrix[:, keep]
        means = sub.mean(axis=0)
        stds_kept = sub.std(axis=0, ddof=1)
        standardized = (sub - means) / stds_kept

        corr = np.corrcoef(standardized, rowvar=False)
        eigenvalues, eigenvectors = np.linalg.eigh(corr)  # ascending order
        top_idx = int(np.argmax(eigenvalues))
        loadings = eigenvectors[:, top_idx]
        # Sign convention: orient so higher price tier contributes
        # positively (PCA sign is arbitrary; without fixing it, "high
        # index" could mean either end depending on eigenvector sign).
        if "price_tier_mean" in kept_names:
            i = kept_names.index("price_tier_mean")
            if loadings[i] < 0:
                loadings = -loadings
        explained_variance_ratio = float(eigenvalues[top_idx] / eigenvalues.sum())

        raw_scores = standardized @ loadings
        return cls(
            indicator_names=kept_names,
            means=means,
            stds=stds_kept,
            loadings=loadings,
            explained_variance_ratio=explained_variance_ratio,
            score_mean=float(raw_scores.mean()),
            score_std=float(raw_scores.std(ddof=1)) if raw_scores.std(ddof=1) > 1e-9 else 1.0,
            fit_n=n,
        )

    def score(self, indicators: Mapping[str, Optional[float]]) -> tuple[Optional[float], list]:
        """
        Project one observation onto the fitted first PC, rescaled to a
        0-100 index via the fit corpus's own score distribution
        (mean/std -> roughly a 0-100 range through a logistic squash,
        so a single wild outlier can't blow past the scale).
        Missing indicators are imputed at the fit corpus's mean for
        that indicator (contributes exactly 0 to the standardized,
        centered score -- the correct no-information imputation for
        this method) and returned in the warnings list so callers know
        which values were imputed rather than observed.
        Returns (None, warnings) if every indicator this model uses is
        missing -- imputing 100% of the input would be reporting a pure
        artifact of the corpus mean, not an estimate of this catchment.
        """
        warnings = []
        vec = []
        n_observed = 0
        for name, mean in zip(self.indicator_names, self.means):
            v = indicators.get(name)
            if v is None:
                vec.append(mean)
                warnings.append(f"{name} missing; imputed at fit-corpus mean for PCA scoring.")
            else:
                vec.append(v)
                n_observed += 1
        if n_observed == 0:
            return None, warnings + ["All indicators missing; PCA score not computed."]

        standardized = (np.array(vec) - self.means) / self.stds
        raw_score = float(standardized @ self.loadings)
        # Logistic squash centered on the fit corpus's own score
        # distribution -> approx 0-100, matching this engine's other
        # indices' scale without an arbitrary hard clip.
        z = (raw_score - self.score_mean) / self.score_std
        index_0_100 = 100.0 / (1.0 + math.exp(-z))
        return index_0_100, warnings


def simple_standardized_mean_index(
    indicators: Mapping[str, Optional[float]],
    corpus_means: Mapping[str, float],
    corpus_stds: Mapping[str, float],
) -> tuple[Optional[float], list]:
    """
    Fallback used only when MIN_PCA_FIT_SAMPLE isn't met: equal-weighted
    mean of whatever indicators ARE observed, each standardized against
    the (smaller, still real) corpus available. Still a real statistical
    transform (z-scores, not raw units mashed together) -- just without
    PCA's data-driven weighting, which needs more samples than this to
    be trustworthy. Same logistic rescale as PCAWealthIndexModel.score
    for a consistent 0-100 output.
    """
    warnings = ["PCA fit corpus below MIN_PCA_FIT_SAMPLE; using standardized-mean fallback (see module docstring)."]
    zs = []
    for name, v in indicators.items():
        if v is None or name not in corpus_means or corpus_stds.get(name, 0) < 1e-9:
            continue
        zs.append((v - corpus_means[name]) / corpus_stds[name])
    if not zs:
        return None, warnings + ["No usable indicators for fallback index."]
    z = sum(zs) / len(zs)
    index_0_100 = 100.0 / (1.0 + math.exp(-z))
    return index_0_100, warnings


# ============================================================================
# Empirical Bayes (method-of-moments) shrinkage toward the county prior
# Efron & Morris (1975); same family as this codebase's own Fay-Herriot
# use in population.py, simplified to the case with no fitted variance
# model available.
# ============================================================================

def estimate_between_area_variance(direct_estimates: Sequence[float], sampling_variances: Sequence[float]) -> float:
    """
    Method-of-moments estimate of A (the true between-area variance of
    the underlying index, net of sampling noise), same technique
    population.py cites for its own Fay-Herriot A. Clipped to 0 per the
    standard convention when the raw estimate goes negative (a real,
    expected outcome with few/noisy areas -- meaning "shrink fully to
    the prior," not an error).
    """
    if len(direct_estimates) < 2:
        return 0.0
    y = np.array(direct_estimates)
    d_bar = float(np.mean(sampling_variances)) if sampling_variances else 0.0
    sample_var = float(np.var(y, ddof=1))
    return max(0.0, sample_var - d_bar)


def empirical_bayes_shrink(
    direct_estimate: Optional[float],
    direct_sample_size: int,
    prior_estimate: Optional[float],
    between_area_variance_A: float,
    indicator_std_estimate: float = 15.0,
) -> tuple[Optional[float], Optional[float]]:
    """
    theta_hat = gamma * y_i + (1 - gamma) * prior,   gamma = A / (A + D_i)
    D_i (sampling variance of the direct estimate) approximated as
    indicator_std_estimate^2 / n -- the standard SE^2-of-a-mean form,
    using indicator_std_estimate=15.0 (index points, on the 0-100
    scale) as a documented, conservative default for a single index's
    spread when no better local variance estimate exists; pass a
    tighter/looser value if one is available. This is the same role
    Fay-Herriot's D_i plays in population.py, just estimated rather than
    supplied by a fitted sampling-variance model.

    Returns (composite, gamma). If direct is None, returns
    (prior, 0.0). If prior is None, returns (direct, 1.0). If both are
    None, returns (None, None).
    """
    if direct_estimate is None and prior_estimate is None:
        return None, None
    if direct_estimate is None:
        return prior_estimate, 0.0
    if prior_estimate is None:
        return direct_estimate, 1.0
    n = max(direct_sample_size, 1)
    D_i = (indicator_std_estimate ** 2) / n
    gamma = between_area_variance_A / (between_area_variance_A + D_i) if (between_area_variance_A + D_i) > 0 else 0.0
    composite = gamma * direct_estimate + (1 - gamma) * prior_estimate
    return composite, gamma


# ============================================================================
# Feature output
# ============================================================================

@dataclass
class PurchasingPowerFeatures:
    catchment: Optional[Catchment] = None

    price_tier_mean: Optional[float] = None
    price_tier_sample_size: int = 0
    discretionary_share_pct: Optional[float] = None
    necessity_share_pct: Optional[float] = None
    business_density_per_km2: Optional[float] = None
    avg_rating: Optional[float] = None
    avg_review_count: Optional[float] = None
    category_mix_sample_size: int = 0

    wealth_index_pca: Optional[float] = None
    wealth_index_method: str = ""          # "pca_first_component" | "standardized_mean_fallback"
    pca_explained_variance_ratio: Optional[float] = None
    indicators_imputed: list = field(default_factory=list)

    county_prior_index: Optional[float] = None
    county_prior_source: str = ""
    county_prior_source_class: str = ""

    between_area_variance_A: Optional[float] = None
    composite_purchasing_power_index: Optional[float] = None
    empirical_bayes_gamma: Optional[float] = None

    resolved_admin_level: str = ""         # "catchment" | "subcounty" | "county"
    resolved_admin_name: str = ""
    direct_estimate_sample_size: int = 0

    source: str = ""
    source_class: str = "unknown"
    warnings: list = field(default_factory=list)


# ============================================================================
# Aggregation helpers: catchment-level and admin-unit-level raw indicators
# ============================================================================

def _raw_indicators_for_businesses(
    business_qs, area_km2: Optional[float], price_level_observations: Optional[Sequence[PriceLevelObservation]],
) -> tuple[RawIndicators, int, int]:
    """Shared aggregation over any queryset of operational BusinessEntity
    rows -- used identically whether the scope is one catchment or a
    whole admin unit, so admin-level narrowing reuses the exact same
    math as catchment-level, not a parallel implementation."""
    categories = list(business_qs.values_list("category", flat=True))
    disc_pct, nec_pct, cat_n = category_mix_shares(categories)

    agg = business_qs.aggregate(avg_rating=Avg("current_rating"), avg_reviews=Avg("current_review_count"), n=Count("id"))
    business_density = (agg["n"] / area_km2) if (area_km2 and agg["n"]) else None

    observations = price_level_observations or []
    price_tier_mean, price_n = price_tier_composite(observations)

    raw = RawIndicators(
        price_tier_mean=price_tier_mean,
        discretionary_share_pct=disc_pct,
        business_density_per_km2=business_density,
        avg_rating=float(agg["avg_rating"]) if agg["avg_rating"] is not None else None,
        avg_review_count=float(agg["avg_reviews"]) if agg["avg_reviews"] is not None else None,
    )
    return raw, cat_n, price_n


def _fit_pca_model_from_corpus() -> Optional[PCAWealthIndexModel]:
    """
    Live-fit fallback across every Catchment currently in the DB with a
    usable indicator set (see module docstring: caching a periodically
    refit model is real separate infra work, intentionally not built
    here). Returns None if fewer than MIN_PCA_FIT_SAMPLE usable rows
    exist -- callers must fall back to simple_standardized_mean_index.
    """
    rows = []
    for catchment in Catchment.objects.select_related("location_cell").all():
        cell = catchment.location_cell
        area_km2 = getattr(catchment, "area_km2", None)
        qs = BusinessEntity.objects.filter(location_cell=cell, current_status="operational")
        if not qs.exists():
            continue
        raw, cat_n, _ = _raw_indicators_for_businesses(qs, area_km2, price_level_observations=None)
        vec = raw.as_vector()
        # Only rows with every indicator present go into the FIT corpus
        # (imputation is fine for scoring a new point, not for fitting
        # the loadings themselves -- imputed fit rows would bias the
        # correlation matrix toward 0).
        if all(v is not None for v in vec.values()):
            rows.append([vec[name] for name in INDICATOR_NAMES])

    if len(rows) < MIN_PCA_FIT_SAMPLE:
        return None
    return PCAWealthIndexModel.fit(np.array(rows), INDICATOR_NAMES)


def _corpus_means_stds() -> tuple[dict, dict]:
    """Same corpus as _fit_pca_model_from_corpus, but returns raw
    per-indicator mean/std for the small-sample fallback path (usable
    even when there isn't enough data to fit PCA itself)."""
    rows = []
    for catchment in Catchment.objects.select_related("location_cell").all():
        cell = catchment.location_cell
        area_km2 = getattr(catchment, "area_km2", None)
        qs = BusinessEntity.objects.filter(location_cell=cell, current_status="operational")
        if not qs.exists():
            continue
        raw, _, _ = _raw_indicators_for_businesses(qs, area_km2, price_level_observations=None)
        rows.append(raw.as_vector())

    means, stds = {}, {}
    for name in INDICATOR_NAMES:
        vals = [r[name] for r in rows if r[name] is not None]
        if len(vals) >= 2:
            means[name] = float(np.mean(vals))
            stds[name] = float(np.std(vals, ddof=1))
    return means, stds


# ============================================================================
# Administrative-unit narrowing
# ============================================================================

def _businesses_for_admin_unit(county: str, subcounty: Optional[str] = None):
    """Operational BusinessEntity rows across every BusinessLocationCell
    tagged with this county (and, if given, this subcounty) --
    aggregation up the SAME real KNBS hierarchy population.py resolves
    coordinates against, not a separate ad hoc geography."""
    cell_filter = {"county": county}
    if subcounty:
        cell_filter["subcounty"] = subcounty
    cell_ids = BusinessLocationCell.objects.filter(**cell_filter).values_list("id", flat=True)
    return BusinessEntity.objects.filter(location_cell_id__in=cell_ids, current_status="operational")


def compute_purchasing_power_for_admin_unit(
    county: str,
    prior_provider: CountyEconomicPriorProvider,
    subcounty: Optional[str] = None,
    pca_model: Optional[PCAWealthIndexModel] = None,
    price_level_observations: Optional[Sequence[PriceLevelObservation]] = None,
) -> PurchasingPowerFeatures:
    """
    Direct entry point for a caller who wants purchasing power scoped to
    a whole county or sub-county rather than one catchment -- "narrow
    down to administrative unit" per product requirement. Reuses every
    piece of math compute_purchasing_power uses per-catchment; the only
    difference is which BusinessEntity rows get aggregated.
    """
    qs = _businesses_for_admin_unit(county, subcounty)
    raw, cat_n, price_n = _raw_indicators_for_businesses(qs, area_km2=None, price_level_observations=price_level_observations)
    return _compute_from_raw(
        raw, sample_size=max(cat_n, price_n), price_tier_sample_size=price_n, category_mix_sample_size=cat_n,
        county=county, prior_provider=prior_provider, pca_model=pca_model,
        resolved_admin_level="subcounty" if subcounty else "county",
        resolved_admin_name=subcounty or county, catchment=None,
    )


def _compute_from_raw(
    raw: RawIndicators,
    sample_size: int,
    price_tier_sample_size: int,
    category_mix_sample_size: int,
    county: str,
    prior_provider: CountyEconomicPriorProvider,
    pca_model: Optional[PCAWealthIndexModel],
    resolved_admin_level: str,
    resolved_admin_name: str,
    catchment: Optional[Catchment],
) -> PurchasingPowerFeatures:
    features = PurchasingPowerFeatures(catchment=catchment)
    features.price_tier_mean = raw.price_tier_mean
    features.price_tier_sample_size = price_tier_sample_size
    features.discretionary_share_pct = raw.discretionary_share_pct
    features.necessity_share_pct = (
        100.0 - raw.discretionary_share_pct if raw.discretionary_share_pct is not None else None
    )
    features.business_density_per_km2 = raw.business_density_per_km2
    features.avg_rating = raw.avg_rating
    features.avg_review_count = raw.avg_review_count
    features.category_mix_sample_size = category_mix_sample_size
    features.direct_estimate_sample_size = sample_size
    features.resolved_admin_level = resolved_admin_level
    features.resolved_admin_name = resolved_admin_name

    vec = raw.as_vector()
    if pca_model is not None:
        features.wealth_index_pca, imputed_warnings = pca_model.score(vec)
        features.wealth_index_method = "pca_first_component"
        features.pca_explained_variance_ratio = pca_model.explained_variance_ratio
        features.indicators_imputed = imputed_warnings
        features.warnings.extend(imputed_warnings)
    else:
        corpus_means, corpus_stds = _corpus_means_stds()
        features.wealth_index_pca, fb_warnings = simple_standardized_mean_index(vec, corpus_means, corpus_stds)
        features.wealth_index_method = "standardized_mean_fallback"
        features.warnings.extend(fb_warnings)

    if features.wealth_index_pca is None:
        features.warnings.append(
            f"No wealth index could be computed at {resolved_admin_level} level "
            f"({resolved_admin_name!r}); zero usable indicators."
        )

    prior = prior_provider.get_prior(county, resolved_admin_name if resolved_admin_level == "subcounty" else None)
    if prior is not None:
        features.county_prior_index = prior.index_0_100
        features.county_prior_source = prior.source
        features.county_prior_source_class = prior.source_class
        if prior.source_class == "synthetic":
            features.warnings.append(
                "County economic prior came from SyntheticCountyEconomicPriorProvider -- "
                "NOT real KIHBS/GCP data. Do not use in a production report."
            )
    else:
        features.warnings.append(f"No county economic prior available for county={county!r}.")

    # Between-area variance: estimated from whatever other admin units'
    # direct estimates are already available this run would be ideal,
    # but a single-unit call has no "other areas" to compare against --
    # use the documented conservative default (see empirical_bayes_shrink's
    # indicator_std_estimate) rather than fabricating a between-area
    # sample of size 1. run() (multi-catchment orchestration) computes
    # A from the real batch of catchments it processes instead -- see there.
    features.composite_purchasing_power_index, features.empirical_bayes_gamma = empirical_bayes_shrink(
        features.wealth_index_pca, features.direct_estimate_sample_size, features.county_prior_index,
        between_area_variance_A=0.0,  # forces gamma toward the prior for a single out-of-context unit; run() overrides with a real A
    )

    features.source = "business_intel.engines.purchasing_power"
    features.source_class = "synthetic" if features.county_prior_source_class == "synthetic" else "internal_derived"
    return features


# ============================================================================
# Django-facing computation for one catchment (with automatic admin fallback)
# ============================================================================

def compute_purchasing_power(
    catchment: Catchment,
    admin_hierarchy: AdministrativeHierarchyProvider,
    prior_provider: CountyEconomicPriorProvider,
    price_level_observations: Optional[Sequence[PriceLevelObservation]] = None,
    pca_model: Optional[PCAWealthIndexModel] = None,
    min_direct_sample_size: int = MIN_DIRECT_SAMPLE_SIZE,
) -> PurchasingPowerFeatures:
    """
    Full Section-3 calculation for one catchment, with automatic
    hierarchical widening (catchment -> subcounty -> county) when the
    catchment's own business sample is too thin -- same "finest
    available, not silently substituted" rule
    resolve_population_source_unit() applies, just keyed on sample-size
    adequacy rather than presence of a published figure.
    """
    cell = catchment.location_cell
    qs = BusinessEntity.objects.filter(location_cell=cell, current_status="operational")
    area_km2 = getattr(catchment, "area_km2", None)
    raw, cat_n, price_n = _raw_indicators_for_businesses(qs, area_km2, price_level_observations)
    sample_size = max(cat_n, price_n)

    county = getattr(cell, "county", "") or ""
    subcounty = getattr(cell, "subcounty", "") or None

    if sample_size >= min_direct_sample_size or not county:
        features = _compute_from_raw(
            raw, sample_size, price_n, cat_n, county, prior_provider, pca_model,
            resolved_admin_level="catchment", resolved_admin_name=cell.geohash, catchment=catchment,
        )
        if sample_size < min_direct_sample_size:
            features.warnings.append(
                f"Catchment sample size ({sample_size}) below MIN_DIRECT_SAMPLE_SIZE "
                f"({min_direct_sample_size}) and no county on record to widen to; "
                "reporting catchment-level estimate anyway, low confidence."
            )
        return features

    # Widen: subcounty first, then county -- real hierarchy fallback,
    # not silent substitution (each widening step is recorded).
    for level, name in (("subcounty", subcounty), ("county", None)):
        if level == "subcounty" and not subcounty:
            continue
        wider_qs = _businesses_for_admin_unit(county, subcounty if level == "subcounty" else None)
        wider_n = wider_qs.count()
        if wider_n >= min_direct_sample_size:
            wider_raw, wider_cat_n, wider_price_n = _raw_indicators_for_businesses(
                wider_qs, area_km2=None, price_level_observations=price_level_observations,
            )
            features = _compute_from_raw(
                wider_raw, wider_n, wider_price_n, wider_cat_n, county, prior_provider, pca_model,
                resolved_admin_level=level, resolved_admin_name=(subcounty if level == "subcounty" else county),
                catchment=catchment,
            )
            features.warnings.append(
                f"Catchment-level sample ({sample_size}) below MIN_DIRECT_SAMPLE_SIZE "
                f"({min_direct_sample_size}); widened to {level}={features.resolved_admin_name!r} "
                f"(n={wider_n}) per hierarchical fallback."
            )
            return features

    # Nothing at any level meets the floor -- report the thin
    # catchment-level estimate anyway, clearly flagged, rather than
    # returning nothing.
    features = _compute_from_raw(
        raw, sample_size, price_n, cat_n, county, prior_provider, pca_model,
        resolved_admin_level="catchment", resolved_admin_name=cell.geohash, catchment=catchment,
    )
    features.warnings.append(
        f"No level (catchment={sample_size}, subcounty/county) met MIN_DIRECT_SAMPLE_SIZE "
        f"({min_direct_sample_size}); reporting thinnest available estimate, low confidence."
    )
    return features


# ============================================================================
# Orchestration / EngineRun integration
# ============================================================================

METRIC_FIELDS = [
    ("price_tier_mean", "ordinal_0_4", "numeric"),
    ("discretionary_share_pct", "%", "numeric"),
    ("necessity_share_pct", "%", "numeric"),
    ("business_density_per_km2", "businesses/km2", "numeric"),
    ("avg_rating", "stars_0_5", "numeric"),
    ("avg_review_count", "count", "numeric"),
    ("wealth_index_pca", "index_0_100", "numeric"),
    ("pca_explained_variance_ratio", "ratio_0_1", "numeric"),
    ("county_prior_index", "index_0_100", "numeric"),
    ("between_area_variance_A", "index_points_squared", "numeric"),
    ("composite_purchasing_power_index", "index_0_100", "numeric"),
    ("empirical_bayes_gamma", "ratio_0_1", "numeric"),
]


def run(
    query: BusinessQuery,
    admin_hierarchy: AdministrativeHierarchyProvider,
    prior_provider: CountyEconomicPriorProvider,
    catchments: Optional[Sequence[Catchment]] = None,
    price_level_observations_by_catchment: Optional[Mapping[int, Sequence[PriceLevelObservation]]] = None,
) -> dict:
    """
    Engine 3 entry point. Fits (or reuses, if the corpus is large
    enough) the PCA wealth-index model, computes purchasing-power
    features for every Catchment belonging to query.location_cell,
    estimates a real between-area variance A across THIS run's
    catchments for Empirical Bayes shrinkage, writes one
    FeatureObservation per metric per catchment, and records an
    EngineRun row -- same contract shape as population.py's run().

    admin_hierarchy and prior_provider are both required explicitly (no
    default) so a synthetic provider is never used in production by
    omission -- same reasoning population.py applies to its own
    admin_hierarchy argument.
    """
    started_at = timezone.now()
    cell = query.location_cell
    target_catchments = list(catchments) if catchments is not None else list(cell.catchments.all())
    price_level_observations_by_catchment = price_level_observations_by_catchment or {}

    run_row = EngineRun.objects.create(
        query=query, engine_number=ENGINE_NUMBER, status="succeeded", started_at=started_at,
    )

    if not target_catchments:
        run_row.status = "failed"
        run_row.error_message = "No Catchment rows found for this cell. Engine 1 (Spatial Context) must run first."
        run_row.finished_at = timezone.now()
        run_row.save(update_fields=["status", "error_message", "finished_at"])
        return {"cell": cell, "features": [], "engine_run": run_row}

    pca_model = _fit_pca_model_from_corpus()  # None -> per-catchment fallback to standardized-mean index

    features_written = 0
    warnings_seen = []
    all_features = []

    try:
        with transaction.atomic():
            per_catchment = [
                compute_purchasing_power(
                    c, admin_hierarchy, prior_provider,
                    price_level_observations=price_level_observations_by_catchment.get(c.id),
                    pca_model=pca_model,
                )
                for c in target_catchments
            ]

            # Real between-area variance across THIS run's catchments
            # (method-of-moments, see estimate_between_area_variance) --
            # then re-shrink each catchment's composite with that real
            # A instead of the single-unit A=0.0 fallback
            # _compute_from_raw uses when it has no siblings to compare
            # against.
            direct_vals = [f.wealth_index_pca for f in per_catchment if f.wealth_index_pca is not None]
            A_hat = estimate_between_area_variance(
                direct_vals, sampling_variances=[15.0 ** 2 / max(f.direct_estimate_sample_size, 1) for f in per_catchment if f.wealth_index_pca is not None],
            ) if len(direct_vals) >= 2 else 0.0

            for catch_features in per_catchment:
                catch_features.between_area_variance_A = A_hat
                catch_features.composite_purchasing_power_index, catch_features.empirical_bayes_gamma = empirical_bayes_shrink(
                    catch_features.wealth_index_pca, catch_features.direct_estimate_sample_size,
                    catch_features.county_prior_index, between_area_variance_A=A_hat,
                )
                all_features.append(catch_features)
                warnings_seen.extend(catch_features.warnings)

                for field_name, unit, value_type in METRIC_FIELDS:
                    value = getattr(catch_features, field_name)
                    if value is None:
                        continue
                    FeatureObservation.write_latest(
                        location_cell=cell, catchment=catch_features.catchment,
                        engine_number=ENGINE_NUMBER, feature_name=field_name,
                        defaults=dict(
                            value_type=value_type,
                            value_numeric=Decimal(str(round(value, 6))),
                            unit=unit,
                            observed_at=started_at,
                            source=catch_features.source or "business_intel.engines.purchasing_power",
                            source_class=catch_features.source_class,
                            method=(
                                catch_features.wealth_index_method if field_name == "wealth_index_pca"
                                else "empirical_bayes_method_of_moments" if field_name == "composite_purchasing_power_index"
                                else "purchasing_power_engine_calculation"
                            ),
                            derivation_level=(
                                "model_estimate" if field_name in ("wealth_index_pca", "composite_purchasing_power_index")
                                else "calculation"
                            ),
                            sample_size=catch_features.direct_estimate_sample_size or None,
                        ),
                    )
                    features_written += 1

                if catch_features.resolved_admin_level:
                    FeatureObservation.write_latest(
                        location_cell=cell, catchment=catch_features.catchment,
                        engine_number=ENGINE_NUMBER, feature_name="resolved_admin_level",
                        defaults=dict(
                            value_type="categorical",
                            value_text=f"{catch_features.resolved_admin_level}:{catch_features.resolved_admin_name}"[:500],
                            unit="", observed_at=started_at,
                            source=catch_features.source, source_class=catch_features.source_class,
                            method="hierarchical_sample_size_fallback", derivation_level="observation",
                        ),
                    )
                    features_written += 1

    except Exception as exc:
        logger.exception("Engine %s failed for query %s", ENGINE_NUMBER, query.id)
        run_row.status = "failed"
        run_row.error_message = str(exc)
        run_row.finished_at = timezone.now()
        run_row.save(update_fields=["status", "error_message", "finished_at"])
        raise

    finished_at = timezone.now()
    run_row.finished_at = finished_at
    run_row.duration_ms = int((finished_at - started_at).total_seconds() * 1000)
    run_row.features_written = features_written
    if warnings_seen:
        run_row.status = "degraded"
        run_row.degraded_reason = "; ".join(sorted(set(warnings_seen)))[:255]
    if isinstance(prior_provider, SyntheticCountyEconomicPriorProvider) or isinstance(admin_hierarchy, SyntheticAdminHierarchyProvider):
        run_row.status = "degraded"
        run_row.degraded_reason = "Synthetic provider in use (prior and/or admin hierarchy) -- not real production data."[:255]
    run_row.save(update_fields=["status", "finished_at", "duration_ms", "features_written", "degraded_reason"])

    return {"cell": cell, "features": all_features, "engine_run": run_row, "pca_model_used": pca_model is not None}