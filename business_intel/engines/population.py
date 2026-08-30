"""
business_intel/engines/population.py

Engine 2 — Population & Demographic Engine
Per business_location_intelligence_engines_data_models_v2.md, Section 2.

Purpose
-------
Measure how many people live in, work in, move through, and can
realistically access a location, at high spatial resolution, per
Catchment.

Scope boundary
--------------
Depends only on business_intel.models and, for catchment geometry, on
business_intel.engines.spatial_context's public Catchment output. No
dependency on services.py, tasks.py, or google_client.py.

Administrative hierarchy (real, not simplified)
--------------------------------------------------
Kenya's actual census hierarchy, per KNBS's 2019 Kenya Population and
Housing Census (KPHC) Volume II ("Distribution of Population by
Administrative Units"), goes:

    Country -> County (47) -> Sub-County -> Division -> Location
             -> Sub-Location

KNBS publishes population, households, land area and density at every
one of those levels (KPHC Volume I: county/sub-county; Volume II: down
to sub-location). The devolved-government hierarchy in parallel use
(County -> Sub-County -> Ward, ~1,450 wards) is also modeled here,
since many other datasets (IEBC, county dashboards) report at ward
level instead of location/sub-location.

This module treats "which level has a published population figure for
this specific unit" as a per-unit fact, not a fixed assumption --
some sub-locations will have a direct census figure, others won't, and
the hierarchy-fallback logic below (`resolve_population_source_unit`)
always uses the finest available level rather than assuming sub-
location data is universally present.

Real methods used (researched and cited, not invented)
------------------------------------------------------
1. Tobler's Pycnophylactic Interpolation (Tobler, W.R. 1979. "Smooth
   Pycnophylactic Interpolation for Geographic Regions." Journal of
   the American Statistical Association 74(367), 519-530.)

   Redistributes a known admin-unit population total onto a fine
   regular grid via iterative Laplacian-smoothing + volume-preserving
   rescaling, producing a continuous density surface with no
   artificial jumps at admin boundaries -- while the sum over any
   admin unit's grid cells still exactly equals that unit's known
   census total. This is the real "dasymetric/spatial estimation"
   layer the spec's Section 2 calls for; PycnophylacticInterpolator
   below is a faithful implementation of Tobler's published algorithm
   (uniform-density initialization, relaxation-parameter smoothing,
   per-zone rescale correction, convergence on max pixel change),
   not an approximation of it.

2. Fay-Herriot small area estimation (Fay, R.E. and Herriot, R.A.
   1979. "Estimates of Income for Small Places: An Application of
   James-Stein Procedures to Census Data." Journal of the American
   Statistical Association 74(366), 269-277.)

       Level 1 (sampling model): y_i | theta_i ~ N(theta_i, D_i)
       Level 2 (linking model):  theta_i ~ N(x_i'beta, A)

   Combined:  y_i = x_i'beta + u_i + e_i,  u_i~N(0,A),  e_i~N(0,D_i)

   EBLUP: theta_hat_i = gamma_i*y_i + (1-gamma_i)*x_i'beta_hat,
          gamma_i = A / (A + D_i)   ("shrinkage factor")

   This is the actual model national statistics agencies use to
   combine a noisy direct estimate (e.g. a Pycnophylactic-interpolated
   figure for a small catchment, with real sampling/model
   uncertainty) with a regression-based synthetic estimate built from
   auxiliary covariates (building density, night-lights, distance to
   CBD, etc.), shrinking toward whichever is more reliable. Fit here
   via Fay & Herriot's own iterative method-of-moments procedure for
   the between-area variance A -- a closed, well-defined statistical
   procedure, not an approximation and not requiring MCMC.

3. Bayesian spatial hierarchical smoothing -- the BYM2 model (Riebler,
   A., Sorbye, S.H., Simpson, D., Rue, H. 2016. "An intuitive Bayesian
   spatial model for disease mapping that accounts for scaling."
   Statistical Methods in Medical Research 25(4), 1145-1165.) is the
   correctly-specified, citable model for the next tier of
   sophistication -- a convex combination of a structured (ICAR /
   spatially-correlated) component and an unstructured (iid) random
   effect, fit via MCMC/INLA. That is genuine model-fitting work
   (choosing a neighborhood graph, sampling a posterior, calibrating
   against held-out data) that belongs to you, not something this
   module fakes. `SpatialSmoothingProvider` below is the documented
   hook point where a real BYM2 fit plugs in; it is deliberately left
   unimplemented rather than faked, per our agreement on this point.

4. Inter-census linear interpolation and the Housing Unit Method
   (MEASURE Evaluation, "Population Analysis for Planners," Lesson 5:
   Estimating Population Size -- itself citing Shryock, H.S. and
   Siegel, J.S. 1973, "Population Estimates," The Methods and
   Materials of Demography, U.S. Bureau of the Census, 725-770; and
   Morrison, P.A. 1971, Demographic Information for Cities, RAND.)

   Inter-census linear interpolation (Eq. 5-2):
       P_estimate = P1 + (n/N) * (P2 - P1)
   The standard *arithmetic* alternative to the *geometric* (CAGR)
   growth model already used elsewhere in this engine -- offered
   alongside it, not instead of it, per the lesson's own guideline
   that estimates should be cross-checked against an independently-
   derived figure.

   Housing Unit Method (Eq. 5-4 through 5-10):
       Population = (Occupied Housing Units x Avg Household Size)
                    + Population in Group Housing
   The standard way planning offices estimate current-year population
   for a locale with limited direct demographic data between census
   rounds -- built from housing-unit counts, vacancy rates, and
   average household size. Implemented here as an independent
   cross-check against the Pycnophylactic/Fay-Herriot estimate, per
   the lesson's guideline #4 ("an estimate should always be checked by
   comparing it with another estimate...that employs an equally
   accurate or more accurate method") and guideline #6 ("the most
   accurate projection is usually based on a combination of methods
   ... take an average of the two").

Real-data acquisition
-----------------------
WorldPop's stats API and the OSM Overpass API were both tested
directly from this environment and are blocked (bot detection /
robots.txt respectively) -- confirmed by trying multiple request
formats, not assumed. KNBS publishes county/sub-county/ward figures as
downloadable XLSX/PDF, which is a manual/batch download, not a live
API. `SyntheticAdminHierarchyProvider` exists purely to prove the
calculation, interpolation, and estimation layers below are correct
against real Django DB writes; it is explicitly not real Kenyan data
and must never back a production request.
"""

from __future__ import annotations

import logging
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional, Sequence

import numpy as np
from django.db import transaction
from django.utils import timezone

from ..models import (
    BusinessLocationCell,
    BusinessQuery,
    Catchment,
    EngineRun,
    FeatureObservation,
)

logger = logging.getLogger("business_intel.engines.population")

ENGINE_NUMBER = 2

# Real Kenya administrative hierarchy, coarse -> fine, per KNBS 2019
# KPHC Volume II. Kept as an ordered list (not a fixed-depth enum) so
# providers can supply a partial hierarchy where that's all that's
# published for a given area.
ADMIN_LEVELS_CENSUS = ["county", "subcounty", "division", "location", "sublocation"]
# Parallel devolved-government hierarchy used by many non-KNBS datasets.
ADMIN_LEVELS_DEVOLVED = ["county", "subcounty", "ward"]


# ============================================================================
# Administrative hierarchy
# ============================================================================

@dataclass
class AdminUnit:
    """One node in the administrative hierarchy. `population` is None
    unless this specific unit has a *directly published* figure at
    this level for `year` -- hierarchy fallback (not silent
    substitution) is how a query for a unit without its own figure
    gets an answer; see resolve_population_source_unit().
    """
    level: str
    code: str
    name: str
    parent_code: Optional[str]
    year: int
    population: Optional[float] = None
    households: Optional[float] = None
    area_km2: Optional[float] = None
    polygon_geojson: Optional[dict] = None
    # Sex breakdown, per KNBS 2019 KPHC Volume II Table 2.2 (county) /
    # Table 2.3 (sub-county). intersex_population is only published at
    # county level -- KNBS's own footnote states intersex counts are "too
    # few to be distributed at sub national level" below that, so it's
    # None for sub-county/sub-location units, not silently zero.
    male_population: Optional[float] = None
    female_population: Optional[float] = None
    intersex_population: Optional[float] = None
    # Household split, per the same tables: "conventional" households vs
    # "group quarters" (barracks, hostels, institutions).
    households_conventional: Optional[float] = None
    households_group_quarters: Optional[float] = None
    # Age/sex structure, per KNBS 2019 KPHC Volume III (published at
    # county/sub-county level; None if not published for this unit).
    children_pct: Optional[float] = None      # age 0-14
    working_age_pct: Optional[float] = None   # age 15-64
    young_adult_pct: Optional[float] = None   # subset of working-age, e.g. 15-24
    elderly_pct: Optional[float] = None       # age 65+
    # Everything else KNBS published (Volume IV): disability, religion,
    # ethnicity/nationality, education level, water source, ICT/mobile
    # ownership, housing conditions, agriculture -- sourced from
    # business_intel.data.kenya_census_loader.CensusData when available.
    # Kept as one open dict rather than a dozen new dataclass fields
    # because coverage varies (some tables are county-only, some go to
    # sub-county) and the set of tables is expected to grow; each key's
    # own docstring-equivalent lives in kenya_census_loader.py /
    # 00_TABLE_CATALOGUE.csv, not duplicated here. None if the loader
    # isn't installed or this unit has no matching row -- an honest gap,
    # not a silent zero.
    extra_demographics: Optional[dict] = None

    @property
    def sex_ratio_males_per_100_females(self) -> Optional[float]:
        """Males per 100 females, the standard demographic sex-ratio
        convention -- None if either sex count isn't published for this
        unit (see intersex_population's note above for why that happens)."""
        if not self.male_population or not self.female_population:
            return None
        return round(self.male_population / self.female_population * 100.0, 2)


