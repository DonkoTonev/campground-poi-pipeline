# Notes for AI assistants

## Commands
- `make setup` (deps + git hooks), `make up` / `make down` (Postgres+PostGIS, Elasticsearch), `make migrate`
- `make lint` (ruff + format check + mypy strict), `make test` (integration tests skip if Postgres is down)
- `uv run poi --help` lists the pipeline stages; `uv run poi doctor` checks config without printing secrets
- Pipeline order: `ingest → normalize → match → resolve`, then `crawl → reduce → extract` and
  `normalize → match → resolve` again, then `index`, `serve`, `report`; each stage is idempotent
- `make demo`: keyless demo on the committed snapshot (own database `poi_demo`, index prefix)
- LLM: never run `poi extract` without `--dry-run` first and the user's approval of the estimate;
  always pass `--max-cost-usd`. Tests use fixtures and recordings only.
- Manual fixes go in `data/overrides.yaml` (via `poi review queue`), never as direct DB edits

## Conventions
- Python 3.12, typed (mypy strict), small modules under `src/poi/<stage>/`.
- Every stage is idempotent and wraps its work in `poi.runs.track_run` (one `runs` row per invocation).
- Fetch once, process many times: external responses go to the raw store first; later stages read from it.
- Schema changes only through hand-written Alembic migrations in `migrations/versions/`.
- Domain rules live in `config/*.yaml`, secrets in `.env` (see `.env.example`). Never hardcode either.
- Never "fix" data silently: every rule violation writes a `quality_flags` row.
- Conventional commits (`feat:`, `fix:`, `test:`, `docs:`, `chore:`), small and focused.

## Data rules
- The repository is public: every push is a publication. `make publishable` (also a pre-push hook and a CI
  step) runs gitleaks over the full history plus `scripts/check_publishable.py`. Never bypass it (`--no-verify`).
- Never commit `.env`, raw or reduced third-party page content, or personal data.
- Demo data: phones → fictional 555-01xx numbers, emails → example.com.
- Crawling: obey robots.txt, honest User-Agent from `CONTACT_URL`, never bypass captchas/logins.
- Paid LLM runs need a dry-run cost estimate and explicit approval first.
- Docs contain only measured numbers; say "not measured" otherwise.
