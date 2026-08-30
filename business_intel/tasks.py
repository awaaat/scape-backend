"""
business_intel/tasks.py

Celery entry point: generate_business_report.

REWRITE NOTE (see PR description / commit message): this used to call only
5 of the 15 fully-built engines directly (spatial_context, population,
competition, market_gap, anchor) via their bare compute_*() functions, and
faked the rest of the pipeline: opportunity_level/recommendation came from
a naive average of 3 scores against hardcoded thresholds, and
confidence_score was a hardcoded `50`. 10 other fully-implemented engines
(purchasing_power, mobility_traffic, forced_traffic, business_ecosystem,
business_survival, business_adjacency, development_activity, evidence,
confidence, decision) were never imported anywhere in the codebase.

This version calls every built engine's real Django-facing run() entry
point (competition.py's run() was missing entirely and is added alongside
this change -- see engines/competition.py), so every engine actually
writes FeatureObservation + EngineRun rows, then lets Engine 23
(Evidence), Engine 24 (Confidence), and Engine 26 (Decision) -- all
already fully implemented, just never wired in -- do the auditing and
aggregation they were built to do, instead of tasks.py re-implementing a
cruder version of the same logic inline.

CORRECTION (second pass): Engine 23 (Evidence) was missed in the first
version of this rewrite -- every other engine got wired in, but Evidence
did not, even though confidence.py's own run() explicitly reads back
Engine 23's evidence_dossier FeatureObservation and degrades with a
"no_evidence_engine_output_falling_back_to_unshrunk" warning when it's
absent. That meant Confidence (and everything downstream of it) was
running in a silently degraded mode despite this file's own docstring
claiming the pipeline was now real. evidence.run(query) is now called
before confidence.run(query), since Confidence reads its output off the
DB rather than taking it as an argument.

Two things are deliberately NOT faked here, and are surfaced as risks
instead of silently working around them:

1. Engine 3 (Purchasing Power) is NOT run. It requires a
   CountyEconomicPriorProvider, and the only implementation that exists
   is purchasing_power.py's own SyntheticCountyEconomicPriorProvider,
   whose docstring says outright: "NOT real data -- must never back a
   production request." Running it anyway and hiding that behind a
   generic score would be exactly the kind of fake this rewrite exists
   to remove. It stays unwired until a real provider exists.

2. Engines 12 (Development Activity, partially built -- no run()),
   13-22, 25, 27 are still stubs or lack a persistence entry point, so
   they cannot contribute FeatureObservation rows for Engine 26 to
   discover. Engine 18 (Economic Momentum) in particular is why
   BusinessReport.momentum_summary / .timing are left blank below --
   there is no real signal to fill them with yet.

Every other built engine's run() is now called for real.
"""
import logging

from celery import shared_task
from django.utils import timezone

from . import services
from .google_client import (
    search_places_category,
    sync_places_to_business_entities,
    search_anchors,
    sync_places_to_anchor_facilities,
)
from .engines import (
    spatial_context,
    population,
    mobility_traffic,
    forced_traffic,
    business_ecosystem,
    business_survival,
    business_adjacency,
    competition,
    development_activity,
    market_gap,
    anchor,
    economic_momentum,
    evidence as evidence_engine,  # aliased: generate_business_report() also has a local
                                   # `evidence` list variable (-> report.evidence) that would
                                   # otherwise shadow this module for the rest of the function.
    confidence,
    decision,
)
# --- ADDITION: Import the anomaly detection runner ---
from .engines.anomaly_detection import run_anomaly_detection
# --- END ADDITION ---

from .engines.spatial_context import build_radius_catchments, DISTANCE_BAND_METERS, OSMnxRoadNetworkProvider
from .engines.population import RealAdminHierarchyProvider, PycnophylacticInterpolator, compute_catchment_population
from .models import BusinessReport, FeatureObservation

logger = logging.getLogger("business_intel")

_ADMIN_HIERARCHY = RealAdminHierarchyProvider()
_INTERPOLATOR = PycnophylacticInterpolator()
_ROAD_NETWORK_PROVIDER = OSMnxRoadNetworkProvider()

CATCHMENT_RADIUS_M = 500
assert CATCHMENT_RADIUS_M in DISTANCE_BAND_METERS, (
    f"CATCHMENT_RADIUS_M={CATCHMENT_RADIUS_M} must be one of spatial_context.DISTANCE_BAND_METERS "
    f"{DISTANCE_BAND_METERS} -- build_radius_catchments() only materializes those bands."
)