class AdministrativeHierarchyProvider(ABC):
    """Resolves a coordinate to its full admin path (county down to
    whatever the finest known level is) and answers hierarchy
    queries. Concrete production implementation would load KNBS
    county/sub-county/ward/location/sub-location boundary shapefiles
    (KNBS + IEBC publish these) plus the corresponding population
    tables, and do point-in-polygon resolution.
    """

    @abstractmethod
    def resolve(self, latitude: float, longitude: float) -> Sequence[AdminUnit]:
        """Ordered list of AdminUnit, coarsest first, for the
        administrative path containing (lat, lon). May be shorter than
        the full hierarchy if finer boundaries aren't loaded for this
        area."""

    @abstractmethod
    def children(self, level: str, code: str) -> Sequence[AdminUnit]:
        """Direct children of the given unit (e.g. all wards in a
        sub-county). Used to build a Pycnophylactic interpolation
        zone set."""

    def historical_population(self, level: str, code: str) -> Sequence[tuple]:
        """Returns [(year, population), ...] for this unit across every
        vintage the provider has (e.g. 2009 and 2019 KNBS census
        rounds). Default implementation returns just the unit's
        current (level, code) single vintage, if resolvable via
        children() from its own parent -- concrete providers backed by
        multiple real census rounds should override this to return the
        full history, which is what growth_rate_cagr /
        intercensal_linear_estimate need. Returning fewer than 2
        vintages simply means growth rates can't be computed for this
        unit, which compute_catchment_population handles as a
        (recorded, not silent) gap rather than an error.
        """
        return []


def resolve_population_source_unit(path: Sequence[AdminUnit]) -> Optional[AdminUnit]:
    """Hierarchical fallback: return the FINEST unit in `path` that has
    a directly published population figure. This is the mechanism that
    makes "high resolution where available, coarser where not" honest
    rather than pretending sub-location data always exists.
    """
    for unit in reversed(list(path)):
        if unit.population is not None:
            return unit
    return None


# ============================================================================
# 1. Tobler's Pycnophylactic Interpolation
#    Tobler (1979), JASA 74(367), 519-530.
# ============================================================================

@dataclass
class RasterGrid:
    """A regular lat/lon grid covering the bounding box of a set of
    admin-unit zones, with one population-density value and one zone
    membership label per cell."""
    min_lat: float
    min_lon: float
    cell_size_deg: float
    n_rows: int
    n_cols: int
    values: np.ndarray        # shape (n_rows, n_cols), population per cell
    zone_id: np.ndarray       # shape (n_rows, n_cols), dtype object, admin unit code or None

    def cell_center(self, row: int, col: int) -> tuple:
        lat = self.min_lat + (row + 0.5) * self.cell_size_deg
        lon = self.min_lon + (col + 0.5) * self.cell_size_deg
        return lat, lon


class PycnophylacticInterpolator:
    """Faithful implementation of Tobler's (1979) smooth pycnophylactic
    interpolation algorithm.

    Algorithm (as published, and as implemented in the reference R
    `pycno` package -- matched here for correctness):

      1. Initialize each grid cell within zone z to
         population_z / n_cells_in_zone (uniform-density start;
         already volume-preserving by construction).
      2. Iterate:
         a. Smoothing step: replace each cell's value with a
            relaxation-weighted blend of itself and the mean of its
            4-connected neighbors:
                new = old + r * (mean(neighbors) - old)
            where r (default 0.2) is the relaxation parameter --
            small values prevent the smoothing step from
            overshooting and oscillating.
         b. Non-negativity: clip any cell below 0 to 0 (Tobler's
            method guarantees non-negative output; the clip handles
            floating-point edge cases at zone boundaries).
         c. Volume-preserving correction: for every zone, rescale all
            of its cells by (zone_total / current_zone_sum) so the
            zone's total is exactly preserved after the smoothing
            step perturbed it.
      3. Stop when the largest single-cell change in an iteration
         falls below 10^-`convergence_exponent` times the largest
         initial cell value (Tobler's stated convergence criterion),
         or `max_iterations` is reached.
    """

    def __init__(self, relaxation: float = 0.2, convergence_exponent: int = 3, max_iterations: int = 500):
        if not (0 < relaxation <= 1):
            raise ValueError("relaxation must be in (0, 1]")
        self.relaxation = relaxation
        self.convergence_exponent = convergence_exponent
        self.max_iterations = max_iterations

    def build_grid(self, zones: Sequence[AdminUnit], cell_size_deg: float) -> RasterGrid:
        """Rasterizes the bounding box of `zones`' polygons at
        cell_size_deg resolution, assigning each cell to a zone via
        point-in-polygon test on the cell center."""
        import shapely.geometry as geom

        polygons = {}
        for z in zones:
            if z.polygon_geojson is None:
                continue
            polygons[z.code] = geom.shape(z.polygon_geojson)

        if not polygons:
            raise ValueError("At least one zone must have polygon_geojson to build a grid.")

        min_lon = min(p.bounds[0] for p in polygons.values())
        min_lat = min(p.bounds[1] for p in polygons.values())
        max_lon = max(p.bounds[2] for p in polygons.values())
        max_lat = max(p.bounds[3] for p in polygons.values())

        n_cols = max(1, int(math.ceil((max_lon - min_lon) / cell_size_deg)))
        n_rows = max(1, int(math.ceil((max_lat - min_lat) / cell_size_deg)))

        zone_id = np.empty((n_rows, n_cols), dtype=object)
        for row in range(n_rows):
            for col in range(n_cols):
                lat = min_lat + (row + 0.5) * cell_size_deg
                lon = min_lon + (col + 0.5) * cell_size_deg
                pt = geom.Point(lon, lat)
                for code, poly in polygons.items():
                    if poly.contains(pt):
                        zone_id[row, col] = code
                        break

        return RasterGrid(
            min_lat=min_lat, min_lon=min_lon, cell_size_deg=cell_size_deg,
            n_rows=n_rows, n_cols=n_cols,
            values=np.zeros((n_rows, n_cols), dtype=float),
            zone_id=zone_id,
        )

    def interpolate(self, grid: RasterGrid, zone_totals: dict) -> RasterGrid:
        """Runs Tobler's algorithm on `grid`, given each zone's known
        total (`zone_totals`: {zone_code: population}). Returns a new
        RasterGrid with the smoothed, volume-preserving surface.
        """
        n_rows, n_cols = grid.n_rows, grid.n_cols
        zone_id = grid.zone_id
        values = np.zeros((n_rows, n_cols), dtype=float)

        # Step 1: uniform-density initialization per zone.
        zone_cell_counts = {}
        for row in range(n_rows):
            for col in range(n_cols):
                z = zone_id[row, col]
                if z is not None:
                    zone_cell_counts[z] = zone_cell_counts.get(z, 0) + 1

        for row in range(n_rows):
            for col in range(n_cols):
                z = zone_id[row, col]
                if z is not None and z in zone_totals and zone_cell_counts.get(z):
                    values[row, col] = zone_totals[z] / zone_cell_counts[z]

        if values.size == 0 or not np.any(values):
            return RasterGrid(grid.min_lat, grid.min_lon, grid.cell_size_deg, n_rows, n_cols, values, zone_id)

        max_initial_value = float(np.max(values))
        threshold = (10.0 ** -self.convergence_exponent) * max_initial_value
        max_change = 0.0

        for iteration in range(self.max_iterations):
            # Step 2a: relaxation-weighted 4-neighbor Laplacian smoothing.
            padded = np.pad(values, 1, mode="edge")
            neighbor_mean = (
                padded[0:-2, 1:-1] + padded[2:, 1:-1] + padded[1:-1, 0:-2] + padded[1:-1, 2:]
            ) / 4.0
            smoothed = values + self.relaxation * (neighbor_mean - values)

            # Step 2b: non-negativity.
            smoothed = np.clip(smoothed, 0.0, None)

            # Step 2c: per-zone volume-preserving rescale.
            corrected = smoothed.copy()
            for z, total in zone_totals.items():
                mask = zone_id == z
                current_sum = smoothed[mask].sum()
                if current_sum > 0:
                    corrected[mask] = smoothed[mask] * (total / current_sum)

            max_change = float(np.max(np.abs(corrected - values))) if corrected.size else 0.0
            values = corrected

            if max_change < threshold:
                logger.debug("Pycnophylactic interpolation converged after %d iterations.", iteration + 1)
                break
        else:
            logger.warning(
                "Pycnophylactic interpolation did not converge within %d iterations "
                "(last max_change=%s, threshold=%s).", self.max_iterations, max_change, threshold,
            )

        return RasterGrid(grid.min_lat, grid.min_lon, grid.cell_size_deg, n_rows, n_cols, values, zone_id)

    def population_in_polygon(self, grid: RasterGrid, polygon_geojson: dict) -> float:
        """Sums grid cell values whose center falls inside the query
        polygon. This is a center-in-polygon approximation (a cell
        straddling the boundary counts fully or not at all, rather
        than being area-weighted); documented as an approximation
        rather than silently treated as exact -- refine to
        area-weighted overlap (e.g. via shapely intersection) if a
        catchment is small relative to the grid's cell_size_deg.
        """
        import shapely.geometry as geom

        poly = geom.shape(polygon_geojson)
        total = 0.0
        for row in range(grid.n_rows):
            for col in range(grid.n_cols):
                lat, lon = grid.cell_center(row, col)
                if poly.contains(geom.Point(lon, lat)):
                    total += grid.values[row, col]
        return total

    def verify_volume_preservation(self, grid: RasterGrid, zone_totals: dict, tolerance: float = 1e-6) -> dict:
        """Returns {zone_code: abs relative error} for every zone --
        should be ~0 for all zones if the algorithm is implemented
        correctly. Exposed so this property (the defining feature of
        'pycnophylactic') can be checked, not just assumed.
        """
        errors = {}
        for z, total in zone_totals.items():
            mask = grid.zone_id == z
            actual = float(grid.values[mask].sum())
            errors[z] = abs(actual - total) / total if total else abs(actual)
        return errors


