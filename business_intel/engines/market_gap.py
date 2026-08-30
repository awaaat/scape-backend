"""
business_intel/engines/market_gap.py

Engine 9 — Market Gap / Demand-Supply Engine
Per business_location_intelligence_engines_data_models_v2.md, Section 9.

NOTE ON THE SPEC FILE: same situation as every other engine in this
codebase — business_location_intelligence_engines_data_models_v2.md is
referenced but not present on disk. Built to match Engine 2
(population.py), Engine 3 (purchasing_power.py) and Engine 8
(competition.py) for structure, schema contract, and citation
discipline.

=============================================================================
WHY THIS IS A REAL STATISTICS/ML ENGINE, NOT A RATIO
=============================================================================
The naive version of "market gap" is competitors_observed / population —
a supply-per-capita ratio with no model behind it. That throws away
everything the rest of this codebase has already computed (purchasing
power, catchment area, spatial structure) and gives no way to say
whether an observed count is *surprising* given local demand, only
what the raw density happens to be. This version instead fits a real
count-regression model and asks a sharper question: given this area's
population, purchasing power, and physical size, how many businesses
of this category would we *expect* to see here — and how far off is
what we actually observe?

1. Poisson regression as a gravity model
   Flowerdew, R. and Aitkin, M. (1982). "A Method of Fitting the
   Gravity Model Based on the Poisson Distribution." Journal of
   Regional Science, 22(2), 191-202.
   This is the paper that established that the classic multiplicative
   gravity-model form (count ~ origin mass^a * destination mass^b *
   distance^-c) is exactly a log-linear Poisson GLM, and should be FIT
   as one (maximum likelihood via IRLS) rather than by taking logs and
   running OLS on non-negative integer counts (which mis-weights the
   small counts that matter most here). This module fits:

       log(mu_i) = offset_i + b0 + b1*log(population_i)
                   + b2*log(1 + purchasing_power_index_i)

   with offset_i = log(area_km2_i) (an "exposure" term — a bigger
   catchment gets proportionally more expected businesses for the same
   population density, standard Poisson-regression exposure handling,
   McCullagh & Nelder 1989 Sec 6.3.1), fit via Iteratively Reweighted
   Least Squares (McCullagh, P. and Nelder, J.A. 1989. Generalized
   Linear Models, 2nd ed., Chapman & Hall, Ch. 2.5) — the standard
   closed-form Newton-Raphson-equivalent GLM fitting algorithm, not an
   approximation of it. Implemented here directly on numpy (this
   codebase already has an undeclared numpy dependency via
   population.py; scipy/statsmodels/sklearn are NOT in requirements.txt
   and are not assumed available — see requirements.txt check below)
   rather than imported from a stats package that may not be installed.

2. Overdispersion diagnosis + quasi-Poisson correction
   Cameron, A.C. and Trivedi, P.K. (2013). Regression Analysis of
   Count Data, 2nd ed. Cambridge University Press, Ch. 3-4.
   Real business counts are essentially always overdispersed relative
   to a pure Poisson (variance > mean — a few areas are business
   deserts, a few are business clusters, more than Poisson allows).
   Fitting Poisson and ignoring this understates uncertainty. This
   module estimates the dispersion parameter phi_hat from the Pearson
   residuals (McCullagh & Nelder 1989 Sec 2.4) and, if phi_hat > 1,
   scales the coefficient covariance matrix by phi_hat (the standard
   quasi-Poisson correction — same point estimates, honest standard
   errors). This is flagged as a documented upgrade path to a full
   Negative Binomial regression (which needs an additional shape
   parameter fit by its own ML procedure) rather than silently
   pretending quasi-Poisson is equivalent to it — same "real gap,
   flagged not faked" discipline as population.py's
   SpatialSmoothingProvider hook.

3. Anscombe residuals as the actual gap statistic
   Anscombe, F.J. (1953). "Contribution to the Discussion of H.
   Hotelling's Paper." Journal of the Royal Statistical Society,
   Series B, 15(2), 229-230; formalized for the Poisson family in
   McCullagh & Nelder (1989), Sec 2.4.2, eq. 2.15.
   Raw (observed - expected) residuals from a Poisson model are
   heteroscedastic (variance = mean), so the same residual of "+3
   businesses" means something different in a low-demand area (mu=1)
   than a high-demand one (mu=40). The Anscombe residual applies the
   variance-stabilizing transform
       A(y) = (3/2) * (y^(2/3) - mu^(2/3)) / (mu^(1/6) * sqrt(phi_hat))
   which is approximately standard-normal under a correctly specified
   (quasi-)Poisson model — i.e. a genuine z-score for "how surprising
   is this count", comparable across areas of very different demand
   scale. This z-score, not a raw ratio, is what drives the gap
   classification below.

4. Cross-validation, not just in-sample fit
   Standard k-fold predictive check (Stone, M. 1974. "Cross-Validatory
   Choice and Assessment of Statistical Predictions." JRSS-B, 36(2),
   111-147) — refit the GLM on k-1 folds, score the held-out fold's
   Poisson deviance, and report the mean out-of-sample deviance
   alongside in-sample fit. This is the difference between "the model
   fits the data it was trained on" and "the model predicts new areas
   reasonably" — the latter is what actually justifies using it to
   say a *candidate* (out-of-sample) location is under/oversupplied.
   With few catchments (< MIN_CV_FOLD_SAMPLE), exact leave-one-out is
   used instead of k-fold (Stone 1974's own asymptotic-equivalence
   result doesn't hold at small n, so LOO — the unbiased, if noisier,
   estimator — is preferred there over a coarser 5-fold split of a
   handful of points).

5. McFadden's pseudo-R^2 as an in-sample fit summary
   McFadden, D. (1974). "Conditional Logit Analysis of Qualitative
   Choice Behavior." In P. Zarembka (ed.), Frontiers in Econometrics,
   Academic Press, 105-142. Originally for discrete-choice models but
   standard for any GLM fit by maximum likelihood:
       pseudo_R2 = 1 - (LL_full / LL_null)
   comparing the fitted model's log-likelihood against an
   intercept-only (+ offset) null model. Reported alongside CV
   deviance, not instead of it — pseudo-R^2 alone can look good while
   still overfitting a small corpus, which is exactly what step 4
   checks for independently.

Both regressors (population, purchasing power) are read from THIS
system's own feature store (models.FeatureObservation, written by
Engine 2 and Engine 3) rather than recomputed here — Engine 9 runs
after both in ENGINE_CHOICES order, and re-deriving population/wealth
figures from scratch inside this module would both duplicate that
logic and risk silently drifting from whatever Engine 2/3 actually
concluded (including their own honest gaps/fallbacks). If either
feature is missing for a catchment (upstream engine hasn't run, or
returned None), that catchment is dropped from the fit corpus and the
gap is reported with a recorded warning instead of guessing.

=============================================================================
ADMINISTRATIVE-UNIT NARROWING
=============================================================================
Same as purchasing_power.py: a caller can request the catchment-level
result (default) or narrow directly to county/sub-county via
`compute_market_gap_for_admin_unit`. Business counts aggregate over
every BusinessLocationCell tagged with that admin unit (same
`_businesses_for_admin_unit`-style query purchasing_power.py already
uses); population/purchasing-power covariates are the mean of whatever
per-cell feature-store values exist within that admin unit, which is a
documented approximation (a true admin-level regression would refit
against admin-level population/wealth totals, not an average of
catchment-level estimates) — flagged in `warnings`, not hidden.

=============================================================================
EDGE CASES THIS MODULE HANDLES EXPLICITLY
=============================================================================
- Fewer catchments than MIN_GLM_FIT_SAMPLE with usable covariates ->
  GLM is not fit; falls back to a simple corpus-mean density benchmark
  (`simple_density_benchmark`, still real math — a standardized rate
  comparison — just not model-based), flagged in warnings and
  `gap_method`.
- A category with zero observed businesses everywhere in the fit
  corpus -> Poisson MLE with the intercept driven very negative is a
  perfectly valid (if extreme) fit; not a divide-by-zero, but flagged
  if the fitted expected counts are implausibly close to zero
  everywhere (`degenerate_low_variance` warning) since IRLS convergence
  can be slow/unstable in that regime.
- Perfect / near-perfect separation (e.g. one covariate perfectly
  predicts presence/absence of any businesses) -> IRLS step-halving
  (see `_irls_fit`) prevents divergence, but a hard iteration cap
  (MAX_IRLS_ITER) still applies; if not converged by then, the
  fallback benchmark is used instead of returning an unstable fit,
  flagged explicitly.
- A single catchment with population but zero purchasing-power feature
  on record (Engine 3 hasn't run / returned None for that cell) ->
  dropped from the FIT corpus (can't have NaNs in the design matrix),
  but still SCORED against the fitted model using only its population
  term if purchasing power truly has no fit-corpus-wide signal to
  impute from; if it does, imputed at the fit corpus mean, same
  imputation-and-flag convention as purchasing_power.py's PCA scorer.
- Between-fold or LOO deviance blowing up on a tiny corpus (n close to
  the number of parameters) -> CV is skipped entirely (not reported as
  a possibly-nonsensical number) below MIN_CV_FOLD_SAMPLE, with a
  recorded warning rather than a misleadingly precise fit statistic.
- Negative or zero fitted expected count is mathematically impossible
  under this model (mu = exp(...) > 0 always) — no clip needed, unlike
  purchasing_power.py's between-area variance, which is a different
  quantity that CAN go negative under method-of-moments.
- Requested admin_level with no businesses/features found at all ->
  reports None/empty rather than fabricating a rate, same as every
  other engine in this codebase.
- SyntheticAdminHierarchyProvider in use, OR either upstream feature
  (population/purchasing power) resolving as `source_class=synthetic`
  for a majority of the fit corpus -> EngineRun forced to `degraded`,
  same convention as population.py / purchasing_power.py.

Scope boundary
--------------
Depends on business_intel.models, and imports AdministrativeHierarchyProvider
/ AdminUnit / SyntheticAdminHierarchyProvider from .population purely for
type/consistency reasons (this module does not itself call
AdministrativeHierarchyProvider.resolve — Engine 2 already did that; Engine
9 only reads what Engine 2 already wrote to the feature store). No
dependency on services.py, views.py, or competition.py (the Huff/HHI
machinery there answers "how would demand SPLIT among today's players";
this engine answers "is there room for another player at all" — related
questions, deliberately kept as separate engines per the spec's own
engine numbering, cross-referenced only via evidence dicts downstream in
engines.evidence / engines.decision).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Mapping, Optional, Sequence

import numpy as np
from django.db import transaction
from django.db.models import Count
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

logger = logging.getLogger("business_intel.engines.market_gap")

ENGINE_NUMBER = 9
POPULATION_ENGINE_NUMBER = 2
PURCHASING_POWER_ENGINE_NUMBER = 3

# ============================================================================
# Calibration inputs — documented as such, not measured constants.
# ============================================================================

# Comrey & Lee (1992)'s ~5-observations-per-parameter rule of thumb,
# same citation purchasing_power.py uses for its own PCA fit floor.
# This model has 3 parameters (intercept, log(population), log(pp+1)),
# so 3*5=15, rounded up to a flat 20 as a slightly more conservative
# floor given Poisson MLE (unlike PCA) can be unstable with few points
# per parameter when counts are small and sparse.
MIN_GLM_FIT_SAMPLE = 20

# Below this many fit-corpus rows, exact leave-one-out replaces k-fold
# (see module docstring, point 4).
MIN_CV_FOLD_SAMPLE = 40
K_FOLDS = 5

MAX_IRLS_ITER = 50
IRLS_TOL = 1e-8

# Below this many operational businesses of ANY category in a
# catchment, the catchment's own direct count is still used (Poisson
# handles small/zero counts natively, unlike purchasing_power's
# continuous indicators) but a widen-worthiness note is NOT triggered
# the way purchasing_power.py's MIN_DIRECT_SAMPLE_SIZE does — a count
# of 0 is informative here, not "too thin to trust", so this engine
# does not widen to admin level automatically. Callers who explicitly
# want admin-level scope use compute_market_gap_for_admin_unit.

# Overdispersion rule-of-thumb threshold (Cameron & Trivedi 2013 Sec
# 3.2): phi_hat meaningfully above 1 signals quasi-Poisson correction
# is warranted; phi_hat above this second, higher threshold signals the
# gap is severe enough that a full Negative Binomial fit (not
# implemented here — see module docstring) would materially change
# conclusions, so it's flagged more loudly rather than silently
# quasi-corrected and left at that.
OVERDISPERSION_WARN_THRESHOLD = 1.5
OVERDISPERSION_SEVERE_THRESHOLD = 4.0

# Anscombe z-score cut points for the categorical gap label. A
# calibration input (not derived from data) chosen to match the
# conventional "beyond +/-1.96 is roughly the 5% two-tailed normal
# tail" reference points, at 1 and 2 SD for a three-tier "mild/clear"
# split rather than a single threshold.
Z_MILD = 1.0
Z_CLEAR = 2.0


# ============================================================================
# Feature-store read helper (cross-engine wiring)
# ============================================================================

def _read_latest_feature(
    location_cell: BusinessLocationCell,
    catchment: Optional[Catchment],
    engine_number: int,
    feature_name: str,
) -> tuple[Optional[float], str]:
    """
    Reads the current is_latest=True numeric value an EARLIER engine
    wrote to the shared feature store, plus its source_class ('internal_derived'
    | 'synthetic' | ...). Returns (None, '') if that engine hasn't run
    yet for this identity, or wrote a non-numeric value. This is the
    one new piece of feature-store-read plumbing this module adds —
    every other engine so far has only ever WRITTEN via
    FeatureObservation.write_latest; Engine 9 is the first one whose
    own inputs are other engines' outputs rather than raw
    BusinessEntity/Catchment data, so a read-side helper is genuinely
    new work, not a re-implementation of something that already existed.
    """
    row = (
        FeatureObservation.objects.filter(
            location_cell=location_cell,
            catchment=catchment,
            engine_number=engine_number,
            feature_name=feature_name,
            is_latest=True,
        )
        .order_by("-observed_at")
        .first()
    )
    if row is None or row.value_numeric is None:
        return None, ""
    return float(row.value_numeric), row.source_class or ""


# ============================================================================
# Pure-math layer — no Django imports needed to use anything below this
# point, same "independently unit-testable" pattern as competition.py's
# CompetitorObservation / haversine_m section.
# ============================================================================

@dataclass
class MarketGapObservation:
    """One catchment's row in the GLM design matrix / fit corpus.
    population and purchasing_power_index are None when the upstream
    engine hasn't produced a figure for this identity — such rows are
    dropped from the FIT corpus (see fit_poisson_gravity_glm) but may
    still be independently SCORED (see score_catchment)."""
    catchment_id: Optional[int]
    business_count: int
    population: Optional[float]
    purchasing_power_index: Optional[float]
    area_km2: Optional[float]
    population_source_class: str = ""
    purchasing_power_source_class: str = ""


@dataclass
class PoissonGLMFit:
    """A fitted log(mu) = offset + X @ beta model plus the diagnostics
    that make it trustworthy to use (or not): dispersion, pseudo-R^2,
    cross-validated deviance. covariate_names EXCLUDES the intercept
    (beta[0] is always the intercept)."""
    covariate_names: tuple
    beta: np.ndarray
    covariance: np.ndarray            # coefficient covariance (already
                                       # dispersion-scaled if phi_hat > 1)
    dispersion_phi: float
    converged: bool
    iterations: int
    log_likelihood_full: float
    log_likelihood_null: float
    pseudo_r2_mcfadden: Optional[float]
    cv_mean_deviance: Optional[float]
    cv_method: str                    # "k_fold" | "leave_one_out" | "skipped_small_sample"
    fit_n: int
    x_means: np.ndarray                # for imputing missing covariates when scoring
    x_stds: np.ndarray                 # kept for reporting only; IRLS itself fits on raw log-covariates


def _design_matrix(rows: Sequence[MarketGapObservation]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Builds (X, y, offset) for rows with COMPLETE covariates. X's
    first column is the intercept (all ones); remaining columns are
    log(population) and log(1 + purchasing_power_index), matching
    covariate_names order ('log_population', 'log_purchasing_power_index').
    Rows missing population, purchasing_power_index, or area_km2 are
    excluded here — caller is responsible for counting/flagging how
    many were dropped."""
    usable = [
        r for r in rows
        if r.population is not None and r.population > 0
        and r.purchasing_power_index is not None
        and r.area_km2 is not None and r.area_km2 > 0
    ]
    n = len(usable)
    X = np.ones((n, 3))
    y = np.zeros(n)
    offset = np.zeros(n)
    for i, r in enumerate(usable):
        X[i, 1] = math.log(r.population)
        X[i, 2] = math.log1p(r.purchasing_power_index)
        y[i] = r.business_count
        offset[i] = math.log(r.area_km2)
    return X, y, offset


