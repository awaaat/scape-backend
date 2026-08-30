"""
business_intel/engines/spatial_context.py

Engine 1 — Spatial Context & Catchment Engine
Per business_location_intelligence_engines_data_models_v2.md, Section 1.

Purpose
-------
Creates the geographic and network context in which every other engine
operates: resolves a query's coordinate into a persistent
BusinessLocationCell, materializes every Catchment (circular distance
band + travel-time isochrone) the spec defines, and computes network
accessibility scores using real shortest-path travel time — not
straight-line distance.

Scope boundary
--------------
This module depends ONLY on business_intel.models. It does not import
services.py, tasks.py, or google_client.py — those files currently
reference model fields/constants that don't exist on the real schema
and are being rebuilt separately. Nothing here assumes they exist.

What this engine actually produces (per spec Section 1)
---------------------------------------------------------
1. Distance-band Catchments: 250m, 500m, 1km, 2km, 3km, 5km, 10km.
   Circular area = pi * r^2. Network-reachable area used instead of the
   circular approximation wherever a road network graph is available.
2. Travel-time Catchments: walk 5/10/15/20/30 min, drive 5/10/15/20/30
   min (x3 traffic profiles: off_peak/peak/weekend), transit 15/30/45
   min where transit network data exists.
3. Accessibility score: Accessibility_i = sum_j( W_j * f(travel_time_ij) )
   using real network travel time (Dijkstra/A*) and a distance-decay
   function, per destinations supplied by the caller (this engine
   provides the capability; the weights W_j belong to whichever engine
   or caller has them — population cells, anchors, businesses, etc.).

Real GIS dependency
--------------------
Isochrone generation and shortest-path accessibility require an actual
road-network graph (OSM via OSMnx, per the build plan's Phase 2 tech
stack: pysochrone / graphways / OSMnx / GeoPandas). That is genuine
infrastructure, not something this module can fake without silently
misrepresenting every downstream catchment calculation — exactly the
same judgment call google_client.py's fetch_isochrone() docstring
makes ("not a stub worth faking with a fixed-radius circle").

So the network layer is behind a RoadNetworkProvider interface:

    - OSMnxRoadNetworkProvider  -- real implementation. Requires network
      access to OSM (Overpass/download API) at call time. This is the
      production provider.
    - SyntheticGridRoadNetworkProvider -- deterministic local grid graph,
      no network calls. For tests and offline development only. Never
      use in production; it does not represent real roads.

Traffic-aware driving speeds
------------------------------
Per google_client.py's own note, Uber Movement (movement.uber.com) is
dead (confirmed 404). This module does not depend on it. Driving
isochrones instead apply a TrafficProfileAdjuster: a peak/off-peak/
weekend multiplier on top of OSM-tagged (or inferred) free-flow speeds.
If real traffic-aware travel times are available (e.g. from the Routes
API, per google_client.fetch_traffic_aware_duration), pass them in via
the `travel_time_seconds_fn` hook on the network provider instead of
relying on the static multiplier -- the multiplier is a documented
fallback, not the production ceiling.
"""

from __future__ import annotations

import logging
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable, Iterable, Optional, Sequence

from django.db import transaction
from django.utils import timezone

# Module-level osmnx timeout -- must be set here, not inside any one
# function, because multiple code paths (build_radius_catchments,
# OSMnxRoadNetworkProvider.get_graph via build_isochrone_catchments)
# independently do `import osmnx as ox` and call it directly. Setting
# the timeout only in one of those functions left every other path
# using osmnx's default (effectively unbounded) timeout, which is what
# caused transit-isochrone fetches to hang for minutes.
import osmnx as ox
ox.settings.timeout = 30

from ..models import (
    BusinessLocationCell,
    BusinessQuery,
    Catchment,
    EngineRun,
    FeatureObservation,
    DISTANCE_BAND_METERS,
    WALK_TIME_MINUTES,
    DRIVE_TIME_MINUTES,
    TRANSIT_TIME_MINUTES,
)

logger = logging.getLogger("business_intel.engines.spatial_context")

ENGINE_NUMBER = 1

EARTH_RADIUS_M = 6_371_000.0

# Default free-flow walking / transit speeds where no network graph
# is available at all (used only by the circular-catchment fallback
# to size a sanity-check radius; never used for isochrone geometry).
DEFAULT_WALK_SPEED_KMH = 5.0

# --------------------------------------------------------------------
# Traffic profile adjustment (Uber Movement is dead -- see docstring)
# --------------------------------------------------------------------

# Multiplier applied to free-flow travel time. >1.0 = slower than
# free-flow. Calibrate against real observed data (Routes API
# departure_time samples, county traffic studies) as it becomes
# available; these are conservative placeholder priors, not measured
# constants, and every isochrone built from them is tagged
# computation_method="static_speed_traffic_multiplier" so it is never
# confused with a Routes-API-measured value downstream.
DEFAULT_TRAFFIC_MULTIPLIERS = {
    "off_peak": 1.00,
    "peak": 1.65,
    "weekend": 1.15,
}


class TrafficProfileAdjuster:
    """Pluggable peak/off-peak/weekend travel-time multiplier.

    Swap in a calibrated version (fit against Routes API samples or
    county traffic counts) without touching any caller of this class.
    """

    def __init__(self, multipliers: Optional[dict] = None):
        self.multipliers = dict(DEFAULT_TRAFFIC_MULTIPLIERS)
        if multipliers:
            self.multipliers.update(multipliers)

    def adjust(self, free_flow_seconds: float, traffic_profile: str) -> float:
        factor = self.multipliers.get(traffic_profile, 1.0)
        return free_flow_seconds * factor


