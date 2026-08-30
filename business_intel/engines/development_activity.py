"""
business_intel/engines/development_activity.py

Engine 12 — Development Activity Engine
Per business_location_intelligence_engines_data_models_v2.md, Section 12.

NOTE ON THE SPEC FILE: business_location_intelligence_engines_data_models_v2.md
is referenced by every engine stub in this codebase but does not exist on
disk (confirmed via `find`, same finding population.py/purchasing_power.py/
competition.py already documented). Built without it, matching those
engines' structure/schema contract and citation discipline. Anywhere this
would otherwise need to guess at the missing spec, it says so instead of
guessing silently.

=============================================================================
PURPOSE (per the stub this replaces)
=============================================================================
"Measure whether the physical environment is expanding or changing."
Deliberately PHYSICAL, not economic and not business-churn -- Engine 6
(business_ecosystem.py) and Engine 7 (business_survival.py) already own
"is the catchment's business population growing/dying", and duplicating
that here would violate the same one-engine-one-question discipline
market_gap.py and business_adjacency.py's docstrings already insist on for
themselves. This engine answers a narrower, physical question: is there
construction (or demolition/disuse) actually happening on the ground near
this cell, right now.

=============================================================================
WHY THIS IS A REAL STATISTICS ENGINE, NOT A COUNT DIVIDED BY A GUESS
=============================================================================
The only real, freely-available, already-integrated-in-this-codebase data
source for "is something being physically built here" is OpenStreetMap via
the Overpass API -- "osm_overpass" is already a first-class entry in
models.BusinessAPICallLog.API_CHOICES, and property_intel/amenity_verification.py
already proves the transport/logging pattern works in production. There is
no satellite change-detection pipeline in this codebase (VIIRS/WorldPop
providers referenced in BusinessAPICallLog.API_CHOICES are download-only,
annual-cadence rasters -- wrong temporal grain for "is a building going up
this month" -- and are Engine 18/Economic Momentum's territory, not this
engine's). No Kenyan county publishes machine-readable building-permit data
this codebase has access to (Kenya Gazette scraping, per
property_intel/google_client.py's scrape_gazette API choice, covers land
transactions and public notices, not construction permits). So: OSM tags,
honestly, is what's real here -- everything below is built to survive that
data source being sparse, uneven, and occasionally wrong, rather than
pretending otherwise.

That means the actual statistical problem is NOT "what fraction of
buildings are under construction" (a number you could just print) but
THREE real small-sample-inference problems stacked on top of each other:

1. OSM building/construction tagging in Kenya is real but has DOCUMENTED,
   sharply uneven completeness -- some cells will have dozens of mapped
   buildings, others functionally none, and a raw "0 construction sites
   found" is ambiguous between "genuinely nothing being built" and
   "nobody has mapped this cell's buildings at all." Herfort, B.,
   Lautenbach, S., Porto de Albuquerque, J., Anderson, J. & Zipf, A.
   (2023). "A spatio-temporal analysis investigating completeness and
   inequalities of global urban building data in OpenStreetMap." Nature
   Communications, 14, 3985 (DOI: 10.1038/s41467-023-39698-6) is the real,
   peer-reviewed, machine-learning-based measurement of exactly this
   problem at global scale (13,189 urban centers; completeness ranges from
   >80% to <20% depending on the city, correlated with HDI and prior
   humanitarian-mapping activity). This module does not have a
   Kenya-cell-specific completeness score from that paper (it isn't
   published at that resolution and this codebase has no live connection
   to HeiGIT's ohsome API to query it), so it cannot correct for
   under-mapping numerically -- it can only detect the symptom (very few
   total mapped buildings in the widest search radius) and flag low
   confidence rather than silently reporting a falsely precise "0% activity."

2. Even where OSM has SOME coverage, the counts feeding this engine are
   small -- often single digits per radius band. Treating a raw
   proportion like "2 construction sites out of 9 mapped buildings ->
   22.2% development intensity" as a precise number is exactly the kind
   of small-n overconfidence this codebase's other engines already refuse
   to commit (population.py's Fay-Herriot shrinkage, purchasing_power.py's
   Empirical Bayes step, market_gap.py's dispersion-corrected GLM). Two
   real, standard interval-estimation methods are used instead of a bare
   ratio:
     - Wilson, E. B. (1927). "Probable Inference, the Law of Succession,
       and Statistical Inference." Journal of the American Statistical
       Association, 22(158), 209-212. The Wilson score interval for a
       binomial proportion -- chosen over the naive Wald/normal-
       approximation interval specifically because Wald intervals are
       known to perform badly (can even fall outside [0,1]) at exactly
       the small-n, extreme-p regime this engine lives in.
     - Garwood, F. (1936). "Fiducial Limits for the Poisson Distribution."
       Biometrika, 28(3/4), 437-442. The exact ("Garwood"/chi-squared)
       confidence interval for a Poisson count, giving an honest
       uncertainty band around the raw construction-site COUNT itself
       (independent of the stock denominator), via the standard
       count-to-chi-squared-quantile identity implemented directly against
       scipy.stats.chi2 rather than a normal approximation.

3. A single cell-level ratio, however carefully interval-estimated, is
   still built from too little local data to trust on its own when a
   catchment has only 1-3 mapped features. This is precisely the
   small-area-estimation problem population.py's RealAdminHierarchyProvider
   and purchasing_power.py already solve the same statistical way:
   shrink the noisy local estimate toward a more stable prior using
   method-of-moments Empirical Bayes (the simpler normal-normal case of
   the same family as this codebase's Fay-Herriot use in population.py):
   Efron, B. and Morris, C. (1975). "Data Analysis Using Stein's
   Estimator and Its Generalizations." Journal of the American
   Statistical Association, 70(350), 311-319. Implemented independently
   here (not imported from purchasing_power.py) -- same "import for
   plumbing, duplicate for independent statistics" convention
   forced_traffic.py and anchor.py already document for themselves, since
   this signal's noise characteristics (sparse geodata counts) are
   nothing like purchasing_power's (household asset indicators).

   REVISION (post-review): v1 of this shipped with a hand-picked prior
   mean (0.05) and a hand-picked shrinkage weight k=8, i.e. NOT actually
   empirical -- every sparse cell nationwide got pulled toward the same
   invented number regardless of whether that number was remotely right
   for that county. This is not defensible under the same standard
   purchasing_power.py's own module docstring already applies to itself
   ("that is not defensible as 'pure data science' -- it has no basis in
   how much local vs. prior evidence should count"). Fixed the same way
   purchasing_power.py fixes it for its own composite index: the prior
   mean and the between-area variance A are now estimated from this
   engine's OWN accumulated history (FeatureObservation rows this engine
   itself writes, engine_number=12, feature_name="raw_construction_intensity",
   pooled by county) via estimate_between_cell_variance() -- real
   method-of-moments, not a guess -- with gamma = A / (A + D_i) exactly
   as purchasing_power.py's empirical_bayes_shrink computes it, where D_i
   is the real binomial sampling variance of THIS cell's own raw estimate
   (p(1-p)/n), not an arbitrary constant. The 0.05/k=8 pair survives only
   as COLD_START_PRIOR_CONSTRUCTION_INTENSITY, used (and explicitly
   flagged as such in evidence/warnings) only until MIN_COUNTY_PRIOR_SAMPLE_CELLS
   worth of real history exists for a county -- same "conservative
   placeholder until enough real data accumulates" status
   purchasing_power.py's own SyntheticCountyEconomicPriorProvider has,
   not a permanent stand-in.

=============================================================================
THE SPATIAL-GRADIENT SIGNAL: CORE-LED VS. FRINGE-LED DEVELOPMENT
=============================================================================
Beyond a single-radius intensity number, WHERE around the query point
construction is concentrated is itself real information. Urban-expansion
research measures growth as new development occurring disproportionately
at the urbanized edge rather than through infill at the core -- Angel, S.,
Blei, A. M., Parent, J., Lamson-Hall, P., Galarza Sánchez, N., Civco,
D. L., Qian Lei, R. & Thom, K. (2016). "Atlas of Urban Expansion -- 2016
Edition, Volume I: Areas and Densities." NYU Urban Expansion Program,
UN-Habitat, and Lincoln Institute of Land Policy. This module borrows that
core-vs-fringe framing at a much smaller spatial scale (a handful of
catchment radii around one query point, not whole-city built-up-area
polygons over decades -- a real but deliberately modest reuse of the
concept, not a claim to replicate their satellite-based methodology) by
fitting an ordinary-least-squares trend of construction intensity against
log(radius) across DEVELOPMENT_ACTIVITY_RADII_M. A positive slope
(intensity rising with radius) reads as fringe-led development outward
from an already-built core; a negative slope reads as infill/core-led
development right at the query point; near-zero or a poorly-fitting line
(low R²) is reported as uniform/inconclusive rather than forced into
either label.

=============================================================================
FRAMING THE 0-100 SCORE HONESTLY: NO VALIDATED CALIBRATION EXISTS
=============================================================================
The ratio this engine centers on -- active construction relative to
existing mapped building stock in a catchment -- is structurally the same
quantity real-estate economics calls new construction relative to the
existing stock: DiPasquale, D. and Wheaton, W. C. (1992). "The Markets for
Real Estate Assets and Space: A Conceptual Framework." Real Estate
Economics (AREUEA Journal), 20(1), 181-197 -- their four-quadrant model's
"construction" quadrant is literally new supply as a function of the
stock-adjustment relationship. That gives this module a real conceptual
anchor for WHAT it is measuring, but DiPasquale-Wheaton is a market-
equilibrium theory, not a lookup table of "X% construction-to-stock =
score Y" -- there is no published, Kenya-calibrated threshold this module
can cite for "0.15 construction intensity is a high-opportunity score of
75." The 0-100 DEVELOPMENT_ACTIVITY_SCORE below is therefore an explicit,
documented, MONOTONIC-but-uncalibrated transform of the shrunk intensity
estimate (a smooth 0-100 saturating curve, so more real, better-evidenced
construction activity always scores higher, never lower) -- not a claim
that any specific number on the 0-100 scale corresponds to a specific real
outcome. Same "conservative placeholder, not measured" framing
purchasing_power.py already uses for its own uncalibrated priors.

Net decline (demolition/disuse exceeding construction in a band) is
computed and reported in evidence for transparency, but is deliberately
NOT folded into the 0-100 score with any particular weight -- combining
growth and decline into one signed axis would itself need calibration data
this system does not have. Documented, not implemented, gap (same
"documented interface, not implemented" convention as population.py's BYM2
spatial-smoothing hook and business_survival.py's competing-risks
extension).

REVISION (post-review): the point score above is still uncalibrated, but
it is no longer reported alone -- development_activity_score_low/_high
now bracket it by pushing the Wilson 95% interval's lower/upper bound on
the raw construction intensity through the SAME shrinkage-and-transform
pipeline as the point estimate (see compute_development_activity_result).
This is honest propagation of the one real interval this module already
computes (Wilson, per point 2 above) through to the number someone will
actually look at, rather than dead-ending that interval math at the
per-band evidence dict. It is still not a calibrated confidence interval
on "true development outcome" (the 0-100 transform itself remains
uncalibrated, per above) -- it is a calibrated interval on THIS ENGINE'S
OWN ESTIMATE of the underlying intensity, correctly reflecting that a
count of 1 vs 4 construction sites is much less certain than 40 vs 160.

=============================================================================
WHAT'S ACTUALLY AVAILABLE VS. WHAT ISN'T
=============================================================================
Real data used: OSM Overpass, queried live via
google_client.query_osm_overpass() (added alongside this engine -- see
that function's docstring; OSM_OVERPASS_URL did not previously exist in
business_intel/google_client.py, only in property_intel/google_client.py,
so this is a genuinely new, minimal, documented addition to the business_intel
HOW file, not a silent duplicate of unrelated code). One Overpass call per
report per cell (cached the same way Places category search is -- see
services.needs_category_enrichment's sibling check in tasks.py's wiring
note below), covering the widest configured radius; smaller radii are
derived client-side from the same result set by distance filtering, so
this engine never issues more than one live Overpass request per cell,
matching amenity_verification.py's own "Cost control" discipline.

What is explicitly NOT available and is flagged, not faked:
  - No time-series / historical Overpass snapshots. Overpass answers
    "what does OSM say exists right now", not "what changed since last
    quarter." A true growth RATE (not a snapshot ratio) would need either
    (a) this system's own repeated Overpass snapshots accumulated over
    real elapsed months via FeatureObservation (models.py's own versioned
    feature store was built for exactly this -- "internal_derived" is
    already a STALENESS_DAYS_BY_SOURCE_CLASS entry) or (b) a paid
    satellite change-detection provider. Neither is wired up here; this
    engine is honestly a cross-sectional snapshot, and says so in its own
    evidence dict via `temporal_resolution`.
  - `construction_date`/`start_date` tag coverage in OSM is real when
    present (wiki.openstreetmap.org/wiki/Lifecycle_prefix documents
    start_date as "date on which a feature opened or construction of the
    feature finished") but is known to be sparse -- used here only as a
    supplementary, separately-flagged statistic
    (recent_construction_year_count), never substituted into the primary
    intensity ratio.
  - `demolished:building=*` is, per that same OSM wiki page, meant to be
    removed once the structure is no longer visible in current aerial
    imagery -- so a nonzero decline count here is a real but likely
    STALE-leaning signal (mappers are documented to under-maintain this
    tag), flagged as such in evidence rather than presented as current.

KNOWN UPSTREAM GAP (not fixed here, flagged like every other engine flags
gaps it depends on but doesn't own): tasks.py's generate_business_report
does not currently call this engine at all -- it only wires Competition
(8), Market Gap (9), and Anchor (11) into BusinessReport (see this
codebase's own earlier finding that BusinessReport doesn't even have
competition_score/market_gap_score/anchor_score model fields despite
tasks.py assigning them -- the same gap would apply to a
development_activity_score field, which also does not exist on
BusinessReport yet). Wiring this engine into tasks.py and adding the
corresponding BusinessReport fields is a separate, explicitly out-of-scope
task, same "design decision, not a one-line patch" reasoning tasks.py's
own comments already give for not fixing adjacent gaps inline.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import List, Optional, Sequence, Tuple

import numpy as np
from scipy.stats import chi2, norm

logger = logging.getLogger("business_intel")

# =============================================================================
# Constants
# =============================================================================

# Deliberately a 3-point SUBSET of models.DISTANCE_BAND_METERS (which has 7
# bands: 250/500/1000/2000/3000/5000/10000) rather than all of them --
# three cumulative discs is the minimum needed to fit a meaningful
# log(radius) gradient (MIN_BANDS_FOR_GRADIENT_FIT=2 would technically fit
# on two, but two points can't distinguish a real trend from noise; three
# gives one interior check) while keeping this engine's single Overpass
# call's `around:` radius (and therefore its response size and cost) to
# the smallest area that still supports the analysis. If the codebase's
# spec file ever specifies its own required radii, replace this constant
# with those -- see the module-level "spec file" note above for why that
# file isn't available to consult.
DEVELOPMENT_ACTIVITY_RADII_M: Tuple[int, ...] = (500, 1500, 3000)

OVERPASS_QUERY_TIMEOUT_SECONDS = 25

# Below this many total mapped features (construction + decline + stock)
# in a band, a proportion computed from that band is treated as
# insufficient evidence rather than a real signal -- it is still reported
# (with its Wilson interval, which will correctly be very wide) but
# excluded from the gradient fit and from being chosen as the primary band
# estimate's sole basis. Not a citable universal threshold -- a documented,
# conservative engineering choice, same status as competition.py's
# DEFAULT_COMPETITOR_SEARCH_RADIUS_M or market_gap.py's covariate-imputation
# cutoffs.
MIN_MAPPED_FEATURES_FOR_RATIO = 3

MIN_BANDS_FOR_GRADIENT_FIT = 2

# scipy.stats.norm.ppf(0.975), i.e. the exact two-sided 95% z-value --
# computed once at import time rather than hardcoded to a rounded literal.
WILSON_Z_95 = float(norm.ppf(0.975))
POISSON_CI_ALPHA = 0.05

# Cold-start Empirical Bayes prior -- used ONLY when a county has fewer
# than MIN_COUNTY_PRIOR_SAMPLE_CELLS worth of this engine's own real
# accumulated history to pool (see county_construction_prior() and the
# module docstring's "REVISION (post-review)" note). Once enough real
# history exists, the prior mean and shrinkage weight are both estimated
# from that history via method-of-moments -- this constant stops being
# used for that county from that point on. Flagged in evidence/warnings
# whenever it IS used, same "conservative placeholder, not measured"
# status purchasing_power.py's own priors carry.
COLD_START_PRIOR_CONSTRUCTION_INTENSITY = 0.05

# Below this many OTHER cells in the same county with their own latest
# raw_construction_intensity FeatureObservation on record, a county-level
# prior is not trusted enough to estimate a real between-cell variance A
# from (same reasoning as purchasing_power.py's MIN_PCA_FIT_SAMPLE: a
# variance estimated from too few points is itself mostly noise) --
# falls back to the cold-start prior instead, flagged as such.
MIN_COUNTY_PRIOR_SAMPLE_CELLS = 5

# Cold-start between-cell variance default, used only alongside
# COLD_START_PRIOR_CONSTRUCTION_INTENSITY before a county has enough real
# history to estimate its own A. A=0.0 would force gamma=0 (100% weight
# on the prior, 0% on this cell's own Overpass evidence) for every cell
# until MIN_COUNTY_PRIOR_SAMPLE_CELLS is reached -- silently discarding
# real local evidence rather than shrinking it. This default instead
# gives roughly the same local-vs-prior balance v1's hand-picked k=8 gave
# at small n (worked backward from gamma(n=1) ~ 1/(1+8) against a typical
# cold-start D_i), so cold start behaves like "conservative shrinkage,"
# not "local data ignored." Same documented-default status as
# purchasing_power.py's own indicator_std_estimate=15.0 -- an engineering
# choice, not a citable constant; superseded automatically the moment a
# county has real pooled history.
COLD_START_BETWEEN_CELL_VARIANCE = 0.006

# feature_name this engine writes/reads for FeatureObservation-based
# county pooling (see county_construction_prior()). A named constant so
# the write side (compute_development_activity_score) and the read side
# (county_construction_prior) can't silently drift apart.
FEATURE_NAME_RAW_INTENSITY = "raw_construction_intensity"

# Documented, conservative default for a single cell's own sampling
# variance when its raw construction intensity is exactly 0 or 1 (the
# binomial variance p(1-p)/n is exactly 0 there, which would imply -- 
# wrongly -- perfect certainty). Same role as purchasing_power.py's own
# indicator_std_estimate default for an analogous edge case.
MIN_SAMPLING_VARIANCE = 1e-4

# Saturating-curve scale for the uncalibrated score transform -- see
# "FRAMING THE 0-100 SCORE HONESTLY" above. Larger SCALE = a given
# intensity maps to a lower score (curve saturates more slowly).
SCORE_SATURATION_SCALE = 0.12

# OSM tag keys recognized as the "lifecycle prefix" decline namespace, per
# wiki.openstreetmap.org/wiki/Lifecycle_prefix. Matched as exact keys
# (namespace + "building") rather than any key merely starting with these
# strings, so an unrelated tag like "disused:shop" (a different feature
# type entirely) is not misclassified as a building-decline signal.
_DECLINE_TAG_KEYS = ("demolished:building", "disused:building", "was:building")

_DATE_TAG_KEYS = ("construction_date", "start_date")
_YEAR_RE = re.compile(r"(1[89]\d{2}|20\d{2})")


# =============================================================================
# Pure data structures -- no Django/network import at module scope, so this
# file's math is unit-testable without a DB, matching competition.py's
# "pure-math layer above without a DB" split.
# =============================================================================

@dataclass
class BandObservation:
    radius_m: int
    n_construction: int
    n_decline: int
    n_stock: int

    @property
    def n_total_mapped(self) -> int:
        return self.n_construction + self.n_decline + self.n_stock

    @property
    def net_development_count(self) -> int:
        return self.n_construction - self.n_decline

    @property
    def sample_sufficient(self) -> bool:
        return self.n_total_mapped >= MIN_MAPPED_FEATURES_FOR_RATIO

    @property
    def construction_intensity(self) -> Optional[float]:
        """Active construction as a fraction of (construction + existing
        stock) -- deliberately excludes decline-tagged features from the
        denominator, since a demolished building is no longer part of the
        'existing stock' a new construction site is being compared against.
        The DiPasquale-Wheaton "new construction relative to stock" ratio
        this module cites (see module docstring) is defined this way."""
        denom = self.n_construction + self.n_stock
        if denom == 0:
            return None
        return self.n_construction / denom

    @property
    def intensity_wilson_interval(self) -> Tuple[Optional[float], Optional[float]]:
        denom = self.n_construction + self.n_stock
        if denom == 0:
            return None, None
        return wilson_score_interval(self.n_construction, denom)

    @property
    def construction_count_poisson_interval(self) -> Tuple[float, float]:
        return garwood_poisson_ci(self.n_construction)


@dataclass
class DevelopmentActivityResult:
    bands: List[BandObservation]
    primary_band_radius_m: int
    raw_construction_intensity: Optional[float]
    shrunk_construction_intensity: Optional[float]
    shrinkage_weight_alpha: Optional[float]
    prior_mean_used: Optional[float]
    prior_source: str
    between_cell_variance_A: Optional[float]
    county_prior_n_cells_pooled: int
    gradient_slope: Optional[float]
    gradient_r_squared: Optional[float]
    gradient_interpretation: str
    recent_construction_year_count: int
    development_activity_score: Optional[float]
    development_activity_score_low: Optional[float]
    development_activity_score_high: Optional[float]
    data_sparse: bool
    warnings: List[str] = field(default_factory=list)


# =============================================================================
# Real, cited statistical primitives
# =============================================================================

def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Duplicated locally rather than imported -- same 'import for
    plumbing, duplicate for independent statistics' convention
    competition.py/forced_traffic.py already establish for this exact
    function in this codebase."""
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def wilson_score_interval(successes: int, n: int, z: float = WILSON_Z_95) -> Tuple[Optional[float], Optional[float]]:
    """Wilson (1927) score interval for a binomial proportion. Chosen over
    the naive Wald/normal-approximation interval because Wald intervals are
    documented to perform badly (coverage collapses, bounds can leave
    [0,1]) exactly in the small-n, extreme-p regime this engine operates
    in -- single-digit `n` per catchment band is the norm here, not the
    exception."""
    if n <= 0:
        return None, None
    p = successes / n
    denom = 1 + (z ** 2) / n
    center = (p + (z ** 2) / (2 * n)) / denom
    margin = (z / denom) * math.sqrt((p * (1 - p) / n) + (z ** 2) / (4 * n ** 2))
    return max(0.0, center - margin), min(1.0, center + margin)


