"""
business_intel/engines/property_cost.py

Engine 15 — Property & Cost Environment Engine
Per business_location_intelligence_engines_data_models_v2.md, Section 15.

NOTE ON THE SPEC FILE: same situation as every other engine in this
codebase -- business_location_intelligence_engines_data_models_v2.md is
referenced but not present on disk. Built to match Engine 12/13/14's
structure, schema contract, and citation discipline (all read in full
before writing this module).

=============================================================================
PURPOSE, DATA SOURCE, AND SCOPE BOUNDARY
=============================================================================
"Measure the actual cost and pricing environment of a location." The one
real, already-migrated table built for this: models.PropertyListing --
scraped/observed sale and rent listings, geolocated, with price_kes /
monthly_rent_kes / floor_area_sqm / listed_at / delisted_at / is_active,
explicitly kept historical ("Historical listings are kept for
price-growth calculations" -- PropertyListing's own docstring) so this
is the one engine in this codebase whose primary table was purpose-built
for a time-series question, not just a snapshot one.

PropertyListing.PROPERTY_TYPE_CHOICES = residential / commercial / land /
mixed_use. This engine treats commercial + mixed_use as the PRIMARY
market (COMMERCIAL_MARKET_TYPES below) -- a business evaluating a site
looks at commercial-zoned space and at ground-floor-commercial units in
mixed-use buildings, which is a real, common leasing/purchase pattern in
Kenyan urban markets, not an arbitrary merge. residential and land are
priced in fundamentally different markets (a per-sqm land price is not
comparable to a per-sqm built-commercial-space price, and residential
rents answer a different question than commercial rents) -- both are
read and reported as CONTEXT_PROPERTY_TYPES, kept out of the primary
price/growth/percentile computation so their different scale cannot
silently distort it, same "don't average incomparable markets together"
discipline commercial_occupancy.py and infrastructure_change.py already
apply to their own scope boundaries.

Sale and rent are modeled entirely separately throughout (different
markets, different units, different buyers) -- everywhere below that
says "the market," read it as "sale market, computed the same way as
rent market, kept in separate result objects."

=============================================================================
THE REAL STATISTICAL / ML PROBLEM, AND WHY EACH PIECE IS THE METHOD USED
=============================================================================
Four genuinely separate problems, each handled with a named, real,
citable method -- no single invented formula stands in for all four.

1. WHAT DOES THIS EXACT POINT COST, GIVEN COMPS SCATTERED AROUND IT AT
   VARYING DISTANCES? -- This is the appraisal industry's own "sales
   comparison approach" (weight comparable transactions by relevance,
   here operationalized as distance) turned into an actual estimator
   rather than an analyst's manual judgment call: Nadaraya-Watson kernel
   regression --
     Nadaraya, E. A. (1964). "On Estimating Regression." Theory of
     Probability & Its Applications, 9(1), 141-142.
     Watson, G. S. (1964). "Smooth Regression Analysis." Sankhya: The
     Indian Journal of Statistics, Series A, 26(4), 359-372.
   -- a Gaussian-kernel-weighted local average of comp prices, evaluated
   AT the query point (distance = 0), i.e. exactly the Hansen-style
   "closer comps count more" logic development_activity.py /
   infrastructure_change.py already use for their own spatial weighting,
   here applied to price estimation specifically rather than an
   accessibility score.

2. HOW WIDE SHOULD THAT KERNEL BE? -- Rather than a fixed, guessed
   distance constant (which Engine 13 uses and explicitly flags as an
   engineering choice for ITS problem), this module selects the
   bandwidth data-drivenly per computation via leave-one-out
   cross-validation over a small candidate grid, minimizing
   out-of-sample squared prediction error -- the standard bandwidth-
   selection method for kernel regression:
     Hardle, W. (1990). Applied Nonparametric Regression. Cambridge
     University Press. (Ch. 3-5, kernel bandwidth selection via CV.)
     Hastie, T., Tibshirani, R., & Friedman, J. (2009). The Elements of
     Statistical Learning (2nd ed.). Springer. Ch. 6, "Kernel Smoothing
     Methods" -- CV-selected bandwidth for local regression.
   Real estate comp density varies enormously by area (dense CBD vs.
   sparse peri-urban), so a single fixed radius is a worse fit here than
   for Engine 13's infrastructure-accessibility problem -- CV lets the
   effective "how far counts as nearby" adapt per query instead of
   guessing one number for all of Kenya. Falls back to the median
   candidate (undisguised, flagged not-cross-validated) when fewer than
   4 comps exist, since LOOCV is not meaningfully defined below that.

3. IS THE MARKET HERE GETTING MORE OR LESS EXPENSIVE OVER TIME? -- The
   textbook-correct answer is a repeat-sales index (the same physical
   unit, sold/listed twice, price change isolated from quality mix):
     Bailey, M. J., Muth, R. F., & Nourse, H. O. (1963). "A Regression
     Method for Real Estate Price Index Construction." Journal of the
     American Statistical Association, 58(304), 933-942.
     Case, K. E., & Shiller, R. J. (1989). "The Efficiency of the Market
     for Single-Family Homes." American Economic Review, 79(1), 125-137.
   PropertyListing rows are independent scraped listings, not matched
   same-unit pairs (there is no cross-platform property identity key in
   this schema -- see "WHAT'S NOT AVAILABLE" below), so a true
   repeat-sales index cannot be built from this table. What IS computed
   instead, and reported as exactly what it is rather than dressed up as
   a repeat-sales index: a weighted-least-squares regression of
   log(price_per_sqm) on listing month, weight = the same spatial kernel
   weight as point 1, across ALL distinct listings in the window (not
   matched pairs) -- a cruder, hedonic-style time trend, same
   "log-linear is the standard hedonic functional form because housing
   prices are right-skewed" convention documented in:
     Malpezzi, S. (2003). "Hedonic Pricing Models: A Selected and
     Applied Review." In O'Sullivan, T. & Gibb, K. (eds.), Housing
     Economics and Public Policy. Blackwell.
   Standard errors, t-statistic, and a two-sided p-value come from
   ordinary weighted-least-squares theory:
     Draper, N. R., & Smith, H. (1998). Applied Regression Analysis (3rd
     ed.). Wiley. (WLS standard errors.)
   flagged unreliable below MIN_COMPS_FOR_TREND, exactly like Engine
   13's normal-approximation-reliability flag.

4. HOW UNCERTAIN IS ANY OF THIS? -- Real estate listing data is thin and
   noisy per location; a point estimate with no interval overstates
   confidence. The nonparametric bootstrap gives an honest interval
   without assuming a parametric price distribution:
     Efron, B. (1979). "Bootstrap Methods: Another Look at the
     Jackknife." Annals of Statistics, 7(1), 1-26.
   applied by resampling comps with replacement and recomputing the
   weighted-median estimate each draw -- flagged unreliable (returns
   None, None) below MIN_COMPS_FOR_BOOTSTRAP, same "don't report a false
   precision" discipline as every CI in this codebase.

DATA HYGIENE, NOT A FIFTH METHOD BUT LOAD-BEARING: scraped listing data
routinely contains entry errors (a price typed with an extra zero, a
floor area in the wrong unit). Before any of the above runs, log(price)
is screened with Tukey's outlier fences:
     Tukey, J. W. (1977). Exploratory Data Analysis. Addison-Wesley.
     (1.5x-IQR fences, Ch. 2.)
in log-space specifically because real-estate prices are right-skewed
(same Malpezzi 2003 log-linear justification as point 3) -- a fence
computed on raw KES values would be dominated by the skew itself rather
than catching genuine data errors. Comps outside the fence are EXCLUDED
from the price/trend computation and separately counted and reported
(n_outliers_excluded), never silently dropped without a trace.

=============================================================================
THE POINT ESTIMATE ITSELF: NW MEAN AND WEIGHTED MEDIAN, BOTH REPORTED
=============================================================================
The Nadaraya-Watson estimate (point 1) is a weighted MEAN in log-space,
exponentiated back -- efficient, but like any mean it can still be
pulled by whatever survives the outlier fence. A weighted MEDIAN (same
kernel weights, standard weighted-median definition: the value at which
cumulative weight first reaches half the total weight) is computed
alongside it as a robustness check, same "report the robust statistic
next to the efficient one, let a reader see if they disagree" discipline
this codebase already applies elsewhere (e.g. reporting both a raw
proportion and its Wilson interval rather than only one). The weighted
median, not the NW mean, is what feeds the bootstrap CI and the
affordability ratios below, since it is the more defensible point
estimate under known data-quality risk; the NW mean is reported in
evidence for comparison, not used downstream.

=============================================================================
FRAMING THE 0-100 FIELD: A PERCENTILE, NOT A SATURATING TRANSFORM
=============================================================================
Every other engine's 0-100 score in this codebase is an explicitly
uncalibrated monotonic saturating transform of a raw magnitude in that
engine's own units (see infrastructure_change.py's SCORE_SATURATION_SCALE
discussion). That pattern does not fit cleanly here: price-per-sqm scale
varies by roughly two orders of magnitude across property types and
markets in this table, so a single fixed saturation constant would be
substantially more arbitrary here than Engine 13's -- and this module has
a better-founded alternative available. cost_level_percentile_0_100 is
instead the empirical percentile rank (scipy.stats.percentileofscore) of
this location's weighted-median point estimate against the pooled RAW
(unweighted, post-outlier-screen) comp values found within the search
radius -- i.e. "does this exact point sit at the expensive or cheap end
of its OWN immediate comparable set." This is a plain nonparametric
statistic, not a model, so it needs no citation beyond the definition
itself, and it is honest about what it does and doesn't claim: it says
nothing about how this location compares to a different part of Kenya
entirely, only about where it sits within the comps that were actually
found nearby it. Purely descriptive (higher = pricier locally), not a
judgment of good or bad, same non-evaluative framing development_activity.py
and infrastructure_change.py already apply to their own 0-100 fields.

=============================================================================
AFFORDABILITY AGAINST THE QUERY'S OWN STATED BUDGET
=============================================================================
BusinessQuery.investment_amount_kes is optional and, when present, is
used for exactly two raw ratios -- no invented affordability threshold,
no "good/bad" cutoff, since no calibrated Kenya-specific figure for what
ratio is "affordable" exists for this system to cite:
  affordability_ratio_sale = investment_amount_kes / weighted_median_sale_price_kes
  months_of_rent_covered   = investment_amount_kes / weighted_median_monthly_rent_kes
Both computed on the raw listing-total (price_kes / monthly_rent_kes)
distribution, not the per-sqm one, since a budget check is naturally
against what a comparable UNIT actually costs, not a size-normalized
rate. Reported as plain numbers for the reader (or a downstream Decision
engine) to interpret, not as a score.

=============================================================================
WHAT'S ACTUALLY AVAILABLE VS. WHAT ISN'T
=============================================================================
Real data used: business_intel.models.PropertyListing rows already in
this system's database (populated by whatever scraping/ETL pipeline
feeds that table -- out of scope for this engine, which only reads it).
No live network call is made by this engine -- like Engine 13, it is a
query over already-migrated internal data, so the only staleness
question is the StaleAwareModel window each PropertyListing already
carries via its own source_class/last_refreshed_at, read and flagged
(not silently trusted) per comp, same as Engine 13's project staleness
handling.

What is explicitly NOT available and is flagged, not faked:
  - No cross-listing property identity. Two rows could be the same
    physical unit re-listed on a different platform (or re-listed after
    a price change) with no key linking them -- a true repeat-sales
    index (point 3 above) cannot be built from this schema as it stands.
    Documented, not implemented, extension: adding a normalized
    property-identity key (address + floor_area fuzzy match, or a
    manually curated dedup table) would be genuine separate work, the
    same status county-level economic priors carry in economic_momentum.py.
  - No condition, age, amenity, or finish-quality data per listing --
    the hedonic literature this module cites (Rosen 1974's foundational
    "Hedonic Prices and Implicit Markets," J. Political Economy 82(1),
    34-55) decomposes price into the value of individual characteristics;
    this table has essentially none of those beyond property_type and
    floor_area_sqm, so no multi-characteristic hedonic regression is
    attempted here -- only property_type-stratified (via the
    COMMERCIAL_MARKET_TYPES scope boundary) and space-stratified (via
    the kernel) estimation. Flagged, not silently assumed uniform.
  - No independent verification that a given listing's price is a real
    market-clearing figure vs. an aspirational asking price (especially
    for "for sale" listings, which are commonly listed above eventual
    transaction price) -- this module uses listing prices as-is because
    no transaction-price data exists in this schema; a reader should
    treat sale-side estimates as an asking-price ceiling proxy, not a
    transaction-price estimate. Explicitly reported as a warning.

KNOWN UPSTREAM GAP (flagged like every other engine flags gaps it
depends on but doesn't own): tasks.py's generate_business_report does
not currently call Engine 15 at all, and BusinessReport has no
property_cost-related field -- same "wiring is a separate, out-of-scope
task" gap development_activity.py's and infrastructure_change.py's own
module docstrings already document for themselves.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
from scipy import stats

logger = logging.getLogger("business_intel")

# =============================================================================
# Constants
# =============================================================================

ENGINE_NUMBER = 15

# See module docstring's "PURPOSE, DATA SOURCE, AND SCOPE BOUNDARY".
COMMERCIAL_MARKET_TYPES = ("commercial", "mixed_use")
CONTEXT_PROPERTY_TYPES = ("residential", "land")
ALL_PROPERTY_TYPES = COMMERCIAL_MARKET_TYPES + CONTEXT_PROPERTY_TYPES

LISTING_TYPES = ("sale", "rent")

# Matches DISTANCE_BAND_METERS' widest band -- same "don't invent a new
# spatial constant when a codebase one already exists at this scale"
# convention as development_activity.py / infrastructure_change.py.
SEARCH_RADIUS_M = 10_000

# Candidate grid for LOOCV bandwidth selection (module docstring point 2).
# Spans "tight CBD block" to "whole search radius" so CV genuinely has a
# meaningful choice to make rather than picking among near-identical values.
BANDWIDTH_CANDIDATES_M = (250.0, 500.0, 1000.0, 1500.0, 2500.0, 4000.0, 6000.0, 10000.0)

# Below this many comps, LOOCV bandwidth selection is not meaningfully
# defined (per Hardle 1990's own applicability range) -- falls back to
# the middle candidate in the sorted grid, flagged as not-cross-validated.
MIN_COMPS_FOR_LOOCV = 4

# Below this many comps, the bootstrap CI (module docstring point 4) is
# not attempted -- returns (None, None) rather than a falsely precise
# interval from too few resamples' worth of real variation.
MIN_COMPS_FOR_BOOTSTRAP = 5
N_BOOTSTRAP_RESAMPLES = 2000
BOOTSTRAP_ALPHA = 0.05

# Below this many time-distinct comps, the WLS growth-trend regression
# (module docstring point 3) is computed but flagged unreliable -- same
# role as infrastructure_change.py's MIN_PROJECTS_FOR_NORMAL_APPROX.
MIN_COMPS_FOR_TREND = 8

# How far back listing history is pulled for the growth-trend regression.
# Real estate "market regimes" shift (interest rates, macro conditions);
# a much longer window risks mixing genuinely different regimes into one
# slope, a much shorter one starves the regression of signal. Engineering
# choice, documented not measured -- same status as infrastructure_change.py's
# COMPLETION_PCT_BLEND_WEIGHT.
TREND_LOOKBACK_MONTHS = 36

# Tukey (1977) fence multiplier -- the standard 1.5x-IQR convention,
# applied in log-space (module docstring's "DATA HYGIENE" section).
TUKEY_FENCE_K = 1.5

# Below this many raw values, Tukey fencing is skipped (IQR is not a
# meaningful outlier signal at n<4) -- all comps kept, per
# tukey_outlier_mask's own guard.
MIN_N_FOR_TUKEY = 4

FEATURE_MEDIAN_PRICE_PER_SQM_SALE = "median_price_per_sqm_sale_kes"
FEATURE_MEDIAN_RENT_PER_SQM = "median_rent_per_sqm_kes"
FEATURE_SALE_GROWTH_ANNUAL_PCT = "sale_price_growth_annual_pct"
FEATURE_RENT_GROWTH_ANNUAL_PCT = "rent_growth_annual_pct"
FEATURE_COST_PERCENTILE_SALE = "cost_level_percentile_sale_0_100"
FEATURE_COST_PERCENTILE_RENT = "cost_level_percentile_rent_0_100"

_RNG_SEED = 42  # fixed seed -> reproducible bootstrap CIs across re-runs of the same input set


# =============================================================================
# Pure data structures -- no Django/network import at module scope, so this
# file's math is unit-testable without a DB, matching Engine 12/13's
# "pure-math layer above without a DB" split.
# =============================================================================

@dataclass
class PropertyComp:
    """One PropertyListing, already resolved to a straight-line distance
    (and retaining its own lat/lng for pairwise kernel-bandwidth
    selection -- see module docstring point 2), ready for pure-math
    scoring."""
    listing_id: str
    property_type: str
    listing_type: str  # "sale" | "rent"
    latitude: float
    longitude: float
    distance_m: float
    price_kes: Optional[float]
    monthly_rent_kes: Optional[float]
    floor_area_sqm: Optional[float]
    listed_at: date
    delisted_at: Optional[date]
    is_active: bool
    is_stale: bool = False

    @property
    def price_per_sqm(self) -> Optional[float]:
        if self.price_kes is not None and self.floor_area_sqm and self.floor_area_sqm > 0:
            return self.price_kes / self.floor_area_sqm
        return None

    @property
    def rent_per_sqm(self) -> Optional[float]:
        if self.monthly_rent_kes is not None and self.floor_area_sqm and self.floor_area_sqm > 0:
            return self.monthly_rent_kes / self.floor_area_sqm
        return None


@dataclass
class MarketEstimate:
    """Full result for one listing_type (sale or rent) within the
    primary commercial+mixed_use market."""
    listing_type: str
    n_comps_total: int
    n_comps_priced_per_sqm: int
    n_outliers_excluded: int
    n_stale: int
    bandwidth_m: Optional[float]
    bandwidth_cross_validated: bool
    nw_mean_per_sqm_kes: Optional[float]
    weighted_median_per_sqm_kes: Optional[float]
    weighted_median_ci_low: Optional[float]
    weighted_median_ci_high: Optional[float]
    cost_level_percentile_0_100: Optional[float]
    growth_annual_pct: Optional[float]
    growth_annual_pct_se: Optional[float]
    growth_p_value: Optional[float]
    growth_n_comps: int
    growth_reliable: bool
    weighted_median_total_kes: Optional[float]
    nearest_comp: Optional[PropertyComp]
    warnings: List[str] = field(default_factory=list)


@dataclass
class ContextMarketSummary:
    """Lightweight, non-primary summary for residential/land comps --
    reported for color, never fed into the primary score. See module
    docstring's scope-boundary note."""
    property_type: str
    n_comps: int
    weighted_median_per_sqm_kes: Optional[float]


