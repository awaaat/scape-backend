"""
business_intel/engines/economic_momentum.py

Engine 18 -- Economic Momentum Engine
Per business_location_intelligence_engines_data_models_v2.md, Section 18.

NOTE ON THE SPEC FILE: business_location_intelligence_engines_data_models_v2.md
is referenced by every engine stub in this codebase but does not exist on
disk (confirmed via `find`, same finding population.py / purchasing_power.py /
competition.py / development_activity.py already documented). Built without
it, matching those engines' structure, schema contract, and citation
discipline. Anywhere this would otherwise need to guess at the missing
spec, it says so instead of guessing silently.

=============================================================================
PURPOSE (per the stub this replaces, and per tasks.py's own rewrite note)
=============================================================================
"Determine whether the local market is accelerating, stable, declining,
emerging, or overheating." tasks.py's REWRITE NOTE names this engine
specifically: BusinessReport.momentum_summary and .timing are left blank
in the pipeline precisely because Engine 18 did not exist -- there was no
real signal to fill them with. This module is that signal.

Deliberately ECONOMY-WIDE and TEMPORAL, not physical and not a snapshot --
Engine 12 (development_activity.py) already owns "is something being
built here right now" (a physical, OSM-based, single-vintage question).
This engine answers a different, narrower question: over the last several
years, is the LOCAL ECONOMY around this cell growing, shrinking, or
changing pace -- and is that trend accelerating, decelerating, or
structurally breaking. One-engine-one-question discipline (the same
discipline market_gap.py, business_adjacency.py, and development_activity.py
already insist on for themselves) is why this module does not touch
construction counts, business counts, or competitor density at all --
those are Engines 6/7/8/12's data, not this one's.

=============================================================================
WHAT'S ACTUALLY AVAILABLE VS. WHAT ISN'T (read this before extending)
=============================================================================
There is no sub-national, high-frequency, real GDP series for Kenyan
locations -- KNBS publishes county GDP (Gross County Product) only
annually, with a multi-year publication lag, and nothing below county
level. That rules out any "just use real GDP" design outright; this is
the same missing-ground-truth situation purchasing_power.py documents for
itself, and it gets the same discipline: do not fabricate a number that
looks like income data.

The one real, physically observed, globally consistent proxy this
codebase can actually reach is satellite-measured nighttime light
radiance -- specifically NOAA/NASA VIIRS Day/Night Band (DNB) Annual
Composites, produced by the Earth Observation Group (EOG) at the
Colorado School of Mines and released into the public domain with no
usage restriction. This is not a novel idea invented for this module; it
is one of the most heavily replicated results in development economics:

  Henderson, J.V., Storeygard, A. and Weil, D.N. (2012). "Measuring
  Economic Growth from Outer Space." American Economic Review, 102(2),
  994-1028. Established that annual GROWTH in night-light radiance
  carries real information about GDP growth beyond what national
  accounts alone capture, with an estimated elasticity of roughly 0.28-0.3
  (a 1 percentage-point change in light growth corresponds to
  roughly a 0.28-0.3 percentage-point change in GDP growth), and that this
  holds at sub-national spatial resolution -- their own headline result is
  a WITHIN-COUNTRY comparison (coastal vs. hinterland sub-Saharan Africa).

  Elvidge, C.D., Zhizhin, M., Ghosh, T., Hsu, F-C. and Taneja, J. (2021).
  "Annual Time Series of Global VIIRS Nighttime Lights Derived from
  Monthly Averages: 2012 to 2019." Remote Sensing, 13(5), 922. The
  production methodology for the Annual VNL V2 composite this module
  reads: monthly cloud-free average-radiance grids combined via a
  twelve-month-median outlier-removal step that isolates and zeroes out
  background (non-light) radiance and discards most biomass-burning /
  aurora / boat-light contamination before the annual composite is
  written. This is why this module reads the "average_masked" /
  "median_masked" bands specifically, not the raw "average" band -- the
  masking step is EOG's own noise-removal, already done upstream, and
  re-doing a cruder version of it here would be redundant and worse.

EOG's own annual VNL V2 product page (https://eogdata.mines.edu/products/vnl/#annual_v2)
and its Earth Engine listing (NOAA/VIIRS/DNB/ANNUAL_V21 / _V22) confirm:
15 arc-second (~463.83m) pixel resolution, public-domain licensing
("Colorado School of Mines data, information and products, regardless of
delivery mechanism, are not subject to copyright protection and there
are no restrictions on their use by the public"), coverage from 2012, and
that the stray-light-corrected configuration (VCMSLCFG -- the one used
here, since stray-light contamination is worst at Kenya's latitude
during specific months and VCMSLCFG corrects for it) is available
starting 2014, NOT 2012-2013. That is why DEFAULT_START_YEAR below is
2014, not the product's nominal 2012 start -- a deliberate exclusion, not
an oversight; see EDGE CASES below.

=============================================================================
WHY THIS IS A REAL STATISTICS/ML ENGINE, NOT "IS THE LATEST PIXEL BRIGHTER"
=============================================================================
A single before/after radiance comparison is not a defensible momentum
signal -- VIIRS DNB pixel radiance at the scale of a single ~150m
business_intel cell is genuinely noisy (cloud-masking gaps, lunar
residuals, sensor recalibration steps, and outright misregistration
between years). Treating one year's value as ground truth and comparing
it to another single year the way a naive "score = latest - previous"
implementation would is exactly the kind of overconfident, small-n
implementation this codebase's other engines already refuse to ship
(population.py's Fay-Herriot shrinkage, purchasing_power.py's Empirical
Bayes step, market_gap.py's dispersion-corrected GLM, development_activity.py's
Wilson/Garwood interval estimation). This module instead treats the
multi-year radiance record as what it actually is -- a short, noisy,
serially-correlated TIME SERIES -- and applies five real, cited,
textbook-standard techniques, each addressing a specific way a naive
approach would fail:

1. ROBUST TREND ESTIMATION (Theil-Sen slope + Mann-Kendall significance,
   Hamed-Rao autocorrelation correction)
   Theil, H. (1950). "A Rank-Invariant Method of Linear and Polynomial
   Regression Analysis." Nederl. Akad. Wetensch., Proc., 53, 386-392,
   521-525, 1397-1401.
   Sen, P.K. (1968). "Estimates of the Regression Coefficient Based on
   Kendall's Tau." Journal of the American Statistical Association,
   63(324), 1379-1389.
   Mann, H.B. (1945). "Nonparametric Tests Against Trend." Econometrica,
   13(3), 245-259.
   Hamed, K.H. and Rao, A.R. (1998). "A Modified Mann-Kendall Trend Test
   for Autocorrelated Data." Journal of Hydrology, 204(1-4), 182-196.
   The Theil-Sen estimator -- the median of all pairwise slopes between
   observations -- is the standard robust-regression choice for exactly
   this data shape: a handful of points (4-11 years), at least one of
   which may be a contaminated outlier (a missed fire, an
   under-corrected lunar-glint year) that would badly distort an OLS fit.
   Its breakdown point is 29.3%, meaning it tolerates roughly one bad
   year in three or four without the slope estimate swinging wildly --
   OLS has a breakdown point of 0%. Significance of the trend uses the
   Mann-Kendall S statistic, with Hamed & Rao's variance correction for
   serial autocorrelation (satellite radiance time series are not
   independent draws -- a bright year tends to be followed by another
   bright year), since the uncorrected Mann-Kendall variance formula
   understates the true variance and overstates significance when the
   series is autocorrelated, exactly the failure mode this module must
   avoid when deciding whether a "trend" is real or noise.

2. A LOCAL LINEAR TREND STATE-SPACE MODEL, FIT BY MAXIMUM LIKELIHOOD
   (the genuinely ML/statistical-learning component of this engine)
   Harvey, A.C. (1989). "Forecasting, Structural Time Series Models and
   the Kalman Filter." Cambridge University Press.
   Durbin, J. and Koopman, S.J. (2012). "Time Series Analysis by State
   Space Methods," 2nd ed. Oxford University Press.
   Kalman, R.E. (1960). "A New Approach to Linear Filtering and
   Prediction Problems." Journal of Basic Engineering, 82(1), 35-45.
   The Theil-Sen slope above is a single, static number for the whole
   window -- it cannot say whether growth is currently ACCELERATING or
   DECELERATING within that window, which is exactly what separates
   "accelerating" from "stable" from "overheating" in the classification
   this engine must produce. A local linear trend model represents the
   unobserved (level, slope) pair as a state that itself evolves with
   process noise, observed through noisy annual radiance measurements --
   the textbook state-space formulation for "smoothly time-varying
   trend extraction," fit here via a genuine maximum-likelihood
   procedure (numerically maximizing the Kalman filter's Gaussian
   log-likelihood over the two process-noise variances, using
   scipy.optimize.minimize) rather than hand-picked smoothing
   constants. This is what recovers a TIME-VARYING slope path -- so the
   engine can see the slope accelerating or decelerating across the
   observed years -- not just one number for the whole window.

3. CURVATURE / ACCELERATION from the filtered slope path
   The second difference of the Kalman-smoothed slope path is this
   engine's estimate of ACCELERATION -- whether growth itself is
   speeding up or slowing down -- the signal that separates
   "accelerating" (positive growth, positive acceleration) from
   "overheating" (very high growth, but decelerating / volatile,
   suggesting the pace is not sustainable) from plain "stable" growth.

4. STRUCTURAL BREAK DETECTION (CUSUM of recursive residuals)
   Page, E.S. (1954). "Continuous Inspection Schemes." Biometrika,
   41(1/2), 100-115.
   Brown, R.L., Durbin, J. and Evans, J.M. (1975). "Techniques for
   Testing the Constancy of Regression Relationships over Time." Journal
   of the Royal Statistical Society, Series B, 37(2), 149-192.
   A single smooth trend line cannot distinguish "this cell has been
   steadily growing all along" from "this cell was flat for years and
   then something changed two years ago" -- and the latter is exactly
   the signature of an EMERGING market (a real regime shift, not a
   continuation of history). CUSUM of standardized one-step-ahead
   Kalman residuals, checked against the standard Brownian-bridge
   boundary (approximately +/-1.36*sqrt(n) at the 5% level, per Brown,
   Durbin & Evans), flags whether and when a break in the series'
   trajectory is statistically detectable, rather than this engine
   guessing "recent" vs. "old" from eyeballing the numbers.

5. EMPIRICAL BAYES (METHOD-OF-MOMENTS) SHRINKAGE TOWARD A COUNTY PRIOR
   Efron, B. and Morris, C. (1975). "Data Analysis Using Stein's
   Estimator and Its Generalizations." Journal of the American
   Statistical Association, 70(350), 311-319.
   Same statistical family as this codebase's Fay-Herriot use in
   population.py and the simpler Empirical Bayes case purchasing_power.py
   and development_activity.py already use for themselves. A cell with
   only 4-6 years of mostly-dim, mostly-noisy radiance produces a
   Theil-Sen/Kalman slope estimate with real, often large, sampling
   variance -- reporting that raw estimate as "the" momentum number
   would systematically overstate confidence for exactly the
   low-signal cells (rural, newly-queried, unlit) where the estimate is
   least trustworthy. This module shrinks the cell's own slope estimate
   toward a real county-level prior (pooled from THIS ENGINE'S OWN
   accumulated FeatureObservation history, same "let the system learn
   its own baseline over time" approach development_activity.py already
   uses, since no published county-level nightlight-momentum series
   exists for this system to cite instead), with the shrinkage weight
   (gamma = A / (A + D_i)) itself estimated from the data -- not a
   hand-picked blending ratio.

CLASSIFICATION (accelerating / stable / declining / emerging /
overheating) is done via a Mahalanobis-distance nearest-prototype
classifier over three standardized features (shrunk growth rate,
acceleration, volatility) -- see classify_momentum_regime() docstring
for why this, rather than a supervised model (this system has no
ground-truth labels of which cells were "actually" overheating, and
fabricating them would be exactly the kind of fake this codebase's other
engines already refuse to ship) or a bare if/else threshold ladder
(which has no basis in how these three signals actually co-vary in this
system's own accumulated data).

=============================================================================
HONEST LIMITATIONS OF THE NIGHTLIGHTS-AS-ECONOMY PROXY ITSELF
=============================================================================
This module does NOT claim to measure GDP, and no FeatureObservation it
writes is labelled as one. The literature is explicit that nightlights
are a proxy with real, structural weaknesses this module cannot correct
away with better statistics on top:

  Gibson, J., Olivia, S. and Boe-Gibson, G. (2020). "Night Lights in
  Economics: Sources and Uses." Journal of Economic Surveys, 34(5),
  955-980. Documents that the light-to-GDP elasticity is not a fixed
  universal constant -- it varies by country, by income level, by
  electrification rate, and by spatial aggregation scale, and popular
  DMSP-era data in particular carries blurring/top-coding measurement
  error that biases naive uses. VIIRS DNB (used here, not the older
  DMSP-OLS sensor) is the specific improvement this literature recommends
  BECAUSE it does not saturate/top-code the way DMSP does -- but the
  elasticity-varies-by-context finding still applies.

  Bluhm, R. and Krause, M. (2022). "Top Lights: Bright Cities and Their
  Contribution to Economic Development." Journal of Development
  Economics, 157. Shows the radiance-to-activity relationship is
  materially NONLINEAR at the bright end ("top lights" -- CBDs, major
  commercial corridors, industrial/mining sites, airports) -- an already
  very bright cell getting brighter carries different economic
  information than a dark cell lighting up for the first time. This is
  the specific citation behind this module's percentile-winsorization
  step (see winsorize_radiance()) and behind NOT treating the
  implied_local_growth_rate feature below as a precise, additive number.

  Chen, X. and Nordhaus, W.D. (2011). "Using Luminosity Data as a Proxy
  for Economic Statistics." Proceedings of the National Academy of
  Sciences, 108(21), 8589-8594; and Chen, X. and Nordhaus, W.D. (2019),
  the update finding under 4% of the variance in ANNUAL GDP GROWTH RATES
  (as opposed to levels) is explained by annual light changes in
  higher-income settings -- a materially weaker year-over-year signal
  than the levels-based literature. This module therefore reports its
  Henderson-Storeygard-Weil-elasticity-derived "implied_local_growth_rate"
  feature with a deliberately wide uncertainty band (see
  implied_local_growth_rate()) and documents in its own evidence output
  that this number is a national-elasticity-based ecological inference
  applied to one small cell, not a locally-estimated or locally-validated
  rate -- exactly the caveat Henderson, Storeygard & Weil's own elasticity
  was estimated FOR cross-country/cross-region comparisons, not
  guaranteed to transfer unchanged to a single ~1-4 km^2 patch of Kiambu
  County. Nothing downstream should read that feature as a GDP forecast.

=============================================================================
EDGE CASES THIS MODULE HANDLES EXPLICITLY (not just tries to avoid)
=============================================================================
- Fewer than MIN_YEARS_FOR_TREND years of successfully fetched radiance
  -> no trend/acceleration/classification computed; only the raw latest-
  year radiance observation is written (derivation_level="observation"),
  everything else is None, and a warning explains why. No score is
  invented from an unfittable series.
- ALL fetched years have radiance == 0 (or below the noise floor after
  masking) -> a genuinely unlit or below-detection-limit cell (common for
  rural/peri-urban Kenyan cells). log(0) is undefined, so this module
  does not silently substitute log(0.0001)-style noise into a real trend
  claim -- classified as "insufficient_signal" rather than forced into
  "declining", with data_is_effectively_dark=True recorded so callers
  don't mistake "we have no signal" for "we observed decline".
- A single non-zero year surrounded by zero years (a likely EOG
  under-masked transient -- an unfiltered fire, flare, or aurora
  fragment) -> Theil-Sen's robustness (29.3% breakdown point) prevents
  one such point from dominating the slope; MIN_NONZERO_YEARS additionally
  gates whether a trend is attempted at all.
- Raw (unmasked) radiance dips slightly negative from sensor noise before
  EOG's own masking step -> this module reads the ALREADY-masked band
  (average_masked / median_masked, per Elvidge et al. 2021's own
  background-zeroing step) specifically so this cannot occur in the
  values this module receives; if a provider implementation nonetheless
  returns a negative value (e.g. a buggy/mocked provider in tests), it is
  clipped to 0.0 and flagged, never passed to log() as-is.
- Missing year (a provider fetch failure for one calendar year inside an
  otherwise usable window) -> the Kalman filter here uses the ACTUAL year
  gaps between observations (not an assumed unit time-step), so a missing
  year degrades precision rather than silently compressing a 2-year gap
  into a 1-year step; Theil-Sen is computed directly over (year, value)
  pairs and is unaffected by irregular spacing by construction.
- A year's cf_cvg (cloud-free observation count backing that year's
  composite) is very low -> that year's Kalman observation variance is
  inflated proportionally (heteroskedastic measurement noise, standard
  practice for state-space models with time-varying observation
  quality), so a low-confidence year is down-weighted in the fit rather
  than trusted equally to a well-observed year.
- A pixel/cell is top-coded/blooming-affected (gas flares, airports,
  large industrial sites, mines) -> PERCENTILE_WINSORIZE_PCT trims the
  extreme tail across the corpus of a run before any single cell's log-
  transform, per Bluhm & Krause's top-lights nonlinearity finding, and
  the affected cell's warnings record that winsorization occurred.
- Fewer than MIN_COUNTY_PRIOR_SAMPLE_CELLS cells of this engine's own
  accumulated history exist for a county -> falls back to
  COLD_START_PRIOR_GROWTH_RATE / COLD_START_BETWEEN_CELL_VARIANCE, with
  prior_source="cold_start_placeholder" recorded (not silently
  indistinguishable from a real pooled prior).
- Between-cell variance estimated as negative (real possibility with
  method-of-moments on a small corpus) -> clipped to 0, meaning full
  shrinkage to the prior -- same convention purchasing_power.py and
  development_activity.py already apply to their own A estimates.
- The Kalman MLE optimizer fails to converge or returns a degenerate
  (near-zero) variance pair -> falls back to a method-of-moments variance
  estimate from the first-differenced series (a real, if less efficient,
  estimator), flagged in warnings rather than silently trusting a failed
  optimization.
- Fewer than MIN_N_FOR_ZSCORE_CLASSIFICATION cells scored in the current
  run/corpus to standardize against -> classify_momentum_regime() falls
  back to fixed, documented absolute thresholds (calibrated against the
  Henderson-Storeygard-Weil elasticity's own effect sizes) instead of
  z-scores computed from a corpus too small to trust.
- The external VIIRS provider is unavailable (network/auth failure, no
  credentials configured) -> the EngineRun is marked "failed" with a real
  error_message, no FeatureObservation rows are written, and the caller
  (tasks.py's _run_engine) surfaces this as a risk on the report rather
  than a fabricated "stable" verdict -- same discipline
  query_osm_overpass / development_activity.py already apply when
  Overpass is unreachable.
"""