def garwood_poisson_ci(count: int, alpha: float = POISSON_CI_ALPHA) -> Tuple[float, float]:
    """Garwood (1936) exact ("fiducial") confidence interval for a Poisson
    count, via the standard count <-> chi-squared-quantile identity:
        lower = chi2.ppf(alpha/2, 2*count) / 2          (0 if count == 0)
        upper = chi2.ppf(1 - alpha/2, 2*(count+1)) / 2
    Verified against published reference tables during development (e.g.
    count=0 -> (0, 3.6889) and count=4 -> (1.0899, 10.2416) at alpha=0.05,
    both standard textbook values) rather than trusted from memory alone."""
    if count < 0:
        raise ValueError("count must be >= 0")
    lower = 0.0 if count == 0 else float(chi2.ppf(alpha / 2, 2 * count) / 2)
    upper = float(chi2.ppf(1 - alpha / 2, 2 * (count + 1)) / 2)
    return lower, upper


def wilson_variance_from_counts(successes: int, n: int, z: float = WILSON_Z_95) -> float:
    """D_i, this cell's own sampling variance of its construction-intensity
    estimate, used as the per-cell noise term in Empirical Bayes shrinkage
    -- replacing v1's arbitrary hand-picked k. Uses the Wilson-interval's
    OWN internal variance term (p_tilde*(1-p_tilde)/(n+z^2), where
    p_tilde=(successes+z^2/2)/(n+z^2)) rather than the naive binomial
    p(1-p)/n. This matters concretely at the small n this engine actually
    sees: naive p(1-p)/n collapses to ~0 whenever successes==0 or
    successes==n (e.g. a single construction site out of 1 mapped feature
    gives p=1.0, variance=0 -- implying perfect certainty from ONE
    observation, exactly the failure mode Wilson (1927) is already cited
    in this module to avoid for the interval itself). Reusing that same
    correction here keeps the interval and the shrinkage weight internally
    consistent, and needs no new citation since it's the identical
    quantity wilson_score_interval already computes. Floored at
    MIN_SAMPLING_VARIANCE only as a divide-by-zero guard for n<=0."""
    if n <= 0:
        return float("inf")
    p_tilde = (successes + (z ** 2) / 2) / (n + z ** 2)
    return max(p_tilde * (1 - p_tilde) / (n + z ** 2), MIN_SAMPLING_VARIANCE)


