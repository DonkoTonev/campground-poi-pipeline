# Targets are added as their milestone lands; see docs/PLAN.md.
UV := uv run

.PHONY: setup up down migrate doctor ingest normalize match eval fixtures test test-unit lint fmt typecheck

setup:  ## install deps + git hooks
	uv sync
	$(UV) pre-commit install  # installs pre-commit and pre-push hooks

up:  ## start Postgres/PostGIS and Elasticsearch, wait until healthy
	docker compose up -d --wait

down:
	docker compose down

migrate:
	$(UV) alembic upgrade head

doctor:
	$(UV) poi doctor

ingest:  ## fetch structured sources (raw store first) into source_records
	$(UV) poi ingest

normalize:  ## source_records -> canonical poi_records + quality_flags
	$(UV) poi normalize

match:  ## entity resolution: candidate pairs, decisions, clusters, relations
	$(UV) poi match

eval:  ## metrics on verified gold labels (reports/)
	$(UV) poi eval er

fixtures:  ## re-cut scrubbed test fixtures from the raw store
	$(UV) python scripts/record_fixtures.py

test:
	$(UV) pytest

test-unit:
	$(UV) pytest -m "not integration"

lint:
	$(UV) ruff check .
	$(UV) ruff format --check .
	$(UV) mypy

fmt:
	$(UV) ruff check --fix .
	$(UV) ruff format .

typecheck:
	$(UV) mypy

.PHONY: publishable
publishable:  ## full-history leak + publication checks (also runs on every push)
	$(UV) pre-commit run --hook-stage pre-push --all-files
