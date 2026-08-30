"""
business_intel/engines/visibility_access.py

Engine 16 — Visibility & Physical Access Engine
Per business_location_intelligence_engines_data_models_v2.md, Section 16.

NOTE ON THE SPEC FILE: same situation as every other engine in this
codebase -- business_location_intelligence_engines_data_models_v2.md is
referenced but not present on disk. Built to match Engine 12/13/15's
structure, schema contract, and citation discipline (all read in full
before writing this module).

PURPOSE, DATA SOURCE, AND SCOPE BOUNDARY
"Evaluate site-level physical characteristics that affect customer access
and visibility." No table in this schema was purpose-built for road
geometry (unlike Engine 15's PropertyListing) -- the one real source that
actually answers "what does the street network look like right here" is
OSM Overpass, the same live external source Engine 12 (development_activity.py)
already established a caching contract for via google_client.query_osm_overpass()
+ models.OSMOverpassQueryCache. This engine reuses that exact contract
with purpose="visibility_access" -- one live Overpass request per cell,
same "cost control" discipline property_intel/amenity_verification.py and
development_activity.py already apply to their own Overpass calls.

Unlike Engine 12/13/15, which each read one already-migrated internal
table, this engine's ENTIRE evidence base is public OSM road-network
tagging -- highway=* ways (the international, actively-maintained OSM
classification scheme, see OpenStreetMap Wiki, "Key:highway") plus
highway=traffic_signals/crossing/mini_roundabout nodes. What that scheme
gives, honestly: a real, current (subject to OSM's own edit cadence, not
this system's) description of the physical street network -- road class,
geometry, tagged crossings. What it does NOT give, and is flagged rather
than invented: verified building frontage/signage exposure, footfall
counts, sightline obstructions (trees, hoardings, parked vehicles),
building setback from the road, or any ground-truthed "can a passer-by
actually see this specific shopfront" measurement -- none of that exists
in any table this system has access to. Every field below is therefore a
STREET-NETWORK PROXY for visibility/access, named as exactly that, not a
verified visibility measurement.

THE REAL GEOMETRIC / STATISTICAL PROBLEMS, AND WHY EACH PIECE IS THE
METHOD USED
Four genuinely separate problems, each handled with a named, real method:

1. HOW FAR IS THE QUERY POINT FROM THE NEAREST ROAD, AND WHAT CLASS OF
   ROAD IS IT? -- OSM way geometry is a polyline (a sequence of node
   coordinates), not a point, so "distance to the road" is a point-to-
   polyline problem: the minimum, over every consecutive node pair
   (segment) of every nearby way, of the perpendicular point-to-segment
   distance -- standard planar computational geometry (projection of the
   query point onto each segment, clamped to the segment's own extent).
   Computed in a local equirectangular (plate carree) tangent-plane
   projection centered on the query point:
     Snyder, J. P. (1987). "Map Projections: A Working Manual." USGS
     Professional Paper 1395. (Ch. on the Equirectangular / Plate Carree
     projection -- the standard small-extent flat-earth approximation.)
   valid at this engine's sub-2km scale (the projection's distortion is
   negligible well below 1% at these extents), and materially more
   accurate than the node-only nearest-point shortcut development_activity.py
   uses for its own (different) problem, since frontage distance is
   naturally measured to the EDGE of the road, not to whichever mapped
   node happens to be nearest. nearest_road_class comes from the same
   winning way's highway=* tag; nearest_major_road_distance_m repeats the
   search restricted to MAJOR_ROAD_CLASSES (motorway/trunk/primary/
   secondary) since a business can be close to a minor lane and far from
   any road a passer-by would actually be driving.

2. HOW MUCH "STREET EXPOSURE" DOES THIS EXACT POINT HAVE, GIVEN SEVERAL
   ROADS OF DIFFERENT CLASSES AT DIFFERENT DISTANCES? -- Reuses the same
   gravity-style decayed-contribution framing infrastructure_change.py
   (Engine 13) already established for this codebase, tracing to:
     Hansen, W. G. (1959). "How Accessibility Shapes Land Use." Journal
     of the American Institute of Planners, 25(2), 73-76.
   applied here to street segments instead of infrastructure projects:
   each nearby way segment contributes weight = (an ordinal highway-class
   weight, HIGHWAY_CLASS_WEIGHTS below) x (Gaussian distance decay from
   the query point) x (the segment's own length, capped -- a long
   frontage contributes more exposure than a short one, capped so one
   very long way near the boundary of the search radius cannot dominate
   the sum). Summed across every segment of every nearby way ->
   raw_street_exposure_index, the module's one composite magnitude.
   KERNEL_BANDWIDTH_M is fixed at 150m -- the same ~150m figure this
   codebase already uses for BusinessLocationCell's own cell width (see
   that model's docstring, "~150m cell") -- reused rather than invented,
   and NOT cross-validated the way property_cost.py's kernel bandwidth
   is: Engine 15 could cross-validate against a real held-out label (a
   comp's own price); this engine has no analogous held-out ground truth
   to validate a bandwidth against, so a fixed, already-established-in-
   this-codebase constant is the more honest choice than dressing up an
   unvalidatable number as data-driven.

3. HOW CONNECTED IS THE IMMEDIATE STREET GRID, AND IS THIS SPECIFICALLY A
   CORNER SITE? -- Intersection density is a standard, literature-
   documented urban-design correlate of pedestrian exposure and walk
   trips:
     Ewing, R., & Cervero, R. (2010). "Travel and the Built Environment:
     A Meta-Analysis." Journal of the American Planning Association,
     76(3), 265-294. (Intersection/connectivity density as one of the
     "3 Ds" -- density, diversity, design -- correlated with pedestrian
     activity.)
   Two intersection counts are computed, from two different kinds of OSM
   evidence, and kept separate rather than merged into one invented
   number: CONTROLLED junctions (nodes explicitly tagged
   traffic_signals/crossing/mini_roundabout -- a marked, human-curated
   fact) and TOPOLOGICAL junctions (nodes that are shared endpoints of
   two or more distinct ways in the fetched set -- inferred from the
   network's own graph structure, the standard definition of a network
   node's degree, needing no citation beyond that definition). A
   corner-proximity flag additionally reports whether ANY junction (of
   either kind) sits within CORNER_PROXIMITY_THRESHOLD_M of the query
   point itself -- reported as a plain fact (is_corner_proximate,
   nearest_junction_distance_m), not a "corner premium" value judgment:
   no calibrated Kenya-specific corner-lot valuation figure exists for
   this system to cite, same "report the raw fact, not an invented
   coefficient" discipline property_cost.py applies to its own
   affordability ratios.

4. IS THIS AREA GENUINELY LOW-ACCESS, OR JUST THINLY MAPPED IN OSM? --
   OSM road-network completeness is real but uneven, and worse in lower-
   income and rural regions:
     Barrington-Leigh, C., & Millard-Ball, A. (2017). "The world's user-
     generated road map is more than 80% complete." PLOS ONE, 12(8),
     e0180698. (Estimates ~83% global completeness of the OSM road
     network as of that study, with documented regional variance.)
   When total mapped highway length within LOCAL_EXPOSURE_RADIUS_M falls
   below MIN_HIGHWAY_LENGTH_M_FOR_COMPLETENESS, every count/exposure
   field is still computed and returned (never silently withheld), but
   flagged with this citation in warnings -- a genuinely low count from
   thin mapping must not be read the same as a genuinely low-access
   location, and this module cannot itself distinguish the two from OSM
   data alone.

A FIFTH, SEPARATE SIGNAL: NETWORK-VS-EUCLIDEAN ACCESS ("CIRCUITY")
Where this cell already has BOTH a "radius" (naive circular buffer) and a
network-based ("network_radius" or an "*_isochrone") models.Catchment row
of comparable size on record (produced by whatever upstream isochrone
step populated them -- out of scope for this engine, which only reads
them), the ratio of the network-reachable area to the naive circular
area is a real, named transportation-network-science quantity (often
called the circuity or route-directness ratio):
  Barthelemy, M. (2011). "Spatial Networks." Physics Reports, 499(1-3),
  1-101. (Survey of circuity/route-factor measures comparing network and
  Euclidean/areal reach in spatial networks.)
A ratio near 1.0 means the street grid lets a customer reach nearly the
full naive catchment; a ratio well below 1.0 means the real street
network is far more constraining than a straight-line radius would
suggest (cul-de-sacs, a single arterial spine, a river/rail barrier).
Computed ONLY when a matching pair of Catchment rows already exists for
this cell (this engine issues no isochrone request of its own -- that is
a separate, already-existing upstream capability, not duplicated here);
reported as None with an explicit warning, never silently omitted,
when no matching pair is on record.

DATA HYGIENE, NOT A SIXTH METHOD BUT LOAD-BEARING: an Overpass way with
fewer than 2 geometry points cannot form a segment and is dropped before
any of the above runs (n_ways_dropped_insufficient_geometry, reported not
silently discarded); an unrecognized highway=* subtype is kept (not
dropped) and scored at HIGHWAY_CLASS_WEIGHTS["other"], the module's
lowest defined ordinal weight, so a road this scheme has never seen
before still counts for SOMETHING rather than vanishing from the index.

FRAMING THE 0-100 FIELD
visibility_exposure_score_0_100 is the same uncalibrated monotonic
saturating transform of a raw magnitude in this engine's own units that
development_activity.py's and infrastructure_change.py's own
SCORE_SATURATION_SCALE constants already establish as this codebase's
default convention for a composite magnitude with no natural comparison
population (see property_cost.py's module docstring for the one
documented EXCEPTION to this default, and why that engine's price data
specifically warranted a percentile instead -- this engine's exposure
index does not share that problem: it does not span orders of magnitude
across markets the way price-per-sqm does, so the default convention,
not property_cost's exception, is the right fit here). Purely descriptive
(higher = more street exposure by this proxy), not a judgment of good or
bad for any particular business type -- a pharmacy and a warehouse do not
want the same exposure level, and this engine does not know which one is
being sited.

WHAT'S ACTUALLY AVAILABLE VS. WHAT ISN'T
Real data used: live OSM Overpass query (highway=* ways + tagged junction
nodes) within SEARCH_RADIUS_M of the query cell, via the exact
google_client.query_osm_overpass() + OSMOverpassQueryCache contract
Engine 12 established (purpose="visibility_access" keeps its cache rows
distinct from Engine 12's own purpose="development_activity" rows against
the same cell); optionally, already-computed models.Catchment rows for
the circuity ratio.

What is explicitly NOT available and is flagged, not faked: verified
sightline/obstruction data, footfall counts, building setback, signage
regulations, parking availability, and any ground-truthed frontage
photograph or street-view derived measurement -- none of that exists in
any table this system has access to. See point 4 above for the further,
specific "thin OSM mapping vs. genuinely low access" confound.

KNOWN UPSTREAM GAP (same status every other engine in this codebase
flags for itself): tasks.py's generate_business_report does not currently
call Engine 16 at all, and BusinessReport has no visibility_access-related
field. Wiring is a separate, out-of-scope task, same status
development_activity.py's, infrastructure_change.py's, and
property_cost.py's own module docstrings already document for themselves.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("business_intel")

# =============================================================================
# Constants
# =============================================================================

ENGINE_NUMBER = 16

# Single live Overpass call radius (module docstring: "one live Overpass
# request per cell", matching Engine 12's own cost-control discipline).
# 1000m is wide enough to find a major arterial from a peri-urban site
# that may sit several hundred meters from one, while keeping the query
# and response small -- an engineering choice, not a citable constant,
# same status DEVELOPMENT_ACTIVITY_RADII_M's own widest band carries.
SEARCH_RADIUS_M = 1000

# Radius within which junction density, controlled-junction counts, and
# pedestrian-infrastructure density are computed. Anchored to the same
# ~400m figure infrastructure_change.py's own UTILITY_DECAY_M discussion
# already cites (Guerra, Cervero & Tischler 2012's transit-catchment
# reference point) -- reused here as "a locally relevant, site-scale
# radius," same order-of-magnitude reference, not a fresh citation claim
# about intersection density specifically.
LOCAL_EXPOSURE_RADIUS_M = 400

# Fixed Gaussian-kernel bandwidth for the street-exposure index (module
# docstring, point 2) -- reuses models.BusinessLocationCell's own ~150m
# cell-width figure rather than inventing a new spatial constant.
KERNEL_BANDWIDTH_M = 150.0

# Within this distance of the query point, a junction (of either kind)
# makes the site itself "corner-proximate." An engineering choice
# (roughly a generous commercial-plot depth), not a citable figure --
# same documented-not-measured status CORNER_PROXIMITY_THRESHOLD_M's
# sibling constants carry throughout this codebase.
CORNER_PROXIMITY_THRESHOLD_M = 40.0

OVERPASS_QUERY_TIMEOUT_SECONDS = 25

# Below this many total mapped meters of highway within
# LOCAL_EXPOSURE_RADIUS_M, results are still computed and returned but
# flagged per module docstring point 4 (Barrington-Leigh & Millard-Ball
# 2017) as possibly reflecting thin OSM coverage rather than genuinely
# low road density.
MIN_HIGHWAY_LENGTH_M_FOR_COMPLETENESS = 50.0

# A single very long way near the search-radius boundary should not, on
# its own, dominate the exposure sum -- each way's length contribution is
# capped at this many meters before kernel weighting. An engineering
# choice (roughly one urban block), not a citable figure.
MAX_WAY_LENGTH_CONTRIBUTION_M = 250.0

# Saturating-curve scale for the uncalibrated score transform -- see
# module docstring's "FRAMING THE 0-100 FIELD". Same
# 100*(1-exp(-x/SCALE)) construction as development_activity.py's and
# infrastructure_change.py's own SCORE_SATURATION_SCALE. Order-of-
# magnitude chosen so a single primary-road frontage segment at close
# range (kernel weight ~1.0 x class weight 1.0 x capped length 250) does
# not immediately saturate the curve, matching the "single band alone
# saturates neither engine's own curve" behavior those two constants were
# tuned to preserve for their own magnitudes.
SCORE_SATURATION_SCALE = 180.0

MAJOR_ROAD_CLASSES = frozenset({"motorway", "trunk", "primary", "secondary"})

# OSM highway=* tag -> ordinal exposure/accessibility weight. A documented
# ENGINEERING JUDGMENT about typical customer-facing exposure and vehicle/
# pedestrian accessibility per road class, not a measured elasticity --
# same "ordinal, flagged as such" status infrastructure_change.py's own
# REACH_MULTIPLIER_BY_TYPE carries for an analogous ordinal judgment.
# Motorways score below primary/secondary/trunk despite carrying the most
# traffic: they are highly visible in passing but structurally deny the
# direct stopping/turning access a customer-facing site needs.
HIGHWAY_CLASS_WEIGHTS: Dict[str, float] = {
    "motorway": 0.40, "motorway_link": 0.35,
    "trunk": 0.90, "trunk_link": 0.80,
    "primary": 1.00, "primary_link": 0.85,
    "secondary": 0.90, "secondary_link": 0.75,
    "tertiary": 0.70, "tertiary_link": 0.60,
    "unclassified": 0.50,
    "residential": 0.40,
    "living_street": 0.30,
    "pedestrian": 0.50,
    "service": 0.20,
    "track": 0.05,
    "path": 0.05,
    "footway": 0.30,
    "cycleway": 0.20,
    "steps": 0.05,
    "other": 0.15,  # unrecognized highway=* subtype -- kept, not dropped; see module docstring's "DATA HYGIENE"
}

# highway=* subtypes treated as pedestrian infrastructure for the
# pedestrian-infrastructure-density count (module docstring point 3's
# sibling signal) -- distinct from vehicle road class.
PEDESTRIAN_HIGHWAY_TYPES = frozenset({"footway", "pedestrian", "steps", "crossing", "living_street"})

CONTROLLED_JUNCTION_NODE_TYPES = frozenset({"traffic_signals", "crossing", "mini_roundabout", "stop", "give_way"})

FEATURE_VISIBILITY_EXPOSURE_SCORE = "visibility_exposure_score_0_100"
FEATURE_NEAREST_ROAD_DISTANCE_M = "nearest_road_distance_m"
FEATURE_NEAREST_ROAD_CLASS = "nearest_road_class"
FEATURE_NEAREST_MAJOR_ROAD_DISTANCE_M = "nearest_major_road_distance_m"
FEATURE_CONTROLLED_JUNCTION_COUNT = "controlled_junction_count"
FEATURE_TOPOLOGICAL_JUNCTION_COUNT = "topological_junction_count"
FEATURE_IS_CORNER_PROXIMATE = "is_corner_proximate"
FEATURE_NEAREST_JUNCTION_DISTANCE_M = "nearest_junction_distance_m"
FEATURE_PEDESTRIAN_DENSITY_PER_KM2 = "pedestrian_infrastructure_density_per_km2"
FEATURE_NETWORK_EUCLIDEAN_ACCESS_RATIO = "network_euclidean_access_ratio"

EARTH_RADIUS_M = 6_371_000.0


# =============================================================================
# Pure data structures -- no Django/network import at module scope, so this
# file's math is unit-testable without a DB, matching Engine 12/13/15's
# "pure-math layer above without a DB" split.
# =============================================================================

@dataclass
class RoadWay:
    """One OSM way, resolved to a local equirectangular polyline (module
    docstring point 1) with its nearest-point distance from the query
    point already computed."""
    way_id: str
    highway_type: str
    class_weight: float
    points_xy: List[Tuple[float, float]]  # local meters, (x=east, y=north)
    node_ids: Optional[List[str]]  # None if Overpass response omitted node refs for this way
    nearest_distance_m: float
    length_within_radius_m: float  # sum of segment lengths whose midpoint falls within SEARCH_RADIUS_M


@dataclass
class JunctionPoint:
    node_id: Optional[str]
    latitude: float
    longitude: float
    distance_m: float
    kind: str  # "controlled" | "topological"
    controlled_type: Optional[str] = None  # e.g. "traffic_signals", when kind == "controlled"


@dataclass
class VisibilityAccessResult:
    n_ways_total: int
    n_ways_dropped_insufficient_geometry: int
    n_ways_dropped_no_geometry_field: int
    total_highway_length_local_m: float
    raw_street_exposure_index: float
    visibility_exposure_score_0_100: float
    nearest_road_distance_m: Optional[float]
    nearest_road_class: Optional[str]
    nearest_major_road_distance_m: Optional[float]
    nearest_major_road_class: Optional[str]
    controlled_junction_count: int
    topological_junction_count: int
    topology_coverage_complete: bool  # False if any way lacked node refs, so topological count is a lower bound
    is_corner_proximate: bool
    nearest_junction_distance_m: Optional[float]
    pedestrian_element_count: int
    pedestrian_infrastructure_density_per_km2: float
    network_euclidean_access_ratio: Optional[float]
    warnings: List[str] = field(default_factory=list)


# =============================================================================
# Real, cited geometric/statistical primitives
# =============================================================================

def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Duplicated locally rather than imported -- same 'import for
    plumbing, duplicate for independent statistics' convention every
    other engine in this codebase already establishes for this exact
    function."""
    r = EARTH_RADIUS_M
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def equirect_xy_m(lat: float, lng: float, ref_lat: float, ref_lng: float) -> Tuple[float, float]:
    """Equirectangular (plate carree) local-tangent-plane projection
    centered at (ref_lat, ref_lng) -- Snyder (1987) USGS Prof. Paper
    1395; module docstring point 1. x = eastward meters, y = northward
    meters. Valid at this engine's sub-2km operating scale."""
    ref_phi = math.radians(ref_lat)
    x = math.radians(lng - ref_lng) * math.cos(ref_phi) * EARTH_RADIUS_M
    y = math.radians(lat - ref_lat) * EARTH_RADIUS_M
    return x, y