# TOPSIS closeness_coefficient is a ratio in [0, 1], not the old 0-100
# compute_*_score() scale. These thresholds are this task's own choice
# (not sourced/calibrated), same "directional, not authoritative until
# backtested" caveat competition.py's own scoring docstring already
# carries -- kept explicit here rather than buried in a magic number.
OPPORTUNITY_HIGH_CLOSENESS = 0.65
OPPORTUNITY_MEDIUM_CLOSENESS = 0.40


def _get_catchment_population(cell, catchments=None):
    catchments = catchments if catchments is not None else build_radius_catchments(cell)
    catchment = next((c for c in catchments if c.catchment_size_value == CATCHMENT_RADIUS_M), None)
    if catchment is None:
        logger.warning(
            "No %sm catchment returned by build_radius_catchments for cell %s.",
            CATCHMENT_RADIUS_M, cell.geohash,
        )
        return None

    features = compute_catchment_population(catchment, _ADMIN_HIERARCHY, _INTERPOLATOR)
    for w in features.warnings:
        logger.info("Catchment population (cell %s): %s", cell.geohash, w)
    return features


def _run_engine(risks, label, fn, *args, **kwargs):
    """Call one engine's run(), and fold its EngineRun status into risks
    if it degraded or failed, instead of raising the whole task over a
    single engine's inputs being unavailable. Returns the engine's
    return dict, or None if it raised."""
    try:
        result = fn(*args, **kwargs)
    except Exception as exc:
        logger.exception("Engine %s raised for query in generate_business_report", label)
        risks.append(f"{label} engine failed to run: {exc}")
        return None

    run_row = result.get("engine_run") if isinstance(result, dict) else None
    if run_row is not None and run_row.status in ("degraded", "failed") and run_row.degraded_reason:
        risks.append(f"{label} engine note: {run_row.degraded_reason}")
    elif run_row is not None and run_row.status == "failed" and run_row.error_message:
        risks.append(f"{label} engine failed: {run_row.error_message}")
    return result


def _latest_feature(cell, engine_number, feature_name, catchment=None, business_category=""):
    return FeatureObservation.objects.filter(
        location_cell=cell, engine_number=engine_number, feature_name=feature_name,
        catchment=catchment, business_category=business_category, is_latest=True,
    ).first()


