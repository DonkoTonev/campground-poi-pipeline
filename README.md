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

## Data sources and attribution
- Map data © OpenStreetMap contributors, available under the Open Database License (ODbL).
- Recreation Information Database (RIDB), Recreation.gov.
- U.S. National Park Service API.