def point_to_segment_distance_m(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    """Standard planar point-to-segment perpendicular distance (clamped
    to the segment's own extent) -- module docstring point 1. Operates in
    the local equirectangular meter frame produced by equirect_xy_m, not
    in lat/lng directly."""
    abx, aby = bx - ax, by - ay
    seg_len_sq = abx * abx + aby * aby
    if seg_len_sq <= 1e-9:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * abx + (py - ay) * aby) / seg_len_sq
    t = max(0.0, min(1.0, t))
    cx, cy = ax + t * abx, ay + t * aby
    return math.hypot(px - cx, py - cy)


def _gaussian_kernel(d: float, h: float = KERNEL_BANDWIDTH_M) -> float:
    h = max(float(h), 1.0)
    return math.exp(-0.5 * (d / h) ** 2)


def classify_highway(highway_tag: Optional[str]) -> Tuple[str, float]:
    """OSM highway=* tag -> (class_name, ordinal weight). Unrecognized
    subtypes are kept and scored at the 'other' weight rather than
    dropped -- module docstring's 'DATA HYGIENE'."""
    if not highway_tag:
        return "other", HIGHWAY_CLASS_WEIGHTS["other"]
    weight = HIGHWAY_CLASS_WEIGHTS.get(highway_tag)
    if weight is None:
        return "other", HIGHWAY_CLASS_WEIGHTS["other"]
    return highway_tag, weight


