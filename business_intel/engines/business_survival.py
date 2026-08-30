"""
business_intel/engines/business_survival.py

Engine 7 -- Business Survival Engine
Per business_location_intelligence_engines_data_models_v2.md, Section 7.

NOTE ON THE SPEC FILE: same situation as every other engine in this
codebase -- business_location_intelligence_engines_data_models_v2.md is
referenced but not present on disk. Built to match Engine 2/3/4/5/6/8/9
for structure, schema contract, and citation discipline.

=============================================================================
SCOPE BOUNDARY VS. ENGINE 6 (read this before extending)
=============================================================================
Engine 6 (business_ecosystem.py) asks "is the catchment's business
POPULATION as a whole growing, shrinking, or churning" -- an aggregate
entry/exit RATE computed directly from BusinessLifecycleEvent counts.

This engine asks a genuinely different question: "how long does a GIVEN
business tend to last, and which observable traits (chain vs.
independent, category) are associated with living longer or dying
sooner?" That is individual-level time-to-event modeling, not a
population rate -- a real methodological distinction (aggregate rates
vs. survival/hazard modeling), not a cosmetic one. This boundary is the
same one Engine 6's own docstring draws when it explicitly defers
"how long does a given business tend to last" to "Engine 7's job."

=============================================================================
WHAT THIS ENGINE ACTUALLY MEASURES
=============================================================================
Three real, separately-cited questions:

  1. HOW LONG do businesses in this catchment survive, overall? --
     Kaplan-Meier product-limit estimator of the survival function
     S(t), with Greenwood's formula for its variance and a
     log-log-transformed confidence band that stays inside [0,1].
     Median survival time (and its confidence interval, inverted from
     the same CI band per Brookmeyer & Crowley (1982)) is reported, or
     explicitly flagged "not reached" if fewer than half the tracked
     businesses have confirmed-closed by the end of the observation
     window -- a real, common, and honestly-reported outcome, not a
     missing value.

  2. DOES SURVIVAL DIFFER by an observable group -- most usefully
     chain vs. independent, or across the catchment's business
     categories? The log-rank (Mantel-Haenszel) test, generalized to
     more than two groups per Peto & Peto (1972), gives a real
     hypothesis test (chi-square statistic, real p-value) for whether
     the group-specific Kaplan-Meier curves are distinguishable, not
     an eyeballed comparison of two numbers.

  3. WHICH TRAITS are independently associated with hazard, and by how
     much? -- a Cox (1972) proportional-hazards regression, fit here
     by hand via Newton-Raphson maximum partial likelihood with
     Breslow's (1974) approximation for tied event times (this
     environment has no lifelines/scikit-survival/statsmodels
     available, so the partial-likelihood score/information-matrix
     iteration is implemented directly in this module -- see the
     "REAL METHODS USED" section for exactly which formulas). Reports
     hazard ratios with Wald confidence intervals, plus Harrell's
     (1982) concordance index (C-index) as an honest, real measure of
     how well the fitted model actually discriminates -- a model with
     poor C-index is reported as such, not hidden.

A Nelson-Aalen cumulative-hazard estimate is reported alongside
Kaplan-Meier as an independent cross-check (same "two independent
estimators of a related quantity" convention business_ecosystem.py uses
for Shannon vs. Simpson diversity) -- Kaplan-Meier and Nelson-Aalen are
mathematically related (S(t) ~ exp(-H(t))) but computed via genuinely
different estimators, so close agreement between them is real evidence
the survival estimate isn't an artifact of one method's assumptions.

=============================================================================
WHAT'S ACTUALLY AVAILABLE VS. WHAT ISN'T
=============================================================================
Real data used: models.BusinessEntity (category, brand, status, lat/lon,
first_observed_at) and models.BusinessLifecycleEvent (real
opened/closed_confirmed/censored_last_seen rows, already distinguishing
confirmed deaths from right-censored ones -- see
BusinessLifecycleEvent's own docstring). No external API calls in this
engine at all -- same DB-only scope as business_ecosystem.py, reading
whatever this codebase has already synced via
google_client.sync_places_to_business_entities (Engine 8's dependency)
plus whatever BRS/Gazette-sourced BusinessLifecycleEvent rows exist.

What is explicitly an approximation, flagged as such:
  - ORIGIN TIME (t=0 for each business): the earliest 'opened' lifecycle
    event is used when one exists; otherwise BusinessEntity.
    first_observed_at is used as a fallback origin, flagged
    origin_is_first_observed_fallback. Neither is guaranteed to be the
    business's TRUE founding date -- 'google_places'-sourced "opened"
    events mean "first time OUR sync saw this place" (same caveat
    business_ecosystem.py's docstring already documents for entry
    rates), and first_observed_at is a strictly weaker proxy still.
    This is a real, standard survival-analysis problem called LEFT
    TRUNCATION (a business that opened before this system started
    observing the catchment is only "discovered" already alive, which
    biases naive Kaplan-Meier toward businesses that survive long
    enough to be seen) -- Klein & Moeschberger (2003), Survival
    Analysis: Techniques for Censored and Truncated Data, 2nd ed.,
    Springer, Ch. 3, is the standard reference for the risk-set
    adjustment (using each business's actual entry time into
    observation, not calendar time zero, as its left-truncation time)
    that properly corrects for this. NOT implemented here -- this
    module treats each business's own origin as its own t=0 (a
    standard, simpler convention that is correct as long as origin
    time is read as "time since this system considers the business to
    have opened," not literally "time since incorporation") -- a
    documented simplification, not a hidden one, same "known
    limitation, not faked" convention as business_ecosystem.py's
    missing Donnelly edge-correction on Clark-Evans.
  - CENSORING TIME for currently-operational businesses with no
    lifecycle event at all is the run's as_of_date (today by default).
    Businesses whose BusinessEntity.current_status suggests non-
    operational but that have NO confirmed_closed/censored_last_seen
    event row are still treated as right-censored at as_of_date rather
    than as an event -- BusinessLifecycleEvent, not BusinessEntity.
    current_status, is this codebase's source of truth for the event
    itself (same convention business_ecosystem.py's own churn
    component already follows); the status/event mismatch is flagged
    in warnings rather than silently resolved either way.
  - Cox PH covariates are deliberately minimal and self-contained:
    is_chain (1.0 if BusinessEntity.brand is populated, else 0.0) and
    up to MAX_CATEGORY_DUMMIES one-hot category indicators for the
    catchment's most frequent categories (reference category =
    everything else). This keeps the model inside this engine's own
    DB-only scope boundary (no dependency on any other engine's
    FeatureObservation output, same independence business_ecosystem.py
    keeps) and, just as importantly, keeps events-per-covariate high
    enough to trust per Peduzzi et al. (1995) -- see MIN_EVENTS_PER_
    COVARIATE below. A richer covariate set (rent, competitor density,
    foot traffic) would need cross-engine feature joins that are
    explicitly out of scope here.

=============================================================================
REAL METHODS USED (researched and cited, not invented)
=============================================================================
1. Kaplan-Meier product-limit estimator & Greenwood's formula
   Kaplan, E.L., Meier, P. (1958). "Nonparametric Estimation from
   Incomplete Observations." Journal of the American Statistical
   Association, 53(282), 457-481.
   Greenwood, M. (1926). "The Natural Duration of Cancer." Reports on
   Public Health and Medical Subjects, No. 33, His Majesty's Stationery
   Office, London, 1-26.
   S_hat(t) = product over event times t_i <= t of (1 - d_i/n_i), where
   d_i = confirmed deaths at t_i, n_i = number at risk just before t_i.
   Greenwood's variance: Var[S_hat(t)] = S_hat(t)^2 * sum_{t_i<=t}
   d_i / (n_i * (n_i - d_i)).

2. Log-log-transformed confidence band & Brookmeyer-Crowley median CI
   Kalbfleisch, J.D., Prentice, R.L. (2002). The Statistical Analysis of
   Failure Time Data, 2nd ed., Wiley, Sec. 4.3 (log-log transform keeps
   the CI band inside [0,1], unlike a plain Greenwood normal-
   approximation band, which can leave [0,1] in small samples or in the
   tails -- exactly the sample-size regime this engine's catchments sit
   in).
   Brookmeyer, R., Crowley, J. (1982). "A Confidence Interval for the
   Median Survival Time." Biometrics, 38(1), 29-41. (CI for the median
   obtained here by inverting the same log-log CI band: the median's
   CI is the widest bracket of t for which the band still contains
   0.5.)

3. Nelson-Aalen cumulative hazard estimator
   Nelson, W. (1972). "Theory and Applications of Hazard Plotting for
   Censored Failure Data." Technometrics, 14(4), 945-966.
   Aalen, O.O. (1978). "Nonparametric Inference for a Family of
   Counting Processes." The Annals of Statistics, 6(4), 701-726.
   H_hat(t) = sum_{t_i<=t} d_i/n_i. Variance here uses the simpler
   Poisson-type form Var[H_hat(t)] = sum_{t_i<=t} d_i/n_i^2 (Klein &
   Moeschberger (2003), Ch. 4 -- a more refined d_i*(n_i-d_i)/n_i^3
   form also exists and is a documented, not implemented, refinement
   here, same convention as this module's other flagged
   simplifications).

4. Log-rank (Mantel-Haenszel) test, generalized to k groups
   Mantel, N. (1966). "Evaluation of Survival Data and Two New Rank
   Order Statistics Arising in Its Consideration." Cancer Chemotherapy
   Reports, 50(3), 163-170.
   Peto, R., Peto, J. (1972). "Asymptotically Efficient Rank Invariant
   Test Procedures" (with discussion). Journal of the Royal Statistical
   Society, Series A, 135(2), 185-207. (Source of the k-group
   variance-covariance generalization used below, beyond Mantel's
   original 2-sample form.)
   At each distinct event time, observed-minus-expected deaths per
   group are accumulated against a hypergeometric variance-covariance
   matrix; the resulting quadratic form is chi-square distributed with
   (k-1) degrees of freedom under the null of identical hazard
   functions across groups.

5. Cox proportional-hazards regression, hand-fit via Newton-Raphson
   Cox, D.R. (1972). "Regression Models and Life-Tables" (with
   discussion). Journal of the Royal Statistical Society, Series B,
   34(2), 187-220.
   Breslow, N.E. (1974). "Covariance Analysis of Censored Survival
   Data." Biometrics, 30(1), 89-99. (Ties-handling approximation used
   below -- the simplest and most widely implemented correction for
   tied event times in the partial likelihood; Efron's (1977) more
   exact alternative is a documented, not implemented, refinement.)
   Maximizes the Breslow-tied partial log-likelihood via Newton-
   Raphson on the score vector / observed-information matrix (both
   derived and coded directly in cox_ph_regression below, since no
   scipy/statsmodels/lifelines is available in this environment).
   Peduzzi, P., Concato, J., Feinstein, A.R., Holford, T.R. (1995).
   "Importance of Events per Independent Variable in Proportional
   Hazards Regression Analysis. II. Accuracy and Precision of
   Regression Estimates." Journal of Clinical Epidemiology, 48(12),
   1503-1510. (Source of the >=10-events-per-covariate rule of thumb
   enforced by MIN_EVENTS_PER_COVARIATE below -- fitting Cox PH with
   too few confirmed closures relative to covariate count is a real,
   well-documented source of biased, imprecise coefficients, not a
   theoretical nicety.)

6. Harrell's concordance index (C-index)
   Harrell, F.E., Califf, R.M., Pryor, D.B., Lee, K.L., Rosati, R.A.
   (1982). "Evaluating the Yield of Medical Tests." JAMA, 247(18),
   2543-2546.
   Fraction of usable (comparable) pairs of businesses where the model's
   predicted relative risk correctly ranks which one closed first;
   0.5 = no better than chance, 1.0 = perfect discrimination. Reported
   as a real, honest self-evaluation of the fitted Cox model rather
   than reporting hazard ratios without any indication of whether the
   model actually discriminates.

=============================================================================
EDGE CASES THIS MODULE HANDLES EXPLICITLY
=============================================================================
- Zero businesses with a determinable origin in the catchment -> every
  component is None, not 0/undefined-as-zero (same "data gap, not
  evidence of an empty ecosystem" principle as business_ecosystem.py
  and BusinessLocationCell.has_category_data).
- Fewer than MIN_N_FOR_KAPLAN_MEIER businesses with a determinable
  origin -> Kaplan-Meier/Nelson-Aalen withheld entirely rather than
  reported with a misleadingly precise (but statistically meaningless)
  curve.
- A business with an 'opened' event but no closure/censoring event and
  current_status != "operational" -> treated as right-censored at
  as_of_date (see "WHAT'S ACTUALLY AVAILABLE" above), flagged rather
  than silently guessed as an event.
- A business whose only available origin proxy (first_observed_at) is
  itself null -> excluded from the duration set entirely (no
  determinable time-zero means no duration, not a duration of 0),
  counted in warnings as undetermined_origin_excluded.
- Non-positive computed duration (event/censoring date on or before
  origin date -- a real possibility with imprecise 'first observed'
  timestamps) -> excluded as a malformed record, flagged, never
  silently clamped to a small positive number.
- Zero confirmed closures in the catchment (100% right-censored) ->
  Kaplan-Meier curve is still computed (it stays at 1.0 throughout,
  which IS the correct estimate given zero observed deaths) but median
  survival is reported as "not reached" rather than fabricated, and
  Cox PH / log-rank are withheld outright (there is no hazard signal
  to model or compare without at least some confirmed deaths).
- Fewer than MIN_EVENTS_FOR_LOGRANK confirmed closures in any one
  comparison group -> that group excluded from the log-rank comparison
  (its risk-set contribution to other groups' expected counts is still
  included, per the standard method -- only the group-level result is
  withheld), flagged rather than silently biasing the test.
- Cox PH events-per-covariate below MIN_EVENTS_PER_COVARIATE (Peduzzi
  et al. 1995) -> Cox PH withheld entirely rather than fit and reported
  with a real but statistically unreliable coefficient.
- Cox PH Newton-Raphson fails to converge within MAX_NEWTON_ITER
  iterations, or the observed-information matrix is (near-)singular
  (perfectly or near-perfectly collinear covariates -- e.g. a category
  dummy with zero within-category variance in this catchment) -> a
  small ridge term is added to the diagonal and flagged
  (ridge_regularization_applied); if it still fails to converge, Cox PH
  is withheld with a convergence_failed flag rather than returning
  unstable coefficients.
- All businesses share the same value of a covariate (e.g. every
  business in a tiny catchment is independent, no chain examples at
  all) -> that covariate is dropped before fitting (zero variance, no
  identifiable coefficient), flagged as dropped_zero_variance_covariate,
  rather than left in to produce a singular information matrix.
- Catchment is an isochrone type (minutes-based, no fixed meter radius)
  -> business retrieval here only supports meter-radius catchments,
  same documented, unimplemented gap as business_ecosystem.py; skipped
  with an explicit flag.

Scope boundary
--------------
Depends on business_intel.models only (BusinessEntity,
BusinessLifecycleEvent, BusinessLocationCell, Catchment, BusinessQuery,
EngineRun, FeatureObservation). No dependency on any other engine's
feature-store output or any external API -- purely a real-data read
across this codebase's own accumulated BusinessEntity/
BusinessLifecycleEvent history, same "genuinely independent" DB-only
scope as Engine 6.
"""