# ============================================================================
# 2. Fay-Herriot Small Area Estimation
#    Fay & Herriot (1979), JASA 74(366), 269-277.
# ============================================================================

@dataclass
class AreaLevelObservation:
    """One small area's data for Fay-Herriot fitting.

    direct_estimate (y_i): a noisy estimate for this area -- e.g. a
        Pycnophylactic-interpolated population figure for a catchment,
        with real uncertainty.
    sampling_variance (D_i): the KNOWN (or externally estimated)
        variance of that direct estimate. For a Pycnophylactic
        estimate, a defensible D_i is the variance across nearby grid
        cells' contribution (finer grid -> lower D_i); for a
        building-count-weighted estimate, bootstrap resampling of the
        building sample is a standard way to get this.
    auxiliary (x_i): covariates believed to predict the true value --
        e.g. [1.0, building_footprint_density, nightlight_radiance,
        distance_to_cbd_km]. First element is the intercept term.
    """
    area_id: str
    direct_estimate: float
    sampling_variance: float
    auxiliary: Sequence[float]


@dataclass
class FayHerriotResult:
    area_id: str
    eblup: float
    shrinkage_gamma: float
    direct_estimate: float
    synthetic_estimate: float


class FittedFayHerriotModel:
    """A fitted Fay-Herriot model: beta (regression coefficients on the
    auxiliary covariates) and A_hat (estimated between-area variance).
    """

    def __init__(self, beta: np.ndarray, between_area_variance: float):
        self.beta = beta
        self.between_area_variance = between_area_variance

    def predict(self, obs: AreaLevelObservation) -> FayHerriotResult:
        x = np.asarray(obs.auxiliary, dtype=float)
        synthetic = float(x @ self.beta)
        A = self.between_area_variance
        gamma = A / (A + obs.sampling_variance) if (A + obs.sampling_variance) > 0 else 0.0
        eblup = gamma * obs.direct_estimate + (1 - gamma) * synthetic
        return FayHerriotResult(
            area_id=obs.area_id, eblup=eblup, shrinkage_gamma=gamma,
            direct_estimate=obs.direct_estimate, synthetic_estimate=synthetic,
        )

    def predict_out_of_sample(self, auxiliary: Sequence[float]) -> float:
        """Synthetic-only estimate (gamma=0) for an area with no direct
        estimate at all -- per Fay-Herriot theory, this is the correct
        fallback for out-of-sample areas."""
        return float(np.asarray(auxiliary, dtype=float) @ self.beta)


class FayHerriotSmallAreaEstimator:
    """Fits a Fay-Herriot model via the original iterative
    method-of-moments procedure (Fay & Herriot 1979, Section 3):
    solve for the between-area variance A such that the weighted
    residual sum of squares equals its degrees of freedom, iterating
    because beta_hat(A) itself depends on A.

        beta_hat(A) = (X'D^-1 X)^-1 X'D^-1 y,   D = diag(D_i + A)
        S(A) = sum_i (y_i - x_i'beta_hat(A))^2 / (D_i + A)

    A_hat solves S(A) = m - p (m areas, p covariates including the
    intercept). S(A) is monotonically non-increasing in A, so this
    has a unique non-negative solution found by bisection; if
    S(0) <= m-p already (the sampling variances alone explain all the
    observed variability), A_hat = 0 and the EBLUP correctly reduces
    to the pure synthetic-regression estimate for every area -- this
    boundary case is a known, expected property of the model, not a
    bug.
    """

    def __init__(self, max_variance_multiplier: float = 50.0, bisection_tolerance: float = 1e-8, max_bisection_iterations: int = 200):
        self.max_variance_multiplier = max_variance_multiplier
        self.bisection_tolerance = bisection_tolerance
        self.max_bisection_iterations = max_bisection_iterations

    def _gls_beta(self, X: np.ndarray, y: np.ndarray, D: np.ndarray, A: float) -> np.ndarray:
        weights = 1.0 / (D + A)
        W = np.diag(weights)
        XtW = X.T @ W
        return np.linalg.solve(XtW @ X, XtW @ y)

    def _moment_residual(self, X, y, D, A, m, p) -> float:
        beta = self._gls_beta(X, y, D, A)
        resid = y - X @ beta
        S = float(np.sum((resid ** 2) / (D + A)))
        return S - (m - p)

    def fit(self, observations: Sequence[AreaLevelObservation]) -> FittedFayHerriotModel:
        m = len(observations)
        if m < 2:
            raise ValueError("Fay-Herriot fitting requires at least 2 areas.")

        X = np.array([list(o.auxiliary) for o in observations], dtype=float)
        y = np.array([o.direct_estimate for o in observations], dtype=float)
        D = np.array([o.sampling_variance for o in observations], dtype=float)
        p = X.shape[1]

        if m <= p:
            raise ValueError(
                f"Fay-Herriot needs more areas ({m}) than auxiliary covariates ({p}) "
                "to be identified; add areas or drop covariates."
            )

        # A=0 boundary check first, per the model's known degenerate case.
        residual_at_zero = self._moment_residual(X, y, D, 0.0, m, p)
        if residual_at_zero <= 0:
            beta = self._gls_beta(X, y, D, 0.0)
            return FittedFayHerriotModel(beta=beta, between_area_variance=0.0)

        lo, hi = 0.0, float(np.var(y) * self.max_variance_multiplier + 1e-9)
        f_hi = self._moment_residual(X, y, D, hi, m, p)
        expansions = 0
        while f_hi > 0 and expansions < 10:
            hi *= 2
            f_hi = self._moment_residual(X, y, D, hi, m, p)
            expansions += 1

        for _ in range(self.max_bisection_iterations):
            mid = (lo + hi) / 2.0
            f_mid = self._moment_residual(X, y, D, mid, m, p)
            if abs(f_mid) < self.bisection_tolerance or (hi - lo) < self.bisection_tolerance:
                lo = hi = mid
                break
            if f_mid > 0:
                lo = mid
            else:
                hi = mid

        A_hat = max(0.0, (lo + hi) / 2.0)
        beta = self._gls_beta(X, y, D, A_hat)
        return FittedFayHerriotModel(beta=beta, between_area_variance=A_hat)


# ============================================================================
# 3. Inter-census linear interpolation & Housing Unit Method
#    MEASURE Evaluation, "Population Analysis for Planners," Lesson 5.
# ============================================================================

def intercensal_linear_estimate(
    population_t1: float, months_since_t1: float,
    population_t2: float, months_between_censuses: float,
) -> Optional[float]:
    """Equation 5-2 (MEASURE Evaluation, Lesson 5): linear interpolation
    of population between two census dates.

        P_estimate = P1 + (n/N) * (P2 - P1)

    P1, P2 are the two census populations; N is the number of months
    (or years -- units must match `months_since_t1`) between the two
    census dates; n is the elapsed time from the first census date to
    the target estimation date. Assumes population change is linear
    between censuses -- the lesson explicitly flags this as a
    simplification, offered here as the standard *arithmetic*
    counterpart to growth_rate_cagr's *geometric* model, so the two
    can be compared per the lesson's own cross-checking guidance.
    """
    if months_between_censuses <= 0:
        return None
    fraction = months_since_t1 / months_between_censuses
    return population_t1 + fraction * (population_t2 - population_t1)


@dataclass
class HousingUnitMethodInputs:
    """Inputs for the Housing Unit Method (Eq. 5-4 through 5-10).
    Typically drawn from a census baseline plus subsequent
    building-permit/demolition records; in this engine's context, a
    BuildingFootprintProvider snapshot at two dates is a usable proxy
    for permits/demolitions when official permit data isn't available.
    """
    census_occupied_units: float
    census_vacant_units: float
    census_avg_household_size: float
    census_group_housing_population: float
    new_units_since_census: float = 0.0
    demolished_units_since_census: float = 0.0
    net_conversions_since_census: float = 0.0  # + = converted TO housing, - = FROM housing
    updated_vacancy_rate: Optional[float] = None  # None -> reuse the census vacancy rate


def estimate_population_housing_unit_method(inputs: HousingUnitMethodInputs) -> float:
    """Equation 5-4 (Housing Unit Method Summary Equation):

        Population = (Occupied Housing Units x Avg Household Size)
                     + Population in Group Housing

    Occupied Housing Units (Eq. 5-7, 5-8, 5-10):
        Total Units_now = Census Occupied + Census Vacant + New
                           - Demolished + Net Conversions
        Vacancy Rate = Census Vacant / Census Total   (Eq. 5-8, unless
                       `updated_vacancy_rate` overrides it with a more
                       current figure -- e.g. from utility-connection
                       data, per Eq. 5-9)
        Occupied Units_now = Total Units_now * (1 - Vacancy Rate)

    Per the lesson: "the housing unit method assumes that a change in
    the number of occupied housing units reflects a change in the
    population size" -- the standard tool for a locale with limited
    direct demographic data between census rounds, which is exactly
    this engine's typical case.
    """
    census_total_units = inputs.census_occupied_units + inputs.census_vacant_units
    census_vacancy_rate = (
        inputs.census_vacant_units / census_total_units if census_total_units else 0.0
    )
    vacancy_rate = (
        inputs.updated_vacancy_rate if inputs.updated_vacancy_rate is not None else census_vacancy_rate
    )
    occupancy_rate = 1.0 - vacancy_rate

    total_units_now = (
        census_total_units + inputs.new_units_since_census
        - inputs.demolished_units_since_census + inputs.net_conversions_since_census
    )
    occupied_units_now = total_units_now * occupancy_rate

    return occupied_units_now * inputs.census_avg_household_size + inputs.census_group_housing_population


