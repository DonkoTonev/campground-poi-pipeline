# `make help` lists the targets. Pipeline order: README.md, "Full run".
UV := uv run

.DEFAULT_GOAL := help

.PHONY: help
help:  ## list targets
	@grep -hE '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | sed -E 's/:.*## /\t/' | expand -t 14

.PHONY: setup up down migrate doctor ingest normalize match resolve crawl reduce index serve report eval fixtures test test-unit lint fmt typecheck

setup:  ## install deps + git hooks
	uv sync
	$(UV) pre-commit install  # installs pre-commit and pre-push hooks

up:  ## start Postgres/PostGIS and Elasticsearch, wait until healthy
	docker compose up -d --wait

down:  ## stop the containers (data volumes are kept)
	docker compose down

migrate:  ## apply database migrations
	$(UV) alembic upgrade head

doctor:  ## check configuration without printing secrets
	$(UV) poi doctor

ingest:  ## fetch structured sources (raw store first) into source_records
	$(UV) poi ingest

normalize:  ## source_records -> canonical poi_records + quality_flags
	$(UV) poi normalize

match:  ## entity resolution: candidate pairs, decisions, clusters, relations
	$(UV) poi match

resolve:  ## golden records with stable ids, provenance and quality score
	$(UV) poi resolve

crawl:  ## responsible crawl of official websites seeded from entities
	$(UV) poi crawl

reduce:  ## pages and RIDB descriptions -> LLM input documents with token metrics
	$(UV) poi reduce

index:  ## new versioned search index, atomic alias swap
	$(UV) poi index

serve:  ## API on http://127.0.0.1:8000 (docs at /docs)
	$(UV) poi serve

report:  ## Markdown reports in reports/
	$(UV) poi report quality
	$(UV) poi report changes
	$(UV) poi report tokens
	$(UV) poi report cost

eval:  ## ER precision/recall against the gold labels (reports/er_eval.md)
	$(UV) poi eval er

fixtures:  ## re-cut scrubbed test fixtures from the raw store
	$(UV) python scripts/record_fixtures.py

test:  ## all tests (integration tests skip without Postgres)
	$(UV) pytest

test-unit:  ## unit tests only
	$(UV) pytest -m "not integration"

lint:  ## ruff, format check, mypy strict
	$(UV) ruff check .
	$(UV) ruff format --check .
	$(UV) mypy

fmt:  ## apply ruff fixes and formatting
	$(UV) ruff check --fix .
	$(UV) ruff format .

typecheck:  ## mypy strict
	$(UV) mypy

.PHONY: publishable
publishable:  ## full-history leak + publication checks (also runs on every push)
	$(UV) pre-commit run --hook-stage pre-push --all-files

# Keyless demo: its own database and index prefix, so it never touches a development setup.
POI_DB_PORT ?= 5433
DEMO_ENV := DATABASE_URL=postgresql+psycopg://poi:poi@localhost:$(POI_DB_PORT)/poi_demo INDEX_PREFIX=demo-campgrounds

.PHONY: demo
demo:  ## fresh clone, no API keys: compose up, load snapshot, pipeline, index, start the API
	uv sync --frozen
	docker compose up -d --wait
	$(DEMO_ENV) $(UV) poi demo
	$(DEMO_ENV) $(UV) poi serve