@dataclass
class PropertyCostResult:
    sale: MarketEstimate
    rent: MarketEstimate
    context: List[ContextMarketSummary]
    affordability_ratio_sale: Optional[float]
    months_of_rent_covered: Optional[float]
    warnings: List[str] = field(default_factory=list)


# =============================================================================
# Real, cited statistical primitives
# =============================================================================

def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Duplicated locally rather than imported -- same 'import for
    plumbing, duplicate for independent statistics' convention
    development_activity.py/infrastructure_change.py already establish
    for this exact function in this codebase."""
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def _pairwise_distance_matrix_m(lat: np.ndarray, lng: np.ndarray) -> np.ndarray:
    """Full pairwise haversine distance matrix between comps -- needed
    for LOOCV bandwidth selection (module docstring point 2), which must
    generalize over the actual spatial field of comps, not merely each
    comp's distance from the query point."""
    n = len(lat)
    D = np.zeros((n, n))
    for i in range(n):
        for j in range(i + 1, n):
            d = _haversine_m(lat[i], lng[i], lat[j], lng[j])
            D[i, j] = D[j, i] = d
    return D


def _gaussian_kernel(d: np.ndarray, h: float) -> np.ndarray:
    h = max(float(h), 1.0)
    return np.exp(-0.5 * (d / h) ** 2)