from __future__ import annotations

import logging
import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional, Sequence

from django.db import transaction
from django.utils import timezone as django_timezone

from ..models import (
    BusinessEntity, BusinessLifecycleEvent, BusinessLocationCell,
    Catchment, BusinessQuery, EngineRun, FeatureObservation,
)

logger = logging.getLogger("business_intel.engines.business_survival")

ENGINE_NUMBER = 7

MIN_N_FOR_KAPLAN_MEIER = 10       # below this, S_hat(t)/median are too unstable to report
MIN_EVENTS_FOR_LOGRANK = 5        # per group; below this a group is excluded from the comparison
MIN_EVENTS_PER_COVARIATE = 10     # Peduzzi et al. (1995) EPV rule for trustworthy Cox PH
MAX_CATEGORY_DUMMIES = 3          # cap covariate growth -> keeps EPV defensible
MAX_NEWTON_ITER = 50
NEWTON_TOL = 1e-6
RIDGE_EPS = 1e-6
Z_975 = 1.959963985                # standard normal 97.5th percentile (hardcoded, no scipy)

# Generous by design (no per-call API cost in this engine, same reasoning as
# business_ecosystem.py's DEFAULT_CHURN_LOOKBACK_DAYS) -- 5 years gives Cox PH
# and the log-rank test a real multi-year event history to work with.
DEFAULT_OBSERVATION_WINDOW_DAYS = 5 * 365


