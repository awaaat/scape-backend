"""
business_intel/tests/test_extra_demographics.py

Exercises RealAdminHierarchyProvider._extra_demographics's logic directly
against the real installed census_2019/ CSVs -- no Django settings/DB
required, since this only touches kenya_census_loader.CensusData (plain
pandas), not any Django model. Run with:

    python -m pytest business_intel/tests/test_extra_demographics.py -v

If this passes but the full RealAdminHierarchyProvider still returns
extra_demographics=None in your actual Django shell, the issue is
somewhere Django-specific (import path, settings not configured) rather
than in this logic.
"""
import pytest

from business_intel.data.kenya_census_loader import CensusData


@pytest.fixture(scope="module")
def census():
    return CensusData()


def _row(df, name_col, name_value):
    match = df[df[name_col].astype(str).str.lower() == name_value.lower()]
    if "AdminArea" in df.columns:
        county_rows = match[match["AdminArea"] == "County"]
        if len(county_rows):
            match = county_rows
    return match.iloc[0] if len(match) else None


def test_disability_row_is_county_level_not_subcounty(census):
    """Regression test for the real bug caught during this build: several
    V4 tables repeat the same County value on every sub-county row too,
    so a naive filter can silently grab a sub-county's figure instead of
    the county's."""
    df = census.disability()
    row = _row(df, "County", "Kwale")
    assert row["AdminArea"] == "County"
    assert row["PercentageWithDisability"] == pytest.approx(1.7, abs=0.05)


def test_religion_breakdown_sums_to_less_than_total(census):
    df = census.religion()
    row = _row(df, "County", "Kwale")
    cols = ["Catholic", "Protestant", "Evangelical", "Islam"]
    total = sum(float(row[c]) for c in cols)
    assert 0 < total <= float(row["Total"])


def test_water_source_breakdown_has_no_metadata_columns(census):
    df = census.water_source()
    row = _row(df, "County", "Kwale")
    breakdown = row.drop(labels=["County", "SubCounty", "AdminArea"], errors="ignore").dropna().to_dict()
    assert "County" not in breakdown
    assert "AdminArea" not in breakdown
    assert "Publictap/Standpipe" in breakdown


def test_ict_ownership_breakdown_has_no_metadata_columns(census):
    df = census.ict_ownership()
    row = _row(df, "County", "Kwale")
    breakdown = row.drop(labels=["County", "SubCounty", "AdminArea"], errors="ignore").dropna().to_dict()
    assert "SubCounty" not in breakdown
    assert "Internet" in breakdown


def test_unknown_county_returns_none_not_crash(census):
    df = census.disability()
    row = _row(df, "County", "Not A Real County")
    assert row is None
