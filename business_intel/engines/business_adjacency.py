"""
business_intel/engines/business_adjacency.py

Engine 10 — Business Adjacency Engine
Per business_location_intelligence_engines_data_models_v2.md, Section 10.

NOTE ON THE SPEC FILE: same situation as every other engine in this
codebase -- business_location_intelligence_engines_data_models_v2.md is
referenced but not present on disk. Built to match Engine 6/7/8/9 for
structure, schema contract, and citation discipline. The stub this
replaced gave only a purpose line: "Discover which business categories
naturally co-locate and identify missing complementary businesses."

=============================================================================
WHY THIS IS A REAL STATISTICAL/ML ENGINE, NOT A CO-OCCURRENCE COUNT
=============================================================================
The naive version of "business adjacency" is a raw co-occurrence tally --
"cafes and bookshops appear together in N catchments" -- with no notion of
whether that's more than chance, no way to separate "in the same catchment"
from "actually near each other," and no principled way to rank which
absent category is worth recommending. This engine instead treats
adjacency as THREE separate, real, cross-checked statistical questions:

  1. CORPUS-LEVEL ASSOCIATION -- across every real catchment this system
     has data for, which category PAIRS co-occur more (or less) often
     than chance, and is that association statistically distinguishable
     from noise once you account for testing many pairs at once? This is
     literal market-basket analysis (Agrawal, Imielinski & Swami 1993),
     scored with lift/PMI (Church & Hanks 1990; Brin, Motwani &
     Silverstein 1997) and tested with a chi-square test of independence
     (Yates 1934) under Benjamini-Hochberg FDR control (1995) -- not an
     eyeballed ratio.

  2. LEARNED CATEGORY AFFINITY -- an independent, second view of the same
     underlying signal via item-based collaborative filtering (Sarwar,
     Karypis, Konstan & Riedl 2001; Deshpande & Karypis 2004): cosine
     similarity between categories' catchment-incidence vectors, the same
     mathematics recommender systems use to say "customers who bought X
     also bought Y," applied here to "catchments that contain X also tend
     to contain Y." Reported alongside lift/PMI, not instead of it -- two
     independently-derived affinity signals that should broadly agree if
     the underlying association is real, same "two independent
     estimators, cross-checked" convention business_ecosystem.py uses for
     Shannon vs. Simpson diversity and business_survival.py uses for
     Kaplan-Meier vs. Nelson-Aalen.

  3. FINE-GRAINED SPATIAL VALIDATION -- corpus-level co-occurrence only
     proves two categories tend to appear in the SAME catchment; it says
     nothing about whether they sit near each other within it (a 10km
     radius catchment containing both a mall-district cafe and a
     industrial-zone hardware store "co-occurs" but isn't a meaningful
     adjacency). This engine runs a genuine bivariate spatial statistic --
     a Besag & Diggle (1977) Monte Carlo permutation test on the
     cross-category nearest-neighbor distance -- on the SUBJECT
     catchment's own point locations, for the pairs corpus-level lift
     flags as worth checking. This is the direct bivariate analog of the
     univariate Clark-Evans test business_ecosystem.py already runs for
     single-category clustering.

"Missing complementary business" recommendations are then not a guess --
they're categories absent from this catchment whose combined statistical
evidence (Fisher's (1925) method for combining the p-values of every
present-category pairing) is significant, ranked by a score blending the
two independent affinity signals from point 1/2 above.

=============================================================================
WHAT'S ACTUALLY AVAILABLE VS. WHAT ISN'T
=============================================================================
Real data used: models.BusinessEntity (category, brand, status, lat/lon)
via the same operational-business retrieval pattern business_ecosystem.py
already established, across every real meter-radius Catchment in the DB
(radius/network_radius; isochrone catchments are skipped -- see edge
cases). No external API calls in this engine at all -- purely a real-data
read across this codebase's own accumulated BusinessEntity history, same
DB-only scope as Engine 6/7.

What is explicitly an approximation, flagged as such:
  - The citywide corpus (every Catchment currently in the DB) is this
    system's OWN accumulated business records, not an authoritative
    external census of business co-location (no such thing is published
    for Kenya at this granularity) -- same "within-corpus reference,
    documented as approximation" convention market_gap.py's
    corpus_rate_per_km2 and business_ecosystem.py's Location Quotient
    reference region already use. Reliable only once enough catchments
    have actually been Places-synced; MIN_CORPUS_CATCHMENTS /
    MIN_CATEGORY_FREQUENCY below flag when they haven't.
  - Fisher's combined-probability method assumes the k pairwise p-values
    being combined are independent; here they share the same corpus and
    the same candidate category, so they are not strictly independent.
    This is a documented approximation, not a hidden one -- same "real
    but approximate, flagged" convention as Fay-Herriot's between-area
    variance in population.py.
  - The Besag-Diggle permutation test's null (random relabeling of a
    FIXED set of point locations) validates "are these two categories
    closer together than the other businesses already here, rearranged
    at random" -- it does not, and cannot, say anything about locations
    with no businesses at all yet. It is a within-catchment spatial test,
    not a market-gap test (that is Engine 9's job).

=============================================================================
REAL METHODS USED (researched and cited, not invented)
=============================================================================
1. Market-basket association rules: support, confidence
   Agrawal, R., Imielinski, T., Swami, A. (1993). "Mining Association
   Rules Between Sets of Items in Large Databases." Proceedings of the
   1993 ACM SIGMOD International Conference on Management of Data,
   207-216.
   Each real meter-radius Catchment is treated as a "transaction" (a
   basket) containing the set of business categories operating within
   it. support(A) = fraction of catchments containing category A;
   support(A,B) = fraction containing both; confidence(A->B) =
   support(A,B)/support(A). This is the exact, unmodified market-basket
   formalism, applied to catchments-as-baskets rather than
   shopping-carts-as-baskets.

2. Lift, pointwise mutual information (PMI), and the chi-square
   significance test for association rules
   Church, K.W., Hanks, P. (1990). "Word Association Norms, Mutual
   Information, and Lexicography." Computational Linguistics, 16(1),
   22-29. (PMI(A,B) = log(P(A,B) / (P(A)*P(B))) -- lift is exp(PMI); the
   same statistic, two names from two different literatures.)
   Brin, S., Motwani, R., Silverstein, C. (1997). "Beyond Market
   Baskets: Generalizing Association Rules to Correlations." ACM SIGMOD
   Record, 26(2), 265-276. (Source of "lift"/"interest" as a
   significance-aware alternative to raw support/confidence, and of
   using a chi-square test of independence on the pair's 2x2
   contingency table as the actual significance test -- not just a
   large lift value taken on faith.)
   Yates, F. (1934). "Contingency Table Involving Small Numbers and the
   chi^2 Test." Supplement to the Journal of the Royal Statistical
   Society, 1(2), 217-235. (Continuity correction applied here because
   catchment corpora are typically dozens to a few hundred rows --
   exactly the small-expected-cell-count regime Yates' correction is
   for.)

3. Benjamini-Hochberg false discovery rate control
   Benjamini, Y., Hochberg, Y. (1995). "Controlling the False Discovery
   Rate: A Practical and Powerful Approach to Multiple Testing." Journal
   of the Royal Statistical Society, Series B, 57(1), 289-300.
   With k business categories this engine tests O(k^2) pairs
   simultaneously (dozens of categories -> hundreds of pairs) -- flagging
   every pair with raw p<0.05 as "significant" would produce a large
   number of false adjacency claims by chance alone. Every pairwise
   p-value is corrected via the BH step-up procedure before a pair is
   marked significant_fdr.

4. Item-based collaborative filtering (cosine similarity)
   Sarwar, B., Karypis, G., Konstan, J., Riedl, J. (2001). "Item-Based
   Collaborative Filtering Recommendation Algorithms." Proceedings of
   the 10th International Conference on World Wide Web (WWW '01),
   285-295.
   Deshpande, M., Karypis, G. (2004). "Item-Based Top-N Recommendation
   Algorithms." ACM Transactions on Information Systems, 22(1), 143-177.
   Each business category's "profile" is its incidence vector across
   every catchment in the corpus (1 if present, 0 if absent); category-
   category cosine similarity is the exact same computation these papers
   use for item-item similarity in recommender systems, substituting
   "catchment" for "user/transaction." This is a genuinely independent
   affinity signal from lift (which is normalized by marginal
   frequencies; cosine similarity is not), not a duplicate of it.

5. Fisher's method for combining independent p-values
   Fisher, R.A. (1925). Statistical Methods for Research Workers.
   Oliver & Boyd, Edinburgh, Section 21.1.
   X^2 = -2 * sum(ln(p_i)) ~ chi-square(2k) under the joint null that all
   k component tests are null. Used to combine, for one candidate absent
   category, the pairwise significance of its association with EVERY
   category actually present in this catchment into one overall
   significance figure -- so a candidate can be recommended on the
   strength of consistent weak-to-moderate evidence across many present
   categories, not just one outlier pair.

6. Besag & Diggle (1977) Monte Carlo permutation test for bivariate
   spatial association
   Besag, J., Diggle, P.J. (1977). "Simple Monte Carlo Tests for Spatial
   Pattern." Applied Statistics, 26(3), 327-333.
   Diggle, P.J. (2003). Statistical Analysis of Spatial Point Patterns,
   2nd ed., Arnold, Ch. 2 & 6. (General cross-nearest-neighbor / cross-G
   function framework this test is a summary statistic of.)
   Statistic: mean, over every category-A point in the subject
   catchment, of the distance to the nearest category-B point. Under the
   null that the A/B labels are exchangeable given the fixed combined
   point locations, this statistic's null distribution is generated
   empirically by randomly relabeling the same points many times --
   exact under that null, and (per Besag & Diggle's own point) valid
   however irregular the real point pattern is, unlike a closed-form
   test that assumes an underlying CSR process. This is the direct
   bivariate analog of the univariate Clark-Evans test
   business_ecosystem.py already runs.

=============================================================================
COMPOSITE SCORE: WHY LIFT AND COSINE SIMILARITY ARE BLENDED BUT SPATIAL
VALIDATION AND RAW CORPUS SIZE ARE NOT
=============================================================================
Lift and cosine similarity both have an unambiguous "more is stronger
evidence of positive association" direction, so they're geometric-mean
blended into recommendation_score for missing-complement candidates
(same "unambiguous direction gets blended" principle business_ecosystem.py
uses for diversity/growth). Spatial co-location test results do NOT get
folded into that score: a pair can have strong corpus-level lift while
this SPECIFIC catchment's spatial test comes back "not distinguishable
from random" (or even "dispersed") simply because this catchment's
instance of the two categories happens to sit at opposite ends of a large
radius -- that is a genuinely catchment-specific fact, not something that
should silently override or get averaged into the citywide affinity
signal. Both are reported in full, consistent with this codebase's
standing rule that ambiguous or scope-mismatched statistics stay
descriptive evidence rather than getting force-blended into one score
(see mobility_traffic.py's temporal_accessibility_volatility and
business_ecosystem.py's Location-Quotient/Clark-Evans-sign exclusions for
the same principle applied elsewhere).

=============================================================================
EDGE CASES THIS MODULE HANDLES EXPLICITLY
=============================================================================
- Zero businesses in the catchment -> every component is None/empty, not
  a zero-diversity or zero-adjacency reading (an unsynced/never-searched
  catchment is a data gap, same principle as business_ecosystem.py and
  BusinessLocationCell.has_category_data's documented distinction).
- Fewer than MIN_CORPUS_CATCHMENTS real catchments in the DB, or fewer
  than 2 categories meeting MIN_CATEGORY_FREQUENCY corpus-wide -> the
  corpus-level GLM-equivalent (association/affinity fitting) is not
  attempted; only the subject catchment's raw category composition is
  reported, flagged rather than silently fit on an unstable corpus.
- A category present in every catchment or absent from all of them (zero
  marginal variance) -> its 2x2 contingency table with any partner
  category is degenerate (a zero row/column); chi_square_association
  returns (0.0, 1.0) -- correctly "not significant" -- rather than
  dividing by zero.
- A category pair that never co-occurs anywhere in the corpus -> lift/PMI
  are reported at their mathematically degenerate floor (lift=0,
  PMI -> -inf, stored as None rather than a non-JSON-serializable -inf),
  flagged, not silently omitted.
- Fewer than MIN_N_FOR_CROSS_COLOCATION_TEST businesses of either category
  within the SUBJECT catchment specifically -> the Besag-Diggle spatial
  test is withheld for that pair; corpus-level lift/cosine similarity are
  still reported (they come from the wider corpus, not this catchment's
  own point count).
- More than MAX_N_FOR_CROSS_COLOCATION_TEST points of either category in
  one catchment -> the O(n^2) permutation test is skipped for that pair
  (flagged, not silently truncated) to bound worst-case runtime; corpus-
  level statistics are unaffected.
- Co-located businesses (distance = 0m, e.g. multiple units sharing one
  Places pin from a mall/plaza) -> real, not an error; pulls the observed
  cross-nearest-neighbor statistic toward 0 (genuine near-total
  co-location), flagged so a surprisingly strong "clustered" reading can
  be sanity-checked against duplicate records, same convention
  business_ecosystem.py's own Clark-Evans co-location flag uses.
- Subject catchment already contains every category present anywhere in
  the corpus -> missing_complements is an empty list (nothing to
  recommend), reported as such rather than raising.
- A candidate absent category with zero corpus-level co-occurrence with
  ANY category present in this catchment -> excluded from
  missing_complements (there is no affinity evidence at all to rank it
  on, not even a weak one).
- Catchment is an isochrone type (minutes-based, no fixed meter radius)
  -> business-mix retrieval here only supports meter-radius catchments,
  same documented, unimplemented gap business_ecosystem.py and
  business_survival.py already flag; skipped with an explicit log line.

Scope boundary
--------------
Depends on business_intel.models only (BusinessEntity,
BusinessLocationCell, Catchment, BusinessQuery, EngineRun,
FeatureObservation). No dependency on any other engine's feature-store
output or any external API -- purely a real-data read across this
codebase's own accumulated BusinessEntity history, same "genuinely
independent" DB-only scope as Engine 6/7. Engine 9 (Market Gap) asks "is
there room for another player at all, given demand"; this engine asks "if
category A is here, which OTHER category tends to belong here too" --
related but deliberately separate questions, same numbering-driven scope
split the codebase already documents between Engine 8 (Competition) and
Engine 9.
"""