def build_road_ways(
    elements: Sequence[dict], ref_lat: float, ref_lng: float, search_radius_m: float = SEARCH_RADIUS_M,
) -> Tuple[List[RoadWay], int, int]:
    """Resolves raw Overpass way elements (as returned by 'out body geom;')
    into RoadWay objects in the local equirectangular frame. Returns
    (ways, n_dropped_insufficient_geometry, n_dropped_no_geometry_field).
    Ways whose nearest point falls outside search_radius_m are excluded
    (Overpass's own 'around' filter already does this approximately, but
    is not exact for arbitrarily long ways, so this is re-checked here)."""
    ways: List[RoadWay] = []
    n_dropped_geom = 0
    n_dropped_no_geom_field = 0

    for el in elements:
        if el.get("type") != "way":
            continue
        tags = el.get("tags") or {}
        if "highway" not in tags:
            continue
        geometry = el.get("geometry")
        if not geometry:
            n_dropped_no_geom_field += 1
            continue
        pts_ll = [(g.get("lat"), g.get("lon")) for g in geometry if g and g.get("lat") is not None and g.get("lon") is not None]
        if len(pts_ll) < 2:
            n_dropped_geom += 1
            continue

        pts_xy = [equirect_xy_m(lat, lng, ref_lat, ref_lng) for lat, lng in pts_ll]

        nearest_d = math.inf
        length_within_radius = 0.0
        for i in range(len(pts_xy) - 1):
            ax, ay = pts_xy[i]
            bx, by = pts_xy[i + 1]
            seg_d = point_to_segment_distance_m(0.0, 0.0, ax, ay, bx, by)
            nearest_d = min(nearest_d, seg_d)
            seg_len = math.hypot(bx - ax, by - ay)
            mid_d = math.hypot((ax + bx) / 2.0, (ay + by) / 2.0)
            if mid_d <= search_radius_m:
                length_within_radius += seg_len

        if nearest_d > search_radius_m:
            continue

        highway_type, class_weight = classify_highway(tags.get("highway"))
        node_ids = [str(n) for n in el["nodes"]] if el.get("nodes") else None
        if node_ids is not None and len(node_ids) != len(pts_ll):
            # Node/geometry length mismatch (can happen if Overpass drops
            # a duplicate or the response is truncated) -- topology for
            # this way is unreliable, so treat as if refs were absent
            # rather than build a mismatched index-to-node mapping.
            node_ids = None

        ways.append(RoadWay(
            way_id=str(el.get("id")), highway_type=highway_type, class_weight=class_weight,
            points_xy=pts_xy, node_ids=node_ids, nearest_distance_m=nearest_d,
            length_within_radius_m=length_within_radius,
        ))

    return ways, n_dropped_geom, n_dropped_no_geom_field


