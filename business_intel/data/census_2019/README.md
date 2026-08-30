# Kenya 2019 Census — Full Demographic Dataset (all KNBS volumes)

## What this is

Every table KNBS published from the 2019 Kenya Population and Housing
Census (KPHC), covering **Volumes I–IV**, converted to clean CSV. This is
not a hand-transcribed subset — it's the full source-of-record.

**Source**: `github.com/Shelmith-Kariuki/rKenyaCensus`, an open-source R
package built by directly scraping every table in KNBS's own published
PDFs. It's the dataset behind the well-known [TidyTuesday Kenya Census
episode](https://github.com/rfordatascience/tidytuesday/blob/main/data/2021/2021-01-19/readme.md)
and is widely cited/used in Kenyan open-data projects.

**Why trust it**: I didn't just take this on faith — I cross-checked it
against the KNBS figures I'd already hand-transcribed and validated
myself in the previous pass (Mombasa/Kwale/Kilifi/Tana River population,
sex split, and single-year age bands). Every number matched exactly,
including figures down to the individual person (e.g. Kwale: 425,121 male
/ 441,681 female / 18 intersex — identical to KNBS Volume I/II). National
population sums to exactly 47,564,296, matching KNBS's own published
total. Disability prevalence checks out at 2.2% nationally, matching
KNBS's own headline figure from Volume IV.

## Coverage — what's actually in here

| Volume | Topic | Granularity |
|---|---|---|
| I | Population, households, land area, density | County, sub-county |
| II | Sex, households, land area, density | County, sub-county, **urban centres/towns (307 of them)** |
| III | Single-year + 5-year age bands, by sex | County, sub-county |
| IV | Education, employment/labour force, housing (roofing/wall/floor material, water source, sanitation, cooking/lighting fuel), agriculture (crops, livestock, fish farming, land area), disability (status + type), albinism, street persons, religion, ethnicity/nationality, ICT & mobile/internet use, household asset ownership, births | County, sub-county (most tables), national (a few) |

This is genuinely everything KNBS published as structured tabular
output for the 2019 census — 73 tables total. `00_TABLE_CATALOGUE.csv`
lists every one with its KNBS table number and description, and the
`by_volume/` folder groups the raw CSVs accordingly.

## What's NOT in here

- **Sub-location** ("village") level data — KNBS's own Volume II has a
  sub-location table, but formatting inconsistencies in it are what
  caused a previous transcription attempt to fail reconciliation. It's
  not in this package; flag if you want it and I'll take another run at
  parsing it specifically, cross-checked the same way as everything above.
- **Constituency/ward level** — KNBS does not publish population by
  constituency/ward in these four volumes; that would need a different
  source (e.g. IEBC).

## Filtering from national → county → sub-county → town

Every table with a `County` and/or `SubCounty` column supports exactly
the drill-down you asked for. `V2_T2.5.csv` additionally gives you the
307 individual urban centres/towns with population and sex split, for
"which specific town" questions that fall below sub-county level.

## Next step

This is the raw material — the actual data engine (`business_intel`)
still needs a loader that reads these CSVs and answers questions like
"how many women aged 20–35 in Ruiru sub-county" the way the existing
`kenya_counties_2019_census.py`-style modules do. That's a real, separate
build step on top of this — say the word and I'll wire it up next,
same validate-before-shipping standard as this pass.