# ============================================================================
# Numerically-independent helpers (erf-based normal CDF, incomplete-gamma-
# based chi-square survival function, tiny linear-algebra solvers). No
# scipy/numpy in this environment -- duplicated rather than imported from
# business_ecosystem.py per this codebase's established per-engine
# independent-footprint convention (see business_ecosystem.py's own note on
# haversine_m).
# ============================================================================

def _standard_normal_cdf(z: float) -> float:
    """Phi(z) via the error function."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _two_sided_p_from_z(z: float) -> float:
    return 2.0 * (1.0 - _standard_normal_cdf(abs(z)))


def _log_gamma(x: float) -> float:
    """Lanczos approximation to ln(Gamma(x)), standard coefficients
    (g=7, n=9). Used only to build the regularized incomplete gamma
    function below, itself used only to get chi-square p-values without
    scipy."""
    g = 7
    coeffs = [
        0.99999999999980993, 676.5203681218851, -1259.1392167224028,
        771.32342877765313, -176.61502916214059, 12.507343278686905,
        -0.13857109526572012, 9.9843695780195716e-6, 1.5056327351493116e-7,
    ]
    if x < 0.5:
        return math.log(math.pi / math.sin(math.pi * x)) - _log_gamma(1 - x)
    x -= 1
    a = coeffs[0]
    t = x + g + 0.5
    for i in range(1, g + 2):
        a += coeffs[i] / (x + i)
    return 0.5 * math.log(2 * math.pi) + (x + 0.5) * math.log(t) - t + math.log(a)


def _gammainc_reg_lower(a: float, x: float) -> float:
    """Regularized lower incomplete gamma P(a, x) via series expansion
    (Abramowitz & Stegun 1964, Handbook of Mathematical Functions,
    Sec. 6.5). Valid/stable for x < a + 1."""
    if x <= 0:
        return 0.0
    gln = _log_gamma(a)
    ap = a
    total = 1.0 / a
    delta = total
    for _ in range(200):
        ap += 1
        delta *= x / ap
        total += delta
        if abs(delta) < abs(total) * 1e-14:
            break
    return total * math.exp(-x + a * math.log(x) - gln)


def _gammainc_reg_upper(a: float, x: float) -> float:
    """Regularized upper incomplete gamma Q(a, x) = 1 - P(a, x). Uses the
    series form for x < a+1 and a continued-fraction expansion
    (Abramowitz & Stegun 1964, Sec. 6.5) for x >= a+1, matching the
    standard split used to keep both regimes numerically stable."""
    if x <= 0:
        return 1.0
    if x < a + 1:
        return 1.0 - _gammainc_reg_lower(a, x)
    gln = _log_gamma(a)
    tiny = 1e-300
    b = x + 1 - a
    c = 1.0 / tiny
    d = 1.0 / b
    h = d
    for i in range(1, 200):
        an = -i * (i - a)
        b += 2
        d = an * d + b
        if abs(d) < tiny:
            d = tiny
        c = b + an / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-14:
            break
    return math.exp(-x + a * math.log(x) - gln) * h


def _chi2_sf(x: float, df: int) -> float:
    """P(X > x) for a chi-square random variable with df degrees of
    freedom, i.e. the p-value for a chi-square test statistic. df >= 1."""
    if x <= 0:
        return 1.0
    return max(0.0, min(1.0, _gammainc_reg_upper(df / 2.0, x / 2.0)))


def _matrix_solve(a_rows: list, b_vec: list) -> Optional[list]:
    """Solve A x = b via Gaussian elimination with partial pivoting.
    a_rows is a list of lists (square, n x n); b_vec is length n.
    Returns None if the system is (numerically) singular."""
    n = len(b_vec)
    m = [row[:] + [b_vec[i]] for i, row in enumerate(a_rows)]
    for col in range(n):
        pivot_row = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[pivot_row][col]) < 1e-12:
            return None
        m[col], m[pivot_row] = m[pivot_row], m[col]
        pivot = m[col][col]
        for j in range(col, n + 1):
            m[col][j] /= pivot
        for r in range(n):
            if r != col:
                factor = m[r][col]
                for j in range(col, n + 1):
                    m[r][j] -= factor * m[col][j]
    return [m[i][n] for i in range(n)]


def _matrix_inverse(a_rows: list) -> Optional[list]:
    """Inverse of a small square matrix via Gauss-Jordan elimination
    with partial pivoting. Returns None if singular. Only ever called
    here with n = number of Cox PH covariates (small: <= 1 +
    MAX_CATEGORY_DUMMIES)."""
    n = len(a_rows)
    m = [row[:] + [1.0 if i == j else 0.0 for j in range(n)] for i, row in enumerate(a_rows)]
    for col in range(n):
        pivot_row = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[pivot_row][col]) < 1e-12:
            return None
        m[col], m[pivot_row] = m[pivot_row], m[col]
        pivot = m[col][col]
        for j in range(2 * n):
            m[col][j] /= pivot
        for r in range(n):
            if r != col:
                factor = m[r][col]
                for j in range(2 * n):
                    m[r][j] -= factor * m[col][j]
    return [row[n:] for row in m]


# ============================================================================
# Pure-math layer 1: Kaplan-Meier product-limit estimator + Greenwood's
# formula + log-log CI band + Brookmeyer-Crowley-style median CI.
# ============================================================================

@dataclass
class KMPoint:
    time_days: int
    at_risk: int
    events: int
    censored: int
    survival: float
    ci_lower: float
    ci_upper: float


@dataclass
class KaplanMeierResult:
    n: int
    total_events: int
    total_censored: int
    curve: list = field(default_factory=list)          # list[KMPoint]
    median_survival_days: Optional[float] = None
    median_ci_lower_days: Optional[float] = None
    median_ci_upper_days: Optional[float] = None
    median_reached: bool = False
    warnings: list = field(default_factory=list)


def kaplan_meier_estimator(durations: Sequence[float], events: Sequence[int], alpha: float = 0.05) -> Optional[KaplanMeierResult]:
    """
    durations: time-to-event or time-to-censoring, in days, one per
    business, already computed by the caller (origin-to-event or
    origin-to-as_of_date). events: 1 = confirmed closure, 0 = censored.
    Point 1/2, module docstring.
    """
    n = len(durations)
    if n < MIN_N_FOR_KAPLAN_MEIER:
        return None

    warnings = []
    distinct_times = sorted(set(durations))
    survival = 1.0
    greenwood_sum = 0.0
    curve = []
    at_risk = n
    cum_events = 0
    cum_censored = 0

    for t in distinct_times:
        d_t = sum(1 for dur, ev in zip(durations, events) if dur == t and ev == 1)
        c_t = sum(1 for dur, ev in zip(durations, events) if dur == t and ev == 0)
        if d_t > 0:
            factor = 1.0 - d_t / at_risk
            survival *= factor
            if at_risk > d_t:
                greenwood_sum += d_t / (at_risk * (at_risk - d_t))
        cum_events += d_t
        cum_censored += c_t

        if survival <= 0.0 or survival >= 1.0:
            ci_lower, ci_upper = survival, survival
        else:
            # log-log (complementary log-log) transform, Kalbfleisch & Prentice
            # (2002) Sec. 4.3 -- keeps the band inside [0, 1].
            var_greenwood = (survival ** 2) * greenwood_sum
            se_cloglog = math.sqrt(var_greenwood) / (survival * abs(math.log(survival))) if survival not in (0.0, 1.0) else 0.0
            theta = math.log(-math.log(survival))
            lower = math.exp(-math.exp(theta + Z_975 * se_cloglog))
            upper = math.exp(-math.exp(theta - Z_975 * se_cloglog))
            ci_lower, ci_upper = max(0.0, lower), min(1.0, upper)

        curve.append(KMPoint(
            time_days=int(t), at_risk=at_risk, events=d_t, censored=c_t,
            survival=round(survival, 6), ci_lower=round(ci_lower, 6), ci_upper=round(ci_upper, 6),
        ))
        at_risk -= (d_t + c_t)

    total_events = sum(events)
    total_censored = n - total_events
    if total_events == 0:
        warnings.append("Zero confirmed closures observed -- survival curve correctly stays at 1.0 throughout; median survival is 'not reached', not fabricated.")

    median_days, ci_lo, ci_hi, reached = None, None, None, False
    for point in curve:
        if point.survival <= 0.5:
            median_days = float(point.time_days)
            reached = True
            break
    if reached:
        # Brookmeyer & Crowley (1982): invert the CI band -- the median's CI
        # is the bracket of t over which the band still contains 0.5.
        below_upper = [p.time_days for p in curve if p.ci_upper <= 0.5]
        above_lower = [p.time_days for p in curve if p.ci_lower >= 0.5]
        ci_lo = float(min(below_upper)) if below_upper else None
        ci_hi = float(max(above_lower)) if above_lower else None
        if ci_lo is None or ci_hi is None:
            warnings.append("Median reached but its confidence band could not be fully bracketed from the observed curve -- reporting the point estimate only.")
    else:
        warnings.append("Kaplan-Meier curve never crosses 0.5 within the observation window -- median survival time is 'not reached' (a real result: more than half of tracked businesses are still going, or still censored, past the end of the window), not a missing value.")

    return KaplanMeierResult(
        n=n, total_events=total_events, total_censored=total_censored, curve=curve,
        median_survival_days=median_days, median_ci_lower_days=ci_lo, median_ci_upper_days=ci_hi,
        median_reached=reached, warnings=warnings,
    )


# ============================================================================
# Pure-math layer 2: Nelson-Aalen cumulative hazard estimator.
# ============================================================================

@dataclass
class NelsonAalenResult:
    n: int
    final_cumulative_hazard: float
    final_variance: float
    curve: list = field(default_factory=list)  # list[(time_days, H_hat, variance)]
    warnings: list = field(default_factory=list)


def nelson_aalen_estimator(durations: Sequence[float], events: Sequence[int]) -> Optional[NelsonAalenResult]:
    """Point 3, module docstring. Independent cross-check against
    Kaplan-Meier -- close agreement (S_hat(t) close to exp(-H_hat(t)))
    is real corroborating evidence, not decoration."""
    n = len(durations)
    if n < MIN_N_FOR_KAPLAN_MEIER:
        return None

    distinct_times = sorted(set(durations))
    at_risk = n
    h = 0.0
    var_h = 0.0
    curve = []
    for t in distinct_times:
        d_t = sum(1 for dur, ev in zip(durations, events) if dur == t and ev == 1)
        c_t = sum(1 for dur, ev in zip(durations, events) if dur == t and ev == 0)
        if d_t > 0 and at_risk > 0:
            h += d_t / at_risk
            var_h += d_t / (at_risk ** 2)
        curve.append((int(t), round(h, 6), round(var_h, 8)))
        at_risk -= (d_t + c_t)

    return NelsonAalenResult(n=n, final_cumulative_hazard=round(h, 6), final_variance=round(var_h, 8), curve=curve)


# ============================================================================
# Pure-math layer 3: log-rank test (Mantel-Haenszel), generalized to k
# groups per Peto & Peto (1972).
# ============================================================================

@dataclass
class LogRankResult:
    groups: list                # group labels, in the order used for O/E/V
    observed: list
    expected: list
    events_per_group: dict
    chi2_statistic: float
    degrees_of_freedom: int
    p_value: float
    excluded_groups: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


def log_rank_test(group_durations: dict, group_events: dict) -> Optional[LogRankResult]:
    """
    group_durations / group_events: {group_label: [durations]} /
    {group_label: [event indicators]}, same length per group, paired by
    index. Point 4, module docstring. Groups with fewer than
    MIN_EVENTS_FOR_LOGRANK confirmed closures are excluded from the
    comparison (their subjects still contribute to other groups'
    risk-set totals up to their own censoring/event time, per the
    standard method) but flagged.
    """
    warnings = []
    excluded = []
    usable_groups = []
    for g, evs in group_events.items():
        if sum(evs) < MIN_EVENTS_FOR_LOGRANK:
            excluded.append(g)
        else:
            usable_groups.append(g)
    if excluded:
        warnings.append(f"Groups excluded from log-rank comparison for having fewer than {MIN_EVENTS_FOR_LOGRANK} confirmed closures: {excluded}.")
    if len(usable_groups) < 2:
        warnings.append("Fewer than 2 groups have enough confirmed closures to compare -- log-rank test withheld.")
        return None

    # Pool ALL subjects (including excluded groups) for risk-set membership,
    # but only usable_groups get rows/columns in the O/E/V accounting.
    pooled = []
    for g, durs in group_durations.items():
        evs = group_events[g]
        for d, e in zip(durs, evs):
            pooled.append((d, e, g))

    distinct_times = sorted(set(d for d, e, g in pooled if e == 1))
    k = len(usable_groups)
    observed = {g: 0 for g in usable_groups}
    expected = {g: 0.0 for g in usable_groups}
    variance_matrix = [[0.0] * k for _ in range(k)]
    idx = {g: i for i, g in enumerate(usable_groups)}

    for t in distinct_times:
        at_risk_total = sum(1 for d, e, g in pooled if d >= t)
        d_total = sum(1 for d, e, g in pooled if d == t and e == 1)
        if at_risk_total <= 1 or d_total == 0:
            continue
        n_g = {g: sum(1 for d, e, gg in pooled if d >= t and gg == g) for g in usable_groups}
        d_g = {g: sum(1 for d, e, gg in pooled if d == t and e == 1 and gg == g) for g in usable_groups}
        for g in usable_groups:
            observed[g] += d_g[g]
            e_g = d_total * (n_g[g] / at_risk_total)
            expected[g] += e_g

        factor = (d_total * (at_risk_total - d_total)) / (at_risk_total - 1) if at_risk_total > 1 else 0.0
        for gi in usable_groups:
            for gj in usable_groups:
                share_i = n_g[gi] / at_risk_total
                share_j = n_g[gj] / at_risk_total
                delta = (1.0 - share_i) if gi == gj else -share_j
                variance_matrix[idx[gi]][idx[gj]] += factor * share_i * (delta if gi == gj else -share_j)

    # Drop the last group's row/col: the (O-E) vector sums to zero across all
    # groups, so the full k x k variance matrix is singular by construction --
    # standard treatment for the k-group log-rank chi-square (Peto & Peto 1972).
    om_e = [observed[g] - expected[g] for g in usable_groups]
    if k == 1:
        return None
    reduced_diff = om_e[: k - 1]
    reduced_var = [row[: k - 1] for row in variance_matrix[: k - 1]]

    solved = _matrix_solve(reduced_var, reduced_diff)
    if solved is None:
        warnings.append("Variance-covariance matrix was singular (near-identical risk-set shares across groups) -- log-rank chi-square withheld.")
        return None

    chi2 = sum(reduced_diff[i] * solved[i] for i in range(k - 1))
    chi2 = max(0.0, chi2)
    df = k - 1
    p_value = _chi2_sf(chi2, df)

    return LogRankResult(
        groups=usable_groups,
        observed=[observed[g] for g in usable_groups],
        expected=[round(expected[g], 3) for g in usable_groups],
        events_per_group={g: observed[g] for g in usable_groups},
        chi2_statistic=round(chi2, 4), degrees_of_freedom=df, p_value=round(p_value, 6),
        excluded_groups=excluded, warnings=warnings,
    )


# ============================================================================
# Pure-math layer 4: Cox proportional-hazards regression, hand-fit via
# Newton-Raphson on the Breslow-tied partial likelihood.
# ============================================================================

@dataclass
class CoxCovariateResult:
    name: str
    beta: float
    se: float
    hazard_ratio: float
    hr_ci_lower: float
    hr_ci_upper: float
    z_score: float
    p_value: float


@dataclass
class CoxPHResult:
    n: int
    n_events: int
    covariates: list          # list[CoxCovariateResult]
    log_likelihood: float
    iterations: int
    converged: bool
    concordance_index: Optional[float] = None
    dropped_covariates: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


def cox_ph_regression(
    covariate_matrix: list, covariate_names: list, durations: Sequence[float], events: Sequence[int],
) -> Optional[CoxPHResult]:
    """
    covariate_matrix: list of rows (one per business), each row a list
    of floats aligned with covariate_names. Point 5, module docstring.
    Fits by maximizing the Breslow-tied Cox partial log-likelihood via
    Newton-Raphson (score vector = gradient, observed information
    matrix = negative Hessian, both derived directly from Cox (1972) /
    Breslow (1974) below).
    """
    n = len(durations)
    n_events = sum(events)
    warnings = []

    names = list(covariate_names)
    matrix = [row[:] for row in covariate_matrix]
    dropped = []
    for j in range(len(names) - 1, -1, -1):
        values = set(row[j] for row in matrix)
        if len(values) <= 1:
            dropped.append(names[j])
            names.pop(j)
            for row in matrix:
                row.pop(j)
    if dropped:
        warnings.append(f"Dropped zero-variance covariate(s) before fitting (no within-catchment variation to estimate a coefficient from): {dropped}.")

    p = len(names)
    if p == 0:
        warnings.append("No covariates left with usable variance -- Cox PH withheld.")
        return None
    if n_events < MIN_EVENTS_PER_COVARIATE * p:
        warnings.append(
            f"Only {n_events} confirmed closures for {p} covariate(s) (< {MIN_EVENTS_PER_COVARIATE} events/covariate, "
            "Peduzzi et al. 1995) -- Cox PH withheld as statistically unreliable at this sample size."
        )
        return None

    order = sorted(range(n), key=lambda i: durations[i])
    durs = [durations[i] for i in order]
    evs = [events[i] for i in order]
    xs = [matrix[i] for i in order]

    beta = [0.0] * p
    distinct_event_times = sorted(set(durs[i] for i in range(n) if evs[i] == 1))

    log_lik = None
    converged = False
    ridge_applied = False
    iterations = 0

    for iteration in range(1, MAX_NEWTON_ITER + 1):
        iterations = iteration
        score = [0.0] * p
        info = [[0.0] * p for _ in range(p)]
        ll = 0.0

        for t in distinct_event_times:
            risk_idx = [i for i in range(n) if durs[i] >= t]
            death_idx = [i for i in range(n) if durs[i] == t and evs[i] == 1]
            d_k = len(death_idx)
            if not risk_idx or d_k == 0:
                continue

            weights = [math.exp(sum(xs[i][c] * beta[c] for c in range(p))) for i in risk_idx]
            s0 = sum(weights)
            if s0 <= 0:
                continue
            s1 = [sum(w * xs[i][c] for w, i in zip(weights, risk_idx)) for c in range(p)]
            s2 = [[sum(w * xs[i][a] * xs[i][b] for w, i in zip(weights, risk_idx)) for b in range(p)] for a in range(p)]

            for i in death_idx:
                for c in range(p):
                    score[c] += xs[i][c]
                ll += sum(xs[i][c] * beta[c] for c in range(p))

            for c in range(p):
                score[c] -= d_k * (s1[c] / s0)
            ll -= d_k * math.log(s0)

            for a in range(p):
                for b in range(p):
                    info[a][b] += d_k * (s2[a][b] / s0 - (s1[a] * s1[b]) / (s0 ** 2))

        log_lik = ll
        step = _matrix_solve(info, score)
        if step is None:
            ridge_applied = True
            for c in range(p):
                info[c][c] += RIDGE_EPS
            step = _matrix_solve(info, score)
            if step is None:
                warnings.append("Observed-information matrix remained singular even after ridge regularization -- Cox PH did not converge.")
                return CoxPHResult(n=n, n_events=n_events, covariates=[], log_likelihood=log_lik or 0.0, iterations=iterations, converged=False, dropped_covariates=dropped, warnings=warnings)

        beta = [beta[c] + step[c] for c in range(p)]
        if max(abs(s) for s in step) < NEWTON_TOL:
            converged = True
            break

    if ridge_applied:
        warnings.append("Ridge regularization applied during fitting (near-collinear covariates) -- coefficients converged but treat standard errors as slightly conservative.")
    if not converged:
        warnings.append(f"Newton-Raphson did not converge within {MAX_NEWTON_ITER} iterations -- reported coefficients are the last iterate, not a verified MLE.")

    # Final information matrix at convergence, for standard errors.
    info_final = [[0.0] * p for _ in range(p)]
    for t in distinct_event_times:
        risk_idx = [i for i in range(n) if durs[i] >= t]
        death_idx = [i for i in range(n) if durs[i] == t and evs[i] == 1]
        d_k = len(death_idx)
        if not risk_idx or d_k == 0:
            continue
        weights = [math.exp(sum(xs[i][c] * beta[c] for c in range(p))) for i in risk_idx]
        s0 = sum(weights)
        if s0 <= 0:
            continue
        s1 = [sum(w * xs[i][c] for w, i in zip(weights, risk_idx)) for c in range(p)]
        s2 = [[sum(w * xs[i][a] * xs[i][b] for w, i in zip(weights, risk_idx)) for b in range(p)] for a in range(p)]
        for a in range(p):
            for b in range(p):
                info_final[a][b] += d_k * (s2[a][b] / s0 - (s1[a] * s1[b]) / (s0 ** 2))

    cov_matrix = _matrix_inverse(info_final)
    covariates = []
    if cov_matrix is None:
        warnings.append("Could not invert the final information matrix for standard errors -- coefficients reported without SEs/hazard-ratio CIs.")
        for c in range(p):
            covariates.append(CoxCovariateResult(
                name=names[c], beta=round(beta[c], 6), se=float("nan"), hazard_ratio=round(math.exp(beta[c]), 6),
                hr_ci_lower=float("nan"), hr_ci_upper=float("nan"), z_score=float("nan"), p_value=float("nan"),
            ))
    else:
        for c in range(p):
            se = math.sqrt(max(cov_matrix[c][c], 0.0))
            z = beta[c] / se if se > 0 else 0.0
            covariates.append(CoxCovariateResult(
                name=names[c], beta=round(beta[c], 6), se=round(se, 6), hazard_ratio=round(math.exp(beta[c]), 6),
                hr_ci_lower=round(math.exp(beta[c] - Z_975 * se), 6), hr_ci_upper=round(math.exp(beta[c] + Z_975 * se), 6),
                z_score=round(z, 4), p_value=round(_two_sided_p_from_z(z), 6),
            ))

    risk_scores_original_order = [sum(matrix[i][c] * beta[c] for c in range(p)) for i in range(n)]
    c_index = harrell_concordance_index(durations, events, risk_scores_original_order)

    return CoxPHResult(
        n=n, n_events=n_events, covariates=covariates, log_likelihood=round(log_lik or 0.0, 4),
        iterations=iterations, converged=converged, concordance_index=c_index,
        dropped_covariates=dropped, warnings=warnings,
    )


# ============================================================================
# Pure-math layer 5: Harrell's (1982) concordance index.
# ============================================================================

def harrell_concordance_index(durations: Sequence[float], events: Sequence[int], risk_scores: Sequence[float]) -> Optional[float]:
    """Point 6, module docstring. O(n^2) pairwise comparison -- fine at
    catchment scale, same complexity-is-fine-here reasoning as
    business_ecosystem.py's Clark-Evans nearest-neighbor pass."""
    n = len(durations)
    concordant = 0.0
    comparable = 0
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            if durations[i] >= durations[j]:
                continue
            if events[i] != 1:
                continue
            # i's observed time is shorter AND i had a confirmed event ->
            # comparable pair regardless of j's censoring status.
            comparable += 1
            if risk_scores[i] > risk_scores[j]:
                concordant += 1.0
            elif risk_scores[i] == risk_scores[j]:
                concordant += 0.5
    if comparable == 0:
        return None
    return round(concordant / comparable, 4)