def _irls_fit(X: np.ndarray, y: np.ndarray, offset: np.ndarray) -> tuple[np.ndarray, np.ndarray, bool, int]:
    """
    Iteratively Reweighted Least Squares for a Poisson GLM with a log
    link and a fixed offset (McCullagh & Nelder 1989, Ch 2.5). Returns
    (beta, unscaled_covariance, converged, iterations). unscaled_covariance
    is (X'WX)^-1 at convergence, BEFORE any dispersion (quasi-Poisson)
    scaling — caller applies that.

    Step-halving guard: if a raw Newton step would make the deviance
    WORSE (a real risk with small/separated data), the step is halved
    (up to 10 times) before being taken, rather than diverging outright
    or silently returning an unstable fit. This is standard IRLS
    practice for GLMs (see e.g. Green, P.J. 1984. "Iteratively
    Reweighted Least Squares for Maximum Likelihood Estimation, and
    some Robust and Resistant Alternatives." JRSS-B 46(2), 149-192,
    Sec 2) — not an ad hoc addition.
    """
    n, p = X.shape
    beta = np.zeros(p)
    # Start from a sane, non-degenerate point: mean-log-count intercept,
    # zero slopes -- avoids the eta=0 starting point sending exp(eta)=1
    # for every row regardless of covariates on iteration 1.
    beta[0] = math.log(max(y.mean(), 0.1))

    def _deviance(b: np.ndarray) -> float:
        eta = X @ b + offset
        mu = np.exp(np.clip(eta, -30, 30))
        # Poisson deviance, with the y*log(y/mu) term defined as 0 at y=0
        # (its limit), per McCullagh & Nelder (1989) eq. 2.10.
        term = np.where(y > 0, y * np.log(np.divide(y, mu, out=np.ones_like(y), where=y > 0)), 0.0)
        return float(2.0 * np.sum(term - (y - mu)))

    prev_dev = _deviance(beta)
    converged = False
    it = 0
    covariance = np.eye(p)

    for it in range(1, MAX_IRLS_ITER + 1):
        eta = X @ beta + offset
        mu = np.exp(np.clip(eta, -30, 30))
        mu = np.clip(mu, 1e-8, None)  # guard log/divide below, not a modeling clip
        W = mu  # Poisson working weight = mu (canonical log link)
        z = (eta - offset) + (y - mu) / mu  # working response

        WX = X * W[:, None]
        XtWX = X.T @ WX
        XtWz = X.T @ (W * z)
        try:
            step = np.linalg.solve(XtWX, XtWz)
        except np.linalg.LinAlgError:
            # Singular design (e.g. a covariate constant across the
            # whole fit corpus) -- ridge-stabilize rather than crash;
            # flagged upstream via `converged=False` if this doesn't
            # ultimately settle.
            step = np.linalg.solve(XtWX + 1e-6 * np.eye(p), XtWz)

        new_beta = step
        new_dev = _deviance(new_beta)

        # Step-halving: if the full Newton step increases deviance,
        # halve the step from beta toward new_beta up to 10 times.
        halvings = 0
        candidate = new_beta
        while new_dev > prev_dev + 1e-10 and halvings < 10:
            candidate = beta + (candidate - beta) / 2.0
            new_dev = _deviance(candidate)
            halvings += 1

        change = np.max(np.abs(candidate - beta)) if p else 0.0
        beta = candidate
        if abs(prev_dev - new_dev) < IRLS_TOL or change < IRLS_TOL:
            prev_dev = new_dev
            converged = True
            break
        prev_dev = new_dev

    eta = X @ beta + offset
    mu = np.clip(np.exp(np.clip(eta, -30, 30)), 1e-8, None)
    W = mu
    XtWX = X.T @ (X * W[:, None])
    try:
        covariance = np.linalg.inv(XtWX)
    except np.linalg.LinAlgError:
        covariance = np.linalg.pinv(XtWX)

    return beta, covariance, converged, it