# ============================================================================
# 4. Spatial smoothing hook (BYM2) -- documented interface, not implemented
#    Riebler et al. (2016), Stat Methods Med Res 25(4), 1145-1165.
# ============================================================================

class SpatialSmoothingProvider(ABC):
    """Hook point for a real Bayesian spatial hierarchical model (BYM2:
    a convex combination of a structured ICAR spatial component and an
    unstructured iid component, per Riebler et al. 2016) fit over the
    admin-unit adjacency graph. This is genuine model-fitting work --
    building the neighborhood graph, choosing priors for the mixing
    parameter phi and marginal variance sigma^2, sampling the
    posterior (Stan/PyMC/R-INLA) -- and is deliberately left as an
    interface, not implemented here, per our agreement that this
    module builds the calculation/interpolation/SAE layer and you
    build the fitted spatial model.
    """

    @abstractmethod
    def smoothed_estimate(self, area_id: str, raw_estimate: float, raw_variance: float) -> float:
        """Returns a spatially-smoothed estimate for one area, given
        its raw (e.g. Fay-Herriot EBLUP) estimate and variance, using a
        fitted BYM2 posterior over the admin-unit adjacency graph."""


# ============================================================================
# Synthetic providers -- local testing only, NOT for production
# ============================================================================

class SyntheticAdminHierarchyProvider(AdministrativeHierarchyProvider):
    """Deterministic small synthetic admin hierarchy (one county, a
    handful of wards, each with a rectangular polygon and a plausible
    population figure) -- NOT real KNBS data. Exists to exercise
    resolve(), children(), Pycnophylactic interpolation, and
    Fay-Herriot fitting against real Django writes without live
    KNBS/OSM access.
    """

    def __init__(self, center_lat: float, center_lon: float):
        self.center_lat = center_lat
        self.center_lon = center_lon
        self._units: dict = {}
        self._historical: dict = {}
        self._build_synthetic_hierarchy()

    def _rect(self, lat0, lon0, lat1, lon1) -> dict:
        return {
            "type": "Polygon",
            "coordinates": [[[lon0, lat0], [lon1, lat0], [lon1, lat1], [lon0, lat1], [lon0, lat0]]],
        }

    def _build_synthetic_hierarchy(self):
        c_lat, c_lon = self.center_lat, self.center_lon
        county = AdminUnit(
            level="county", code="C01", name="Synthetic County", parent_code=None, year=2019,
            population=1_200_000, households=310_000, area_km2=180.0,
            polygon_geojson=self._rect(c_lat - 0.05, c_lon - 0.05, c_lat + 0.05, c_lon + 0.05),
        )
        self._units[("county", "C01")] = county

        ward_specs = [
            ("W01", c_lat - 0.05, c_lon - 0.05, c_lat, c_lon, 420_000),
            ("W02", c_lat - 0.05, c_lon, c_lat, c_lon + 0.05, 260_000),
            ("W03", c_lat, c_lon - 0.05, c_lat + 0.05, c_lon, 310_000),
            ("W04", c_lat, c_lon, c_lat + 0.05, c_lon + 0.05, 210_000),
        ]
        for code, lat0, lon0, lat1, lon1, pop in ward_specs:
            unit = AdminUnit(
                level="ward", code=code, name=f"Synthetic Ward {code}", parent_code="C01", year=2019,
                population=pop, households=pop * 0.24, area_km2=45.0,
                polygon_geojson=self._rect(lat0, lon0, lat1, lon1),
                children_pct=38.5, working_age_pct=57.0, young_adult_pct=19.5, elderly_pct=4.5,
            )
            self._units[("ward", code)] = unit
            # Synthetic historical vintages of the same ward at a
            # constant ~2.8%/yr growth rate, covering the 1/3/5-year
            # windows back from 2019 (plus 2009 for a longer baseline)
            # -- NOT real KNBS data, exists purely so growth_rate_cagr
            # has vintages to compute against in tests.
            self._historical[("ward", code)] = [
                (2009, pop / (1.028 ** 10)),
                (2014, pop / (1.028 ** 5)),
                (2016, pop / (1.028 ** 3)),
                (2018, pop / (1.028 ** 1)),
            ]

    def resolve(self, latitude: float, longitude: float) -> Sequence[AdminUnit]:
        import shapely.geometry as geom
        pt = geom.Point(longitude, latitude)
        path = []
        county = self._units[("county", "C01")]
        if geom.shape(county.polygon_geojson).contains(pt):
            path.append(county)
        for (level, code), unit in self._units.items():
            if level == "ward" and geom.shape(unit.polygon_geojson).contains(pt):
                path.append(unit)
                break
        return path

    def children(self, level: str, code: str) -> Sequence[AdminUnit]:
        if level == "county" and code == "C01":
            return [u for (lvl, _), u in self._units.items() if lvl == "ward"]
        return []

    def historical_population(self, level: str, code: str) -> Sequence[tuple]:
        return self._historical.get((level, code), [])


# ============================================================================
# Real administrative hierarchy provider -- backed by actual KNBS 2019
# KPHC data, not synthetic test fixtures.
# ============================================================================

