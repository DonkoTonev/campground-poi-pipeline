# Data quality report

Region `sierra`, generated 2026-11-24 from the database.

## Pipeline

| source | records | campgrounds |
|---|---|---|
| nps | 16 | 16 |
| osm | 289 | 278 |
| ridb | 89 | 85 |
| **total** | **394** | **379** |

379 campground records resolve to **300 entities** (79 duplicates merged); 60 entities are confirmed by more than one source. 15 records were excluded as non-campgrounds.

## Field coverage

Share of campground records per source, and of golden entities, with a value.

| field | nps | osm | ridb | golden |
|---|---|---|---|---|
| name | 100% (16) | 77% (213) | 100% (85) | 78% (235) |
| phone | 100% (16) | 3% (7) | 69% (59) | 22% (65) |
| website | 94% (15) | 36% (99) | 0% (0) | 35% (104) |
| reservations | 100% (16) | 26% (72) | 100% (85) | 45% (136) |
| season dates | 100% (16) | 0% (0) | 0% (0) | 5% (16) |
| number of sites | 100% (16) | 19% (54) | 0% (0) | 20% (61) |
| drinking water | 100% (16) | 8% (22) | 0% (0) | 12% (36) |
| showers | 100% (16) | 3% (8) | 0% (0) | 8% (23) |
| max RV length | 69% (11) | 0% (0) | 0% (0) | 4% (11) |
| price | 100% (16) | 0% (0) | 0% (0) | 5% (16) |
| polygon | 0% (0) | 53% (147) | 0% (0) | 48% (143) |

## Validation flags and conflicts

Every rule violation is a flag; nothing is fixed silently.

| stage | rule | severity | count |
|---|---|---|---|
| normalize | missing_name | warning | 65 |
| normalize | all_caps_recased | info | 47 |
| normalize | po_box_address | info | 21 |
| normalize | non_campground | warning | 15 |
| normalize | null_island | error | 7 |
| normalize | region_by_recarea | warning | 7 |
| normalize | outside_claimed_state | warning | 2 |
| normalize | low_precision | warning | 2 |
| normalize | outside_region | warning | 1 |
| normalize | state_name_to_code | info | 1 |
| normalize | toll_free_phone | info | 1 |
| resolve | amenity_conflict | info | 3 |
| resolve | geometry_conflict | warning | 2 |

## Freshness and quality score

Records last seen between 2026-11-24 and 2026-11-24. Possibly closed (missing from their sources for several runs): 0. Records with season dates stated for a past year: 0.

Quality score (completeness, corroboration, freshness, validity): mean 0.623, median 0.600, 10th percentile 0.490.

| sources per entity | entities | mean quality |
|---|---|---|
| 1 | 240 | 0.575 |
| 2 | 45 | 0.752 |
| 3 | 15 | 0.994 |

## Top data gaps and next actions

| gap | entities missing it | missing by segment | suggested next action |
|---|---|---|---|
| max RV length | 96% (289) | OSM only 100% of 215 · USDA Forest Service 100% of 69 · NPS 31% of 16 | website extraction |
| season dates | 95% (284) | OSM only 100% of 215 · USDA Forest Service 100% of 69 · NPS 0% of 16 | extract seasons from official websites (crawler + LLM stage) |
| price | 95% (284) | OSM only 100% of 215 · USDA Forest Service 100% of 69 · NPS 0% of 16 | website extraction; RIDB fees |
| showers | 92% (277) | OSM only 98% of 215 · USDA Forest Service 97% of 69 · NPS 0% of 16 | website extraction (amenity lists) |
| drinking water | 88% (264) | OSM only 94% of 215 · USDA Forest Service 88% of 69 · NPS 0% of 16 | website extraction (amenity lists) |
| number of sites | 80% (239) | OSM only 84% of 215 · USDA Forest Service 84% of 69 · NPS 0% of 16 | website extraction; RIDB campsite listings |
| phone | 78% (235) | OSM only 97% of 215 · USDA Forest Service 38% of 69 · NPS 0% of 16 | extract from official websites (crawler + LLM stage) |
| website | 65% (196) | OSM only 70% of 215 · USDA Forest Service 64% of 69 · NPS 6% of 16 | official sites linked from RIDB facility pages; OSM `website` tags |
| reservations | 55% (164) | OSM only 76% of 215 · USDA Forest Service 0% of 69 · NPS 0% of 16 | recreation.gov ids via OSM/website links; website extraction |
| polygon | 52% (157) | OSM only 54% of 215 · USDA Forest Service 55% of 69 · NPS 12% of 16 | map campground outlines in OSM (improves matching and geometry) |