def _nw_predict_at(target_d: np.ndarray, values: np.ndarray, h: float) -> Optional[float]:
    w = _gaussian_kernel(target_d, h)
    sw = float(w.sum())
    if sw <= 1e-12:
        return None
    return float((w * values).sum() / sw)


def select_bandwidth_loocv(
    D: np.ndarray, values: np.ndarray, candidates: Sequence[float] = BANDWIDTH_CANDIDATES_M
) -> Tuple[float, bool]:
    """Hardle (1990) / Hastie-Tibshirani-Friedman ESL ch.6 leave-one-out
    cross-validated bandwidth selection for Nadaraya-Watson regression --
    see module docstring point 2. Returns (bandwidth_m, cross_validated)."""
    n = len(values)
    sorted_candidates = sorted(candidates)
    fallback_h = float(sorted_candidates[len(sorted_candidates) // 2])
    if n < MIN_COMPS_FOR_LOOCV:
        return fallback_h, False
    best_h, best_mse = None, math.inf
    for h in candidates:
        errs = []
        for i in range(n):
            mask = np.ones(n, dtype=bool)
            mask[i] = False
            pred = _nw_predict_at(D[i, mask], values[mask], h)
            if pred is not None:
                errs.append((pred - values[i]) ** 2)
        if errs:
            mse = float(np.mean(errs))
            if mse < best_mse:
                best_mse, best_h = mse, h
    if best_h is None:
        return fallback_h, False
    return float(best_h), True


def weighted_median(values: np.ndarray, weights: np.ndarray) -> Optional[float]:
    """Standard weighted-median: the value at which cumulative weight
    first reaches half the total weight. Robust point estimate used
    alongside the NW mean -- see module docstring's 'THE POINT ESTIMATE
    ITSELF' section."""
    if len(values) == 0 or float(weights.sum()) <= 1e-12:
        return None
    order = np.argsort(values)
    v, w = values[order], weights[order]
    cw = np.cumsum(w)
    cutoff = 0.5 * float(w.sum())
    idx = int(np.searchsorted(cw, cutoff))
    idx = min(idx, len(v) - 1)
    return float(v[idx])


def tukey_keep_mask(log_values: np.ndarray, k: float = TUKEY_FENCE_K) -> np.ndarray:
    """Tukey (1977) 1.5x-IQR fences in log-space -- see module docstring's
    'DATA HYGIENE' section. Returns a boolean KEEP mask."""
    n = len(log_values)
    if n < MIN_N_FOR_TUKEY:
        return np.ones(n, dtype=bool)
    q1, q3 = np.percentile(log_values, [25, 75])
    iqr = q3 - q1
    if iqr <= 1e-12:
        return np.ones(n, dtype=bool)
    lo, hi = q1 - k * iqr, q3 + k * iqr
    return (log_values >= lo) & (log_values <= hi)


def bootstrap_weighted_median_ci(
    values: np.ndarray,
    weights: np.ndarray,
    n_boot: int = N_BOOTSTRAP_RESAMPLES,
    alpha: float = BOOTSTRAP_ALPHA,
    seed: int = _RNG_SEED,
) -> Tuple[Optional[float], Optional[float]]:
    """Efron (1979) nonparametric bootstrap percentile CI on the
    weighted median -- see module docstring point 4. Returns (low, high),
    or (None, None) below MIN_COMPS_FOR_BOOTSTRAP."""
    n = len(values)
    if n < MIN_COMPS_FOR_BOOTSTRAP:
        return None, None
    rng = np.random.default_rng(seed)
    idx_all = np.arange(n)
    ests: List[float] = []
    for _ in range(n_boot):
        idx = rng.choice(idx_all, size=n, replace=True)
        e = weighted_median(values[idx], weights[idx])
        if e is not None:
            ests.append(e)
    if len(ests) < 10:
        return None, None
    lo = float(np.percentile(ests, 100 * alpha / 2))
    hi = float(np.percentile(ests, 100 * (1 - alpha / 2)))
    return lo, hi


def weighted_least_squares_trend(
    t_months: np.ndarray, log_y: np.ndarray, weights: np.ndarray
) -> Optional[Tuple[float, float, Optional[float], Optional[float], int]]:
    """Weighted least squares slope of log(price) on listing month --
    module docstring point 3. Returns (slope_per_month, se, t_stat,
    p_value, n), or None if underdetermined (n<3, singular design, or
    zero total weight)."""
    n = len(t_months)
    if n < 3 or float(weights.sum()) <= 1e-12:
        return None
    W = np.diag(weights)
    X = np.column_stack([np.ones(n), t_months])
    XtW = X.T @ W
    try:
        beta = np.linalg.solve(XtW @ X, XtW @ log_y)
    except np.linalg.LinAlgError:
        return None
    resid = log_y - X @ beta
    dof = n - 2
    if dof <= 0:
        return None
    sigma2 = float((weights * resid ** 2).sum() / dof)
    try:
        cov = sigma2 * np.linalg.inv(XtW @ X)
    except np.linalg.LinAlgError:
        return None
    se_slope = math.sqrt(max(float(cov[1, 1]), 0.0))
    slope = float(beta[1])
    if se_slope <= 1e-12:
        return slope, se_slope, None, None, n
    t_stat = slope / se_slope
    p_value = float(2 * (1 - stats.t.cdf(abs(t_stat), df=dof)))
    return slope, se_slope, t_stat, p_value, n


def percentile_rank(value: Optional[float], pool: np.ndarray) -> Optional[float]:
    """Plain empirical percentile rank -- see module docstring's
    'FRAMING THE 0-100 FIELD' section. No citation needed beyond the
    definition; scipy.stats.percentileofscore implements it directly."""
    if value is None or len(pool) == 0:
        return None
    return float(stats.percentileofscore(pool, value, kind="mean"))


# =============================================================================
# Assembly: one listing_type's full market estimate
# =============================================================================

def _compute_market_estimate(
    comps: Sequence[PropertyComp],
    listing_type: str,
    query_date: date,
) -> MarketEstimate:
    warnings: List[str] = []
    same_type = [c for c in comps if c.listing_type == listing_type]
    n_stale = sum(1 for c in same_type if c.is_stale)

    # Strictly positive only: a listed price/rent of exactly 0 is a data-entry
    # artifact (PropertyListing's own validator only enforces >= 0, not > 0),
    # and log(0) is undefined -- silently including it would break every
    # log-space computation below (Tukey fence, kernel regression, trend).
    # Excluded here and counted, not silently dropped without a trace.
    if listing_type == "sale":
        per_sqm_pairs = [(c, c.price_per_sqm) for c in same_type if c.price_per_sqm is not None and c.price_per_sqm > 0]
        total_pairs = [(c, float(c.price_kes)) for c in same_type if c.price_kes is not None and c.price_kes > 0]
    else:
        per_sqm_pairs = [(c, c.rent_per_sqm) for c in same_type if c.rent_per_sqm is not None and c.rent_per_sqm > 0]
        total_pairs = [
            (c, float(c.monthly_rent_kes)) for c in same_type
            if c.monthly_rent_kes is not None and c.monthly_rent_kes > 0
        ]
    n_nonpositive = sum(
        1 for c in same_type
        if (listing_type == "sale" and c.price_kes is not None and c.price_kes <= 0)
        or (listing_type == "rent" and c.monthly_rent_kes is not None and c.monthly_rent_kes <= 0)
    )
    if n_nonpositive:
        warnings.append(
            f"{n_nonpositive} {listing_type} comp(s) had a zero or negative price/rent value -- "
            "excluded as data-entry artifacts (log-space statistics are undefined at zero)."
        )

    nearest_comp = min(same_type, key=lambda c: c.distance_m) if same_type else None

    if not per_sqm_pairs:
        msg = (
            f"No {listing_type} comps with both a price and a floor_area_sqm found within "
            f"{SEARCH_RADIUS_M}m -- per-sqm pricing cannot be computed. This may reflect genuinely "
            "thin listing coverage here, or simply that floor_area_sqm is often blank on scraped "
            "listings; treat an undefined estimate as low-confidence, not as evidence of a dead "
            "market, same caveat infrastructure_change.py applies to its own sparse-data case."
        )
        warnings.append(msg)
        return MarketEstimate(
            listing_type=listing_type, n_comps_total=len(same_type), n_comps_priced_per_sqm=0,
            n_outliers_excluded=0, n_stale=n_stale, bandwidth_m=None, bandwidth_cross_validated=False,
            nw_mean_per_sqm_kes=None, weighted_median_per_sqm_kes=None, weighted_median_ci_low=None,
            weighted_median_ci_high=None, cost_level_percentile_0_100=None, growth_annual_pct=None,
            growth_annual_pct_se=None, growth_p_value=None, growth_n_comps=0, growth_reliable=False,
            weighted_median_total_kes=(
                weighted_median(np.array([v for _, v in total_pairs]), np.ones(len(total_pairs)))
                if total_pairs else None
            ),
            nearest_comp=nearest_comp, warnings=warnings,
        )

    comps_arr = [c for c, _ in per_sqm_pairs]
    raw_values = np.array([v for _, v in per_sqm_pairs], dtype=float)
    log_values = np.log(raw_values)

    keep = tukey_keep_mask(log_values)
    n_outliers = int((~keep).sum())
    if n_outliers:
        warnings.append(
            f"{n_outliers} of {len(comps_arr)} {listing_type} comp(s) excluded as log-space Tukey "
            f"(1977) outliers ({TUKEY_FENCE_K}x IQR) -- likely scraping/entry errors (e.g. a price "
            "typo or misrecorded floor area), not real market extremes. Excluded from all downstream "
            "statistics, counted here rather than silently dropped."
        )

    comps_k = [c for c, k in zip(comps_arr, keep) if k]
    values_k = raw_values[keep]
    log_k = log_values[keep]
    lat_k = np.array([c.latitude for c in comps_k])
    lng_k = np.array([c.longitude for c in comps_k])
    dist_from_query_k = np.array([c.distance_m for c in comps_k])

    if len(values_k) == 0:
        warnings.append(f"All {listing_type} comps were excluded by the outlier fence -- no usable price signal.")
        return MarketEstimate(
            listing_type=listing_type, n_comps_total=len(same_type), n_comps_priced_per_sqm=len(comps_arr),
            n_outliers_excluded=n_outliers, n_stale=n_stale, bandwidth_m=None, bandwidth_cross_validated=False,
            nw_mean_per_sqm_kes=None, weighted_median_per_sqm_kes=None, weighted_median_ci_low=None,
            weighted_median_ci_high=None, cost_level_percentile_0_100=None, growth_annual_pct=None,
            growth_annual_pct_se=None, growth_p_value=None, growth_n_comps=0, growth_reliable=False,
            weighted_median_total_kes=None, nearest_comp=nearest_comp, warnings=warnings,
        )

    D = _pairwise_distance_matrix_m(lat_k, lng_k)
    bandwidth_m, cv_used = select_bandwidth_loocv(D, log_k, BANDWIDTH_CANDIDATES_M)
    if not cv_used:
        warnings.append(
            f"Only {len(values_k)} usable {listing_type} comp(s) (< {MIN_COMPS_FOR_LOOCV}) -- kernel "
            "bandwidth could not be cross-validated (Hardle 1990's LOOCV method needs more points to "
            "be meaningful at this sample size) and falls back to a fixed mid-range candidate. "
            "Reported anyway, but treat as a rougher estimate."
        )

    kernel_w = _gaussian_kernel(dist_from_query_k, bandwidth_m)
    nw_mean_log = _nw_predict_at(dist_from_query_k, log_k, bandwidth_m)
    nw_mean = math.exp(nw_mean_log) if nw_mean_log is not None else None

    wmed_log = weighted_median(log_k, kernel_w)
    wmed = math.exp(wmed_log) if wmed_log is not None else None

    ci_low_log, ci_high_log = bootstrap_weighted_median_ci(log_k, kernel_w)
    ci_low = math.exp(ci_low_log) if ci_low_log is not None else None
    ci_high = math.exp(ci_high_log) if ci_high_log is not None else None
    if ci_low is None:
        warnings.append(
            f"Only {len(values_k)} usable {listing_type} comp(s) (< {MIN_COMPS_FOR_BOOTSTRAP}) -- "
            "bootstrap (Efron 1979) confidence interval not attempted; point estimate reported without "
            "an interval rather than a falsely precise one."
        )

    pctile = percentile_rank(wmed, values_k)

    # --- growth trend, module docstring point 3 ---
    cutoff = query_date.toordinal() - TREND_LOOKBACK_MONTHS * 30
    trend_comps = [
        (c, lv, kw) for c, lv, kw in zip(comps_k, log_k, kernel_w)
        if c.listed_at.toordinal() >= cutoff
    ]
    growth_annual_pct = growth_se = growth_p = None
    growth_reliable = False
    growth_n = len(trend_comps)
    if growth_n >= 3:
        t_months = np.array([
            (c.listed_at.toordinal() - query_date.toordinal()) / 30.44 for c, _, _ in trend_comps
        ])
        log_y = np.array([lv for _, lv, _ in trend_comps])
        w_trend = np.array([kw for _, _, kw in trend_comps])
        res = weighted_least_squares_trend(t_months, log_y, w_trend)
        if res is not None:
            slope, se, t_stat, p_value, n_used = res
            growth_annual_pct = (math.exp(12 * slope) - 1) * 100.0
            growth_se = (math.exp(12 * se) - 1) * 100.0
            growth_p = p_value
            growth_reliable = growth_n >= MIN_COMPS_FOR_TREND
    if growth_n < MIN_COMPS_FOR_TREND:
        warnings.append(
            f"Only {growth_n} {listing_type} comp(s) within the {TREND_LOOKBACK_MONTHS}-month trend "
            f"window (< {MIN_COMPS_FOR_TREND}) -- growth-rate regression is reported but flagged "
            "unreliable at this sample size."
        )
    warnings.append(
        f"{listing_type.title()} price trend is a listed-price time regression across distinct "
        "listings, NOT a repeat-sales index (Bailey, Muth & Nourse 1963; Case & Shiller 1989) -- this "
        "schema has no cross-listing property identity to build a true repeat-sales index from "
        "(see module docstring's 'WHAT'S NOT AVAILABLE')."
    )
    if listing_type == "sale":
        warnings.append(
            "Sale-side prices are asking prices, not confirmed transaction prices -- treat as an "
            "asking-price ceiling proxy, not a settled market-clearing estimate."
        )

    wmed_total = (
        weighted_median(np.array([v for _, v in total_pairs]), np.ones(len(total_pairs)))
        if total_pairs else None
    )

    return MarketEstimate(
        listing_type=listing_type, n_comps_total=len(same_type), n_comps_priced_per_sqm=len(comps_arr),
        n_outliers_excluded=n_outliers, n_stale=n_stale, bandwidth_m=bandwidth_m,
        bandwidth_cross_validated=cv_used, nw_mean_per_sqm_kes=nw_mean, weighted_median_per_sqm_kes=wmed,
        weighted_median_ci_low=ci_low, weighted_median_ci_high=ci_high, cost_level_percentile_0_100=pctile,
        growth_annual_pct=growth_annual_pct, growth_annual_pct_se=growth_se, growth_p_value=growth_p,
        growth_n_comps=growth_n, growth_reliable=growth_reliable, weighted_median_total_kes=wmed_total,
        nearest_comp=nearest_comp, warnings=warnings,
    )


def _compute_context_summary(comps: Sequence[PropertyComp], property_type: str) -> ContextMarketSummary:
    """Lightweight summary for residential/land -- see module docstring's
    scope-boundary note. No kernel/CI/trend machinery: this is reported
    color, not the primary computation."""
    subset = [c for c in comps if c.property_type == property_type]
    per_sqm = []
    for c in subset:
        v = c.price_per_sqm if c.listing_type == "sale" else c.rent_per_sqm
        if v is not None:
            per_sqm.append(v)
    if not per_sqm:
        return ContextMarketSummary(property_type=property_type, n_comps=len(subset), weighted_median_per_sqm_kes=None)
    arr = np.array(per_sqm)
    med = weighted_median(arr, np.ones(len(arr)))
    return ContextMarketSummary(property_type=property_type, n_comps=len(subset), weighted_median_per_sqm_kes=med)


def compute_property_cost_result(
    comps: Sequence[PropertyComp],
    query_date: date,
    investment_amount_kes: Optional[float] = None,
) -> PropertyCostResult:
    warnings: List[str] = []
    commercial_comps = [c for c in comps if c.property_type in COMMERCIAL_MARKET_TYPES]

    if not commercial_comps:
        warnings.append(
            f"No commercial or mixed_use PropertyListing records found within {SEARCH_RADIUS_M}m -- "
            "this may reflect genuinely no tracked listings nearby, or simply that this system's "
            "PropertyListing table has no records for this area yet. Treat an undefined result as "
            "low-confidence, not as evidence of an inactive market."
        )

    sale_est = _compute_market_estimate(commercial_comps, "sale", query_date)
    rent_est = _compute_market_estimate(commercial_comps, "rent", query_date)

    context = [_compute_context_summary(comps, pt) for pt in CONTEXT_PROPERTY_TYPES]

    affordability_ratio_sale = None
    months_of_rent_covered = None
    if investment_amount_kes is not None and investment_amount_kes > 0:
        if sale_est.weighted_median_total_kes:
            affordability_ratio_sale = investment_amount_kes / sale_est.weighted_median_total_kes
        if rent_est.weighted_median_total_kes:
            months_of_rent_covered = investment_amount_kes / rent_est.weighted_median_total_kes

    return PropertyCostResult(
        sale=sale_est, rent=rent_est, context=context,
        affordability_ratio_sale=affordability_ratio_sale, months_of_rent_covered=months_of_rent_covered,
        warnings=warnings,
    )


# =============================================================================
# Evidence dict builder
# =============================================================================

def _market_evidence(m: MarketEstimate) -> dict:
    return {
        "n_comps_total": m.n_comps_total,
        "n_comps_priced_per_sqm": m.n_comps_priced_per_sqm,
        "n_outliers_excluded": m.n_outliers_excluded,
        "n_stale": m.n_stale,
        "bandwidth_m": round(m.bandwidth_m, 1) if m.bandwidth_m is not None else None,
        "bandwidth_cross_validated": m.bandwidth_cross_validated,
        "nw_mean_per_sqm_kes": round(m.nw_mean_per_sqm_kes, 2) if m.nw_mean_per_sqm_kes is not None else None,
        "weighted_median_per_sqm_kes": (
            round(m.weighted_median_per_sqm_kes, 2) if m.weighted_median_per_sqm_kes is not None else None
        ),
        "weighted_median_per_sqm_95ci": [
            round(m.weighted_median_ci_low, 2) if m.weighted_median_ci_low is not None else None,
            round(m.weighted_median_ci_high, 2) if m.weighted_median_ci_high is not None else None,
        ],
        "cost_level_percentile_0_100": (
            round(m.cost_level_percentile_0_100, 1) if m.cost_level_percentile_0_100 is not None else None
        ),
        "growth_annual_pct": round(m.growth_annual_pct, 2) if m.growth_annual_pct is not None else None,
        "growth_annual_pct_se": round(m.growth_annual_pct_se, 2) if m.growth_annual_pct_se is not None else None,
        "growth_p_value": round(m.growth_p_value, 4) if m.growth_p_value is not None else None,
        "growth_n_comps": m.growth_n_comps,
        "growth_reliable": m.growth_reliable,
        "weighted_median_total_kes": (
            round(m.weighted_median_total_kes, 2) if m.weighted_median_total_kes is not None else None
        ),
        "nearest_comp": (
            {
                "listing_id": m.nearest_comp.listing_id,
                "property_type": m.nearest_comp.property_type,
                "distance_m": round(m.nearest_comp.distance_m, 1),
            } if m.nearest_comp else None
        ),
        "warnings": m.warnings,
    }


def property_cost_evidence(result: PropertyCostResult) -> dict:
    """Structured evidence dict, matching the shape FeatureObservation.value_json
    expects and the field-naming style development_activity_evidence() /
    infrastructure_change_evidence() already establish."""
    return {
        "sale": _market_evidence(result.sale),
        "rent": _market_evidence(result.rent),
        "context_markets": [
            {
                "property_type": c.property_type,
                "n_comps": c.n_comps,
                "weighted_median_per_sqm_kes": (
                    round(c.weighted_median_per_sqm_kes, 2) if c.weighted_median_per_sqm_kes is not None else None
                ),
            }
            for c in result.context
        ],
        "affordability_ratio_sale": (
            round(result.affordability_ratio_sale, 3) if result.affordability_ratio_sale is not None else None
        ),
        "months_of_rent_covered": (
            round(result.months_of_rent_covered, 2) if result.months_of_rent_covered is not None else None
        ),
        "methods": [
            "nadaraya_watson_kernel_regression",
            "loocv_bandwidth_selection",
            "tukey_iqr_outlier_fences_log_space",
            "weighted_median",
            "nonparametric_bootstrap_ci",
            "weighted_least_squares_trend",
            "empirical_percentile_rank",
        ],
        "warnings": result.warnings,
    }


# =============================================================================
# Django-facing entry point
# =============================================================================

METRIC_FIELDS = [
    (FEATURE_MEDIAN_PRICE_PER_SQM_SALE, "sale", "weighted_median_per_sqm_kes", "KES/sqm"),
    (FEATURE_MEDIAN_RENT_PER_SQM, "rent", "weighted_median_per_sqm_kes", "KES/sqm/month"),
    (FEATURE_SALE_GROWTH_ANNUAL_PCT, "sale", "growth_annual_pct", "pct/year"),
    (FEATURE_RENT_GROWTH_ANNUAL_PCT, "rent", "growth_annual_pct", "pct/year"),
    (FEATURE_COST_PERCENTILE_SALE, "sale", "cost_level_percentile_0_100", "percentile_0_100"),
    (FEATURE_COST_PERCENTILE_RENT, "rent", "cost_level_percentile_0_100", "percentile_0_100"),
]


def _load_property_comps(cell, radius_m: int = SEARCH_RADIUS_M) -> List[PropertyComp]:
    """Real Django-facing query over PropertyListing, resolved to
    straight-line distance from the cell. Local import keeps the
    pure-math layer above DB-import-free, matching this module's
    established split."""
    from ..models import PropertyListing

    lat, lng = float(cell.center_latitude), float(cell.center_longitude)

    # A generous bounding box first (cheap index-friendly prefilter), exact
    # haversine distance second -- same two-step pattern this codebase's
    # other radius-based engines use rather than a full-table scan.
    deg_pad = radius_m / 111_000.0 + 0.05
    candidates = PropertyListing.objects.filter(
        property_type__in=ALL_PROPERTY_TYPES,
        latitude__gte=Decimal(str(lat - deg_pad)),
        latitude__lte=Decimal(str(lat + deg_pad)),
        longitude__gte=Decimal(str(lng - deg_pad)),
        longitude__lte=Decimal(str(lng + deg_pad)),
    )

    comps: List[PropertyComp] = []
    for p in candidates:
        p_lat, p_lng = float(p.latitude), float(p.longitude)
        distance_m = _haversine_m(lat, lng, p_lat, p_lng)
        if distance_m > radius_m:
            continue
        comps.append(PropertyComp(
            listing_id=str(p.id),
            property_type=p.property_type,
            listing_type=p.listing_type,
            latitude=p_lat,
            longitude=p_lng,
            distance_m=distance_m,
            price_kes=float(p.price_kes) if p.price_kes is not None else None,
            monthly_rent_kes=float(p.monthly_rent_kes) if p.monthly_rent_kes is not None else None,
            floor_area_sqm=float(p.floor_area_sqm) if p.floor_area_sqm is not None else None,
            listed_at=p.listed_at,
            delisted_at=p.delisted_at,
            is_active=p.is_active,
            is_stale=bool(getattr(p, "is_stale", False)),
        ))
    return comps


def compute_property_cost_score(query, radius_m: int = SEARCH_RADIUS_M) -> tuple:
    """Real Django-facing Engine 15 entry point, matching the (result,
    evidence) tuple shape compute_infrastructure_change_score /
    compute_development_activity_score already return. Writes its own
    FeatureObservation rows and an EngineRun row, matching those
    engines' run() contract."""
    from ..models import EngineRun, FeatureObservation
    from django.utils import timezone as _timezone

    started_at = _timezone.now()
    cell = query.location_cell

    run_row = EngineRun.objects.create(
        query=query, engine_number=ENGINE_NUMBER, status="succeeded", started_at=started_at,
    )

    try:
        comps = _load_property_comps(cell, radius_m=radius_m)
    except Exception as exc:  # pragma: no cover - defensive, mirrors other engines' failure handling
        msg = f"Failed to load PropertyListing records: {exc}"
        logger.warning("Property Cost engine: %s (cell=%s)", msg, cell.geohash)
        run_row.status = "failed"
        run_row.error_message = msg
        run_row.finished_at = _timezone.now()
        run_row.save(update_fields=["status", "error_message", "finished_at"])
        return None, {"warnings": [msg], "engine_run_status": "failed"}

    investment_amount = (
        float(query.investment_amount_kes) if getattr(query, "investment_amount_kes", None) is not None else None
    )
    result = compute_property_cost_result(comps, query_date=started_at.date(), investment_amount_kes=investment_amount)
    evidence = property_cost_evidence(result)
    any_estimate = (
        result.sale.weighted_median_per_sqm_kes is not None
        or result.rent.weighted_median_per_sqm_kes is not None
    )
    evidence["engine_run_status"] = "ok" if any_estimate else "ok_no_nearby_comps"

    features_written = 0
    for field_name, listing_type, attr, unit in METRIC_FIELDS:
        market = result.sale if listing_type == "sale" else result.rent
        value = getattr(market, attr)
        if value is None:
            continue
        uncertainty = None
        if attr == "weighted_median_per_sqm_kes" and market.weighted_median_ci_low is not None:
            uncertainty = (market.weighted_median_ci_high - market.weighted_median_ci_low) / 2.0
        elif attr == "growth_annual_pct" and market.growth_annual_pct_se is not None:
            uncertainty = market.growth_annual_pct_se
        FeatureObservation.write_latest(
            location_cell=cell, engine_number=ENGINE_NUMBER, feature_name=field_name,
            defaults=dict(
                value_type="numeric",
                value_numeric=Decimal(str(round(value, 6))),
                unit=unit,
                observed_at=started_at,
                source="business_intel.engines.property_cost",
                source_class="internal_derived",
                method=(
                    "nadaraya_watson_kernel_weighted_median" if "kes" in field_name
                    else ("weighted_least_squares_trend" if "growth" in field_name else "empirical_percentile_rank")
                ),
                derivation_level="model_estimate",
                sample_size=market.n_comps_priced_per_sqm,
                uncertainty=Decimal(str(round(uncertainty, 6))) if uncertainty is not None else None,
            ),
        )
        features_written += 1

    finished_at = _timezone.now()
    run_row.finished_at = finished_at
    run_row.duration_ms = int((finished_at - started_at).total_seconds() * 1000)
    run_row.features_written = features_written
    all_warnings = result.warnings + result.sale.warnings + result.rent.warnings
    if all_warnings:
        run_row.status = "degraded"
        run_row.degraded_reason = "; ".join(sorted(set(all_warnings)))[:255]
    run_row.save(update_fields=["status", "finished_at", "duration_ms", "features_written", "degraded_reason"])

    return result, evidence


def run(query, radius_m: int = SEARCH_RADIUS_M) -> dict:
    """Engine 15 entry point. Thin wrapper around
    compute_property_cost_score(query, radius_m) -- see that function's
    docstring for what it actually computes/persists. Returns a dict
    shaped like every other wired engine's run() (score, evidence,
    engine_run), so tasks.py's _run_engine() can call it interchangeably
    with competition.run() / infrastructure_change.run() / etc. (Not
    currently called by tasks.py -- see module docstring's 'KNOWN
    UPSTREAM GAP'.)"""
    from ..models import EngineRun

    cell = query.location_cell
    result, evidence = compute_property_cost_score(query, radius_m=radius_m)

    run_row = (
        EngineRun.objects.filter(query=query, engine_number=ENGINE_NUMBER)
        .order_by("-started_at")
        .first()
    )

    return {"cell": cell, "result": result, "evidence": evidence, "engine_run": run_row}