def estimate_between_cell_variance(direct_estimates: Sequence[float], sampling_variances: Sequence[float]) -> float:
    """Method-of-moments estimate of A, the true between-cell variance of
    construction intensity within a county, net of each cell's own
    sampling noise -- SAME technique and same clip-to-zero convention as
    purchasing_power.py's estimate_between_area_variance (Efron & Morris
    1975 family; population.py's Fay-Herriot A uses the same method-of-
    moments idea). Reimplemented independently here (see module docstring's
    'import for plumbing, duplicate for independent statistics' convention)
    since this signal's counts/variances are unrelated to purchasing_power's
    household-asset indicators. A negative raw estimate (expected with few
    or noisy cells) is clipped to 0, meaning 'shrink fully to the prior' --
    not an error."""
    if len(direct_estimates) < 2:
        return 0.0
    y = np.array(direct_estimates, dtype=float)
    d_bar = float(np.mean(sampling_variances)) if sampling_variances else 0.0
    sample_var = float(np.var(y, ddof=1))
    return max(0.0, sample_var - d_bar)


def empirical_bayes_shrink(
    raw_estimate: Optional[float],
    sampling_variance_D: float,
    prior_mean: Optional[float],
    between_cell_variance_A: float,
) -> Tuple[Optional[float], Optional[float]]:
    """theta_hat = gamma * raw_estimate + (1 - gamma) * prior_mean,
    gamma = A / (A + D) -- SAME formula and role as purchasing_power.py's
    own empirical_bayes_shrink (Efron & Morris 1975 / James-Stein family),
    reimplemented independently per this module's established 'duplicate
    for independent statistics' convention. `sampling_variance_D` is the
    caller-supplied real per-cell sampling variance (see
    wilson_variance_from_counts) -- passed in rather than computed here so
    the SAME D can be reused to shrink both the point estimate and its
    Wilson interval bounds with one consistent gamma (see
    compute_development_activity_result).

    Returns (composite, gamma). If raw_estimate is None, returns
    (prior_mean, 0.0). If prior_mean is None, returns (raw_estimate, 1.0).
    If both are None, returns (None, None)."""
    if raw_estimate is None and prior_mean is None:
        return None, None
    if raw_estimate is None:
        return prior_mean, 0.0
    if prior_mean is None:
        return raw_estimate, 1.0
    denom = between_cell_variance_A + sampling_variance_D
    gamma = (between_cell_variance_A / denom) if denom > 0 else 0.0
    shrunk = gamma * raw_estimate + (1 - gamma) * prior_mean
    return shrunk, gamma