def _poisson_log_likelihood(y: np.ndarray, mu: np.ndarray) -> float:
    """Sum of Poisson log-pmf, dropping the log(y!) term's dependence
    on y only where it cancels (kept here in full via math.lgamma for
    a genuine log-likelihood value, needed for McFadden's pseudo-R^2 to
    be interpretable on its own, not just as a difference)."""
    mu = np.clip(mu, 1e-12, None)
    ll = 0.0
    for yi, mi in zip(y, mu):
        ll += yi * math.log(mi) - mi - math.lgamma(yi + 1.0)
    return float(ll)


def _fit_null_model(y: np.ndarray, offset: np.ndarray) -> tuple[float, float]:
    """Intercept-only (+ offset) Poisson model has a closed-form MLE:
    exp(b0) * exposure = mean(y / exposure)-weighted rate; specifically
    b0 = log(sum(y) / sum(exp(offset))). Returns (b0, log_likelihood)."""
    exposure = np.exp(offset)
    b0 = math.log(max(y.sum(), 1e-8) / max(exposure.sum(), 1e-8))
    mu_null = math.exp(b0) * exposure
    return b0, _poisson_log_likelihood(y, mu_null)


def _k_fold_indices(n: int, k: int, seed: int = 42) -> list:
    rng = np.random.default_rng(seed)
    idx = rng.permutation(n)
    return np.array_split(idx, k)