def weighted_street_exposure_index(ways: Sequence[RoadWay]) -> float:
    """Module docstring point 2: Hansen (1959)-style gravity contribution
    per way, summed. weight_i = class_weight_i * gaussian_kernel(nearest_distance_i)
    * min(length_within_radius_i, MAX_WAY_LENGTH_CONTRIBUTION_M)."""
    total = 0.0
    for w in ways:
        capped_length = min(w.length_within_radius_m, MAX_WAY_LENGTH_CONTRIBUTION_M)
        total += w.class_weight * _gaussian_kernel(w.nearest_distance_m) * capped_length
    return total


def score_from_exposure(raw_index: float, scale: float = SCORE_SATURATION_SCALE) -> float:
    """Same 100*(1-exp(-x/scale)) saturating transform this codebase's
    other engines use for an uncalibrated composite magnitude -- see
    module docstring's 'FRAMING THE 0-100 FIELD'."""
    return round(100.0 * (1.0 - math.exp(-max(0.0, raw_index) / scale)), 2)


def nearest_road(ways: Sequence[RoadWay], classes: Optional[frozenset] = None) -> Tuple[Optional[float], Optional[str]]:
    """Nearest way overall (classes=None) or nearest way restricted to
    `classes` (module docstring point 1's nearest_major_road_*)."""
    candidates = [w for w in ways if classes is None or w.highway_type in classes]
    if not candidates:
        return None, None
    best = min(candidates, key=lambda w: w.nearest_distance_m)
    return best.nearest_distance_m, best.highway_type