from __future__ import annotations

import logging
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from itertools import combinations
from typing import Optional, Sequence

import numpy as np
from django.db import transaction
from django.utils import timezone as django_timezone

from ..models import (
    BusinessEntity, BusinessLocationCell, Catchment, BusinessQuery,
    EngineRun, FeatureObservation,
)

logger = logging.getLogger("business_intel.engines.business_adjacency")

ENGINE_NUMBER = 10

EARTH_RADIUS_M = 6_371_000.0

# ============================================================================
# Calibration inputs -- documented as such, not measured constants.
# ============================================================================

# A category must appear in at least this many corpus-wide catchments to
# be included in affinity fitting at all -- below this, its own marginal
# support estimate is too noisy for lift/PMI/cosine similarity built on
# top of it to mean anything (same spirit as market_gap.py's
# MIN_GLM_FIT_SAMPLE floor, applied to a marginal count rather than a
# regression sample size).
MIN_CATEGORY_FREQUENCY = 5

# Below this many usable catchments corpus-wide, no association/affinity
# statistics are fit at all -- same floor market_gap.py enforces
# (MIN_GLM_FIT_SAMPLE=20) for its own gravity-model fit, reused here since
# this corpus plays the analogous role.
MIN_CORPUS_CATCHMENTS = 20