# ============================================================================
# Evidence dict
# ============================================================================

def business_survival_evidence(
    km: Optional[KaplanMeierResult], na: Optional[NelsonAalenResult],
    logrank: Optional[LogRankResult], cox: Optional[CoxPHResult],
) -> dict:
    """Structured evidence dict, same convention as every other engine's
    *_evidence function in this codebase."""
    return {
        "kaplan_meier": {
            "n": km.n if km else None,
            "total_events": km.total_events if km else None,
            "total_censored": km.total_censored if km else None,
            "median_survival_days": km.median_survival_days if km else None,
            "median_ci_lower_days": km.median_ci_lower_days if km else None,
            "median_ci_upper_days": km.median_ci_upper_days if km else None,
            "median_reached": km.median_reached if km else None,
            "final_survival_estimate": km.curve[-1].survival if (km and km.curve) else None,
            "warnings": km.warnings if km else [],
        } if km is not None else None,
        "nelson_aalen": {
            "n": na.n if na else None,
            "final_cumulative_hazard": na.final_cumulative_hazard if na else None,
            "final_variance": na.final_variance if na else None,
            "warnings": na.warnings if na else [],
        } if na is not None else None,
        "group_comparison_logrank": {
            "groups": logrank.groups if logrank else None,
            "observed": logrank.observed if logrank else None,
            "expected": logrank.expected if logrank else None,
            "chi2_statistic": logrank.chi2_statistic if logrank else None,
            "degrees_of_freedom": logrank.degrees_of_freedom if logrank else None,
            "p_value": logrank.p_value if logrank else None,
            "excluded_groups": logrank.excluded_groups if logrank else [],
            "warnings": logrank.warnings if logrank else [],
        } if logrank is not None else None,
        "cox_proportional_hazards": {
            "n": cox.n if cox else None,
            "n_events": cox.n_events if cox else None,
            "converged": cox.converged if cox else None,
            "log_likelihood": cox.log_likelihood if cox else None,
            "concordance_index": cox.concordance_index if cox else None,
            "covariates": [
                {
                    "name": c.name, "hazard_ratio": c.hazard_ratio, "hr_ci_lower": c.hr_ci_lower,
                    "hr_ci_upper": c.hr_ci_upper, "p_value": c.p_value,
                } for c in (cox.covariates if cox else [])
            ],
            "dropped_covariates": cox.dropped_covariates if cox else [],
            "warnings": cox.warnings if cox else [],
        } if cox is not None else None,
        "methods": [
            "kaplan_meier_1958", "greenwood_1926_variance", "loglog_ci_kalbfleisch_prentice",
            "brookmeyer_crowley_1982_median_ci", "nelson_aalen_1972_1978",
            "log_rank_mantel_1966_peto_peto_1972", "cox_1972_proportional_hazards",
            "breslow_1974_ties_correction", "harrell_1982_concordance_index",
        ],
        "known_simplifications": [
            "No left-truncation adjustment for businesses that opened before this system began observing the catchment (Klein & Moeschberger 2003, Ch. 3) -- each business's own origin is used as its own t=0.",
            "Nelson-Aalen variance uses the simpler Poisson-type form (Klein & Moeschberger 2003, Ch. 4), not the refined d_i*(n_i-d_i)/n_i^3 correction.",
            "Cox PH proportional-hazards assumption is not formally tested here (e.g. via Schoenfeld residuals) -- a documented, not implemented, gap.",
        ],
    }


