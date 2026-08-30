"""
business_intel/engines/competition.py

Engine 8 -- Competition Engine
Per business_location_intelligence_engines_data_models_v2.md, Section 8.

Purpose
-------
Measure competitive intensity, concentration, incumbent strength, spatial
clustering, and inequality of demand distribution around a candidate
location.

Tool families used, and what is/isn't empirically grounded
-------------------------------------------------------------
  1. Market concentration -- HHI and CR4/CR8/CR20.
       https://www.justice.gov/atr/herfindahl-hirschman-index
     Thresholds (<1000 / 1000-1800 / >1800) are DOJ/FTC's own published
     merger-review cutoffs, not something calibrated here.

  2. Huff spatial-interaction (gravity) model:
       Pij = (Aj^alpha * Dij^-beta) / sum_k(Ak^alpha * Dik^-beta)
     Huff, D. (1963/1964). The distance-decay exponent beta is the one
     parameter every source insists must be calibrated per-market from
     real customer-store interaction data (Esri ArcGIS Pro "Understanding
     the Huff Model" documentation states its own uncalibrated defaults
     of 1.0/1.5 are "arbitrary" and may not apply). In the absence of
     that calibration data, this module uses the empirical RANGE found
     across multiple retail-catchment studies -- beta typically falls
     between -1 and -2 depending on category and competition (Young,
     1975; Joseph & Kuby, 2011; summarized in Woodward & Duque-Calvache,
     "Estimating attractiveness, hierarchy and catchment area extents
     for a national set of retail centre agglomerations", ScienceDirect
     2015: https://www.sciencedirect.com/science/article/pii/S0969698915300412)
     -- and validates any caller-supplied beta against that range,
     warning (not rejecting) if it falls outside [1.0, 2.0], since a
     value outside the commonly-observed range is not necessarily wrong
     but is worth flagging rather than silently accepting.

  3. Store density / saturation screening -- competitors per 100k
     catchment population (supply-side half of Index of Retail
     Saturation; demand-side left as an explicit opt-in, see
     index_of_retail_saturation, since this codebase has no real Kenya
     category-spend data).

  4. Inequality-of-shares -- Gini coefficient, Theil index, Shannon
     entropy over the Huff-modeled shares. Standard formulas (see e.g.
     any welfare/information-economics text); no category-specific
     calibration exists or is needed for these -- they're descriptive
     statistics of the share distribution itself, not a fitted model.

  5. Spatial pattern -- DBSCAN clustering of competitor coordinates,
     Gaussian KDE at the subject point, and Moran's I over competitor
     review counts.
       - DBSCAN (Ester, Kriegel, Sander & Xu, 1996, "A density-based
         algorithm for discovering clusters..."). Both of DBSCAN's
         parameters were previously hardcoded guesses (eps_km=0.3,
         min_samples=3). That is now fixed: eps is derived per-call from
         the actual competitor spacing via the k-distance "knee" method
         the original DBSCAN paper itself prescribes (compute each
         point's distance to its k-th nearest neighbor, sort descending,
         take the point of maximum curvature -- see `_estimate_eps_km`
         below), and min_samples defaults to 2 * dimensionality = 4, the
         standard heuristic for noisy low-dimensional spatial data
         (Sander, Ester, Kriegel & Xu, 1998, "Density-Based Clustering in
         Spatial Databases"). Both remain overridable for callers who
         have done their own tuning.
       - Gaussian KDE and Moran's I are standard, well-known formulas
         (scipy.stats.gaussian_kde; Moran, 1950) -- not something that
         needs or has category-specific calibration.

  6. Uncertainty -- weighted bootstrap resampling of the competitor set
     for a composite-score confidence interval, instead of reporting one
     point estimate as if it were exact. Standard nonparametric bootstrap
     technique; the CI reflects sampling uncertainty in which competitors
     happened to be observed, not model uncertainty in the scoring
     formula itself (see honest-limitation note below).

Everything here operates on REAL data already in this codebase:
BusinessEntity (name, category, brand, current_status, current_rating,
current_review_count, lat/lon) for competitor identity and strength, and
engines.population's compute_catchment_population / RealAdminHierarchyProvider
for real catchment population (not synthetic).

Honest limitation, stated plainly (not swept into a docstring corner)
------------------------------------------------------------------------
The Huff attractiveness proxy --
    Aj = (1 + review_count) ** review_weight * rating_multiplier(rating)
-- and the composite competition_score's weighting (50% subject Huff
share / 30% pre-entry HHI / 20% incumbent strength) are THIS MODULE'S
OWN design choices, not sourced from a paper and not fitted to any
observed outcome. No amount of literature review substitutes for what
they actually need: calibration against real BusinessQuery outcomes
(locations known to have succeeded or failed, or observed market
shares). That data does not currently exist in this codebase. Every
other function in this file (HHI, CR-n, Huff mechanics, Gini/Theil/
entropy, DBSCAN, KDE, Moran's I, bootstrap) is a standard, citable
method now used with literature-backed parameter choices; these two
specific pieces are flagged here so nobody mistakes "the surrounding
machinery is well-sourced" for "the final number is validated". If/when
outcome-labeled locations exist, refitting review_weight, the rating
multiplier shape, and the three composite-score weights against them is
the highest-value next step for this module -- ahead of adding further
algorithms.

Bootstrap performance note (read before raising n_iter)
---------------------------------------------------------
bootstrap_competition_score resamples the competitor set and recomputes
Huff shares + the composite score formula directly (NOT the full
compute_competition_result pipeline -- clustering/KDE/Moran's I don't
feed the composite score, so re-running them per iteration would be
pure waste). Even so this is O(n_iter * n_competitors); n_iter defaults
to 200. For run() (the Django-facing path, called per query) this is
the dominant cost of this engine.

Changelog (prior revisions, preserved)
-----------------------------------------
1. Sparse-competitor attractiveness prior blended toward the honest
   "new entrant" baseline instead of pure median-of-competitors.
2. CR8/CR20 actually computed and returned, not just documented.
3. Zero-competitors-for-category checked against "any operational
   business nearby regardless of category" to catch taxonomy mismatches.
4. Added Gini/Theil/entropy, DBSCAN clustering, KDE, Moran's I, and
   bootstrap CI (previous revision).

Changelog (this revision)
--------------------------
  - beta is now validated against the empirically-observed [1.0, 2.0]
    range from retail-catchment literature (see citation above); values
    outside it produce a warning, not a silent accept.
  - cluster_competitors no longer hardcodes eps_km: it now defaults to
    None and, when unset, derives eps from the competitor set itself via
    the k-distance knee method (`_estimate_eps_km`), per the original
    DBSCAN paper's own prescribed parameter-selection procedure. Passing
    an explicit eps_km still works for callers who've tuned their own.
  - min_samples for clustering now defaults to 2 * 2 = 4 (dimensionality
    heuristic) instead of an unexplained 3, overridable as before.
  - Everything else unchanged from the prior revision -- no functions
    removed, no logic stripped.

Dependency note: numpy, scipy.stats.gaussian_kde, sklearn.cluster.DBSCAN.
Confirm these are in requirements.txt / pyproject before deploying.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Optional, Sequence, Dict, List, Tuple

import numpy as np
from scipy.stats import gaussian_kde
from sklearn.cluster import DBSCAN

logger = logging.getLogger(__name__)

EARTH_RADIUS_M = 6_371_000.0

# Engine 8 per business_location_intelligence_engines_data_models_v2.md /
# models.ENGINE_CHOICES.
ENGINE_NUMBER = 8

# Minimum number of nearby competitors needed before we trust "median
# competitor attractiveness" as a stand-in for the subject's own
# attractiveness. Below this, the prior is blended toward the honest
# zero-review "new entrant" baseline instead.
MIN_SAMPLE_FOR_MEDIAN_TRUST = 5

# Minimum competitors before DBSCAN clustering is attempted at all.
MIN_COMPETITORS_FOR_CLUSTERING = 3

# Empirically-observed range for the Huff distance-decay exponent beta
# across retail-catchment calibration studies (Young, 1975; Joseph &
# Kuby, 2011; see module docstring for the full citation). This is a
# soft bound used only to emit a warning -- categories genuinely do vary
# outside it, so it is not enforced as a hard constraint.
BETA_EMPIRICAL_RANGE = (1.0, 2.0)


# =============================================================================
# 0. Geometry primitive
# =============================================================================

def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


# =============================================================================
# 1. Competitor observation
# =============================================================================

@dataclass
class CompetitorObservation:
    """One competing business, as needed by this engine. Maps 1:1 onto
    BusinessEntity's real fields -- see models.BusinessEntity."""
    id: str
    name: str
    latitude: float
    longitude: float
    rating: Optional[float] = None          # BusinessEntity.current_rating (0-5)
    review_count: int = 0                    # BusinessEntity.current_review_count
    brand: str = ""                          # BusinessEntity.brand, for chain concentration
    distance_m: Optional[float] = None       # filled in by nearest-neighbor helpers if unset