@shared_task
def generate_business_report(report_id):
    # already patched: duplicate-run guard -- a re-queued dispatch from
    # sweep_stuck_reports() racing a normal run (or a worker restart)
    # must never re-generate a report that already finished.
    report = BusinessReport.objects.select_related("query", "query__location_cell").get(pk=report_id)
    if report.status == "ready":
        logger.info("BusinessReport %s already ready -- skipping duplicate task run.", report_id)
        return
    if report.status == "failed":
        logger.info("BusinessReport %s already failed -- skipping duplicate task run.", report_id)
        return
    query = report.query
    cell = query.location_cell
    category = query.business_category

    report.status = "generating"
    report.save(update_fields=["status"])

    started_at = timezone.now()
    risks: list = []
    evidence: list = []

    try:
        # --- Places/anchor enrichment (unchanged) ---
        if services.needs_category_enrichment(cell, category):
            results = search_places_category(cell, category)
            sync_places_to_business_entities(cell, category, results)

        if services.needs_category_enrichment(cell, "anchors"):
            anchor_results = search_anchors(cell)
            sync_places_to_anchor_facilities(cell, anchor_results)

        # --- Engine 1: Spatial Context. Builds/refreshes catchments for
        # real via OSMnxRoadNetworkProvider and records an EngineRun, so
        # Engine 24's completeness accounting sees Engine 1 actually ran
        # -- build_radius_catchments() alone (still used just below to
        # get the Catchment objects for this request) never wrote that
        # bookkeeping row. ---
        _run_engine(risks, "Spatial Context", spatial_context.run, query, network_provider=_ROAD_NETWORK_PROVIDER)
        catchments = _run_engine(risks, "Radius Catchments", build_radius_catchments, cell)
        if catchments is None:
            catchments = []

        primary_catchment = next((c for c in catchments if c.catchment_size_value == CATCHMENT_RADIUS_M), None)

        population_features = _get_catchment_population(cell, catchments=catchments)
        catchment_population = population_features.population if population_features else None

        if catchment_population is None:
            risks.append(
                f"No catchment population could be resolved for this location "
                f"({cell.county or 'unknown county'}) -- Competition and Market Gap scores "
                f"below are based on zero population signal, not a verified open market."
            )
        elif population_features and population_features.warnings:
            for w in population_features.warnings:
                risks.append(f"Population estimate precision note: {w}")

        # --- Engine 2: Population. Persists per-catchment population
        # features so Engine 26 has real criteria to rank on, not just
        # the primary catchment's number computed above. ---
        _run_engine(
            risks, "Population", population.run, query,
            admin_hierarchy=_ADMIN_HIERARCHY, interpolator=_INTERPOLATOR, catchments=catchments,
        )

        # --- Engine 3: Purchasing Power -- deliberately NOT run. See
        # module docstring: the only CountyEconomicPriorProvider
        # implementation is explicitly documented as synthetic and
        # "must never back a production request". ---
        risks.append(
            "Purchasing Power engine (Engine 3) was not run for this report -- no production "
            "CountyEconomicPriorProvider is implemented yet; the only provider in the codebase "
            "is a fixed-neutral synthetic value its own docstring says must not back production "
            "requests."
        )

        # --- Engines 4/5: cell-level, no catchment/provider arguments. ---
        _run_engine(risks, "Mobility & Traffic", mobility_traffic.run, query)
        _run_engine(risks, "Forced-Traffic", forced_traffic.run, query)

        # --- Engines 6/7/10: catchment-scoped. ---
        _run_engine(risks, "Business Ecosystem", business_ecosystem.run, query, catchments=catchments)
        _run_engine(risks, "Business Survival", business_survival.run, query, catchments=catchments)
        _run_engine(risks, "Business Adjacency", business_adjacency.run, query, catchments=catchments)

        # --- Engine 8: Competition. Was previously a bare (score,
        # evidence) tuple with no persistence at all -- see
        # engines/competition.py's new run(). ---
        _run_engine(
            risks, "Competition", competition.run, query,
            catchment_population=catchment_population,
        )

        # --- Engine 9: Market Gap. ---
        _run_engine(
            risks, "Market Gap", market_gap.run, query,
            admin_hierarchy=_ADMIN_HIERARCHY, catchments=catchments,
        )

        # --- Engine 11: Anchor. ---
        _run_engine(risks, "Anchor", anchor.run, query)

        # --- Engine 12: Development Activity. Cell-level, no catchment
        # argument -- same contract shape as Engines 4/5/11. See run()'s
        # own docstring for the still-open derivation_level/Decision-
        # criterion caveat. ---
        _run_engine(risks, "Development Activity", development_activity.run, query)

        # --- Engine 18: Economic Momentum. Satellite-nightlights-derived
        # market momentum (accelerating/stable/declining/emerging/
        # overheating). Independent of every other engine's inputs (reads
        # only VIIRS + its own accumulated FeatureObservation history for
        # county/corpus pooling), so it can run anywhere in this sequence
        # that is before Evidence/Confidence/Decision -- placed here,
        # right after the other single-cell engines, rather than earlier
        # or later, purely for readability (groups with Engine 11, not
        # for any data-dependency reason). ---
        momentum_result = _run_engine(risks, "Economic Momentum", economic_momentum.run, query)

        # --- Engine 23: Evidence. Audits every FeatureObservation +
        # EngineRun on record for this query/cell (drift checks, orphan
        # conclusions, cross-engine agreement where configured) and
        # writes a cell-level evidence_dossier. Must run BEFORE Engine
        # 24 -- confidence.run() reads this dossier straight off the DB
        # by (location_cell, engine_number=23), not as a function
        # argument, so calling order here is load-bearing, not cosmetic. ---
        _run_engine(risks, "Evidence", evidence_engine.run, query)

        # --- Engine 24: Confidence. Reads back every business_conclusion
        # -level FeatureObservation just written above (plus Engine 23's
        # dossier, now available) and produces a real aggregate
        # confidence score -- replaces the previous hardcoded
        # `report.confidence_score = 50`. ---
        confidence_result = _run_engine(risks, "Confidence", confidence.run, query)
        confidence_dossier = confidence_result["dossier"] if confidence_result else None
        confidence_score = None
        if confidence_dossier:
            confidence_score = confidence_dossier.get("query_level", {}).get("mean_confidence")
            risks.extend(f"Confidence engine note: {w}" for w in (confidence_dossier.get("warnings") or []))

        # --- Engine 26: Decision. TOPSIS-ranks every Catchment belonging
        # to this cell against whatever business_conclusion features the
        # engines above actually wrote -- replaces the previous
        # `overall = sum(scores) / len(scores)` naive average. ---
        decision_result = _run_engine(risks, "Decision", decision.run, query, catchments=catchments)
        decision_dossier = decision_result["dossier"] if decision_result else None

        primary_ranking_entry = None
        if decision_dossier:
            risks.extend(f"Decision engine note: {w}" for w in (decision_dossier.get("warnings") or []))
            if primary_catchment is not None:
                primary_ranking_entry = next(
                    (r for r in decision_dossier.get("ranking", []) if r["catchment_id"] == primary_catchment.pk),
                    None,
                )
            if len(catchments) > 1:
                risks.append(
                    f"Decision engine (Engine 26) ranks every Catchment radius band for this cell "
                    f"against every other one via TOPSIS -- for a single-site query, that ranks "
                    f"analysis radii against each other, not candidate sites against each other. "
                    f"opportunity_level/recommendation below use the {CATCHMENT_RADIUS_M}m catchment's "
                    f"own closeness score, not its rank relative to the other radius bands."
                )

            for c in (decision_dossier.get("criteria") or [])[:6]:
                evidence.append(
                    f"{c['feature_name']}: weight {c['weight']} "
                    f"({'direction inferred as benefit (unverified)' if c['direction_defaulted'] else c['direction']})"
                )

        # ===================================================================
        # --- ADDITION: Engine 27 - Anomaly Detection ---
        # This runs unsupervised ML (Isolation Forest, LOF, Mahalanobis,
        # Autoencoder, EVT) on all features aggregated from Engines 1-26.
        # It modifies `report` directly by adding anomaly_risk_score and
        # flags without halting the pipeline on failure.
        # ===================================================================
        try:
            anomaly_results = run_anomaly_detection(report.id)
            # Refresh local report object to pick up the new anomaly fields
            report.refresh_from_db(fields=[
                'anomaly_risk_score', 'anomaly_confidence',
                'is_anomalous', 'risks'
            ])
            # Append human-readable evidence
            if report.is_anomalous:
                evidence.append(
                    f"Anomaly Engine (27) flagged this location (Risk Score: "
                    f"{report.anomaly_risk_score:.1f}/100). {len(anomaly_results.get('anomaly_signals', []))} "
                    f"statistical deviations detected."
                )
            else:
                evidence.append(
                    f"Anomaly Engine (27) validated this location's business pattern "
                    f"as statistically normal (Confidence: {report.anomaly_confidence:.1f}%)."
                )
        except Exception as e:
            logger.error(f"Anomaly Detection Engine 27 failed for Report {report.id}: {e}")
            risks.append(f"Anomaly Engine (27) failed: {e}. Location risk assessment proceeds without ML validation.")
        # ===================================================================

        # --- Derive report fields from real engine output ---
        if primary_ranking_entry and not primary_ranking_entry.get("vetoed"):
            closeness = primary_ranking_entry["closeness_coefficient"]
            if closeness >= OPPORTUNITY_HIGH_CLOSENESS:
                opportunity_level, recommendation = "high", "proceed"
            elif closeness >= OPPORTUNITY_MEDIUM_CLOSENESS:
                opportunity_level, recommendation = "medium", "investigate_further"
            else:
                opportunity_level, recommendation = "low", "avoid"
            evidence.append(f"TOPSIS closeness coefficient for the {CATCHMENT_RADIUS_M}m catchment: {closeness}")
        else:
            # No usable ranking (no criteria discovered, primary
            # catchment vetoed, or Decision engine failed outright).
            # BusinessReport.opportunity_level/recommendation have no
            # "unknown"/"insufficient_data" choice -- see models.py's
            # OPPORTUNITY_CHOICES/RECOMMENDATION_CHOICES -- so this
            # reports the safest concrete answer and says exactly why in
            # risks, rather than inventing a choice the schema doesn't have.
            opportunity_level, recommendation = "low", "avoid"
            risks.append(
                "No TOPSIS ranking was available for this location's primary catchment (vetoed, or no "
                "criteria discovered) -- opportunity_level/recommendation default to the most "
                "conservative choice this report schema supports, not a genuine 'this is a bad "
                "location' finding."
            )

        # --- Human-readable summaries pulled from what was actually
        # persisted above (not regenerated/recomputed here). ---
        competition_feature = _latest_feature(cell, competition.ENGINE_NUMBER, "competition_evidence", business_category=category)
        if competition_feature and competition_feature.value_json:
            ev = competition_feature.value_json
            report.competition_summary = (
                f"{ev.get('competitor_count', 0)} competing {category} location(s) found nearby "
                f"(pre-entry HHI {ev.get('hhi_pre_entry')}, {ev.get('hhi_classification', 'unclassified')})."
            )

        if primary_catchment is not None:
            gap_feature = _latest_feature(cell, market_gap.ENGINE_NUMBER, "gap_classification", catchment=primary_catchment, business_category=category)
            score_feature = _latest_feature(cell, market_gap.ENGINE_NUMBER, "market_gap_score_0_100", catchment=primary_catchment, business_category=category)
            if gap_feature or score_feature:
                report.market_gap_summary = (
                    f"Market gap classification: {gap_feature.value_text if gap_feature else 'n/a'} "
                    f"(score {score_feature.value_numeric if score_feature else 'n/a'}/100)."
                )

        if momentum_result and momentum_result.get("report_fields"):
            report.momentum_summary = momentum_result["report_fields"]["momentum_summary"]
            report.timing = momentum_result["report_fields"]["timing"]
        else:
            # Engine 18 failed outright (EngineRun status="failed", e.g.
            # VIIRS/Earth Engine unreachable) -- _run_engine() already
            # folded that into `risks` above via run_row.error_message
            # (economic_momentum.run()'s dict return carries
            # "engine_run", exactly like anchor.run()/competition.run()/
            # every other wired engine -- see the REVISION note in
            # economic_momentum.run()'s own docstring). momentum_summary/
            # timing are left blank here rather than populated with a
            # fabricated "insufficient data" sentence that would look
            # identical to a real "insufficient_signal" classification
            # the engine itself can and does report when it runs
            # successfully but the location is genuinely dark/short-history.
            pass

        report.opportunity_level = opportunity_level
        report.recommendation = recommendation
        report.confidence_score = confidence_score
        report.risks = risks
        report.evidence = evidence
        report.feature_snapshot_ids = list(
            FeatureObservation.objects.filter(
                location_cell=cell, is_latest=True, updated_at__gte=started_at,
            ).values_list("id", flat=True)
        )

        report.status = "ready"
        report.save()
        logger.info("BusinessReport %s ready: %s (%s)", report.id, recommendation, opportunity_level)

    except Exception as exc:
        logger.exception("BusinessReport %s failed", report.id)
        report.status = "failed"
        report.failure_reason = str(exc)
        report.save(update_fields=["status", "failure_reason"])
        raise