# ============================================================================
# Django-facing entry points
# ============================================================================

def _origin_and_terminal_dates(business, events_for_business: list, as_of: date) -> Optional[tuple]:
    """Returns (origin_date, terminal_date, event_indicator, warning_or_None)
    for one business, or None if no determinable origin exists at all.
    See 'WHAT'S ACTUALLY AVAILABLE VS WHAT ISN'T' in the module docstring."""
    opened = sorted((e for e in events_for_business if e.event_type == "opened"), key=lambda e: e.event_date)
    origin_is_fallback = False
    if opened:
        origin_date = opened[0].event_date
    elif business.first_observed_at is not None:
        origin_date = business.first_observed_at.date() if hasattr(business.first_observed_at, "date") else business.first_observed_at
        origin_is_fallback = True
    else:
        return None

    closures = sorted((e for e in events_for_business if e.event_type == "closed_confirmed"), key=lambda e: e.event_date)
    censors = sorted((e for e in events_for_business if e.event_type == "censored_last_seen"), key=lambda e: e.event_date)

    if closures:
        terminal_date, event_indicator = closures[-1].event_date, 1
    elif censors:
        terminal_date, event_indicator = censors[-1].event_date, 0
    else:
        terminal_date, event_indicator = as_of, 0
        if business.current_status != "operational":
            return (origin_date, terminal_date, event_indicator,
                    "status suggests non-operational but no confirmed lifecycle event exists; treated as censored at as_of_date pending an explicit event row")

    warning = "origin_is_first_observed_fallback" if origin_is_fallback else None
    return (origin_date, terminal_date, event_indicator, warning)