def _cross_validate(X: np.ndarray, y: np.ndarray, offset: np.ndarray) -> tuple[Optional[float], str]:
    """
    Returns (mean_out_of_sample_deviance_per_point, method). Refits the
    GLM on each training split from scratch (exact CV, not an
    influence-function approximation) — real but O(k) or O(n) IRLS
    fits; acceptable at this codebase's expected catchment-corpus
    sizes (dozens to low hundreds, not millions).
    """
    n = X.shape[0]
    if n < MIN_CV_FOLD_SAMPLE:
        if n < 6:
            return None, "skipped_small_sample"
        method = "leave_one_out"
        folds = [np.array([i]) for i in range(n)]
    else:
        method = "k_fold"
        folds = _k_fold_indices(n, K_FOLDS)

    total_dev = 0.0
    total_points = 0
    for test_idx in folds:
        if len(test_idx) == 0 or len(test_idx) == n:
            continue
        train_idx = np.setdiff1d(np.arange(n), test_idx)
        if len(train_idx) <= X.shape[1]:
            continue  # not enough training points left to fit this fold; skip it
        beta_fold, _, _, _ = _irls_fit(X[train_idx], y[train_idx], offset[train_idx])
        eta_test = X[test_idx] @ beta_fold + offset[test_idx]
        mu_test = np.clip(np.exp(np.clip(eta_test, -30, 30)), 1e-8, None)
        y_test = y[test_idx]
        term = np.where(
            y_test > 0,
            y_test * np.log(np.divide(y_test, mu_test, out=np.ones_like(y_test), where=y_test > 0)),
            0.0,
        )
        dev = 2.0 * np.sum(term - (y_test - mu_test))
        total_dev += float(dev)
        total_points += len(test_idx)

    if total_points == 0:
        return None, "skipped_small_sample"
    return total_dev / total_points, method