class RealAdminHierarchyProvider(AdministrativeHierarchyProvider):
    """Production admin-hierarchy provider, backed by real KNBS 2019 KPHC
    data (business_intel.data.kenya_counties_2019_census,
    .kenya_subcounties_2019_census, .kenya_age_structure_2019_census).

    Resolution depth is honest about what real geometry actually exists:

    - County: resolved by real point-in-polygon against the county
      boundary GeoJSON files in business_intel/data/county_boundaries/
      (cloned from github.com/Mondieki/kenya-counties-subcounties).
      This works from coordinates alone.
    - Sub-county: KNBS publishes real population figures for all 333
      sub-counties (business_intel.data.kenya_subcounties_2019_census),
      but this codebase does not yet have sub-county boundary polygons,
      so sub-county CANNOT be resolved from coordinates alone. If the
      caller already knows the sub-county name -- e.g. from
      BusinessLocationCell.subcounty, itself typically populated via
      Google Geocoding's admin_area_level_2 address component in
      property_intel/google_client.py's pattern -- pass it via
      `subcounty_hint` and this provider will attach the matching real
      AdminUnit. Without a hint, resolve() stops at county, and
      resolve_population_source_unit() (in this module) correctly falls
      back to the county's own real population figure -- an honest
      coarser answer, not a fabricated finer one.
    - Sub-location ("village"): KNBS publishes real sub-location figures
      too (KPHC Volume II Table 2.4), but transcribing all ~1,450 of
      them was deliberately not done in one pass -- see
      business_intel.data.kenya_sublocations_2019_census's docstring (or,
      if that module doesn't exist yet in your checkout, the PR that
      added county+sub-county real data) for why: a first attempt at
      hand-transcribing one county's sub-locations produced numbers that
      did NOT reconcile against that county's own sub-county total (the
      source PDF collapses a location with exactly one child
      sub-location into two identical-looking rows, and a naive
      transcription double-counts it) -- caught by exactly the kind of
      cross-validation this module's docstrings insist on, and dropped
      rather than shipped. Wire in a validated
      kenya_sublocations_2019_census module the same way once one
      exists; nothing else here needs to change.

    County boundary loading is lazy and cached per-instance so repeated
    resolve() calls (e.g. across many BusinessQuery rows in one process)
    don't re-parse 47 GeoJSON files every time.
    """

    def __init__(self, county_boundaries_dir: Optional[str] = None):
        import os
        self.county_boundaries_dir = county_boundaries_dir or os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "data", "county_boundaries"
        )
        self._county_polygons_cache: Optional[dict] = None
        self._county_by_name = self._load_county_table()
        self._subcounty_by_key = self._load_subcounty_table()
        # self._census must be assigned BEFORE _load_age_structure() /
        # _load_subcounty_age_structure() -- both read self._census to
        # decide whether they can compute live age data from V3_T2.3.
        self._census = self._load_census_data()
        self._age_by_county = self._load_age_structure()
        self._age_by_subcounty = self._load_subcounty_age_structure()

    def _load_census_data(self):
        """Lazily loads the full KNBS Volume I-IV dataset (business_intel.
        data.kenya_census_loader.CensusData) if it's installed. Returns
        None (not raise) if the census_2019/ CSVs or pandas aren't
        present -- extra_demographics simply stays unset for every unit
        in that case, same honest-gap pattern as everything else here.
        """
        try:
            from ..data.kenya_census_loader import CensusData
            return CensusData()
        except Exception:  # noqa: BLE001 - missing pandas, missing CSVs, etc. are all "not available"
            logger.info(
                "kenya_census_loader.CensusData not available; extra_demographics "
                "(education/disability/religion/water/ICT/etc.) will be unset. "
                "Run install_census_dataset.sh and `pip install pandas` to enable it."
            )
            return None

    def _extra_demographics(self, county: str, subcounty: Optional[str] = None) -> Optional[dict]:
        """Curated headline figures from the Volume IV+ tables, keyed by a
        short label rather than the raw KNBS table code. Each lookup is
        independently try/excepted -- a malformed or missing single table
        (e.g. a county name that doesn't match KNBS's own spelling in
        one specific table) degrades that one key to absent rather than
        failing the whole unit. Use `self._census.table(<code>)` directly
        (see 00_TABLE_CATALOGUE.csv) for anything not surfaced here.
        """
        if self._census is None:
            return None

        out: dict = {}

        def _try(key, fn):
            try:
                value = fn()
                if value is not None:
                    out[key] = value
            except Exception:  # noqa: BLE001 - one bad table shouldn't sink the rest
                logger.debug("extra_demographics: could not compute %r for %s/%s", key, county, subcounty)

        def _row(df, name_col, name_value):
            def _normalize(s):
                return str(s).lower().replace("/", " ").replace("-", " ").split()
            target = _normalize(name_value)
            match = df[df[name_col].astype(str).apply(lambda s: _normalize(s) == target)]
            if "AdminArea" in df.columns:
                # Several V4 tables repeat the same County value on every
                # one of that county's sub-county rows too (AdminArea
                # distinguishes 'County' vs 'SubCounty' vs the national
                # 'xxx' rows) -- filter explicitly rather than relying on
                # row order, which isn't guaranteed by pandas.
                county_rows = match[match["AdminArea"] == "County"]
                if len(county_rows):
                    match = county_rows
            return match.iloc[0] if len(match) else None

        def _disability():
            df = self._census.disability()
            row = _row(df, "County", county)
            return None if row is None else float(row["PercentageWithDisability"])

        def _religion():
            df = self._census.religion()
            row = _row(df, "County", county)
            if row is None:
                return None
            cols = ["Catholic", "Protestant", "Evangelical", "Islam"]
            return {c: float(row[c]) for c in cols if c in row and row[c] == row[c]}  # NaN-safe

        def _water_source():
            df = self._census.water_source()
            row = _row(df, "County", county)
            if row is None:
                return None
            return row.drop(labels=["County", "SubCounty", "AdminArea"], errors="ignore").dropna().to_dict()

        def _ict_ownership():
            df = self._census.ict_ownership()
            row = _row(df, "County", county)
            if row is None:
                return None
            return row.drop(labels=["County", "SubCounty", "AdminArea"], errors="ignore").dropna().to_dict()

        _try("disability_pct", _disability)
        _try("religion", _religion)
        _try("water_source", _water_source)
        _try("ict_ownership", _ict_ownership)

        return out or None

    # -- data loading -----------------------------------------------------

    def _load_county_table(self) -> dict:
        from ..data.kenya_counties_2019_census import KENYA_COUNTIES_2019
        by_name = {}
        for row in KENYA_COUNTIES_2019:
            (name, slug, pop19, pop09, hh, area, male, female, intersex,
             hh_conv, hh_group) = row
            by_name[name] = {
                "slug": slug, "population_2019": pop19, "population_2009": pop09,
                "households_2019": hh, "area_km2": area, "male": male,
                "female": female, "intersex": intersex,
                "households_conventional": hh_conv, "households_group_quarters": hh_group,
            }
        return by_name

    def _load_subcounty_table(self) -> dict:
        try:
            from ..data.kenya_subcounties_2019_census import KENYA_SUBCOUNTIES_2019
        except ImportError:
            return {}
        by_key = {}
        for row in KENYA_SUBCOUNTIES_2019:
            (county, subcounty, slug, pop19, male, female, hh, hh_conv, hh_group, area) = row
            by_key[(county.lower(), subcounty.lower())] = {
                "county": county, "name": subcounty, "slug": slug,
                "population_2019": pop19, "male": male, "female": female,
                "households_2019": hh, "households_conventional": hh_conv,
                "households_group_quarters": hh_group, "area_km2": area,
            }
        return by_key

    # Single-year age band groupings, matching the formulas already used
    # throughout this module (children=0-14, working=15-64, young_adult
    # is the 15-24 subset, elderly=65+). V3_T2.3's "Age" column is either
    # a literal integer age (as a string, e.g. "23"), "100+" for the top
    # band, or "Total" (a summary row we treat separately).
    _CHILDREN_MAX_AGE = 14
    _WORKING_AGE_MIN, _WORKING_AGE_MAX = 15, 64
    _YOUNG_ADULT_MIN, _YOUNG_ADULT_MAX = 15, 24
    _ELDERLY_MIN_AGE = 65

    def _age_structure_from_v3_table(self, df) -> Optional[dict]:
        """Computes children_pct/working_age_pct/young_adult_pct/elderly_pct/
        dependency_ratio from a slice of V3_T2.3 (real KNBS Volume III,
        single-year age bands by sex) already filtered to one county or
        one county+sub-county. Returns None if the slice is empty or the
        age rows don't reconcile against the table's own reported total --
        an honest gap rather than a wrong number, same standard as the
        rest of this provider.
        """
        if df is None or len(df) == 0:
            return None

        total_row = df[df["Age"].astype(str).str.lower() == "total"]
        if len(total_row) == 0:
            return None
        reported_total = float(total_row.iloc[0]["Total"])
        if not reported_total:
            return None

        # V3_T2.3 mixes THREE row kinds under one "Age" column: single-year
        # rows ("0", "1", ... "100+"), 5-year band SUBTOTAL rows ("0 - 4",
        # "5-9", ...), and a "Not Stated" residual row. Summing all of them
        # double-counts every single-year value against its own band
        # subtotal -- caught by the reconciliation check below during
        # development (every county came back at ~2x reported_total before
        # this filter was added). Only single-year rows go into the
        # bucketed totals.
        import re as _re

        def _is_single_year(label: str) -> bool:
            label = label.strip()
            if label == "100+":
                return True
            return bool(_re.fullmatch(r"\d+", label))

        single_year_rows = df[df["Age"].astype(str).apply(_is_single_year)]

        children = working = young_adult = elderly = 0.0
        summed = 0.0
        for _, row in single_year_rows.iterrows():
            age_label = str(row["Age"]).strip()
            value = float(row["Total"]) if row["Total"] == row["Total"] else 0.0  # NaN-safe
            summed += value
            age_num = 100 if age_label == "100+" else int(age_label)
            if age_num <= self._CHILDREN_MAX_AGE:
                children += value
            elif self._WORKING_AGE_MIN <= age_num <= self._WORKING_AGE_MAX:
                working += value
                if self._YOUNG_ADULT_MIN <= age_num <= self._YOUNG_ADULT_MAX:
                    young_adult += value
            elif age_num >= self._ELDERLY_MIN_AGE:
                elderly += value

        # Cross-check: single-year rows should sum back to the table's own
        # "Total" row. A small gap is expected (KNBS's own "age not
        # stated" residual isn't broken out by single year in this table);
        # a large gap means something's misparsed -- bail rather than
        # silently publish a skewed percentage.
        if summed <= 0 or abs(summed - reported_total) / reported_total > 0.02:
            logger.warning(
                "V3_T2.3 age reconciliation off by >2%% (summed=%s, reported=%s); "
                "not computing age structure for this slice.", summed, reported_total,
            )
            return None

        if working <= 0:
            return None

        return {
            "total_population": reported_total,
            "children_pct": round(children / reported_total * 100, 4),
            "working_age_pct": round(working / reported_total * 100, 4),
            "young_adult_pct": round(young_adult / reported_total * 100, 4),
            "elderly_pct": round(elderly / reported_total * 100, 4),
            "dependency_ratio": round((children + elderly) / working * 100, 4),
        }

    def _load_age_structure(self) -> dict:
        """County-level age structure, computed live from the real KNBS
        Volume III table (V3_T2.3, 'Distribution of Population by Age,
        Sex, County and Sub-County') via self._census, for all 47
        counties -- not a hardcoded per-county dict. Falls back to the
        static kenya_age_structure_2019_census module (national profile
        only) if the full census_2019/ dataset isn't installed, same
        honest-degradation pattern as everything else here.
        """
        try:
            from ..data.kenya_age_structure_2019_census import NATIONAL_AGE_STRUCTURE_2019
        except ImportError:
            NATIONAL_AGE_STRUCTURE_2019 = {}

        result = {"_national": NATIONAL_AGE_STRUCTURE_2019}
        if self._census is None:
            return result

        try:
            df = self._census.table("V3_T2.3")
        except Exception:  # noqa: BLE001 - missing table shouldn't crash the provider
            logger.info("V3_T2.3 not available; county age structure falls back to national.")
            return result

        for county_name in self._county_by_name:
            def _normalize(s):
                return str(s).lower().replace("/", " ").replace("-", " ").split()
            target = _normalize(county_name)
            county_slice = df[
                df["County"].astype(str).apply(lambda s: _normalize(s) == target)
                & (df["SubCounty"].astype(str).str.upper() == "ALL")
            ]
            structure = self._age_structure_from_v3_table(county_slice)
            if structure is not None:
                result[county_name] = structure
        return result

    def _load_subcounty_age_structure(self) -> dict:
        """Sub-county-level age structure, computed live from V3_T2.3 for
        all 333 sub-counties -- not a hardcoded per-subcounty dict.
        Keyed the same way as self._subcounty_by_key,
        (county.lower(), subcounty.lower()). Falls back to {} (meaning
        callers fall through to county-level, then national) if the full
        census_2019/ dataset isn't installed.
        """
        if self._census is None:
            return {}
        try:
            df = self._census.table("V3_T2.3")
        except Exception:  # noqa: BLE001 - missing table shouldn't crash the provider
            return {}

        def _normalize(s):
            return str(s).lower().replace("/", " ").replace("-", " ").split()

        result = {}
        for (county_lower, subcounty_lower), info in self._subcounty_by_key.items():
            county_target = _normalize(info["county"])
            subcounty_target = _normalize(info["name"])
            sc_slice = df[
                df["County"].astype(str).apply(lambda s: _normalize(s) == county_target)
                & df["SubCounty"].astype(str).apply(lambda s: _normalize(s) == subcounty_target)
            ]
            structure = self._age_structure_from_v3_table(sc_slice)
            if structure is not None:
                result[(county_lower, subcounty_lower)] = structure
        return result

    def _county_polygons(self) -> dict:
        """Lazily loads every business_intel/data/county_boundaries/*.json
        file, keyed by the county's real display name (matched via each
        county's geojson_slug in kenya_counties_2019_census.py, not by
        filename guessing). Handles both bare GeoJSON Feature/Polygon
        files and FeatureCollection-wrapped ones, since boundary-file
        repos aren't consistent about which they export."""
        if self._county_polygons_cache is not None:
            return self._county_polygons_cache

        import json
        import os

        polygons = {}
        for name, info in self._county_by_name.items():
            path = os.path.join(self.county_boundaries_dir, f"{info['slug']}.json")
            if not os.path.exists(path):
                continue
            try:
                with open(path) as f:
                    geo = json.load(f)
            except (OSError, ValueError):
                logger.warning("Could not read/parse county boundary file %s", path)
                continue

            geometry = None
            if geo.get("type") == "FeatureCollection":
                feats = geo.get("features") or []
                if feats:
                    geometry = feats[0].get("geometry")
            elif geo.get("type") == "Feature":
                geometry = geo.get("geometry")
            elif geo.get("type") in ("Polygon", "MultiPolygon"):
                geometry = geo
            elif geo.get("type") == "GeometryCollection":
                # Real gap found and fixed: this branch didn't exist at
                # all before, so every county boundary file exported as a
                # bare GeometryCollection (confirmed: all 47 files in this
                # deployment are) silently produced geometry=None --
                # meaning _county_polygons() returned an EMPTY dict, and
                # RealAdminHierarchyProvider.resolve() could never match
                # ANY point to ANY county, for any location in Kenya.
                # Verified via a real boundary file: mombasa.json is a
                # GeometryCollection with exactly one MultiPolygon inside.
                geoms = geo.get("geometries") or []
                if len(geoms) == 1:
                    geometry = geoms[0]
                elif len(geoms) > 1:
                    # More than one geometry in the collection (seen in
                    # some exports when a county's boundary was split
                    # into disjoint parts as separate entries rather than
                    # one MultiPolygon) -- union them into a single shape
                    # so point-in-polygon resolution still works no
                    # matter how a given county's file was exported.
                    import shapely.geometry as _geom
                    import shapely.ops as _ops
                    shapes = [_geom.shape(g) for g in geoms if g is not None]
                    if shapes:
                        geometry = _geom.mapping(_ops.unary_union(shapes))
            if geometry is not None:
                polygons[name] = geometry

        self._county_polygons_cache = polygons
        return polygons

    # -- AdminUnit construction --------------------------------------------

    def _county_unit(self, name: str) -> Optional[AdminUnit]:
        info = self._county_by_name.get(name)
        if info is None:
            return None
        age = self._age_by_county.get(name) or self._age_by_county.get("_national") or {}
        polygons = self._county_polygons()
        return AdminUnit(
            level="county", code=info["slug"], name=name, parent_code=None, year=2019,
            population=info["population_2019"], households=info["households_2019"],
            area_km2=info["area_km2"], polygon_geojson=polygons.get(name),
            male_population=info["male"], female_population=info["female"],
            intersex_population=info["intersex"],
            households_conventional=info["households_conventional"],
            households_group_quarters=info["households_group_quarters"],
            children_pct=age.get("children_pct"), working_age_pct=age.get("working_age_pct"),
            young_adult_pct=age.get("young_adult_pct"), elderly_pct=age.get("elderly_pct"),
            extra_demographics=self._extra_demographics(name),
        )

    def _subcounty_unit(self, county_name: str, subcounty_name: str) -> Optional[AdminUnit]:
        info = self._subcounty_by_key.get((county_name.lower(), subcounty_name.lower()))
        if info is None:
            return None
        # Real sub-county age structure where it's been transcribed and
        # validated (see kenya_age_structure_2019_census.py's coverage
        # note); otherwise fall back to the county's own age profile --
        # an approximation, and still the unit's OWN real
        # population/sex/household figures either way.
        real_subcounty_age = self._age_by_subcounty.get((county_name.lower(), subcounty_name.lower()))
        age = real_subcounty_age or self._age_by_county.get(county_name) or self._age_by_county.get("_national") or {}
        extra = self._extra_demographics(county_name)
        if extra is not None and real_subcounty_age is None:
            # extra_demographics below is always county-level (KNBS's own
            # Volume IV tables this loader currently surfaces don't
            # reliably break out below county), so flag it as such
            # rather than silently presenting it as sub-county-specific.
            extra = {**extra, "_granularity": "county_level_approximation"}
        return AdminUnit(
            level="subcounty", code=info["slug"], name=info["name"],
            parent_code=self._county_by_name[county_name]["slug"], year=2019,
            population=info["population_2019"], households=info["households_2019"],
            area_km2=info["area_km2"], polygon_geojson=None,
            male_population=info["male"], female_population=info["female"],
            intersex_population=None,  # KNBS: not distributed below county level
            households_conventional=info["households_conventional"],
            households_group_quarters=info["households_group_quarters"],
            children_pct=age.get("children_pct"), working_age_pct=age.get("working_age_pct"),
            young_adult_pct=age.get("young_adult_pct"), elderly_pct=age.get("elderly_pct"),
            extra_demographics=extra,
        )

    # -- AdministrativeHierarchyProvider interface -------------------------

    def resolve(
        self, latitude: float, longitude: float,
        subcounty_hint: Optional[str] = None, sublocation_hint: Optional[str] = None,
    ) -> Sequence[AdminUnit]:
        """Real point-in-polygon county resolution, then real name-based
        descent to sub-county if `subcounty_hint` is supplied (see class
        docstring for why coordinates alone can't reach sub-county yet).
        `sublocation_hint` is accepted for forward compatibility once a
        validated sub-location dataset exists, but currently has no data
        to resolve against, so it's a documented no-op.
        """
        import shapely.geometry as geom

        path = []
        pt = geom.Point(longitude, latitude)
        matched_county = None
        for county_name, polygon_geojson in self._county_polygons().items():
            try:
                if geom.shape(polygon_geojson).contains(pt):
                    matched_county = county_name
                    break
            except Exception:  # noqa: BLE001 - malformed boundary file shouldn't crash resolution
                logger.exception("Bad boundary geometry for county %s", county_name)
                continue

        if matched_county is None:
            return path

        county_unit = self._county_unit(matched_county)
        if county_unit is not None:
            path.append(county_unit)

        if subcounty_hint:
            subcounty_unit = self._subcounty_unit(matched_county, subcounty_hint)
            if subcounty_unit is not None:
                path.append(subcounty_unit)
            else:
                logger.info(
                    "subcounty_hint %r not found under county %r; resolution stays at county level.",
                    subcounty_hint, matched_county,
                )

        return path

    def resolve_by_names(
        self, county: str, subcounty: Optional[str] = None,
    ) -> Sequence[AdminUnit]:
        """Name-only resolution, for callers that already know the admin
        path (e.g. from Google Geocoding address components, or a form
        field) and don't need point-in-polygon lookup at all. This is
        the most precise real resolution this provider can currently do:
        exact county + sub-county population, sex, and household figures
        with no geometry involved."""
        path = []
        county_unit = self._county_unit(county)
        if county_unit is None:
            return path
        path.append(county_unit)
        if subcounty:
            subcounty_unit = self._subcounty_unit(county, subcounty)
            if subcounty_unit is not None:
                path.append(subcounty_unit)
        return path

    def children(self, level: str, code: str) -> Sequence[AdminUnit]:
        if level != "county":
            return []
        county_name = next((n for n, i in self._county_by_name.items() if i["slug"] == code), None)
        if county_name is None:
            return []
        return [
            self._subcounty_unit(county_name, info["name"])
            for key, info in self._subcounty_by_key.items()
            if key[0] == county_name.lower()
        ]

    def historical_population(self, level: str, code: str) -> Sequence[tuple]:
        if level != "county":
            # No real 2009 sub-county figures transcribed yet -- honest
            # empty result, not an interpolated guess.
            return []
        info = next((i for i in self._county_by_name.values() if i["slug"] == code), None)
        if info is None:
            return []
        return [(2009, info["population_2009"]), (2019, info["population_2019"])]