# --------------------------------------------------------------------
# Distance-decay functions (used by the accessibility calculation)
# --------------------------------------------------------------------

def _negative_exponential(travel_time_seconds: float, beta: float) -> float:
    """f(t) = exp(-beta * t). Standard Hansen-style decay; beta in 1/seconds."""
    return math.exp(-beta * travel_time_seconds)


def _power_decay(travel_time_seconds: float, beta: float) -> float:
    """f(t) = t^-beta. Undefined at t=0; guarded with a 1-second floor."""
    t = max(travel_time_seconds, 1.0)
    return t ** (-beta)


def _gaussian_decay(travel_time_seconds: float, beta: float) -> float:
    """f(t) = exp(-beta * t^2). Sharper falloff than negative exponential."""
    return math.exp(-beta * (travel_time_seconds ** 2))


def _linear_decay(travel_time_seconds: float, beta: float) -> float:
    """f(t) = max(0, 1 - beta * t). Hard cutoff once beta*t exceeds 1."""
    return max(0.0, 1.0 - beta * travel_time_seconds)


DECAY_FUNCTIONS: dict = {
    "negative_exponential": _negative_exponential,
    "power": _power_decay,
    "gaussian": _gaussian_decay,
    "linear": _linear_decay,
}


# --------------------------------------------------------------------
# Geometry helpers
# --------------------------------------------------------------------

def circle_area_km2(radius_m: float) -> Decimal:
    """Area = pi * r^2, per spec Section 1. Returns km^2 to 4 dp."""
    area_m2 = math.pi * (radius_m ** 2)
    return Decimal(str(round(area_m2 / 1_000_000.0, 4)))


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters. Used for fallback distance checks
    and for sizing the local equal-area projection below -- never as a
    substitute for real network travel time in an isochrone."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def _local_azimuthal_projection(center_lat: float, center_lon: float):
    """Returns forward/inverse functions for a simple local equidistant
    azimuthal projection centered on (center_lat, center_lon), accurate
    enough for isochrones spanning a few kilometers. Avoids a hard
    dependency on pyproj/GeoPandas for this module's core algorithm --
    swap in a proper CRS transform (e.g. via pyproj to a local UTM zone)
    if sub-meter precision is ever required downstream."""
    lat0 = math.radians(center_lat)
    lon0 = math.radians(center_lon)
    m_per_deg_lat = 111_132.92 - 559.82 * math.cos(2 * lat0) + 1.175 * math.cos(4 * lat0)
    m_per_deg_lon = 111_412.84 * math.cos(lat0) - 93.5 * math.cos(3 * lat0)

    def to_xy(lat: float, lon: float):
        x = (lon - center_lon) * m_per_deg_lon
        y = (lat - center_lat) * m_per_deg_lat
        return x, y

    return to_xy


def circular_polygon_geojson(center_lat: float, center_lon: float, radius_m: float, num_points: int = 64):
    """Analytic circle polygon (NOT a hull of sampled reachable-node
    points -- there are no nodes here, this is a plain distance-band
    catchment) projected to lat/lon via the same local azimuthal
    projection used everywhere else in this module. Returns
    (geojson_dict, area_km2), the same shape polygon_from_points
    returns, so callers can treat both interchangeably.

    Added to fix a real gap: build_radius_catchments() previously left
    geometry_geojson=None for every radius band whenever no
    network_provider was supplied (or the network fetch failed) --
    which meant engines.population.compute_catchment_population(),
    which unconditionally requires geometry_geojson to spatially join
    against its interpolated grid, could never return a population
    figure for a plain circular catchment. Since engines.competition
    only ever asks for a plain radius (it has no RoadNetworkProvider
    wired in), this made catchment_population permanently unreachable
    for the Competition engine. A circle is exact, known geometry --
    there is no honest reason to leave it as None instead of computing
    it, unlike the network-isochrone case (google_client.fetch_isochrone
    / build_isochrone_catchments), which correctly refuses to fake a
    circle in place of real road-network reachability.

    num_points=64 keeps area error under ~0.16% of the true pi*r^2
    value at any of these radii (verified: 250m-10km, multiple Kenyan
    latitudes) -- accurate enough that population-within-catchment
    error from this polygon is negligible next to the Pycnophylactic
    interpolation's own documented center-in-polygon-cell approximation
    (population.py's population_in_polygon docstring already flags
    that as the dominant source of imprecision, not this).
    """
    to_xy = _local_azimuthal_projection(center_lat, center_lon)
    lat0 = math.radians(center_lat)
    m_per_deg_lat = 111_132.92 - 559.82 * math.cos(2 * lat0) + 1.175 * math.cos(4 * lat0)
    m_per_deg_lon = 111_412.84 * math.cos(lat0) - 93.5 * math.cos(3 * lat0)

    coords = []
    for i in range(num_points):
        theta = 2 * math.pi * i / num_points
        x = radius_m * math.cos(theta)
        y = radius_m * math.sin(theta)
        lon = center_lon + x / m_per_deg_lon
        lat = center_lat + y / m_per_deg_lat
        coords.append((lon, lat))
    coords.append(coords[0])  # close the ring

    geojson = {"type": "Polygon", "coordinates": [coords]}
    area_km2 = circle_area_km2(radius_m)  # exact analytic value; no need to re-derive from the polygon
    return geojson, area_km2