from __future__ import annotations

import logging
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

logger = logging.getLogger("business_intel")

# NOTE ON REMOVING THE scipy DEPENDENCY (post-review): this module's
# first draft imported scipy.stats.norm and scipy.optimize.minimize.
# This codebase's OTHER engines (population.py, business_survival.py,
# mobility_traffic.py, forced_traffic.py) all document, explicitly and
# repeatedly, "no scipy in this environment" and hand-roll the
# equivalents (an erf-based normal CDF, a numpy-only lognormal fit, a
# bisection root-finder for Fay-Herriot's variance component). Shipping
# a scipy import here would silently break in the same environment every
# other engine module was written to run in. Fixed below: `_standard_
# normal_cdf()` (erf-based, same formula population.py/mobility_traffic.py/
# forced_traffic.py already use for themselves) replaces norm.cdf(), and
# `_grid_refine_maximize_2d()` (a dependency-free coarse-to-fine grid
# search over the 2D log-variance space) replaces scipy.optimize.minimize
# -- see local_linear_trend_kalman_filter()'s docstring for the
# documented precision trade-off of that swap.

# =============================================================================
# Constants
# =============================================================================

ENGINE_NUMBER = 18

# VIIRS DNB annual composite, stray-light-corrected configuration (VCMSLCFG)
# is only available from 2014 onward -- 2012/2013 exist only in the
# non-stray-light-corrected VCMCFG configuration, which is materially
# noisier at Kenya's latitude (see module docstring). Excluding them is
# deliberate, not an oversight.
VIIRS_FIRST_USABLE_YEAR = 2014
DEFAULT_LOOKBACK_YEARS = 8  # ~8 annual composites is enough for a Kalman
                            # local-linear-trend fit to have real power
                            # without reaching back into a different sensor
                            # generation (VIIRS DNB itself only starts 2012).

# A single ~150m business_intel cell is 0-1 VIIRS pixels (15 arcsec =
# ~463.83m at the equator) -- far too few pixels for a stable mean. Two
# radii are analyzed: a tight "site" buffer (still mostly sub-pixel, kept
# for completeness/diagnostics) and a wider "neighborhood" buffer that
# reliably covers multiple pixels and is the one this engine's real
# trend/classification features are computed from.
ECONOMIC_MOMENTUM_SITE_RADIUS_M = 500
ECONOMIC_MOMENTUM_NEIGHBORHOOD_RADIUS_M = 2000
PRIMARY_RADIUS_M = ECONOMIC_MOMENTUM_NEIGHBORHOOD_RADIUS_M

# --- Preprocessing ---
# Additive constant before log-transform. Bluhm, R. and Krause, M. (2022)
# show the choice of constant is not innocuous for cross-sectional
# comparisons of luminosity growth; 0.1 nW/cm^2/sr is a commonly used,
# small-relative-to-typical-urban-radiance choice (typical small Kenyan
# trading-center radiance in the low single digits nW/cm^2/sr; typical
# unlit-background noise floor << 0.1) documented here rather than
# silently hardcoded.
LOG_TRANSFORM_OFFSET_C = 0.1

# Trim the top/bottom tail of THIS RUN's cross-sectional radiance
# distribution before log-transforming any single cell, per Bluhm &
# Krause's "top lights" nonlinearity finding (a flare/airport/mine pixel
# should not be treated as a linearly-comparable "brighter business
# district"). Applied only when enough cells exist in the comparison
# corpus to make a percentile meaningful.
PERCENTILE_WINSORIZE_PCT = 1.0  # trim top/bottom 1%
MIN_CORPUS_FOR_WINSORIZE = 20

# --- Trend fitting gates ---
MIN_YEARS_FOR_TREND = 4          # fewer than this -> no trend attempted at all
MIN_NONZERO_YEARS = 2            # fewer non-zero years -> data too dark to trend
MIN_YEARS_FOR_KALMAN_MLE = 5     # fewer than this -> use method-of-moments
                                  # variances instead of MLE-fitting them

# --- County-level Empirical Bayes prior ---
MIN_COUNTY_PRIOR_SAMPLE_CELLS = 8
# Cold-start prior: essentially "no momentum, moderate uncertainty" --
# not a guess at a plausible Kenyan growth rate, deliberately neutral
# until real pooled evidence exists. Units: log-radiance-points per year
# (i.e. the Theil-Sen/Kalman slope scale, NOT a percentage).
COLD_START_PRIOR_LOG_SLOPE = 0.0
COLD_START_BETWEEN_CELL_VARIANCE = 0.01

# --- Structural break (CUSUM) ---
# Brown, Durbin & Evans (1975) 5%-level boundary for the CUSUM of
# recursive residuals is a straight line at +/- a*sqrt(n) with a=0.948
# for a two-sided 5% test (a=1.143 for 1%); using the commonly-cited
# a=0.948 constant here, applied to STANDARDIZED one-step Kalman
# residuals (mean 0, unit variance under a well-specified model), which
# is the same normalization the boundary assumes.
CUSUM_BOUNDARY_CONSTANT_5PCT = 0.948

# --- Classification ---
MIN_N_FOR_ZSCORE_CLASSIFICATION = 15
# Fallback absolute thresholds when the run corpus is too small to
# z-score against -- calibrated to the Henderson-Storeygard-Weil (2012)
# elasticity's own effect sizes: their paper treats an annual light-
# growth differential on the order of a few percentage points as
# economically meaningful. A log-slope of +0.05/year is ~5%/year implied
# light growth -- the threshold below is deliberately conservative
# (higher than the elasticity's own smallest reported meaningful
# effects) given how noisy a single small cell's estimate is.
FALLBACK_GROWTH_THRESHOLD_LOG_SLOPE = 0.05
FALLBACK_ACCELERATION_THRESHOLD = 0.01

