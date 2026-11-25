# Entity resolution evaluation

Region `sierra`, 2026-11-25, matching config `f65fb1b624`.

**Labels:** Claude-drafted labels (200 pairs, drafted blind to the matcher's score and decision), 50 drawn for human verification (0 verified so far), agreement pending.

Gold pairs with a match / non-match label: **189** (52 matches); pairs whose records are missing or no longer campgrounds: 0. The drafted labels were written from the two records alone, each with a one-line rationale; the human sample was drawn at random and labelled without seeing them. Intervals are Wilson 95%.

## Clustering (what the app shows)

| precision | 95% CI | recall | 95% CI | F1 | TP | FP | FN | TN |
|---|---|---|---|---|---|---|---|---|
| 1.000 | [0.92, 1.00] | 0.865 | [0.75, 0.93] | 0.928 | 45 | 0 | 7 | 137 |

## Against the human-verified sample

0 pairs with a human match / non-match label.

| precision | 95% CI | recall | 95% CI | F1 | TP | FP | FN | TN |
|---|---|---|---|---|---|---|---|---|
| n/a | n/a | n/a | n/a | n/a | 0 | 0 | 0 | 0 |

Disagreements between the drafted and the human label:

- none so far

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
- osm:Crags Campground vs ridb:Crags Campground (score 3; name +5, prior -1.5, distance -1, category +0.5)
- osm:Buckeye vs ridb:Buckeye (score 3; name +5, prior -1.5, distance -1, category +0.5)
- osm:Obsidian Campground vs ridb:Obsidian (score 5; name +5, prior -1.5, distance +1, category +0.5)
