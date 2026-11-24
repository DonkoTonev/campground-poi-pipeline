# Plan

Milestones in build order. Entity resolution and golden records are built on the structured
sources first (M3–M4), so the pipeline works end to end before any crawling or LLM spend.
Website-extracted records later join matching as one more source (M7), and we measure what they add.

| | Milestone | Status |
|---|---|---|
| M0 | Bootstrap: repo, tooling, compose (PostGIS, Elasticsearch), CI, pre-commit + gitleaks, baseline migration | ✅ done |
| M1 | Ingest OSM (Overpass), RIDB, NPS → raw store + `source_records`; counts per source | ⏳ next |
| M2 | Canonical model: normalization + validation flags (names, contact, geometry, categories, seasons) | |
| M3 | Entity resolution on structured sources: blocking, features, log-odds scoring, constrained clustering, hierarchy; label CLI, ER gold set + eval | |
| M4 | Golden records: survivorship, stable IDs + redirects, overrides, quality score; quality / changes reports | |
| M5 | Responsible crawler seeded from M1 records; crawl stats | |
| M6 | Reduce + deterministic extraction; token metrics per representation | |
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
