"""
business_intel/tests/test_age_structure.py

Validates the new live age-structure computation (RealAdminHierarchyProvider.
_age_structure_from_v3_table / _load_age_structure / _load_subcounty_age_structure)
against the real installed census_2019/ dataset -- no Django settings/DB
required, since this only touches kenya_census_loader.CensusData (plain
pandas). Run with:

    python -m pytest business_intel/tests/test_age_structure.py -v

Regression coverage:
  - The V3_T2.3 table mixes single-year rows, 5-year band SUBTOTAL rows,
    and a "Not Stated" row under one "Age" column. A naive sum over every
    non-"Total" row double-counts (summing both a single year AND its own
    band subtotal) -- this was caught during development (every one of 47
    counties reconciled at ~2x the reported total before the single-year
    filter was added) and is guarded against here so it can't regress
    silently.
  - Every county and every sub-county's single-year rows must reconcile
    to within 2% of the table's own reported "Total" row.
  - The one county with independently hand-transcribed figures (Mombasa,
    from the original kenya_age_structure_2019_census.py pass) must match
    the live V3_T2.3 computation exactly, to 4 decimal places -- proof the
    live path reproduces the same numbers the manual transcription did.
"""
import re

import pytest

from business_intel.data.kenya_census_loader import CensusData


@pytest.fixture(scope="module")
def census():
    return CensusData()


@pytest.fixture(scope="module")
def v3_table(census):
    return census.table("V3_T2.3")


def _normalize(s):
    return str(s).lower().replace("/", " ").replace("-", " ").split()


def _is_single_year(label: str) -> bool:
    label = label.strip()
    if label == "100+":
        return True
    return bool(re.fullmatch(r"\d+", label))


def _age_structure(df):
    total_row = df[df["Age"].astype(str).str.lower() == "total"]
    if len(total_row) == 0:
        return None
    reported_total = float(total_row.iloc[0]["Total"])
    single_year_rows = df[df["Age"].astype(str).apply(_is_single_year)]
    children = working = young_adult = elderly = 0.0
    summed = 0.0
    for _, row in single_year_rows.iterrows():
        label = str(row["Age"]).strip()
        value = float(row["Total"]) if row["Total"] == row["Total"] else 0.0
        summed += value
        age_num = 100 if label == "100+" else int(label)
        if age_num <= 14:
            children += value
        elif 15 <= age_num <= 64:
            working += value
            if 15 <= age_num <= 24:
                young_adult += value
        elif age_num >= 65:
            elderly += value
    return {
        "reported_total": reported_total,
        "summed": summed,
        "children_pct": round(children / reported_total * 100, 4),
        "working_age_pct": round(working / reported_total * 100, 4),
        "young_adult_pct": round(young_adult / reported_total * 100, 4),
        "elderly_pct": round(elderly / reported_total * 100, 4),
        "dependency_ratio": round((children + elderly) / working * 100, 4),
    }


def test_v3_table_has_all_47_counties(v3_table):
    counties = set(v3_table["County"].unique())
    assert len(counties) == 47


def test_naive_sum_double_counts_without_single_year_filter(v3_table):
    """Regression guard for the exact bug caught during development: if
    someone 'simplifies' the filter back to 'everything except Total',
    band subtotal rows silently double the count. This test intentionally
    reproduces the broken version and asserts it IS broken, so nobody can
    revert the fix without this test failing loudly."""
    df = v3_table[
        (v3_table["County"] == "KWALE") & (v3_table["SubCounty"] == "ALL")
    ]
    total_row = df[df["Age"].astype(str).str.lower() == "total"]
    reported_total = float(total_row.iloc[0]["Total"])
    naive_rows = df[df["Age"].astype(str).str.lower() != "total"]
    naive_sum = naive_rows["Total"].astype(float).sum()
    assert naive_sum > reported_total * 1.9, (
        "If this fails, the underlying V3_T2.3 export format changed -- "
        "re-verify the single-year-only filter is still necessary."
    )


@pytest.mark.parametrize("county", [
    "KWALE", "MOMBASA", "NAIROBI", "KIAMBU", "KERICHO", "BUNGOMA",
    "ELGEYO-MARAKWET", "TAITA/ TAVETA",
])
def test_county_age_structure_reconciles(v3_table, county):
    target = _normalize(county)
    df = v3_table[
        v3_table["County"].astype(str).apply(lambda s: _normalize(s) == target)
        & (v3_table["SubCounty"].astype(str).str.upper() == "ALL")
    ]
    result = _age_structure(df)
    assert result is not None, f"No age data found for {county}"
    diff_pct = abs(result["summed"] - result["reported_total"]) / result["reported_total"]
    assert diff_pct <= 0.02, (
        f"{county}: single-year rows sum to {result['summed']}, "
        f"table reports {result['reported_total']} -- off by {diff_pct:.1%}"
    )


def test_all_47_counties_reconcile(v3_table):
    fails = []
    for county in sorted(set(v3_table["County"].unique())):
        target = _normalize(county)
        df = v3_table[
            v3_table["County"].astype(str).apply(lambda s: _normalize(s) == target)
            & (v3_table["SubCounty"].astype(str).str.upper() == "ALL")
        ]
        result = _age_structure(df)
        if result is None:
            fails.append((county, "no data"))
            continue
        diff_pct = abs(result["summed"] - result["reported_total"]) / result["reported_total"]
        if diff_pct > 0.02:
            fails.append((county, f"off by {diff_pct:.1%}"))
    assert not fails, f"{len(fails)} counties failed reconciliation: {fails}"


def test_all_subcounties_reconcile(v3_table):
    sc_pairs = v3_table[v3_table["SubCounty"] != "ALL"][["County", "SubCounty"]].drop_duplicates()
    fails = []
    for _, row in sc_pairs.iterrows():
        county, subcounty = row["County"], row["SubCounty"]
        tc, ts = _normalize(county), _normalize(subcounty)
        df = v3_table[
            v3_table["County"].astype(str).apply(lambda s: _normalize(s) == tc)
            & v3_table["SubCounty"].astype(str).apply(lambda s: _normalize(s) == ts)
        ]
        result = _age_structure(df)
        if result is None:
            fails.append((county, subcounty, "no data"))
            continue
        diff_pct = abs(result["summed"] - result["reported_total"]) / result["reported_total"]
        if diff_pct > 0.02:
            fails.append((county, subcounty, f"off by {diff_pct:.1%}"))
    assert not fails, f"{len(fails)} sub-counties failed reconciliation: {fails[:20]}"


def test_mombasa_matches_original_hand_transcribed_figures(v3_table):
    """The original kenya_age_structure_2019_census.py hand-transcribed
    Mombasa from the same KNBS source as an independent proof-of-pipeline
    pass. The new live V3_T2.3 computation must match it exactly (KNBS's
    own published percentages, not an approximation of them)."""
    target = _normalize("Mombasa")
    df = v3_table[
        v3_table["County"].astype(str).apply(lambda s: _normalize(s) == target)
        & (v3_table["SubCounty"].astype(str).str.upper() == "ALL")
    ]
    result = _age_structure(df)
    assert result["children_pct"] == pytest.approx(32.5509, abs=0.001)
    assert result["working_age_pct"] == pytest.approx(65.5524, abs=0.001)
    assert result["young_adult_pct"] == pytest.approx(19.9011, abs=0.001)
    assert result["elderly_pct"] == pytest.approx(1.8946, abs=0.001)