def fit_intensity_gradient(points: Sequence[Tuple[float, float]]) -> Tuple[Optional[float], Optional[float]]:
    """Ordinary least squares of intensity ~ ln(radius_m) over the supplied
    (radius_m, intensity) points. Standard closed-form OLS (no external
    citation needed -- this is textbook least squares, not a novel or
    debatable method choice); the interpretive framing of the resulting
    slope as a core-vs-fringe development signal is what's cited to Angel
    et al. (2016) in the module docstring, not the OLS mechanics
    themselves. Returns (slope, r_squared); (None, None) if fewer than 2
    points or the x-values are degenerate (all equal radius, which cannot
    happen with this module's fixed DEVELOPMENT_ACTIVITY_RADII_M but is
    guarded anyway since this function is also unit-tested directly)."""
    if len(points) < MIN_BANDS_FOR_GRADIENT_FIT:
        return None, None
    x = np.array([math.log(r) for r, _ in points], dtype=float)
    y = np.array([v for _, v in points], dtype=float)
    x_mean, y_mean = x.mean(), y.mean()
    ss_xx = float(((x - x_mean) ** 2).sum())
    if ss_xx == 0:
        return None, None
    slope = float(((x - x_mean) * (y - y_mean)).sum() / ss_xx)
    intercept = y_mean - slope * x_mean
    y_pred = slope * x + intercept
    ss_res = float(((y - y_pred) ** 2).sum())
    ss_tot = float(((y - y_mean) ** 2).sum())
    r_squared = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return slope, r_squared