# Henderson, Storeygard & Weil (2012), Table 4 / text: elasticity of GDP
# growth with respect to light growth, point estimate ~0.28-0.30 in their
# preferred specifications. Reported here with a wide documented
# uncertainty band per Gibson, Olivia & Boe-Gibson (2020) and Chen &
# Nordhaus (2011/2019) -- see module docstring's HONEST LIMITATIONS.
HSW2012_LIGHT_TO_GDP_ELASTICITY = 0.28
HSW2012_ELASTICITY_LOW = 0.10   # conservative low end seen across replications
HSW2012_ELASTICITY_HIGH = 0.60  # conservative high end seen across replications


# =============================================================================
# Data provider interface (kept separate from the pure-math layer below --
# same "import for plumbing, duplicate for independent statistics"
# convention forced_traffic.py / anchor.py / development_activity.py
# already document for themselves)
# =============================================================================

@dataclass
class RadianceYearObservation:
    """One year of VIIRS annual-composite radiance for one (cell, radius).

    mean_radiance_masked is read from EOG's "average_masked" band (the
    already background-zeroed, outlier-filtered band -- see module
    docstring's WHY VIIRS section) -- NOT the raw "average" band.
    cloud_free_obs_mean is that year's mean "cf_cvg" band value over the
    buffer -- how many cloud-free nightly observations actually backed
    this year's composite in this area. Low values mean a noisier
    composite for that specific year, used to inflate that observation's
    variance in the Kalman fit (see module docstring's cf_cvg edge case).
    """
    year: int
    mean_radiance_masked: Optional[float]
    pixel_count: int
    cloud_free_obs_mean: Optional[float]
    succeeded: bool
    warning: str = ""


class VIIRSAnnualCompositeProvider(ABC):
    """Interface for fetching one year of VIIRS radiance for one cell/radius.

    Kept abstract so the pure-math/statistics layer below can be
    exercised in tests against a fake provider with zero network/DB
    dependency -- the same discipline population.py's
    AdministrativeHierarchyProvider and purchasing_power.py's
    CountyEconomicPriorProvider already apply to themselves.
    """

    @abstractmethod
    def fetch_annual_radiance(self, cell, year: int, radius_m: int) -> RadianceYearObservation:
        raise NotImplementedError


class EOGVIIRSAnnualCompositeProvider(VIIRSAnnualCompositeProvider):
    """Real Django-facing provider. Downloads/reads EOG Annual VNL V2
    windowed rasters via business_intel.google_client.fetch_viirs_annual_radiance
    (transport + retry/backoff + BusinessAPICallLog + VIIRSAnnualRadianceCache
    live in google_client.py / models.py, same split query_osm_overpass
    already establishes for Overpass -- see the ADDITION blocks accompanying
    this file for the exact model/client code, which are additive-only,
    same as development_activity.py's OSMOverpassQueryCache addition).

    This class is intentionally a thin adapter, not where the download
    logic lives -- keeps this engine module's business logic testable
    against EOGVIIRSAnnualCompositeProvider's ABC without a live network
    call, and keeps transport concerns (mirrors, auth, retries) in one
    place shared with any other future engine that might need VIIRS data.
    """

    def fetch_annual_radiance(self, cell, year: int, radius_m: int) -> RadianceYearObservation:
        from ..google_client import fetch_viirs_annual_radiance  # local import: keep this module's pure-math layer DB/network-import-free
        return fetch_viirs_annual_radiance(cell, year, radius_m)


# =============================================================================
# Pure-math layer -- no Django/DB/network imports below this line, so it
# can be unit-tested and reasoned about independent of the ORM. Mirrors
# development_activity.py's own "pure-math functions first, Django-facing
# entry point last" file layout.
# =============================================================================

def winsorize_radiance(values: Sequence[float], pct: float = PERCENTILE_WINSORIZE_PCT) -> np.ndarray:
    """Clip `values` to the [pct, 100-pct] percentile range of `values`
    itself. Guards against a single top-coded/blooming pixel (flare,
    airport, mine -- see Bluhm & Krause 2022 in the module docstring)
    dominating a log-transform. With fewer than 2 values, or all values
    identical, returns the input unchanged (nothing meaningful to trim).
    """
    arr = np.asarray(values, dtype=float)
    if arr.size < 2 or np.ptp(arr) == 0:
        return arr
    lo, hi = np.percentile(arr, [pct, 100 - pct])
    return np.clip(arr, lo, hi)


def log_transform_radiance(value: float, offset_c: float = LOG_TRANSFORM_OFFSET_C) -> float:
    """log(value + c). Value is clipped to >= 0 first (see module
    docstring's negative-radiance edge case) -- this module only ever
    receives already-masked EOG radiance, where negative values should
    not occur, but a provider implementation bug (or a test double)
    could still hand one in, and log() must never be called on a raw
    negative number.
    """
    return math.log(max(0.0, value) + offset_c)