# Below this many businesses of EITHER category within the specific
# subject catchment, the Besag-Diggle cross-colocation permutation test
# is withheld -- a cross-nearest-neighbor statistic needs enough points on
# both sides to be meaningful, same principle as business_ecosystem.py's
# MIN_N_FOR_CLARK_EVANS floor for its univariate analog.
MIN_N_FOR_CROSS_COLOCATION_TEST = 4

# Above this many points of either category in one catchment, the exact
# O(n^2) permutation test is skipped for that pair rather than silently
# eating a large amount of runtime on an unusually dense catchment.
MAX_N_FOR_CROSS_COLOCATION_TEST = 300

# Number of Monte Carlo permutations for the Besag-Diggle test.
# Besag & Diggle's own convention: with N permutations plus the one
# observed statistic, an exact permutation p-value has resolution
# 1/(N+1); 999 gives a minimum resolvable p-value of 0.001, fine
# granularity for a 0.05 decision threshold without an excessive
# permutation budget.
N_PERMUTATIONS = 999

# Deterministic seed so re-running this engine for the same query
# reproduces the same permutation p-values -- EngineRun idempotency
# matters here the same way it does for every other engine's
# update_or_create/write_latest writes.
RANDOM_SEED = 1337

# Benjamini-Hochberg FDR level. 0.10 (rather than the stricter 0.05) is a
# deliberate choice given this is an exploratory "worth investigating"
# recommendation engine, not a confirmatory clinical test -- a
# documented calibration input, not derived from data.
FDR_ALPHA = 0.10

# A pair's lift must clear this floor to count as "supporting evidence"
# for a missing-complement recommendation (lift=1.0 is the independence
# baseline; 1.2 requires a real, if modest, positive association before
# a present category counts toward a candidate's case).
LIFT_INTEREST_THRESHOLD = 1.2

# How many of the highest-corpus-lift co-present pairs get spent on the
# expensive spatial permutation test per catchment -- bounds worst-case
# per-catchment runtime regardless of how many categories are present.
MAX_SPATIAL_TESTS_PER_CATCHMENT = 8

TOP_N_RECOMMENDATIONS = 10


# ============================================================================
# Numerically-independent helpers (erf-based normal CDF, incomplete-
# gamma-based chi-square survival function, vectorized haversine). No
# scipy/numpy-stats in this environment -- duplicated rather than
# imported from business_survival.py per this codebase's established
# per-engine independent-footprint convention (see business_ecosystem.py
# / business_survival.py's own notes on why haversine_m is duplicated,
# not shared, across engines).
# ============================================================================

def _log_gamma(x: float) -> float:
    """Lanczos approximation to ln(Gamma(x)), standard coefficients
    (g=7, n=9). Used only to build the regularized incomplete gamma
    function below, itself used only to get chi-square p-values without
    scipy -- identical implementation to business_survival.py's, kept
    independent per this codebase's per-engine footprint convention."""
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
    """Regularized upper incomplete gamma Q(a, x) = 1 - P(a, x). Series
    form for x < a+1, continued-fraction expansion (Abramowitz & Stegun
    1964, Sec. 6.5) for x >= a+1."""
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


def _chi2_sf(x: float, df: float) -> float:
    """P(X > x) for a chi-square random variable with df degrees of
    freedom -- the p-value for a chi-square test statistic. df may be
    fractional here (Fisher's combined test uses df=2k for integer k, so
    df is always an even integer in this module's own calls, but the
    underlying incomplete-gamma implementation itself has no such
    restriction)."""
    if x <= 0:
        return 1.0
    return max(0.0, min(1.0, _gammainc_reg_upper(df / 2.0, x / 2.0)))


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters, scalar form. Duplicated (not
    imported) from business_ecosystem.py/business_survival.py's
    identical function, per this codebase's own established convention
    of keeping each engine's dependency footprint independent."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def _haversine_matrix(lats: np.ndarray, lons: np.ndarray) -> np.ndarray:
    """Full pairwise great-circle distance matrix (meters) for a set of
    (lat, lon) points, vectorized. Computed ONCE per catchment's spatial
    test (not once per permutation) -- every permutation then only needs
    to re-index this fixed matrix by the permuted label assignment, which
    is what makes running N_PERMUTATIONS=999 relabelings per pair
    computationally trivial at the catchment scale (business_ecosystem.py
    documents typical catchments as "well under a few hundred real
    businesses")."""
    phi1 = np.radians(lats)[:, None]
    phi2 = np.radians(lats)[None, :]
    dphi = np.radians(lats[None, :] - lats[:, None])
    dlambda = np.radians(lons[None, :] - lons[:, None])
    a = np.sin(dphi / 2.0) ** 2 + np.cos(phi1) * np.cos(phi2) * np.sin(dlambda / 2.0) ** 2
    a = np.clip(a, 0.0, 1.0)  # guard asin domain against floating-point overshoot at a~1
    return 2.0 * EARTH_RADIUS_M * np.arcsin(np.sqrt(a))