def get_edge_attribute_value(edge_data: dict, key: str):
    """Reads an edge attribute correctly for BOTH plain Graph edge data
    (a flat attribute dict) and MultiGraph/MultiDiGraph edge data (a
    dict of parallel-edge-key -> attribute dict). Real OSMnx graphs are
    always MultiDiGraphs -- a naive edge_data.get(key) silently returns
    None on them, since `key` isn't a top-level key in that shape.
    Used by every Dijkstra/ego_graph weight callable in this module so
    this class of bug (confirmed to have produced silently-wrong
    "reachable within N meters" results, since a hop-count fallback of
    1 made every radius band return the identical, full-graph area)
    cannot recur silently.
    """
    if edge_data and all(isinstance(v, dict) for v in edge_data.values()):
        # MultiGraph/MultiDiGraph shape: take the minimum value among
        # parallel edges, same convention NetworkX itself uses when
        # `weight` is passed as a plain string on a multigraph.
        values = [v.get(key) for v in edge_data.values() if v.get(key) is not None]
        return min(values) if values else None
    return edge_data.get(key)


def polygon_from_points(points: Sequence[tuple], center_lat: float, center_lon: float):
    """Builds a concave-ish reachability boundary from a set of
    (lat, lon) reachable-node points using an alpha shape when the
    `alphashape` package is available, falling back to a convex hull
    (via shapely) otherwise. Returns (geojson_dict, area_km2) or
    (None, None) if fewer than 3 distinct points are given.
    """
    import shapely.geometry as geom
    from shapely.ops import unary_union

    if len(points) < 3:
        return None, None

    to_xy = _local_azimuthal_projection(center_lat, center_lon)
    xy_points = [to_xy(lat, lon) for lat, lon in points]

    shape = None
    try:
        import alphashape  # optional dependency; not required for the fallback path
        shape = alphashape.alphashape(xy_points, alpha=0.0)
    except ImportError:
        shape = None
    except Exception:  # noqa: BLE001 - alpha shape can fail on degenerate point sets
        shape = None

    if shape is None or shape.is_empty:
        shape = geom.MultiPoint(xy_points).convex_hull

    if shape.geom_type not in ("Polygon", "MultiPolygon"):
        # Degenerate (collinear points etc.) -- buffer slightly so we still
        # return a valid area rather than silently dropping the catchment.
        shape = shape.buffer(1.0)

    shape = unary_union([shape])
    area_km2 = Decimal(str(round(shape.area / 1_000_000.0, 4)))

    # Project polygon exterior back to lat/lon for storage.
    def to_lonlat(x, y):
        lat0 = math.radians(center_lat)
        m_per_deg_lat = 111_132.92 - 559.82 * math.cos(2 * lat0) + 1.175 * math.cos(4 * lat0)
        m_per_deg_lon = 111_412.84 * math.cos(lat0) - 93.5 * math.cos(3 * lat0)
        lon = center_lon + x / m_per_deg_lon
        lat = center_lat + y / m_per_deg_lat
        return lon, lat

    if shape.geom_type == "Polygon":
        rings = [shape]
    else:
        rings = list(shape.geoms)

    features_coords = []
    for ring in rings:
        coords = [to_lonlat(x, y) for x, y in ring.exterior.coords]
        features_coords.append([coords])

    geojson = {
        "type": "MultiPolygon" if len(features_coords) > 1 else "Polygon",
        "coordinates": features_coords if len(features_coords) > 1 else features_coords[0],
    }
    return geojson, area_km2


# --------------------------------------------------------------------
# Road network provider interface
# --------------------------------------------------------------------

@dataclass
class IsochroneResult:
    geometry_geojson: Optional[dict]
    area_km2: Optional[Decimal]
    reachable_node_count: int
    computation_method: str


class RoadNetworkProvider(ABC):
    """Interface every routing backend must satisfy. Engine code (and
    every other engine that needs travel time -- Mobility, Competition,
    Anchor, Market Gap, ...) depends on this interface, not on OSMnx or
    any other concrete library directly."""

    @abstractmethod
    def get_graph(self, center_lat: float, center_lon: float, max_dist_m: float, network_type: str):
        """Return a routable graph covering at least max_dist_m around
        the center point, for the given network_type ('walk', 'drive',
        or 'transit')."""

    @abstractmethod
    def nearest_node(self, graph, lat: float, lon: float):
        """Return the graph node id nearest to (lat, lon)."""

    @abstractmethod
    def node_coords(self, graph, node) -> tuple:
        """Return (lat, lon) for a graph node."""

    @abstractmethod
    def shortest_travel_time_seconds(self, graph, source_node, target_node) -> Optional[float]:
        """Dijkstra/A* shortest-path travel time in seconds, or None if
        unreachable."""

    @abstractmethod
    def ego_reachable_nodes(self, graph, source_node, max_travel_time_seconds: float) -> Iterable:
        """All nodes reachable from source_node within
        max_travel_time_seconds, via the graph's travel_time edge
        weight (networkx ego_graph pattern)."""


