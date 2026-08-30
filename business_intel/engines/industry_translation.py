"""
business_intel/engines/industry_translation.py

Engine 22 -- Industry Translation Engine
Per business_location_intelligence_engines_data_models_v2.md, Section 22.

NOTE ON THE SPEC FILE: same situation as every other engine in this
codebase -- business_location_intelligence_engines_data_models_v2.md is
referenced but not present on disk. Built to match Engine 9/14/24/26 for
structure, schema contract, and citation discipline. The stub this
replaced gave only a purpose line: "Translate general location
intelligence into the requirements of a particular business."

=============================================================================
WHAT THIS ENGINE ACTUALLY DOES, AND WHY IT'S NEEDED
=============================================================================
Every engine built so far falls into one of two buckets:

  (a) CATEGORY-AGNOSTIC facts about the place itself -- population
      density (2), purchasing power (3), mobility/forced-traffic
      accessibility (4/5), ecosystem vitality (6), business survival (7),
      business-mix complementarity (10), anchor demand (11), commercial
      occupancy (14), economic momentum (18). These describe the
      LOCATION, not any particular business. They are deliberately
      written this way -- see the Population conversation this engine
      follows up on: population should stay calculation-only and
      category-agnostic because "a bakery and a hardware store don't
      want the same density profile" is a judgment call, not a fact,
      and baking that judgment into Engine 2 would make it lie for
      every category it wasn't tuned for.

  (b) CATEGORY-SPECIFIC facts, but only two of them: competition (8) and
      market gap (9), which are inherently about a specific
      business_category by construction (you can't measure "competition"
      without naming what's competing).

Nothing in the pipeline currently says how much each category-agnostic
fact should MATTER to a specific business_category, and nothing
identifies category-specific angles the raw facts don't surface on their
own (e.g. "the #1 missing complement in this ecosystem happens to be
exactly your category"). That is this engine's whole job: take what's
already known about the LOCATION and reframe it for the QUESTION being
asked (query.business_category), so Engine 26 (Decision) has a genuine
category-aware criterion to rank on instead of only the two
category-scoped inputs Engines 8/9 already give it.

=============================================================================
WHAT'S ACTUALLY AVAILABLE VS. WHAT ISN'T
=============================================================================
Real data used: models.FeatureObservation rows already written by Engines
2/3/4/5/6/7/8/9/10/11/14/18 for query.location_cell, filtered to
is_latest=True, numeric, and scoped per that engine's own contract
(cell-level with catchment=None for Engines 4/5/8/11/18 -- confirmed by
reading their own run() docstrings, not guessed -- broadcast onto every
catchment being scored, same pattern decision.py's
_discover_criteria_matrix already established; catchment-level for
Engines 2/3/6/7/9/10/14).

This engine reads OTHER engines' persisted output, unlike Engines 1-21
which each depend on business_intel.models only. That is intentional,
not a violation of the per-engine independent-footprint convention:
Engine 22 sits in the same "meta" layer as Engine 24 (Confidence) and
Engine 26 (Decision), which both read cross-engine FeatureObservation
rows by design. It is simply not counted in decision.py/confidence.py's
own META_ENGINE_NUMBERS = {23, 24, 25, 26, 27} set, which means its own
category_fit_score_0_100 output is -- correctly -- visible to Engine 26's
dynamic business_conclusion criteria discovery as an ordinary criterion,
the same way Engine 8/9's category-scoped scores already are.

What is explicitly NOT done, and why: this engine does NOT fit
category-importance weights to historical outcomes. There is no
BusinessReport outcome/ground-truth table in this codebase yet (no
"did this business survive/thrive" feedback loop -- the same absence
business_survival.py's own honest-limitations section flags for hazard
modeling). Inventing fitted-looking weights against data that doesn't
exist would be worse than being explicit about it. Instead, category
importance is a documented, hand-authored POLICY TABLE grounded in
real, cited retail-geography literature (see below) -- the same kind of
explicit, editable business-rule table CATEGORY_PLACES_TYPE_MAP already
is in models.py, not a statistical estimate. It is built to be replaced
by fitted weights the day outcome data exists, and says so.

=============================================================================
REAL METHODS USED (researched and cited, not invented)
=============================================================================
1. Copeland, M.T. (1923). "Relation of Consumers' Buying Habits to
   Marketing Methods." Harvard Business Review, 1(3), 282-289. Source of
   the classic convenience / shopping / specialty goods classification
   used here (GOODS_TYPE_CLASS) to group business_category values by how
   consumers actually shop for them, before any weight is assigned.

2. Nelson, R.L. (1958). The Selection of Retail Locations. New York:
   Dodge Corporation. Source of two of this engine's weighting
   principles: (a) "cumulative attraction" -- comparison/destination
   goods retailers (restaurants, boutiques, electronics) draw MORE
   combined trade when clustered near similar and complementary
   businesses than any one would alone, so competitive_favorability is
   DE-emphasized (not sign-flipped -- see limitation below) and
   complement_synergy is emphasized for those categories; and (b)
   "minimum competition" -- convenience/necessity retailers (pharmacy,
   supermarket, clinic) do best avoiding direct same-category
   competitors, since customers rarely comparison-shop for a
   prescription or a loaf of bread, so competitive_favorability is
   emphasized for those categories instead.

3. Reilly, W.J. (1931) / Huff, D.L. (1964) gravity-model trade-area
   literature (already the basis for market_gap.py's Poisson-gravity GLM
   and competition.py's Huff-model share) -- background justification
   for why destination/comparison categories draw from wider, less
   hyperlocal trade areas than convenience categories, informing why
   demand_scale (hyperlocal population density) is weighted down for
   specialty/shopping categories relative to purchasing_power and
   complement_synergy. Not independently re-implemented here (no new
   gravity model); cited only as the standing justification already
   established elsewhere in this codebase.

=============================================================================
KNOWN LIMITATIONS (stated, not hidden)
=============================================================================
- Weights are a policy table, not a fitted model. Two different, equally
  well-read retail geographers could reasonably disagree on the exact
  multipliers below. What is NOT arbitrary: the DIRECTION of every
  deviation from a flat weight is tied to a cited principle above, and
  every deviation is visible (not baked into an opaque score) in the
  category_dimension_profile FeatureObservation this engine writes.
- Nelson's cumulative-attraction principle would, in its strongest form,
  argue competitive_favorability should sometimes matter POSITIVELY
  (more same-category neighbors = more combined draw) for
  agglomeration-tolerant categories, not just less negatively. This
  engine deliberately does NOT flip the sign, only the weight -- with no
  calibration data on WHERE clustering stops helping and starts
  cannibalizing (the classic inverted-U agglomeration curve), reversing
  the sign would be a bigger, less honest assumption than just
  de-emphasizing it. Flagged, not implemented.
- business_adjacency.py (Engine 10) only persists the SINGLE top missing
  complement category per catchment, not a per-category score for every
  category. So complement_synergy can only be populated when
  query.business_category happens to BE that top-ranked category; every
  other case is correctly reported as "no signal available", not
  silently zero or imputed -- see complement_synergy handling below.
- Like every corpus-normalized feature in this codebase (see
  decision.py's _corpus_reference_frame), demand_scale's min-max scaling
  is only as good as how many cells this engine has ever scored. Early
  in the corpus's life, normalization is wide and noisy; this is
  reported via a warning, not hidden.

Depends on business_intel.models (FeatureObservation, EngineRun,
BusinessQuery, BusinessLocationCell, Catchment) and reads persisted
output from Engines 2/3/4/5/6/7/8/9/10/11/14/18. No external API calls.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional, Sequence

from django.db import transaction
from django.db.models import Min, Max
from django.utils import timezone as django_timezone

from ..models import (
    BusinessQuery, BusinessLocationCell, Catchment, EngineRun, FeatureObservation,
)

logger = logging.getLogger("business_intel.engines.industry_translation")

ENGINE_NUMBER = 22

# =============================================================================
# 1. Goods-type classification (Copeland 1923)
# =============================================================================
# One class per BUSINESS_CATEGORY_CHOICES value in models.py. "other" gets
# its own neutral class rather than being forced into one of the real
# ones -- we genuinely don't know what an "other" business needs.
GOODS_TYPE_CLASS = {
    "pharmacy": "convenience_necessity",
    "supermarket": "convenience_necessity",
    "clinic": "convenience_necessity",
    "bank_agent": "convenience_necessity",
    "bakery": "convenience_necessity",
    "salon_barbershop": "convenience_service",
    "gym": "specialty_subscription",
    "restaurant": "specialty_destination",
    "bar": "specialty_destination",
    "hardware_store": "shopping_comparison",
    "electronics_shop": "shopping_comparison",
    "boutique_clothing": "shopping_comparison",
    "other": "unclassified",
}

# =============================================================================
# 2. Dimensions this engine scores, and where each one's raw value comes from
# =============================================================================
# scope: "cell" (catchment=None, broadcast to every catchment being scored --
#        confirmed per-engine from each engine's own run() docstring, same
#        broadcast decision.py already implements) or "catchment".
# category_scoped: whether the upstream row is already written with
#        business_category=query.business_category (Engines 8/9 only).
DIMENSIONS = (
    "demand_scale", "purchasing_power", "footfall", "ecosystem_health",
    "competitive_favorability", "market_gap", "complement_synergy",
    "space_stability", "anchor_demand", "growth_momentum", "durability",
)

DIMENSION_SOURCES = {
    "demand_scale": [("population_density_per_km2", 2, "catchment", False)],
    "purchasing_power": [("composite_purchasing_power_index", 3, "catchment", False)],
    "footfall": [
        ("forced_traffic_score", 5, "cell", False),
        ("mobility_score", 4, "cell", False),
    ],
    "ecosystem_health": [("ecosystem_vitality_score", 6, "catchment", False)],
    "competitive_favorability": [("competition_score", 8, "cell", True)],
    "market_gap": [("market_gap_score", 9, "catchment", True)],
    "complement_synergy": [("top_missing_complement_score", 10, "catchment", False)],
    "space_stability": [("occupancy_stability_score", 14, "catchment", False)],
    "anchor_demand": [("anchor_demand_score", 11, "cell", False)],
    "growth_momentum": [("momentum_score", 18, "cell", False)],
    "durability": [("survival_rate_at_end_of_window", 7, "catchment", False)],
}

# =============================================================================
# 3. Weight policy table (Nelson 1958 principles applied by goods-type class)
# =============================================================================
# Base weight = 1 / len(DIMENSIONS) for every dimension. Each class below
# lists ONLY the multipliers that deviate from 1.0 (neutral); everything
# else defaults to 1.0. Final weights are renormalized to sum to 1.0.
CLASS_WEIGHT_MULTIPLIERS = {
    # Minimum-competition principle: avoid saturated convenience/necessity
    # markets. Demand is hyperlocal and footfall/passby-driven. Low
    # discretionary-spend sensitivity, low growth sensitivity (habitual,
    # stable demand once open).
    "convenience_necessity": {
        "demand_scale": 1.6, "footfall": 1.5, "competitive_favorability": 1.4,
        "purchasing_power": 0.7, "growth_momentum": 0.6, "complement_synergy": 0.7,
    },
    # Same family as convenience_necessity but discretionary personal-care
    # spend makes purchasing_power somewhat more relevant.
    "convenience_service": {
        "demand_scale": 1.3, "footfall": 1.2, "purchasing_power": 1.2,
        "competitive_favorability": 1.1, "growth_momentum": 0.7,
    },
    # Subscription/membership model: destination, not passby -- footfall
    # barely matters. Purchasing power and new-resident growth matter a
    # lot (new developments = new members). Head-to-head competition
    # matters less once a member is locked into a contract.
    "specialty_subscription": {
        "purchasing_power": 1.7, "demand_scale": 1.3, "growth_momentum": 1.4,
        "footfall": 0.4, "competitive_favorability": 0.8, "durability": 1.2,
    },
    # Cumulative-attraction principle: restaurant/bar rows and nightlife
    # districts draw more combined trade clustered than isolated, so
    # competitive_favorability is de-emphasized (not inverted -- see
    # module docstring) and complement_synergy is emphasized instead.
    # Evening/leisure footfall matters a lot; this engine has no separate
    # day-part signal to draw on, so footfall is used as the closest
    # available proxy.
    "specialty_destination": {
        "footfall": 1.5, "complement_synergy": 1.6, "competitive_favorability": 0.6,
        "growth_momentum": 1.3, "market_gap": 0.8, "ecosystem_health": 1.2,
    },
    # Cumulative-attraction / comparison-shopping clustering (the same
    # Reilly/Huff trade-area logic behind why hardware, electronics, and
    # clothing retailers co-locate): draws from a wider trade area than
    # hyperlocal density implies, tolerates same-category neighbors far
    # better than convenience goods, and benefits from a visibly stable
    # commercial strip to browse in.
    "shopping_comparison": {
        "complement_synergy": 1.6, "competitive_favorability": 0.6, "purchasing_power": 1.4,
        "space_stability": 1.4, "demand_scale": 0.9, "market_gap": 1.1,
    },
    # No classification basis -- flat weights, flagged as low-confidence
    # in the evidence dict.
    "unclassified": {},
}


def category_weights(category: str) -> dict:
    """Renormalized {dimension: weight} for one business_category, summing
    to 1.0. Deterministic policy lookup, not a statistical fit -- see
    module docstring."""
    goods_class = GOODS_TYPE_CLASS.get(category, "unclassified")
    multipliers = CLASS_WEIGHT_MULTIPLIERS.get(goods_class, {})
    base = 1.0 / len(DIMENSIONS)
    raw = {d: base * multipliers.get(d, 1.0) for d in DIMENSIONS}
    total = sum(raw.values())
    return {d: (v / total) for d, v in raw.items()}


# =============================================================================
# 4. Reading upstream FeatureObservation rows
# =============================================================================

def _corpus_minmax(feature_name: str, engine_number: int) -> Optional[dict]:
    """{"min":.., "max":..} across every is_latest row for this
    feature_name/engine in the whole database. Same corpus-normalization
    idea decision.py's _corpus_reference_frame already uses. Returns None
    (not a fabricated 0/100 range) if fewer than 2 distinct values are on
    record yet."""
    agg = FeatureObservation.objects.filter(
        feature_name=feature_name, engine_number=engine_number,
        is_latest=True, value_type="numeric",
    ).aggregate(lo=Min("value_numeric"), hi=Max("value_numeric"))
    if agg["lo"] is None or agg["hi"] is None or agg["lo"] == agg["hi"]:
        return None
    return {"min": float(agg["lo"]), "max": float(agg["hi"])}


def _cell_level_value(cell: BusinessLocationCell, feature_name: str, engine_number: int,
                       category: Optional[str] = None) -> Optional[float]:
    qs = FeatureObservation.objects.filter(
        location_cell=cell, catchment__isnull=True, feature_name=feature_name,
        engine_number=engine_number, is_latest=True, value_type="numeric",
    )
    if category is not None:
        qs = qs.filter(business_category=category)
    row = qs.first()
    return float(row.value_numeric) if row and row.value_numeric is not None else None


def _catchment_level_value(catchment: Catchment, feature_name: str, engine_number: int,
                            category: Optional[str] = None) -> Optional[float]:
    qs = FeatureObservation.objects.filter(
        catchment=catchment, feature_name=feature_name,
        engine_number=engine_number, is_latest=True, value_type="numeric",
    )
    if category is not None:
        qs = qs.filter(business_category=category)
    row = qs.first()
    return float(row.value_numeric) if row and row.value_numeric is not None else None


def _top_missing_complement(catchment: Catchment) -> tuple:
    """Returns (score_or_None, top_category_or_None). See module docstring's
    complement_synergy limitation -- Engine 10 only ranks ONE category."""
    cat_row = FeatureObservation.objects.filter(
        catchment=catchment, feature_name="top_missing_complement_category",
        engine_number=10, is_latest=True, value_type="categorical",
    ).first()
    score_row = FeatureObservation.objects.filter(
        catchment=catchment, feature_name="top_missing_complement_score",
        engine_number=10, is_latest=True, value_type="numeric",
    ).first()
    top_category = cat_row.value_text if cat_row else None
    score = float(score_row.value_numeric) if score_row and score_row.value_numeric is not None else None
    return score, top_category


@dataclass
class DimensionValue:
    dimension: str
    value: Optional[float]
    weight: float
    source_feature: str
    source_engine: int
    included: bool
    note: str = ""


@dataclass
class CategoryFitResult:
    catchment: Optional[Catchment]
    goods_type_class: str
    score_0_100: Optional[float]
    coverage_pct: Optional[float]
    n_dimensions_available: int
    dimension_values: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


def compute_category_fit(cell: BusinessLocationCell, catchment: Optional[Catchment],
                          category: str) -> CategoryFitResult:
    """Computes the category-weighted fit for one catchment (or the
    cell-only case when catchment is None -- callers should still pass a
    real Catchment for anything that reads catchment-scoped dimensions;
    None is only meaningful if every dimension for this category happened
    to be cell-level, which is not the case here, so `catchment` is
    effectively required in practice)."""
    weights = category_weights(category)
    goods_class = GOODS_TYPE_CLASS.get(category, "unclassified")
    warnings = []
    dim_values = []

    for dim in DIMENSIONS:
        sources = DIMENSION_SOURCES[dim]
        raw_value = None
        chosen_feature, chosen_engine = sources[0][0], sources[0][1]

        if dim == "demand_scale":
            feature_name, engine_number, scope, cat_scoped = sources[0]
            chosen_feature, chosen_engine = feature_name, engine_number
            raw = _catchment_level_value(catchment, feature_name, engine_number) if catchment else None
            if raw is not None:
                rng = _corpus_minmax(feature_name, engine_number)
                if rng is None:
                    warnings.append(
                        f"{dim}: fewer than 2 distinct '{feature_name}' values on record yet -- "
                        "corpus min-max normalization withheld."
                    )
                else:
                    raw_value = max(0.0, min(100.0, 100.0 * (raw - rng["min"]) / (rng["max"] - rng["min"])))

        elif dim == "footfall":
            vals = []
            for feature_name, engine_number, scope, cat_scoped in sources:
                v = _cell_level_value(cell, feature_name, engine_number)
                if v is not None:
                    vals.append(v)
            if vals:
                raw_value = sum(vals) / len(vals)
                chosen_feature = "+".join(f for f, *_ in sources)
                chosen_engine = 0  # blended, no single engine

        elif dim == "competitive_favorability":
            feature_name, engine_number, scope, cat_scoped = sources[0]
            chosen_feature, chosen_engine = feature_name, engine_number
            raw_value = _cell_level_value(cell, feature_name, engine_number, category=category)

        elif dim == "market_gap":
            feature_name, engine_number, scope, cat_scoped = sources[0]
            chosen_feature, chosen_engine = feature_name, engine_number
            raw_value = _catchment_level_value(catchment, feature_name, engine_number, category=category) if catchment else None

        elif dim == "complement_synergy":
            chosen_feature, chosen_engine = "top_missing_complement_score", 10
            if catchment is None:
                warnings.append(f"{dim}: no catchment to evaluate.")
            else:
                score, top_category = _top_missing_complement(catchment)
                if top_category is None:
                    warnings.append(f"{dim}: Engine 10 (business_adjacency) has no data for this catchment yet.")
                elif top_category != category:
                    warnings.append(
                        f"{dim}: Engine 10's top-ranked missing complement here is '{top_category}', not "
                        f"'{category}' -- Engine 10 only persists the single top-ranked gap per catchment, so "
                        f"no complement-synergy signal exists specifically for '{category}'. Not imputed as zero."
                    )
                else:
                    raw_value = score

        elif dim == "durability":
            feature_name, engine_number, scope, cat_scoped = sources[0]
            chosen_feature, chosen_engine = feature_name, engine_number
            raw = _catchment_level_value(catchment, feature_name, engine_number) if catchment else None
            raw_value = raw * 100.0 if raw is not None else None

        else:
            # ecosystem_health, space_stability (catchment) /
            # anchor_demand, growth_momentum (cell) -- plain lookups,
            # already on a 0-100 scale by this codebase's *_score_0_100
            # convention (see decision.py's own note on this).
            feature_name, engine_number, scope, cat_scoped = sources[0]
            chosen_feature, chosen_engine = feature_name, engine_number
            if scope == "cell":
                raw_value = _cell_level_value(cell, feature_name, engine_number)
            else:
                raw_value = _catchment_level_value(catchment, feature_name, engine_number) if catchment else None

        dim_values.append(DimensionValue(
            dimension=dim, value=raw_value, weight=weights[dim],
            source_feature=chosen_feature, source_engine=chosen_engine,
            included=raw_value is not None,
            note="" if raw_value is not None else "no upstream value available",
        ))

    available = [dv for dv in dim_values if dv.included]
    weight_available = sum(dv.weight for dv in available)
    if not available or weight_available <= 0:
        warnings.append("No upstream dimensions had usable data -- category_fit_score withheld.")
        return CategoryFitResult(
            catchment=catchment, goods_type_class=goods_class, score_0_100=None,
            coverage_pct=None, n_dimensions_available=0, dimension_values=dim_values, warnings=warnings,
        )

    weighted_sum = sum(dv.value * dv.weight for dv in available)
    score = weighted_sum / weight_available
    coverage_pct = 100.0 * weight_available  # weights always sum to 1.0 across all dims

    return CategoryFitResult(
        catchment=catchment, goods_type_class=goods_class,
        score_0_100=round(max(0.0, min(100.0, score)), 2),
        coverage_pct=round(coverage_pct, 2),
        n_dimensions_available=len(available),
        dimension_values=dim_values, warnings=warnings,
    )


def industry_translation_evidence(result: CategoryFitResult, category: str) -> dict:
    """Structured evidence dict, same convention as every other engine's
    *_evidence function in this codebase."""
    return {
        "business_category": category,
        "goods_type_class": result.goods_type_class,
        "category_fit_score_0_100": result.score_0_100,
        "coverage_pct": result.coverage_pct,
        "n_dimensions_available": result.n_dimensions_available,
        "n_dimensions_total": len(DIMENSIONS),
        "dimensions": [
            {
                "dimension": dv.dimension,
                "weight": round(dv.weight, 4),
                "value_0_100": dv.value,
                "source_feature": dv.source_feature,
                "source_engine": dv.source_engine,
                "included": dv.included,
                "note": dv.note,
            }
            for dv in result.dimension_values
        ],
        "warnings": result.warnings,
        "methods": [
            "copeland_1923_goods_type_classification",
            "nelson_1958_cumulative_attraction_and_minimum_competition_weighting",
            "corpus_minmax_normalization_for_raw_density_features",
        ],
        "known_simplifications": [
            "Weights are a hand-authored policy table grounded in cited retail-geography "
            "principles, not fitted to historical outcomes -- no BusinessReport outcome/"
            "ground-truth table exists yet in this codebase to fit against.",
            "Nelson's cumulative-attraction principle is applied only as a weight de-emphasis "
            "for agglomeration-tolerant categories, never as a sign flip on competitive_favorability "
            "-- there is no calibration data on where clustering stops helping and starts "
            "cannibalizing (the inverted-U agglomeration curve).",
            "complement_synergy can only be populated when query.business_category matches "
            "Engine 10's single top-ranked missing complement for the catchment -- Engine 10 "
            "does not persist a per-category score for every category.",
        ],
    }


# ============================================================================
# Django-facing entry point
# ============================================================================

def run(query: BusinessQuery, catchments: Optional[Sequence[Catchment]] = None) -> dict:
    """
    Engine 22 entry point. For query.location_cell and
    query.business_category, computes a category-weighted fit score for
    every Catchment belonging to the cell (or an explicit `catchments`
    subset), writes FeatureObservation rows, and records an EngineRun --
    same contract shape as every other engine.

    Requires at least Engine 1 (Spatial Context) to have run so
    catchments exist; benefits from as many of Engines 2/3/4/5/6/7/8/9/
    10/11/14/18 having already run for this cell as possible -- this
    engine degrades gracefully (fewer dimensions available, lower
    coverage_pct) rather than failing when some are missing, same
    "run against however much upstream evidence exists" discipline
    decision.py's criteria discovery already established.
    """
    started_at = django_timezone.now()
    cell = query.location_cell
    category = query.business_category
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
    all_results = []

    try:
        with transaction.atomic():
            for catchment in target_catchments:
                result = compute_category_fit(cell, catchment, category)
                all_results.append(result)
                warnings_seen.extend(result.warnings)
                evidence = industry_translation_evidence(result, category)

                if result.score_0_100 is not None:
                    FeatureObservation.write_latest(
                        location_cell=cell, catchment=catchment, engine_number=ENGINE_NUMBER,
                        feature_name="category_fit_score_0_100", business_category=category,
                        defaults=dict(
                            value_type="numeric", value_numeric=Decimal(str(result.score_0_100)), unit="score_0_100",
                            observed_at=started_at, source="internal_derived", source_class="internal_derived",
                            method="category_weighted_dimension_blend", derivation_level="business_conclusion",
                            sample_size=result.n_dimensions_available,
                            coverage_pct=Decimal(str(result.coverage_pct)) if result.coverage_pct is not None else None,
                        ),
                    )
                    features_written += 1

                FeatureObservation.write_latest(
                    location_cell=cell, catchment=catchment, engine_number=ENGINE_NUMBER,
                    feature_name="category_dimension_profile", business_category=category,
                    defaults=dict(
                        value_type="json", value_json=evidence, unit="",
                        observed_at=started_at, source="internal_derived", source_class="internal_derived",
                        method="nelson_1958_copeland_1923_weight_policy_table", derivation_level="calculation",
                    ),
                )
                features_written += 1

                FeatureObservation.write_latest(
                    location_cell=cell, catchment=catchment, engine_number=ENGINE_NUMBER,
                    feature_name="industry_translation_evidence", business_category=category,
                    defaults=dict(
                        value_type="json", value_json=evidence, unit="",
                        observed_at=started_at, source="internal_derived", source_class="internal_derived",
                        method="industry_translation_engine", derivation_level="calculation",
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

    return {"cell": cell, "category": category, "features": all_results, "engine_run": run_row}