# ============================================================================
# Pure-statistics layer 1: 2x2 contingency chi-square test (Yates
# corrected), Benjamini-Hochberg FDR, Fisher's combined-probability test.
# No DB access below this point -- every function here takes plain
# numbers, independently unit-testable, same pattern as every other
# engine's pure-math layer.
# ============================================================================

def chi_square_association(n11: int, n10: int, n01: int, n00: int) -> tuple:
    """
    Pearson chi-square test of independence for a 2x2 contingency table,
    with Yates' (1934) continuity correction -- the standard correction
    for small expected cell counts, which is the typical regime here
    (catchment corpora are usually dozens to a few hundred rows, not
    thousands). n11 = catchments with both categories, n10 = with A only,
    n01 = with B only, n00 = with neither. Returns (chi2_statistic,
    p_value). Degenerate tables (a zero row or column -- e.g. a category
    present in literally every catchment) correctly return (0.0, 1.0):
    there is no variability in that margin to test association against.
    """
    n = n11 + n10 + n01 + n00
    if n == 0:
        return 0.0, 1.0
    row1, row0 = n11 + n10, n01 + n00
    col1, col0 = n11 + n01, n10 + n00
    denom = row1 * row0 * col1 * col0
    if denom == 0:
        return 0.0, 1.0
    numerator = n * (max(abs(n11 * n00 - n10 * n01) - n / 2.0, 0.0)) ** 2
    chi2 = numerator / denom
    p = _chi2_sf(chi2, df=1)
    return round(chi2, 6), round(p, 8)


def benjamini_hochberg(p_values: Sequence[float], alpha: float = FDR_ALPHA) -> list:
    """
    Benjamini-Hochberg (1995) step-up false-discovery-rate procedure.
    Returns a boolean list (same order/length as input) marking which
    hypotheses are rejected (declared significant) at FDR level alpha.
    Essential here because this engine tests O(k^2) category pairs
    simultaneously -- with, say, 25 categories that's 300 simultaneous
    tests; reporting raw p<0.05 for every one without a multiple-testing
    correction would manufacture a large number of false "adjacency"
    claims purely from running that many tests.
    """
    m = len(p_values)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: p_values[i])
    threshold_rank = 0
    for rank, idx in enumerate(order, start=1):
        if p_values[idx] <= (rank / m) * alpha:
            threshold_rank = rank
    reject = [False] * m
    for rank, idx in enumerate(order, start=1):
        if rank <= threshold_rank:
            reject[idx] = True
    return reject


def fisher_combined_p(p_values: Sequence[float]) -> Optional[float]:
    """
    Fisher's (1925) method for combining independent p-values:
    X^2 = -2 * sum(ln(p_i)) ~ chi-square(2k) under the joint null that
    ALL k component tests are null. Used to ask, for one candidate
    (currently-absent) category: across every category actually present
    in the subject catchment, is the candidate's association with the
    catchment's OVERALL profile collectively significant, even if no
    single pairwise p-value alone clears a strict threshold? The
    independence assumption is a documented approximation (the pairwise
    tests share the same corpus and candidate, so are not strictly
    independent) -- same "real but approximate, flagged" convention this
    codebase uses for Fay-Herriot's between-area variance.
    """
    ps = [max(min(p, 1.0), 1e-300) for p in p_values if p is not None]
    k = len(ps)
    if k == 0:
        return None
    stat = -2.0 * sum(math.log(p) for p in ps)
    return round(_chi2_sf(stat, df=2 * k), 8)


# ============================================================================
# Pure-statistics layer 2: market-basket corpus construction, lift/PMI/
# cosine similarity per category pair.
# ============================================================================

@dataclass
class AdjacencyCorpus:
    """Citywide (corpus-wide) category co-occurrence statistics, fit once
    across every real meter-radius Catchment currently in the DB. Same
    live-corpus-per-run pattern as market_gap.py's
    _fit_corpus_for_category / purchasing_power.py's PCA fit -- caching a
    periodically-refit corpus is real separate infra work, intentionally
    left as a caller's responsibility, not faked here."""
    n_catchments: int
    category_counts: dict                    # category -> n catchments containing it
    pair_incidence: dict                     # frozenset({A, B}) -> n catchments containing both
    category_vectors: dict                   # category -> np.ndarray incidence vector, length n_catchments
    included_categories: list
    excluded_low_frequency: list
    warnings: list = field(default_factory=list)


def build_adjacency_corpus(catchment_category_sets: Sequence[frozenset]) -> Optional[AdjacencyCorpus]:
    """
    Builds the market-basket corpus from a sequence of "baskets" -- one
    frozenset of business categories per real catchment. Returns None
    (not an exception) if there are fewer than MIN_CORPUS_CATCHMENTS
    usable catchments, or fewer than 2 categories meeting
    MIN_CATEGORY_FREQUENCY -- caller falls back to reporting only the
    subject catchment's own raw composition (see module docstring, edge
    cases).
    """
    n = len(catchment_category_sets)
    if n < MIN_CORPUS_CATCHMENTS:
        return None

    warnings = []
    counts = Counter()
    for basket in catchment_category_sets:
        for category in basket:
            counts[category] += 1

    included = sorted(c for c, cnt in counts.items() if cnt >= MIN_CATEGORY_FREQUENCY)
    excluded = sorted(c for c, cnt in counts.items() if cnt < MIN_CATEGORY_FREQUENCY)
    if excluded:
        preview = excluded[:10]
        suffix = ", ..." if len(excluded) > 10 else ""
        warnings.append(
            f"{len(excluded)} categories appear in fewer than {MIN_CATEGORY_FREQUENCY} catchments "
            f"corpus-wide; excluded from affinity fitting as too sparse to estimate reliably: "
            f"{', '.join(preview)}{suffix}."
        )
    if len(included) < 2:
        return None

    category_vectors = {category: np.zeros(n) for category in included}
    for i, basket in enumerate(catchment_category_sets):
        for category in basket:
            if category in category_vectors:
                category_vectors[category][i] = 1.0

    pair_incidence = {}
    for cat_a, cat_b in combinations(included, 2):
        both = int(np.sum(category_vectors[cat_a] * category_vectors[cat_b]))
        pair_incidence[frozenset((cat_a, cat_b))] = both

    return AdjacencyCorpus(
        n_catchments=n,
        category_counts={c: counts[c] for c in included},
        pair_incidence=pair_incidence,
        category_vectors=category_vectors,
        included_categories=included,
        excluded_low_frequency=excluded,
        warnings=warnings,
    )