def detect_topological_junctions(ways: Sequence[RoadWay]) -> Tuple[Dict[str, int], bool]:
    """A topological junction is a node shared by >=2 distinct ways in
    the fetched set -- the standard graph-degree definition of a network
    intersection (module docstring point 3), needing no citation beyond
    the definition itself. Returns (node_id -> set-count of distinct ways
    it appears in, as a plain int >= 2 only for true junctions; nodes with
    count 1 are omitted) and a `complete` flag that is False if any way
    lacked node refs (so the returned count is a documented lower bound,
    not silently presented as exhaustive)."""
    node_way_counts: Dict[str, int] = {}
    complete = True
    for w in ways:
        if w.node_ids is None:
            complete = False
            continue
        seen_in_this_way = set(w.node_ids)  # a way should not repeat a node except possibly a closed loop's first/last
        for nid in seen_in_this_way:
            node_way_counts[nid] = node_way_counts.get(nid, 0) + 1
    junctions = {nid: c for nid, c in node_way_counts.items() if c >= 2}
    return junctions, complete


def _node_xy_lookup(ways: Sequence[RoadWay]) -> Dict[str, Tuple[float, float]]:
    """node_id -> local xy, built from whichever way first supplies it
    (coordinates for the same real-world node should agree across ways
    that reference it)."""
    lookup: Dict[str, Tuple[float, float]] = {}
    for w in ways:
        if w.node_ids is None:
            continue
        for nid, xy in zip(w.node_ids, w.points_xy):
            if nid not in lookup:
                lookup[nid] = xy
    return lookup


def corner_proximity(
    junction_distances_m: Sequence[float], threshold_m: float = CORNER_PROXIMITY_THRESHOLD_M,
) -> Tuple[bool, Optional[float]]:
    """Module docstring point 3: is the query point itself within
    threshold_m of ANY junction (of either kind)? Returns
    (is_corner_proximate, nearest_junction_distance_m)."""
    if not junction_distances_m:
        return False, None
    nearest = min(junction_distances_m)
    return nearest <= threshold_m, nearest


def pedestrian_density_per_km2(
    elements: Sequence[dict], ref_lat: float, ref_lng: float, radius_m: float = LOCAL_EXPOSURE_RADIUS_M,
) -> Tuple[int, float]:
    """Count of pedestrian-tagged OSM elements (module docstring's
    PEDESTRIAN_HIGHWAY_TYPES sibling signal) within radius_m, expressed
    as a plain areal density (count / km^2 of the search disc) -- a
    descriptive statistic needing no citation beyond its own definition,
    same status property_cost.py's percentile-rank field carries for
    itself."""
    count = 0
    for el in elements:
        tags = el.get("tags") or {}
        highway = tags.get("highway")
        is_pedestrian_tagged = highway in PEDESTRIAN_HIGHWAY_TYPES or "sidewalk" in tags
        if not is_pedestrian_tagged:
            continue
        if el.get("type") == "node":
            lat, lng = el.get("lat"), el.get("lon")
            if lat is None or lng is None:
                continue
            d = _haversine_m(ref_lat, ref_lng, lat, lng)
            if d <= radius_m:
                count += 1
        else:
            geometry = el.get("geometry") or []
            pts = [(g.get("lat"), g.get("lon")) for g in geometry if g and g.get("lat") is not None]
            if not pts:
                continue
            # A way counts once if ANY of its points falls within radius_m
            # -- consistent with "is this pedestrian infrastructure present
            # nearby," not a length-weighted measure (that role is already
            # filled by the exposure index for vehicle roads; duplicating
            # it here for pedestrian ways would double up two different
            # kinds of claim under one number).
            if any(_haversine_m(ref_lat, ref_lng, lat, lng) <= radius_m for lat, lng in pts):
                count += 1

    area_km2 = math.pi * (radius_m / 1000.0) ** 2
    density = count / area_km2 if area_km2 > 0 else 0.0
    return count, density