# ============================================================================
# Deterministic calculations (spec Section 2, "Calculations")
# ============================================================================

def population_density(population: float, area_km2: float) -> Optional[float]:
    """Population Density = Population / Area_km2"""
    if not area_km2:
        return None
    return population / area_km2


def household_density(households: float, area_km2: float) -> Optional[float]:
    """Household Density = Households / Area_km2"""
    if not area_km2:
        return None
    return households / area_km2


def average_household_size(population: float, households: float) -> Optional[float]:
    """Average Household Size = Population / Households"""
    if not households:
        return None
    return population / households


def growth_rate_cagr(population_now: float, population_then: float, years: int) -> Optional[float]:
    """Growth Rate = ((Population_t / Population_t-k)^(1/k) - 1) * 100"""
    if population_then <= 0 or years <= 0:
        return None
    return ((population_now / population_then) ** (1.0 / years) - 1.0) * 100.0


def dependency_ratio(children_pct: float, elderly_pct: float, working_age_pct: float) -> Optional[float]:
    """Dependency Ratio = ((Pop_0-14 + Pop_65+) / Pop_15-64) * 100, computed
    on percentage shares (equivalent to raw counts since the population
    denominator cancels)."""
    if not working_age_pct:
        return None
    return ((children_pct + elderly_pct) / working_age_pct) * 100.0


def growth_density(population_increase: float, area_km2: float) -> Optional[float]:
    """Growth Density = Population Increase / Area_km2"""
    if not area_km2:
        return None
    return population_increase / area_km2


# ============================================================================
# Per-catchment feature computation
# ============================================================================

@dataclass
class CatchmentPopulationFeatures:
    catchment: Catchment
    population: Optional[float] = None
    population_estimation_method: str = ""
    population_density_per_km2: Optional[float] = None
    households: Optional[float] = None
    household_density_per_km2: Optional[float] = None
    avg_household_size: Optional[float] = None
    children_pct: Optional[float] = None
    working_age_pct: Optional[float] = None
    young_adult_pct: Optional[float] = None
    elderly_pct: Optional[float] = None
    dependency_ratio: Optional[float] = None
    growth_rate_1yr_pct: Optional[float] = None
    growth_rate_3yr_pct: Optional[float] = None
    growth_rate_5yr_pct: Optional[float] = None
    growth_density_per_km2: Optional[float] = None
    fay_herriot_shrinkage_gamma: Optional[float] = None
    pycnophylactic_population_estimate: Optional[float] = None
    housing_unit_method_population_estimate: Optional[float] = None
    cross_check_disagreement_pct: Optional[float] = None
    extra_demographics: Optional[dict] = None
    source: str = ""
    source_class: str = "unknown"
    warnings: list = field(default_factory=list)