@dataclass
class PairAssociation:
    """One category pair's full corpus-level association profile --
    market-basket support/confidence (point 1), lift/PMI with a real
    significance test (point 2), and item-based-CF cosine similarity
    (point 4) -- three independently-motivated statistics on the same
    underlying co-occurrence data, per module docstring."""
    category_a: str
    category_b: str
    support_a: float
    support_b: float
    support_ab: float
    confidence_a_to_b: float
    confidence_b_to_a: float
    lift: Optional[float]
    pmi: Optional[float]
    cosine_similarity: float
    chi2_statistic: float
    p_value: float
    significant_fdr: bool = False
    n_catchments_with_a: int = 0
    n_catchments_with_b: int = 0
    n_catchments_with_both: int = 0
    warnings: list = field(default_factory=list)


def compute_pair_association(corpus: AdjacencyCorpus, cat_a: str, cat_b: str) -> Optional[PairAssociation]:
    """Full association profile for one category pair against a fitted
    AdjacencyCorpus. Returns None if either category isn't in the
    corpus's included set (too sparse -- see build_adjacency_corpus) or
    if cat_a == cat_b (no self-pairs)."""
    if cat_a == cat_b:
        return None
    if cat_a not in corpus.category_vectors or cat_b not in corpus.category_vectors:
        return None

    n = corpus.n_catchments
    n_a = corpus.category_counts[cat_a]
    n_b = corpus.category_counts[cat_b]
    n_ab = corpus.pair_incidence.get(frozenset((cat_a, cat_b)), 0)

    support_a = n_a / n
    support_b = n_b / n
    support_ab = n_ab / n
    confidence_ab = (n_ab / n_a) if n_a else 0.0
    confidence_ba = (n_ab / n_b) if n_b else 0.0

    expected = support_a * support_b
    lift = (support_ab / expected) if expected > 0 else None
    pmi = None
    if lift is not None and lift > 0:
        pmi = math.log(lift)
    # lift == 0 (never co-occur) is a real, meaningful floor -- PMI's
    # true value there is -infinity, which is not JSON-serializable and
    # not usefully distinguishable from "a very strong negative
    # association" at this sample size; stored as None (see warning)
    # rather than a fabricated large negative number.

    va, vb = corpus.category_vectors[cat_a], corpus.category_vectors[cat_b]
    denom = float(np.linalg.norm(va) * np.linalg.norm(vb))
    cosine = float(np.dot(va, vb) / denom) if denom > 0 else 0.0

    n10 = n_a - n_ab
    n01 = n_b - n_ab
    n00 = n - n_a - n_b + n_ab
    chi2, p = chi_square_association(n_ab, n10, n01, n00)

    warnings = []
    if n_ab == 0:
        warnings.append(
            "Never co-occur in the current corpus -- lift/PMI are reported at their degenerate "
            "floor (lift=0, PMI withheld as None rather than -inf), not evidence of a strong real "
            "negative association given typical corpus sample sizes."
        )

    return PairAssociation(
        category_a=cat_a, category_b=cat_b,
        support_a=round(support_a, 4), support_b=round(support_b, 4), support_ab=round(support_ab, 4),
        confidence_a_to_b=round(confidence_ab, 4), confidence_b_to_a=round(confidence_ba, 4),
        lift=round(lift, 4) if lift is not None else None,
        pmi=round(pmi, 4) if pmi is not None else None,
        cosine_similarity=round(cosine, 4),
        chi2_statistic=chi2, p_value=p,
        n_catchments_with_a=n_a, n_catchments_with_b=n_b, n_catchments_with_both=n_ab,
        warnings=warnings,
    )


def compute_all_pair_associations(corpus: AdjacencyCorpus) -> list:
    """Every category pair in the corpus's included set, each scored via
    compute_pair_association, with Benjamini-Hochberg FDR correction
    applied across the FULL set of simultaneous tests before any pair is
    marked significant_fdr (point 3, module docstring)."""
    pairs = []
    for cat_a, cat_b in combinations(corpus.included_categories, 2):
        assoc = compute_pair_association(corpus, cat_a, cat_b)
        if assoc is not None:
            pairs.append(assoc)

    p_values = [p.p_value for p in pairs]
    rejections = benjamini_hochberg(p_values, FDR_ALPHA)
    for assoc, is_significant in zip(pairs, rejections):
        assoc.significant_fdr = is_significant
    return pairs


# ============================================================================
# Pure-statistics layer 3: Besag & Diggle (1977) Monte Carlo permutation
# test for bivariate spatial co-location.
# ============================================================================

@dataclass
class CrossColocationResult:
    category_a: str
    category_b: str
    n_a: int
    n_b: int
    observed_mean_cross_nn_distance_m: float
    permuted_mean_distance_m: float
    permuted_std_distance_m: float
    p_value: float
    n_permutations: int
    interpretation: str  # "spatially_clustered" | "spatially_dispersed" | "not_distinguishable_from_random"
    co_located_pairs: int
    warnings: list = field(default_factory=list)


