"""
business_intel/tests/test_kenya_census_loader.py

Validates that the installed census_2019 dataset loads correctly and that
its headline figures match KNBS's own published totals. Run with:

    python -m pytest business_intel/tests/test_kenya_census_loader.py -v
"""
import pytest

from business_intel.data.kenya_census_loader import CensusData


@pytest.fixture(scope="module")
def census():
    return CensusData()


def test_catalogue_has_73_tables(census):
    cat = census.catalogue()
    assert len(cat) >= 70, f"Expected ~73 tables, found {len(cat)}"


def test_national_population_matches_knbs(census):
    df = census.population_by_county()
    total_row = df[df["County"].str.lower() == "total"]
    assert int(total_row["Total"].iloc[0]) == 47_564_296


def test_kwale_sex_split_matches_knbs(census):
    row = census.population_by_county("Kwale").iloc[0]
    assert int(row["Male"]) == 425_121
    assert int(row["Female"]) == 441_681
    assert int(row["Intersex"]) == 18
    assert int(row["Total"]) == 866_820


def test_kinango_age_structure_matches_hand_validated_figures(census):
    df = census.age_structure("Kwale", subcounty="Kinango")
    total_row = df[df["Age"].str.lower() == "total"]
    assert int(total_row["Male"].iloc[0]) == 45_413
    assert int(total_row["Female"].iloc[0]) == 48_806
    assert int(total_row["Total"].iloc[0]) == 94_219


def test_national_disability_rate_matches_knbs_headline(census):
    df = census.disability()
    kenya_row = df[df["SubCounty"].str.lower() == "kenya"].iloc[0]
    assert kenya_row["PercentageWithDisability"] == pytest.approx(2.2, abs=0.05)


def test_urban_centres_count(census):
    df = census.urban_centres()
    # first row is the KENYA total, so subtract it
    assert len(df) - 1 >= 300, "Expected 300+ individual towns/urban centres"


def test_search_tables_finds_water(census):
    results = census.search_tables("water")
    assert len(results) >= 1


def test_religion_table_sums_reasonably(census):
    df = census.religion()
    kenya_row = df[df["County"].str.lower() == "kenya"].iloc[0]
    # Catholic + Protestant + Evangelical + Islam should be the bulk of the
    # population -- a loose sanity check, not a strict reconciliation,
    # since KNBS's own category boundaries aren't perfectly additive.
    major = kenya_row[["Catholic", "Protestant", "Evangelical", "Islam"]].sum()
    assert major > 30_000_000