# Slope magnitude below which the gradient is called "uniform" even if the
# fit is otherwise clean -- an engineering threshold (not a citable
# statistical constant), chosen so that noise-level slopes on a 0-1
# intensity scale across ~2 log-radius units don't get over-interpreted as
# a directional trend.
_GRADIENT_FLAT_THRESHOLD = 0.01
_GRADIENT_MIN_R_SQUARED = 0.5


def classify_gradient(slope: Optional[float], r_squared: Optional[float]) -> str:
    if slope is None or r_squared is None:
        return "insufficient_data"
    if r_squared < _GRADIENT_MIN_R_SQUARED or abs(slope) < _GRADIENT_FLAT_THRESHOLD:
        return "uniform"
    return "fringe_led" if slope > 0 else "core_led"


def score_from_intensity(shrunk_intensity: Optional[float]) -> Optional[float]:
    """Monotonic, explicitly uncalibrated saturating transform to 0-100 --
    see module docstring's 'FRAMING THE 0-100 SCORE HONESTLY' section for
    why no linear or externally-validated mapping is used instead."""
    if shrunk_intensity is None:
        return None
    return round(100.0 * (1.0 - math.exp(-max(0.0, shrunk_intensity) / SCORE_SATURATION_SCALE)), 2)


# =============================================================================
# OSM tag classification
# =============================================================================

def classify_element(tags: dict) -> Optional[str]:
    """Classifies one Overpass element's tag dict into 'construction',
    'decline', 'stock', or None (not relevant to this engine, e.g. a
    node/way returned by the query's broad tag filters that turns out to
    be neither a building nor a construction/lifecycle-prefixed feature --
    Overpass tag-key filters can return near-misses; this function is the
    honest boundary, not the query itself)."""
    if not tags:
        return None

    for key in _DECLINE_TAG_KEYS:
        if key in tags:
            return "decline"
    # Deprecated but still real and present in live data: a bare
    # `demolished=*` tag co-occurring with a `building=*` tag, per
    # OpenStreetMap Wiki's Proposal:Demolished page (obsoleted in favor of
    # the demolished:building=* namespace form above, but not purged from
    # the live database).
    if tags.get("building") and tags.get("demolished"):
        return "decline"

    if tags.get("building") == "construction":
        return "construction"
    if tags.get("landuse") == "construction":
        return "construction"
    if "construction" in tags:
        return "construction"

    if tags.get("building"):
        return "stock"

    return None


def parse_construction_year(tags: dict) -> Optional[int]:
    """Extracts a real observed year from OSM's start_date/construction_date
    lifecycle tags where present (wiki.openstreetmap.org/wiki/Lifecycle_prefix:
    'start_date -- date on which a feature opened or construction of the
    feature finished'; construction_date is the more specific alternative).
    Coverage of these tags in OSM is known to be sparse -- this is used only
    as a supplementary statistic, never substituted into the primary
    intensity ratio (see module docstring)."""
    for key in _DATE_TAG_KEYS:
        value = tags.get(key)
        if not value:
            continue
        match = _YEAR_RE.search(str(value))
        if match:
            return int(match.group(1))
    return None


# =============================================================================
# Overpass query construction (pure string building -- the actual HTTP call
# lives in google_client.query_osm_overpass, per services.py's WHEN/HOW split)
# =============================================================================

def build_overpass_query(lat: float, lng: float, radius_m: int, timeout: int = OVERPASS_QUERY_TIMEOUT_SECONDS) -> str:
    """One Overpass QL query covering the widest configured radius, unioning
    node+way elements across all tag classes this engine cares about
    (existing buildings, explicit construction tags, and the lifecycle
    decline namespace). Smaller radii in DEVELOPMENT_ACTIVITY_RADII_M are
    derived client-side from this single result set by distance filtering
    (see bucket_elements_by_radius) rather than issued as separate Overpass
    calls -- one live request per cell, matching
    property_intel/amenity_verification.py's own 'Cost control' discipline."""
    clauses = "".join([
        f'node["building"](around:{radius_m},{lat},{lng});',
        f'way["building"](around:{radius_m},{lat},{lng});',
        f'node["construction"](around:{radius_m},{lat},{lng});',
        f'way["construction"](around:{radius_m},{lat},{lng});',
        f'way["landuse"="construction"](around:{radius_m},{lat},{lng});',
        f'node["demolished:building"](around:{radius_m},{lat},{lng});',
        f'way["demolished:building"](around:{radius_m},{lat},{lng});',
        f'node["disused:building"](around:{radius_m},{lat},{lng});',
        f'way["disused:building"](around:{radius_m},{lat},{lng});',
        f'node["was:building"](around:{radius_m},{lat},{lng});',
        f'way["was:building"](around:{radius_m},{lat},{lng});',
    ])
    return f'[out:json][timeout:{timeout}];({clauses});out center tags;'