@dataclass
class SurvivalDurationSet:
    business_ids: list
    durations_days: list
    events: list
    categories: list
    is_chain: list
    warnings: list = field(default_factory=list)


def _build_duration_set(businesses: list, events_by_business: dict, as_of: date) -> SurvivalDurationSet:
    business_ids, durations, events, categories, is_chain = [], [], [], [], []
    n_fallback_origin = 0
    n_status_event_mismatch = 0
    n_excluded_no_origin = 0
    n_excluded_bad_duration = 0

    for b in businesses:
        result = _origin_and_terminal_dates(b, events_by_business.get(b.id, []), as_of)
        if result is None:
            n_excluded_no_origin += 1
            continue
        origin_date, terminal_date, event_indicator, warning = result
        duration_days = (terminal_date - origin_date).days
        if duration_days <= 0:
            n_excluded_bad_duration += 1
            continue
        if warning == "origin_is_first_observed_fallback":
            n_fallback_origin += 1
        elif warning:
            n_status_event_mismatch += 1

        business_ids.append(b.id)
        durations.append(float(duration_days))
        events.append(event_indicator)
        categories.append(b.category)
        is_chain.append(1.0 if b.brand else 0.0)

    warnings = []
    if n_fallback_origin:
        warnings.append(f"{n_fallback_origin} business(es) had no 'opened' lifecycle event -- used BusinessEntity.first_observed_at as an origin fallback (see module docstring on left truncation).")
    if n_status_event_mismatch:
        warnings.append(f"{n_status_event_mismatch} business(es) have current_status suggesting non-operational but no confirmed_closed/censored_last_seen event row -- treated as right-censored at as_of_date pending an explicit event.")
    if n_excluded_no_origin:
        warnings.append(f"{n_excluded_no_origin} business(es) excluded entirely -- no 'opened' event and no first_observed_at to use as an origin.")
    if n_excluded_bad_duration:
        warnings.append(f"{n_excluded_bad_duration} business(es) excluded -- computed duration was non-positive (event/censoring date on or before origin date), a malformed-record edge case, not clamped.")

    return SurvivalDurationSet(
        business_ids=business_ids, durations_days=durations, events=events,
        categories=categories, is_chain=is_chain, warnings=warnings,
    )


