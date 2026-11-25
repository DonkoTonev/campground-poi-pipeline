# Entity resolution evaluation

Region `sierra`, 2026-11-25, matching config `f65fb1b624`.

**Labels:** Claude-drafted labels (200 pairs, drafted blind to the matcher's score and decision), 50 human-verified, agreement 56% (28/50). After adjudicating 22 of the 22 disagreements, the final label sides with the draft on 16 and with the human label on 5, neither on 1.

Gold pairs with a match / non-match label: **189** (52 matches); pairs whose records are missing or no longer campgrounds: 0. The drafted labels were written from the two records alone, each with a one-line rationale; the human sample was drawn at random and labelled without seeing them. Intervals are Wilson 95%.

## Clustering (what the app shows)

| precision | 95% CI | recall | 95% CI | F1 | TP | FP | FN | TN |
|---|---|---|---|---|---|---|---|---|
| 1.000 | [0.92, 1.00] | 0.865 | [0.75, 0.93] | 0.928 | 45 | 0 | 7 | 137 |

## Against the human-verified sample

42 pairs with a human match / non-match label.

| precision | 95% CI | recall | 95% CI | F1 | TP | FP | FN | TN |
|---|---|---|---|---|---|---|---|---|
| 1.000 | [0.80, 1.00] | 0.500 | [0.33, 0.67] | 0.667 | 15 | 0 | 15 | 12 |

Disagreements between the drafted and the human label:

- B Circle vs Meadowview Campground: drafted non_match, human unsure (rationale: B Circle loop node vs Meadowview Campground; different names; note: -)
- Bridalveil Creek Campground vs Bridalveil Creek Horse Camp: drafted non_match, human match (rationale: Bridalveil Creek Campground vs Horse Camp; horse vs family camp; note: -)
- Lake George Tract vs Lake Mary Tract: drafted non_match, human match (rationale: Lake George Tract vs Lake Mary Tract; different names; note: -)
- Lower Lee Vining Campground vs Cattleguard Campground: drafted non_match, human unsure (rationale: Lower Lee Vining vs Cattleguard; different names; note: -)
- Upper Pines Campground vs North Pines Campground: drafted non_match, human match (rationale: Upper Pines vs North Pines; different NPS and recgov ids; note: -)
- Soda Springs Campground vs Soda Springs Campground: drafted non_match, human match (rationale: Same name but 41 km apart; different places; note: -)
- Wawona Campground vs Wawona Horse Campsites: drafted non_match, human match (rationale: Two different RIDB ids; Wawona vs Wawona Horse Campsites; note: -)
- Granite Creek Campground vs Green Creek Campground: drafted non_match, human unsure (rationale: Granite Creek vs Green Creek; different names, 63 km; note: -)
- Pumice Flat Campground vs Soda Springs Campground: drafted non_match, human match (rationale: Pumice Flat vs Soda Springs; different names; note: -)
- Kamook Staging Area vs : drafted unsure, human match (rationale: Kamook Staging Area vs unnamed node 1.6 km away; nothing else shared; note: -)
- D Circle vs Pinecrest Campground: drafted non_match, human unsure (rationale: D Circle loop node vs Pinecrest Campground; lettered loop presented separately, 1.1 km; note: -)
- Minaret Falls Campground vs Soda Springs Campground: drafted non_match, human unsure (rationale: Minaret Falls vs Soda Springs: different names, 1.6 km; note: -)
- Lake Mary Tract vs Twin Lakes Campground: drafted non_match, human match (rationale: Lake Mary Tract vs Twin Lakes Campground: different names, 1.8 km; note: -)
- Crags Campground vs Robinson Creek South: drafted non_match, human match (rationale: Crags Campground vs Robinson Creek South: different names; note: -)
- Agnew Meadows Horse Campground vs Agnew Meadows Campground: drafted non_match, human match (rationale: Horse campground vs family campground of same base name Agnew Meadows; note: -)
- D Circle vs C Circle: drafted non_match, human match (rationale: D Circle vs C Circle: different lettered loops; note: -)
-  vs : drafted unsure, human match (rationale: Two unnamed backcountry nodes 73 m apart; nothing to confirm; note: -)
-  vs : drafted unsure, human match (rationale: Two unnamed backcountry nodes 112 m apart; nothing to confirm; note: -)
- Mammoth Pool vs Placer Campground: drafted non_match, human unsure (rationale: Two different RIDB facility ids: Mammoth Pool vs Placer; note: -)
- Goat Meadow Dispersed Campsite vs Summerdale Campground: drafted non_match, human unsure (rationale: Goat Meadow Dispersed vs Summerdale Campground: different names/types; note: -)
-  vs : drafted non_match, human unsure (rationale: Two unnamed point nodes 1.8 km apart; distinct locations; note: -)
- Tuolumne Meadows Backpackers Campground vs Tuolumne Meadows Campground: drafted non_match, human match (rationale: Backpackers Campground vs Tuolumne Meadows Campground: separate walk-in facility; note: -)

## Pair score threshold sweep (before clustering constraints)

| threshold | precision | recall | F1 | FP | FN |
|---|---|---|---|---|---|
| -4 | 0.591 | 1.000 | 0.743 | 36 | 0 |
| -2 | 0.729 | 0.981 | 0.836 | 19 | 1 |
| 0 | 0.794 | 0.962 | 0.870 | 13 | 2 |
| 2 | 0.960 | 0.923 | 0.941 | 2 | 4 |
| 4 | 1.000 | 0.885 | 0.939 | 0 | 6 |
| 6 | 1.000 | 0.827 | 0.905 | 0 | 9 |
| 8 | 1.000 | 0.442 | 0.613 | 0 | 29 |

## False merges (0)


## Missed matches (7)

- osm:Agnew Meadows Horse Campground vs ridb:Agnew Horse Camp (score -1.5; name -1.5, prior -1.5, distance +1, category +0.5)
- osm:Pines Campground vs ridb:Pines Stanislaus (score -3; name -4, distance +2, prior -1.5, category +0.5)
- osm:Forks Campground vs ridb:Forks Campground (Sierra) (score 0.5; distance +3, name -1.5, prior -1.5, category +0.5)
- osm:Silver Lake Campground vs ridb:Silver Lake Campground June Lake (CA) (score 0.5; distance +3, name -1.5, prior -1.5, category +0.5)
- osm:Buckeye vs ridb:Buckeye (score 3; name +5, prior -1.5, distance -1, category +0.5)
- osm:Obsidian Campground vs ridb:Obsidian (score 5; name +5, prior -1.5, distance +1, category +0.5)
- osm:Crane Flat Campground vs nps:Crane Flat Campground (score 7; name +5, distance +3, prior -1.5, category +0.5)

## Matcher against each label set

The raw agreement above is kept as measured; adjudication adds a third label per
disagreement and never changes the drafted or the human one.

| labels | n | precision | 95% CI | recall | 95% CI | F1 | TP | FP | FN | TN |
|---|---|---|---|---|---|---|---|---|---|---|
| drafted labels (all pairs) | 189 | 1.000 | [0.92, 1.00] | 0.865 | [0.75, 0.93] | 0.928 | 45 | 0 | 7 | 137 |
| human labels (sample, before adjudication) | 42 | 1.000 | [0.80, 1.00] | 0.500 | [0.33, 0.67] | 0.667 | 15 | 0 | 15 | 12 |
| final labels (sample, after adjudication) | 47 | 1.000 | [0.80, 1.00] | 0.750 | [0.53, 0.89] | 0.857 | 15 | 0 | 5 | 27 |
| drafted + adjudicated corrections (all pairs) | 189 | 1.000 | [0.92, 1.00] | 0.804 | [0.68, 0.89] | 0.891 | 45 | 0 | 11 | 133 |

Disagreement kinds (draft -> human): non_match -> match: 11, non_match -> unsure: 8, unsure -> match: 3

| A | B | draft | rationale | human | final | why |
|---|---|---|---|---|---|---|
| B Circle | Meadowview Campground | non_match | B Circle loop node vs Meadowview Campground; different names | unsure | non_match |  |
| Bridalveil Creek Campground | Bridalveil Creek Horse Camp | non_match | Bridalveil Creek Campground vs Horse Camp; horse vs family camp | match | match |  |
| Lake George Tract | Lake Mary Tract | non_match | Lake George Tract vs Lake Mary Tract; different names | match | non_match |  |
| Lower Lee Vining Campground | Cattleguard Campground | non_match | Lower Lee Vining vs Cattleguard; different names | unsure | non_match |  |
| Upper Pines Campground | North Pines Campground | non_match | Upper Pines vs North Pines; different NPS and recgov ids | match | non_match |  |
| Soda Springs Campground | Soda Springs Campground | non_match | Same name but 41 km apart; different places | match | non_match |  |
| Wawona Campground | Wawona Horse Campsites | non_match | Two different RIDB ids; Wawona vs Wawona Horse Campsites | match | non_match |  |
| Granite Creek Campground | Green Creek Campground | non_match | Granite Creek vs Green Creek; different names, 63 km | unsure | non_match |  |
| Pumice Flat Campground | Soda Springs Campground | non_match | Pumice Flat vs Soda Springs; different names | match | non_match |  |
| Kamook Staging Area | (unnamed) | unsure | Kamook Staging Area vs unnamed node 1.6 km away; nothing else shared | match | non_match |  |
| D Circle | Pinecrest Campground | non_match | D Circle loop node vs Pinecrest Campground; lettered loop presented separately, 1.1 km | unsure | non_match |  |
| Minaret Falls Campground | Soda Springs Campground | non_match | Minaret Falls vs Soda Springs: different names, 1.6 km | unsure | non_match |  |
| Lake Mary Tract | Twin Lakes Campground | non_match | Lake Mary Tract vs Twin Lakes Campground: different names, 1.8 km | match | non_match |  |
| Crags Campground | Robinson Creek South | non_match | Crags Campground vs Robinson Creek South: different names | match | non_match |  |
| Agnew Meadows Horse Campground | Agnew Meadows Campground | non_match | Horse campground vs family campground of same base name Agnew Meadows | match | match |  |
| D Circle | C Circle | non_match | D Circle vs C Circle: different lettered loops | match | match |  |
| (unnamed) | (unnamed) | unsure | Two unnamed backcountry nodes 73 m apart; nothing to confirm | match | unsure |  |
| (unnamed) | (unnamed) | unsure | Two unnamed backcountry nodes 112 m apart; nothing to confirm | match | unsure |  |
| Mammoth Pool | Placer Campground | non_match | Two different RIDB facility ids: Mammoth Pool vs Placer | unsure | unsure |  |
| Goat Meadow Dispersed Campsite | Summerdale Campground | non_match | Goat Meadow Dispersed vs Summerdale Campground: different names/types | unsure | non_match |  |
| (unnamed) | (unnamed) | non_match | Two unnamed point nodes 1.8 km apart; distinct locations | unsure | non_match |  |
| Tuolumne Meadows Backpackers Campground | Tuolumne Meadows Campground | non_match | Backpackers Campground vs Tuolumne Meadows Campground: separate walk-in facility | match | match |  |