# ─── Stuck-report recovery ──────────────────────────────────────────────
# Mirrors property_intel.tasks.sweep_stuck_reports -- business_intel never
# had an equivalent, so a worker killed mid-task (OOM, hard timeout,
# deploy restart) left reports stuck in "generating" forever, since only
# a normal Python exception (caught by generate_business_report's own
# try/except) can flip a report to "failed". Wire this into
# CELERY_BEAT_SCHEDULE (see backend/settings.py) to run every few minutes.
from datetime import timedelta

STUCK_REPORT_CUTOFF_MINUTES = 15


@shared_task
def sweep_stuck_reports():
    """
    Scheduled task -- wire into CELERY_BEAT_SCHEDULE to run every few
    minutes. Catches BusinessReports stuck in 'generating' past the
    cutoff (e.g. a worker was killed mid-task, so the outer try/except
    in generate_business_report() never got a chance to mark it
    'failed'). Without this, a report can hang forever with no
    automatic recovery -- this is the safety net underneath the task's
    own error handling, not a replacement for it.
    """
    cutoff = timezone.now() - timedelta(minutes=STUCK_REPORT_CUTOFF_MINUTES)
    stuck_ids = list(
        BusinessReport.objects.filter(status="generating", updated_at__lt=cutoff).values_list("pk", flat=True)
    )
    for report_id in stuck_ids:
        generate_business_report.delay(str(report_id))
    if stuck_ids:
        logger.warning(
            "Re-queued %s stuck BusinessReport(s) found in 'generating' past the %s-minute cutoff.",
            len(stuck_ids), STUCK_REPORT_CUTOFF_MINUTES,
        )