def circuity_ratio(network_area_km2: Optional[float], euclidean_area_km2: Optional[float]) -> Optional[float]:
    """Module docstring's 'A FIFTH, SEPARATE SIGNAL': Barthelemy (2011)
    network-vs-Euclidean reach ratio. None (not 0.0) when either input is
    missing or the Euclidean area is non-positive -- an absent ratio must
    not be read as a ratio of zero."""
    if network_area_km2 is None or euclidean_area_km2 is None or euclidean_area_km2 <= 0:
        return None
    return round(network_area_km2 / euclidean_area_km2, 4)


# =============================================================================
# Assembly: pure-math result builder (no Django import -- unit-testable
# directly, matching Engine 12/13/15's compute_*_result split)
# =============================================================================

def compute_visibility_access_result(
    elements: Sequence[dict],
    query_lat: float,
    query_lng: float,
    search_radius_m: float = SEARCH_RADIUS_M,
    local_radius_m: float = LOCAL_EXPOSURE_RADIUS_M,
    network_area_km2: Optional[float] = None,
    euclidean_area_km2: Optional[float] = None,
) -> VisibilityAccessResult:
    warnings: List[str] = []

    ways, n_dropped_geom, n_dropped_no_geom = build_road_ways(elements, query_lat, query_lng, search_radius_m)
    if n_dropped_geom:
        warnings.append(f"{n_dropped_geom} way(s) dropped: fewer than 2 usable geometry points.")
    if n_dropped_no_geom:
        warnings.append(f"{n_dropped_no_geom} way(s) dropped: Overpass response had no geometry field.")

    local_ways = [w for w in ways if w.nearest_distance_m <= local_radius_m]
    total_highway_length_local_m = sum(w.length_within_radius_m for w in local_ways)

    raw_index = weighted_street_exposure_index(ways)
    score = score_from_exposure(raw_index)

    nearest_d, nearest_class = nearest_road(ways)
    nearest_major_d, nearest_major_class = nearest_road(ways, classes=MAJOR_ROAD_CLASSES)

    controlled_junctions: List[JunctionPoint] = []
    for el in elements:
        if el.get("type") != "node":
            continue
        tags = el.get("tags") or {}
        matched_types = [t for t in CONTROLLED_JUNCTION_NODE_TYPES if tags.get("highway") == t or tags.get(t) == "yes"]
        if not matched_types:
            continue
        lat, lng = el.get("lat"), el.get("lon")
        if lat is None or lng is None:
            continue
        d = _haversine_m(query_lat, query_lng, lat, lng)
        if d <= search_radius_m:
            controlled_junctions.append(JunctionPoint(
                node_id=str(el.get("id")), latitude=lat, longitude=lng, distance_m=d,
                kind="controlled", controlled_type=matched_types[0],
            ))
    controlled_local = [j for j in controlled_junctions if j.distance_m <= local_radius_m]

    topo_junction_ids, topo_complete = detect_topological_junctions(ways)
    node_xy = _node_xy_lookup(ways)
    topological_junctions: List[JunctionPoint] = []
    for nid in topo_junction_ids:
        xy = node_xy.get(nid)
        if xy is None:
            continue
        d = math.hypot(xy[0], xy[1])
        if d <= search_radius_m:
            topological_junctions.append(JunctionPoint(node_id=nid, latitude=0.0, longitude=0.0, distance_m=d, kind="topological"))
    topological_local = [j for j in topological_junctions if j.distance_m <= local_radius_m]
    if not topo_complete:
        warnings.append(
            "One or more ways lacked node references in the Overpass response; "
            "topological_junction_count is a lower bound, not exhaustive."
        )

    all_junction_distances = [j.distance_m for j in controlled_junctions] + [j.distance_m for j in topological_junctions]
    is_corner, nearest_junction_d = corner_proximity(all_junction_distances)

    ped_count, ped_density = pedestrian_density_per_km2(elements, query_lat, query_lng, local_radius_m)

    ratio = circuity_ratio(network_area_km2, euclidean_area_km2)
    if ratio is None:
        warnings.append("No matching radius/network Catchment pair on record for this cell -- network_euclidean_access_ratio not computed.")

    if total_highway_length_local_m < MIN_HIGHWAY_LENGTH_M_FOR_COMPLETENESS:
        warnings.append(
            f"Only {total_highway_length_local_m:.0f}m of mapped highway found within {local_radius_m:.0f}m -- "
            "may reflect thin OSM coverage rather than genuinely low road density "
            "(Barrington-Leigh & Millard-Ball 2017 estimate ~83% global OSM road-network completeness, "
            "with known regional variance)."
        )

    return VisibilityAccessResult(
        n_ways_total=len(ways),
        n_ways_dropped_insufficient_geometry=n_dropped_geom,
        n_ways_dropped_no_geometry_field=n_dropped_no_geom,
        total_highway_length_local_m=round(total_highway_length_local_m, 1),
        raw_street_exposure_index=round(raw_index, 4),
        visibility_exposure_score_0_100=score,
        nearest_road_distance_m=round(nearest_d, 1) if nearest_d is not None else None,
        nearest_road_class=nearest_class,
        nearest_major_road_distance_m=round(nearest_major_d, 1) if nearest_major_d is not None else None,
        nearest_major_road_class=nearest_major_class,
        controlled_junction_count=len(controlled_local),
        topological_junction_count=len(topological_local),
        topology_coverage_complete=topo_complete,
        is_corner_proximate=is_corner,
        nearest_junction_distance_m=round(nearest_junction_d, 1) if nearest_junction_d is not None else None,
        pedestrian_element_count=ped_count,
        pedestrian_infrastructure_density_per_km2=round(ped_density, 2),
        network_euclidean_access_ratio=ratio,
        warnings=warnings,
    )