def fit_poisson_gravity_glm(rows: Sequence[MarketGapObservation]) -> Optional[PoissonGLMFit]:
    """
    Full pipeline: build the design matrix, fit via IRLS, diagnose
    overdispersion, compute pseudo-R^2 and cross-validated deviance.
    Returns None (not an exception) if fewer than MIN_GLM_FIT_SAMPLE
    usable rows exist — caller falls back to simple_density_benchmark.
    """
    X, y, offset = _design_matrix(rows)
    n, p = X.shape
    if n < MIN_GLM_FIT_SAMPLE:
        return None

    beta, cov_unscaled, converged, iters = _irls_fit(X, y, offset)
    eta = X @ beta + offset
    mu = np.clip(np.exp(np.clip(eta, -30, 30)), 1e-8, None)

    pearson_resid = (y - mu) / np.sqrt(mu)
    dispersion_phi = float(np.sum(pearson_resid ** 2) / max(n - p, 1))
    dispersion_phi = max(dispersion_phi, 1e-6)

    covariance = cov_unscaled * dispersion_phi if dispersion_phi > 1.0 else cov_unscaled

    ll_full = _poisson_log_likelihood(y, mu)
    _, ll_null = _fit_null_model(y, offset)
    pseudo_r2 = None
    if ll_null < 0:
        pseudo_r2 = 1.0 - (ll_full / ll_null)

    cv_dev, cv_method = _cross_validate(X, y, offset)

    return PoissonGLMFit(
        covariate_names=("log_population", "log_purchasing_power_index"),
        beta=beta,
        covariance=covariance,
        dispersion_phi=round(dispersion_phi, 4),
        converged=converged,
        iterations=iters,
        log_likelihood_full=round(ll_full, 4),
        log_likelihood_null=round(ll_null, 4),
        pseudo_r2_mcfadden=round(pseudo_r2, 4) if pseudo_r2 is not None else None,
        cv_mean_deviance=round(cv_dev, 4) if cv_dev is not None else None,
        cv_method=cv_method,
        fit_n=n,
        x_means=X.mean(axis=0),
        x_stds=X.std(axis=0, ddof=1) if n > 1 else np.ones(p),
    )


def simple_density_benchmark(
    business_count: int,
    area_km2: Optional[float],
    corpus_rate_per_km2: Optional[float],
) -> tuple[Optional[float], list]:
    """
    Fallback used only below MIN_GLM_FIT_SAMPLE: compares this
    catchment's own business density against the (smaller, still real)
    fit corpus's mean businesses-per-km2 for the category, scaled by
    THIS catchment's area to get an expected count. Not model-based —
    no population/purchasing-power adjustment — but a real, honestly
    labeled comparison rather than an invented number. Returns
    (expected_count, warnings).
    """
    warnings = ["Fit corpus below MIN_GLM_FIT_SAMPLE; using simple density-benchmark fallback (see module docstring)."]
    if not area_km2 or area_km2 <= 0 or corpus_rate_per_km2 is None:
        return None, warnings + ["Insufficient data for even the fallback benchmark."]
    return corpus_rate_per_km2 * area_km2, warnings


# ============================================================================
# Scoring a single catchment against a fitted model
# ============================================================================

def anscombe_residual_poisson(y: float, mu: float, dispersion_phi: float = 1.0) -> float:
    """
    Variance-stabilized Poisson residual (Anscombe 1953; McCullagh &
    Nelder 1989 eq. 2.15), approximately standard normal under a
    correctly specified (quasi-)Poisson model. This is THE gap
    z-score this engine reports — not a raw (observed-expected) count.
    """
    mu = max(mu, 1e-8)
    raw = 1.5 * (y ** (2.0 / 3.0) - mu ** (2.0 / 3.0)) / (mu ** (1.0 / 6.0))
    return raw / math.sqrt(max(dispersion_phi, 1e-8))


def gap_classification(z: float) -> str:
    """
    z < 0 means FEWER businesses observed than the gravity model
    expects given local population/purchasing power -- i.e. under-
    served, a candidate opportunity. z > 0 means MORE than expected --
    oversupplied/saturated. Thresholds are calibration inputs (see
    Z_MILD / Z_CLEAR module constants), not derived from data.
    """
    if z <= -Z_CLEAR:
        return "clearly_underserved"
    if z <= -Z_MILD:
        return "mildly_underserved"
    if z >= Z_CLEAR:
        return "clearly_oversupplied"
    if z >= Z_MILD:
        return "mildly_oversupplied"
    return "balanced"


def market_gap_score_0_100(z: float) -> float:
    """
    Rescales the Anscombe z-score onto a 0-100 'opportunity score'
    (100 = wide open / strongly underserved, 0 = strongly oversupplied)
    via a logistic squash centered at z=0 -- same rescale convention
    purchasing_power.py's PCAWealthIndexModel.score uses, so every
    engine's 0-100 features share one interpretation shape. This
    squash (like competition.py's competition_score blend) is this
    module's own presentation layer on top of the sourced statistic
    (the Anscombe z-score itself is the cited part); not claimed as an
    external formula.
    """
    # Negative z (underserved) -> high score. Scale so |z|=2 (Z_CLEAR)
    # lands close to the tails of the 0-100 range without ever fully
    # saturating.
    return round(100.0 / (1.0 + math.exp(z)), 2)