def _element_latlng(element: dict) -> Optional[Tuple[float, float]]:
    if element.get("type") == "node":
        lat, lng = element.get("lat"), element.get("lon")
    else:
        center = element.get("center") or {}
        lat, lng = center.get("lat"), center.get("lon")
    if lat is None or lng is None:
        return None
    return float(lat), float(lng)


def bucket_elements_by_radius(
    elements: Sequence[dict],
    center_lat: float,
    center_lng: float,
    radii_m: Sequence[int] = DEVELOPMENT_ACTIVITY_RADII_M,
) -> List[BandObservation]:
    """Classifies every element once, computes its distance from the cell
    center once, then assigns it into every configured band whose radius is
    >= that distance -- bands are cumulative discs from the center (same
    semantics as models.Catchment's 'radius' catchment_type: 'Fixed-radius
    circular band', a full disc, not an annulus), so counts are naturally
    non-decreasing as radius grows and every band is a complete, valid
    catchment on its own, not a difference of two others."""
    sorted_radii = sorted(radii_m)
    counts = {r: {"construction": 0, "decline": 0, "stock": 0} for r in sorted_radii}
    recent_years = 0

    for el in elements:
        tags = el.get("tags") or {}
        kind = classify_element(tags)
        if kind is None:
            continue
        latlng = _element_latlng(el)
        if latlng is None:
            continue
        distance_m = _haversine_m(center_lat, center_lng, latlng[0], latlng[1])
        for r in sorted_radii:
            if distance_m <= r:
                counts[r][kind] += 1
        if kind in ("construction", "stock") and parse_construction_year(tags) is not None:
            recent_years += 1

    bands = [
        BandObservation(
            radius_m=r,
            n_construction=counts[r]["construction"],
            n_decline=counts[r]["decline"],
            n_stock=counts[r]["stock"],
        )
        for r in sorted_radii
    ]
    return bands, recent_years


# =============================================================================
# Assembly: pure-math result builder (no Django import -- unit-testable
# directly, matching competition.py's compute_competition_result /
# compute_competition_score split)
# =============================================================================

def compute_development_activity_result(
    elements: Sequence[dict],
    center_lat: float,
    center_lng: float,
    radii_m: Sequence[int] = DEVELOPMENT_ACTIVITY_RADII_M,
    prior_mean: float = COLD_START_PRIOR_CONSTRUCTION_INTENSITY,
    prior_source: str = "cold_start_placeholder",
    between_cell_variance_A: float = COLD_START_BETWEEN_CELL_VARIANCE,
    county_prior_n_cells_pooled: int = 0,
) -> DevelopmentActivityResult:
    """`prior_mean` / `between_cell_variance_A` / `prior_source` are
    normally supplied by county_construction_prior() (real, pooled from
    this engine's own accumulated FeatureObservation history -- see
    module docstring). The defaults here are the cold-start placeholder
    ONLY, kept so this function stays independently callable/unit-testable
    without a DB, matching this module's pure-math-layer convention --
    compute_development_activity_score (the Django-facing entry point)
    always calls county_construction_prior() first and passes its real
    result in rather than relying on these defaults."""
    bands, recent_construction_year_count = bucket_elements_by_radius(elements, center_lat, center_lng, radii_m)
    warnings: List[str] = []

    primary = bands[0]  # smallest configured radius: most locally relevant to the exact query point
    widest = bands[-1]

    if widest.n_total_mapped < MIN_MAPPED_FEATURES_FOR_RATIO:
        warnings.append(
            f"Only {widest.n_total_mapped} building/construction feature(s) mapped in OSM within "
            f"{widest.radius_m}m -- OSM building-footprint completeness is documented to vary sharply "
            "by region (Herfort et al. 2023, Nature Communications) and is not independently verified "
            "for this specific cell. A low count here may reflect genuinely little development, or "
            "simply that this area has not been comprehensively mapped -- treat this engine's output "
            "as low-confidence, not as evidence of stagnation."
        )
    data_sparse = widest.n_total_mapped < MIN_MAPPED_FEATURES_FOR_RATIO

    if prior_source == "cold_start_placeholder":
        warnings.append(
            f"County-level prior used {MIN_COUNTY_PRIOR_SAMPLE_CELLS} or fewer real prior cells "
            "(cold start) -- falling back to a fixed, unmeasured placeholder prior "
            f"({COLD_START_PRIOR_CONSTRUCTION_INTENSITY}) rather than a real county-calibrated one. "
            "This will self-correct as this engine accumulates more real FeatureObservation history "
            "for this county; not a permanent limitation."
        )

    raw_intensity = primary.construction_intensity
    denom_n = primary.n_construction + primary.n_stock
    wilson_low, wilson_high = primary.intensity_wilson_interval

    if raw_intensity is None:
        warnings.append(
            f"No construction- or stock-classified buildings found within the primary "
            f"{primary.radius_m}m radius -- construction intensity is undefined (not zero) for that band."
        )
        shrunk_intensity, alpha = None, None
        shrunk_low, shrunk_high = None, None
    else:
        D_i = wilson_variance_from_counts(primary.n_construction, denom_n)
        shrunk_intensity, alpha = empirical_bayes_shrink(raw_intensity, D_i, prior_mean, between_cell_variance_A)
        # Propagate the SAME real interval already computed for this band
        # (Wilson, point 2 in the module docstring) through the identical
        # shrinkage transform (same D_i, same gamma) used for the point
        # estimate, rather than dead-ending that interval math at the
        # per-band evidence dict -- see module docstring's
        # 'REVISION (post-review)' note under 'FRAMING THE 0-100 SCORE
        # HONESTLY'.
        shrunk_low, _ = empirical_bayes_shrink(wilson_low, D_i, prior_mean, between_cell_variance_A)
        shrunk_high, _ = empirical_bayes_shrink(wilson_high, D_i, prior_mean, between_cell_variance_A)

    gradient_points = [
        (b.radius_m, b.construction_intensity)
        for b in bands
        if b.sample_sufficient and b.construction_intensity is not None
    ]
    slope, r_squared = fit_intensity_gradient(gradient_points)
    if slope is None:
        warnings.append(
            f"Fewer than {MIN_BANDS_FOR_GRADIENT_FIT} bands had sufficient mapped features "
            f"(>= {MIN_MAPPED_FEATURES_FOR_RATIO}) to fit a core-vs-fringe gradient; "
            "gradient_interpretation is 'insufficient_data', not a real trend estimate."
        )
    gradient_interpretation = classify_gradient(slope, r_squared)

    if primary.n_decline > primary.n_construction:
        warnings.append(
            f"Net decline signal in the primary {primary.radius_m}m band: {primary.n_decline} "
            f"demolished/disused-building feature(s) vs {primary.n_construction} active construction "
            "feature(s). Not folded into development_activity_score (see module docstring) -- reported "
            "here for transparency. Note OSM's demolished:building=* tag is documented to be under-"
            "maintained once a structure leaves current aerial imagery, so this may be a stale signal."
        )

    score = score_from_intensity(shrunk_intensity)
    score_low = score_from_intensity(shrunk_low) if raw_intensity is not None else None
    score_high = score_from_intensity(shrunk_high) if raw_intensity is not None else None

    return DevelopmentActivityResult(
        bands=bands,
        primary_band_radius_m=primary.radius_m,
        raw_construction_intensity=raw_intensity,
        shrunk_construction_intensity=shrunk_intensity,
        shrinkage_weight_alpha=alpha,
        prior_mean_used=prior_mean,
        prior_source=prior_source,
        between_cell_variance_A=between_cell_variance_A,
        county_prior_n_cells_pooled=county_prior_n_cells_pooled,
        gradient_slope=slope,
        gradient_r_squared=r_squared,
        gradient_interpretation=gradient_interpretation,
        recent_construction_year_count=recent_construction_year_count,
        development_activity_score=score,
        development_activity_score_low=score_low,
        development_activity_score_high=score_high,
        data_sparse=data_sparse,
        warnings=warnings,
    )