class OSMnxRoadNetworkProvider(RoadNetworkProvider):
    """Production provider: real OSM road network via OSMnx + NetworkX
    Dijkstra/A*. Requires network access to OSM data (Overpass API or a
    local .pbf extract loaded via osmnx.graph_from_xml) at call time --
    this is the "deploy pysochrone / graphways ... Dijkstra-based
    isochrone generation" step from the build plan's Phase 2.

    Graphs are cached in-process per (geohash, network_type, radius
    bucket) for the lifetime of the process. Swap `cache` for a Redis-
    backed implementation in production so isochrone requests for a
    popular cell don't refetch/rebuild the graph on every worker.
    """

    def __init__(self, traffic_adjuster: Optional[TrafficProfileAdjuster] = None, cache: Optional[dict] = None):
        self.traffic_adjuster = traffic_adjuster or TrafficProfileAdjuster()
        self._cache = cache if cache is not None else {}

    def _cache_key(self, center_lat, center_lon, max_dist_m, network_type):
        # Round coordinates to ~11m precision so nearby queries reuse cache.
        return (round(center_lat, 4), round(center_lon, 4), int(max_dist_m), network_type)

    def get_graph(self, center_lat, center_lon, max_dist_m, network_type):
        import osmnx as ox

        key = self._cache_key(center_lat, center_lon, max_dist_m, network_type)
        if key in self._cache:
            return self._cache[key]

        osmnx_network_type = {"walk": "walk", "drive": "drive", "transit": "all"}[network_type]
        graph = ox.graph_from_point(
            (center_lat, center_lon),
            dist=max_dist_m,
            network_type=osmnx_network_type,
            simplify=True,
        )

        if network_type == "drive":
            graph = ox.routing.add_edge_speeds(graph)
            graph = ox.routing.add_edge_travel_times(graph)
        else:
            walk_speed_mps = DEFAULT_WALK_SPEED_KMH * 1000 / 3600
            for _, _, _, data in graph.edges(keys=True, data=True):
                length_m = data.get("length", 0.0)
                data["travel_time"] = length_m / walk_speed_mps if walk_speed_mps else float("inf")

        self._cache[key] = graph
        return graph

    def nearest_node(self, graph, lat, lon):
        import osmnx as ox
        return ox.distance.nearest_nodes(graph, X=lon, Y=lat)

    def node_coords(self, graph, node):
        data = graph.nodes[node]
        return data["y"], data["x"]

    def _weighted_travel_time(self, graph, traffic_profile: Optional[str] = None):
        """Returns an edge-weight function honoring traffic_profile for
        driving graphs, plain travel_time otherwise.

        CRITICAL: OSMnx graphs are MultiDiGraphs (real roads can have
        multiple parallel edges between two nodes). When NetworkX is
        given a *callable* weight function on a MultiGraph/MultiDiGraph,
        it passes the callable the FULL dict of parallel edges
        (`{edge_key: attrs, ...}`), not a single edge's attribute dict
        -- unlike when weight is a plain string, where NetworkX handles
        that unwrapping internally. A naive `edge_data.get("travel_time")`
        therefore silently returns None for every edge on a real OSMnx
        graph, which is exactly why traffic-profile-adjusted drive
        isochrones were failing outright. get_edge_attribute_value()
        below handles both graph shapes correctly.
        """
        if traffic_profile is None:
            return "travel_time"

        def weight_fn(u, v, edge_data):
            base = get_edge_attribute_value(edge_data, "travel_time")
            if base is None:
                return None
            return self.traffic_adjuster.adjust(base, traffic_profile)

        return weight_fn

    def shortest_travel_time_seconds(self, graph, source_node, target_node, traffic_profile=None):
        import networkx as nx
        try:
            return nx.shortest_path_length(
                graph, source_node, target_node,
                weight=self._weighted_travel_time(graph, traffic_profile),
            )
        except nx.NetworkXNoPath:
            return None

    def ego_reachable_nodes(self, graph, source_node, max_travel_time_seconds, traffic_profile=None):
        import networkx as nx
        ego = nx.ego_graph(
            graph, source_node,
            radius=max_travel_time_seconds,
            distance=self._weighted_travel_time(graph, traffic_profile),
        )
        return list(ego.nodes)


class SyntheticGridRoadNetworkProvider(RoadNetworkProvider):
    """Deterministic local grid graph -- NO external network calls.

    For tests and offline development only. Generates a square grid of
    nodes spaced `spacing_m` apart, edges weighted by straight-line
    distance / a fixed speed. This does not represent real roads and
    must never be used to produce a Catchment served to an end user;
    it exists so the algorithm layer (Dijkstra, ego_graph, alpha-shape
    polygon extraction, accessibility scoring) can be exercised and
    verified without a live OSM connection.
    """

    def __init__(self, spacing_m: float = 100.0, speed_kmh: float = 20.0, grid_radius_cells: int = 40):
        self.spacing_m = spacing_m
        self.speed_mps = speed_kmh * 1000 / 3600
        self.grid_radius_cells = grid_radius_cells

    def get_graph(self, center_lat, center_lon, max_dist_m, network_type):
        import networkx as nx

        to_xy = _local_azimuthal_projection(center_lat, center_lon)
        n = min(self.grid_radius_cells, int(max_dist_m / self.spacing_m) + 2)

        # MultiGraph, not plain Graph -- matches real OSMnx graph shape
        # (roads can have parallel edges), so tests against this
        # provider actually exercise the MultiGraph edge-data handling
        # real usage needs, instead of silently passing on a simpler
        # graph type that never triggers that class of bug.
        graph = nx.MultiGraph()
        for i in range(-n, n + 1):
            for j in range(-n, n + 1):
                x, y = i * self.spacing_m, j * self.spacing_m
                lat0 = math.radians(center_lat)
                m_per_deg_lat = 111_132.92 - 559.82 * math.cos(2 * lat0) + 1.175 * math.cos(4 * lat0)
                m_per_deg_lon = 111_412.84 * math.cos(lat0) - 93.5 * math.cos(3 * lat0)
                lat = center_lat + y / m_per_deg_lat
                lon = center_lon + x / m_per_deg_lon
                graph.add_node((i, j), y=lat, x=lon)
                if i > -n:
                    graph.add_edge((i, j), (i - 1, j), travel_time=self.spacing_m / self.speed_mps)
                if j > -n:
                    graph.add_edge((i, j), (i, j - 1), travel_time=self.spacing_m / self.speed_mps)
        graph.graph["center"] = (0, 0)
        graph.graph["center_latlon"] = (center_lat, center_lon)
        return graph

    def nearest_node(self, graph, lat, lon):
        """Nearest grid node to an arbitrary (lat, lon), found by
        re-deriving that point's approximate grid indices from the same
        local projection used to build the graph, then checking the
        surrounding cells. O(1) rather than O(n) since grid spacing is
        known -- correct even for a destination point that isn't
        exactly on the grid."""
        center_lat, center_lon = graph.graph["center_latlon"]
        to_xy = _local_azimuthal_projection(center_lat, center_lon)
        x, y = to_xy(lat, lon)
        i0 = round(x / self.spacing_m)
        j0 = round(y / self.spacing_m)

        best_node, best_dist = None, float("inf")
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                node = (i0 + di, j0 + dj)
                if node not in graph.nodes:
                    continue
                node_lat, node_lon = self.node_coords(graph, node)
                dist = haversine_m(lat, lon, node_lat, node_lon)
                if dist < best_dist:
                    best_node, best_dist = node, dist

        if best_node is None:
            # Fell outside the generated grid radius -- clamp to nearest edge node.
            i0 = max(-self.grid_radius_cells, min(self.grid_radius_cells, i0))
            j0 = max(-self.grid_radius_cells, min(self.grid_radius_cells, j0))
            best_node = (i0, j0)
        return best_node

    def node_coords(self, graph, node):
        data = graph.nodes[node]
        return data["y"], data["x"]

    def shortest_travel_time_seconds(self, graph, source_node, target_node, traffic_profile=None):
        import networkx as nx
        try:
            seconds = nx.shortest_path_length(graph, source_node, target_node, weight="travel_time")
        except nx.NetworkXNoPath:
            return None
        if traffic_profile:
            adjuster = TrafficProfileAdjuster()
            seconds = adjuster.adjust(seconds, traffic_profile)
        return seconds

    def ego_reachable_nodes(self, graph, source_node, max_travel_time_seconds, traffic_profile=None):
        import networkx as nx
        radius = max_travel_time_seconds
        if traffic_profile:
            factor = TrafficProfileAdjuster().multipliers.get(traffic_profile, 1.0)
            radius = max_travel_time_seconds / factor
        ego = nx.ego_graph(graph, source_node, radius=radius, distance="travel_time")
        return list(ego.nodes)