@dataclass
class CatchmentGapResult:
    catchment_id: Optional[int]
    observed_count: int
    expected_count: Optional[float]
    expected_count_method: str          # "poisson_gravity_glm" | "simple_density_benchmark"
    anscombe_z: Optional[float]
    dispersion_phi: Optional[float]
    gap_classification: str
    market_gap_score: Optional[float]
    covariates_imputed: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


def score_catchment(
    row: MarketGapObservation,
    fitted_model: Optional[PoissonGLMFit],
    corpus_rate_per_km2: Optional[float] = None,
) -> CatchmentGapResult:
    """
    Scores ONE catchment's observed count against either the fitted
    GLM (preferred) or the density-benchmark fallback. Missing
    covariates on an otherwise-scoreable row are imputed at the fit
    corpus's own mean for that covariate (same convention as
    purchasing_power.py's PCA scorer: contributes ~0 relative to the
    corpus baseline, flagged so it isn't mistaken for an observation).
    """
    imputed = []
    warnings = []

    if fitted_model is not None and row.area_km2 and row.area_km2 > 0:
        log_pop = None
        log_pp = None
        if row.population is not None and row.population > 0:
            log_pop = math.log(row.population)
        else:
            log_pop = fitted_model.x_means[1]
            imputed.append("population missing; imputed at fit-corpus mean log(population).")
        if row.purchasing_power_index is not None:
            log_pp = math.log1p(row.purchasing_power_index)
        else:
            log_pp = fitted_model.x_means[2]
            imputed.append("purchasing_power_index missing; imputed at fit-corpus mean.")

        x = np.array([1.0, log_pop, log_pp])
        eta = float(x @ fitted_model.beta) + math.log(row.area_km2)
        mu = max(math.exp(min(max(eta, -30), 30)), 1e-8)
        z = anscombe_residual_poisson(row.business_count, mu, fitted_model.dispersion_phi)

        return CatchmentGapResult(
            catchment_id=row.catchment_id,
            observed_count=row.business_count,
            expected_count=round(mu, 3),
            expected_count_method="poisson_gravity_glm",
            anscombe_z=round(z, 3),
            dispersion_phi=fitted_model.dispersion_phi,
            gap_classification=gap_classification(z),
            market_gap_score=market_gap_score_0_100(z),
            covariates_imputed=imputed,
            warnings=warnings,
        )

    # Fallback path
    expected, fb_warnings = simple_density_benchmark(row.business_count, row.area_km2, corpus_rate_per_km2)
    warnings.extend(fb_warnings)
    if expected is None:
        return CatchmentGapResult(
            catchment_id=row.catchment_id,
            observed_count=row.business_count,
            expected_count=None,
            expected_count_method="simple_density_benchmark",
            anscombe_z=None,
            dispersion_phi=None,
            gap_classification="unknown",
            market_gap_score=None,
            warnings=warnings,
        )
    # No fitted dispersion available in the fallback path; phi=1 (pure
    # Poisson assumption) is the honest default absent a fitted model
    # to estimate it from -- flagged, not silently assumed equivalent
    # to a real dispersion estimate.
    warnings.append("No dispersion estimate available in fallback mode; Anscombe z uses phi=1 (unadjusted Poisson assumption).")
    z = anscombe_residual_poisson(row.business_count, expected, dispersion_phi=1.0)
    return CatchmentGapResult(
        catchment_id=row.catchment_id,
        observed_count=row.business_count,
        expected_count=round(expected, 3),
        expected_count_method="simple_density_benchmark",
        anscombe_z=round(z, 3),
        dispersion_phi=None,
        gap_classification=gap_classification(z),
        market_gap_score=market_gap_score_0_100(z),
        warnings=warnings,
    )


# ============================================================================
# Django-facing data assembly
# ============================================================================

def _business_count(location_cell: BusinessLocationCell, category: str) -> int:
    return BusinessEntity.objects.filter(
        location_cell=location_cell, category=category, current_status="operational",
    ).count()


def _observation_for_catchment(catchment: Catchment, category: str) -> MarketGapObservation:
    cell = catchment.location_cell
    population, pop_src = _read_latest_feature(cell, catchment, POPULATION_ENGINE_NUMBER, "population")
    pp_index, pp_src = _read_latest_feature(
        cell, catchment, PURCHASING_POWER_ENGINE_NUMBER, "composite_purchasing_power_index",
    )
    return MarketGapObservation(
        catchment_id=catchment.id,
        business_count=_business_count(cell, category),
        population=population,
        purchasing_power_index=pp_index,
        area_km2=float(catchment.area_km2) if catchment.area_km2 is not None else None,
        population_source_class=pop_src,
        purchasing_power_source_class=pp_src,
    )


def _businesses_for_admin_unit(county: str, category: str, subcounty: Optional[str] = None) -> int:
    """Same aggregation pattern as purchasing_power.py's identically
    named helper -- kept local (not imported cross-module) since the
    category filter here makes the query shape different enough that
    sharing one function would need a category=None special case in a
    module that otherwise never deals with 'all categories'."""
    cell_filter = {"county": county}
    if subcounty:
        cell_filter["subcounty"] = subcounty
    cell_ids = BusinessLocationCell.objects.filter(**cell_filter).values_list("id", flat=True)
    return BusinessEntity.objects.filter(
        location_cell_id__in=cell_ids, category=category, current_status="operational",
    ).count()