def visibility_access_evidence(result: VisibilityAccessResult) -> dict:
    """Human/downstream-Evidence-engine-facing summary, same role
    property_cost_evidence() / development_activity's evidence dict play
    for their own engines."""
    return {
        "engine": "visibility_access",
        "engine_number": ENGINE_NUMBER,
        "n_ways_total": result.n_ways_total,
        "n_ways_dropped_insufficient_geometry": result.n_ways_dropped_insufficient_geometry,
        "n_ways_dropped_no_geometry_field": result.n_ways_dropped_no_geometry_field,
        "total_highway_length_local_m": result.total_highway_length_local_m,
        "raw_street_exposure_index": result.raw_street_exposure_index,
        "visibility_exposure_score_0_100": result.visibility_exposure_score_0_100,
        "nearest_road_distance_m": result.nearest_road_distance_m,
        "nearest_road_class": result.nearest_road_class,
        "nearest_major_road_distance_m": result.nearest_major_road_distance_m,
        "nearest_major_road_class": result.nearest_major_road_class,
        "controlled_junction_count": result.controlled_junction_count,
        "topological_junction_count": result.topological_junction_count,
        "topology_coverage_complete": result.topology_coverage_complete,
        "is_corner_proximate": result.is_corner_proximate,
        "nearest_junction_distance_m": result.nearest_junction_distance_m,
        "pedestrian_element_count": result.pedestrian_element_count,
        "pedestrian_infrastructure_density_per_km2": result.pedestrian_infrastructure_density_per_km2,
        "network_euclidean_access_ratio": result.network_euclidean_access_ratio,
        "warnings": result.warnings,
        "methods": {
            "nearest_road_distance": "point_to_segment_equirectangular (Snyder 1987)",
            "street_exposure_index": "gaussian_kernel_gravity_weighted_sum (Hansen 1959)",
            "score_transform": "saturating_exponential_uncalibrated",
            "topological_junction_count": "shared_node_graph_degree (definitional)",
            "network_euclidean_access_ratio": "circuity_ratio (Barthelemy 2011)",
        },
    }


METRIC_FIELDS = [
    (FEATURE_VISIBILITY_EXPOSURE_SCORE, "visibility_exposure_score_0_100", "score_0_100", "model_estimate"),
    (FEATURE_NEAREST_ROAD_DISTANCE_M, "nearest_road_distance_m", "meters", "calculation"),
    (FEATURE_NEAREST_MAJOR_ROAD_DISTANCE_M, "nearest_major_road_distance_m", "meters", "calculation"),
    (FEATURE_CONTROLLED_JUNCTION_COUNT, "controlled_junction_count", "count", "calculation"),
    (FEATURE_TOPOLOGICAL_JUNCTION_COUNT, "topological_junction_count", "count", "calculation"),
    (FEATURE_NEAREST_JUNCTION_DISTANCE_M, "nearest_junction_distance_m", "meters", "calculation"),
    (FEATURE_PEDESTRIAN_DENSITY_PER_KM2, "pedestrian_infrastructure_density_per_km2", "count/km2", "calculation"),
    (FEATURE_NETWORK_EUCLIDEAN_ACCESS_RATIO, "network_euclidean_access_ratio", "ratio", "calculation"),
]


# =============================================================================
# Django-facing plumbing (local imports throughout -- keeps the pure-math
# layer above DB/network-import-free, matching this codebase's established
# split for Engine 12/13/15).
# =============================================================================

def build_overpass_query(lat: float, lng: float, radius_m: int, timeout: int = OVERPASS_QUERY_TIMEOUT_SECONDS) -> str:
    """One Overpass QL query per cell (module docstring's cost-control
    discipline): every highway=* way, plus explicitly tagged junction/
    crossing nodes, within radius_m. 'out body geom;' (not 'out geom;'
    alone) so ways carry BOTH their member node IDs ('nodes', needed for
    topological-junction detection) AND resolved coordinates per node
    ('geometry', needed for point-to-segment distance) in one response --
    the combined verbosity mode Overpass's own documentation defines for
    exactly this "need real geometry AND graph structure" case."""
    clauses = "".join([
        f'way["highway"](around:{radius_m},{lat},{lng});',
        f'node["highway"~"traffic_signals|crossing|mini_roundabout|stop|give_way"](around:{radius_m},{lat},{lng});',
    ])
    return f'[out:json][timeout:{timeout}];({clauses});out body geom;'


def _load_catchment_areas(cell) -> Tuple[Optional[float], Optional[float]]:
    """Best-available (network_area_km2, euclidean_area_km2) pair from
    this cell's already-computed models.Catchment rows (module
    docstring's 'A FIFTH, SEPARATE SIGNAL'). Prefers the smallest
    matching-size network/radius pair (closest to this engine's own
    site-level scope) over a larger one, if more than one size is on
    record. Returns (None, None) if no matching pair exists -- this
    engine issues no isochrone request of its own."""
    catchments = list(cell.catchments.all())
    network_by_key: Dict[Tuple[str, int], float] = {}
    radius_by_size: Dict[int, float] = {}

    for c in catchments:
        if c.area_km2 is None:
            continue
        area = float(c.area_km2)
        if c.catchment_type == "radius":
            radius_by_size[c.catchment_size_value] = area
        elif c.catchment_type in ("network_radius", "walk_isochrone", "drive_isochrone", "transit_isochrone"):
            network_by_key[(c.catchment_type, c.catchment_size_value)] = area

    if not network_by_key or not radius_by_size:
        return None, None

    # network_radius rows share catchment_size_value's *meters* semantics
    # with 'radius' rows (models.Catchment.clean() enforces this), so only
    # those pair directly on size; isochrones (minutes) have no size-
    # comparable 'radius' counterpart in this schema and are skipped for
    # this specific ratio -- a genuine scope limit, not a bug: an apples-
    # to-apples circuity ratio needs the same nominal reach on both sides.
    matching_sizes = sorted(
        size for (ctype, size) in network_by_key if ctype == "network_radius" and size in radius_by_size
    )
    if not matching_sizes:
        return None, None
    size = matching_sizes[0]
    return network_by_key[("network_radius", size)], radius_by_size[size]


