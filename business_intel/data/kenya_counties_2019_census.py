"""
business_intel/data/kenya_counties_2019_census.py

Real Kenya county population, sex, household, land-area, and density figures
from the 2019 Kenya Population and Housing Census (KPHC).

Two source tables are combined here:
  - Volume I: "Population by County and Sub-County" (November 2019) --
    population_2019, households_2019, land_area_sqkm_2019.
  - Volume II: "Distribution of Population by Administrative Units"
    (Table 2.2, page 11) -- male_2019, female_2019, intersex_2019,
    households_conventional_2019, households_group_quarters_2019.

Both were fetched from S3-hosted mirrors (knbs.or.ke blocks automated
fetching per its own robots.txt, confirmed directly):
  Vol I:  https://s3-eu-west-1.amazonaws.com/s3.sourceafrica.net/documents/119522/Kenya-Population-and-Housing-Census-2019-Vol-1.pdf
  Vol II: https://s3-eu-west-1.amazonaws.com/s3.sourceafrica.net/documents/119746/2019-Kenya-Population-and-Housing-Census-Volume.pdf

Cross-checks performed:
  - Nairobi's 1,506,888 households / 4,397,073 population = 2.918 average
    household size, matching Wikipedia's independently KNBS-Volume-II-cited
    figure of 2.9.
  - male_2019 + female_2019 + intersex_2019 == population_2019 for all 47
    counties (checked at module import time below).
  - households_conventional_2019 + households_group_quarters_2019 ==
    households_2019 for all 47 counties (checked at module import time).
  - National totals (sum over all 47 counties) match KNBS's own published
    national aggregate in NATIONAL_TOTALS_2019 below, including the
    male/female/intersex split (23,548,056 / 24,014,716 / 1,524).

population_2009 is from the same Wikipedia "List of counties of Kenya by
population" table used previously (itself citing KNBS Volume I for the 2009
round), used for real inter-censal growth-rate calculation.

Sub-county-level detail (sex, households, land area, density for all 333
sub-counties) lives in business_intel.data.kenya_subcounties_2019_census.
Sub-location-level detail (partial coverage) lives in
business_intel.data.kenya_sublocations_2019_census. Age-structure detail
(national + selected county age bands) lives in
business_intel.data.kenya_age_structure_2019_census.

geojson_slug matches the filename (minus .json) in the county boundary
polygons cloned from github.com/Mondieki/kenya-counties-subcounties
(OSM-derived county boundaries), used by the real point-in-polygon
resolution in business_intel.engines.population.RealAdminHierarchyProvider.
"""