def _standard_normal_cdf(z: float) -> float:
    """Phi(z) via the error function -- no scipy in this environment
    (same formula population.py / mobility_traffic.py / forced_traffic.py
    already use for themselves; see the module-level NOTE above)."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _grid_refine_maximize_2d(
    objective,
    x0: Tuple[float, float],
    initial_half_width: float = 4.0,
    n_points_per_axis: int = 9,
    n_refinements: int = 6,
    shrink_factor: float = 0.4,
):
    """Dependency-free coarse-to-fine grid search that MAXIMIZES
    `objective(x, y)` over a 2D real domain, starting centered at `x0`.
    Replaces scipy.optimize.minimize for this module's specific need
    (maximizing the Kalman filter's Gaussian log-likelihood over two
    log-variance parameters) -- see local_linear_trend_kalman_filter()'s
    docstring for why scipy is not available in this environment.

    Algorithm: evaluate `objective` on an n_points_per_axis x
    n_points_per_axis grid centered at the current best point, spanning
    +/- `half_width` on each axis; move the center to the best grid
    point found; shrink `half_width` by `shrink_factor`; repeat
    `n_refinements` times. This is a standard, textbook "pattern search" /
    coarse-to-fine grid refinement -- not gradient-based, so it cannot
    exploit curvature the way L-BFGS-B does, but for a smooth,
    well-behaved 2-parameter log-likelihood surface (which the Kalman
    filter's is, away from degenerate corners) it reliably converges to
    within a small, bounded fraction of the true optimum, and it can
    never raise on a singular Hessian or a bad line-search step the way
    a quasi-Newton method occasionally can -- a real robustness advantage
    for an engine that must never crash on a single cell's unusual data.

    Returns (best_x, best_y, best_value, converged), where `converged`
    is True unless every evaluated point (including the start) returned
    a non-finite objective value (a degenerate/undefined likelihood
    surface -- e.g. a completely flat or zero-variance input series),
    in which case the caller should fall back to a non-MLE estimate
    (see local_linear_trend_kalman_filter()'s fit_method fallback).
    """
    best_x, best_y = x0
    best_val = objective(best_x, best_y)
    converged = math.isfinite(best_val)
    half_width = initial_half_width

    for _ in range(n_refinements):
        improved = False
        xs = np.linspace(best_x - half_width, best_x + half_width, n_points_per_axis)
        ys = np.linspace(best_y - half_width, best_y + half_width, n_points_per_axis)
        for cand_x in xs:
            for cand_y in ys:
                val = objective(float(cand_x), float(cand_y))
                if math.isfinite(val) and val > best_val:
                    best_val, best_x, best_y = val, float(cand_x), float(cand_y)
                    improved = True
                    converged = True
        half_width *= shrink_factor
        if not improved and half_width < 1e-4:
            break  # tight local optimum reached; further shrinking won't move the estimate meaningfully

    return best_x, best_y, best_val, converged


def theil_sen_slope(years: Sequence[int], values: Sequence[float]) -> Tuple[Optional[float], Optional[float]]:
    """Theil (1950) / Sen (1968) robust slope estimator: the median of
    all pairwise slopes (v_j - v_i) / (t_j - t_i) for i < j. Returns
    (slope, intercept) where intercept is chosen so the line passes
    through (median(years), median(values) - slope*median(years)),
    the standard Theil-Sen intercept convention. Returns (None, None)
    with fewer than 2 points.
    """
    years_arr = np.asarray(years, dtype=float)
    values_arr = np.asarray(values, dtype=float)
    n = len(years_arr)
    if n < 2:
        return None, None

    slopes = []
    for i in range(n):
        for j in range(i + 1, n):
            dt = years_arr[j] - years_arr[i]
            if dt == 0:
                continue  # duplicate year -- not a valid pairwise slope
            slopes.append((values_arr[j] - values_arr[i]) / dt)

    if not slopes:
        return None, None

    slope = float(np.median(slopes))
    intercept = float(np.median(values_arr) - slope * np.median(years_arr))
    return slope, intercept


def mann_kendall_hamed_rao(years: Sequence[int], values: Sequence[float]) -> Dict[str, Optional[float]]:
    """Mann-Kendall trend test (Mann 1945) with Hamed & Rao (1998)
    autocorrelation-corrected variance.

    Returns a dict with:
        tau           : Kendall's tau (S normalized to [-1, 1])
        s             : the raw Mann-Kendall S statistic
        var_s_raw     : the classical (independence-assumed) variance of S
        var_s_corrected: Hamed-Rao-corrected variance of S (>= var_s_raw
                         under positive autocorrelation, which is the
                         realistic case for a satellite radiance series)
        z             : standardized test statistic using the corrected variance
        p_value       : two-sided p-value from the standard normal
        significant_05: whether p_value < 0.05

    Assumes `years` are sorted ascending and correspond 1:1 to `values`
    (irregular spacing is fine -- S only uses sign(v_j - v_i), not the
    time gap itself, by construction; see module docstring's missing-year
    edge case for why irregular spacing does not need special handling
    here specifically -- unlike the Kalman filter below, which does need it).
    """
    values_arr = np.asarray(values, dtype=float)
    n = len(values_arr)
    if n < MIN_YEARS_FOR_TREND:
        return dict(tau=None, s=None, var_s_raw=None, var_s_corrected=None, z=None, p_value=None, significant_05=None)

    # --- S statistic ---
    s = 0
    for i in range(n - 1):
        for j in range(i + 1, n):
            s += np.sign(values_arr[j] - values_arr[i])
    s = float(s)

    # Kendall's tau normalization (no-ties case; ties are not expected in
    # a continuous radiance series but handled gracefully via the
    # standard denominator n*(n-1)/2 even if present, matching common
    # practice when the tie correction itself is not separately modeled)
    denom = n * (n - 1) / 2.0
    tau = s / denom if denom > 0 else None

    # --- Classical (independence-assumed) variance of S, Mann (1945) ---
    var_s_raw = n * (n - 1) * (2 * n + 5) / 18.0

    # --- Hamed & Rao (1998) autocorrelation correction ---
    # Rank-transform the series, then compute the lag-k autocorrelation
    # of the ranks for k = 1..n-1, and inflate var_s_raw by the
    # "effective sample size" correction factor n / n* Hamed & Rao derive:
    #   n/n* = 1 + (2 / (n*(n-1)*(n-2))) * sum_{k=1}^{n-1} (n-k)(n-k-1)(n-k-2) * rho_k
    # Only rho_k values that are "significant" under the standard
    # +/-1.96/sqrt(n) bound are included, per Hamed & Rao's own
    # recommendation (including noisy near-zero rho_k estimates would
    # inject, not remove, spurious variance inflation/deflation).
    ranks = np.argsort(np.argsort(values_arr)).astype(float)
    ranks -= ranks.mean()
    denom_acf = np.sum(ranks ** 2)
    correction_sum = 0.0
    if denom_acf > 0 and n >= 4:
        significance_bound = 1.96 / math.sqrt(n)
        for k in range(1, n - 1):
            num = np.sum(ranks[:n - k] * ranks[k:])
            rho_k = num / denom_acf
            if abs(rho_k) > significance_bound:
                correction_sum += (n - k) * (n - k - 1) * (n - k - 2) * rho_k

    if n > 2:
        n_star_ratio = 1.0 + (2.0 / (n * (n - 1) * (n - 2))) * correction_sum
    else:
        n_star_ratio = 1.0
    # The correction factor should not reduce variance below the
    # independence case in a way that reverses sign or explodes for a
    # pathological near-zero denom_acf -- clip to a sane positive floor.
    n_star_ratio = max(n_star_ratio, 0.1)
    var_s_corrected = var_s_raw * n_star_ratio

    if var_s_corrected <= 0:
        z = 0.0
    elif s > 0:
        z = (s - 1) / math.sqrt(var_s_corrected)
    elif s < 0:
        z = (s + 1) / math.sqrt(var_s_corrected)
    else:
        z = 0.0

    p_value = float(2 * (1 - _standard_normal_cdf(abs(z))))
    return dict(
        tau=tau, s=s, var_s_raw=var_s_raw, var_s_corrected=var_s_corrected,
        z=z, p_value=p_value, significant_05=(p_value < 0.05),
    )


@dataclass
class KalmanTrendFit:
    """Output of local_linear_trend_kalman_filter()."""
    filtered_levels: np.ndarray          # filtered level estimate at each observed year
    filtered_slopes: np.ndarray          # filtered slope (annual growth in log-radiance) at each year
    smoothed_slopes: np.ndarray          # backward-smoothed slope path (uses all data, not just past)
    final_slope: float                   # smoothed slope at the LAST observed year -- "current" growth rate
    final_slope_variance: float          # smoothed variance of final_slope
    acceleration: Optional[float]        # second difference of smoothed_slopes, most recent -- see docstring
    one_step_forecast_level: float       # forecast of next year's log-radiance level
    one_step_forecast_variance: float
    level_process_variance: float        # fitted (or fallback) process noise variance, level equation
    slope_process_variance: float        # fitted (or fallback) process noise variance, slope equation
    fit_method: str                      # "mle" or "method_of_moments_fallback"
    standardized_residuals: np.ndarray   # one-step-ahead residuals / sqrt(forecast variance), for CUSUM
    log_likelihood: Optional[float]


def local_linear_trend_kalman_filter(
    years: Sequence[int],
    log_values: Sequence[float],
    observation_variances: Sequence[float],
) -> Optional[KalmanTrendFit]:
    """Local linear trend (Harvey 1989; Durbin & Koopman 2012) state-space
    model, fit by maximum likelihood (Kalman 1960's filter supplies the
    Gaussian likelihood; _grid_refine_maximize_2d(), a dependency-free
    coarse-to-fine grid search -- see that function's own docstring for
    why scipy.optimize is not used in this environment -- maximizes it
    over the two process-noise variances).

    State at year t: x_t = [level_t, slope_t]'.
    Transition (irregular time steps handled via dt, see module
    docstring's missing-year edge case):
        level_t = level_{t-1} + dt * slope_{t-1} + w_level,  w_level ~ N(0, dt * Q_level)
        slope_t = slope_{t-1} + w_slope,                     w_slope ~ N(0, dt * Q_slope)
    Observation:
        log_value_t = level_t + v_t,  v_t ~ N(0, observation_variances[t])
    observation_variances[t] should already reflect that year's
    cloud-free-observation-count-based confidence (see module docstring's
    cf_cvg edge case) -- this function does not itself look at cf_cvg,
    it only consumes whatever variance the caller supplies.

    Q_level and Q_slope (the two free parameters) are fit by maximizing
    the Kalman filter's own Gaussian log-likelihood of the innovations
    (Harvey 1989, ch. 3) via a dependency-free coarse-to-fine grid search
    (_grid_refine_maximize_2d), log-parameterized to keep both variances
    positive without a constrained optimizer. Falls back to a
    method-of-moments variance estimate (from the first-differenced
    series) if there are fewer than MIN_YEARS_FOR_KALMAN_MLE points, or
    if the optimizer fails to converge / returns a degenerate
    (near-zero) result -- see module docstring's optimizer-failure edge case.

    Returns None if there are fewer than 3 points (cannot estimate both
    a level and a slope with useful precision below that).
    """
    years_arr = np.asarray(years, dtype=float)
    y = np.asarray(log_values, dtype=float)
    obs_var = np.asarray(observation_variances, dtype=float)
    n = len(y)
    if n < 3:
        return None

    dt = np.diff(years_arr)
    dt = np.clip(dt, 1e-6, None)  # guard against a zero/duplicate-year gap

    def run_filter(q_level: float, q_slope: float):
        """Standard Kalman filter forward pass. Returns filtered states,
        filtered covariances, one-step forecasts, and the Gaussian
        log-likelihood of the innovations (needed both for MLE fitting
        and for the final returned fit)."""
        state = np.array([y[0], 0.0])                 # x_0|0: start slope at 0, level at first obs
        cov = np.array([[obs_var[0], 0.0], [0.0, 1.0]])  # generous initial slope uncertainty

        filtered_states = np.zeros((n, 2))
        filtered_covs = np.zeros((n, 2, 2))
        forecast_states = np.zeros((n, 2))
        forecast_vars = np.zeros(n)
        residuals = np.zeros(n)
        loglik = 0.0

        filtered_states[0] = state
        filtered_covs[0] = cov
        forecast_states[0] = state
        forecast_vars[0] = cov[0, 0]

        for t in range(1, n):
            step = dt[t - 1]
            F = np.array([[1.0, step], [0.0, 1.0]])
            Q = np.array([[q_level * step, 0.0], [0.0, q_slope * step]])

            # Predict
            state_pred = F @ state
            cov_pred = F @ cov @ F.T + Q

            # Update (H = [1, 0] -- only the level is observed)
            forecast_val = state_pred[0]
            forecast_var = cov_pred[0, 0] + obs_var[t]
            forecast_var = max(forecast_var, 1e-8)  # numerical floor
            innovation = y[t] - forecast_val

            K = cov_pred[:, 0] / forecast_var  # Kalman gain (2,)
            state = state_pred + K * innovation
            cov = cov_pred - np.outer(K, cov_pred[0, :])

            filtered_states[t] = state
            filtered_covs[t] = cov
            forecast_states[t] = state_pred
            forecast_vars[t] = forecast_var
            residuals[t] = innovation / math.sqrt(forecast_var)

            loglik += -0.5 * (math.log(2 * math.pi * forecast_var) + innovation ** 2 / forecast_var)

        return filtered_states, filtered_covs, forecast_states, forecast_vars, residuals, loglik

    fit_method = "mle"
    diffs = np.diff(y)
    mom_var = float(np.var(diffs, ddof=1)) if len(diffs) >= 2 else 0.01
    mom_var = max(mom_var, 1e-6)

    if n >= MIN_YEARS_FOR_KALMAN_MLE:
        def loglik_objective(log_q_level: float, log_q_slope: float) -> float:
            """Objective _grid_refine_maximize_2d MAXIMIZES -- the raw
            (not negated) Kalman log-likelihood, log-parameterized so
            the search happens over an unconstrained real plane while
            q_level/q_slope themselves stay positive by construction."""
            q_level = math.exp(log_q_level)
            q_slope = math.exp(log_q_slope)
            try:
                *_, loglik = run_filter(q_level, q_slope)
            except (np.linalg.LinAlgError, FloatingPointError, ValueError):
                return float("-inf")
            return loglik if math.isfinite(loglik) else float("-inf")

        x0 = (math.log(mom_var / 2), math.log(mom_var / 10))
        log_q_level, log_q_slope, best_loglik, converged = _grid_refine_maximize_2d(loglik_objective, x0)

        if converged and math.isfinite(best_loglik):
            q_level, q_slope = math.exp(log_q_level), math.exp(log_q_slope)
            if q_level < 1e-8 and q_slope < 1e-8:
                fit_method = "method_of_moments_fallback"  # degenerate -- both variances collapsed to ~0
        else:
            fit_method = "method_of_moments_fallback"
    else:
        fit_method = "method_of_moments_fallback"

    if fit_method == "method_of_moments_fallback":
        q_level = mom_var
        q_slope = mom_var / 10.0  # slope process noise assumed an order of magnitude
                                   # calmer than level process noise -- a documented,
                                   # conservative default (a slope that itself
                                   # whips around as fast as the level would make
                                   # "acceleration" indistinguishable from noise)

    filtered_states, filtered_covs, forecast_states, forecast_vars, residuals, loglik = run_filter(q_level, q_slope)

    # --- Backward (Rauch-Tung-Striebel) smoother pass for the slope path ---
    # Uses ALL observed years to estimate the slope at each point (not
    # just past years, as the raw filter does), giving a materially
    # better "was growth accelerating THROUGH this window" picture --
    # standard practice (Durbin & Koopman 2012, ch. 4) whenever the full
    # series (not real-time filtering) is available, which it is here.
    smoothed_states = filtered_states.copy()
    smoothed_covs = filtered_covs.copy()
    for t in range(n - 2, -1, -1):
        step = dt[t]
        F = np.array([[1.0, step], [0.0, 1.0]])
        Q = np.array([[q_level * step, 0.0], [0.0, q_slope * step]])
        cov_pred = F @ filtered_covs[t] @ F.T + Q
        try:
            J = filtered_covs[t] @ F.T @ np.linalg.inv(cov_pred)
        except np.linalg.LinAlgError:
            J = np.zeros((2, 2))
        smoothed_states[t] = filtered_states[t] + J @ (smoothed_states[t + 1] - F @ filtered_states[t])
        smoothed_covs[t] = filtered_covs[t] + J @ (smoothed_covs[t + 1] - cov_pred) @ J.T

    smoothed_slopes = smoothed_states[:, 1]
    acceleration = None
    if n >= 3:
        # Second difference of the smoothed slope path, most recent
        # available pair -- positive means growth is speeding up,
        # negative means it is decelerating, near-zero means steady.
        # Uses actual dt so an irregular final gap is accounted for.
        d_slope_recent = (smoothed_slopes[-1] - smoothed_slopes[-2]) / dt[-1]
        d_slope_prior = (smoothed_slopes[-2] - smoothed_slopes[-3]) / dt[-2] if n >= 4 else d_slope_recent
        acceleration = float(d_slope_recent - d_slope_prior) if n >= 4 else float(d_slope_recent)

    return KalmanTrendFit(
        filtered_levels=filtered_states[:, 0],
        filtered_slopes=filtered_states[:, 1],
        smoothed_slopes=smoothed_slopes,
        final_slope=float(smoothed_states[-1, 1]),
        final_slope_variance=float(max(smoothed_covs[-1, 1, 1], 0.0)),
        acceleration=acceleration,
        one_step_forecast_level=float(forecast_states[-1][0] + smoothed_states[-1, 1] * dt[-1]) if n >= 2 else float(y[-1]),
        one_step_forecast_variance=float(forecast_vars[-1]),
        level_process_variance=float(q_level),
        slope_process_variance=float(q_slope),
        fit_method=fit_method,
        standardized_residuals=residuals,
        log_likelihood=float(loglik) if math.isfinite(loglik) else None,
    )


def cusum_structural_break(standardized_residuals: Sequence[float]) -> Dict[str, Optional[object]]:
    """CUSUM of recursive (standardized) residuals (Page 1954; Brown,
    Durbin & Evans 1975). Detects whether the series' relationship to
    its own fitted trend shifted at some point -- the signature of a
    genuine regime change (e.g. "emerging": years of flat/dark radiance
    followed by a real, sustained step up) rather than a continuously
    graduated trend the Theil-Sen/Kalman fit already captures on its own.

    Returns:
        cusum_path        : cumulative sum of standardized residuals
        max_abs_cusum      : largest |cusum| reached
        boundary_at_max     : the Brown-Durbin-Evans 5% boundary value at
                              that point in the series (boundary widens
                              with sqrt(n), so this is index-dependent)
        break_detected      : whether max_abs_cusum exceeded its boundary
        break_index         : index (into the input array) of the peak
                              deviation, if break_detected
    The first residual (index 0) is always 0 by this module's Kalman
    filter construction (no prior observation to form an innovation
    from) and is excluded from the CUSUM path, matching standard
    practice of starting recursive residuals from the second observation.
    """
    resid = np.asarray(standardized_residuals, dtype=float)
    usable = resid[1:]
    n = len(usable)
    if n < 3:
        return dict(cusum_path=None, max_abs_cusum=None, boundary_at_max=None, break_detected=False, break_index=None)

    cusum_path = np.cumsum(usable)
    # Boundary at step k (1-indexed within the usable series) per Brown,
    # Durbin & Evans's straight-line approximation, scaled by sqrt(n) of
    # the FULL usable series (the standard normalization -- the boundary
    # is a fixed pair of lines across the whole path, not recomputed
    # per-step).
    boundary = CUSUM_BOUNDARY_CONSTANT_5PCT * math.sqrt(n)

    max_idx = int(np.argmax(np.abs(cusum_path)))
    max_abs_cusum = float(np.abs(cusum_path[max_idx]))
    break_detected = max_abs_cusum > boundary

    return dict(
        cusum_path=cusum_path,
        max_abs_cusum=max_abs_cusum,
        boundary_at_max=boundary,
        break_detected=bool(break_detected),
        break_index=(max_idx + 1) if break_detected else None,  # +1 to re-align with original (pre-slice) indexing
    )


# =============================================================================
# Empirical Bayes shrinkage (local to this module -- "import for plumbing,
# duplicate for independent statistics", same convention development_activity.py
# / forced_traffic.py / anchor.py already document for themselves; this
# signal's noise characteristics -- a Kalman-filtered log-radiance slope --
# are nothing like purchasing_power's household-asset PCA index or
# development_activity's OSM binomial proportion)
# =============================================================================

def estimate_between_cell_variance(direct_estimates: Sequence[float], sampling_variances: Sequence[float]) -> float:
    """Method-of-moments estimate of A (the true between-cell variance of
    the underlying growth-rate signal, net of sampling noise). Clipped to
    0 per the standard convention (population.py's Fay-Herriot A,
    purchasing_power.py's and development_activity.py's own A all apply
    the identical clip) when the raw estimate goes negative -- an
    expected outcome with few/noisy cells, meaning "shrink fully to the
    prior," not an error.
    """
    if len(direct_estimates) < 2:
        return 0.0
    y = np.array(direct_estimates, dtype=float)
    d_bar = float(np.mean(sampling_variances)) if len(sampling_variances) else 0.0
    sample_var = float(np.var(y, ddof=1))
    return max(0.0, sample_var - d_bar)


def empirical_bayes_shrink_scalar(
    direct_estimate: Optional[float],
    direct_estimate_variance: Optional[float],
    prior_estimate: Optional[float],
    between_cell_variance_A: float,
) -> Tuple[Optional[float], Optional[float]]:
    """theta_hat = gamma * y_i + (1 - gamma) * prior,  gamma = A / (A + D_i).

    D_i here is the cell's OWN Kalman-filter final_slope_variance (a real
    fitted sampling variance, not an approximated constant -- an
    improvement this module can make that purchasing_power.py's simpler
    scalar case cannot, since the Kalman fit already produces a genuine
    per-cell variance for the exact quantity being shrunk).

    Returns (composite, gamma). If direct is None, returns (prior, 0.0).
    If prior is None, returns (direct, 1.0). If both are None, returns
    (None, None).
    """
    if direct_estimate is None and prior_estimate is None:
        return None, None
    if direct_estimate is None:
        return prior_estimate, 0.0
    if prior_estimate is None:
        return direct_estimate, 1.0

    d_i = direct_estimate_variance if direct_estimate_variance is not None else between_cell_variance_A
    d_i = max(d_i, 1e-8)
    denom = between_cell_variance_A + d_i
    gamma = between_cell_variance_A / denom if denom > 0 else 0.0
    composite = gamma * direct_estimate + (1 - gamma) * prior_estimate
    return composite, gamma


def implied_local_growth_rate(shrunk_log_slope: Optional[float]) -> Dict[str, Optional[float]]:
    """Convert a shrunk log-radiance slope into an "implied local GDP
    growth rate" range using the Henderson, Storeygard & Weil (2012)
    elasticity -- reported as a WIDE range (HSW2012_ELASTICITY_LOW to
    HSW2012_ELASTICITY_HIGH), not a point estimate, per this module's own
    HONEST LIMITATIONS section: applying a national/cross-region
    elasticity to a single ~2km-radius buffer is an ecological inference,
    not a locally-fitted or locally-validated rate, and Gibson, Olivia &
    Boe-Gibson (2020) / Chen & Nordhaus (2011/2019) both document real,
    substantial context-dependence in this elasticity that this module
    cannot correct for with better local statistics -- the RANGE itself
    is the honest output, and callers must not collapse it to the point
    estimate and treat it as precise.

    A log-slope of x corresponds to an (e^x - 1) proportional annual
    change in radiance; multiplying that (not x itself, to stay correct
    for larger slopes where the log-linear approximation degrades) by the
    elasticity gives the implied proportional GDP growth.
    """
    if shrunk_log_slope is None:
        return dict(point_pct=None, low_pct=None, high_pct=None)
    radiance_growth_pct = (math.exp(shrunk_log_slope) - 1.0) * 100.0
    return dict(
        point_pct=radiance_growth_pct * HSW2012_LIGHT_TO_GDP_ELASTICITY,
        low_pct=radiance_growth_pct * HSW2012_ELASTICITY_LOW,
        high_pct=radiance_growth_pct * HSW2012_ELASTICITY_HIGH,
    )


# =============================================================================
# Classification: Mahalanobis-distance nearest-prototype over standardized
# (growth, acceleration, volatility)
# =============================================================================

# Five hand-specified PROTOTYPE LOCATIONS in standardized (z-scored)
# feature space -- not five hand-specified DECISION THRESHOLDS. The
# distinction matters: which prototype a given cell is CLOSEST TO (by
# Mahalanobis distance, using the run corpus's own covariance -- a
# genuine multivariate-Gaussian-nearest-centroid classifier, the same
# family as Linear/Quadratic Discriminant Analysis with a shared/
# per-class covariance) is a data-relative judgment, not a fixed cutoff
# -- the same cell could classify differently across two different
# corpora of comparison cells, which is honest: "accelerating" is
# inherently a RELATIVE judgment against what else this system has seen.
#
# This is NOT a supervised classifier -- there is no ground-truth label
# set of "cells that were actually overheating" for this system to train
# against (fabricating one would be exactly the kind of fake this
# codebase's other engines already refuse to ship, per
# purchasing_power.py's own module docstring). The prototypes below are
# the analyst's structural definition of what each regime word MEANS in
# (growth, acceleration, volatility) space, not a fitted decision
# boundary -- the same role purchasing_power.py's OPPORTUNITY_HIGH_CLOSENESS
# threshold plays for itself, upgraded here to a full covariance-aware
# distance rather than a single scalar cutoff because momentum has three
# genuinely distinct axes of information (level of growth, direction of
# change in growth, and stability of that growth) that a single number
# cannot honestly compress.
MOMENTUM_PROTOTYPES: Dict[str, Tuple[float, float, float]] = {
    # (growth_z, acceleration_z, volatility_z)
    "accelerating":       (1.0,  1.0, 0.0),
    "stable":             (0.0,  0.0, -0.5),
    "declining":          (-1.0, -0.5, 0.0),
    "emerging":           (0.5,  1.5, 0.5),   # low/mid absolute growth but sharply
                                                # rising + a recent structural break
                                                # is what distinguishes "emerging"
                                                # from plain "accelerating" -- see
                                                # classify_momentum_regime()'s
                                                # break_bonus handling below
    "overheating":        (2.0, -1.0, 1.5),   # very high growth, but decelerating
                                                # and volatile -- the textbook
                                                # "unsustainable pace" signature
}
INSUFFICIENT_SIGNAL_LABEL = "insufficient_signal"


def classify_momentum_regime(
    shrunk_growth: Optional[float],
    acceleration: Optional[float],
    volatility: Optional[float],
    structural_break_detected: bool,
    corpus_growth: Sequence[float],
    corpus_acceleration: Sequence[float],
    corpus_volatility: Sequence[float],
) -> Dict[str, object]:
    """Nearest-prototype classification by Mahalanobis distance in
    standardized 3-feature space. See MOMENTUM_PROTOTYPES docstring for
    why prototypes-in-z-space rather than a fitted supervised model or a
    bare threshold ladder.

    With fewer than MIN_N_FOR_ZSCORE_CLASSIFICATION comparison cells in
    `corpus_*`, this function does NOT attempt to estimate a covariance
    matrix from too little data -- it falls back to fixed absolute
    thresholds (FALLBACK_GROWTH_THRESHOLD_LOG_SLOPE /
    FALLBACK_ACCELERATION_THRESHOLD), flagged via classification_method.

    A recently-detected structural break nudges the distance toward
    "emerging" specifically (per MOMENTUM_PROTOTYPES' own emerging
    prototype already encoding "elevated acceleration_z" -- the break
    flag is additionally used as a tie-breaker between "emerging" and
    "accelerating" when the two are otherwise close, since a graduating
    smooth acceleration and a sudden regime shift are the actual real-
    world distinction between those two words).
    """
    if shrunk_growth is None:
        return dict(label=INSUFFICIENT_SIGNAL_LABEL, classification_method="none", distances={})

    accel = acceleration if acceleration is not None else 0.0
    vol = volatility if volatility is not None else 0.0

    corpus_growth_arr = np.asarray(corpus_growth, dtype=float)
    corpus_growth_arr = corpus_growth_arr[~np.isnan(corpus_growth_arr)] if corpus_growth_arr.size else corpus_growth_arr

    if len(corpus_growth_arr) >= MIN_N_FOR_ZSCORE_CLASSIFICATION:
        method = "mahalanobis_zscore"
        raw_growth = np.nan_to_num(np.asarray(corpus_growth, dtype=float), nan=0.0)
        raw_accel = np.nan_to_num(np.asarray(corpus_acceleration, dtype=float), nan=0.0)
        raw_vol = np.nan_to_num(np.asarray(corpus_volatility, dtype=float), nan=0.0)
        # Cross-sectional winsorization of the COMPARISON CORPUS (not this
        # cell's own point, which must remain exactly as estimated) --
        # per Bluhm & Krause (2022)'s "top lights" finding that a handful
        # of very bright/very fast-moving cells (CBDs, mines, flares) can
        # be nonlinearly unlike the rest of the distribution; letting a
        # few such cells set the corpus mean/std would distort every
        # other cell's z-score. Only applied once the corpus is large
        # enough for a percentile to be meaningful.
        if len(raw_growth) >= MIN_CORPUS_FOR_WINSORIZE:
            raw_growth = winsorize_radiance(raw_growth)
            raw_accel = winsorize_radiance(raw_accel)
            raw_vol = winsorize_radiance(raw_vol)
        feat_matrix = np.column_stack([raw_growth, raw_accel, raw_vol])
        means = feat_matrix.mean(axis=0)
        stds = feat_matrix.std(axis=0, ddof=1)
        stds = np.where(stds < 1e-6, 1.0, stds)  # a zero-variance feature across
                                                   # the whole corpus can't inform
                                                   # distance -- treat as already
                                                   # standardized (no-op) rather
                                                   # than divide-by-zero

        z = np.array([(shrunk_growth - means[0]) / stds[0],
                      (accel - means[1]) / stds[1],
                      (vol - means[2]) / stds[2]])

        # Shared diagonal covariance across prototypes (a simplifying,
        # documented choice -- estimating a separate full covariance PER
        # class would need labeled class membership this system does not
        # have; a shared covariance across the corpus is the standard
        # "Linear Discriminant Analysis"-style simplification when only
        # one, unlabeled, pooled sample is available).
        cov = np.cov(((feat_matrix - means) / stds).T)
        cov = cov + np.eye(3) * 1e-6  # regularize against a singular/near-singular corpus covariance
        try:
            inv_cov = np.linalg.inv(cov)
        except np.linalg.LinAlgError:
            inv_cov = np.eye(3)
    else:
        method = "fixed_threshold_fallback"
        z = np.array([
            shrunk_growth / FALLBACK_GROWTH_THRESHOLD_LOG_SLOPE,
            accel / FALLBACK_ACCELERATION_THRESHOLD,
            vol,  # volatility has no natural absolute scale here -- left
                  # unstandardized in the fallback, meaning the fallback
                  # path relies primarily on growth/acceleration, which is
                  # documented behavior, not an oversight
        ])
        inv_cov = np.eye(3)

    distances = {}
    for label, prototype in MOMENTUM_PROTOTYPES.items():
        p = np.array(prototype)
        diff = z - p
        d2 = float(diff @ inv_cov @ diff.T)
        if label == "emerging" and structural_break_detected:
            d2 *= 0.7  # documented tie-break: a genuine detected regime
                       # shift makes "emerging" a better-supported label
                       # for the same underlying (growth, acceleration)
                       # point than "accelerating", which implies a
                       # continuous graduation rather than a break
        distances[label] = d2

    best_label = min(distances, key=distances.get)
    return dict(label=best_label, classification_method=method, distances=distances)


def momentum_score_from_features(
    shrunk_growth: Optional[float],
    empirical_bayes_gamma: Optional[float],
    label: str,
) -> Optional[float]:
    """0-100 composite "economic momentum score" -- a business_conclusion-
    level summary, NOT a re-derivation of GDP growth. Maps the shrunk
    log-slope through a logistic squashing function (so a very large
    slope does not linearly blow past 100, and a very negative one does
    not blow past 0 -- the same "bounded, monotonic, saturating" shape a
    0-100 index needs), centered at 0 (no growth = score 50), scaled so
    that FALLBACK_GROWTH_THRESHOLD_LOG_SLOPE (this module's own
    documented "clearly meaningful" growth threshold) lands at
    approximately score 68 -- i.e. one "meaningful growth unit" moves the
    score about 18 points off center, a deliberately moderate (not
    saturating-on-one-observation) sensitivity given how noisy the
    underlying signal is.

    Then damps the result toward 50 (neutral) by empirical_bayes_gamma
    -- a cell whose own signal was almost entirely replaced by the
    county prior (gamma near 0) should not present as confidently as one
    with strong direct evidence (gamma near 1), even if the point
    estimate happens to be identical; this mirrors development_activity.py's
    own "gamma-weighted confidence" treatment of its shrunk estimate.
    """
    if shrunk_growth is None:
        return None
    scale = FALLBACK_GROWTH_THRESHOLD_LOG_SLOPE / 0.75  # logistic(0.75) ~ 0.679 -> ~68 points off center at that slope
    raw = 100.0 / (1.0 + math.exp(-shrunk_growth / scale))
    gamma = empirical_bayes_gamma if empirical_bayes_gamma is not None else 0.5
    damped = 50.0 + (raw - 50.0) * gamma
    return float(np.clip(damped, 0.0, 100.0))


# =============================================================================
# Result container + evidence text
# =============================================================================

@dataclass
class EconomicMomentumResult:
    radius_m: int = PRIMARY_RADIUS_M
    years_requested: List[int] = field(default_factory=list)
    years_observed: List[int] = field(default_factory=list)
    latest_radiance: Optional[float] = None
    latest_year: Optional[int] = None
    data_is_effectively_dark: bool = False

    theil_sen_slope: Optional[float] = None
    mann_kendall: Dict[str, Optional[float]] = field(default_factory=dict)

    kalman_fit_method: Optional[str] = None
    kalman_final_slope: Optional[float] = None
    kalman_final_slope_variance: Optional[float] = None
    kalman_acceleration: Optional[float] = None
    kalman_volatility: Optional[float] = None  # std of standardized residuals -- see compute_economic_momentum_result

    structural_break_detected: bool = False
    structural_break_year: Optional[int] = None

    county_prior_log_slope: Optional[float] = None
    between_cell_variance_A: Optional[float] = None
    county_prior_source: str = ""
    county_prior_n_cells_pooled: int = 0
    empirical_bayes_gamma: Optional[float] = None
    shrunk_growth_log_slope: Optional[float] = None

    implied_local_growth_rate: Dict[str, Optional[float]] = field(default_factory=dict)
    momentum_classification: str = INSUFFICIENT_SIGNAL_LABEL
    classification_method: str = "none"
    economic_momentum_score: Optional[float] = None

    warnings: List[str] = field(default_factory=list)


def economic_momentum_evidence(result: EconomicMomentumResult) -> List[str]:
    """Human-facing evidence strings for BusinessReport.evidence /
    .momentum_summary, mirroring mobility_evidence() /
    development_activity_evidence()'s role for their own engines. Always
    includes the ecological-inference caveat on the implied growth rate
    when that feature is present, per module docstring's HONEST
    LIMITATIONS -- this text is the load-bearing place that caveat
    reaches the eventual report reader, not just this module's own
    internal docstring.
    """
    lines: List[str] = []

    if result.data_is_effectively_dark:
        lines.append(
            f"No detectable nighttime-light activity across {len(result.years_observed)} years of VIIRS "
            f"satellite composites within {result.radius_m}m -- consistent with a rural/undeveloped area, "
            f"or with a genuinely new commercial area not yet visible to satellite; momentum could not be assessed."
        )
        return lines

    if result.momentum_classification == INSUFFICIENT_SIGNAL_LABEL:
        lines.append(
            f"Only {len(result.years_observed)} year(s) of usable VIIRS satellite radiance data were available "
            f"within {result.radius_m}m (minimum {MIN_YEARS_FOR_TREND} required) -- economic momentum trend "
            f"could not be estimated for this location."
        )
        return lines

    direction = {
        "accelerating": "growing, at an increasing pace",
        "stable": "growing steadily, without a clear acceleration or deceleration",
        "declining": "showing a declining nighttime-light trend",
        "emerging": "showing a recent, sharp upward shift in nighttime-light activity",
        "overheating": "growing very rapidly, but with signs of decelerating or unstable pace",
    }.get(result.momentum_classification, "showing an unclear momentum signal")

    lines.append(
        f"Satellite-measured nighttime economic activity (VIIRS DNB, {len(result.years_observed)} years, "
        f"{result.years_observed[0]}-{result.years_observed[-1]}) around this location is {direction} "
        f"(classification: {result.momentum_classification}, method={result.classification_method})."
    )

    if result.mann_kendall.get("significant_05"):
        lines.append(
            f"The multi-year trend is statistically significant (Mann-Kendall, autocorrelation-corrected "
            f"p={result.mann_kendall['p_value']:.3f})."
        )
    elif result.mann_kendall.get("p_value") is not None:
        lines.append(
            f"The multi-year trend did not reach standard statistical significance "
            f"(p={result.mann_kendall['p_value']:.3f}) -- treat the direction as suggestive, not confirmed."
        )

    if result.structural_break_detected:
        lines.append(
            f"A structural break in the nighttime-light trajectory was detected around "
            f"{result.structural_break_year if result.structural_break_year is not None else 'the recent period'} "
            f"-- a real shift in trajectory, not just continued historical growth."
        )

    if result.implied_local_growth_rate.get("point_pct") is not None:
        igr = result.implied_local_growth_rate
        lines.append(
            f"Applying the Henderson-Storeygard-Weil (2012) nightlights-to-GDP elasticity gives an IMPLIED "
            f"local growth rate of roughly {igr['low_pct']:.1f}% to {igr['high_pct']:.1f}% annually "
            f"(point estimate {igr['point_pct']:.1f}%) -- this is a national-average elasticity applied to "
            f"one small area (an ecological inference, per Gibson, Olivia & Boe-Gibson 2020), NOT a locally "
            f"measured or validated growth rate, and should be read as a wide directional range, not a precise figure."
        )

    if result.county_prior_source == "cold_start_placeholder":
        lines.append(
            "This engine has not yet accumulated enough history in this county to pool a real county-level "
            "momentum prior -- this estimate used a neutral cold-start placeholder and will become more "
            "reliable as more reports are generated in this county."
        )

    lines.extend(result.warnings)
    return lines
# =============================================================================
# APPEND THIS ENTIRE BLOCK TO THE END OF business_intel/engines/economic_momentum.py
# (i.e. directly after the existing economic_momentum_evidence() function --
# do not touch anything above it, this only ADDS the four pieces that were
# missing: radiance-series assembly, county-pooled Empirical Bayes prior,
# a nationwide classification corpus, the orchestrator that wires the
# pure-math layer together, and the Django-facing run() entry point.)
# =============================================================================

# --- Feature-store identity strings (FeatureObservation.feature_name) ---
# Kept as named constants (not inlined string literals scattered through
# run()/the prior-pooling functions) for the same reason
# development_activity.py's FEATURE_NAME_RAW_INTENSITY is a constant: the
# prior-pooling function and the writer below must agree on the exact
# string, and a typo in one but not the other would silently break county
# pooling without raising anywhere.
FEATURE_NAME_LATEST_RADIANCE = "latest_radiance_masked"
FEATURE_NAME_THEIL_SEN_SLOPE = "theil_sen_log_slope"
FEATURE_NAME_KALMAN_SLOPE = "kalman_final_log_slope"
FEATURE_NAME_KALMAN_ACCELERATION = "kalman_acceleration"
FEATURE_NAME_KALMAN_VOLATILITY = "kalman_volatility"
FEATURE_NAME_SHRUNK_GROWTH = "shrunk_growth_log_slope"
FEATURE_NAME_MOMENTUM_SCORE = "economic_momentum_score"

# County-level radioactivity is measured on a completely different scale
# than population.py's Fay-Herriot count-based A or development_activity.py's
# proportion-based A -- deliberately its own constant name, not reused,
# even though the shrinkage machinery (estimate_between_cell_variance /
# empirical_bayes_shrink_scalar) is shared/imported.


# =============================================================================
# Radiance-series assembly (pure orchestration over the provider ABC --
# no statistics happen here, only fetching + defensive negative-value
# clipping per module docstring's own documented edge case)
# =============================================================================

def assemble_radiance_series(
    cell,
    provider: VIIRSAnnualCompositeProvider,
    years: Sequence[int],
    radius_m: int = PRIMARY_RADIUS_M,
) -> List[RadianceYearObservation]:
    """Fetch one RadianceYearObservation per year in `years`, via
    `provider`. Never raises -- a provider exception for a single year is
    caught and converted into a failed RadianceYearObservation for that
    year specifically, so one bad year does not take down every other
    year's otherwise-good fetch (the same "one missing/bad year degrades,
    doesn't crash" discipline the module docstring's EDGE CASES section
    already commits to for the statistics layer; this is that same
    discipline applied one level up, at the fetch layer).

    Does not sort, dedupe, or filter -- assemble_radiance_series's job is
    purely "ask the provider for these years", not the ordering/
    truthiness judgments compute_economic_momentum_result() below makes.
    """
    observations: List[RadianceYearObservation] = []
    for year in years:
        try:
            obs = provider.fetch_annual_radiance(cell, year, radius_m)
        except Exception as exc:  # noqa: BLE001 -- deliberately broad: a
            # provider implementation (network client, GEE client, a test
            # double) can fail in ways this module cannot enumerate in
            # advance; the contract this function guarantees its caller
            # is "you get one RadianceYearObservation per requested year,
            # always", not "the provider never raises".
            logger.warning(
                "VIIRS provider raised fetching year=%s radius_m=%s cell=%s: %s",
                year, radius_m, getattr(cell, "geohash", "?"), exc,
            )
            obs = RadianceYearObservation(
                year=year, mean_radiance_masked=None, pixel_count=0,
                cloud_free_obs_mean=None, succeeded=False,
                warning=f"Provider raised {type(exc).__name__}: {exc}",
            )
        observations.append(obs)
    return observations


# =============================================================================
# Per-year observation-variance heuristic (cf_cvg -> Kalman observation
# noise) -- see module docstring's cf_cvg edge case. No published
# per-pixel VIIRS DNB annual-composite error model exists for this system
# to cite (EOG's own product documentation reports composite-level
# quality flags, not a numeric per-pixel variance), so this is a
# documented, monotonic heuristic -- lower cloud-free-observation count
# for a year means a noisier composite for that year, and this function's
# only job is to translate "noisier" into a larger Kalman observation
# variance for that year, not to claim a validated absolute noise figure.
# =============================================================================

BASE_OBSERVATION_LOG_VARIANCE = 0.02
# Reference cf_cvg (mean cloud-free nightly observations backing a
# composite pixel) above which a year is treated as "fully confident" --
# EOG's own Annual VNL documentation describes typical global coverage on
# the order of 100+ cloud-free observations per year for most
# non-polar/non-persistently-cloudy locations; used here only as a
# relative denominator, not asserted as a precise universal constant.
CF_CVG_FULL_CONFIDENCE_REFERENCE = 100.0
# A year with unknown/zero cf_cvg is not treated as infinitely
# untrustworthy (which would make the Kalman filter effectively discard
# it) -- capped at a documented, finite penalty instead.
MAX_OBSERVATION_VARIANCE_INFLATION = 4.0


def _observation_log_variance_for_year(cloud_free_obs_mean: Optional[float]) -> float:
    """Heteroskedastic Kalman observation-noise variance for one year's
    log-radiance observation, inflated for low cf_cvg. See module-level
    heuristic note above -- this is a documented design choice, not a
    citation-backed noise model, and is flagged as such wherever this
    function's output materially changes a downstream conclusion (it
    generally does not: it changes RELATIVE weighting between years, not
    whether a trend is estimated at all).
    """
    if cloud_free_obs_mean is None or cloud_free_obs_mean <= 0:
        return BASE_OBSERVATION_LOG_VARIANCE * MAX_OBSERVATION_VARIANCE_INFLATION
    inflation = min(MAX_OBSERVATION_VARIANCE_INFLATION, max(1.0, CF_CVG_FULL_CONFIDENCE_REFERENCE / cloud_free_obs_mean))
    return BASE_OBSERVATION_LOG_VARIANCE * inflation


# =============================================================================
# Django-facing: county-pooled Empirical Bayes prior for the GROWTH SLOPE,
# and a nationwide corpus for the Mahalanobis classification step.
# Same "let the system learn its own baseline over time" approach
# development_activity.py's county_construction_prior() already applies
# for itself, since no published county-level nightlight-momentum series
# exists for this system to cite instead (see module docstring).
# =============================================================================

def economic_momentum_county_prior(county: str, exclude_cell_id=None):
    """Real Empirical-Bayes prior for the log-radiance growth slope in
    county `county`, pooled from this engine's OWN accumulated
    FeatureObservation history (feature_name=FEATURE_NAME_KALMAN_SLOPE,
    engine_number=ENGINE_NUMBER, is_latest=True) -- i.e. every other
    cell in the same county this engine has already produced a direct
    (unshrunk) Kalman slope estimate for. Mirrors
    development_activity.county_construction_prior()'s contract exactly:
    returns (prior_mean, between_cell_variance_A, prior_source,
    n_cells_pooled), with prior_source distinguishing a real pooled
    prior ("county_pooled_empirical") from the neutral cold-start
    placeholder -- callers must check this string, not just the numeric
    value.

    Each pooled cell's OWN Kalman-fitted final_slope_variance (written
    alongside kalman_final_log_slope as that FeatureObservation row's
    `uncertainty`, stored as a standard deviation -- see run()'s writer
    below) is used as that cell's sampling variance for the
    method-of-moments A estimate, exactly the same role
    development_activity.py's wilson_variance_from_counts() plays for
    its own county prior -- a REAL per-cell sampling variance, not a
    corpus-wide approximation.
    """
    from ..models import FeatureObservation  # local import: keep pure-math layer DB-import-free

    qs = FeatureObservation.objects.filter(
        engine_number=ENGINE_NUMBER,
        feature_name=FEATURE_NAME_KALMAN_SLOPE,
        is_latest=True,
        location_cell__county=county,
    )
    if exclude_cell_id is not None:
        qs = qs.exclude(location_cell_id=exclude_cell_id)

    rows = list(qs.values_list("value_numeric", "uncertainty"))
    if len(rows) < MIN_COUNTY_PRIOR_SAMPLE_CELLS:
        return COLD_START_PRIOR_LOG_SLOPE, COLD_START_BETWEEN_CELL_VARIANCE, "cold_start_placeholder", len(rows)

    direct_estimates = [float(v) for v, _u in rows]
    # `uncertainty` was written as a standard deviation (sqrt of the
    # Kalman final_slope_variance) -- squared back here to recover the
    # variance estimate_between_cell_variance() expects. A row with no
    # recorded uncertainty (should not occur post-wiring, but defensively
    # handled) falls back to the cold-start between-cell variance as a
    # conservative per-cell sampling-variance stand-in, same convention
    # development_activity.py applies when a denom is missing.
    sampling_variances = [
        (float(u) ** 2) if u is not None else COLD_START_BETWEEN_CELL_VARIANCE
        for _v, u in rows
    ]
    prior_mean = float(np.mean(direct_estimates))
    A_hat = estimate_between_cell_variance(direct_estimates, sampling_variances)
    return prior_mean, A_hat, "county_pooled_empirical", len(rows)


# Cap on how many cells feed the Mahalanobis classification corpus query
# -- a nationwide, unbounded scan grows without limit as the system
# accumulates history; a large-but-fixed cap keeps classification stable
# and query latency bounded once the corpus is well past
# MIN_N_FOR_ZSCORE_CLASSIFICATION anyway, which is all it needs to be.
CLASSIFICATION_CORPUS_MAX_CELLS = 5000


def economic_momentum_classification_corpus(exclude_cell_id=None, limit: int = CLASSIFICATION_CORPUS_MAX_CELLS):
    """Pulls this engine's own accumulated (shrunk_growth, acceleration,
    volatility) triples across ALL counties (not county-scoped, unlike
    the growth prior above) to serve as the comparison corpus for
    classify_momentum_regime()'s Mahalanobis distance.

    Deliberately NATIONWIDE rather than county-scoped: MIN_N_FOR_ZSCORE_
    CLASSIFICATION cells of real, comparable (growth, acceleration,
    volatility) triples is a much higher bar to clear per-county than
    the county growth-prior's own MIN_COUNTY_PRIOR_SAMPLE_CELLS, and
    "how this cell's momentum compares to what the system has seen
    anywhere" is still an honest, documented definition of "accelerating"
    / "overheating" etc. -- the module docstring's own MOMENTUM_PROTOTYPES
    note already states classification is inherently corpus-relative, and
    does not require the comparison corpus to be county-scoped.

    Only cells with ALL THREE features present (a real intersection, not
    a zero-filled union) are returned, since classify_momentum_regime()'s
    covariance estimate would otherwise be corrupted by cells missing one
    or more of the three axes.
    """
    from ..models import FeatureObservation  # local import: keep pure-math layer DB-import-free

    def _feature_map(feature_name: str) -> Dict[int, float]:
        qs = FeatureObservation.objects.filter(
            engine_number=ENGINE_NUMBER, feature_name=feature_name, is_latest=True,
        )
        if exclude_cell_id is not None:
            qs = qs.exclude(location_cell_id=exclude_cell_id)
        return {cell_id: float(val) for cell_id, val in qs.values_list("location_cell_id", "value_numeric") if val is not None}

    growth_map = _feature_map(FEATURE_NAME_SHRUNK_GROWTH)
    accel_map = _feature_map(FEATURE_NAME_KALMAN_ACCELERATION)
    vol_map = _feature_map(FEATURE_NAME_KALMAN_VOLATILITY)

    common_ids = list(set(growth_map) & set(accel_map) & set(vol_map))
    if limit and len(common_ids) > limit:
        common_ids = common_ids[:limit]

    corpus_growth = [growth_map[i] for i in common_ids]
    corpus_acceleration = [accel_map[i] for i in common_ids]
    corpus_volatility = [vol_map[i] for i in common_ids]
    return corpus_growth, corpus_acceleration, corpus_volatility


# =============================================================================
# Orchestrator: wires the pure-math layer above into one EconomicMomentumResult.
# No Django/DB/network imports -- takes already-fetched observations and an
# already-pooled prior as plain arguments, same "pure function of its
# inputs, testable without Django" discipline every other compute_*_result
# function in this codebase (development_activity's own
# compute_development_activity_result, market_gap's, etc.) already follows.
# =============================================================================

def compute_economic_momentum_result(
    observations: Sequence[RadianceYearObservation],
    radius_m: int,
    prior_log_slope: Optional[float],
    between_cell_variance_A: float,
    prior_source: str,
    prior_n_cells_pooled: int,
    corpus_growth: Sequence[float] = (),
    corpus_acceleration: Sequence[float] = (),
    corpus_volatility: Sequence[float] = (),
) -> EconomicMomentumResult:
    result = EconomicMomentumResult(
        radius_m=radius_m,
        years_requested=[o.year for o in observations],
    )

    # --- Gather usable (succeeded, non-None) observations, defensively
    # clipping any negative radiance a provider implementation bug (or a
    # mocked/test provider) might hand in -- see module docstring's
    # negative-radiance edge case. This module only ever expects to
    # RECEIVE already-masked, non-negative EOG values; this clip is a
    # defensive backstop, not the primary correctness mechanism.
    usable: List[RadianceYearObservation] = []
    for obs in observations:
        if not obs.succeeded or obs.mean_radiance_masked is None:
            if obs.warning:
                result.warnings.append(f"Year {obs.year}: {obs.warning}")
            continue
        if obs.mean_radiance_masked < 0:
            result.warnings.append(
                f"Year {obs.year}: provider returned negative radiance ({obs.mean_radiance_masked:.4f}); clipped to 0.0."
            )
            obs.mean_radiance_masked = 0.0
        usable.append(obs)

    usable.sort(key=lambda o: o.year)
    result.years_observed = [o.year for o in usable]

    if not usable:
        result.warnings.append("No usable VIIRS radiance observations were fetched for any requested year.")
        result.momentum_classification = INSUFFICIENT_SIGNAL_LABEL
        return result

    result.latest_year = usable[-1].year
    result.latest_radiance = usable[-1].mean_radiance_masked

    nonzero_years = [o for o in usable if o.mean_radiance_masked and o.mean_radiance_masked > 0]

    # --- Dark-cell gate (see module docstring's all-zero-years edge case) ---
    if len(nonzero_years) < MIN_NONZERO_YEARS:
        result.data_is_effectively_dark = True
        result.momentum_classification = INSUFFICIENT_SIGNAL_LABEL
        return result

    # --- Minimum-years-for-trend gate ---
    if len(usable) < MIN_YEARS_FOR_TREND:
        result.momentum_classification = INSUFFICIENT_SIGNAL_LABEL
        result.warnings.append(
            f"Only {len(usable)} usable year(s) of VIIRS data (minimum {MIN_YEARS_FOR_TREND} required) -- trend not attempted."
        )
        return result

    years_arr = [o.year for o in usable]
    raw_values = [o.mean_radiance_masked for o in usable]
    log_values = [log_transform_radiance(v) for v in raw_values]

    # --- Theil-Sen slope + Mann-Kendall significance ---
    theil_slope, _theil_intercept = theil_sen_slope(years_arr, log_values)
    result.theil_sen_slope = theil_slope
    result.mann_kendall = mann_kendall_hamed_rao(years_arr, log_values)

    # --- Kalman local-linear-trend fit, heteroskedastic per cf_cvg ---
    observation_variances = [_observation_log_variance_for_year(o.cloud_free_obs_mean) for o in usable]
    kalman_fit = local_linear_trend_kalman_filter(years_arr, log_values, observation_variances)

    if kalman_fit is None:
        # Only reachable if usable has fewer than 3 points, which
        # MIN_YEARS_FOR_TREND (>= 4) already rules out -- kept as a
        # defensive branch rather than an assumption, per this module's
        # own "handle it explicitly, don't just avoid it" discipline.
        result.warnings.append(
            "Kalman local-linear-trend fit could not be computed despite meeting the minimum-years gate -- "
            "falling back to the Theil-Sen slope only; acceleration, structural-break, and classification are unavailable."
        )
        result.momentum_classification = INSUFFICIENT_SIGNAL_LABEL
        return result

    result.kalman_fit_method = kalman_fit.fit_method
    result.kalman_final_slope = kalman_fit.final_slope
    result.kalman_final_slope_variance = kalman_fit.final_slope_variance
    result.kalman_acceleration = kalman_fit.acceleration

    if len(kalman_fit.standardized_residuals) > 2:
        # Index 0 is always 0 by construction (no prior observation to
        # form an innovation from -- same convention cusum_structural_break
        # documents for itself) and excluded here for the same reason.
        result.kalman_volatility = float(np.std(kalman_fit.standardized_residuals[1:], ddof=1))

    if kalman_fit.fit_method == "method_of_moments_fallback" and len(usable) >= MIN_YEARS_FOR_KALMAN_MLE:
        result.warnings.append(
            "Kalman MLE optimizer did not converge to a well-specified fit -- used a method-of-moments "
            "variance fallback instead (see module docstring's optimizer-failure edge case)."
        )

    # --- Structural break detection (CUSUM) ---
    cusum = cusum_structural_break(kalman_fit.standardized_residuals)
    result.structural_break_detected = bool(cusum["break_detected"])
    if result.structural_break_detected and cusum["break_index"] is not None:
        idx = cusum["break_index"]
        if 0 <= idx < len(years_arr):
            result.structural_break_year = years_arr[idx]

    # --- Empirical Bayes shrinkage of the direct Kalman slope toward the county prior ---
    shrunk, gamma = empirical_bayes_shrink_scalar(
        direct_estimate=kalman_fit.final_slope,
        direct_estimate_variance=kalman_fit.final_slope_variance,
        prior_estimate=prior_log_slope,
        between_cell_variance_A=between_cell_variance_A,
    )
    result.shrunk_growth_log_slope = shrunk
    result.empirical_bayes_gamma = gamma
    result.county_prior_log_slope = prior_log_slope
    result.between_cell_variance_A = between_cell_variance_A
    result.county_prior_source = prior_source
    result.county_prior_n_cells_pooled = prior_n_cells_pooled

    # --- Ecological-inference implied growth rate (wide range, not a point estimate) ---
    result.implied_local_growth_rate = implied_local_growth_rate(shrunk)

    # --- Classification ---
    classification = classify_momentum_regime(
        shrunk_growth=shrunk,
        acceleration=result.kalman_acceleration,
        volatility=result.kalman_volatility,
        structural_break_detected=result.structural_break_detected,
        corpus_growth=corpus_growth,
        corpus_acceleration=corpus_acceleration,
        corpus_volatility=corpus_volatility,
    )
    result.momentum_classification = classification["label"]
    result.classification_method = classification["classification_method"]

    # --- 0-100 composite score, damped by shrinkage confidence ---
    result.economic_momentum_score = momentum_score_from_features(shrunk, gamma, result.momentum_classification)

    return result


# =============================================================================
# BusinessReport.momentum_summary / .timing -- the two fields tasks.py's own
# REWRITE NOTE names as blank specifically because Engine 18 did not exist.
# Kept as a small, separate function (not inlined into run()) so
# tasks.generate_business_report can call it directly against an
# EconomicMomentumResult without re-deriving report-facing text itself --
# same "engine module owns its own report-facing text" split
# development_activity_evidence() already establishes.
# =============================================================================

_TIMING_TEXT_BY_CLASSIFICATION = {
    "accelerating": "Market momentum is currently accelerating -- entering now is timed with, not ahead of, the visible trend.",
    "stable": "Market momentum is steady -- timing is not a strong factor either for or against entering now.",
    "declining": "Market momentum is declining -- entering now would be against the visible trend; consider whether the underlying cause is temporary.",
    "emerging": "A recent structural shift suggests this market may be emerging -- early entry carries more uncertainty but potentially more upside than entering an already-established trend.",
    "overheating": "Growth has been very rapid but shows signs of decelerating or unstable pace -- entering now risks buying into a peak rather than a sustained trend.",
    INSUFFICIENT_SIGNAL_LABEL: "Momentum could not be assessed for this location -- insufficient satellite data to judge market timing from this signal alone.",
}


def momentum_report_fields(result: EconomicMomentumResult) -> Dict[str, str]:
    """Returns {"momentum_summary": ..., "timing": ...} for
    BusinessReport. momentum_summary is the first (headline) line of
    economic_momentum_evidence(result); timing is a separate,
    classification-keyed sentence -- kept distinct from the summary
    because "what is happening" (summary) and "what that implies for
    entry timing" (timing) are different questions this module can
    answer independently, and BusinessReport models them as two fields
    for exactly that reason.
    """
    evidence_lines = economic_momentum_evidence(result)
    momentum_summary = evidence_lines[0] if evidence_lines else "Economic momentum could not be assessed for this location."
    timing = _TIMING_TEXT_BY_CLASSIFICATION.get(result.momentum_classification, _TIMING_TEXT_BY_CLASSIFICATION[INSUFFICIENT_SIGNAL_LABEL])
    return {"momentum_summary": momentum_summary, "timing": timing}


# =============================================================================
# Django-facing entry point -- matches compute_development_activity_score's
# (score, evidence) tuple contract exactly, so tasks.py's _run_engine
# wiring pattern (see development_activity's wiring diff) needs no special
# casing for this engine.
# =============================================================================

METRIC_FIELDS = [
    # (feature_name, unit, value_type, derivation_level, method)
    (FEATURE_NAME_LATEST_RADIANCE, "nW/cm2/sr", "numeric", "observation", "viirs_dnb_annual_composite_masked"),
    (FEATURE_NAME_THEIL_SEN_SLOPE, "log_radiance_per_year", "numeric", "model_estimate", "theil_sen_median_pairwise_slope"),
    (FEATURE_NAME_KALMAN_SLOPE, "log_radiance_per_year", "numeric", "model_estimate", "local_linear_trend_kalman_mle"),
    (FEATURE_NAME_KALMAN_ACCELERATION, "log_radiance_per_year2", "numeric", "model_estimate", "kalman_smoothed_slope_second_difference"),
    (FEATURE_NAME_KALMAN_VOLATILITY, "standardized_residual_stdev", "numeric", "model_estimate", "kalman_standardized_residual_stdev"),
    (FEATURE_NAME_SHRUNK_GROWTH, "log_radiance_per_year", "numeric", "model_estimate", "empirical_bayes_shrinkage_method_of_moments_county_pooled"),
    (FEATURE_NAME_MOMENTUM_SCORE, "index_0_100", "numeric", "business_conclusion", "momentum_score_from_features"),
]


def run(
    query,
    years: Optional[Sequence[int]] = None,
    radius_m: int = PRIMARY_RADIUS_M,
    provider: Optional[VIIRSAnnualCompositeProvider] = None,
) -> dict:
    """Real Django-facing Engine 18 entry point.

    REVISION (post-review, matching the SAME wiring-convention fix
    development_activity.py needed): earlier drafts of this function
    returned a bare (score, evidence) tuple, mirroring
    compute_development_activity_score()'s pre-fix shape -- exactly the
    naming/shape mismatch that kept Engine 12 out of tasks.py's
    `_run_engine(risks, label, fn, *args, **kwargs)` calling convention.
    Every ACTUALLY-WIRED engine's run() (anchor.run, competition.run,
    market_gap.run, etc.) returns a dict with an "engine_run" key --
    _run_engine() specifically does `result.get("engine_run") if
    isinstance(result, dict) else None` to fold degraded/failed status
    into `risks`. This function now returns that same dict shape so it
    plugs into tasks.py's existing `_run_engine(risks, "Economic
    Momentum", economic_momentum.run, query)` call with ZERO special
    casing -- no tuple-unwrapping helper needed in tasks.py.

    - Fetches DEFAULT_LOOKBACK_YEARS years of VIIRS annual composite
      radiance (or `years` if the caller overrides it) via `provider`
      (EOGVIIRSAnnualCompositeProvider by default -- injectable for
      tests, same as every other engine's provider-ABC pattern).
    - Pools a real county-level Empirical Bayes prior via
      economic_momentum_county_prior() and a nationwide classification
      corpus via economic_momentum_classification_corpus() -- not
      hand-picked constants.
    - Writes FeatureObservation rows (METRIC_FIELDS above) plus an
      EngineRun row.
    - Returns {"cell": cell, "score": economic_momentum_score,
      "result": EconomicMomentumResult, "evidence": {...},
      "report_fields": {"momentum_summary": ..., "timing": ...},
      "engine_run": run_row} -- tasks.py reads "report_fields" directly
      to populate BusinessReport.momentum_summary/.timing without a
      second FeatureObservation lookup.
    """
    from ..models import EngineRun, FeatureObservation
    from django.utils import timezone as _timezone

    started_at = _timezone.now()
    cell = query.location_cell
    county = getattr(cell, "county", "") or ""
    fetch_years = list(years) if years is not None else _default_years()
    active_provider = provider or EOGVIIRSAnnualCompositeProvider()

    run_row = EngineRun.objects.create(query=query, engine_number=ENGINE_NUMBER, status="succeeded", started_at=started_at)

    observations = assemble_radiance_series(cell, active_provider, fetch_years, radius_m)

    if not any(o.succeeded for o in observations):
        msg = (
            "VIIRS annual radiance provider was unavailable for every requested year "
            "(network/auth failure or no credentials configured) -- Economic Momentum engine could not run."
        )
        logger.warning("Economic Momentum engine: %s (cell=%s)", msg, cell.geohash)
        run_row.status = "failed"
        run_row.error_message = msg
        run_row.finished_at = _timezone.now()
        run_row.save(update_fields=["status", "error_message", "finished_at"])
        return {"cell": cell, "score": None, "evidence": {"warnings": [msg]}, "report_fields": None, "engine_run": run_row}

    prior_log_slope, between_cell_variance_A, prior_source, n_pooled = (
        economic_momentum_county_prior(county, exclude_cell_id=cell.id) if county
        else (COLD_START_PRIOR_LOG_SLOPE, COLD_START_BETWEEN_CELL_VARIANCE, "cold_start_placeholder", 0)
    )
    corpus_growth, corpus_acceleration, corpus_volatility = economic_momentum_classification_corpus(exclude_cell_id=cell.id)

    result = compute_economic_momentum_result(
        observations, radius_m=radius_m,
        prior_log_slope=prior_log_slope, between_cell_variance_A=between_cell_variance_A,
        prior_source=prior_source, prior_n_cells_pooled=n_pooled,
        corpus_growth=corpus_growth, corpus_acceleration=corpus_acceleration, corpus_volatility=corpus_volatility,
    )
    if not county:
        result.warnings.append(
            "No county on record for this cell -- county-level momentum prior pooling not possible; used cold-start placeholder."
        )

    evidence_lines = economic_momentum_evidence(result)
    report_fields = momentum_report_fields(result)
    evidence = {
        "lines": evidence_lines,
        "engine_run_status": "ok" if not (result.data_is_effectively_dark or result.momentum_classification == INSUFFICIENT_SIGNAL_LABEL) else "ok_insufficient_signal",
    }

    features_written = 0
    for field_name, unit, value_type, derivation_level, method in METRIC_FIELDS:
        value = getattr(result, field_name if field_name != FEATURE_NAME_LATEST_RADIANCE else "latest_radiance", None)
        # FEATURE_NAME_LATEST_RADIANCE's attribute on EconomicMomentumResult
        # is `latest_radiance`, not a same-named attribute -- every other
        # entry in METRIC_FIELDS uses a feature_name that IS also the
        # result attribute name, so this one explicit mapping is called
        # out rather than silently relying on getattr(..., field_name)
        # matching by coincidence for the other six.
        if value is None:
            continue

        uncertainty = None
        if field_name == FEATURE_NAME_KALMAN_SLOPE and result.kalman_final_slope_variance is not None:
            uncertainty = math.sqrt(max(result.kalman_final_slope_variance, 0.0))
        elif field_name == FEATURE_NAME_SHRUNK_GROWTH and result.kalman_final_slope_variance is not None and result.empirical_bayes_gamma is not None:
            # Shrunk estimate's uncertainty scales down with gamma toward
            # the (assumed tighter, pooled) prior -- an approximation
            # (the exact posterior variance of an Empirical-Bayes
            # composite is gamma * D_i, treating the prior as known
            # rather than itself estimated with error), documented as
            # such rather than presented as an exact posterior SE.
            uncertainty = math.sqrt(max(result.kalman_final_slope_variance, 0.0)) * math.sqrt(max(result.empirical_bayes_gamma, 0.0))

        FeatureObservation.write_latest(
            location_cell=cell, engine_number=ENGINE_NUMBER, feature_name=field_name,
            defaults=dict(
                value_type=value_type,
                value_numeric=Decimal(str(round(float(value), 6))),
                unit=unit,
                observed_at=started_at,
                source="business_intel.engines.economic_momentum",
                source_class="internal_derived",
                method=method,
                derivation_level=derivation_level,
                sample_size=len(result.years_observed) or None,
                uncertainty=Decimal(str(round(uncertainty, 6))) if uncertainty is not None else None,
            ),
        )
        features_written += 1

    # implied_local_growth_rate is a range, not a scalar -- written
    # separately as a JSON feature rather than forced into METRIC_FIELDS'
    # numeric shape, same "the range itself is the honest output" point
    # the module docstring makes about never collapsing it to one number.
    if result.implied_local_growth_rate.get("point_pct") is not None:
        FeatureObservation.write_latest(
            location_cell=cell, engine_number=ENGINE_NUMBER, feature_name="implied_local_growth_rate_pct",
            defaults=dict(
                value_type="json",
                value_json=result.implied_local_growth_rate,
                unit="pct_annual",
                observed_at=started_at,
                source="business_intel.engines.economic_momentum",
                source_class="internal_derived",
                method="henderson_storeygard_weil_2012_elasticity_ecological_inference",
                derivation_level="prediction",
                sample_size=len(result.years_observed) or None,
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

    return {
        "cell": cell,
        "score": result.economic_momentum_score,
        "result": result,
        "evidence": evidence,
        "report_fields": report_fields,
        "engine_run": run_row,
    }


def _default_years() -> List[int]:
    """DEFAULT_LOOKBACK_YEARS most recent USABLE VIIRS years, ending at
    (calendar_year - 1) -- EOG's Annual VNL composites are published with
    a real lag after each calendar year ends (the composite for year Y
    cannot exist before Y is over), so "this year" is never a valid fetch
    target; using (current_year - 1) as the latest candidate year is a
    deliberate, documented choice, not an off-by-one bug. Never returns a
    year before VIIRS_FIRST_USABLE_YEAR (2014), even if that means fewer
    than DEFAULT_LOOKBACK_YEARS years are requested for locations/systems
    queried very early in VIIRS's own usable history.
    """
    from django.utils import timezone as _timezone

    latest_candidate_year = _timezone.now().year - 1
    start_year = max(VIIRS_FIRST_USABLE_YEAR, latest_candidate_year - DEFAULT_LOOKBACK_YEARS + 1)
    return list(range(start_year, latest_candidate_year + 1))