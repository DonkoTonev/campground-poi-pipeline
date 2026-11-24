# Plan

Milestones in build order. Entity resolution and golden records are built on the structured
sources first (M3–M4), so the pipeline works end to end before any crawling or LLM spend.
Website-extracted records later join matching as one more source (M7), and we measure what they add.

| | Milestone | Status |
|---|---|---|
| M0 | Bootstrap: repo, tooling, compose (PostGIS, Elasticsearch), CI, pre-commit + gitleaks, baseline migration | ✅ done |
| M1 | Ingest OSM (Overpass), RIDB, NPS → raw store + `source_records`; counts per source | ✅ done |
| M2 | Canonical model: normalization + validation flags (names, contact, geometry, categories, seasons) | ✅ done |
| M3 | Entity resolution on structured sources: blocking, features, log-odds scoring, constrained clustering, hierarchy; label CLI, ER gold set + eval | ⏳ awaiting verified labels |
| M4 | Golden records: survivorship, stable IDs + redirects, overrides, quality score; quality / changes reports | ✅ done |
| M5 | Responsible crawler seeded from M1 records; crawl stats | ✅ done |
| M6 | Reduce + deterministic extraction; token metrics per representation | ⏳ next |
| M7 | LLM extraction: cost ladder, guardrails, ledger, dry-run → approval → run; extraction gold set + eval; ablation; extracted records join ER; coverage delta vs cost | |
| M8 | Serving (small): ES index with alias swap; search, bbox, along-route, get-by-id (+ redirects), health | |
| M9 | Polish: demo snapshot, `make demo`, README / DESIGN / aws docs with measured numbers | |

Stretch (only if time is left after M9): S1 embeddings, S2 LLM adjudication of gray-zone pairs,
S3 MCP server, S4 PDF brochures, S5 induced selectors.

## M0 checklist
- [x] `.gitignore` before first commit
- [x] uv project (Python 3.12), Typer CLI `poi`, pydantic-settings, structlog
- [x] Docker Compose: PostGIS 17-3.7, Elasticsearch 9.5.5 (single node)
- [x] Alembic baseline: `postgis`, `pg_trgm`, `runs`
- [x] pre-commit: ruff, ruff-format, gitleaks
- [x] GitHub Actions: ruff, mypy, alembic, pytest with a PostGIS service
- [x] Repo created and pushed (public); author identity verified
- [x] Publication gate: full-history gitleaks + `scripts/check_publishable.py` on every push and in CI

## M1 checklist
- [x] Raw store interface: local filesystem (default) and S3 (tested with moto)
- [x] `raw_objects` + `source_records` (migration 0002); fetch-once cache keyed by request
- [x] Polite HTTP: honest User-Agent, timeouts, retries honouring `Retry-After`
- [x] OSM adapter: nodes, ways and multipolygon relations, representative points
- [x] RIDB adapter: union of state / radius / rec-area listings (pagination is unstable)
- [x] NPS adapter: campgrounds for the region's park codes
- [x] Idempotent upsert, change detection, `missed_runs` for records that disappear
- [x] Recorded fixtures (real records, contact data scrubbed) + unit and integration tests
- [x] Counts per source reported (README)

## M2 checklist
- [x] `poi_records` (canonical, raw values kept) and `quality_flags` (migration 0003)
- [x] Names: NFKC, title junk, smart title case, matching keys with abbreviation expansion
- [x] Contact: E.164 phones (extensions, toll-free, multi-number fields), canonical URLs, emails, placeholders
- [x] Geometry: null island, out of range, swapped, positive longitude, low precision, outside region, outside claimed state (Natural Earth admin-1)
- [x] Categories, non-campground detection (organized camps, lodges, day use, aggregates), operator type
- [x] Seasons: month-day ranges, holiday anchors, OSM `opening_hours`, NPS recurring closures, stale flag
- [x] Amenities tri-state (never default to `no`), units (m → ft), sites, price, reservability
- [x] Unit tests for each edge case; idempotent stage integration test

## M3 checklist
- [x] Blocking in PostGIS: spatial (polygon-aware, precision-adaptive), pg_trgm names, shared ids
- [x] Features and explainable log-odds scoring (`config/matching.yaml`)
- [x] Discriminators, IDF-weighted phone/website, shared-id dominance, rename → review
- [x] Constrained clustering (cannot-link, strong non-match, diameter) and hierarchy
- [x] All §10 matching edge cases as unit tests; idempotent stage integration test
- [x] Gold set suggestion (200 pairs, stratified) and `poi review label`
- [ ] Labels verified by a person
- [ ] `poi eval er` on verified labels, thresholds tuned, numbers in README

## M4 checklist
- [x] `entities`, `entity_members`, `entity_fields` (value, provenance, reason, alternatives), `entity_redirects`, `entity_changes` (migration 0005)
- [x] Stable ids by member overlap: merge → redirect, split → largest part keeps id, retire, never delete
- [x] Survivorship in YAML: per-field trust, corroboration, polygon-first geometry, name vote, alt names
- [x] High-impact, geometry and amenity conflicts flagged for review
- [x] `data/overrides.yaml`: match decisions (must/cannot link) and field values, applied every run
- [x] `poi review queue` writes decisions to the overrides file
- [x] Quality score with components; `poi report quality` (coverage, flags, gaps by segment) and `poi report changes`
- [x] Unit tests (ids, survivorship, quality, overrides) and an entity-lifecycle integration test

## M5 checklist
- [x] Seeds from active entities, screened: scheme, SSRF (IP literals, private/loopback/link-local DNS), social, aggregators, API domains, booking engines (recorded as attribute)
- [x] Scrapy: robots.txt, honest User-Agent, 1 request per host, delay + AutoThrottle, timeouts, size cap
- [x] Retry-After middleware (capped, exponential backoff); block/captcha stops the whole host (per-host lock)
- [x] Scope: registrable domain, path subtree / identifying query on multi-location hosts, depth 2, 6 pages ranked by relevance; traps excluded
- [x] Canonical URL for identity, real URL for fetching; www and moved-seed redirects followed in scope
- [x] Detection: soft 404, parked, off-domain redirect, non-HTML (PDF stored), blocked, needs-render
- [x] Raw bodies gzipped in the raw store; `sites` and `pages` rows; conditional re-crawls (ETag / Last-Modified, content hash)
- [x] Playwright rendering behind `--render` (only for pages flagged needs-render)
- [x] Unit tests and an end-to-end crawl test against a local fixture server