def development_activity_evidence(result: DevelopmentActivityResult) -> dict:
    """Structured evidence dict, matching the shape
    engines.evidence / models.FeatureObservation.value_json expects and the
    same field-naming style as competition_evidence()/market_gap_evidence():
    every number a human (or the Decision engine) would need to see WHY the
    score came out the way it did."""
    return {
        "primary_band_radius_m": result.primary_band_radius_m,
        "raw_construction_intensity": (
            round(result.raw_construction_intensity, 4) if result.raw_construction_intensity is not None else None
        ),
        "shrunk_construction_intensity": (
            round(result.shrunk_construction_intensity, 4) if result.shrunk_construction_intensity is not None else None
        ),
        "shrinkage_weight_alpha": (
            round(result.shrinkage_weight_alpha, 3) if result.shrinkage_weight_alpha is not None else None
        ),
        "prior_mean_used": round(result.prior_mean_used, 4) if result.prior_mean_used is not None else None,
        "prior_source": result.prior_source,
        "between_cell_variance_A": (
            round(result.between_cell_variance_A, 6) if result.between_cell_variance_A is not None else None
        ),
        "county_prior_n_cells_pooled": result.county_prior_n_cells_pooled,
        "development_activity_score_0_100": result.development_activity_score,
        "development_activity_score_95ci": [result.development_activity_score_low, result.development_activity_score_high],
        "gradient_slope_intensity_per_ln_radius": (
            round(result.gradient_slope, 5) if result.gradient_slope is not None else None
        ),
        "gradient_r_squared": (round(result.gradient_r_squared, 3) if result.gradient_r_squared is not None else None),
        "gradient_interpretation": result.gradient_interpretation,
        "recent_construction_year_tagged_count": result.recent_construction_year_count,
        "data_sparse": result.data_sparse,
        "temporal_resolution": "cross_sectional_snapshot",
        "bands": [
            {
                "radius_m": b.radius_m,
                "n_construction": b.n_construction,
                "n_decline": b.n_decline,
                "n_stock": b.n_stock,
                "n_total_mapped": b.n_total_mapped,
                "net_development_count": b.net_development_count,
                "construction_intensity": (
                    round(b.construction_intensity, 4) if b.construction_intensity is not None else None
                ),
                "construction_intensity_wilson_95ci": [
                    round(v, 4) if v is not None else None for v in b.intensity_wilson_interval
                ],
                "construction_count_poisson_95ci": [round(v, 3) for v in b.construction_count_poisson_interval],
                "sample_sufficient": b.sample_sufficient,
            }
            for b in result.bands
        ],
        "methods": [
            "osm_overpass_lifecycle_prefix_tag_classification",
            "wilson_score_interval",
            "garwood_exact_poisson_ci",
            "empirical_bayes_shrinkage_method_of_moments_county_pooled",
            "ols_log_radius_intensity_gradient",
        ],
        "warnings": result.warnings,
    }


# =============================================================================
# Django-facing: county-pooled Empirical Bayes prior (REVISION post-review)
# =============================================================================

ENGINE_NUMBER = 12

METRIC_FIELDS = [
    ("raw_construction_intensity", "ratio_0_1", "numeric"),
    ("shrunk_construction_intensity", "ratio_0_1", "numeric"),
    ("development_activity_score", "index_0_100", "numeric"),
]


def county_construction_prior(county: str, exclude_cell_id=None):
    """Real Empirical-Bayes prior for county `county`, pooled from this
    engine's OWN accumulated FeatureObservation history -- i.e. every
    other cell in the same county this engine has already scored, via
    feature_name=FEATURE_NAME_RAW_INTENSITY, engine_number=ENGINE_NUMBER,
    is_latest=True. Same role as purchasing_power.py's
    CountyEconomicPriorProvider, but built from this engine's own
    accumulated runs (internal_derived) rather than an external published
    survey, since no published county-level construction-activity survey
    exists for this system to cite (see module docstring's data-source
    discussion) -- this is the honest, available substitute: let the
    system learn its own county-level baseline over time, the same way
    purchasing_power.py's PCAWealthIndexModel is fit fresh from whatever
    corpus currently exists.

    Returns (prior_mean, between_cell_variance_A, prior_source, n_cells_pooled).
    prior_source is "county_pooled_empirical" once MIN_COUNTY_PRIOR_SAMPLE_CELLS
    is met, else "cold_start_placeholder" -- callers must check this string,
    not just the numeric value, since a cold-start prior and a real one can
    coincidentally be numerically close.
    """
    from ..models import FeatureObservation  # local import: keep this module's pure-math layer DB-import-free

    qs = FeatureObservation.objects.filter(
        engine_number=ENGINE_NUMBER,
        feature_name=FEATURE_NAME_RAW_INTENSITY,
        is_latest=True,
        location_cell__county=county,
    )
    if exclude_cell_id is not None:
        qs = qs.exclude(location_cell_id=exclude_cell_id)

    rows = list(qs.values_list("value_numeric", "sample_size"))
    if len(rows) < MIN_COUNTY_PRIOR_SAMPLE_CELLS:
        return COLD_START_PRIOR_CONSTRUCTION_INTENSITY, COLD_START_BETWEEN_CELL_VARIANCE, "cold_start_placeholder", len(rows)

    direct_estimates = [float(v) for v, _n in rows]
    # sample_size stored per row is denom_n (n_construction + n_stock) at
    # write time; successes isn't stored separately, so it's reconstructed
    # as round(value * n) -- exact given how value was computed and
    # written (a straight ratio of two integer counts), not an
    # approximation of anything this system doesn't already know.
    sampling_variances = [
        wilson_variance_from_counts(round(float(v) * (n or 1)), n or 1) for v, n in rows
    ]
    prior_mean = float(np.mean(direct_estimates))
    A_hat = estimate_between_cell_variance(direct_estimates, sampling_variances)
    return prior_mean, A_hat, "county_pooled_empirical", len(rows)