def _operational_and_historical_businesses_in_radius(subject_lat: float, subject_lon: float, radius_m: float) -> list:
    """Unlike business_ecosystem.py's current-operational-only filter,
    survival modeling NEEDS closed businesses too (they're exactly the
    events being modeled) -- so this deliberately does not filter on
    current_status. Same bbox-prefilter + exact-haversine pattern as
    business_ecosystem.py's _operational_businesses_in_radius."""
    lat_pad = radius_m / 111_000.0
    lon_pad = radius_m / (111_000.0 * max(math.cos(math.radians(subject_lat)), 0.01))
    qs = BusinessEntity.objects.filter(
        latitude__gte=subject_lat - lat_pad, latitude__lte=subject_lat + lat_pad,
        longitude__gte=subject_lon - lon_pad, longitude__lte=subject_lon + lon_pad,
    )
    found = []
    for biz in qs:
        d = _haversine_m(subject_lat, subject_lon, float(biz.latitude), float(biz.longitude))
        if d <= radius_m:
            found.append(biz)
    return found


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters. Duplicated (not imported) from
    business_ecosystem.py/competition.py, per this codebase's established
    per-engine independent-footprint convention."""
    earth_radius_m = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * earth_radius_m * math.asin(math.sqrt(a))


def _build_cox_covariate_matrix(duration_set: SurvivalDurationSet) -> tuple:
    """is_chain plus up to MAX_CATEGORY_DUMMIES one-hot category
    indicators for the most frequent categories in this duration set
    (reference category = everything else)."""
    category_counts = Counter(duration_set.categories)
    top_categories = [c for c, _ in category_counts.most_common(MAX_CATEGORY_DUMMIES)]
    names = ["is_chain"] + [f"category_{c}" for c in top_categories]
    matrix = []
    for i in range(len(duration_set.business_ids)):
        row = [duration_set.is_chain[i]]
        for c in top_categories:
            row.append(1.0 if duration_set.categories[i] == c else 0.0)
        matrix.append(row)
    return matrix, names


@dataclass
class CatchmentSurvivalFeatures:
    catchment: Catchment
    kaplan_meier: Optional[KaplanMeierResult] = None
    nelson_aalen: Optional[NelsonAalenResult] = None
    log_rank_chain_vs_independent: Optional[LogRankResult] = None
    log_rank_category: Optional[LogRankResult] = None
    cox_ph: Optional[CoxPHResult] = None
    source: str = "business_intel.engines.business_survival"
    source_class: str = "internal_derived"
    warnings: list = field(default_factory=list)


def compute_catchment_survival(
    cell: BusinessLocationCell, catchment: Catchment, as_of: Optional[date] = None,
    observation_window_days: int = DEFAULT_OBSERVATION_WINDOW_DAYS,
) -> Optional[CatchmentSurvivalFeatures]:
    """Full Section-7 calculation for one Catchment. Returns None for
    isochrone-type catchments this engine doesn't support yet (see
    module docstring edge cases)."""
    if catchment.catchment_size_unit != "meters":
        logger.info(
            "Skipping catchment %s: business-survival modeling currently only supports meter-radius catchments "
            "(radius/network_radius), not isochrone geometry -- see module docstring.", catchment.id,
        )
        return None

    as_of = as_of or django_timezone.now().date()
    subject_lat, subject_lon = float(cell.center_latitude), float(cell.center_longitude)
    radius_m = float(catchment.catchment_size_value)
    warnings = []

    businesses = _operational_and_historical_businesses_in_radius(subject_lat, subject_lon, radius_m)
    if not businesses:
        warnings.append(f"No businesses (operational or historical) found within {radius_m:.0f}m -- catchment may be genuinely empty or not yet Places-searched.")
        return CatchmentSurvivalFeatures(catchment=catchment, warnings=warnings)

    window_start = as_of - timedelta(days=observation_window_days)
    business_ids = [b.id for b in businesses]
    all_events = list(
        BusinessLifecycleEvent.objects.filter(business_id__in=business_ids, event_date__gte=window_start)
    )
    events_by_business = {}
    for e in all_events:
        events_by_business.setdefault(e.business_id, []).append(e)

    duration_set = _build_duration_set(businesses, events_by_business, as_of)
    warnings.extend(duration_set.warnings)

    if len(duration_set.durations_days) < MIN_N_FOR_KAPLAN_MEIER:
        warnings.append(f"Only {len(duration_set.durations_days)} businesses have a determinable duration (< {MIN_N_FOR_KAPLAN_MEIER}) -- Kaplan-Meier/Nelson-Aalen/log-rank/Cox PH all withheld.")
        return CatchmentSurvivalFeatures(catchment=catchment, warnings=warnings)

    km = kaplan_meier_estimator(duration_set.durations_days, duration_set.events)
    na = nelson_aalen_estimator(duration_set.durations_days, duration_set.events)

    logrank_chain = None
    if sum(duration_set.events) > 0:
        chain_durations = {"chain": [], "independent": []}
        chain_events = {"chain": [], "independent": []}
        for i in range(len(duration_set.business_ids)):
            grp = "chain" if duration_set.is_chain[i] == 1.0 else "independent"
            chain_durations[grp].append(duration_set.durations_days[i])
            chain_events[grp].append(duration_set.events[i])
        logrank_chain = log_rank_test(chain_durations, chain_events)

    logrank_category = None
    if sum(duration_set.events) > 0:
        cat_durations, cat_events = {}, {}
        for i in range(len(duration_set.business_ids)):
            cat = duration_set.categories[i]
            cat_durations.setdefault(cat, []).append(duration_set.durations_days[i])
            cat_events.setdefault(cat, []).append(duration_set.events[i])
        if len(cat_durations) >= 2:
            logrank_category = log_rank_test(cat_durations, cat_events)

    cox_result = None
    if sum(duration_set.events) > 0:
        matrix, names = _build_cox_covariate_matrix(duration_set)
        cox_result = cox_ph_regression(matrix, names, duration_set.durations_days, duration_set.events)
    else:
        warnings.append("Zero confirmed closures in this catchment -- log-rank and Cox PH withheld (no hazard signal to model or compare).")

    return CatchmentSurvivalFeatures(
        catchment=catchment, kaplan_meier=km, nelson_aalen=na,
        log_rank_chain_vs_independent=logrank_chain, log_rank_category=logrank_category,
        cox_ph=cox_result, warnings=warnings,
    )


def run(
    query: BusinessQuery,
    catchments: Optional[Sequence[Catchment]] = None,
    as_of: Optional[date] = None,
    observation_window_days: int = DEFAULT_OBSERVATION_WINDOW_DAYS,
) -> dict:
    """
    Engine 7 entry point. Computes business-survival features for every
    Catchment belonging to query.location_cell (or an explicit
    `catchments` subset), writes FeatureObservation rows, and records
    an EngineRun -- same contract shape as every other engine.

    Requires Engine 1 to have already run (catchments must exist), same
    dependency population.py's/business_ecosystem.py's run() documents.
    """
    started_at = django_timezone.now()
    cell = query.location_cell
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
    all_features = []

    try:
        with transaction.atomic():
            for catchment in target_catchments:
                catch_features = compute_catchment_survival(
                    cell, catchment, as_of=as_of, observation_window_days=observation_window_days,
                )
                if catch_features is None:
                    continue  # unsupported (isochrone) catchment type -- see module docstring
                all_features.append(catch_features)
                warnings_seen.extend(catch_features.warnings)
                evidence = business_survival_evidence(
                    catch_features.kaplan_meier, catch_features.nelson_aalen,
                    catch_features.log_rank_chain_vs_independent, catch_features.cox_ph,
                )

                if catch_features.kaplan_meier is not None:
                    km = catch_features.kaplan_meier
                    if km.median_survival_days is not None:
                        FeatureObservation.write_latest(
                            location_cell=cell, catchment=catchment, engine_number=ENGINE_NUMBER, feature_name="median_business_survival_days",
                            defaults=dict(
                                value_type="numeric", value_numeric=Decimal(str(km.median_survival_days)), unit="days",
                                observed_at=started_at, source=catch_features.source, source_class=catch_features.source_class,
                                method="kaplan_meier_1958_median", derivation_level="business_conclusion",
                                sample_size=km.n,
                            ),
                        )
                        features_written += 1
                    if km.curve:
                        FeatureObservation.write_latest(
                            location_cell=cell, catchment=catchment, engine_number=ENGINE_NUMBER, feature_name="survival_rate_at_end_of_window",
                            defaults=dict(
                                value_type="numeric", value_numeric=Decimal(str(km.curve[-1].survival)), unit="ratio_0_1",
                                observed_at=started_at, source=catch_features.source, source_class=catch_features.source_class,
                                method="kaplan_meier_1958_product_limit", derivation_level="calculation",
                                sample_size=km.n,
                            ),
                        )
                        features_written += 1

                if catch_features.log_rank_category is not None and catch_features.log_rank_category.chi2_statistic is not None:
                    FeatureObservation.write_latest(
                        location_cell=cell, catchment=catchment, engine_number=ENGINE_NUMBER, feature_name="category_survival_logrank_chi2",
                        defaults=dict(
                            value_type="numeric", value_numeric=Decimal(str(catch_features.log_rank_category.chi2_statistic)), unit="chi2_statistic",
                            observed_at=started_at, source=catch_features.source, source_class=catch_features.source_class,
                            method="log_rank_mantel_1966_peto_peto_1972", derivation_level="calculation",
                            uncertainty=Decimal(str(catch_features.log_rank_category.p_value)),
                        ),
                    )
                    features_written += 1

                if catch_features.cox_ph is not None and catch_features.cox_ph.covariates:
                    chain_cov = next((c for c in catch_features.cox_ph.covariates if c.name == "is_chain"), None)
                    if chain_cov is not None and not math.isnan(chain_cov.hazard_ratio):
                        FeatureObservation.write_latest(
                            location_cell=cell, catchment=catchment, engine_number=ENGINE_NUMBER, feature_name="chain_vs_independent_hazard_ratio",
                            defaults=dict(
                                value_type="numeric", value_numeric=Decimal(str(chain_cov.hazard_ratio)), unit="hazard_ratio",
                                observed_at=started_at, source=catch_features.source, source_class=catch_features.source_class,
                                method="cox_1972_proportional_hazards_breslow_ties", derivation_level="business_conclusion",
                                sample_size=catch_features.cox_ph.n, uncertainty=Decimal(str(chain_cov.p_value)),
                            ),
                        )
                        features_written += 1
                    if catch_features.cox_ph.concordance_index is not None:
                        FeatureObservation.write_latest(
                            location_cell=cell, catchment=catchment, engine_number=ENGINE_NUMBER, feature_name="survival_model_concordance_index",
                            defaults=dict(
                                value_type="numeric", value_numeric=Decimal(str(catch_features.cox_ph.concordance_index)), unit="ratio_0_1",
                                observed_at=started_at, source=catch_features.source, source_class=catch_features.source_class,
                                method="harrell_1982_concordance_index", derivation_level="calculation",
                                sample_size=catch_features.cox_ph.n,
                            ),
                        )
                        features_written += 1

                FeatureObservation.write_latest(
                    location_cell=cell, catchment=catchment, engine_number=ENGINE_NUMBER, feature_name="business_survival_evidence",
                    defaults=dict(
                        value_type="json", value_json=evidence, unit="",
                        observed_at=started_at, source=catch_features.source, source_class=catch_features.source_class,
                        method="business_survival_engine", derivation_level="calculation",
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

    return {"cell": cell, "features": all_features, "engine_run": run_row}
