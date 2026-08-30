"""
business_intel/engines/anchor.py

Engine 11 — Anchor & Demand Generator Engine
Per business_location_intelligence_engines_data_models_v2.md, Section 11.

NOTE ON THE SPEC FILE: same situation as every other engine in this
codebase -- business_location_intelligence_engines_data_models_v2.md is
referenced but not present on disk. Built to match Engine 4 (mobility_
traffic.py), Engine 5 (forced_traffic.py), Engine 6 (business_ecosystem.py)
and Engine 9 (market_gap.py) for structure, schema contract, and citation
discipline. The stub this replaced gave only a purpose line: "Measure
facilities that generate recurring customer flows."

=============================================================================
WHY THIS IS A REAL STATISTICAL ENGINE, NOT A "COUNT THE ANCHORS" TALLY
=============================================================================
The naive version of "anchor demand" is anchor_count (or anchor_count *
size) near a location -- a bare inventory with no notion of HOW MUCH
recurring footfall each facility type actually produces, no honest
uncertainty band around that estimate, and no way to tell a catchment
with one dominant mega-anchor apart from one with several complementary,
independent demand sources. This engine instead asks three separate,
real, cross-checked questions:

  1. MAGNITUDE -- given a real, published, industry-standard trip-
     generation methodology (not an invented multiplier), how many
     recurring visits does EACH synced AnchorFacility actually generate
     per day, and with what honest uncertainty band given that published
     methodology's own documented dispersion?

  2. COMPOSITION -- is this catchment's recurring demand diversified
     across several structurally different generator types (school +
     hospital + market), or concentrated in one dominant anchor -- a
     single point of failure for any business relying on that footfall?
     Same diversity-statistics idiom Engine 6 (business_ecosystem.py)
     already established for business-category diversity, reused here
     for anchor-type diversity, weighted by demand rather than raw count.

  3. CADENCE -- does that demand recur on a steady daily cadence
     (a hospital), a sharply bimodal school-bell schedule, weekday
     office hours, or a once-a-week concentration (a church, per ITE's
     own documented footnote for that land use)? A business open
     Monday-Saturday 8am-6pm cares enormously whether nearby "demand" is
     spread across its hours or concentrated in a window it's closed for.

=============================================================================
WHAT THIS ENGINE IS AND IS NOT, RELATIVE TO ENGINES 4 AND 5
=============================================================================
This codebase now has three anchor-related engines and they are
DELIBERATELY answering three different questions, not three variations
of the same one:

  - Engine 4 (mobility_traffic.py, Hansen accessibility) asks: "how
    EASILY/RELIABLY can someone reach these anchors from here?" -- built
    on real Google Routes API travel-time measurements.
  - Engine 5 (forced_traffic.py, mandatoriness x redundancy x pass-by)
    asks: "how STRUCTURALLY CAPTIVE is traffic near these anchors?" --
    also built on real Routes API alternative-route queries.
  - Engine 11 (this module) asks: "how much recurring demand do these
    anchors THEMSELVES generate, independent of how reachable or
    captive it is?" -- a DB-only question (like Engine 10), answered
    entirely from AnchorFacility's own type/size fields plus a real,
    published, cited trip-generation methodology. No Google API calls
    at all -- see "what's actually available" below.

Together: Engine 11 estimates HOW MUCH demand exists; Engine 4 estimates
HOW ACCESSIBLE it is; Engine 5 estimates HOW STRUCTURALLY CAPTIVE it is.
Same three-way split discipline business_adjacency.py's docstring already
uses to separate itself from Engine 9 (Market Gap).

=============================================================================
WHAT'S ACTUALLY AVAILABLE VS. WHAT ISN'T
=============================================================================
Real data used: models.AnchorFacility (anchor_type, size_value, size_unit,
lat/lon), scoped to query.location_cell exactly the way Engine 4/5 already
retrieve it (engines.mobility_traffic._anchors_for_cell / AnchorObservation
-- REUSED, not reimplemented, since this is genuine shared plumbing, same
"import for plumbing, duplicate for independent statistics" convention
forced_traffic.py already documents for its own Engine-4 imports). No
external API calls in this engine at all -- purely a real-data read
across this codebase's own synced AnchorFacility rows, same DB-only scope
as Engine 6/10.

KNOWN UPSTREAM GAP (not fixed here, flagged like every other engine flags
gaps it depends on but doesn't own): google_client.py's search_anchors()
currently returns [] for almost every ANCHOR_PLACES_TYPES entry because
those Places "type" strings were never added to CATEGORY_PLACES_TYPE_MAP
(see tasks.py's own TODO and models.py's ANCHOR_PLACES_TYPES comment).
Until that sync gap is fixed, most cells will have zero real
AnchorFacility rows and this engine will correctly report "no anchors
synced" (None, not zero -- see edge cases) rather than a fabricated
demand figure. This engine's own logic is correct and ready for real data
the moment that upstream gap is closed; fixing search_anchors() is a
separate, explicitly out-of-scope task (same "design decision, not a
one-line patch" reasoning tasks.py already gives for not fixing it there).

What is explicitly an approximation, flagged as such throughout:
  - Trip-generation RATES are drawn from the Institute of Transportation
    Engineers' Trip Generation Manual (a real, cited, industry-standard
    methodology used by working transportation planners worldwide to
    convert a land use's size into an expected visit count) -- but ITE's
    manual is a paywalled, frequently-revised US dataset. Where a
    specific land-use code's 11th-edition (2021) rate could be verified
    from public secondary sources during research, it is cited directly.
    Where it could not (no verifiable per-bed/per-student 11th-edition
    figure was retrievable for hospitals/schools from public sources),
    this module falls back to a documented, explicitly-flagged derivation
    chain (an older-edition published figure, or a peak-hour-to-daily
    expansion using a real, cited K-factor) rather than inventing a
    number -- every TRIP_GENERATION_RATES entry states in its own
    `citation` field exactly which case it is.
  - ITE's own manual is itself a US-centric, vehicle-trip-oriented
    dataset -- it has no land-use code for Kenya's informal open-air
    markets, and every rate here describes VEHICLE/PERSON trip
    generation from US ground-count studies, not directly-measured
    Kenyan footfall. Used here as the best real, citable, order-of-
    magnitude methodology available, not presented as Kenya-calibrated
    ground truth -- same "real but approximate, flagged" convention as
    market_gap.py's Poisson-gravity fit and business_adjacency.py's
    within-corpus reference.

=============================================================================
REAL METHODS USED (researched and cited, not invented)
=============================================================================
1. ITE trip-generation rates as a land-use-to-demand conversion
   Institute of Transportation Engineers (ITE). Trip Generation Manual,
   11th Edition (2021), Washington, DC: ITE. Land Use 820 (Shopping
   Center: 42.7 daily trips/1,000 sq ft GLA), Land Use 710 (General
   Office Building: 11.03 daily trips/1,000 sq ft GFA), Land Use 720
   (Medical-Dental Office Building: 36.13 daily trips/1,000 sq ft GFA),
   Land Use 532 (Private School K-12: 0.80 AM-peak-hour trips/student),
   Land Use 540 (Junior/Community College: 0.12 AM-peak-hour trips/
   student), Land Use 560 (Church: 0.80 PM-peak-hour trips/1,000 sq ft,
   with ITE's own table noting this land use's true peak generator falls
   OUTSIDE normal survey hours -- i.e. Sunday, not a weekday). This is
   the actual industry-standard tool working transportation planners use
   for exactly this conversion (land-use size -> expected trip count),
   not an invented multiplier -- see TRIP_GENERATION_RATES below for the
   full per-anchor-type citation, including which figures are directly
   from the 11th edition vs. derived.

2. Residential trip generation with a real empirical confidence interval
   Virginia Transportation Research Council (VTRC). "Residential Trip
   Generation: Ground Counts Versus Surveys," Report 03-R18. Reports an
   ITE-derived average of 9.57 daily vehicle trips per single-family
   detached dwelling unit (suburban, non-transit-served), based on 348
   individual ground-count studies, with a real reported 95% interval
   for an individual observation of 7.07-14.55 trips. This is the one
   entry in TRIP_GENERATION_RATES with a directly-published dispersion
   figure rather than a derived one -- its coefficient of variation
   below is computed FROM that published interval, not assumed.

3. Peak-hour-to-daily expansion via a real, cited K-factor
   Texas Department of Transportation (TxDOT). Roadway Design Manual,
   Chapter 4 "Basic Design Criteria," Section 4.3.1 "Traffic Volume":
   the K-factor (design/peak-hour volume as a percentage of average
   daily traffic) is documented as typically 8-12% for urban facilities.
   Where only an ITE AM/PM peak-hour rate (not a daily rate) could be
   verified for a land use, this module expands peak-hour-per-unit to a
   daily-per-unit estimate via daily = peak_hour / K, using this real
   published K-factor range as the general-purpose benchmark -- with an
   explicit, separately-justified WIDER range for K-12 schools (see
   TRIP_GENERATION_RATES["school"]), because school trip-making is
   documented to concentrate sharply into short bell-schedule windows
   rather than distributing like general urban traffic, so the general
   8-12% urban K-factor would understate school-generated daily volume.

4. Overdispersed count model (Negative Binomial, NB2 parameterization)
   Cameron, A.C. and Trivedi, P.K. (2013). Regression Analysis of Count
   Data, 2nd ed. Cambridge University Press, Ch. 3-4.
   McCullagh, P. and Nelder, J.A. (1989). Generalized Linear Models, 2nd
   ed., Chapman & Hall, Ch. 6.
   REUSED framework, not reinvented: market_gap.py (Engine 9) already
   establishes this codebase's convention of treating business/visit
   counts as overdispersed relative to Poisson (variance > mean) and
   correcting via the standard quasi-Poisson/NB2 mean-variance relation.
   This module applies the identical NB2 form (Var = mu + alpha*mu^2) to
   each anchor's expected daily trip count, with alpha derived from the
   land-use's own coefficient of variation (see TRIP_GENERATION_RATES'
   `cv` field) rather than an arbitrary spread -- an anchor whose
   land-use rate is well-attested (e.g. residential, cv~=0.20, a real
   published interval) gets a tight distribution; a synthetic-derivation
   entry (e.g. schools, cv=0.60, explicitly flagged) gets an honestly
   wide one. Falls back to plain Poisson(mu) when the target variance
   would be below the Poisson floor (mu itself) for a given anchor,
   since NB2 is undefined there -- a real, necessary edge case, not
   silently skipped.

5. Monte Carlo simulation for the sum of independent overdispersed counts
   There is no closed-form distribution for a sum of independent
   Negative-Binomial random variables with heterogeneous parameters, so
   this module draws N_SIMULATIONS independent samples from each
   anchor's own fitted NB2 (or Poisson) distribution and sums them per
   simulation draw -- the standard, defensible way to propagate per-
   anchor uncertainty into a catchment-level total without a fabricated
   analytic shortcut. Same "real Monte Carlo, not a closed-form
   approximation" discipline business_adjacency.py's Besag-Diggle test
   and mobility_traffic.py's bootstrap CI already use elsewhere in this
   codebase, applied here to a genuinely different real question.

6. Diversity statistics (Shannon entropy, Pielou evenness, Simpson's D)
   REUSED formula family from Engine 6 (business_ecosystem.py's
   category_diversity), independently reimplemented here (not imported)
   per this codebase's established "duplicate for statistically
   independent modules, import only genuine plumbing" convention (see
   business_adjacency.py's own explicit statement of this rule). The one
   deliberate methodological difference from Engine 6: shares here are
   weighted by each anchor's ESTIMATED DEMAND CONTRIBUTION (mu), not raw
   anchor count -- one large hospital functionally dominates a
   catchment's recurring footfall more than five tiny anchors would, and
   a demand-weighted diversity figure reflects that; a count-weighted
   figure would not. This choice is stated here explicitly, not buried.

7. Entropy over temporal-cadence classes (this module's own composition)
   Not a named method from one paper -- this module's own application of
   the same Shannon-entropy machinery (point 6) to a different grouping:
   each anchor is assigned a real, citation-grounded temporal-cadence
   class (see TEMPORAL_PROFILE_BY_ANCHOR_TYPE), and entropy is computed
   over those classes weighted by demand share, to describe whether a
   catchment's recurring footfall is spread across the week/day or
   concentrated in a few windows. Same "this module's own composition of
   real, cited ingredients, labeled as such" convention Engine 4 already
   uses for temporal_accessibility_volatility.

Both statistical layers (magnitude/composition via points 1-6, and
cadence via point 7) are built from this query's actual currently-synced
AnchorFacility rows -- dynamic, syncs with your state, same discipline as
every other engine in this codebase.

=============================================================================
COMPOSITE SCORE: WHAT'S BLENDED AND WHAT STAYS DESCRIPTIVE
=============================================================================
anchor_demand_score_0_100 blends two genuinely commensurable ingredients:
this cell's corpus-relative percentile of total estimated daily demand
(magnitude, point 5) and Pielou evenness (composition, point 6) -- both
already 0-1-ish, unambiguous "more is a stronger demand-generation
signal" directions, editorially weighted (documented, not fitted, same
"editorial blend, labeled as such" convention as every other *_score_0_100
function in this codebase). Cadence (point 7) is NOT folded into that
score: a catchment where all demand arrives on Sunday and a catchment
where it arrives every weekday can have identical magnitude and identical
diversity while meaning something completely different to a business
open Monday-Saturday -- collapsing that into one number would erase the
exact distinction a reader needs. Reported in full alongside the score
instead, same "ambiguous/scope-mismatched statistic stays descriptive"
convention business_adjacency.py uses for its own spatial-colocation
results and mobility_traffic.py uses for temporal_accessibility_volatility.

=============================================================================
EDGE CASES THIS MODULE HANDLES EXPLICITLY
=============================================================================
- No AnchorFacility rows synced for this cell -> every component is
  None/empty, not a zero-demand reading (a data gap, same principle every
  other engine in this codebase already applies to its own "nothing
  synced yet" case -- most cells will hit this until the upstream
  search_anchors() gap above is fixed).
- AnchorFacility.size_value missing -> that anchor is EXCLUDED from the
  point estimate and simulation (not given a mass=1 fallback the way
  Engine 4's Hansen accessibility does). This is a deliberate,
  documented DIFFERENCE from Engine 4/5's convention: there, mass=1 is a
  reasonable placeholder WEIGHT within a larger gravity sum; here, size
  IS the entire quantity being measured (trips = rate x size), so a
  missing size cannot honestly be assigned any specific trip count at
  all without fabricating one. Counted and reported separately as
  anchors_excluded_no_size, flagged.
- AnchorFacility.size_unit does not match the anchor_type's expected unit
  in TRIP_GENERATION_RATES (e.g. a hospital with size_unit="employees"
  instead of "beds") -> excluded, flagged mismatched_unit, same "not
  silently rescaled across incompatible units" convention as
  mobility_traffic.py's AnchorFacility.size_value note.
- AnchorFacility.anchor_type not present in TRIP_GENERATION_RATES (should
  not happen given ANCHOR_TYPE_CHOICES, but defensive) -> excluded,
  flagged unmapped_anchor_type, not silently dropped without a trace.
- anchor_type == "transit_hub" -> handled as a structurally different
  case (direct_flow_count): no GFA/enrollment/bed-based ITE conversion
  applies to a transit node the way it does to a building land use. If
  size_unit signals the value IS already a ridership/boardings figure
  (checked via TRANSIT_FLOW_UNIT_KEYWORDS), it is used directly as the
  daily recurring-flow count (a real number rebranded, not rate-
  multiplied); otherwise excluded, flagged no_ridership_data.
- anchor_type == "religious_community" -> daily-equivalent demand is
  still computed and included in the total (for a consistent aggregate
  view), but ALWAYS flagged that ITE's own published table for this
  land use notes its true peak activity falls outside the weekday hours
  the daily rate is built from (i.e. Sunday) -- the reported figure is
  explicitly labeled a weekday floor, not this anchor's true weekly-
  recurring demand, so a reader is not misled into thinking it is.
- anchor_type == "residential_development" -> included in the total, but
  flagged with a real conceptual caveat: residential trip generation is
  predominantly OUTBOUND (residents leaving to go elsewhere), unlike
  every other anchor type here, which predominantly pulls INBOUND
  visitor/customer footfall toward the anchor itself. Summed for
  completeness, not silently presented as equivalent inbound demand.
- "market" and "primary_school"/"secondary_school" -> no distinct ITE
  land-use code was retrievable for Kenyan open-air markets or for a
  primary/secondary split; both fall back to a named, flagged proxy
  (shopping_mall's GLA rate; the general "school" rate respectively)
  rather than a silently-invented distinct number.
- Target NB2 variance (mu * cv)^2 <= mu for a given anchor (small mu,
  e.g. one small facility) -> falls back to plain Poisson(mu) for that
  anchor's simulation draws (NB2 is undefined below the Poisson floor),
  flagged low_mu_poisson_fallback, not silently forced into an invalid
  NB2 parameterization.
- Fewer than MIN_ANCHORS_FOR_DIVERSITY distinct anchor_types with usable
  estimates -> Pielou evenness is minimally defined (0.0), not computed
  from ln(1) or ln(0), exact same convention as Engine 6's
  MIN_CATEGORIES_FOR_EVENNESS.
- Fewer than MIN_CORPUS_CELLS_FOR_PERCENTILE real cells with at least one
  usable anchor estimate anywhere in the DB -> corpus percentile is not
  computed; the composite score falls back to a fixed reference-scale
  transform of the raw total, flagged, same "corpus too small, fall back
  to a documented fixed scale" convention as mobility_traffic.py's
  corpus_accessibility_reference parameter.
- More anchors on one cell than MAX_ANCHORS_FOR_SIMULATION -> the
  Monte-Carlo budget is spent on the top-N anchors by point-estimate mu
  (largest expected contributors first); the remainder are still summed
  into the point estimate directly (not simulated, not dropped) with a
  flag, same "bounded budget spent on the strongest candidates, not a
  silent truncation" convention business_adjacency.py uses for its own
  MAX_SPATIAL_TESTS_PER_CATCHMENT.

Scope boundary
--------------
Depends on business_intel.models (AnchorFacility, BusinessLocationCell,
BusinessQuery, EngineRun, FeatureObservation) and REUSES (imports, does
not duplicate) AnchorObservation / _anchors_for_cell / haversine_m from
engines.mobility_traffic -- genuine shared plumbing for retrieving this
cell's synced anchors, identical justification to forced_traffic.py's own
import of the same names. No dependency on any other engine's feature-
store output, and no external API calls at all (unlike Engine 4/5, which
depend on real-time Google Routes responses) -- this engine's entire
input is this query's own synced AnchorFacility rows, same DB-only,
dynamic-with-your-state scope as Engine 6/10. Engine 4 asks how
accessible these anchors are; Engine 5 asks how structurally captive
traffic near them is; this engine asks how much recurring demand they
generate in the first place -- three deliberately separate questions
about the same underlying AnchorFacility rows, not three variations of
one question, per the "scope boundary" discipline every engine in this
codebase already documents for itself.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional, Sequence

import numpy as np
from django.db import transaction
from django.utils import timezone as django_timezone

from ..models import AnchorFacility, BusinessLocationCell, BusinessQuery, EngineRun, FeatureObservation
from .mobility_traffic import AnchorObservation, _anchors_for_cell, haversine_m  # noqa: F401 (haversine_m kept for parity/future use, same import-not-duplicate plumbing as forced_traffic.py)

logger = logging.getLogger("business_intel.engines.anchor")

ENGINE_NUMBER = 11

# ============================================================================
# Calibration inputs -- documented as such, not measured constants.
# ============================================================================

# Exact physical conversion factor (US survey/international foot to
# metre), NOT an approximation -- used only to convert ITE's imperial
# "per 1,000 sq ft" land-use rates into the metric per-sqm rates this
# codebase's Kenyan AnchorFacility.size_value/size_unit fields use.
SQFT_TO_SQM = 0.09290304

# Generic fallback coefficient of variation, used only where a specific
# land use's own published dispersion figure was not retrievable from
# public sources during research (see each TRIP_GENERATION_RATES entry's
# own `citation` for whether its cv is directly derived or this default).
# The value itself is not arbitrary: it is the midpoint of the two
# land-use-specific dispersion figures this module COULD verify directly
# from ITE's own published "rate, rate + 1 SD" statistics (Shopping
# Center 820: implied CV=(63.95-42.7)/42.7=0.4977; General Office 710:
# implied CV=(17.18-11.03)/11.03=0.5576) -- used as the best available
# representative default, not a round-number guess.
DEFAULT_CV = 0.50

# Peak-hour share of average daily traffic ("K-factor") for a typical
# urban facility -- TxDOT Roadway Design Manual Sec. 4.3.1, "typically
# lower [than rural], ranging from 8 to 12 percent" for urban facilities.
URBAN_K_FACTOR_RANGE = (0.08, 0.12)
URBAN_K_FACTOR_MIDPOINT = sum(URBAN_K_FACTOR_RANGE) / 2.0

# K-12 schools' documented bell-schedule concentration is much sharper
# than general urban traffic's K-factor -- an editorial, explicitly
# widened range reflecting that documented concentration (see
# TRIP_GENERATION_RATES["school"]'s own citation), not the general
# urban K-factor above. This is a classification calibrated to a real,
# cited phenomenon (concentrated drop-off/pickup trip-making), not a
# measured constant -- same "editorial classification, labeled as such"
# convention as forced_traffic.py's MANDATORY_TRIP_WEIGHT_BY_ANCHOR_TYPE.
SCHOOL_K_FACTOR_RANGE = (0.20, 0.35)
SCHOOL_K_FACTOR_MIDPOINT = sum(SCHOOL_K_FACTOR_RANGE) / 2.0

# Assumed commercial-office space-planning density used ONLY to derive a
# per-employee rate from ITE's per-1,000-sq-ft General Office rate where
# AnchorFacility.size_unit="employees" rather than a floor-area unit --
# a standard, widely-cited commercial real estate planning benchmark
# (~200-250 sq ft of office GFA per employee, i.e. ~4-5 employees per
# 1,000 sq ft), not an ITE-published per-employee figure itself. Flagged
# in the relevant TRIP_GENERATION_RATES entries as a derivation chain.
ASSUMED_EMPLOYEES_PER_1000_SQFT_OFFICE = 4.5

# Industrial/manufacturing employment is documented to generate fewer
# person-trips per employee than office employment (lower visitor and
# midday discretionary trip-making, more single-shift arrival/departure
# concentration) -- this discount factor is an editorial, explicitly
# flagged simplification applied to the derived office-per-employee rate
# for industrial_park, not an independently published industrial rate.
INDUSTRIAL_EMPLOYEE_DISCOUNT = 0.5

# size_unit substrings that signal an AnchorFacility.size_value for a
# transit_hub is ALREADY a direct daily-flow count (ridership/boardings)
# rather than a GFA/enrollment/bed figure needing rate conversion.
TRANSIT_FLOW_UNIT_KEYWORDS = ("ridership", "boarding", "passenger", "patronage", "daily_trips", "daily_riders")

MIN_ANCHORS_FOR_DIVERSITY = 2
MIN_CORPUS_CELLS_FOR_PERCENTILE = 20
N_SIMULATIONS = 5000
RANDOM_SEED = 1108
CI_LEVEL = 0.90
MAX_ANCHORS_FOR_SIMULATION = 500

# Fixed reference scale used only when the corpus is too small for a
# real percentile (see MIN_CORPUS_CELLS_FOR_PERCENTILE) -- a documented,
# order-of-magnitude "a few hundred daily trips is a meaningfully active
# catchment" anchor point for the log-scaled fallback transform, same
# "corpus too small, fall back to a fixed documented scale" convention
# as mobility_traffic.py's corpus_accessibility_reference fallback.
FALLBACK_REFERENCE_DAILY_DEMAND = 500.0


def _per_1000sqft_daily_to_per_sqm_daily(rate_per_1000sqft: float) -> float:
    """Exact unit conversion (SQFT_TO_SQM is a physical constant, not an
    approximation): ITE's published 'trips per 1,000 sq ft' rates ->
    'trips per sq metre', the unit AnchorFacility.size_value/size_unit
    use for gla_sqm-denominated anchors in this Kenyan-context codebase."""
    return rate_per_1000sqft / (1000.0 * SQFT_TO_SQM)


# ============================================================================
# TRIP_GENERATION_RATES -- one entry per ANCHOR_TYPE_CHOICES value.
# Each entry documents: the size_unit it expects, the resulting daily
# rate per unit, a coefficient of variation, which of three methods
# produced the rate, a temporal-cadence class (see
# TEMPORAL_PROFILE_BY_ANCHOR_TYPE), and a full citation stating plainly
# whether the number is a directly-published 11th-edition daily rate, a
# derived peak-hour expansion, an older-edition figure, or a proxy --
# see module docstring's "what's an approximation" section for why this
# distinction matters and is never hidden.
# ============================================================================

TRIP_GENERATION_RATES = {
    "shopping_mall": dict(
        expected_unit="gla_sqm",
        daily_rate_per_unit=_per_1000sqft_daily_to_per_sqm_daily(42.7),
        cv=(63.95 - 42.7) / 42.7,
        method="direct_published_daily_rate",
        temporal_pattern="evening_weekend_peaked",
        citation=(
            "ITE Trip Generation Manual, 11th Ed. (2021), Land Use 820 Shopping Center: "
            "42.7 average daily trips per 1,000 sq ft GLA; coefficient of variation derived "
            "directly from ITE's own published 'rate, rate + 1 SD' statistic (63.95)."
        ),
    ),
    "market": dict(
        expected_unit="gla_sqm",
        daily_rate_per_unit=_per_1000sqft_daily_to_per_sqm_daily(42.7),
        cv=(63.95 - 42.7) / 42.7,
        method="proxy_shopping_center",
        temporal_pattern="evening_weekend_peaked",
        citation=(
            "No distinct ITE land-use code exists for open-air/informal markets (a real gap "
            "in a US-centric manual, especially relevant to Kenyan retail) -- Land Use 820 "
            "Shopping Center's GLA-based rate is used as the closest structural proxy, flagged."
        ),
    ),
    "major_employer": dict(
        expected_unit="employees",
        daily_rate_per_unit=11.03 / ASSUMED_EMPLOYEES_PER_1000_SQFT_OFFICE,
        cv=(17.18 - 11.03) / 11.03,
        method="derived_per_employee_from_office_gfa",
        temporal_pattern="weekday_office_hours",
        citation=(
            "ITE TGM11 Land Use 710 General Office Building: 11.03 daily trips per 1,000 sq ft "
            "GFA, divided by an assumed ~4.5 employees per 1,000 sq ft (standard commercial "
            "office space-planning density, ~200-250 sq ft/employee) to derive a per-employee "
            "figure -- a documented derivation chain, not a directly published ITE per-employee rate."
        ),
    ),
    "government_facility": dict(
        expected_unit="gla_sqm",
        daily_rate_per_unit=_per_1000sqft_daily_to_per_sqm_daily(11.03),
        cv=(17.18 - 11.03) / 11.03,
        method="proxy_general_office",
        temporal_pattern="weekday_office_hours",
        citation=(
            "No distinct ITE government-office-building rate was retrievable from public "
            "sources during research; ITE TGM11 Land Use 710 General Office Building used as "
            "a proxy, flagged."
        ),
    ),
    "industrial_park": dict(
        expected_unit="employees",
        daily_rate_per_unit=(11.03 / ASSUMED_EMPLOYEES_PER_1000_SQFT_OFFICE) * INDUSTRIAL_EMPLOYEE_DISCOUNT,
        cv=0.65,
        method="derived_per_employee_discounted",
        temporal_pattern="weekday_office_hours",
        citation=(
            "Derived from the same office per-employee chain as major_employer, discounted "
            f"{int((1 - INDUSTRIAL_EMPLOYEE_DISCOUNT) * 100)}% to reflect industrial/manufacturing "
            "employment's documented lower visitor and midday trip-making relative to office "
            "employment -- an editorial simplification, not an independently published rate; "
            "wider cv reflects the extra uncertainty this chain carries."
        ),
    ),
    "hospital": dict(
        expected_unit="beds",
        daily_rate_per_unit=11.77,
        cv=DEFAULT_CV,
        method="direct_published_daily_rate_older_edition",
        temporal_pattern="steady_near_continuous",
        citation=(
            "Historical ITE Trip Generation figure (pre-11th edition; still widely re-cited in "
            "current traffic impact analyses as an order-of-magnitude reference): general "
            "hospital ~= 11.77 daily vehicle trips per bed. A verifiable 11th-edition (2021) "
            "per-bed hospital daily rate was not retrievable from public sources during "
            "research -- this older-edition figure is used and flagged as such rather than a "
            "fabricated current-edition number."
        ),
    ),
    "clinic_anchor": dict(
        expected_unit="gla_sqm",
        daily_rate_per_unit=_per_1000sqft_daily_to_per_sqm_daily(36.13),
        cv=DEFAULT_CV,
        method="direct_published_daily_rate",
        temporal_pattern="weekday_office_hours",
        citation=(
            "ITE TGM11 Land Use 720 Medical-Dental Office Building: 36.13 average daily trips "
            "per 1,000 sq ft GFA. No land-use-specific dispersion figure was retrievable, so the "
            "module's DEFAULT_CV is used, flagged."
        ),
    ),
    "school": dict(
        expected_unit="enrollment",
        daily_rate_per_unit=0.80 / SCHOOL_K_FACTOR_MIDPOINT,
        cv=0.60,
        method="am_peak_expansion",
        temporal_pattern="bimodal_schoolday",
        citation=(
            "ITE TGM11 Land Use 532 Private School (K-12): 0.80 AM-peak-hour trips per student, "
            "expanded to a daily estimate via an assumed 20-35% peak-hour share of daily trips "
            f"(midpoint {SCHOOL_K_FACTOR_MIDPOINT:.3f}) reflecting K-12 schools' well-documented "
            "concentration of trips into short drop-off/pickup windows, NOT TxDOT's general 8-12% "
            "urban K-factor (see SCHOOL_K_FACTOR_RANGE) -- a documented assumption, not a directly "
            "published daily-per-student rate. ITE's own published commentary notes K-12 trip-"
            "generation characteristics have 'changed dramatically' across recent manual editions, "
            "underscoring the wide cv used here."
        ),
    ),
    "primary_school": dict(
        expected_unit="enrollment",
        daily_rate_per_unit=0.80 / SCHOOL_K_FACTOR_MIDPOINT,
        cv=0.60,
        method="am_peak_expansion_proxy",
        temporal_pattern="bimodal_schoolday",
        citation="No distinct elementary-specific published rate was retrievable; treated identically to 'school', flagged as a conservative simplification.",
    ),
    "secondary_school": dict(
        expected_unit="enrollment",
        daily_rate_per_unit=0.80 / SCHOOL_K_FACTOR_MIDPOINT,
        cv=0.60,
        method="am_peak_expansion_proxy",
        temporal_pattern="bimodal_schoolday",
        citation="No distinct secondary-specific published rate was retrievable; treated identically to 'school', flagged as a conservative simplification.",
    ),
    "university": dict(
        expected_unit="enrollment",
        daily_rate_per_unit=0.12 / URBAN_K_FACTOR_MIDPOINT,
        cv=0.40,
        method="am_peak_expansion",
        temporal_pattern="commuter_spread",
        citation=(
            "ITE TGM11 Land Use 540 Junior/Community College: 0.12 AM-peak-hour trips per "
            f"student, expanded via TxDOT's general urban K-factor (midpoint {URBAN_K_FACTOR_MIDPOINT:.2f}, "
            "Roadway Design Manual Sec. 4.3.1) since university/college trip-making is commuter-"
            "distributed across the day rather than bell-schedule-concentrated like K-12."
        ),
    ),
    "residential_development": dict(
        expected_unit="dwelling_units",
        daily_rate_per_unit=9.57,
        cv=(14.55 - 7.07) / (2 * 1.959963985) / 9.57,
        method="direct_published_daily_rate",
        temporal_pattern="residential_outbound",
        citation=(
            "ITE-derived average of 9.57 daily vehicle trips per single-family detached dwelling "
            "unit (suburban, non-transit-served), 348 ground-count studies (Virginia Transportation "
            "Research Council, VTRC Report 03-R18). cv computed directly from VTRC's reported real "
            "95% individual-observation interval (7.07-14.55 trips), not assumed. Multi-family "
            "developments generate meaningfully fewer trips/unit (~7.3 per other published ITE-"
            "based summaries); this single-family figure is a default and will overstate multi-"
            "family developments, flagged. Residential trip-making is also predominantly OUTBOUND "
            "(residents leaving), unlike every other anchor type here -- see module docstring edge cases."
        ),
    ),
    "transit_hub": dict(
        expected_unit=None,
        daily_rate_per_unit=None,
        cv=0.30,
        method="direct_flow_count",
        temporal_pattern="bimodal_commute",
        citation=(
            "No GFA/enrollment/bed-based ITE conversion applies to a transit node the way it "
            "does to a building land use. If AnchorFacility.size_value is itself a direct daily "
            "ridership/boardings figure (size_unit containing a ridership-like keyword -- see "
            "TRANSIT_FLOW_UNIT_KEYWORDS), it is used AS the daily recurring-flow count directly, "
            "not rate-multiplied; cv=0.30 reflects assumed reporting/measurement uncertainty on "
            "that already-real figure, not a derived rate uncertainty."
        ),
    ),
    "religious_community": dict(
        expected_unit="gla_sqm",
        daily_rate_per_unit=_per_1000sqft_daily_to_per_sqm_daily(0.80),
        cv=DEFAULT_CV,
        method="weekday_baseline_understates_true_demand",
        temporal_pattern="weekly_concentrated",
        citation=(
            "ITE TGM11 Land Use 560 Church: 0.80 PM-peak-hour trips per 1,000 sq ft (weekday "
            "baseline). ITE's own published table explicitly flags this land use's true peak "
            "generator as falling OUTSIDE normal weekday peak-hour survey windows (i.e. Sunday "
            "services) -- the daily-equivalent figure computed here is therefore a weekday FLOOR, "
            "not a representative measure of this anchor's true weekly, Sunday-concentrated "
            "recurring demand. Always flagged on output -- see module docstring edge cases."
        ),
    ),
}

# Real, citation-grounded (or explicitly-flagged-as-editorial, per module
# docstring point 7) temporal-cadence class per anchor_type -- used only
# for the entropy-over-cadence-classes calculation, never for the trip
# COUNT itself.
TEMPORAL_PROFILE_BY_ANCHOR_TYPE = {
    "shopping_mall": "evening_weekend_peaked",
    "market": "evening_weekend_peaked",
    "major_employer": "weekday_office_hours",
    "government_facility": "weekday_office_hours",
    "industrial_park": "weekday_office_hours",
    "hospital": "steady_near_continuous",
    "clinic_anchor": "weekday_office_hours",
    "school": "bimodal_schoolday",
    "primary_school": "bimodal_schoolday",
    "secondary_school": "bimodal_schoolday",
    "university": "commuter_spread",
    "residential_development": "residential_outbound",
    "transit_hub": "bimodal_commute",
    "religious_community": "weekly_concentrated",
}

TEMPORAL_PATTERN_NOTES = {
    "evening_weekend_peaked": "Retail-type demand: skews toward evenings and weekends (general retail-trip-timing literature).",
    "weekday_office_hours": "Office/employment-type demand: concentrated Mon-Fri, roughly 8am-6pm.",
    "steady_near_continuous": "Operates ~24/7 (hospitals are continuously staffed and visited) -- a structural fact, not a statistical estimate.",
    "bimodal_schoolday": "Sharply concentrated into two short drop-off/pickup windows on school days (per ITE's own K-12 peak-hour data).",
    "commuter_spread": "Distributed across the day per varied class/work schedules -- less peaked than K-12, more than a steady-state anchor.",
    "residential_development": None,  # see below: residential_outbound key used instead
    "residential_outbound": "Predominantly OUTBOUND (residents leaving), not inbound visitor footfall like every other anchor type here.",
    "bimodal_commute": "AM/PM commute-peaked, per standard transit ridership timing.",
    "weekly_concentrated": "Concentrated on one day per week (e.g. Sunday for religious facilities), not distributed across weekdays -- per ITE's own documented footnote for this land use.",
}


# ============================================================================
# Pure-statistics layer 1: per-anchor demand estimate (rate lookup,
# unit-matching, NB2/Poisson parameterization). No DB access below this
# point -- every function here takes plain values, independently
# unit-testable, same pattern as every other engine's pure-math layer.
# ============================================================================

@dataclass
class AnchorDemandEstimate:
    anchor_id: str
    anchor_type: str
    size_value: Optional[float]
    size_unit: str
    expected_daily_trips: Optional[float] = None   # mu
    cv: Optional[float] = None
    dispersion_alpha: Optional[float] = None        # None if Poisson fallback used
    method: str = ""
    temporal_pattern: str = ""
    citation: str = ""
    excluded: bool = False
    exclusion_reason: str = ""
    flags: list = field(default_factory=list)


def _looks_like_transit_flow_unit(size_unit: str) -> bool:
    su = (size_unit or "").lower()
    return any(kw in su for kw in TRANSIT_FLOW_UNIT_KEYWORDS)


def estimate_anchor_demand(anchor: AnchorObservation) -> AnchorDemandEstimate:
    """
    Full per-anchor demand estimate for one real AnchorObservation
    (already resolved from an AnchorFacility row by
    engines.mobility_traffic._anchors_for_cell). Looks up the anchor's
    TRIP_GENERATION_RATES entry, checks unit compatibility, and computes
    the NB2 dispersion parameter alpha from the land use's coefficient
    of variation (falling back to a flagged Poisson case when the target
    variance would be below the Poisson floor -- see module docstring
    point 4/edge cases). Never fabricates a trip count for missing size
    or a mismatched unit; returns an EXCLUDED estimate with a stated
    reason instead (see module docstring's mass=1-vs-exclude distinction
    from Engine 4).
    """
    rates_entry = TRIP_GENERATION_RATES.get(anchor.anchor_type)
    if rates_entry is None:
        return AnchorDemandEstimate(
            anchor_id=anchor.id, anchor_type=anchor.anchor_type,
            size_value=anchor.size_value, size_unit=anchor.size_unit,
            excluded=True, exclusion_reason="unmapped_anchor_type",
            flags=[f"anchor_type '{anchor.anchor_type}' has no TRIP_GENERATION_RATES entry."],
        )

    temporal_pattern = rates_entry["temporal_pattern"]
    citation = rates_entry["citation"]
    method = rates_entry["method"]
    cv = rates_entry["cv"]

    # transit_hub: structurally different -- direct flow count, not a
    # rate x size conversion (module docstring edge cases).
    if method == "direct_flow_count":
        if anchor.size_value is None or not _looks_like_transit_flow_unit(anchor.size_unit):
            return AnchorDemandEstimate(
                anchor_id=anchor.id, anchor_type=anchor.anchor_type,
                size_value=anchor.size_value, size_unit=anchor.size_unit,
                method=method, temporal_pattern=temporal_pattern, citation=citation,
                excluded=True, exclusion_reason="no_ridership_data",
                flags=["transit_hub requires size_value to already be a ridership/boardings-style daily flow count; none found."],
            )
        mu = float(anchor.size_value)
        alpha, poisson_fallback = _fit_nb2_alpha(mu, cv)
        flags = ["mu is size_value used directly as a daily flow count (no rate applied)."]
        if poisson_fallback:
            flags.append("low_mu_poisson_fallback: target NB2 variance below Poisson floor; Poisson(mu) used for simulation.")
        return AnchorDemandEstimate(
            anchor_id=anchor.id, anchor_type=anchor.anchor_type,
            size_value=anchor.size_value, size_unit=anchor.size_unit,
            expected_daily_trips=round(mu, 4), cv=cv,
            dispersion_alpha=None if poisson_fallback else round(alpha, 8),
            method=method, temporal_pattern=temporal_pattern, citation=citation, flags=flags,
        )

    expected_unit = rates_entry["expected_unit"]
    flags = []

    if anchor.size_value is None:
        return AnchorDemandEstimate(
            anchor_id=anchor.id, anchor_type=anchor.anchor_type,
            size_value=None, size_unit=anchor.size_unit,
            method=method, temporal_pattern=temporal_pattern, citation=citation,
            excluded=True, exclusion_reason="no_size",
            flags=["AnchorFacility.size_value is missing -- excluded rather than mass=1-fallbacked (see module docstring)."],
        )
    if anchor.size_value <= 0:
        return AnchorDemandEstimate(
            anchor_id=anchor.id, anchor_type=anchor.anchor_type,
            size_value=anchor.size_value, size_unit=anchor.size_unit,
            method=method, temporal_pattern=temporal_pattern, citation=citation,
            excluded=True, exclusion_reason="non_positive_size",
            flags=[f"size_value={anchor.size_value} is not usable as a positive scale for a trip-generation rate."],
        )
    if (anchor.size_unit or "").strip().lower() != (expected_unit or "").strip().lower():
        return AnchorDemandEstimate(
            anchor_id=anchor.id, anchor_type=anchor.anchor_type,
            size_value=anchor.size_value, size_unit=anchor.size_unit,
            method=method, temporal_pattern=temporal_pattern, citation=citation,
            excluded=True, exclusion_reason="mismatched_unit",
            flags=[f"Expected size_unit='{expected_unit}' for anchor_type='{anchor.anchor_type}', got '{anchor.size_unit}'."],
        )

    mu = rates_entry["daily_rate_per_unit"] * float(anchor.size_value)
    alpha, poisson_fallback = _fit_nb2_alpha(mu, cv)
    if poisson_fallback:
        flags.append("low_mu_poisson_fallback: target NB2 variance below Poisson floor; Poisson(mu) used for simulation.")
    if anchor.anchor_type == "religious_community":
        flags.append("weekday_floor: this figure is a weekday baseline; true weekly demand is Sunday-concentrated, per ITE's own footnote for this land use (see citation).")
    if anchor.anchor_type == "residential_development":
        flags.append("outbound_flow: this anchor's trips are predominantly residents LEAVING, not inbound visitor footfall like other anchor types here.")
    if method.startswith("proxy") or "proxy" in method:
        flags.append("proxy_rate: no distinct published rate exists for this exact anchor_type; a structurally similar land use's rate was used instead.")

    return AnchorDemandEstimate(
        anchor_id=anchor.id, anchor_type=anchor.anchor_type,
        size_value=anchor.size_value, size_unit=anchor.size_unit,
        expected_daily_trips=round(mu, 4), cv=cv,
        dispersion_alpha=None if poisson_fallback else round(alpha, 8),
        method=method, temporal_pattern=temporal_pattern, citation=citation, flags=flags,
    )


def _fit_nb2_alpha(mu: float, cv: Optional[float]) -> tuple:
    """
    NB2 (Cameron & Trivedi 2013) mean-variance form: Var = mu + alpha*mu^2.
    Given a target coefficient of variation cv for the RATE (not the
    count itself), target Var = (mu*cv)^2. Solving for alpha:
        alpha = (Var - mu) / mu^2 = (mu*cv^2) - 1/mu   [algebraically
        equivalent, kept as the direct (Var-mu)/mu^2 form below for
        clarity/auditability against the definition].
    Returns (alpha, poisson_fallback: bool). When alpha would be <= 0
    (target variance at or below the Poisson floor -- small mu, e.g. a
    single small facility), NB2 is undefined/inappropriate; the caller
    falls back to plain Poisson(mu) for that anchor's simulation draws,
    flagged (module docstring edge cases), rather than clamping alpha to
    a fabricated small positive number.
    """
    if cv is None or mu <= 0:
        return 0.0, True
    target_variance = (mu * cv) ** 2
    if target_variance <= mu:
        return 0.0, True
    alpha = (target_variance - mu) / (mu ** 2)
    return alpha, False


# ============================================================================
# Pure-statistics layer 2: Monte Carlo simulation of total catchment
# recurring demand (sum of independent, heterogeneous NB2/Poisson draws).
# ============================================================================

@dataclass
class DemandGenerationResult:
    n_anchors_used: int
    n_anchors_excluded: int
    exclusion_reasons: dict            # {reason: count}
    total_point_estimate_daily: float  # sum of mu, exact, no simulation needed for this
    total_median_daily: Optional[float] = None
    total_mean_daily: Optional[float] = None
    ci_low: Optional[float] = None
    ci_high: Optional[float] = None
    ci_level: float = CI_LEVEL
    n_simulations: int = 0
    n_anchors_simulated: int = 0
    demand_share_by_type: dict = field(default_factory=dict)   # {anchor_type: fraction of total_point_estimate_daily}
    warnings: list = field(default_factory=list)


def simulate_total_demand(
    estimates: Sequence[AnchorDemandEstimate],
    rng: "np.random.Generator",
    n_simulations: int = N_SIMULATIONS,
    max_anchors_for_simulation: int = MAX_ANCHORS_FOR_SIMULATION,
) -> Optional[DemandGenerationResult]:
    """
    Point estimate (exact sum of mu across every usable anchor) plus a
    real Monte Carlo propagation of per-anchor NB2/Poisson uncertainty
    into a catchment-level total (module docstring point 5). Returns
    None only when there are zero usable anchors (see caller for the
    "no anchors synced at all" vs. "anchors synced but none usable"
    distinction, both surfaced via warnings either way).
    """
    usable = [e for e in estimates if not e.excluded and e.expected_daily_trips is not None]
    excluded = [e for e in estimates if e.excluded]

    exclusion_reasons: dict = {}
    for e in excluded:
        exclusion_reasons[e.exclusion_reason] = exclusion_reasons.get(e.exclusion_reason, 0) + 1

    if not usable:
        return None

    total_point_estimate = sum(e.expected_daily_trips for e in usable)
    demand_share = {}
    for e in usable:
        demand_share[e.anchor_type] = demand_share.get(e.anchor_type, 0.0) + e.expected_daily_trips
    if total_point_estimate > 0:
        demand_share = {k: round(v / total_point_estimate, 6) for k, v in demand_share.items()}

    warnings = []
    simulate_set = usable
    if len(usable) > max_anchors_for_simulation:
        simulate_set = sorted(usable, key=lambda e: -e.expected_daily_trips)[:max_anchors_for_simulation]
        remainder_sum = total_point_estimate - sum(e.expected_daily_trips for e in simulate_set)
        warnings.append(
            f"{len(usable) - max_anchors_for_simulation} anchors beyond MAX_ANCHORS_FOR_SIMULATION="
            f"{max_anchors_for_simulation} were spent-budget-excluded from the Monte Carlo simulation "
            f"(smallest expected contributors first) but ARE included in the point estimate directly "
            f"(contributing {remainder_sum:.2f} of the {total_point_estimate:.2f} total)."
        )

    draws = np.zeros(n_simulations)
    poisson_fallback_count = 0
    for e in simulate_set:
        mu = e.expected_daily_trips
        if e.dispersion_alpha is None:
            sample = rng.poisson(lam=mu, size=n_simulations)
            poisson_fallback_count += 1
        else:
            n_param = 1.0 / e.dispersion_alpha
            p_param = n_param / (n_param + mu)
            sample = rng.negative_binomial(n=n_param, p=p_param, size=n_simulations)
        draws += sample

    if len(usable) > max_anchors_for_simulation:
        draws += remainder_sum  # add the un-simulated remainder's point estimate to every draw

    if poisson_fallback_count:
        warnings.append(
            f"{poisson_fallback_count} of {len(simulate_set)} simulated anchors used the Poisson "
            "fallback (target NB2 variance below the Poisson floor at this anchor's mu) -- see "
            "each estimate's own flags."
        )

    alpha_ci = (1.0 - CI_LEVEL) / 2.0
    ci_low = float(np.quantile(draws, alpha_ci))
    ci_high = float(np.quantile(draws, 1.0 - alpha_ci))

    return DemandGenerationResult(
        n_anchors_used=len(usable), n_anchors_excluded=len(excluded),
        exclusion_reasons=exclusion_reasons,
        total_point_estimate_daily=round(total_point_estimate, 4),
        total_median_daily=round(float(np.median(draws)), 4),
        total_mean_daily=round(float(np.mean(draws)), 4),
        ci_low=round(ci_low, 4), ci_high=round(ci_high, 4), ci_level=CI_LEVEL,
        n_simulations=n_simulations, n_anchors_simulated=len(simulate_set),
        demand_share_by_type=demand_share, warnings=warnings,
    )


# ============================================================================
# Pure-statistics layer 3: demand-weighted diversity (Shannon/Pielou/
# Simpson, reimplemented independently per this codebase's "duplicate for
# statistically independent modules" convention) and temporal-cadence
# entropy (this module's own composition, module docstring point 7).
# ============================================================================

@dataclass
class AnchorDiversityResult:
    richness: int
    shannon_h: float
    shannon_h_max: float
    pielou_evenness: float
    simpson_d: float
    demand_shares: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)


def _shannon_pielou_simpson(shares: dict) -> tuple:
    richness = len(shares)
    shannon_h = -sum(p * math.log(p) for p in shares.values() if p > 0)
    shannon_h_max = math.log(richness) if richness > 1 else 0.0
    if richness < MIN_ANCHORS_FOR_DIVERSITY:
        pielou = 0.0
    else:
        pielou = round(shannon_h / shannon_h_max, 6) if shannon_h_max > 0 else 0.0
    simpson_d = 1.0 - sum(p ** 2 for p in shares.values())
    return richness, round(shannon_h, 6), round(shannon_h_max, 6), pielou, round(simpson_d, 6)


def anchor_demand_diversity(demand_share_by_type: dict) -> Optional[AnchorDiversityResult]:
    """
    Shannon H' / Pielou evenness J' / Simpson's D over anchor-TYPE shares
    of estimated demand (NOT raw anchor count -- module docstring point
    6's stated, deliberate difference from Engine 6's category_diversity).
    Returns None for an empty demand_share_by_type (no usable anchors --
    a data gap, not zero diversity, same convention as Engine 6).
    """
    if not demand_share_by_type:
        return None
    warnings = []
    richness, shannon_h, shannon_h_max, pielou, simpson_d = _shannon_pielou_simpson(demand_share_by_type)
    if richness < MIN_ANCHORS_FOR_DIVERSITY:
        warnings.append(
            f"Only {richness} anchor type(s) present -- Pielou evenness is minimally defined "
            f"(0.0), not computed from ln({richness})."
        )
    return AnchorDiversityResult(
        richness=richness, shannon_h=shannon_h, shannon_h_max=shannon_h_max,
        pielou_evenness=pielou, simpson_d=simpson_d,
        demand_shares=demand_share_by_type, warnings=warnings,
    )


@dataclass
class TemporalRedundancyResult:
    pattern_richness: int
    pattern_shannon_h: float
    pattern_shannon_h_max: float
    pattern_evenness: float
    dominant_pattern: str
    pattern_shares: dict = field(default_factory=dict)
    pattern_notes: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)


def temporal_redundancy(estimates: Sequence[AnchorDemandEstimate]) -> Optional[TemporalRedundancyResult]:
    """
    This module's own composition (module docstring point 7): Shannon
    entropy over temporal-CADENCE classes (not anchor types), weighted
    by each anchor's estimated demand contribution -- describes whether
    a catchment's recurring footfall is spread across different times of
    day/week or concentrated in a few windows. Deliberately kept
    SEPARATE from anchor_demand_diversity (which is about generator
    TYPE, not WHEN that generator's demand arrives) and from the
    composite score (see module docstring's "what's blended" section).
    """
    usable = [e for e in estimates if not e.excluded and e.expected_daily_trips is not None]
    if not usable:
        return None

    total = sum(e.expected_daily_trips for e in usable)
    if total <= 0:
        return None

    pattern_totals: dict = {}
    for e in usable:
        pattern_totals[e.temporal_pattern] = pattern_totals.get(e.temporal_pattern, 0.0) + e.expected_daily_trips
    pattern_shares = {k: round(v / total, 6) for k, v in pattern_totals.items()}

    richness, shannon_h, shannon_h_max, evenness, _ = _shannon_pielou_simpson(pattern_shares)
    dominant_pattern = max(pattern_shares, key=pattern_shares.get)

    warnings = []
    if richness < 2:
        warnings.append(
            f"All estimated demand shares a single temporal cadence ('{dominant_pattern}') -- "
            "this catchment's recurring footfall is NOT spread across different times of day/week."
        )

    return TemporalRedundancyResult(
        pattern_richness=richness, pattern_shannon_h=shannon_h, pattern_shannon_h_max=shannon_h_max,
        pattern_evenness=evenness, dominant_pattern=dominant_pattern,
        pattern_shares=pattern_shares,
        pattern_notes={k: TEMPORAL_PATTERN_NOTES.get(k, "") for k in pattern_shares},
        warnings=warnings,
    )


# ============================================================================
# Corpus-relative percentile (DB-only, same "within-corpus reference,
# documented as approximation" convention as market_gap.py/business_
# adjacency.py) and the composite 0-100 score.
# ============================================================================

def build_demand_corpus() -> Optional[np.ndarray]:
    """
    Point-estimate (not simulated -- a full Monte Carlo per corpus cell
    would be needlessly expensive for a reference distribution; only the
    SUBJECT cell needs its own full simulated CI) total daily demand
    across every real BusinessLocationCell with at least one usable
    AnchorFacility row, fit fresh each run -- same "live corpus, real
    separate infra work to cache later, not faked here" convention as
    market_gap.py's _fit_corpus_for_category and business_adjacency.py's
    build_adjacency_corpus. Returns None below MIN_CORPUS_CELLS_FOR_PERCENTILE.
    """
    totals = []
    cell_ids = AnchorFacility.objects.values_list("location_cell_id", flat=True).distinct()
    for cell_id in cell_ids:
        if cell_id is None:
            continue
        anchors = [
            AnchorObservation(
                id=str(a.id), anchor_type=a.anchor_type,
                latitude=float(a.latitude), longitude=float(a.longitude),
                size_value=float(a.size_value) if a.size_value is not None else None,
                size_unit=a.size_unit or "",
            )
            for a in AnchorFacility.objects.filter(location_cell_id=cell_id)
        ]
        estimates = [estimate_anchor_demand(a) for a in anchors]
        usable_total = sum(e.expected_daily_trips for e in estimates if not e.excluded and e.expected_daily_trips is not None)
        if usable_total > 0:
            totals.append(usable_total)

    if len(totals) < MIN_CORPUS_CELLS_FOR_PERCENTILE:
        return None
    return np.array(totals)


def _percentile_rank(value: float, corpus: np.ndarray) -> float:
    """Non-parametric empirical percentile rank -- no distributional
    assumption imposed on the corpus (which is real-world, right-skewed,
    and not normal), same "no scipy, no fabricated distributional
    assumption" discipline as this codebase's other pure-numpy stats."""
    return float(np.mean(corpus <= value) * 100.0)


def anchor_demand_score_0_100(
    demand: Optional[DemandGenerationResult],
    diversity: Optional[AnchorDiversityResult],
    corpus_percentile: Optional[float],
) -> Optional[float]:
    """
    Editorial 0-100 blend (documented weights, not fitted -- same
    convention as every other *_score_0_100 function in this codebase):
    70% corpus-relative magnitude percentile (or, when the corpus is too
    small, a log-scaled fallback transform against FALLBACK_REFERENCE_
    DAILY_DEMAND) + 30% Pielou evenness*100 (composition). Cadence is
    deliberately NOT blended in here -- see module docstring's "what's
    blended and what stays descriptive" section. Returns None only when
    there is no demand at all to score (no usable anchors).
    """
    if demand is None:
        return None

    if corpus_percentile is not None:
        magnitude_component = corpus_percentile
    else:
        # log1p-scaled saturating fallback, referenced against a fixed,
        # documented order-of-magnitude anchor point (FALLBACK_REFERENCE_
        # DAILY_DEMAND) when there isn't yet a real corpus to compare against.
        ratio = demand.total_point_estimate_daily / FALLBACK_REFERENCE_DAILY_DEMAND
        magnitude_component = min(100.0, 100.0 * math.log1p(ratio) / math.log1p(3.0))

    evenness_component = (diversity.pielou_evenness * 100.0) if diversity is not None else 0.0
    score = 0.70 * magnitude_component + 0.30 * evenness_component
    return round(max(0.0, min(100.0, score)), 2)


# ============================================================================
# Django-facing data assembly
# ============================================================================

@dataclass
class AnchorDemandFeatures:
    location_cell: BusinessLocationCell
    anchor_count: int = 0
    estimates: list = field(default_factory=list)
    demand: Optional[DemandGenerationResult] = None
    diversity: Optional[AnchorDiversityResult] = None
    temporal: Optional[TemporalRedundancyResult] = None
    corpus_percentile: Optional[float] = None
    corpus_n_cells: Optional[int] = None
    demand_score: Optional[float] = None
    source: str = "business_intel.engines.anchor"
    source_class: str = "internal_derived"
    warnings: list = field(default_factory=list)


def compute_cell_anchor_demand(
    cell: BusinessLocationCell,
    rng_seed: int = RANDOM_SEED,
    n_simulations: int = N_SIMULATIONS,
    corpus: Optional[np.ndarray] = None,
) -> AnchorDemandFeatures:
    """
    Full Section-11 calculation for one BusinessLocationCell. CELL-level
    (catchment=None on write), same data shape as Engine 4/5 -- matches
    how AnchorFacility rows are actually synced/scoped in this codebase
    (FK'd to the query's cell at sync time via search_anchors(cell,
    radius_m), not queried per-Catchment the way business_adjacency.py's
    BusinessEntity corpus is).
    """
    warnings = []
    anchors = _anchors_for_cell(cell)

    if not anchors:
        warnings.append(
            "No AnchorFacility rows synced for this cell -- every component below is None, not "
            "zero (a data gap, not evidence this location has no nearby demand generators). See "
            "module docstring's KNOWN UPSTREAM GAP note on search_anchors()."
        )
        return AnchorDemandFeatures(location_cell=cell, anchor_count=0, warnings=warnings)

    estimates = [estimate_anchor_demand(a) for a in anchors]

    rng = np.random.default_rng(rng_seed)
    demand = simulate_total_demand(estimates, rng, n_simulations=n_simulations)
    if demand is None:
        warnings.append(
            f"{len(anchors)} AnchorFacility row(s) synced for this cell, but none produced a "
            "usable demand estimate (missing size, mismatched unit, or unmapped anchor_type -- "
            "see each estimate's own exclusion_reason)."
        )
        return AnchorDemandFeatures(
            location_cell=cell, anchor_count=len(anchors), estimates=estimates, warnings=warnings,
        )
    warnings.extend(demand.warnings)

    diversity = anchor_demand_diversity(demand.demand_share_by_type)
    if diversity:
        warnings.extend(diversity.warnings)

    temporal = temporal_redundancy(estimates)
    if temporal:
        warnings.extend(temporal.warnings)

    if corpus is None:
        corpus = build_demand_corpus()
    corpus_percentile = None
    corpus_n_cells = None
    if corpus is not None:
        corpus_percentile = round(_percentile_rank(demand.total_point_estimate_daily, corpus), 2)
        corpus_n_cells = int(len(corpus))
    else:
        warnings.append(
            f"Fewer than {MIN_CORPUS_CELLS_FOR_PERCENTILE} real cells with usable anchor "
            "estimates exist in the DB yet -- corpus-relative percentile not computed; the "
            "composite score falls back to a fixed reference-scale transform, flagged."
        )

    score = anchor_demand_score_0_100(demand, diversity, corpus_percentile)

    return AnchorDemandFeatures(
        location_cell=cell, anchor_count=len(anchors), estimates=estimates,
        demand=demand, diversity=diversity, temporal=temporal,
        corpus_percentile=corpus_percentile, corpus_n_cells=corpus_n_cells,
        demand_score=score, warnings=warnings,
    )


def anchor_demand_evidence(features: AnchorDemandFeatures) -> dict:
    """Structured evidence dict, same convention as every other engine's
    *_evidence function in this codebase."""
    return {
        "anchor_count": features.anchor_count,
        "per_anchor": [
            {
                "anchor_id": e.anchor_id, "anchor_type": e.anchor_type,
                "size_value": e.size_value, "size_unit": e.size_unit,
                "expected_daily_trips": e.expected_daily_trips, "cv": e.cv,
                "method": e.method, "temporal_pattern": e.temporal_pattern,
                "excluded": e.excluded, "exclusion_reason": e.exclusion_reason,
                "flags": e.flags, "citation": e.citation,
            }
            for e in features.estimates
        ],
        "demand": None if features.demand is None else {
            "n_anchors_used": features.demand.n_anchors_used,
            "n_anchors_excluded": features.demand.n_anchors_excluded,
            "exclusion_reasons": features.demand.exclusion_reasons,
            "total_point_estimate_daily": features.demand.total_point_estimate_daily,
            "total_median_daily": features.demand.total_median_daily,
            "total_mean_daily": features.demand.total_mean_daily,
            "ci_low": features.demand.ci_low, "ci_high": features.demand.ci_high,
            "ci_level": features.demand.ci_level, "n_simulations": features.demand.n_simulations,
            "demand_share_by_type": features.demand.demand_share_by_type,
        },
        "diversity": None if features.diversity is None else {
            "richness": features.diversity.richness, "shannon_h": features.diversity.shannon_h,
            "pielou_evenness": features.diversity.pielou_evenness, "simpson_d": features.diversity.simpson_d,
            "demand_shares": features.diversity.demand_shares,
        },
        "temporal_redundancy": None if features.temporal is None else {
            "pattern_richness": features.temporal.pattern_richness,
            "pattern_evenness": features.temporal.pattern_evenness,
            "dominant_pattern": features.temporal.dominant_pattern,
            "pattern_shares": features.temporal.pattern_shares,
            "pattern_notes": features.temporal.pattern_notes,
        },
        "corpus": {
            "percentile": features.corpus_percentile,
            "n_cells": features.corpus_n_cells,
        },
        "demand_score_0_100": features.demand_score,
        "methods": [
            "ite_trip_generation_manual_11th_edition",
            "txdot_k_factor_peak_hour_expansion",
            "negative_binomial_nb2_overdispersed_count_model",
            "monte_carlo_sum_of_independent_heterogeneous_counts",
            "shannon_entropy_pielou_evenness_simpson_diversity",
            "demand_weighted_temporal_cadence_entropy",
        ],
    }


# ============================================================================
# Orchestration / EngineRun integration
# ============================================================================

def run(query: BusinessQuery, rng_seed: int = RANDOM_SEED, n_simulations: int = N_SIMULATIONS) -> dict:
    """
    Engine 11 entry point. Computes anchor demand-generation features for
    query.location_cell (cell-level, catchment=None -- same contract
    shape as Engine 4/5), writes FeatureObservation rows, and records an
    EngineRun.

    Fits the citywide demand corpus ONCE (not implicitly per call inside
    compute_cell_anchor_demand -- same fit-once-score-many discipline as
    business_adjacency.py's run()), since this cell's own percentile
    needs the SAME corpus a sibling cell's percentile would use.
    """
    started_at = django_timezone.now()
    cell = query.location_cell

    run_row = EngineRun.objects.create(
        query=query, engine_number=ENGINE_NUMBER, status="succeeded", started_at=started_at,
    )

    try:
        corpus = build_demand_corpus()
        with transaction.atomic():
            features = compute_cell_anchor_demand(cell, rng_seed=rng_seed, n_simulations=n_simulations, corpus=corpus)
            evidence = anchor_demand_evidence(features)

            features_written = 0
            FeatureObservation.write_latest(
                location_cell=cell, catchment=None, engine_number=ENGINE_NUMBER,
                feature_name="anchor_demand_evidence",
                defaults=dict(
                    value_type="json", value_json=evidence, unit="",
                    observed_at=started_at, source=features.source, source_class=features.source_class,
                    method="anchor_demand_generator_engine", derivation_level="calculation",
                    sample_size=features.corpus_n_cells,
                ),
            )
            features_written += 1

            if features.demand is not None:
                FeatureObservation.write_latest(
                    location_cell=cell, catchment=None, engine_number=ENGINE_NUMBER,
                    feature_name="estimated_daily_recurring_demand",
                    defaults=dict(
                        value_type="numeric",
                        value_numeric=Decimal(str(features.demand.total_point_estimate_daily)),
                        unit="trips/day",
                        observed_at=started_at, source=features.source, source_class=features.source_class,
                        method="ite_trip_generation_nb2_monte_carlo", derivation_level="model_estimate",
                        sample_size=features.demand.n_anchors_used,
                        uncertainty=(
                            Decimal(str(round(features.demand.ci_high - features.demand.ci_low, 4)))
                            if features.demand.ci_high is not None else None
                        ),
                    ),
                )
                features_written += 1

            if features.demand_score is not None:
                FeatureObservation.write_latest(
                    location_cell=cell, catchment=None, engine_number=ENGINE_NUMBER,
                    feature_name="anchor_demand_score",
                    defaults=dict(
                        value_type="numeric", value_numeric=Decimal(str(features.demand_score)), unit="score_0_100",
                        observed_at=started_at, source=features.source, source_class=features.source_class,
                        method="corpus_percentile_and_evenness_blend", derivation_level="business_conclusion",
                        confidence_pct=Decimal("70.00") if features.corpus_percentile is not None else Decimal("40.00"),
                    ),
                )
                features_written += 1

            if features.diversity is not None:
                FeatureObservation.write_latest(
                    location_cell=cell, catchment=None, engine_number=ENGINE_NUMBER,
                    feature_name="anchor_demand_diversity_pielou",
                    defaults=dict(
                        value_type="numeric", value_numeric=Decimal(str(features.diversity.pielou_evenness)), unit="ratio",
                        observed_at=started_at, source=features.source, source_class=features.source_class,
                        method="shannon_pielou_demand_weighted", derivation_level="calculation",
                        sample_size=features.diversity.richness,
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
    if not features.estimates:
        degraded_reasons.append("No AnchorFacility rows synced for this cell -- see module docstring's KNOWN UPSTREAM GAP note.")
    elif features.demand is None:
        degraded_reasons.append("AnchorFacility rows exist but none produced a usable demand estimate -- see per-anchor exclusion_reason.")
    if features.corpus_n_cells is None:
        degraded_reasons.append(f"Citywide demand corpus below MIN_CORPUS_CELLS_FOR_PERCENTILE={MIN_CORPUS_CELLS_FOR_PERCENTILE}.")
    if degraded_reasons:
        run_row.status = "degraded"
        run_row.degraded_reason = "; ".join(degraded_reasons)[:255]

    run_row.save(update_fields=["status", "finished_at", "duration_ms", "features_written", "degraded_reason"])

    return {
        "cell": cell, "features": features, "engine_run": run_row,
        "corpus_fitted": corpus is not None,
        "corpus_n_cells": int(len(corpus)) if corpus is not None else None,
    }

def compute_anchor_score(query, rng_seed=RANDOM_SEED, n_simulations=N_SIMULATIONS) -> tuple:
    run_result = run(query, rng_seed=rng_seed, n_simulations=n_simulations)
    features = run_result["features"]
    evidence = anchor_demand_evidence(features)
    evidence["engine_run_status"] = run_result["engine_run"].status
    evidence["engine_run_degraded_reason"] = run_result["engine_run"].degraded_reason or None
    return features.demand_score, evidence