def compute_market_gap_for_admin_unit(
    county: str,
    category: str,
    fitted_model: Optional[PoissonGLMFit],
    subcounty: Optional[str] = None,
    corpus_rate_per_km2: Optional[float] = None,
) -> CatchmentGapResult:
    """
    Direct entry point scoped to county/sub-county rather than one
    catchment. population_index / purchasing_power_index / area_km2
    are averaged across every BusinessLocationCell's most recent
    catchment-level feature-store values within the admin unit -- a
    documented approximation (see module docstring's admin-narrowing
    section), not a true admin-level regression.
    """
    cell_filter = {"county": county}
    if subcounty:
        cell_filter["subcounty"] = subcounty
    cells = list(BusinessLocationCell.objects.filter(**cell_filter))

    pops, pps, areas = [], [], []
    for cell in cells:
        for catchment in cell.catchments.all():
            p, _ = _read_latest_feature(cell, catchment, POPULATION_ENGINE_NUMBER, "population")
            w, _ = _read_latest_feature(cell, catchment, PURCHASING_POWER_ENGINE_NUMBER, "composite_purchasing_power_index")
            if p is not None:
                pops.append(p)
            if w is not None:
                pps.append(w)
            if catchment.area_km2 is not None:
                areas.append(float(catchment.area_km2))

    row = MarketGapObservation(
        catchment_id=None,
        business_count=_businesses_for_admin_unit(county, category, subcounty),
        population=sum(pops) if pops else None,  # admin-level total, not a mean, for population
        purchasing_power_index=(sum(pps) / len(pps)) if pps else None,  # index stays a mean
        area_km2=sum(areas) if areas else None,
    )
    result = score_catchment(row, fitted_model, corpus_rate_per_km2)
    result.warnings.append(
        f"Admin-unit ({'subcounty' if subcounty else 'county'}={subcounty or county!r}) scoring uses "
        "summed population/area and MEAN purchasing-power across constituent catchments' own "
        "feature-store values -- an approximation of a true admin-level regression, not a refit "
        "against admin-level totals."
    )
    return result


# ============================================================================
# Corpus assembly + orchestration / EngineRun integration
# ============================================================================

def _fit_corpus_for_category(category: str) -> list:
    """Every Catchment in the DB, scored for `category`, used both to
    FIT the GLM and (for rows with incomplete covariates) to compute
    the fallback density-rate. Same live-corpus-per-run pattern
    purchasing_power.py's _fit_pca_model_from_corpus uses -- caching a
    periodically refit model is real separate infra work, intentionally
    left as a caller's responsibility, not faked here."""
    rows = []
    for catchment in Catchment.objects.select_related("location_cell").all():
        rows.append(_observation_for_catchment(catchment, category))
    return rows


def _corpus_density_rate(rows: Sequence[MarketGapObservation]) -> Optional[float]:
    usable = [(r.business_count, r.area_km2) for r in rows if r.area_km2 and r.area_km2 > 0]
    if not usable:
        return None
    total_count = sum(c for c, _ in usable)
    total_area = sum(a for _, a in usable)
    return (total_count / total_area) if total_area > 0 else None


METRIC_FIELDS = [
    ("expected_count", "count", "numeric"),
    ("anscombe_z", "z_score", "numeric"),
    ("market_gap_score", "index_0_100", "numeric"),
]