# =============================================================================
# 2. Attractiveness (Huff model's Aj)
# =============================================================================

def huff_attractiveness(
    rating: Optional[float],
    review_count: int,
    review_weight: float = 1.0,
    default_rating: float = 3.5,
) -> float:
    """Aj proxy = (1 + review_count) ** review_weight * rating_multiplier.

    See module docstring's "Honest limitation" section: this proxy's
    exact shape (review_weight=1.0, the [0.4, 1.3] rating-multiplier
    range) is this module's own design choice, not fitted to observed
    outcomes. It is a reasonable, explainable substitute for the
    literature's preferred store-size/sales attractiveness measures,
    neither of which exist in this dataset.
    """
    r = default_rating if rating is None else float(rating)
    rating_multiplier = 0.4 + 0.9 * (max(0.0, min(5.0, r)) / 5.0)
    return ((1.0 + max(0, review_count)) ** review_weight) * rating_multiplier


def new_entrant_baseline_attractiveness(default_rating: float = 3.5) -> float:
    """Attractiveness proxy for a location that has not opened yet: zero
    reviews, an unremarkable/default rating assumption."""
    return huff_attractiveness(rating=None, review_count=0, default_rating=default_rating)


def _subject_attractiveness_prior(competitors: Sequence["CompetitorObservation"]) -> float:
    """Neutral prior for the subject's own Huff attractiveness when the
    caller hasn't supplied one. Blends the honest "new entrant, zero
    reviews" baseline with the median competitor attractiveness,
    weighted by how many competitors are actually in the sample:

        trust = min(len(competitors) / MIN_SAMPLE_FOR_MEDIAN_TRUST, 1.0)
        prior = trust * median_competitor_attractiveness
              + (1 - trust) * new_entrant_baseline
    """
    baseline = new_entrant_baseline_attractiveness()
    if not competitors:
        return baseline
    proxies = sorted(huff_attractiveness(c.rating, c.review_count) for c in competitors)
    median = proxies[len(proxies) // 2]
    trust = min(len(competitors) / MIN_SAMPLE_FOR_MEDIAN_TRUST, 1.0)
    return trust * median + (1.0 - trust) * baseline


# =============================================================================
# 3. Huff spatial-interaction market shares
# =============================================================================

@dataclass
class HuffShareResult:
    shares: dict             # id -> probability/share, sums to 1.0 across all entrants
    attractiveness: dict     # id -> Aj used
    beta: float
    subject_id: str


def _validate_beta(beta: float, warnings: Optional[list] = None) -> None:
    """Warns (does not raise) if beta falls outside the empirically-
    observed range from retail-catchment calibration literature. See
    BETA_EMPIRICAL_RANGE and the module docstring citation. Categories
    genuinely do fall outside this range (e.g. destination/specialty
    retail with very flat decay), so this is advisory, not a hard
    constraint -- but a caller passing beta=0.1 or beta=10 without
    realizing it is a real, previously-silent failure mode this closes."""
    lo, hi = BETA_EMPIRICAL_RANGE
    if not (lo <= beta <= hi):
        msg = (
            f"beta={beta} is outside the empirically-observed range "
            f"[{lo}, {hi}] found across retail-catchment calibration studies "
            "(Young 1975; Joseph & Kuby 2011); proceeding, but this value has "
            "not been validated against real customer-store interaction data "
            "for any category in this dataset."
        )
        logger.warning(msg)
        if warnings is not None:
            warnings.append(msg)


def huff_market_shares(
    subject_id: str,
    subject_latitude: float,
    subject_longitude: float,
    subject_attractiveness: float,
    competitors: Sequence[CompetitorObservation],
    beta: float = 1.5,
    min_distance_m: float = 25.0,
) -> HuffShareResult:
    """Computes Pij for the subject location entering against `competitors`,
    per the Huff model:  Pj = (Aj / Dj^beta) / sum_k(Ak / Dk^beta).

    beta defaults to 1.5, inside the empirically-observed [1.0, 2.0]
    range (see module docstring citation) but NOT itself calibrated
    against this codebase's data -- see _validate_beta and the "Honest
    limitation" section above. Callers with real calibration data for a
    specific category should override it.

    Known simplification: evaluates Huff shares from a SINGLE
    representative demand origin (the subject coordinate itself), not
    an aggregate over every customer origin in the trade area. Read
    subject_share as "how much of the demand AT THIS EXACT POINT would
    this location capture", not a trade-area-wide average.
    """
    _validate_beta(beta)

    def _weight(a: float, d: float) -> float:
        d = max(d, min_distance_m)
        return a / (d ** beta)

    attractiveness = {subject_id: subject_attractiveness}
    weights = {subject_id: _weight(subject_attractiveness, min_distance_m)}

    for c in competitors:
        a = huff_attractiveness(c.rating, c.review_count)
        d = c.distance_m if c.distance_m is not None else haversine_m(
            subject_latitude, subject_longitude, c.latitude, c.longitude
        )
        attractiveness[c.id] = a
        weights[c.id] = _weight(a, d)

    total = sum(weights.values())
    if total <= 0:
        shares = {k: 0.0 for k in weights}
    else:
        shares = {k: v / total for k, v in weights.items()}

    return HuffShareResult(shares=shares, attractiveness=attractiveness, beta=beta, subject_id=subject_id)


# =============================================================================
# 4. Concentration measures -- HHI, CR-n
# =============================================================================

def herfindahl_hirschman_index(shares: Sequence[float]) -> float:
    """HHI = sum(share_pct^2), 0-10,000 scale. Per DOJ/FTC Merger
    Guidelines Sec 2.1: <1,000 unconcentrated; 1,000-1,800 moderately
    concentrated; >1,800 highly concentrated."""
    return sum((s * 100.0) ** 2 for s in shares)


def hhi_classification(hhi: float) -> str:
    if hhi < 1000:
        return "unconcentrated"
    if hhi <= 1800:
        return "moderately_concentrated"
    return "highly_concentrated"


def concentration_ratio(shares: Sequence[float], n: int) -> float:
    """CR_n = sum of the n largest shares (as a percent, 0-100)."""
    top_n = sorted(shares, reverse=True)[:n]
    return sum(top_n) * 100.0


def effective_number_of_competitors(hhi: float) -> Optional[float]:
    """1 / HHI_fraction -- the 'equivalent number of equal-sized firms'
    this market behaves like. Undefined at HHI=0."""
    if hhi <= 0:
        return None
    hhi_fraction = hhi / 10_000.0
    return 1.0 / hhi_fraction


def brand_concentration(competitors: Sequence[CompetitorObservation]) -> dict:
    """Same-brand share of competitor COUNT (not Huff demand)."""
    if not competitors:
        return {}
    counts: dict = {}
    for c in competitors:
        key = c.brand.strip() if c.brand and c.brand.strip() else None
        if key is None:
            continue
        counts[key] = counts.get(key, 0) + 1
    total_branded = sum(counts.values())
    if total_branded == 0:
        return {}
    return {brand: count / total_branded for brand, count in counts.items()}


# =============================================================================
# 4b. Inequality-of-shares -- Gini, Theil, Shannon entropy over the same
#     Huff shares HHI/CR-n use. Distributional-shape complement to HHI.
# =============================================================================

def gini_coefficient(shares: Sequence[float]) -> float:
    """Gini coefficient (0=perfectly equal, ->1 as inequality grows) of
    the Huff share distribution:
        G = sum_i sum_j |s_i - s_j| / (2 * n * sum(s))
    Returns 0.0 for an empty or all-zero input rather than raising."""
    if not shares:
        return 0.0
    total = sum(shares)
    if total <= 0:
        return 0.0
    sorted_shares = sorted(shares)
    n = len(sorted_shares)
    sum_abs_diff = 0.0
    for i in range(n):
        for j in range(n):
            sum_abs_diff += abs(sorted_shares[i] - sorted_shares[j])
    return sum_abs_diff / (2 * n * total)


def theil_index(shares: Sequence[float]) -> float:
    """Theil T index (generalized entropy, alpha=1) over the share
    distribution: T = (1/n) * sum(s_i/mean * ln(s_i/mean)) for s_i > 0.
    Returns 0.0 if shares is empty or all zero."""
    if not shares:
        return 0.0
    n = len(shares)
    mean = sum(shares) / n
    if mean <= 0:
        return 0.0
    total = 0.0
    for s in shares:
        if s > 0:
            ratio = s / mean
            total += ratio * math.log(ratio)
    return total / n


def shannon_entropy(shares: Sequence[float]) -> float:
    """Shannon entropy (nats) of the share distribution -- higher means
    demand is spread more evenly across players. Renormalizes over the
    nonzero shares before computing."""
    if not shares:
        return 0.0
    total = sum(shares)
    if total <= 0:
        return 0.0
    probs = [s / total for s in shares if s > 0]
    return -sum(p * math.log(p) for p in probs)


# =============================================================================
# 5. Store density / saturation screening
# =============================================================================

def store_density_per_100k(competitor_count: int, catchment_population: Optional[float]) -> Optional[float]:
    """Competitors per 100,000 catchment population. Returns None (not 0
    or a fabricated figure) when catchment_population is unavailable."""
    if not catchment_population or catchment_population <= 0:
        return None
    return (competitor_count / catchment_population) * 100_000.0


def index_of_retail_saturation(
    catchment_population: Optional[float],
    households_per_capita: float,
    category_annual_spend_per_household: Optional[float],
    total_competitor_supply_units: Optional[float],
) -> Optional[float]:
    """IRS = (households * annual category spend per household) / retail
    supply. Deliberately NOT called by compute_competition_score below --
    this codebase has no real Kenya category-level household-expenditure
    data to supply category_annual_spend_per_household honestly."""
    if catchment_population is None or category_annual_spend_per_household is None:
        return None
    if not total_competitor_supply_units or total_competitor_supply_units <= 0:
        return None
    households = catchment_population * households_per_capita
    return (households * category_annual_spend_per_household) / total_competitor_supply_units


# =============================================================================
# 6. Incumbent strength
# =============================================================================

def incumbent_strength(competitors: Sequence[CompetitorObservation]) -> Optional[dict]:
    """Review-weighted average rating plus max single-competitor review
    count. Returns None if no competitor has any reviews."""
    rated = [c for c in competitors if c.rating is not None and c.review_count > 0]
    if not rated:
        return None
    total_reviews = sum(c.review_count for c in rated)
    weighted_rating = sum(float(c.rating) * c.review_count for c in rated) / total_reviews
    return {
        "review_weighted_avg_rating": round(weighted_rating, 3),
        "max_competitor_review_count": max(c.review_count for c in rated),
        "total_competitor_reviews": total_reviews,
        "rated_competitor_count": len(rated),
    }


# =============================================================================
# 6b. Spatial pattern -- DBSCAN clustering (with literature-backed
#     automatic parameter selection), Gaussian KDE, Moran's I.
# =============================================================================

def _estimate_eps_km(coords_km: np.ndarray, k: int) -> Optional[float]:
    """k-distance knee method for DBSCAN eps selection, per Ester et al.
    (1996), the original DBSCAN paper: for each point, compute its
    distance to its k-th nearest neighbor; sort these descending; the
    point of maximum curvature ("knee") in that sorted curve is the
    recommended eps. k should equal min_samples (the paper's own
    convention -- k-distance graphs are conventionally built with
    k = MinPts).

    Curvature is estimated via the standard "maximum distance from the
    chord connecting the curve's endpoints" formalization of the knee --
    the same approach used by the `kneedle` algorithm family, implemented
    here directly (no extra dependency) since the curve is small (one
    point per competitor) and doesn't need kneedle's more general
    smoothing machinery.

    Returns None if there are fewer than k+1 points (not enough to form
    a k-distance curve at all) -- callers should fall back to a
    documented default eps in that case, not silently proceed with a
    meaningless curve.
    """
    n = coords_km.shape[0]
    if n <= k:
        return None

    # k-th nearest neighbor distance for every point.
    k_distances = np.empty(n)
    for i in range(n):
        diffs = coords_km - coords_km[i]
        dists = np.sqrt(np.sum(diffs ** 2, axis=1))
        dists.sort()
        # dists[0] is the point itself (distance 0); k-th neighbor is dists[k].
        k_distances[i] = dists[min(k, n - 1)]

    sorted_desc = np.sort(k_distances)[::-1]
    m = len(sorted_desc)
    if m < 3:
        return float(sorted_desc[-1]) if m else None

    # Distance of each point on the curve from the chord connecting its
    # first and last point -- the knee is where this distance peaks.
    x = np.arange(m, dtype=float)
    y = sorted_desc
    x1, y1 = x[0], y[0]
    x2, y2 = x[-1], y[-1]
    chord_len = math.hypot(x2 - x1, y2 - y1)
    if chord_len == 0:
        return float(y[0])
    # Perpendicular distance from each (x_i, y_i) to the chord.
    numerators = np.abs((y2 - y1) * x - (x2 - x1) * y + x2 * y1 - y2 * x1)
    distances_from_chord = numerators / chord_len
    knee_idx = int(np.argmax(distances_from_chord))
    return float(sorted_desc[knee_idx])


def cluster_competitors(
    competitors: Sequence[CompetitorObservation],
    eps_km: Optional[float] = None,
    min_samples: int = 4,
) -> List[Dict]:
    """DBSCAN over competitor lat/lon (degrees rescaled to an approximate
    km grid via *111.0, which is only accurate near the equator and
    distorts east-west distance away from it -- acceptable at the
    sub-kilometer scale this operates at within Kenya's latitude range).

    eps_km: if None (the default), derived from the competitor set
    itself via the k-distance knee method (`_estimate_eps_km`), per the
    original DBSCAN paper's own parameter-selection procedure -- NOT a
    fixed guess. Pass an explicit value to override (e.g. if you've
    tuned eps against known ground-truth clusters for this market).

    min_samples: defaults to 4 (2 * dimensionality, the standard
    heuristic for noisy low-dimensional spatial data per Sander, Ester,
    Kriegel & Xu, 1998) rather than an unexplained fixed 3.

    Points DBSCAN labels as noise (-1) are excluded -- they are not
    "clusters of one". Returns [] if there are fewer than
    MIN_COMPETITORS_FOR_CLUSTERING competitors, or if eps could not be
    estimated (too few points for a meaningful k-distance curve).
    """
    if len(competitors) < MIN_COMPETITORS_FOR_CLUSTERING:
        return []
    coords_deg = np.array([[c.latitude, c.longitude] for c in competitors])
    coords_km = coords_deg * 111.0

    resolved_eps_km = eps_km
    if resolved_eps_km is None:
        resolved_eps_km = _estimate_eps_km(coords_km, k=min_samples)
        if resolved_eps_km is None or resolved_eps_km <= 0:
            logger.info(
                "cluster_competitors: could not estimate eps from %d competitors "
                "(need > min_samples=%d points for a k-distance curve); skipping clustering.",
                len(competitors), min_samples,
            )
            return []

    labels = DBSCAN(eps=resolved_eps_km, min_samples=min_samples).fit(coords_km).labels_

    clusters: List[Dict] = []
    for label in sorted(set(labels)):
        if label == -1:
            continue
        idx = np.where(labels == label)[0]
        member_coords = coords_deg[idx]
        center_lat = float(np.mean(member_coords[:, 0]))
        center_lon = float(np.mean(member_coords[:, 1]))
        radii = [
            haversine_m(center_lat, center_lon, competitors[i].latitude, competitors[i].longitude)
            for i in idx
        ]
        clusters.append({
            "center_lat": round(center_lat, 6),
            "center_lon": round(center_lon, 6),
            "size": int(len(idx)),
            "avg_radius_m": round(float(np.mean(radii)), 1) if radii else 0.0,
            "competitor_ids": [competitors[i].id for i in idx],
            "eps_km_used": round(resolved_eps_km, 4),
        })
    return clusters


def compute_kde_at_point(
    competitors: Sequence[CompetitorObservation],
    lat: float,
    lon: float,
    bandwidth_km: float = 0.5,
) -> Optional[float]:
    """Gaussian KDE of competitor locations, evaluated at (lat, lon).
    Unlike competitor_count / fixed-radius density, this weights nearer
    competitors more heavily and has no hard cutoff. scipy's
    gaussian_kde requires at least 2 points AND nonzero variance across
    them -- both degenerate cases return None here rather than raising,
    since "not enough spatial spread to estimate a density" is a real,
    reportable state, not a bug.
    """
    if len(competitors) < 2:
        return None
    coords_rad = np.radians(np.array([[c.latitude, c.longitude] for c in competitors]))
    bandwidth_rad = bandwidth_km / 111.0
    try:
        kde = gaussian_kde(coords_rad.T, bw_method=bandwidth_rad)
    except (np.linalg.LinAlgError, ValueError):
        return None
    point_rad = np.radians([lat, lon])
    return float(kde.evaluate(point_rad)[0])


def morans_i(competitors: Sequence[CompetitorObservation]) -> Optional[float]:
    """Moran's I (Moran, 1950) for spatial autocorrelation of competitor
    review_count: do strong (high-review) and weak (low-review)
    incumbents cluster together spatially, or are they intermixed? +1 =
    strong spatial clustering of similar values, -1 = dispersion, ~0 =
    no spatial pattern. Uses an inverse-distance weight matrix
    (1 / (distance_m + 1)) rather than a k-nearest-neighbors graph, since
    competitor counts here are typically small (search-radius-bounded).
    Requires >= 3 competitors and nonzero variance in review_count;
    returns None otherwise."""
    if len(competitors) < 3:
        return None
    review_counts = np.array([c.review_count for c in competitors], dtype=float)
    std = np.std(review_counts)
    if std == 0:
        return None
    z = (review_counts - np.mean(review_counts)) / std

    n = len(competitors)
    coords = [(c.latitude, c.longitude) for c in competitors]
    w = np.zeros((n, n))
    for i in range(n):
        for j in range(n):
            if i != j:
                d = haversine_m(coords[i][0], coords[i][1], coords[j][0], coords[j][1])
                w[i, j] = 1.0 / (d + 1.0)

    w_sum = w.sum()
    if w_sum == 0:
        return None
    numerator = 0.0
    for i in range(n):
        for j in range(n):
            if i != j:
                numerator += w[i, j] * z[i] * z[j]
    denominator = np.sum(z ** 2)
    if denominator == 0:
        return None
    return float((n / w_sum) * (numerator / denominator))


# =============================================================================
# 6c. Uncertainty -- weighted bootstrap over the composite score.
# =============================================================================

def bootstrap_competition_score(
    subject_id: str,
    subject_latitude: float,
    subject_longitude: float,
    subject_attractiveness: float,
    competitors: Sequence[CompetitorObservation],
    beta: float = 1.5,
    n_iter: int = 200,
    alpha: float = 0.05,
    random_seed: Optional[int] = None,
) -> Tuple[float, float, float]:
    """Weighted bootstrap resample of the competitor set (weight =
    review_count + 1, so established competitors have proportionally
    more resampling influence than a single-review outlier), recomputing
    ONLY the Huff-shares -> composite-score chain per resample. Returns
    (mean_score, ci_lower, ci_upper) at the given alpha (default: 95%
    CI). This CI reflects sampling uncertainty over which competitors
    were observed -- it does NOT capture uncertainty in the scoring
    formula's own weights (see module docstring's "Honest limitation").
    Falls back to a degenerate (score, score, score) when there are no
    competitors to resample.
    """
    n = len(competitors)
    if n == 0:
        huff = huff_market_shares(
            subject_id, subject_latitude, subject_longitude,
            subject_attractiveness, competitors, beta=beta,
        )
        score = _composite_score_from_shares(huff.shares.get(subject_id, 0.0), None, None)
        return score, score, score

    rng = np.random.default_rng(random_seed)
    weights = np.array([c.review_count + 1 for c in competitors], dtype=float)
    probs = weights / weights.sum()

    scores = np.empty(n_iter)
    for i in range(n_iter):
        idx = rng.choice(n, size=n, replace=True, p=probs)
        resampled = [competitors[k] for k in idx]

        huff = huff_market_shares(
            subject_id, subject_latitude, subject_longitude,
            subject_attractiveness, resampled, beta=beta,
        )
        subject_share = huff.shares.get(subject_id, 0.0)

        pre_shares = {k: v for k, v in huff.shares.items() if k != subject_id}
        pre_total = sum(pre_shares.values())
        hhi_pre = herfindahl_hirschman_index([v / pre_total for v in pre_shares.values()]) if pre_total > 0 else None

        strength = incumbent_strength(resampled)
        scores[i] = _composite_score_from_shares(subject_share, hhi_pre, strength)

    mean_score = float(np.mean(scores))
    lower = float(np.percentile(scores, 100 * alpha / 2))
    upper = float(np.percentile(scores, 100 * (1 - alpha / 2)))
    return mean_score, lower, upper


# =============================================================================
# 7. Composite competition result + score
# =============================================================================

@dataclass
class CompetitionResult:
    competitor_count: int
    huff: HuffShareResult
    subject_share: float
    hhi_post_entry: float
    hhi_pre_entry: Optional[float]
    hhi_classification: str
    cr4: float
    cr8: float
    cr20: float
    effective_competitors: Optional[float]
    brand_shares: dict
    store_density_per_100k: Optional[float]
    incumbent: Optional[dict]
    gini_coefficient: float
    theil_index: float
    shannon_entropy: float
    spatial_clusters: List[Dict]
    kde_peak_density: Optional[float]
    morans_i: Optional[float]
    score_ci_lower: Optional[float] = None
    score_ci_upper: Optional[float] = None
    feature_weights: Dict[str, float] = field(default_factory=dict)
    warnings: list = field(default_factory=list)


def compute_competition_result(
    subject_id: str,
    subject_latitude: float,
    subject_longitude: float,
    competitors: Sequence[CompetitorObservation],
    catchment_population: Optional[float] = None,
    subject_attractiveness: Optional[float] = None,
    beta: float = 1.5,
    compute_bootstrap_ci: bool = True,
    bootstrap_n_iter: int = 200,
    cluster_eps_km: Optional[float] = None,
    cluster_min_samples: int = 4,
) -> CompetitionResult:
    """Runs the full pipeline and returns every intermediate figure, not
    just a final score.

    compute_bootstrap_ci: set False to skip the bootstrap pass --
    score_ci_lower / score_ci_upper will be None, not a fabricated
    interval.
    cluster_eps_km / cluster_min_samples: passed through to
    cluster_competitors; leave eps_km unset to use the literature-backed
    k-distance knee auto-selection (see cluster_competitors docstring).
    """
    warnings: list = []
    _validate_beta(beta, warnings)

    if subject_attractiveness is None:
        subject_attractiveness = _subject_attractiveness_prior(competitors)
        if competitors and len(competitors) < MIN_SAMPLE_FOR_MEDIAN_TRUST:
            warnings.append(
                f"subject_attractiveness not supplied and only {len(competitors)} competitor(s) "
                f"found (< {MIN_SAMPLE_FOR_MEDIAN_TRUST}); used a blend of the new-entrant baseline "
                "and competitor-median attractiveness, weighted toward the baseline."
            )
        else:
            warnings.append(
                "subject_attractiveness not supplied; used category-median competitor "
                "attractiveness as a neutral prior for a not-yet-open location."
            )

    huff = huff_market_shares(
        subject_id, subject_latitude, subject_longitude,
        subject_attractiveness, competitors, beta=beta,
    )
    subject_share = huff.shares.get(subject_id, 0.0)
    all_shares = list(huff.shares.values())

    hhi_post = herfindahl_hirschman_index(all_shares)
    hhi_pre = None
    if competitors:
        pre_shares = {k: v for k, v in huff.shares.items() if k != subject_id}
        pre_total = sum(pre_shares.values())
        if pre_total > 0:
            hhi_pre = herfindahl_hirschman_index([v / pre_total for v in pre_shares.values()])

    cr4 = concentration_ratio(all_shares, 4)
    cr8 = concentration_ratio(all_shares, 8)
    cr20 = concentration_ratio(all_shares, 20)
    effective = effective_number_of_competitors(hhi_post)
    brands = brand_concentration(competitors)
    density = store_density_per_100k(len(competitors), catchment_population)
    if catchment_population is None:
        warnings.append("catchment_population not supplied; store_density_per_100k is unset (honest gap, not 0).")
    strength = incumbent_strength(competitors)

    gini = gini_coefficient(all_shares)
    theil = theil_index(all_shares)
    entropy_val = shannon_entropy(all_shares)

    clusters = cluster_competitors(competitors, eps_km=cluster_eps_km, min_samples=cluster_min_samples)
    kde_peak = compute_kde_at_point(competitors, subject_latitude, subject_longitude)
    if kde_peak is None and len(competitors) >= 2:
        warnings.append("kde_peak_density unset: competitor coordinates lack sufficient spatial spread to estimate a density.")
    moran = morans_i(competitors)

    ci_lower: Optional[float] = None
    ci_upper: Optional[float] = None
    if compute_bootstrap_ci:
        _, ci_lower, ci_upper = bootstrap_competition_score(
            subject_id, subject_latitude, subject_longitude, subject_attractiveness,
            competitors, beta=beta, n_iter=bootstrap_n_iter,
        )

    return CompetitionResult(
        competitor_count=len(competitors),
        huff=huff,
        subject_share=subject_share,
        hhi_post_entry=round(hhi_post, 2),
        hhi_pre_entry=round(hhi_pre, 2) if hhi_pre is not None else None,
        hhi_classification=hhi_classification(hhi_post),
        cr4=round(cr4, 2),
        cr8=round(cr8, 2),
        cr20=round(cr20, 2),
        effective_competitors=round(effective, 2) if effective is not None else None,
        brand_shares={k: round(v, 4) for k, v in brands.items()},
        store_density_per_100k=round(density, 2) if density is not None else None,
        incumbent=strength,
        gini_coefficient=round(gini, 4),
        theil_index=round(theil, 4),
        shannon_entropy=round(entropy_val, 4),
        spatial_clusters=clusters,
        kde_peak_density=round(kde_peak, 8) if kde_peak is not None else None,
        morans_i=round(moran, 4) if moran is not None else None,
        score_ci_lower=round(ci_lower, 2) if ci_lower is not None else None,
        score_ci_upper=round(ci_upper, 2) if ci_upper is not None else None,
        feature_weights={"subject_share": 0.5, "hhi_pre_entry": 0.3, "incumbent_strength": 0.2},
        warnings=warnings,
    )


def _composite_score_from_shares(
    subject_share: float,
    hhi_pre_entry: Optional[float],
    incumbent: Optional[dict],
) -> float:
    """The composite-score formula, factored out so
    bootstrap_competition_score can call it directly per-resample.
    competition_score(result) below is a thin wrapper over this using an
    already-built CompetitionResult's fields -- keep the two in sync if
    this formula changes. See module docstring's "Honest limitation":
    these weights (0.5/0.3/0.2) are uncalibrated against real outcomes."""
    share_component = min(subject_share / 0.5, 1.0) * 100.0
    if hhi_pre_entry is None:
        hhi_component = 100.0
    else:
        hhi_component = max(0.0, 1.0 - hhi_pre_entry / 10_000.0) * 100.0

    if incumbent is None:
        strength_component = 100.0
    else:
        rating_term = incumbent["review_weighted_avg_rating"] / 5.0
        volume_term = min(math.log1p(incumbent["total_competitor_reviews"]) / math.log1p(5000), 1.0)
        difficulty = (0.6 * rating_term + 0.4 * volume_term) * 100.0
        strength_component = 100.0 - difficulty

    score = 0.5 * share_component + 0.3 * hhi_component + 0.2 * strength_component
    return round(max(0.0, min(100.0, score)), 2)


def competition_score(result: CompetitionResult) -> float:
    """Maps CompetitionResult onto a 0-100 'how favorable is this
    competitive environment for a new entrant' score (100 = wide open,
    0 = saturated by strong incumbents).

    Weights: 50% subject Huff share / 30% pre-entry HHI (inverted) /
    20% incumbent strength (inverted). NOT calibrated against real
    outcomes -- see module docstring "Honest limitation". Treat the
    absolute number as directional until real outcome data exists to
    fit against.
    """
    return _composite_score_from_shares(result.subject_share, result.hhi_pre_entry, result.incumbent)


def competition_evidence(result: CompetitionResult) -> dict:
    """Structured evidence dict, matching the shape
    engines.evidence / models.FeatureObservation.value_json expects."""
    return {
        "competitor_count": result.competitor_count,
        "subject_projected_huff_share_pct": round(result.subject_share * 100.0, 2),
        "hhi_post_entry": result.hhi_post_entry,
        "hhi_pre_entry": result.hhi_pre_entry,
        "hhi_classification": result.hhi_classification,
        "cr4_pct": result.cr4,
        "cr8_pct": result.cr8,
        "cr20_pct": result.cr20,
        "effective_number_of_competitors": result.effective_competitors,
        "top_brand_shares": dict(sorted(result.brand_shares.items(), key=lambda kv: -kv[1])[:5]),
        "store_density_per_100k_population": result.store_density_per_100k,
        "incumbent_strength": result.incumbent,
        "gini_coefficient": result.gini_coefficient,
        "theil_index": result.theil_index,
        "shannon_entropy": result.shannon_entropy,
        "spatial_clusters": result.spatial_clusters,
        "kde_peak_density": result.kde_peak_density,
        "morans_i": result.morans_i,
        "composite_score_ci_95_lower": result.score_ci_lower,
        "composite_score_ci_95_upper": result.score_ci_upper,
        "feature_weights": result.feature_weights,
        "methods": [
            "huff_model", "HHI", "CR4", "CR8", "CR20", "gini", "theil",
            "shannon_entropy", "dbscan_clustering_kdist_auto_eps", "gaussian_kde",
            "morans_i", "weighted_bootstrap_ci",
        ],
        "warnings": result.warnings,
    }


# =============================================================================
# 8. Django-facing entry point.
# =============================================================================

DEFAULT_COMPETITOR_SEARCH_RADIUS_M = 2000.0


def compute_competition_score(
    location_cell,
    category: str,
    search_radius_m: float = DEFAULT_COMPETITOR_SEARCH_RADIUS_M,
    catchment_population: Optional[float] = None,
    beta: float = 1.5,
    compute_bootstrap_ci: bool = True,
    bootstrap_n_iter: int = 200,
):
    """Real Django-facing Engine 8 entry point.

    Queries models.BusinessEntity for active competitors of `category`
    within search_radius_m of location_cell, runs the full pipeline
    above, and returns (score, evidence).

    If zero competitors are found for `category`, also checks whether
    ANY operational business exists in the same radius regardless of
    category, to distinguish a genuinely open market from a
    category-taxonomy mismatch, flagging the latter in
    evidence["warnings"].
    """
    from ..models import BusinessEntity  # local import: keep this module DB-import-free for pure-math callers/tests

    subject_lat = float(location_cell.center_latitude)
    subject_lon = float(location_cell.center_longitude)

    lat_pad = search_radius_m / 111_000.0
    lon_pad = search_radius_m / (111_000.0 * max(math.cos(math.radians(subject_lat)), 0.01))

    bbox_filter = dict(
        current_status="operational",
        latitude__gte=subject_lat - lat_pad,
        latitude__lte=subject_lat + lat_pad,
        longitude__gte=subject_lon - lon_pad,
        longitude__lte=subject_lon + lon_pad,
    )

    qs = BusinessEntity.objects.filter(category=category, **bbox_filter)

    competitors = []
    for biz in qs:
        d = haversine_m(subject_lat, subject_lon, float(biz.latitude), float(biz.longitude))
        if d <= search_radius_m:
            competitors.append(CompetitorObservation(
                id=str(biz.id),
                name=biz.name,
                latitude=float(biz.latitude),
                longitude=float(biz.longitude),
                rating=float(biz.current_rating) if biz.current_rating is not None else None,
                review_count=biz.current_review_count or 0,
                brand=biz.brand or "",
                distance_m=d,
            ))

    result = compute_competition_result(
        subject_id="subject",
        subject_latitude=subject_lat,
        subject_longitude=subject_lon,
        competitors=competitors,
        catchment_population=catchment_population,
        beta=beta,
        compute_bootstrap_ci=compute_bootstrap_ci,
        bootstrap_n_iter=bootstrap_n_iter,
    )

    if not competitors:
        any_business_nearby = BusinessEntity.objects.filter(**bbox_filter).exists()
        if any_business_nearby:
            msg = (
                f"zero competitors found for category={category!r} within {search_radius_m:.0f}m, "
                "but other operational businesses exist in that radius -- verify category "
                "taxonomy before treating this as an open market."
            )
            logger.warning(msg)
            result.warnings.append(msg)

    score = competition_score(result)
    evidence = competition_evidence(result)
    return score, evidence


# =============================================================================
# 9. Django-facing entry point (persistence wrapper around #8 above).
# =============================================================================

def run(
    query,
    search_radius_m: float = DEFAULT_COMPETITOR_SEARCH_RADIUS_M,
    catchment_population: Optional[float] = None,
    beta: float = 1.5,
    compute_bootstrap_ci: bool = True,
    bootstrap_n_iter: int = 200,
) -> dict:
    """
    Engine 8 entry point. Computes the competitive environment for
    query.location_cell against query.business_category (cell-level,
    catchment=None), writes one business_conclusion-level
    FeatureObservation ('competition_score') plus a calculation-level
    evidence row, and records an EngineRun.

    See the module docstring's "Bootstrap performance note" before
    raising bootstrap_n_iter at scale.
    """
    from ..models import BusinessQuery, EngineRun, FeatureObservation  # noqa: F401 (BusinessQuery for type context only)
    from decimal import Decimal
    from django.db import transaction
    from django.utils import timezone as django_timezone

    started_at = django_timezone.now()
    cell = query.location_cell
    category = query.business_category

    run_row = EngineRun.objects.create(
        query=query, engine_number=ENGINE_NUMBER, status="succeeded", started_at=started_at,
    )

    features_written = 0
    warnings_seen: list = []

    try:
        with transaction.atomic():
            score, evidence = compute_competition_score(
                cell, category, search_radius_m=search_radius_m,
                catchment_population=catchment_population, beta=beta,
                compute_bootstrap_ci=compute_bootstrap_ci,
                bootstrap_n_iter=bootstrap_n_iter,
            )
            warnings_seen.extend(evidence.get("warnings") or [])

            FeatureObservation.write_latest(
                location_cell=cell, catchment=None, engine_number=ENGINE_NUMBER,
                feature_name="competition_score", business_category=category,
                defaults=dict(
                    value_type="numeric", value_numeric=Decimal(str(score)), unit="score_0_100",
                    observed_at=started_at, source="business_intel_internal", source_class="internal_derived",
                    method="huff_hhi_cr20_gini_theil_dbscan_autoeps_composite", derivation_level="business_conclusion",
                    sample_size=evidence.get("competitor_count"),
                ),
            )
            features_written += 1

            FeatureObservation.write_latest(
                location_cell=cell, catchment=None, engine_number=ENGINE_NUMBER,
                feature_name="competition_evidence", business_category=category,
                defaults=dict(
                    value_type="json", value_json=evidence, unit="",
                    observed_at=started_at, source="business_intel_internal", source_class="internal_derived",
                    method="huff_hhi_cr20_gini_theil_dbscan_autoeps_composite", derivation_level="calculation",
                    sample_size=evidence.get("competitor_count"),
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

    return {"cell": cell, "score": score, "evidence": evidence, "engine_run": run_row}