# =============================================================================
# Django-facing entry point
# =============================================================================

def compute_development_activity_score(query, radii_m: Sequence[int] = DEVELOPMENT_ACTIVITY_RADII_M) -> tuple:
    """Real Django-facing Engine 12 entry point, matching the (score,
    evidence) tuple shape compute_competition_score / compute_market_gap_score
    / compute_anchor_score already return.

    REVISION (post-review): this now (1) issues at most one live Overpass
    request per (cell, purpose, radius) -- google_client.query_osm_overpass
    itself caches via OSMOverpassQueryCache and retries transient failures
    with backoff, so the "callers are responsible for caching" gap this
    function used to document is closed, not just documented; (2) pools a
    real county-level Empirical Bayes prior via county_construction_prior()
    instead of a hand-picked constant; and (3) writes its own
    FeatureObservation rows (raw_construction_intensity, shrunk_
    construction_intensity, development_activity_score) plus an EngineRun
    row, matching purchasing_power.py's run() contract -- this is also
    what makes future county pooling possible at all (county_construction_prior
    reads exactly the rows this function writes).
    """
    from ..google_client import query_osm_overpass  # local import: keep this module DB/network-import-free for pure-math callers/tests
    from ..models import EngineRun, FeatureObservation
    from django.utils import timezone as _timezone

    started_at = _timezone.now()
    cell = query.location_cell
    lat, lng = float(cell.center_latitude), float(cell.center_longitude)
    county = getattr(cell, "county", "") or ""

    run_row = EngineRun.objects.create(query=query, engine_number=ENGINE_NUMBER, status="succeeded", started_at=started_at)

    widest_radius = max(radii_m)
    overpass_query = build_overpass_query(lat, lng, widest_radius)
    elements, succeeded = query_osm_overpass(cell, overpass_query, purpose="development_activity", radius_m=widest_radius)

    if not succeeded and not elements:
        msg = "OSM Overpass query failed or returned no data (all endpoints, after retries) -- Development Activity engine could not run."
        logger.warning("Development Activity engine: %s (cell=%s)", msg, cell.geohash)
        run_row.status = "failed"
        run_row.error_message = msg
        run_row.finished_at = _timezone.now()
        run_row.save(update_fields=["status", "error_message", "finished_at"])
        return None, {"warnings": [msg], "engine_run_status": "failed"}

    prior_mean, between_cell_variance_A, prior_source, n_pooled = (
        county_construction_prior(county, exclude_cell_id=cell.id) if county
        else (COLD_START_PRIOR_CONSTRUCTION_INTENSITY, COLD_START_BETWEEN_CELL_VARIANCE, "cold_start_placeholder", 0)
    )

    result = compute_development_activity_result(
        elements, lat, lng, radii_m=radii_m,
        prior_mean=prior_mean, prior_source=prior_source,
        between_cell_variance_A=between_cell_variance_A, county_prior_n_cells_pooled=n_pooled,
    )
    if not county:
        result.warnings.append("No county on record for this cell -- county-level prior pooling not possible; used cold-start placeholder.")

    evidence = development_activity_evidence(result)
    evidence["engine_run_status"] = "ok" if not result.data_sparse else "ok_data_sparse"

    features_written = 0
    denom_n = None
    if result.raw_construction_intensity is not None:
        primary = result.bands[0]
        denom_n = primary.n_construction + primary.n_stock

    for field_name, unit, value_type in METRIC_FIELDS:
        value = getattr(result, field_name)
        if value is None:
            continue
        uncertainty = None
        if field_name == "development_activity_score" and result.development_activity_score_low is not None:
            uncertainty = (result.development_activity_score_high - result.development_activity_score_low) / 2.0
        FeatureObservation.write_latest(
            location_cell=cell, engine_number=ENGINE_NUMBER, feature_name=field_name,
            defaults=dict(
                value_type=value_type,
                value_numeric=Decimal(str(round(value, 6))),
                unit=unit,
                observed_at=started_at,
                source="business_intel.engines.development_activity",
                source_class="internal_derived",
                method="empirical_bayes_shrinkage_method_of_moments_county_pooled" if field_name != "raw_construction_intensity" else "osm_overpass_binomial_proportion",
                derivation_level="model_estimate" if field_name != "raw_construction_intensity" else "calculation",
                sample_size=denom_n,
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

    return result.development_activity_score, evidence

# =============================================================================
# Django-facing run() -- matches the {"engine_run": ...} dict contract every
# other wired engine's run() returns (see competition.run() for the pattern
# this mirrors). compute_development_activity_score() above already creates
# the EngineRun row, writes every FeatureObservation, and sets the row's
# final status/degraded_reason -- this wrapper just calls it and hands that
# same row back in the shape tasks.py's _run_engine() expects, instead of
# tasks.py getting a bare (score, evidence) tuple it can't introspect.
#
# NOTE left deliberately visible, not fixed silently here: the FeatureObservation
# rows compute_development_activity_score() writes use
# derivation_level="model_estimate"/"calculation", never "business_conclusion".
# Engine 26 (Decision) only discovers business_conclusion-level numeric
# features as ranking criteria -- so wiring this run() into tasks.py makes
# Development Activity actually execute and persist, but it will NOT yet
# show up as a Decision criterion until/unless that derivation_level choice
# is revisited. That's a scoring-policy decision, not a wiring bug, so it's
# left for whoever owns that call rather than changed here.
# =============================================================================

def run(query, radii_m: Sequence[int] = DEVELOPMENT_ACTIVITY_RADII_M) -> dict:
    """Engine 12 entry point. Thin wrapper around
    compute_development_activity_score(query, radii_m) -- see that function's
    docstring for what it actually computes/persists. Returns a dict shaped
    like every other wired engine's run() (score, evidence, engine_run),
    so tasks.py's _run_engine() can call it interchangeably with
    competition.run() / anchor.run() / etc.
    """
    from ..models import EngineRun

    cell = query.location_cell
    score, evidence = compute_development_activity_score(query, radii_m=radii_m)

    # compute_development_activity_score() already created and saved this
    # row (status/finished_at/degraded_reason all set) -- fetch it back
    # rather than duplicating that bookkeeping here.
    run_row = (
        EngineRun.objects.filter(query=query, engine_number=ENGINE_NUMBER)
        .order_by("-started_at")
        .first()
    )

    return {"cell": cell, "score": score, "evidence": evidence, "engine_run": run_row}
