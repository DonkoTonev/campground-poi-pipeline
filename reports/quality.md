# Data quality report

Region `sierra`, generated 2026-11-25 from the database.

## Pipeline

| source | records | campgrounds |
|---|---|---|
| nps | 16 | 16 |
| osm | 289 | 278 |
| ridb | 89 | 85 |
| ridb_description | 85 | 85 |
| web | 19 | 19 |
| **total** | **498** | **483** |

483 campground records resolve to **304 entities** (179 duplicates merged); 97 entities are confirmed by more than one source. 15 records were excluded as non-campgrounds.

## Field coverage

Share of campground records per source, and of golden entities, with a value.

| field | nps | osm | ridb | ridb_description | web | golden |
|---|---|---|---|---|---|---|
| name | 100% (16) | 77% (213) | 100% (85) | 100% (85) | 100% (19) | 79% (239) |
| phone | 100% (16) | 3% (7) | 69% (59) | 22% (19) | 89% (17) | 27% (81) |
| website | 94% (15) | 36% (99) | 0% (0) | 6% (5) | 100% (19) | 38% (114) |
| reservations | 100% (16) | 26% (72) | 100% (85) | 28% (24) | 74% (14) | 46% (139) |
| season dates | 100% (16) | 0% (0) | 0% (0) | 7% (6) | 74% (14) | 11% (34) |
| number of sites | 100% (16) | 19% (54) | 0% (0) | 12% (10) | 37% (7) | 25% (77) |
| drinking water | 100% (16) | 8% (22) | 0% (0) | 76% (65) | 47% (9) | 28% (84) |
| showers | 100% (16) | 3% (8) | 0% (0) | 15% (13) | 84% (16) | 15% (45) |
| max RV length | 69% (11) | 0% (0) | 0% (0) | 6% (5) | 5% (1) | 6% (17) |
| price | 100% (16) | 0% (0) | 0% (0) | 5% (4) | 53% (10) | 9% (27) |
| polygon | 0% (0) | 53% (147) | 0% (0) | 0% (0) | 0% (0) | 47% (143) |

## Validation flags and conflicts

Every rule violation is a flag; nothing is fixed silently.

| stage | rule | severity | count |
|---|---|---|---|
| normalize | missing_coordinates | warning | 103 |
| normalize | missing_name | warning | 65 |
| normalize | all_caps_recased | info | 47 |
| normalize | po_box_address | info | 21 |
| normalize | non_campground | warning | 15 |
| normalize | region_by_recarea | warning | 7 |
| normalize | null_island | error | 7 |
| normalize | toll_free_phone | info | 6 |
| normalize | outside_claimed_state | warning | 2 |
| normalize | low_precision | warning | 2 |
| normalize | state_name_to_code | info | 2 |
| normalize | outside_region | warning | 1 |
| resolve | amenity_conflict | info | 32 |
| resolve | geometry_conflict | warning | 3 |
| resolve | high_impact_conflict | warning | 2 |

## Freshness and quality score

Records last seen between 2026-11-24 and 2026-11-25. Possibly closed (missing from their sources for several runs): 0. Records with season dates stated for a past year: 0.

Quality score (completeness, corroboration, freshness, validity): mean 0.664, median 0.600, 10th percentile 0.490.

| sources per entity | entities | mean quality |
|---|---|---|
| 1 | 207 | 0.567 |
| 2 | 34 | 0.796 |
| 3 | 49 | 0.888 |
| 4 | 14 | 0.983 |

## Top data gaps and next actions

| gap | entities missing it | missing by segment | suggested next action |
|---|---|---|---|
| max RV length | 94% (287) | OSM only 100% of 219 · USDA Forest Service 93% of 69 · NPS 31% of 16 | website extraction |
| price | 91% (277) | OSM only 95% of 219 · USDA Forest Service 99% of 69 · NPS 0% of 16 | website extraction; RIDB fees |
| season dates | 89% (270) | OSM only 94% of 219 · USDA Forest Service 94% of 69 · NPS 0% of 16 | extract seasons from official websites (crawler + LLM stage) |
| showers | 85% (259) | OSM only 91% of 219 · USDA Forest Service 86% of 69 · NPS 0% of 16 | website extraction (amenity lists) |
| number of sites | 75% (227) | OSM only 81% of 219 · USDA Forest Service 71% of 69 · NPS 0% of 16 | website extraction; RIDB campsite listings |
| phone | 73% (223) | OSM only 91% of 219 · USDA Forest Service 33% of 69 · NPS 0% of 16 | extract from official websites (crawler + LLM stage) |
| drinking water | 72% (220) | OSM only 91% of 219 · USDA Forest Service 30% of 69 · NPS 0% of 16 | website extraction (amenity lists) |
| website | 62% (190) | OSM only 68% of 219 · USDA Forest Service 58% of 69 · NPS 6% of 16 | official sites linked from RIDB facility pages; OSM `website` tags |
| reservations | 54% (165) | OSM only 75% of 219 · USDA Forest Service 0% of 69 · NPS 0% of 16 | recreation.gov ids via OSM/website links; website extraction |
| polygon | 53% (161) | OSM only 56% of 219 · USDA Forest Service 51% of 69 · NPS 19% of 16 | map campground outlines in OSM (improves matching and geometry) |