# --------------------------------------------------------------------
# Catchment builders
# --------------------------------------------------------------------

def build_radius_catchments(
    location_cell: BusinessLocationCell,
    network_provider: Optional[RoadNetworkProvider] = None,
    distance_bands: Optional[Sequence[int]] = None,
) -> list:
    # already patched: tightened lock scope + osmnx timeout.
    # v1 of this lock wrapped the ENTIRE function -- including the live
    # OSMnx network fetch below -- in transaction.atomic()+select_for_update().
    # That correctly serialized the racy Catchment INSERT, but also meant
    # that while the (unbounded, no-timeout) OSMnx call was slow/hung, EVERY
    # other request touching this same LocationCell -- including a totally
    # unrelated new query submission from the dashboard -- blocked on that
    # Postgres row lock too, which is worse. Fetch the graph OUTSIDE any
    # lock (it's read-only, nothing to race), and only take the lock around
    # the actual DB write loop, which is fast.
    import osmnx as ox
    ox.settings.timeout = 30  # was unset -- osmnx would otherwise retry/backoff
                              # on OSM rate-limiting for a long, unbounded time
    from django.db import transaction
    return _build_radius_catchments_locked(
        location_cell, network_provider=network_provider, distance_bands=distance_bands,
    )


def _build_radius_catchments_locked(
    location_cell: BusinessLocationCell,
    network_provider: Optional[RoadNetworkProvider] = None,
    distance_bands: Optional[Sequence[int]] = None,
) -> list:
    """Distance-band catchments per spec: 250m..10km.

    If a network_provider is supplied, area is computed from the
    actual reachable road network within radius_m of travel *distance*
    (not time) rather than the circular approximation, per spec's
    explicit instruction: "For network catchments, use actual reachable
    network area rather than circular area." Falls back to the circular
    Area = pi*r^2 formula when no provider is given.

    Fetches the road graph ONCE at the largest requested radius and
    reuses it for every smaller band via Dijkstra's cutoff parameter --
    a smaller cutoff on the same graph naturally stops earlier, giving
    the correct smaller reachable set without a second network fetch.
    Fetching a fresh graph per radius (the original approach) triggered
    one live OSM download per band -- 7x slower for no benefit, since
    the 10km graph already contains every node reachable within 250m.
    """
    distance_bands = distance_bands or DISTANCE_BAND_METERS
    catchments = []

    shared_graph = None
    shared_source = None
    if network_provider is not None:
        try:
            max_radius_m = max(distance_bands)
            shared_graph = network_provider.get_graph(
                float(location_cell.center_latitude), float(location_cell.center_longitude),
                max_dist_m=max_radius_m, network_type="drive",
            )
            shared_source = network_provider.nearest_node(
                shared_graph, float(location_cell.center_latitude), float(location_cell.center_longitude)
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "Failed to fetch shared road graph for cell %s; all radius bands will use circular fallback.",
                location_cell.geohash,
            )
            shared_graph = None

    # already patched: lock scoped to the write loop only (not the
    # graph fetch above, which is read-only and can be slow without
    # blocking anything else -- see build_radius_catchments()'s comment).
    from django.db import transaction
    with transaction.atomic():
        type(location_cell).objects.select_for_update().get(pk=location_cell.pk)
        for radius_m in distance_bands:
            is_network_based = False
            # Real circle geometry by default now (see circular_polygon_geojson's
            # docstring for why this used to be None and what that broke
            # downstream in engines.population.compute_catchment_population).
            # A network-reachable polygon below, if available, overwrites this
            # with the more accurate reachable-area shape.
            geometry_geojson, area_km2 = circular_polygon_geojson(
                float(location_cell.center_latitude), float(location_cell.center_longitude), radius_m,
            )
            computation_method = "circular_buffer"

            if shared_graph is not None:
                try:
                    import networkx as nx
                    reachable = nx.single_source_dijkstra_path_length(
                        shared_graph, shared_source, cutoff=radius_m,
                        weight=lambda u, v, d: get_edge_attribute_value(d, "length") or get_edge_attribute_value(d, "travel_time") or 1,
                    )
                    points = [network_provider.node_coords(shared_graph, n) for n in reachable]
                    network_geojson, network_area_km2 = polygon_from_points(
                        points, float(location_cell.center_latitude), float(location_cell.center_longitude)
                    )
                    if network_geojson is not None:
                        geometry_geojson = network_geojson
                        area_km2 = network_area_km2
                        is_network_based = True
                        computation_method = "network_reachable_dijkstra"
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "Network-based radius catchment failed for cell %s @ %sm; falling back to circular area.",
                        location_cell.geohash, radius_m,
                    )

            catchment, _created = Catchment.objects.update_or_create(
                location_cell=location_cell,
                catchment_type="radius",
                catchment_size_value=radius_m,
                traffic_profile="",
                defaults=dict(
                    catchment_size_unit="meters",
                    catchment_size_label=f"{radius_m}m" if radius_m < 1000 else f"{radius_m // 1000}km",
                    is_network_based=is_network_based,
                    geometry_geojson=geometry_geojson,
                    area_km2=area_km2,
                    computation_method=computation_method,
                ),
            )
            catchments.append(catchment)

    return catchments