# name, geojson_slug, population_2019, population_2009, households_2019,
# land_area_sqkm_2019, male_2019, female_2019, intersex_2019,
# households_conventional_2019, households_group_quarters_2019
KENYA_COUNTIES_2019 = [
    ("Nairobi", "nairobi", 4_397_073, 3_138_369, 1_506_888, 703.9, 2_192_452, 2_204_376, 245, 1_494_676, 12_212),
    ("Kiambu", "kiambu", 2_417_735, 1_623_282, 795_241, 2_538.6, 1_187_146, 1_230_454, 135, 792_333, 2_908),
    ("Nakuru", "nakuru", 2_162_202, 1_603_325, 616_046, 7_462.4, 1_077_272, 1_084_835, 95, 598_237, 17_809),
    ("Kakamega", "kakamega", 1_867_579, 1_660_651, 433_207, 3_020.0, 897_133, 970_406, 40, 432_284, 923),
    ("Bungoma", "bungoma", 1_670_570, 1_375_063, 358_796, 3_023.9, 812_146, 858_389, 35, 357_714, 1_082),
    ("Meru", "meru", 1_545_714, 1_356_301, 426_360, 7_006.3, 767_698, 777_975, 41, 423_931, 2_429),
    ("Kilifi", "kilifi", 1_453_787, 1_109_735, 298_472, 12_539.7, 704_089, 749_673, 25, 297_990, 482),
    ("Machakos", "machakos", 1_421_932, 1_098_584, 402_466, 6_042.7, 710_707, 711_191, 34, 399_523, 2_943),
    ("Kisii", "kisii", 1_266_860, 1_152_282, 308_054, 1_323.0, 605_784, 661_038, 38, 307_254, 800),
    ("Mombasa", "mombasa", 1_208_333, 939_370, 378_422, 219.9, 610_257, 598_046, 30, 376_295, 2_127),
    ("Narok", "narok", 1_157_873, 850_920, 241_125, 17_950.3, 579_042, 578_805, 26, 238_115, 3_010),
    ("Kajiado", "kajiado", 1_117_840, 687_312, 316_179, 21_871.1, 557_098, 560_704, 38, 313_218, 2_961),
    ("Uasin Gishu", "uasin-gishu", 1_163_186, 894_179, 304_943, 3_392.2, 580_269, 582_889, 28, 301_110, 3_833),
    ("Kisumu", "kisumu", 1_155_574, 968_909, 300_745, 2_085.4, 560_942, 594_609, 23, 296_846, 3_899),
    ("Migori", "migori", 1_116_436, 917_170, 240_168, 2_613.5, 536_187, 580_214, 35, 238_133, 2_035),
    ("Homa Bay", "homabay", 1_131_950, 963_794, 262_036, 3_152.5, 539_560, 592_367, 23, 260_290, 1_746),
    ("Kitui", "kitui", 1_136_187, 1_012_709, 262_942, 30_429.5, 549_003, 587_151, 33, 261_814, 1_128),
    ("Murang'a", "muranga", 1_056_640, 942_581, 318_105, 2_524.2, 523_940, 532_669, 31, 317_496, 609),
    ("Trans Nzoia", "trans-nzoia", 990_341, 818_757, 223_808, 2_495.2, 489_107, 501_206, 28, 222_989, 819),
    ("Siaya", "siaya", 993_183, 842_304, 250_698, 2_529.8, 471_669, 521_496, 18, 249_341, 1_357),
    ("Makueni", "makueni", 987_653, 884_527, 244_669, 8_169.8, 489_691, 497_942, 20, 243_979, 690),
    ("Turkana", "turkana", 926_976, 855_399, 164_519, 68_232.9, 478_087, 448_868, 21, 162_627, 1_892),
    ("Busia", "busia", 893_681, 743_946, 198_152, 1_696.3, 426_252, 467_401, 28, 197_944, 208),
    ("Mandera", "mandera", 867_457, 1_025_756, 125_763, 25_939.8, 434_976, 432_444, 37, 123_954, 1_809),
    ("Kericho", "kericho", 901_777, 752_396, 206_036, 2_436.1, 450_741, 451_008, 28, 205_932, 104),
    ("Nandi", "nandi", 885_711, 752_965, 199_426, 2_855.8, 441_259, 444_430, 22, 199_040, 386),
    ("Kwale", "kwale", 866_820, 649_931, 173_176, 8_267.1, 425_121, 441_681, 18, 172_802, 374),
    ("Bomet", "bomet", 875_689, 730_129, 187_641, 2_530.9, 434_287, 441_379, 23, 187_230, 411),
    ("Garissa", "garissa", 841_353, 623_060, 141_394, 44_736.0, 458_975, 382_344, 34, 138_940, 2_454),
    ("Wajir", "wajir", 781_263, 661_941, 127_932, 56_773.1, 415_374, 365_840, 49, 126_878, 1_054),
    ("Nyeri", "nyeri", 759_164, 693_558, 248_050, 3_325.0, 374_288, 384_845, 31, 244_564, 3_486),
    ("Baringo", "baringo", 666_763, 555_561, 142_518, 10_976.4, 336_322, 330_428, 13, 141_877, 641),
    ("Nyandarua", "nyandarua", 638_289, 596_268, 179_686, 3_285.7, 315_022, 323_247, 20, 178_224, 1_462),
    ("West Pokot", "west-pokot", 621_241, 512_690, 116_182, 9_123.2, 307_013, 314_213, 15, 115_761, 421),
    ("Nyamira", "nyamira", 605_576, 598_252, 150_669, 897.3, 290_907, 314_656, 13, 150_499, 170),
    ("Kirinyaga", "kirinyaga", 610_411, 528_054, 204_188, 1_478.3, 302_011, 308_369, 31, 203_576, 612),
    ("Embu", "embu", 608_599, 516_212, 182_743, 2_820.7, 304_208, 304_367, 24, 182_427, 316),
    ("Vihiga", "vihiga", 590_013, 554_622, 143_365, 563.8, 283_678, 306_323, 12, 143_288, 77),
    ("Laikipia", "laikipia", 518_560, 399_227, 149_271, 9_532.2, 259_440, 259_102, 18, 145_776, 3_495),
    ("Marsabit", "marsabit", 459_785, 291_166, 77_495, 70_944.1, 243_548, 216_219, 18, 76_689, 806),
    ("Elgeyo-Marakwet", "elgeyo-marakwet", 454_480, 369_998, 99_861, 3_032.0, 227_317, 227_151, 12, 99_119, 742),
    ("Tharaka-Nithi", "tharaka-nithi", 393_177, 365_330, 109_860, 2_564.4, 193_764, 199_406, 7, 109_450, 410),
    ("Taita Taveta", "taita-taveta", 340_671, 284_657, 96_429, 17_152.0, 173_337, 167_327, 7, 94_468, 1_961),
    ("Tana River", "tana-river", 315_943, 240_075, 68_242, 37_950.5, 158_550, 157_391, 2, 66_984, 1_258),
    ("Samburu", "samburu", 310_327, 223_947, 65_910, 21_065.1, 156_774, 153_546, 7, 63_951, 1_959),
    ("Isiolo", "isiolo", 268_002, 143_294, 58_072, 25_350.6, 139_510, 128_483, 9, 53_217, 4_855),
    ("Lamu", "lamu", 143_920, 101_539, 37_963, 6_253.3, 76_103, 67_813, 4, 34_231, 3_732),
]