def compute_catchment_population(
    catchment: Catchment,
    admin_hierarchy: AdministrativeHierarchyProvider,
    interpolator: PycnophylacticInterpolator,
    cell_size_deg: float = 0.005,
    subcounty_hint: Optional[str] = None,
    sublocation_hint: Optional[str] = None,
    fay_herriot_model: Optional[FittedFayHerriotModel] = None,
    fay_herriot_direct_variance: Optional[float] = None,
    fay_herriot_auxiliary: Optional[Sequence[float]] = None,
    housing_unit_inputs: Optional[HousingUnitMethodInputs] = None,
) -> CatchmentPopulationFeatures:
    """Full Section-2 population calculation for one catchment, at real
    administrative-hierarchy resolution.

    Pipeline:
      1. Resolve the catchment center's admin path (county -> ... ->
         finest known unit).
      2. Hierarchical fallback to find the finest unit with a
         published population figure.
      3. Build a Pycnophylactic-interpolated fine grid over that unit's
         siblings (so zone totals + volume-preservation apply at the
         resolution where real census data actually exists), and sum
         the grid cells intersecting the catchment's own geometry.
      4. If a fitted Fay-Herriot model and a direct-estimate variance
         are supplied, reconcile the Pycnophylactic estimate (as the
         direct estimate y_i) against the model's synthetic-regression
         prediction via the EBLUP shrinkage formula.
      5. If housing_unit_inputs are supplied, independently estimate
         population via the Housing Unit Method (MEASURE Evaluation
         Lesson 5) and average the two estimates, per that lesson's
         explicit guidance to cross-check and combine independent
         methods rather than trust one. Both individual estimates are
         retained on the returned features for transparency.
    """
    features = CatchmentPopulationFeatures(catchment=catchment)
    cell = catchment.location_cell
    lat, lon = float(cell.center_latitude), float(cell.center_longitude)

    # Real bug fixed here: this function previously never accepted or
    # forwarded a subcounty/sublocation hint at all, even though
    # RealAdminHierarchyProvider.resolve() supports one -- so finer-than-
    # county resolution was unreachable through this public entry point no
    # matter what a caller did. Auto-derive from the cell's own real
    # geocoded fields (BusinessLocationCell.subcounty / .sublocation,
    # populated via Google Geocoding -- see google_client.py's pattern) when
    # the caller doesn't explicitly override them, so finer resolution is
    # attempted by default rather than silently staying at county level
    # because nobody manually threaded a hint through three call layers.
    if subcounty_hint is None:
        subcounty_hint = getattr(cell, "subcounty", "") or None
    if sublocation_hint is None:
        sublocation_hint = getattr(cell, "sublocation", "") or None

    resolve_kwargs = {}
    if subcounty_hint:
        resolve_kwargs["subcounty_hint"] = subcounty_hint
    if sublocation_hint:
        resolve_kwargs["sublocation_hint"] = sublocation_hint
    try:
        path = list(admin_hierarchy.resolve(lat, lon, **resolve_kwargs))
    except TypeError:
        # Some AdministrativeHierarchyProvider implementations (e.g.
        # SyntheticAdminHierarchyProvider, or a caller-supplied test
        # double) only accept (lat, lon) -- degrade gracefully to
        # unhinted resolution rather than crashing every non-Real
        # provider that doesn't (yet) support hints.
        path = list(admin_hierarchy.resolve(lat, lon))
    if not path:
        features.warnings.append("Could not resolve this location to any administrative unit.")
        return features

    source_unit = resolve_population_source_unit(path)
    if source_unit is None:
        features.warnings.append("No administrative unit in the resolved path has a published population figure.")
        return features

    def _siblings_for(unit: AdminUnit) -> list:
        idx_u = path.index(unit)
        if idx_u > 0:
            parent_u = path[idx_u - 1]
            sib = list(admin_hierarchy.children(level=parent_u.level, code=parent_u.code))
        else:
            sib = []
        if unit not in sib:
            sib = sib + [unit]
        return sib

    # spatial_unit is the level actually used to build the interpolation
    # grid. It starts equal to source_unit (the finest level with a
    # published population figure) but may fall back to a coarser
    # ancestor below -- see the fallback block. Kept distinct from
    # source_unit throughout so demographic figures (age structure,
    # growth rate) can stay at source_unit's finer resolution even when
    # the SPATIAL interpolation had to use a coarser level.
    spatial_unit = source_unit
    siblings = _siblings_for(spatial_unit)
    zones_with_polygons = [u for u in siblings if u.polygon_geojson is not None]

    if not zones_with_polygons:
        # Real fallback, not a hard failure. E.g. RealAdminHierarchyProvider
        # resolves subcounty_hint to a subcounty source_unit that has a
        # real published population figure but NO boundary polygon (no
        # subcounty boundary dataset exists yet -- see that provider's
        # class docstring). Previously this unconditionally returned an
        # empty result: asking for MORE precision (a subcounty hint)
        # silently produced LESS (no population at all), while omitting
        # the hint worked. Walk up `path` to the nearest ANCESTOR that
        # itself carries real polygon_geojson and interpolate at that
        # coarser level instead -- the same "hierarchical fallback, not
        # silent substitution" principle resolve_population_source_unit()
        # already applies to population figures, applied here to geometry.
        for ancestor in reversed(path[: path.index(source_unit)]):
            if ancestor.polygon_geojson is not None:
                spatial_unit = ancestor
                siblings = _siblings_for(spatial_unit)
                zones_with_polygons = [u for u in siblings if u.polygon_geojson is not None]
                break

    if not zones_with_polygons:
        features.warnings.append(
            f"No polygon geometry available for {source_unit.level} '{source_unit.name}' "
            f"or any of its resolved ancestors -- population cannot be spatially interpolated."
        )
        return features

    if spatial_unit is not source_unit:
        features.warnings.append(
            f"No boundary polygon for {source_unit.level} '{source_unit.name}'; population was "
            f"spatially interpolated at the coarser {spatial_unit.level} '{spatial_unit.name}' level "
            f"instead. Age structure and growth-rate figures below still use the finer "
            f"{source_unit.level} '{source_unit.name}' data where KNBS publishes it -- only the "
            f"spatial population/household magnitude is coarsened, not the demographic percentages."
        )

    grid = interpolator.build_grid(zones_with_polygons, cell_size_deg=cell_size_deg)
    zone_totals = {u.code: u.population for u in zones_with_polygons if u.population is not None}
    smoothed = interpolator.interpolate(grid, zone_totals)

    if catchment.geometry_geojson is None:
        features.warnings.append(
            "Catchment has no geometry_geojson (radius bands with no network provider produce a "
            "circular buffer only; population-within-catchment needs real polygon geometry to spatially "
            "join against the interpolated grid)."
        )
        return features

    interpolated_population = interpolator.population_in_polygon(smoothed, catchment.geometry_geojson)
    method = f"pycnophylactic_interpolation_from_{spatial_unit.level}_{spatial_unit.code}"
    if spatial_unit is not source_unit:
        method += f"_demographics_from_{source_unit.level}_{source_unit.code}"
    features.pycnophylactic_population_estimate = interpolated_population

    final_population = interpolated_population
    if fay_herriot_model is not None and fay_herriot_direct_variance is not None and fay_herriot_auxiliary is not None:
        obs = AreaLevelObservation(
            area_id=str(catchment.pk) if catchment.pk else "unsaved",
            direct_estimate=interpolated_population,
            sampling_variance=fay_herriot_direct_variance,
            auxiliary=fay_herriot_auxiliary,
        )
        result = fay_herriot_model.predict(obs)
        final_population = result.eblup
        features.fay_herriot_shrinkage_gamma = result.shrinkage_gamma
        method += "_fay_herriot_eblup_reconciled"

    if housing_unit_inputs is not None:
        hum_population = estimate_population_housing_unit_method(housing_unit_inputs)
        features.housing_unit_method_population_estimate = hum_population
        if final_population > 0:
            features.cross_check_disagreement_pct = (
                abs(hum_population - final_population) / final_population * 100.0
            )
        # Per MEASURE Evaluation Lesson 5 guideline #6: combine
        # independent methods rather than trust either alone.
        final_population = (final_population + hum_population) / 2.0
        method += "_cross_checked_against_housing_unit_method"

    features.population = final_population
    features.population_estimation_method = method
    if spatial_unit is source_unit:
        features.source = f"business_intel.engines.population (admin unit: {source_unit.name})"
    else:
        features.source = (
            f"business_intel.engines.population (spatial unit: {spatial_unit.name}; "
            f"demographic unit: {source_unit.name})"
        )
    features.source_class = "internal_derived"

    area_km2 = float(catchment.area_km2) if catchment.area_km2 is not None else None
    if area_km2:
        features.population_density_per_km2 = population_density(final_population, area_km2)

    # IMPORTANT: household_share must be computed against spatial_unit's
    # own population/households, NOT source_unit's -- final_population's
    # magnitude was derived from spatial_unit's interpolated grid (see
    # `interpolated_population` above), so dividing by source_unit's
    # (potentially much smaller, e.g. a subcounty inside the county that
    # was actually interpolated) population would produce a household
    # share >1 and a nonsensical households estimate whenever spatial_unit
    # and source_unit diverge (the fallback path above).
    if spatial_unit.households is not None and spatial_unit.population:
        household_share = final_population / spatial_unit.population if spatial_unit.population else 0.0
        features.households = spatial_unit.households * household_share
        if area_km2:
            features.household_density_per_km2 = household_density(features.households, area_km2)
        features.avg_household_size = average_household_size(final_population, features.households)

    # Age composition + dependency ratio (spec Section 2), from the
    # source admin unit's published age/sex structure. Assumed uniform
    # across the unit and applied to the catchment -- the standard
    # small-area simplification when age structure isn't independently
    # available at finer resolution than the admin unit itself.
    features.children_pct = source_unit.children_pct
    features.working_age_pct = source_unit.working_age_pct
    features.young_adult_pct = source_unit.young_adult_pct
    features.elderly_pct = source_unit.elderly_pct
    features.extra_demographics = source_unit.extra_demographics
    if source_unit.children_pct is not None and source_unit.elderly_pct is not None and source_unit.working_age_pct:
        features.dependency_ratio = dependency_ratio(
            source_unit.children_pct, source_unit.elderly_pct, source_unit.working_age_pct
        )

    # Growth rates (spec Section 2), from historical vintages of the
    # SAME admin unit. The unit-level CAGR is assumed to apply
    # uniformly to the catchment (same simplification as above), then
    # used to back-calculate the catchment's own population k years
    # ago for growth_density. Deliberately keyed on source_unit (not
    # spatial_unit) even when they diverge -- growth rate is a
    # demographic-timeline figure, not a spatial-magnitude one, so the
    # finer source_unit resolution is the right one to use here, same as
    # children_pct/working_age_pct/etc. above. Note RealAdminHierarchyProvider
    # only has real historical vintages at county level today (see its
    # historical_population() implementation) -- so if source_unit is a
    # subcounty, history will come back empty and growth rates simply
    # won't be computed (recorded as a warning below), same honest gap
    # as before this patch, not something this patch changes.
    history = dict(admin_hierarchy.historical_population(source_unit.level, source_unit.code))
    if source_unit.population is not None:
        history.setdefault(source_unit.year, source_unit.population)
    any_growth_computed = False
    for k, attr in ((1, "growth_rate_1yr_pct"), (3, "growth_rate_3yr_pct"), (5, "growth_rate_5yr_pct")):
        past_year = source_unit.year - k
        past_pop = history.get(past_year)
        method_note = "exact_vintage"
        if past_pop is None and len(history) >= 2:
            # No exact vintage k years back -- fall back to inter-census
            # linear interpolation (MEASURE Evaluation Lesson 5, Eq. 5-2)
            # between the two REAL census vintages that bracket the
            # target year, rather than leaving the window empty just
            # because real census rounds (e.g. KNBS's 2009/2019, 10
            # years apart) don't land on a 1/3/5-year boundary.
            years_sorted = sorted(history.keys())
            lower = max((y for y in years_sorted if y <= past_year), default=None)
            upper = min((y for y in years_sorted if y >= past_year), default=None)
            if lower is not None and upper is not None and lower != upper:
                past_pop = intercensal_linear_estimate(
                    history[lower], past_year - lower, history[upper], upper - lower,
                )
                method_note = f"intercensal_linear_interpolation_between_{lower}_and_{upper}"
        if past_pop is None or past_pop <= 0:
            continue
        rate = growth_rate_cagr(source_unit.population, past_pop, k)
        if rate is None:
            continue
        setattr(features, attr, rate)
        any_growth_computed = True
        if k == 1 and area_km2:
            catchment_population_then = final_population / ((1 + rate / 100.0) ** k)
            features.growth_density_per_km2 = growth_density(
                final_population - catchment_population_then, area_km2
            )
    if not any_growth_computed:
        features.warnings.append(
            f"No historical population vintages (exact or inter-census-interpolable) available for "
            f"{source_unit.level} '{source_unit.name}'; growth rates could not be computed."
        )

    return features