_ISOCHRONE_TYPE_MAP = {
    "walk": "walk_isochrone",
    "drive": "drive_isochrone",
    "transit": "transit_isochrone",
}


def build_isochrone_catchments(
    location_cell: BusinessLocationCell,
    mode: str,
    minutes_list: Sequence[int],
    network_provider: RoadNetworkProvider,
    traffic_profile: str = "",
) -> list:
    """Travel-time catchments for one mode. Requires a network_provider
    -- an isochrone with no routing backend is not a real isochrone, so
    this raises rather than silently writing a fake circle (matching
    google_client.fetch_isochrone's existing judgment call)."""
    if mode not in _ISOCHRONE_TYPE_MAP:
        raise ValueError(f"Unknown isochrone mode: {mode!r}")
    catchment_type = _ISOCHRONE_TYPE_MAP[mode]

    if catchment_type != "drive_isochrone" and traffic_profile:
        raise ValueError("traffic_profile only applies to drive_isochrone.")
    if network_provider is None:
        raise ValueError(
            "build_isochrone_catchments requires a RoadNetworkProvider. "
            "Real isochrones need real road network data; there is no honest fallback."
        )

    center_lat = float(location_cell.center_latitude)
    center_lon = float(location_cell.center_longitude)
    max_minutes = max(minutes_list)

    # Generous distance bound for the graph fetch, sized per mode's real
    # free-flow speed -- NOT a single car-speed assumption for every
    # mode. The previous version used 40km/h even for walk/transit
    # isochrones, so a 30-minute WALK isochrone requested a 26km-radius
    # road network (30min at 40km/h) instead of the ~2.5km a walk
    # actually covers -- a hugely oversized, slow OSM fetch for no
    # benefit. Each mode now sizes its own fetch radius correctly.
    MODE_SPEED_KMH = {
        "walk": DEFAULT_WALK_SPEED_KMH,   # ~5 km/h
        "drive": 40.0,                     # brisk urban free-flow
        "transit": 20.0,                   # bus/matatu average incl. stops
    }
    speed_kmh = MODE_SPEED_KMH.get(mode, 40.0)
    max_dist_m = max_minutes * 60 * (speed_kmh * 1000 / 3600) * 1.3

    graph = network_provider.get_graph(center_lat, center_lon, max_dist_m=max_dist_m, network_type=mode)
    source_node = network_provider.nearest_node(graph, center_lat, center_lon)

    catchments = []
    for minutes in minutes_list:
        max_seconds = minutes * 60
        method_kwargs = {"traffic_profile": traffic_profile} if catchment_type == "drive_isochrone" else {}

        try:
            reachable_nodes = network_provider.ego_reachable_nodes(graph, source_node, max_seconds, **method_kwargs)
        except TypeError:
            # SyntheticGridRoadNetworkProvider / OSMnxRoadNetworkProvider both
            # accept traffic_profile; a bare custom provider might not.
            reachable_nodes = network_provider.ego_reachable_nodes(graph, source_node, max_seconds)

        points = [network_provider.node_coords(graph, n) for n in reachable_nodes]
        geometry_geojson, area_km2 = polygon_from_points(points, center_lat, center_lon)

        is_osmnx = isinstance(network_provider, OSMnxRoadNetworkProvider)
        traffic_adjusted = catchment_type == "drive_isochrone" and bool(traffic_profile)
        if is_osmnx:
            computation_method = "osmnx_dijkstra_traffic_adjusted" if traffic_adjusted else "osmnx_dijkstra"
        else:
            computation_method = "synthetic_grid_dijkstra_traffic_adjusted" if traffic_adjusted else "synthetic_grid_dijkstra"

        catchment, _created = Catchment.objects.update_or_create(
            location_cell=location_cell,
            catchment_type=catchment_type,
            catchment_size_value=minutes,
            traffic_profile=traffic_profile,
            defaults=dict(
                catchment_size_unit="minutes",
                catchment_size_label=f"{minutes}min",
                is_network_based=geometry_geojson is not None,
                geometry_geojson=geometry_geojson,
                area_km2=area_km2,
                computation_method=computation_method,
            ),
        )
        catchments.append(catchment)

    return catchments