assert len(KENYA_COUNTIES_2019) == 47, f"Expected 47 counties, got {len(KENYA_COUNTIES_2019)}"

for _row in KENYA_COUNTIES_2019:
    _name, _slug, _pop, _pop09, _hh, _area, _male, _female, _intersex, _hh_conv, _hh_group = _row
    assert _male + _female + _intersex == _pop, f"{_name}: sex split does not sum to population"
    assert _hh_conv + _hh_group == _hh, f"{_name}: household split does not sum to household total"

# National totals from KNBS Volume I/II, for validating the transcription above.
NATIONAL_TOTALS_2019 = {
    "population": 47_564_296,
    "male": 23_548_056,
    "female": 24_014_716,
    "intersex": 1_524,
    "households": 12_143_913,   # excludes special population, per KNBS footnote
    "households_conventional": 12_043_016,
    "households_group_quarters": 100_897,
    "land_area_sqkm": 580_895.4,
}

assert sum(r[2] for r in KENYA_COUNTIES_2019) == NATIONAL_TOTALS_2019["population"]
assert sum(r[6] for r in KENYA_COUNTIES_2019) == NATIONAL_TOTALS_2019["male"]
assert sum(r[7] for r in KENYA_COUNTIES_2019) == NATIONAL_TOTALS_2019["female"]
assert sum(r[8] for r in KENYA_COUNTIES_2019) == NATIONAL_TOTALS_2019["intersex"]
assert sum(r[4] for r in KENYA_COUNTIES_2019) == NATIONAL_TOTALS_2019["households"]