def cross_category_colocation_test(
    points_a: Sequence[tuple],
    points_b: Sequence[tuple],
    rng: "np.random.Generator",
    n_permutations: int = N_PERMUTATIONS,
) -> Optional[CrossColocationResult]:
    """
    Besag & Diggle (1977) Monte Carlo label-permutation test for
    bivariate spatial association, applied to THIS catchment's own
    category-A and category-B point locations. See module docstring,
    point 6, for the full citation and rationale.

    Statistic T = mean, over every category-A point, of the great-circle
    distance to the nearest category-B point (a real cross-nearest-
    neighbor summary of the bivariate point pattern; Diggle 2003, Ch. 2
    & 6). Under the null that the A/B labels are exchangeable given the
    fixed combined set of point locations, T's null distribution is
    built empirically: randomly reassign the A/B labels among the same
    combined points n_permutations times, recomputing T each time. This
    needs no closed-form variance formula and is EXACT under the stated
    null regardless of how irregular the real point pattern is -- Besag
    & Diggle's own point in proposing it.

    A smaller-than-random observed T is evidence category A tends to sit
    closer to category B than chance (positive spatial co-location); a
    larger-than-random T is evidence of spatial avoidance/dispersion.
    The two-sided decision (matching Clark & Evans' two-tailed
    convention in business_ecosystem.py) is reported via whichever tail
    the observed statistic falls into, using the standard exact
    Monte-Carlo p-value construction (rank the observed value among the
    N permuted values plus itself, giving resolution 1/(N+1)).
    """
    n_a, n_b = len(points_a), len(points_b)
    if n_a < MIN_N_FOR_CROSS_COLOCATION_TEST or n_b < MIN_N_FOR_CROSS_COLOCATION_TEST:
        return None
    if n_a > MAX_N_FOR_CROSS_COLOCATION_TEST or n_b > MAX_N_FOR_CROSS_COLOCATION_TEST:
        return None

    warnings = []
    combined = list(points_a) + list(points_b)
    n_total = len(combined)
    lats = np.array([p[0] for p in combined], dtype=float)
    lons = np.array([p[1] for p in combined], dtype=float)

    dist = _haversine_matrix(lats, lons)
    np.fill_diagonal(dist, np.inf)  # a point is never its own nearest neighbor

    def _mean_cross_nn(idx_a: np.ndarray, idx_b: np.ndarray) -> float:
        sub = dist[np.ix_(idx_a, idx_b)]
        return float(np.mean(np.min(sub, axis=1)))

    idx_a0 = np.arange(n_a)
    idx_b0 = np.arange(n_a, n_total)
    observed_t = _mean_cross_nn(idx_a0, idx_b0)

    co_located = int(np.sum(dist[np.ix_(idx_a0, idx_b0)] == 0.0))
    if co_located:
        warnings.append(
            f"{co_located} A-B point pairs sit at identical coordinates (co-located Places pins, "
            "e.g. multiple units in one mall/plaza) -- pulls the observed statistic toward 0, a "
            "genuine reflection of true or near-true co-location, not an error; worth a sanity "
            "check against duplicate records if this reading is surprising."
        )

    permuted_ts = np.empty(n_permutations)
    all_idx = np.arange(n_total)
    for i in range(n_permutations):
        perm = rng.permutation(all_idx)
        permuted_ts[i] = _mean_cross_nn(perm[:n_a], perm[n_a:])

    permuted_mean = float(np.mean(permuted_ts))
    permuted_std = float(np.std(permuted_ts, ddof=1)) if n_permutations > 1 else 0.0

    all_values = np.concatenate([permuted_ts, [observed_t]])
    rank_low = int(np.sum(all_values <= observed_t))    # support for "clustered" (small T)
    rank_high = int(np.sum(all_values >= observed_t))   # support for "dispersed" (large T)
    p_low = rank_low / (n_permutations + 1)
    p_high = rank_high / (n_permutations + 1)

    if p_low < 0.05:
        interpretation, p_value = "spatially_clustered", p_low
    elif p_high < 0.05:
        interpretation, p_value = "spatially_dispersed", p_high
    else:
        interpretation, p_value = "not_distinguishable_from_random", min(1.0, 2.0 * min(p_low, p_high))

    return CrossColocationResult(
        category_a="", category_b="",  # filled in by the caller, which knows the labels
        n_a=n_a, n_b=n_b,
        observed_mean_cross_nn_distance_m=round(observed_t, 2),
        permuted_mean_distance_m=round(permuted_mean, 2),
        permuted_std_distance_m=round(permuted_std, 2),
        p_value=round(p_value, 6), n_permutations=n_permutations,
        interpretation=interpretation, co_located_pairs=co_located, warnings=warnings,
    )


# ============================================================================
# Django-facing data assembly
# ============================================================================

def _operational_businesses_in_radius(subject_lat: float, subject_lon: float, radius_m: float) -> list:
    """Same bbox-prefilter + exact-haversine pattern as
    business_ecosystem.py's identically named helper (itself matching
    competition.py's), across ALL categories -- this engine's whole point
    is the co-occurrence PATTERN across categories, not one category's
    density."""
    lat_pad = radius_m / 111_000.0
    lon_pad = radius_m / (111_000.0 * max(math.cos(math.radians(subject_lat)), 0.01))
    bbox_filter = dict(
        current_status="operational",
        latitude__gte=subject_lat - lat_pad, latitude__lte=subject_lat + lat_pad,
        longitude__gte=subject_lon - lon_pad, longitude__lte=subject_lon + lon_pad,
    )
    qs = BusinessEntity.objects.filter(**bbox_filter)
    found = []
    for biz in qs:
        d = haversine_m(subject_lat, subject_lon, float(biz.latitude), float(biz.longitude))
        if d <= radius_m:
            found.append(biz)
    return found


def _category_sets_for_all_catchments() -> list:
    """
    Builds the citywide market-basket corpus: one frozenset of business
    categories per real meter-radius Catchment currently in the DB. Same
    live-corpus-per-run pattern as market_gap.py's
    _fit_corpus_for_category -- recomputed fresh each run, flagged as
    real separate infra work to cache later, not faked here. Isochrone
    catchments are excluded (this engine's business-mix retrieval only
    supports meter-radius catchments, same documented gap
    business_ecosystem.py already flags).
    """
    baskets = []
    for catchment in Catchment.objects.select_related("location_cell").filter(catchment_size_unit="meters"):
        cell = catchment.location_cell
        radius_m = float(catchment.catchment_size_value)
        businesses = _operational_businesses_in_radius(
            float(cell.center_latitude), float(cell.center_longitude), radius_m,
        )
        if businesses:
            baskets.append(frozenset(b.category for b in businesses))
    return baskets


@dataclass
class MissingComplementCandidate:
    """One candidate category, absent from the subject catchment, ranked
    by combined corpus-level evidence that it belongs alongside what IS
    present -- point 5 (Fisher's combined test) blended with the point
    2/4 affinity signals (lift, cosine similarity), per the module
    docstring's composite-score section."""
    category: str
    mean_lift: float
    mean_cosine_similarity: float
    fisher_combined_p_value: Optional[float]
    significant: bool
    recommendation_score: float
    supporting_present_categories: list = field(default_factory=list)


@dataclass
class CatchmentAdjacencyFeatures:
    catchment: Catchment
    present_categories: list = field(default_factory=list)
    pair_associations: list = field(default_factory=list)          # PairAssociation, co-present pairs only
    cross_colocation_results: list = field(default_factory=list)   # CrossColocationResult
    missing_complements: list = field(default_factory=list)        # MissingComplementCandidate, ranked
    corpus_n_catchments: Optional[int] = None
    corpus_excluded_categories: list = field(default_factory=list)
    source: str = "business_intel.engines.business_adjacency"
    source_class: str = "internal_derived"
    warnings: list = field(default_factory=list)


