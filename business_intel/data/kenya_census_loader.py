"""
business_intel/data/kenya_census_loader.py

Loads the full KNBS 2019 census dataset (business_intel/data/census_2019/,
installed by install_census_dataset.sh) and exposes simple query functions
over it: population/sex, age structure, education, disability, religion,
ethnicity, housing, water, ICT, agriculture -- all filterable by county,
sub-county, or urban centre/town.

This is a thin, lazy-loading wrapper over the CSVs in
census_2019/by_volume/. It does not duplicate the data as Python literals
(the previous approach, kenya_counties_2019_census.py etc.) because the
full dataset is too large for that to be maintainable -- 73 tables, most
of them county x sub-county x (age or category). Those older per-topic
modules can stay as-is for backward compatibility; this loader is the
path to everything else.

Requires pandas (`pip install pandas`).

Usage:
    from business_intel.data.kenya_census_loader import CensusData

    census = CensusData()
    census.population_by_county("Kwale")
    census.age_structure("Kwale", subcounty="Kinango")
    census.table("V4_T2.30")   # raw table by KNBS code, see catalogue()
    census.catalogue()          # DataFrame of all 73 tables + descriptions
    census.search_tables("disability")
"""
from __future__ import annotations

import functools
import os

try:
    import pandas as pd
except ImportError as e:  # pragma: no cover
    raise ImportError(
        "kenya_census_loader requires pandas. Install with: pip install pandas"
    ) from e

_DATA_DIR = os.path.join(os.path.dirname(__file__), "census_2019")
_BY_VOLUME_DIR = os.path.join(_DATA_DIR, "by_volume")
_CATALOGUE_PATH = os.path.join(_DATA_DIR, "00_TABLE_CATALOGUE.csv")


def _find_table_path(table_code: str) -> str:
    for vol in ("V1", "V2", "V3", "V4", "other"):
        candidate = os.path.join(_BY_VOLUME_DIR, vol, f"{table_code}.csv")
        if os.path.exists(candidate):
            return candidate
    raise FileNotFoundError(
        f"No table '{table_code}' found under {_BY_VOLUME_DIR}. "
        f"Did you run install_census_dataset.sh? "
        f"See catalogue() for valid table codes."
    )


class CensusData:
    """Lazy-loading accessor over the full KNBS 2019 census CSV dataset."""

    def __init__(self, data_dir: str = _DATA_DIR):
        self.data_dir = data_dir
        if not os.path.isdir(data_dir):
            raise FileNotFoundError(
                f"Census data not found at {data_dir}. "
                f"Run install_census_dataset.sh first."
            )

    @functools.lru_cache(maxsize=None)
    def catalogue(self) -> "pd.DataFrame":
        """Every table KNBS published, with its code, volume, and description."""
        return pd.read_csv(_CATALOGUE_PATH)

    def search_tables(self, keyword: str) -> "pd.DataFrame":
        """Find table codes whose description mentions `keyword`, e.g. 'water'."""
        cat = self.catalogue()
        mask = cat["Dataset.Description"].str.contains(keyword, case=False, na=False)
        return cat[mask]

    @functools.lru_cache(maxsize=None)
    def table(self, table_code: str) -> "pd.DataFrame":
        """Raw table by KNBS code, e.g. table('V4_T2.30') for religion by county."""
        return pd.read_csv(_find_table_path(table_code))

    # ---- convenience wrappers over the tables people actually ask for ----

    def population_by_county(self, county: str | None = None) -> "pd.DataFrame":
        """Population, sex split, households, land area, density. Volume I/II (V1_T2.2)."""
        df = self.table("V1_T2.2")
        return df if county is None else df[df["County"].str.lower() == county.lower()]

    def population_by_subcounty(self, county: str | None = None) -> "pd.DataFrame":
        df = self.table("V1_T2.6")
        if county is not None:
            df = df[df["County"].str.lower() == county.lower()]
        return df

    def urban_centres(self, county: str | None = None) -> "pd.DataFrame":
        """All 307 towns/urban centres with population and sex split."""
        df = self.table("V2_T2.5")
        if county is not None:
            df = df[df["County"].str.lower() == county.lower()]
        return df

    def age_structure(self, county: str, subcounty: str | None = None) -> "pd.DataFrame":
        """Single-year age bands by sex. Volume III (V3_T2.3)."""
        df = self.table("V3_T2.3")
        df = df[df["County"].str.lower() == county.lower()]
        if subcounty is not None:
            df = df[df["SubCounty"].str.lower() == subcounty.lower()]
        return df

    def disability(self, county: str | None = None) -> "pd.DataFrame":
        df = self.table("V4_T2.26")
        return df if county is None else df[df["County"].str.lower() == county.lower()]

    def religion(self, county: str | None = None) -> "pd.DataFrame":
        df = self.table("V4_T2.30")
        return df if county is None else df[df["County"].str.lower() == county.lower()]

    def education_level(self, county: str | None = None) -> "pd.DataFrame":
        df = self.table("V4_T2.4")
        return df if county is None else df[df["SubCounty"].str.lower() == county.lower()]

    def water_source(self, county: str | None = None) -> "pd.DataFrame":
        df = self.table("V4_T2.15")
        return df if county is None else df[df["County"].str.lower() == county.lower()]

    def ict_ownership(self, county: str | None = None) -> "pd.DataFrame":
        df = self.table("V4_T2.36")
        return df if county is None else df[df["County"].str.lower() == county.lower()]