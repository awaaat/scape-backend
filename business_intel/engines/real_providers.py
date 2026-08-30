"""
business_intel/engines/real_providers.py

A REAL (not synthetic) AdministrativeHierarchyProvider, at county-level
resolution, built from:

  - Real 2019 & 2009 KNBS census population, households, and land-area
    figures for all 47 counties (business_intel/data/
    kenya_counties_2019_census.py), transcribed directly from KNBS
    Volume I (fetched via an S3-hosted mirror, since knbs.or.ke itself
    blocks automated fetching -- confirmed directly). Households and
    land area are real KNBS-reported figures, not derived from
    third-party shapefile geometry. Validated: summed population,
    households, and land area across all 47 counties match KNBS's
    published national totals exactly (population, households) or to
    within 0.2 km2 (land area, rounding noise).
  - Real county boundary polygons (business_intel/data/county_boundaries/),
    OSM-derived, from github.com/Mondieki/kenya-counties-subcounties.

This is a genuine upgrade from SyntheticAdminHierarchyProvider: real
numbers (population, households, land area), real geometry, real
historical vintages for growth-rate calculations -- at COUNTY
resolution. It does NOT have sub-county, ward, location, or
sub-location level data; resolve() and children() both operate at the
county level only, so Pycnophylactic interpolation run through this
provider will produce a uniform density surface within whichever
single county contains the query point (correct, honest behavior when
only one zone's total is known -- there is nothing finer to smooth
against yet). Sub-county-level real data (KNBS Volume I Tables 2.5-2.7,
~300 units) has been located and fetched but not yet transcribed into
this codebase, pending a validated parser rather than manual entry.
Age/sex-by-age-group data (KNBS Volume III) has not yet been sourced.
"""

from __future__ import annotations

import json
import os
from typing import Sequence

from .population import AdminUnit, AdministrativeHierarchyProvider

_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
_BOUNDARIES_DIR = os.path.join(_DATA_DIR, "county_boundaries")


def _load_census_table():
    import importlib.util
    path = os.path.join(_DATA_DIR, "kenya_counties_2019_census.py")
    spec = importlib.util.spec_from_file_location("kenya_counties_2019_census", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.KENYA_COUNTIES_2019


class RealKenyaCountyAdminHierarchyProvider(AdministrativeHierarchyProvider):
    """Real KNBS county-level population + real OSM-derived county
    boundaries. See module docstring for exactly what is and isn't
    real here."""

    def __init__(self):
        self._units: dict = {}
        self._historical: dict = {}
        self._load()

    def _load(self):
        import shapely.geometry as geom

        for name, slug, pop_2019, pop_2009, households_2019, land_area_sqkm in _load_census_table():
            boundary_path = os.path.join(_BOUNDARIES_DIR, f"{slug}.json")
            polygon_geojson = None
            if os.path.exists(boundary_path):
                with open(boundary_path) as f:
                    raw = json.load(f)
                # Source files are GeometryCollection wrapping one
                # MultiPolygon; unwrap to a bare MultiPolygon geometry,
                # which shapely.geometry.shape() and this engine's
                # polygon_geojson consumers both expect directly.
                if raw.get("type") == "GeometryCollection":
                    polygon_geojson = raw["geometries"][0]
                else:
                    polygon_geojson = raw

            unit = AdminUnit(
                level="county", code=slug, name=name, parent_code=None, year=2019,
                population=float(pop_2019),
                households=float(households_2019),
                area_km2=float(land_area_sqkm),  # real KNBS-reported land area, not shapefile-derived
                polygon_geojson=polygon_geojson,
            )
            self._units[slug] = unit
            self._historical[slug] = [(2009, float(pop_2009))]

    def resolve(self, latitude: float, longitude: float) -> Sequence[AdminUnit]:
        import shapely.geometry as geom
        pt = geom.Point(longitude, latitude)
        for unit in self._units.values():
            if unit.polygon_geojson is None:
                continue
            try:
                if geom.shape(unit.polygon_geojson).contains(pt):
                    return [unit]
            except Exception:
                continue
        return []

    def children(self, level: str, code: str) -> Sequence[AdminUnit]:
        # County-level provider: a county has no modeled children here.
        # compute_catchment_population's fallback (siblings = [source_unit]
        # when parent_code is None) means Pycnophylactic interpolation
        # runs over just this one county polygon -- correct given we
        # only have one zone's real total at this resolution.
        return []

    def historical_population(self, level: str, code: str) -> Sequence[tuple]:
        return self._historical.get(code, [])
