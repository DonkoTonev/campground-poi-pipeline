# Embeddings in entity resolution: ablation

Region `sierra`, 2026-11-25, model `BAAI/bge-small-en-v1.5` (local, fastembed), top 5 neighbours at cosine ≥ 0.85 within 20 km.

**Labels:** Claude-drafted labels (200 pairs, drafted blind to the matcher's score and decision), 50 drawn for human verification (0 verified so far), agreement pending.

189 gold pairs with a match / non-match label (52 matches). Intervals are Wilson 95%. The gold pairs were sampled from the blocking candidates that existed before embeddings (plus a stratum of similar names blocking missed), so pairs that only embeddings find are under-represented; every changed decision is listed below, labelled or not.

## Clustering against the labels

| variant | precision [95% CI] | recall [95% CI] | F1 | TP | FP | FN |
|---|---|---|---|---|---|---|
| off (current default) | 1.000 [0.92, 1.00] | 0.865 [0.75, 0.93] | 0.928 | 45 | 0 | 7 |
| candidates only | 1.000 [0.92, 1.00] | 0.865 [0.75, 0.93] | 0.928 | 45 | 0 | 7 |
| candidates + feature | 0.959 [0.86, 0.99] | 0.904 [0.79, 0.96] | 0.931 | 47 | 2 | 5 |

## Candidates and pair scores

| variant | candidate pairs | gold matches generated | pair P / R at the match threshold | clusters |
|---|---|---|---|---|
| off (current default) | 660 | 52/52 | 1.000 / 0.885 | 300 |
| candidates only | 681 | 52/52 | 1.000 / 0.885 | 300 |
| candidates + feature | 681 | 52/52 | 0.980 / 0.923 | 296 |

## Merge decisions that change: candidates only vs off (0)

- none

## Merge decisions that change: candidates + feature vs off (5)

- merged: osm:Buckeye (Upper) + osm:Buckeye (label: non-match)
- merged: osm:Buckeye (Upper) + ridb:Buckeye (label: non-match)
- merged: osm:Buckeye + ridb:Buckeye (label: match)
- merged: osm:Lake Mary Tract + osm:Upper Lake Mary Tract (unlabelled)
- merged: osm:Crags Campground + ridb:Crags Campground (label: match)