# ============================================================================
# Orchestration / EngineRun integration
# ============================================================================

def run(
    query: BusinessQuery,
    admin_hierarchy: AdministrativeHierarchyProvider,
    interpolator: Optional[PycnophylacticInterpolator] = None,
    catchments: Optional[Sequence[Catchment]] = None,
    cell_size_deg: float = 0.005,
    fay_herriot_model: Optional[FittedFayHerriotModel] = None,
    fay_herriot_direct_variance: Optional[float] = None,
    fay_herriot_auxiliary: Optional[Sequence[float]] = None,
    housing_unit_inputs: Optional[HousingUnitMethodInputs] = None,
) -> dict:
    """Engine 2 entry point. Computes population features for every
    Catchment belonging to query.location_cell (or an explicit
    `catchments` subset) via real administrative-hierarchy resolution
    and Pycnophylactic interpolation, writes one FeatureObservation per
    computed metric per catchment, and records an EngineRun row.

    Requires Engine 1 to have already run -- catchments must exist and
    (for anything beyond a plain circular radius band) have real
    geometry_geojson to spatially join against.
    """
    started_at = timezone.now()
    cell = query.location_cell
    interpolator = interpolator or PycnophylacticInterpolator()
    target_catchments = list(catchments) if catchments is not None else list(cell.catchments.all())

    run_row = EngineRun.objects.create(
        query=query, engine_number=ENGINE_NUMBER, status="succeeded", started_at=started_at,
    )

    if not target_catchments:
        run_row.status = "failed"
        run_row.error_message = (
            "No Catchment rows found for this cell. Engine 1 (Spatial Context) must run first."
        )
        run_row.finished_at = timezone.now()
        run_row.save(update_fields=["status", "error_message", "finished_at"])
        return {"cell": cell, "features": [], "engine_run": run_row}

    features_written = 0
    warnings_seen = []
    all_features = []

    METRIC_FIELDS = [
        ("population", "people", "numeric"),
        ("population_density_per_km2", "people/km2", "numeric"),
        ("households", "households", "numeric"),
        ("household_density_per_km2", "households/km2", "numeric"),
        ("avg_household_size", "people/household", "numeric"),
        ("children_pct", "%", "numeric"),
        ("working_age_pct", "%", "numeric"),
        ("young_adult_pct", "%", "numeric"),
        ("elderly_pct", "%", "numeric"),
        ("dependency_ratio", "%", "numeric"),
        ("growth_rate_1yr_pct", "%/yr", "numeric"),
        ("growth_rate_3yr_pct", "%/yr", "numeric"),
        ("growth_rate_5yr_pct", "%/yr", "numeric"),
        ("growth_density_per_km2", "people/km2/yr", "numeric"),
        ("pycnophylactic_population_estimate", "people", "numeric"),
        ("housing_unit_method_population_estimate", "people", "numeric"),
        ("cross_check_disagreement_pct", "%", "numeric"),
    ]

    try:
        with transaction.atomic():
            for catchment in target_catchments:
                catch_features = compute_catchment_population(
                    catchment, admin_hierarchy, interpolator, cell_size_deg=cell_size_deg,
                    fay_herriot_model=fay_herriot_model,
                    fay_herriot_direct_variance=fay_herriot_direct_variance,
                    fay_herriot_auxiliary=fay_herriot_auxiliary,
                    housing_unit_inputs=housing_unit_inputs,
                )
                all_features.append(catch_features)
                warnings_seen.extend(catch_features.warnings)

                for field_name, unit, value_type in METRIC_FIELDS:
                    value = getattr(catch_features, field_name)
                    if value is None:
                        continue
                    FeatureObservation.write_latest(
                        location_cell=cell, catchment=catchment,
                        engine_number=ENGINE_NUMBER, feature_name=field_name,
                        defaults=dict(
                            value_type=value_type,
                            value_numeric=Decimal(str(round(value, 6))),
                            unit=unit,
                            observed_at=started_at,
                            source=catch_features.source or "business_intel.engines.population",
                            source_class=catch_features.source_class,
                            method=catch_features.population_estimation_method or "population_engine_calculation",
                            derivation_level="calculation",
                        ),
                    )
                    features_written += 1

                # Everything from Volume IV+ (disability, religion, water
                # source, ICT ownership, etc.) -- see AdminUnit.
                # extra_demographics's docstring. disability_pct also gets
                # its own numeric metric since it's the single most
                # commonly requested figure from that set; the rest stay
                # as one JSON blob rather than dozens of ad hoc numeric
                # fields, since which sub-keys exist varies by unit.
                extra = catch_features.extra_demographics
                if extra:
                    if isinstance(extra.get("disability_pct"), (int, float)):
                        FeatureObservation.write_latest(
                            location_cell=cell, catchment=catchment,
                            engine_number=ENGINE_NUMBER, feature_name="disability_pct",
                            defaults=dict(
                                value_type="numeric",
                                value_numeric=Decimal(str(round(extra["disability_pct"], 6))),
                                unit="%",
                                observed_at=started_at,
                                source=catch_features.source or "business_intel.engines.population",
                                source_class=catch_features.source_class,
                                method="kenya_census_loader.CensusData.disability",
                                derivation_level="observation",
                            ),
                        )
                        features_written += 1
                    FeatureObservation.write_latest(
                        location_cell=cell, catchment=catchment,
                        engine_number=ENGINE_NUMBER, feature_name="demographic_profile",
                        defaults=dict(
                            value_type="json",
                            value_json=extra,
                            unit="",
                            observed_at=started_at,
                            source=catch_features.source or "business_intel.engines.population",
                            source_class=catch_features.source_class,
                            method="kenya_census_loader.CensusData",
                            derivation_level="observation",
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
    if isinstance(admin_hierarchy, SyntheticAdminHierarchyProvider):
        run_row.status = "degraded"
        run_row.degraded_reason = "SyntheticAdminHierarchyProvider in use -- not real KNBS data."[:255]
    run_row.save(update_fields=["status", "finished_at", "duration_ms", "features_written", "degraded_reason"])

    return {"cell": cell, "features": all_features, "engine_run": run_row}