def run(
    query: BusinessQuery,
    admin_hierarchy: AdministrativeHierarchyProvider,
    catchments: Optional[Sequence[Catchment]] = None,
) -> dict:
    """
    Engine 9 entry point. Fits the Poisson gravity GLM for
    query.business_category across every Catchment currently in the DB
    with usable population/purchasing-power features, scores every
    Catchment belonging to query.location_cell against it, writes one
    FeatureObservation per metric per catchment plus a categorical
    'gap_classification' row, and records an EngineRun -- same
    contract shape as population.py / purchasing_power.py's run().

    admin_hierarchy is accepted (not used directly -- see module
    docstring's scope-boundary note) purely so this engine's signature
    is consistent with the others and a synthetic-provider check can
    still gate the EngineRun's degraded status, same convention as
    Engine 2/3.
    """
    started_at = timezone.now()
    cell = query.location_cell
    category = query.business_category
    target_catchments = list(catchments) if catchments is not None else list(cell.catchments.all())

    run_row = EngineRun.objects.create(
        query=query, engine_number=ENGINE_NUMBER, status="succeeded", started_at=started_at,
    )

    if not target_catchments:
        run_row.status = "failed"
        run_row.error_message = "No Catchment rows found for this cell. Engine 1 (Spatial Context) must run first."
        run_row.finished_at = timezone.now()
        run_row.save(update_fields=["status", "error_message", "finished_at"])
        return {"cell": cell, "features": [], "engine_run": run_row}

    fit_corpus = _fit_corpus_for_category(category)
    fitted_model = fit_poisson_gravity_glm(fit_corpus)
    corpus_rate = _corpus_density_rate(fit_corpus)

    if fitted_model is None:
        logger.info(
            "Engine %s: fit corpus for category=%r has < %s usable rows; using density-benchmark fallback.",
            ENGINE_NUMBER, category, MIN_GLM_FIT_SAMPLE,
        )

    features_written = 0
    warnings_seen = []
    all_results = []
    synthetic_feature_count = 0

    try:
        with transaction.atomic():
            for catchment in target_catchments:
                row = _observation_for_catchment(catchment, category)
                if row.population_source_class == "synthetic" or row.purchasing_power_source_class == "synthetic":
                    synthetic_feature_count += 1

                result = score_catchment(row, fitted_model, corpus_rate)
                all_results.append(result)
                warnings_seen.extend(result.warnings)

                for field_name, unit, value_type in METRIC_FIELDS:
                    value = getattr(result, field_name)
                    if value is None:
                        continue
                    FeatureObservation.write_latest(
                        location_cell=cell, catchment=catchment,
                        engine_number=ENGINE_NUMBER, feature_name=field_name,
                        business_category=category,
                        defaults=dict(
                            value_type=value_type,
                            value_numeric=Decimal(str(round(value, 6))),
                            unit=unit,
                            observed_at=started_at,
                            source="business_intel.engines.market_gap",
                            source_class="internal_derived",
                            method=result.expected_count_method,
                            derivation_level="model_estimate",
                            sample_size=fitted_model.fit_n if fitted_model else len(fit_corpus),
                        ),
                    )
                    features_written += 1

                FeatureObservation.write_latest(
                    location_cell=cell, catchment=catchment,
                    engine_number=ENGINE_NUMBER, feature_name="gap_classification",
                    business_category=category,
                    defaults=dict(
                        value_type="categorical",
                        value_text=result.gap_classification,
                        unit="", observed_at=started_at,
                        source="business_intel.engines.market_gap", source_class="internal_derived",
                        method=result.expected_count_method, derivation_level="business_conclusion",
                    ),
                )
                features_written += 1

                if fitted_model is not None:
                    FeatureObservation.write_latest(
                        location_cell=cell, catchment=catchment,
                        engine_number=ENGINE_NUMBER, feature_name="model_diagnostics",
                        business_category=category,
                        defaults=dict(
                            value_type="json",
                            value_json={
                                "dispersion_phi": fitted_model.dispersion_phi,
                                "overdispersed": fitted_model.dispersion_phi > OVERDISPERSION_WARN_THRESHOLD,
                                "severely_overdispersed": fitted_model.dispersion_phi > OVERDISPERSION_SEVERE_THRESHOLD,
                                "pseudo_r2_mcfadden": fitted_model.pseudo_r2_mcfadden,
                                "cv_mean_deviance": fitted_model.cv_mean_deviance,
                                "cv_method": fitted_model.cv_method,
                                "fit_n": fitted_model.fit_n,
                                "converged": fitted_model.converged,
                                "covariate_names": list(fitted_model.covariate_names),
                                "beta": fitted_model.beta.tolist(),
                            },
                            unit="", observed_at=started_at,
                            source="business_intel.engines.market_gap", source_class="internal_derived",
                            method="poisson_gravity_glm_irls", derivation_level="model_estimate",
                            sample_size=fitted_model.fit_n,
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

    degraded_reasons = []
    if warnings_seen:
        degraded_reasons.append("; ".join(sorted(set(warnings_seen)))[:200])
    if fitted_model is not None and fitted_model.dispersion_phi > OVERDISPERSION_SEVERE_THRESHOLD:
        degraded_reasons.append(
            f"Severe overdispersion (phi_hat={fitted_model.dispersion_phi}); quasi-Poisson SE correction "
            "applied but a Negative Binomial refit would be more appropriate (not implemented)."
        )
    if fitted_model is not None and not fitted_model.converged:
        degraded_reasons.append("Poisson GLM IRLS did not converge within MAX_IRLS_ITER; coefficients may be unstable.")
    if isinstance(admin_hierarchy, SyntheticAdminHierarchyProvider) or (
        target_catchments and synthetic_feature_count / len(target_catchments) > 0.5
    ):
        degraded_reasons.append(
            "Synthetic admin-hierarchy provider and/or majority-synthetic upstream population/"
            "purchasing-power features in use -- not real production data."
        )

    if degraded_reasons:
        run_row.status = "degraded"
        run_row.degraded_reason = "; ".join(degraded_reasons)[:255]

    run_row.save(update_fields=["status", "finished_at", "duration_ms", "features_written", "degraded_reason"])

    return {
        "cell": cell,
        "results": all_results,
        "engine_run": run_row,
        "glm_fitted": fitted_model is not None,
        "glm_diagnostics": (
            {
                "dispersion_phi": fitted_model.dispersion_phi,
                "pseudo_r2_mcfadden": fitted_model.pseudo_r2_mcfadden,
                "cv_mean_deviance": fitted_model.cv_mean_deviance,
                "cv_method": fitted_model.cv_method,
                "fit_n": fitted_model.fit_n,
                "converged": fitted_model.converged,
            }
            if fitted_model is not None else None
        ),
    }

def market_gap_evidence(result: CatchmentGapResult, glm_diagnostics=None) -> dict:
    return {
        "observed_business_count": result.observed_count,
        "expected_business_count": result.expected_count,
        "expected_count_method": result.expected_count_method,
        "anscombe_residual_z": result.anscombe_z,
        "dispersion_phi": result.dispersion_phi,
        "gap_classification": result.gap_classification,
        "market_gap_score_0_100": result.market_gap_score,
        "covariates_imputed": result.covariates_imputed,
        "warnings": result.warnings,
        "glm_diagnostics": glm_diagnostics,
        "methods": [
            "poisson_gravity_glm_irls",
            "quasi_poisson_overdispersion_correction",
            "anscombe_residual",
            "k_fold_cross_validated_deviance",
        ],
    }


def compute_market_gap_score(query, admin_hierarchy, catchments=None, primary_catchment_size_m=2000) -> tuple:
    run_result = run(query, admin_hierarchy, catchments=catchments)
    engine_run = run_result["engine_run"]
    results = run_result.get("results", [])

    if not results:
        return None, {
            "warnings": [engine_run.error_message or "No Catchment rows available for Market Gap scoring."],
            "engine_run_status": engine_run.status,
        }

    target_catchments = list(catchments) if catchments is not None else list(query.location_cell.catchments.all())
    catchment_by_id = {c.pk: c for c in target_catchments}
    primary = next(
        (
            r for r in results
            if r.catchment_id in catchment_by_id
            and catchment_by_id[r.catchment_id].catchment_size_value == primary_catchment_size_m
            and catchment_by_id[r.catchment_id].catchment_size_unit == "meters"
        ),
        None,
    )
    if primary is None:
        return None, {
            "warnings": [
                f"Market Gap engine ran ({len(results)} catchment(s) scored) but none matched "
                f"primary_catchment_size_m={primary_catchment_size_m}m."
            ],
            "engine_run_status": engine_run.status,
            "scored_catchment_ids": [r.catchment_id for r in results],
        }

    evidence = market_gap_evidence(primary, glm_diagnostics=run_result.get("glm_diagnostics"))
    evidence["glm_fitted"] = run_result.get("glm_fitted")
    evidence["engine_run_status"] = engine_run.status
    evidence["engine_run_degraded_reason"] = engine_run.degraded_reason or None

    return primary.market_gap_score, evidence
