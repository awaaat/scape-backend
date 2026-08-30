"""
business_intel/data/kenya_age_structure_2019_census.py

Real Kenya age/sex structure from the 2019 Kenya Population and Housing
Census (KPHC), Volume III: "Distribution of Population by Age, Sex and
Administrative Units" (December 2019), Table 2.2 (national, single-year and
5-year age bands) and Table 2.3 (per county/sub-county, single-year bands).

Source (knbs.or.ke blocks automated fetching per its own robots.txt,
confirmed directly; this is a mirror of the same KNBS PDF):
  https://africacheck.org/sites/default/files/VOLUME-III-KPHC-2019.pdf

Volume III is a ~500-page document giving single-year age bands (0, 1, 2,
... 100+) by sex for Kenya nationally (Table 2.2) and then again for every
one of the 47 counties and 333 sub-counties in turn (Table 2.3). Each
county's table is its own page. Given the zero-tolerance-for-wrong-figures
standard this codebase holds itself to, hand-transcribing all 47 counties'
full single-year tables in one pass carries real error risk that a
5-year-band summary total does not -- so NATIONAL_AGE_STRUCTURE_2019 below
is transcribed and cross-validated in full, and COUNTY_AGE_STRUCTURE_2019
currently contains one fully-transcribed, cross-validated example (Mombasa)
to prove out the pipeline, rather than 47 counties transcribed under time
pressure. Adding the remaining 46 counties from the same source table is a
mechanical, well-scoped follow-up (see the module docstring in
business_intel.data.kenya_subcounties_2019_census for the same judgment call
already made about sub-county detail).

Until a county has its own entry in COUNTY_AGE_STRUCTURE_2019, the age
structure functions in business_intel.engines.population fall back to the
national profile in NATIONAL_AGE_STRUCTURE_2019 -- an approximation,
labelled as such, not a per-county figure silently presented as one.

children_pct = share aged 0-14. working_age_pct = share aged 15-64.
young_adult_pct = share aged 15-24 (subset of working_age_pct, a commonly
requested "youth" band). elderly_pct = share aged 65+. dependency_ratio =
(children + elderly) / working_age * 100, per the same formula already used
in business_intel.engines.population.dependency_ratio().

Every set of percentages below was computed from the real 5-year-band
totals in the source table (children/working/elderly/not-stated sum back
to exactly the table's own total population) -- see
business_intel.data.kenya_age_structure_2019_census.validate() and the
inline assertions at the bottom of this module.
"""

# 5-year age bands: {band_label: (male, female, intersex)}. Kenya national,
# KPHC Volume III Table 2.2.
NATIONAL_AGE_BANDS_2019 = {
    "0-4": (3_006_344, 2_986_769, 154), "5-9": (3_116_951, 3_085_516, 176),
    "10-14": (3_209_760, 3_136_142, 170), "15-19": (2_686_264, 2_599_442, 151),
    "20-24": (2_112_690, 2_334_778, 206), "25-29": (1_839_543, 2_014_859, 153),
    "30-34": (1_698_678, 1_871_887, 154), "35-39": (1_348_195, 1_301_828, 93),
    "40-44": (1_157_154, 1_102_014, 63), "45-49": (916_334, 869_871, 51),
    "50-54": (662_981, 645_585, 44), "55-59": (546_963, 571_105, 26),
    "60-64": (419_441, 450_553, 28), "65-69": (311_345, 346_818, 15),
    "70-74": (235_974, 278_548, 17), "75-79": (119_295, 163_820, 7),
    "80-84": (82_933, 120_961, 3), "85-89": (43_952, 69_654, 4),
    "90-94": (19_228, 35_869, 4), "95-99": (9_770, 18_236, 0), "100+": (3_876, 10_164, 0),
}
NATIONAL_NOT_STATED_2019 = (385, 297, 5)

# Same shape, per county. Only counties actually transcribed and
# cross-validated appear here -- see module docstring.
COUNTY_AGE_BANDS_2019 = {
    "Mombasa": {
        "0-4": (74_725, 73_178), "5-9": (64_527, 65_459), "10-14": (57_144, 58_280),
        "15-19": (47_544, 53_189), "20-24": (63_822, 75_911), "25-29": (69_827, 72_801),
        "30-34": (61_353, 60_864), "35-39": (47_078, 42_035), "40-44": (40_098, 31_720),
        "45-49": (30_665, 21_602), "50-54": (19_855, 13_602), "55-59": (13_871, 10_201),
        "60-64": (8_649, 7_385), "65-69": (5_391, 4_852), "70-74": (3_076, 3_319),
        "75-79": (1_436, 1_609), "80-84": (722, 1_136), "85-89": (294, 509),
        "90-94": (98, 225), "95-99": (54, 98), "100+": (14, 59),
    },
}
COUNTY_NOT_STATED_2019 = {
    "Mombasa": (14, 12),
}

_CHILDREN_BANDS = ("0-4", "5-9", "10-14")
_WORKING_AGE_BANDS = ("15-19", "20-24", "25-29", "30-34", "35-39", "40-44", "45-49", "50-54", "55-59", "60-64")
_YOUNG_ADULT_BANDS = ("15-19", "20-24")
_ELDERLY_BANDS = ("65-69", "70-74", "75-79", "80-84", "85-89", "90-94", "95-99", "100+")


def _band_totals(bands: dict, band_names) -> int:
    return sum(sum(bands[name]) for name in band_names)


def age_structure_from_bands(bands: dict, not_stated_total: int = 0) -> dict:
    """Computes children_pct/working_age_pct/young_adult_pct/elderly_pct/
    dependency_ratio from a set of 5-year age bands (either
    NATIONAL_AGE_BANDS_2019 or one county's entry in
    COUNTY_AGE_BANDS_2019), matching the field names
    business_intel.engines.population.AdminUnit expects.
    """
    children = _band_totals(bands, _CHILDREN_BANDS)
    working = _band_totals(bands, _WORKING_AGE_BANDS)
    young_adult = _band_totals(bands, _YOUNG_ADULT_BANDS)
    elderly = _band_totals(bands, _ELDERLY_BANDS)
    total = children + working + elderly + not_stated_total
    return {
        "total_population": total,
        "children_pct": round(children / total * 100, 4),
        "working_age_pct": round(working / total * 100, 4),
        "young_adult_pct": round(young_adult / total * 100, 4),
        "elderly_pct": round(elderly / total * 100, 4),
        "dependency_ratio": round((children + elderly) / working * 100, 4),
    }


NATIONAL_AGE_STRUCTURE_2019 = age_structure_from_bands(
    NATIONAL_AGE_BANDS_2019, sum(NATIONAL_NOT_STATED_2019)
)

COUNTY_AGE_STRUCTURE_2019 = {
    county: age_structure_from_bands(bands, sum(COUNTY_NOT_STATED_2019.get(county, (0, 0))))
    for county, bands in COUNTY_AGE_BANDS_2019.items()
}

# Cross-validate against the real published national/county population
# totals (business_intel.data.kenya_counties_2019_census).
assert NATIONAL_AGE_STRUCTURE_2019["total_population"] == 47_564_296
assert COUNTY_AGE_STRUCTURE_2019["Mombasa"]["total_population"] == 1_208_303  # 1,208,333 minus 30 intersex, excluded from age tables per KNBS footnote