def build_all_catchments(
    location_cell: BusinessLocationCell,
    network_provider: RoadNetworkProvider,
    drive_traffic_profiles: Sequence[str] = ("off_peak", "peak", "weekend"),
    # DEV NOTE: transit isochrones fetch a ~19.5km-radius "all roads" OSM
    # graph (45min @ 20km/h) since there is no real transit-route data
    # source wired in yet -- that single fetch/graph-build is what was
    # hanging generate_business_report for minutes at a time locally.
    # Defaulting this to False unblocks local dev; flip back to True
    # (or pass it explicitly at the call site) once the graph is
    # disk/Redis-cached per cell instead of only in-process (see
    # OSMnxRoadNetworkProvider's own docstring, which already flags
    # this as a known gap) -- after that the first-fetch cost is paid
    # once per cell, not once per request.
    include_transit: bool = False,
) -> list:
    """Full Engine 1 sweep for one cell: every radius band + every
    isochrone mode/size the spec defines. `include_transit` defaults to
    True but should be set False for cells with no known transit
    network -- per spec, transit isochrones are generated "where data
    exists", not unconditionally."""
    catchments = []
    catchments += build_radius_catchments(location_cell, network_provider=network_provider)
    catchments += build_isochrone_catchments(location_cell, "walk", WALK_TIME_MINUTES, network_provider)
    if include_transit:
        catchments += build_isochrone_catchments(location_cell, "transit", TRANSIT_TIME_MINUTES, network_provider)
    for profile in drive_traffic_profiles:
        catchments += build_isochrone_catchments(
            location_cell, "drive", DRIVE_TIME_MINUTES, network_provider, traffic_profile=profile
        )
    return catchments


# --------------------------------------------------------------------
# Accessibility (spec Section 1, "Calculations > Accessibility")
# --------------------------------------------------------------------

@dataclass
class AccessibilityDestination:
    """One destination `j` in Accessibility_i = sum_j( W_j * f(t_ij) ).
    W_j is whatever weight the caller's engine cares about -- population
    of a cell, an anchor's size_value, a competitor's strength score,
    etc. This engine does not decide what W_j means; it only computes
    the network travel time and applies the decay function."""
    latitude: float
    longitude: float
    weight: float
    label: str = ""


@dataclass
class AccessibilityResult:
    score: float
    decay_function: str
    beta: float
    mode: str
    destinations_used: int
    destinations_unreachable: int
    evidence: list = field(default_factory=list)


def compute_accessibility_score(
    location_cell: BusinessLocationCell,
    destinations: Sequence[AccessibilityDestination],
    network_provider: RoadNetworkProvider,
    mode: str = "drive",
    decay: str = "negative_exponential",
    beta: float = 0.0008,
    traffic_profile: Optional[str] = None,
) -> AccessibilityResult:
    """Accessibility_i = sum_j( W_j * f(travel_time_ij) ), per spec.

    Uses real network shortest-path travel time (Dijkstra/A* via the
    supplied provider) for every destination -- the spec is explicit
    that competitor/anchor distance must be network distance/time, not
    straight-line, and this is the shared implementation every engine
    with a "distance to X" feature should call rather than reimplementing
    its own straight-line shortcut.

    beta's default (0.0008 /second) corresponds to a decay to ~1/e
    (37%) of full weight at roughly a 20-minute network travel time --
    a reasonable prior for urban Kenya, not a fitted constant. Calibrate
    per business category once outcome data exists (see spec's
    Confidence Engine notes on calibrating against historical error).
    """
    if decay not in DECAY_FUNCTIONS:
        raise ValueError(f"Unknown decay function {decay!r}. Choices: {list(DECAY_FUNCTIONS)}")
    decay_fn = DECAY_FUNCTIONS[decay]

    center_lat = float(location_cell.center_latitude)
    center_lon = float(location_cell.center_longitude)

    max_dist_m = max(
        (haversine_m(center_lat, center_lon, d.latitude, d.longitude) for d in destinations),
        default=1000.0,
    ) * 1.3
    graph = network_provider.get_graph(center_lat, center_lon, max_dist_m=max_dist_m, network_type=mode)
    source_node = network_provider.nearest_node(graph, center_lat, center_lon)

    score = 0.0
    evidence = []
    unreachable = 0

    for dest in destinations:
        target_node = network_provider.nearest_node(graph, dest.latitude, dest.longitude)
        kwargs = {"traffic_profile": traffic_profile} if mode == "drive" and traffic_profile else {}
        try:
            travel_time = network_provider.shortest_travel_time_seconds(graph, source_node, target_node, **kwargs)
        except TypeError:
            travel_time = network_provider.shortest_travel_time_seconds(graph, source_node, target_node)

        if travel_time is None:
            unreachable += 1
            continue

        contribution = dest.weight * decay_fn(travel_time, beta)
        score += contribution
        evidence.append({
            "destination": dest.label or f"({dest.latitude}, {dest.longitude})",
            "weight": dest.weight,
            "travel_time_seconds": round(travel_time, 1),
            "decay_value": round(decay_fn(travel_time, beta), 6),
            "contribution": round(contribution, 6),
        })

    evidence.sort(key=lambda e: e["contribution"], reverse=True)

    return AccessibilityResult(
        score=score,
        decay_function=decay,
        beta=beta,
        mode=mode,
        destinations_used=len(destinations) - unreachable,
        destinations_unreachable=unreachable,
        evidence=evidence,
    )


