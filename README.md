# campground-poi-pipeline

A small, production-minded data pipeline that aggregates campground points of interest from
several public sources (OpenStreetMap, Recreation.gov RIDB, NPS API, official campground websites),
extracts structured facts from websites with an LLM at the lowest possible cost, resolves
duplicates — including near neighbours that are *not* duplicates — builds golden records with
field-level provenance, measures its own quality and cost, and serves the result through search
and an API.

> Work in progress. Progress is tracked in [docs/PLAN.md](docs/PLAN.md). Results will be filled
> in with measured numbers only.

## Quick start (development)

```bash
make setup      # uv sync + pre-commit hooks
cp .env.example .env   # then fill in keys and CONTACT_URL
make up migrate # Postgres/PostGIS + Elasticsearch, schema
make lint test
uv run poi doctor
make ingest     # OSM always; RIDB / NPS when their keys are set (otherwise skipped with a log)
make normalize  # canonical poi_records + quality_flags
make match      # entity resolution
make resolve    # golden records with stable ids and provenance
make report     # reports/quality.md, reports/changes.md
uv run poi crawl --plan   # screen website seeds, fetch nothing
make crawl      # crawl official campground websites (robots.txt, 1 request/host, honest UA)
make reduce     # pages + RIDB descriptions -> capped LLM input documents, token metrics
uv run poi review queue   # decide gray-zone pairs and conflicts -> data/overrides.yaml
uv run poi review label   # verify ER gold pairs
make eval       # precision / recall on verified labels
```

## Results so far

Measured on the default `sierra` region (Yosemite and the Eastern Sierra), 2026-11-24.
Numbers for later stages will be added as they are measured.

| Source | Records in region | Located | With polygon | Requests (first run) |
|---|---|---|---|---|
| OpenStreetMap (Overpass) | 289 | 289 | 158 | 1 |
| Recreation.gov RIDB | 89 | 82 | – | 54 |
| NPS API | 16 | 16 | – | 1 |

RIDB needs three listing strategies because its pagination silently skips facilities; the state
listing alone found 68 of the 82 located campgrounds. Details and other real-data edge cases:
[docs/DESIGN.md](docs/DESIGN.md#edge-cases-found-in-real-data).

Re-runs are served from the raw store: a second full ingest makes 0 network requests.

After normalization (`poi normalize`), 379 of 394 records are campgrounds. The other 15 are
organized camps, cabin-only lodges, a day-use area and aggregate pseudo-facilities, each with
a flag for review. Field coverage of the campgrounds shows where the gaps are:

| Source | Campgrounds | Phone | Website | Season dates | Site count | Drinking water known |
|---|---|---|---|---|---|---|
| OSM | 278 | 7 | 105 | 0 | 54 | 22 |
| RIDB | 85 | 60 | 0 | 0 | 0 | 0 |
| NPS | 16 | 16 | 15 | 16 | 16 | 16 |

Season dates are known for 16 of 379 campgrounds, all from NPS. Closing that gap from official
websites is the job of the LLM extraction stage, and its effect will be measured against this
baseline.

Entity resolution (`poi match`) turns the 379 campground records into 300 clusters: 79 records
merged, 15 campgrounds confirmed by all three sources, 36 pairs in the review queue, 5 group or
horse areas linked to a parent campground. Precision and recall are not reported yet: they
will be computed on a gold set of 200 pairs once a person has verified the labels.

Golden records (`poi resolve`): 300 entities with stable ids, each field chosen by
configurable source trust and corroboration, with every alternative and its provenance kept.
60 entities are confirmed by more than one source (15 by all three); their mean quality score
is 0.75 for two sources and 0.99 for three, against 0.58 for single-source entities. The
[quality report](reports/quality.md) shows where data is missing and which source would close
the gap: season dates are known for every NPS-backed campground and for none of the 69 Forest
Service or 215 OSM-only ones.

Crawling official websites (`poi crawl`): 121 candidate URLs → 78 crawlable seeds after
screening (30 on API domains, 10 booking engines, 2 aggregators, 1 unofficial site skipped).
15 sites crawled successfully (77 pages, about 5 per site), 2 seeds are dead, and 61 seeds on
fs.usda.gov were not crawled: the host answers an identified crawler with a block page, so the
crawler stopped there after one request instead of working around it. No page needed a
headless browser. Fetched pages average 144 KB of HTML but only about 5,600 characters of
visible text, which is the first hint of how much cheaper extraction gets after reduction.

Reduction (`poi reduce`, no LLM involved): crawled pages average about 36,400 tokens of raw
HTML, 2,100 as cleaned HTML and 950 as Markdown, i.e. 2.6% of the raw size, while keeping 97%
of distinct prices and phone numbers and 96% of month mentions (token counts estimated as
bytes / 4 until calibrated against the API). That gives 15 website bundles (about 4,300 tokens
each) and 85 RIDB description documents (about 760 tokens each) as LLM inputs; details in
[reports/tokens.md](reports/tokens.md).

## Data sources and attribution
- Map data © OpenStreetMap contributors, available under the Open Database License (ODbL).
- Recreation Information Database (RIDB), Recreation.gov.
- U.S. National Park Service API.
- State and province boundaries: Natural Earth (public domain), simplified subset in `data/reference/`.
