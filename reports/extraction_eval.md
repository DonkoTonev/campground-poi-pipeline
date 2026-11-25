# Extraction evaluation

18 synthetic gold cases (expected values correct by construction), prompt `d02faa951d0b`, provider `anthropic` (claude-haiku-5-5 → claude-sonnet-5-5), 2026-11-25.

| metric | value |
|---|---|
| page kind accuracy | 100% |
| campground recall | 100% |
| field accuracy | 96% (46 fields) |
| empty-field rate | 4% |
| hallucination / privacy bait violations | 0 |
| escalations to the strong model | 1 of 18 |
| input tokens per page (real, anthropic) | 5,240 |
| output tokens per page | 202 |
| same tokens on Claude Haiku 5.5 (estimate) | $0.000625 per page |

## Per field

| field | correct |
|---|---|
| amenities | 5/5 |
| currency | 2/2 |
| email | 1/1 |
| max_rv_length | 1/1 |
| name | 17/17 |
| num_sites | 5/5 |
| phone | 1/1 |
| price_max | 1/2 |
| price_min | 2/3 |
| reservable | 1/1 |
| season | 4/4 |
| status | 3/3 |
| status_as_of | 1/1 |

## Errors

- cad_french: price_min expected 32, got None
- cad_french: price_max expected 48, got None