# --------------------------------------------------------------------
# R-tree spatial index (for callers needing fast nearest-destination
# lookups before deciding which destinations to even run through
# compute_accessibility_score -- e.g. "give me the 50 nearest
# businesses" before computing network time to each of them).
# --------------------------------------------------------------------

def build_spatial_index(points: Sequence[tuple]):
    """points: sequence of (id, lat, lon). Returns an rtree.index.Index
    for O(log n) nearest/bbox queries, per spec's "R-tree spatial
    indexing" requirement. The index stores integer positions; look up
    the original id via `points[hit_index][0]`."""
    from rtree import index as rtree_index

    idx = rtree_index.Index()
    for i, (_id, lat, lon) in enumerate(points):
        idx.insert(i, (lon, lat, lon, lat))
    return idx


def nearest_via_index(idx, points: Sequence[tuple], lat: float, lon: float, num_results: int = 10) -> list:
    """Returns the `num_results` nearest (id, lat, lon) entries to
    (lat, lon) using the R-tree built by build_spatial_index."""
    hits = list(idx.nearest((lon, lat, lon, lat), num_results))
    return [points[i] for i in hits]


# --------------------------------------------------------------------
# Location cell resolution
# --------------------------------------------------------------------

def get_or_create_location_cell(latitude, longitude, formatted_address: str = "", county: str = "", ward: str = ""):
    """Resolve (lat, lon) to a persistent BusinessLocationCell, reusing
    an existing cell at this geohash precision if one exists (per
    models.compute_geohash's ~150m cell size). Increments times_reused
    so cache-hit-rate is observable without a separate log table."""
    from django.db.models import F
    from ..models import compute_geohash

    quantum = Decimal("0.0000001")
    lat_decimal = Decimal(str(latitude)).quantize(quantum)
    lon_decimal = Decimal(str(longitude)).quantize(quantum)

    geohash_value = compute_geohash(lat_decimal, lon_decimal)
    cell, created = BusinessLocationCell.objects.get_or_create(
        geohash=geohash_value,
        defaults=dict(
            center_latitude=lat_decimal,
            center_longitude=lon_decimal,
            formatted_address=formatted_address,
            county=county,
            ward=ward,
        ),
    )
    if not created:
        BusinessLocationCell.objects.filter(pk=cell.pk).update(times_reused=F("times_reused") + 1)
        cell.refresh_from_db(fields=["times_reused"])
    return cell, created


# --------------------------------------------------------------------
# Orchestration / EngineRun integration
# --------------------------------------------------------------------

def run(
    query: BusinessQuery,
    network_provider: RoadNetworkProvider,
    destinations: Optional[Sequence[AccessibilityDestination]] = None,
    drive_traffic_profiles: Sequence[str] = ("off_peak", "peak", "weekend"),
    include_transit: bool = True,
    accessibility_mode: str = "drive",
    accessibility_decay: str = "negative_exponential",
    accessibility_beta: float = 0.0008,
) -> dict:
    """Engine 1 entry point. Builds every Catchment for query's cell and,
    if destinations were supplied, writes an accessibility_score
    FeatureObservation. Records an EngineRun row either way.

    Idempotent: catchment writes are update_or_create, and
    FeatureObservation writes go through write_latest(), so re-running
    this for the same query safely produces a new feature version
    rather than a duplicate "latest" row.
    """
    started_at = timezone.now()
    cell = query.location_cell

    run_row = EngineRun.objects.create(
        query=query,
        engine_number=ENGINE_NUMBER,
        status="succeeded",
        started_at=started_at,
    )

    features_written = 0
    degraded_reason = ""

    try:
        with transaction.atomic():
            catchments = build_all_catchments(
                cell,
                network_provider=network_provider,
                drive_traffic_profiles=drive_traffic_profiles,
                include_transit=include_transit,
            )

            accessibility_result = None
            if destinations:
                accessibility_result = compute_accessibility_score(
                    cell, destinations, network_provider,
                    mode=accessibility_mode, decay=accessibility_decay, beta=accessibility_beta,
                )
                FeatureObservation.write_latest(
                    location_cell=cell,
                    engine_number=ENGINE_NUMBER,
                    feature_name="accessibility_score",
                    defaults=dict(
                        value_type="numeric",
                        value_numeric=Decimal(str(round(accessibility_result.score, 6))),
                        value_json={
                            "decay_function": accessibility_result.decay_function,
                            "beta": accessibility_result.beta,
                            "mode": accessibility_result.mode,
                            "destinations_used": accessibility_result.destinations_used,
                            "destinations_unreachable": accessibility_result.destinations_unreachable,
                            "top_contributors": accessibility_result.evidence[:10],
                        },
                        unit="accessibility_index",
                        observed_at=started_at,
                        source="business_intel.engines.spatial_context",
                        source_class="internal_derived",
                        method=f"hansen_accessibility_{accessibility_result.decay_function}_decay",
                        derivation_level="calculation",
                    ),
                )
                features_written = 1
                if accessibility_result.destinations_unreachable:
                    degraded_reason = (
                        f"{accessibility_result.destinations_unreachable}/"
                        f"{len(destinations)} accessibility destinations unreachable on the network graph."
                    )
            elif isinstance(network_provider, SyntheticGridRoadNetworkProvider):
                degraded_reason = "SyntheticGridRoadNetworkProvider in use -- not real road data."

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
    if degraded_reason:
        run_row.status = "degraded"
        run_row.degraded_reason = degraded_reason
    run_row.save(update_fields=["status", "finished_at", "duration_ms", "features_written", "degraded_reason"])

    return {
        "cell": cell,
        "catchments": catchments,
        "accessibility": accessibility_result if destinations else None,
        "engine_run": run_row,
    }