def _load_overpass_elements(cell, radius_m: int = SEARCH_RADIUS_M) -> Tuple[list, bool]:
    from ..google_client import query_osm_overpass

    lat, lng = float(cell.center_latitude), float(cell.center_longitude)
    overpass_query = build_overpass_query(lat, lng, radius_m)
    return query_osm_overpass(cell, overpass_query, purpose="visibility_access", radius_m=radius_m)


def compute_visibility_access_score(query, search_radius_m: int = SEARCH_RADIUS_M) -> tuple:
    """Real Django-facing Engine 16 entry point, matching the (result,
    evidence) tuple shape compute_property_cost_score /
    compute_infrastructure_change_score already return. Writes its own
    FeatureObservation rows and an EngineRun row, matching those engines'
    run() contract."""
    from ..models import EngineRun, FeatureObservation
    from django.utils import timezone as _timezone

    started_at = _timezone.now()
    cell = query.location_cell

    run_row = EngineRun.objects.create(
        query=query, engine_number=ENGINE_NUMBER, status="succeeded", started_at=started_at,
    )

    elements, succeeded = _load_overpass_elements(cell, radius_m=search_radius_m)
    if not succeeded and not elements:
        msg = "OSM Overpass query failed or returned no data (all endpoints, after retries) -- Visibility & Access engine could not run."
        logger.warning("Visibility & Access engine: %s (cell=%s)", msg, cell.geohash)
        run_row.status = "failed"
        run_row.error_message = msg
        run_row.finished_at = _timezone.now()
        run_row.save(update_fields=["status", "error_message", "finished_at"])
        return None, {"warnings": [msg], "engine_run_status": "failed"}

    network_area_km2, euclidean_area_km2 = _load_catchment_areas(cell)

    result = compute_visibility_access_result(
        elements, float(cell.center_latitude), float(cell.center_longitude),
        search_radius_m=search_radius_m, network_area_km2=network_area_km2, euclidean_area_km2=euclidean_area_km2,
    )
    evidence = visibility_access_evidence(result)
    evidence["engine_run_status"] = "ok" if result.n_ways_total > 0 else "ok_no_nearby_roads"

    features_written = 0
    for field_name, attr, unit, derivation_level in METRIC_FIELDS:
        value = getattr(result, attr)
        if value is None:
            continue
        FeatureObservation.write_latest(
            location_cell=cell, engine_number=ENGINE_NUMBER, feature_name=field_name,
            defaults=dict(
                value_type="numeric",
                value_numeric=Decimal(str(round(float(value), 6))),
                unit=unit,
                observed_at=started_at,
                source="business_intel.engines.visibility_access",
                source_class="internal_derived",
                method=(
                    "saturating_exponential_transform" if field_name == FEATURE_VISIBILITY_EXPOSURE_SCORE
                    else "osm_overpass_geometric_calculation"
                ),
                derivation_level=derivation_level,
                sample_size=result.n_ways_total,
            ),
        )
        features_written += 1

    # Categorical + boolean fields, written separately since METRIC_FIELDS'
    # numeric write path above only handles value_numeric -- same split
    # FeatureObservation.clean()'s own value_type/value_field constraint
    # requires.
    if result.nearest_road_class is not None:
        FeatureObservation.write_latest(
            location_cell=cell, engine_number=ENGINE_NUMBER, feature_name=FEATURE_NEAREST_ROAD_CLASS,
            defaults=dict(
                value_type="categorical", value_text=result.nearest_road_class, unit="",
                observed_at=started_at, source="business_intel.engines.visibility_access",
                source_class="internal_derived", method="osm_highway_tag_classification",
                derivation_level="observation", sample_size=result.n_ways_total,
            ),
        )
        features_written += 1

    FeatureObservation.write_latest(
        location_cell=cell, engine_number=ENGINE_NUMBER, feature_name=FEATURE_IS_CORNER_PROXIMATE,
        defaults=dict(
            value_type="boolean", value_json=result.is_corner_proximate, unit="",
            observed_at=started_at, source="business_intel.engines.visibility_access",
            source_class="internal_derived", method="junction_proximity_threshold",
            derivation_level="calculation", sample_size=result.n_ways_total,
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

    return result, evidence


def run(query, search_radius_m: int = SEARCH_RADIUS_M) -> dict:
    """Engine 16 entry point. Thin wrapper around
    compute_visibility_access_score(query, search_radius_m) -- see that
    function's docstring for what it actually computes/persists. Returns
    a dict shaped like every other wired engine's run() (score, evidence,
    engine_run), so tasks.py's _run_engine() can call it interchangeably
    with the rest. (Not currently called by tasks.py -- see module
    docstring's 'KNOWN UPSTREAM GAP'.)"""
    from ..models import EngineRun

    cell = query.location_cell
    result, evidence = compute_visibility_access_score(query, search_radius_m=search_radius_m)

    run_row = (
        EngineRun.objects.filter(query=query, engine_number=ENGINE_NUMBER)
        .order_by("-started_at")
        .first()
    )

    return {"cell": cell, "result": result, "evidence": evidence, "engine_run": run_row}