def compute_catchment_adjacency(
    cell: BusinessLocationCell,
    catchment: Catchment,
    rng_seed: int = RANDOM_SEED,
    n_permutations: int = N_PERMUTATIONS,
    corpus: Optional[AdjacencyCorpus] = None,
) -> Optional[CatchmentAdjacencyFeatures]:
    """
    Full Section-10 calculation for one Catchment: corpus-level
    association/affinity (points 1-4), fine-grained spatial validation
    for the top co-present pairs by lift (point 6), and ranked
    missing-complement recommendations (point 5). Returns None for
    isochrone-type catchments this engine doesn't support (see module
    docstring edge cases) so the caller can skip them cleanly.
    """
    if catchment.catchment_size_unit != "meters":
        logger.info(
            "Skipping catchment %s: business-adjacency analysis currently only supports "
            "meter-radius catchments (radius/network_radius), not isochrone geometry -- see "
            "module docstring.", catchment.id,
        )
        return None

    subject_lat, subject_lon = float(cell.center_latitude), float(cell.center_longitude)
    radius_m = float(catchment.catchment_size_value)
    warnings = []

    businesses = _operational_businesses_in_radius(subject_lat, subject_lon, radius_m)
    if not businesses:
        warnings.append(
            f"No operational businesses found within {radius_m:.0f}m -- catchment may be genuinely "
            "empty or not yet Places-searched."
        )
        return CatchmentAdjacencyFeatures(catchment=catchment, warnings=warnings)

    category_counts_local = Counter(b.category for b in businesses)
    present_categories = sorted(category_counts_local)
    points_by_category = defaultdict(list)
    for biz in businesses:
        points_by_category[biz.category].append((float(biz.latitude), float(biz.longitude)))

    if corpus is None:
        corpus = build_adjacency_corpus(_category_sets_for_all_catchments())

    if corpus is None:
        warnings.append(
            f"Fewer than {MIN_CORPUS_CATCHMENTS} usable catchments (or fewer than 2 categories "
            f"meeting MIN_CATEGORY_FREQUENCY={MIN_CATEGORY_FREQUENCY}) in the citywide corpus -- "
            "corpus-level association/affinity statistics were not fit; only this catchment's own "
            "raw category composition is reported."
        )
        return CatchmentAdjacencyFeatures(
            catchment=catchment, present_categories=present_categories, warnings=warnings,
        )
    warnings.extend(corpus.warnings)

    all_pair_associations = compute_all_pair_associations(corpus)
    assoc_lookup = {frozenset((p.category_a, p.category_b)): p for p in all_pair_associations}

    present_in_corpus = sorted(set(present_categories) & set(corpus.included_categories))
    co_present_pairs = [
        assoc_lookup[frozenset((a, b))]
        for a, b in combinations(present_in_corpus, 2)
        if frozenset((a, b)) in assoc_lookup
    ]

    # Spatial validation: spend the (bounded) permutation budget on the
    # co-present pairs the corpus flags as the strongest candidates worth
    # checking, per module docstring point 6.
    rng = np.random.default_rng(rng_seed)
    cross_results = []
    ranked_for_spatial_test = sorted(
        (p for p in co_present_pairs if p.lift is not None),
        key=lambda p: -p.lift,
    )
    for assoc in ranked_for_spatial_test[:MAX_SPATIAL_TESTS_PER_CATCHMENT]:
        pts_a = points_by_category.get(assoc.category_a, [])
        pts_b = points_by_category.get(assoc.category_b, [])
        if len(pts_a) > MAX_N_FOR_CROSS_COLOCATION_TEST or len(pts_b) > MAX_N_FOR_CROSS_COLOCATION_TEST:
            warnings.append(
                f"Skipped spatial co-location test for {assoc.category_a}/{assoc.category_b}: "
                f"too many points ({len(pts_a)}/{len(pts_b)}) for the O(n^2) permutation test in "
                "this catchment; corpus-level lift/cosine similarity are unaffected."
            )
            continue
        result = cross_category_colocation_test(pts_a, pts_b, rng, n_permutations=n_permutations)
        if result is not None:
            result.category_a, result.category_b = assoc.category_a, assoc.category_b
            cross_results.append(result)
        else:
            warnings.append(
                f"Fewer than {MIN_N_FOR_CROSS_COLOCATION_TEST} businesses of {assoc.category_a} or "
                f"{assoc.category_b} within this catchment -- spatial co-location test withheld for "
                "this pair (corpus-level lift is unaffected)."
            )

    # Missing-complement recommendations: point 5 (Fisher-combined
    # significance) blended with points 2/4 (mean lift, mean cosine
    # similarity) across every category actually present.
    missing_categories = [c for c in corpus.included_categories if c not in present_categories]
    if not missing_categories:
        warnings.append(
            "This catchment already contains every category represented anywhere in the citywide "
            "corpus -- no missing-complement candidates to recommend."
        )

    candidates = []
    for candidate in missing_categories:
        lifts, cosines, pvals, supporting = [], [], [], []
        for present_cat in present_in_corpus:
            assoc = assoc_lookup.get(frozenset((candidate, present_cat)))
            if assoc is None or assoc.lift is None:
                continue
            lifts.append(assoc.lift)
            cosines.append(assoc.cosine_similarity)
            pvals.append(assoc.p_value)
            if assoc.lift >= LIFT_INTEREST_THRESHOLD:
                supporting.append({
                    "present_category": present_cat, "lift": assoc.lift,
                    "cosine_similarity": assoc.cosine_similarity, "p_value": assoc.p_value,
                    "significant_fdr": assoc.significant_fdr,
                })
        if not lifts:
            continue  # no affinity evidence at all against this catchment's own present categories

        mean_lift = float(np.mean(lifts))
        mean_cosine = float(np.mean(cosines))
        combined_p = fisher_combined_p(pvals)
        significant = bool(combined_p is not None and combined_p < FDR_ALPHA and mean_lift > 1.0)

        # Recommendation score: geometric mean of a capped mean lift and
        # mean cosine similarity -- blends the two independently-derived
        # affinity signals (market-basket lift vs. item-based-CF cosine
        # similarity) into one comparable ranking number. Capping lift at
        # 10.0 stops one very sparse, very high-lift pair from
        # dominating the ranking (a real risk here: lift's ceiling is
        # unbounded as marginal supports shrink, per its own definition).
        capped_lift = min(max(mean_lift, 0.0), 10.0)
        score = math.sqrt(capped_lift * mean_cosine) if mean_cosine > 0 else 0.0

        candidates.append(MissingComplementCandidate(
            category=candidate, mean_lift=round(mean_lift, 4), mean_cosine_similarity=round(mean_cosine, 4),
            fisher_combined_p_value=combined_p, significant=significant,
            recommendation_score=round(score, 4), supporting_present_categories=supporting,
        ))

    candidates.sort(key=lambda c: (not c.significant, -c.recommendation_score))
    top_candidates = candidates[:TOP_N_RECOMMENDATIONS]

    return CatchmentAdjacencyFeatures(
        catchment=catchment, present_categories=present_categories,
        pair_associations=co_present_pairs, cross_colocation_results=cross_results,
        missing_complements=top_candidates, corpus_n_catchments=corpus.n_catchments,
        corpus_excluded_categories=corpus.excluded_low_frequency, warnings=warnings,
    )


def business_adjacency_evidence(features: CatchmentAdjacencyFeatures) -> dict:
    """Structured evidence dict, same convention as every other engine's
    *_evidence function in this codebase."""
    return {
        "present_categories": features.present_categories,
        "confirmed_adjacencies": [
            {
                "category_a": p.category_a, "category_b": p.category_b,
                "support_ab": p.support_ab,
                "confidence_a_to_b": p.confidence_a_to_b, "confidence_b_to_a": p.confidence_b_to_a,
                "lift": p.lift, "pmi": p.pmi, "cosine_similarity": p.cosine_similarity,
                "p_value": p.p_value, "significant_fdr": p.significant_fdr,
                "n_catchments_with_both": p.n_catchments_with_both,
            }
            for p in sorted(features.pair_associations, key=lambda x: -(x.lift or 0.0))[:15]
        ],
        "spatial_colocation_tests": [
            {
                "category_a": r.category_a, "category_b": r.category_b,
                "observed_mean_cross_nn_distance_m": r.observed_mean_cross_nn_distance_m,
                "permuted_mean_distance_m": r.permuted_mean_distance_m,
                "p_value": r.p_value, "interpretation": r.interpretation,
                "n_permutations": r.n_permutations, "co_located_pairs": r.co_located_pairs,
            }
            for r in features.cross_colocation_results
        ],
        "missing_complementary_business_recommendations": [
            {
                "category": c.category, "recommendation_score": c.recommendation_score,
                "mean_lift": c.mean_lift, "mean_cosine_similarity": c.mean_cosine_similarity,
                "fisher_combined_p_value": c.fisher_combined_p_value, "significant": c.significant,
                "supporting_present_categories": c.supporting_present_categories,
            }
            for c in features.missing_complements
        ],
        "corpus": {
            "n_catchments": features.corpus_n_catchments,
            "excluded_low_frequency_categories": features.corpus_excluded_categories,
        },
        "methods": [
            "market_basket_association_rules_support_confidence",
            "lift_and_pointwise_mutual_information",
            "yates_corrected_chi_square_test_of_independence",
            "benjamini_hochberg_fdr_correction",
            "item_based_collaborative_filtering_cosine_similarity",
            "fisher_combined_probability_test",
            "besag_diggle_1977_monte_carlo_cross_colocation_test",
        ],
    }


# ============================================================================
# Orchestration / EngineRun integration
# ============================================================================

def run(
    query: BusinessQuery,
    catchments: Optional[Sequence[Catchment]] = None,
    rng_seed: int = RANDOM_SEED,
    n_permutations: int = N_PERMUTATIONS,
) -> dict:
    """
    Engine 10 entry point. Fits the citywide market-basket/affinity
    corpus ONCE (not once per catchment -- identical corpus-level
    statistics apply to every catchment belonging to this query, same
    fit-once-score-many pattern market_gap.py already uses), scores
    every Catchment belonging to query.location_cell against it, writes
    FeatureObservation rows, and records an EngineRun -- same contract
    shape as Engine 6/7/9.

    Requires Engine 1 to have already run (catchments must exist), same
    dependency every other per-catchment engine in this codebase
    documents.
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

    corpus = build_adjacency_corpus(_category_sets_for_all_catchments())

    features_written = 0
    warnings_seen = []
    all_features = []

    try:
        with transaction.atomic():
            for catchment in target_catchments:
                catch_features = compute_catchment_adjacency(
                    cell, catchment, rng_seed=rng_seed, n_permutations=n_permutations, corpus=corpus,
                )
                if catch_features is None:
                    continue  # unsupported (isochrone) catchment type -- see module docstring
                all_features.append(catch_features)
                warnings_seen.extend(catch_features.warnings)

                evidence = business_adjacency_evidence(catch_features)
                FeatureObservation.write_latest(
                    location_cell=cell, catchment=catchment, engine_number=ENGINE_NUMBER,
                    feature_name="business_adjacency_evidence",
                    defaults=dict(
                        value_type="json", value_json=evidence, unit="",
                        observed_at=started_at, source=catch_features.source, source_class=catch_features.source_class,
                        method="business_adjacency_engine", derivation_level="calculation",
                        sample_size=catch_features.corpus_n_catchments,
                    ),
                )
                features_written += 1

                n_significant_pairs = sum(1 for p in catch_features.pair_associations if p.significant_fdr)
                FeatureObservation.write_latest(
                    location_cell=cell, catchment=catchment, engine_number=ENGINE_NUMBER,
                    feature_name="significant_adjacency_pair_count",
                    defaults=dict(
                        value_type="numeric", value_numeric=Decimal(str(n_significant_pairs)), unit="count",
                        observed_at=started_at, source=catch_features.source, source_class=catch_features.source_class,
                        method="benjamini_hochberg_fdr_chi_square", derivation_level="calculation",
                        sample_size=catch_features.corpus_n_catchments,
                    ),
                )
                features_written += 1

                if catch_features.missing_complements:
                    top = catch_features.missing_complements[0]
                    FeatureObservation.write_latest(
                        location_cell=cell, catchment=catchment, engine_number=ENGINE_NUMBER,
                        feature_name="top_missing_complement_score",
                        defaults=dict(
                            value_type="numeric", value_numeric=Decimal(str(top.recommendation_score)), unit="score",
                            observed_at=started_at, source=catch_features.source, source_class=catch_features.source_class,
                            method="item_based_collaborative_filtering_and_lift_blend",
                            derivation_level="business_conclusion",
                        ),
                    )
                    features_written += 1
                    FeatureObservation.write_latest(
                        location_cell=cell, catchment=catchment, engine_number=ENGINE_NUMBER,
                        feature_name="top_missing_complement_category",
                        defaults=dict(
                            value_type="categorical", value_text=top.category, unit="",
                            observed_at=started_at, source=catch_features.source, source_class=catch_features.source_class,
                            method="item_based_collaborative_filtering_and_lift_blend",
                            derivation_level="business_conclusion",
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

    degraded_reasons = []
    if corpus is None:
        degraded_reasons.append(
            f"Citywide corpus below MIN_CORPUS_CATCHMENTS={MIN_CORPUS_CATCHMENTS} or fewer than 2 "
            f"categories meeting MIN_CATEGORY_FREQUENCY={MIN_CATEGORY_FREQUENCY} -- "
            "association/affinity statistics were not fit this run."
        )
    if warnings_seen:
        degraded_reasons.append("; ".join(sorted(set(warnings_seen)))[:200])
    if degraded_reasons:
        run_row.status = "degraded"
        run_row.degraded_reason = "; ".join(degraded_reasons)[:255]

    run_row.save(update_fields=["status", "finished_at", "duration_ms", "features_written", "degraded_reason"])

    return {
        "cell": cell, "features": all_features, "engine_run": run_row,
        "corpus_fitted": corpus is not None,
        "corpus_n_catchments": corpus.n_catchments if corpus is not None else None,
    }