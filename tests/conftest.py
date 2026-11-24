from __future__ import annotations

import json
import uuid
from collections.abc import Iterator, Sequence
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import numpy.typing as npt
import pytest
import respx
from alembic import command
from alembic.config import Config
from elasticsearch import Elasticsearch
from sqlalchemy import Engine, create_engine, make_url, text

from poi.settings import PROJECT_ROOT, Settings, get_settings, load_region

FIXTURES = Path(__file__).parent / "fixtures"
TEST_DB = "poi_test"


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings isolated from the developer's .env: fake keys, temp raw store."""
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        contact_url="https://example.org/contact",
        ridb_api_key="test-ridb-key",
        nps_api_key="test-nps-key",
        raw_store_path=tmp_path / "raw",
    )


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    """A dedicated, freshly migrated test database on the same server; skips if unreachable."""
    base = make_url(get_settings().database_url)
    admin = create_engine(base, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)"))
            conn.execute(text(f"CREATE DATABASE {TEST_DB}"))
    except Exception:
        pytest.skip("Postgres not reachable (run `make up`)")
    finally:
        admin.dispose()
    url = base.set(database=TEST_DB).render_as_string(hide_password=False)
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    cfg.attributes["url"] = url
    command.upgrade(cfg, "head")
    eng = create_engine(url)
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine: Engine) -> Iterator[Engine]:
    """Per-test clean tables."""
    yield engine
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE source_records, raw_objects, runs RESTART IDENTITY CASCADE"))


# Served pipeline shared by the API and MCP tests.
@pytest.fixture
def es() -> Iterator[Elasticsearch]:
    client = Elasticsearch(get_settings().elasticsearch_url)
    try:
        if not client.ping():
            pytest.skip("Elasticsearch not reachable (run `make up`)")
    except Exception:
        pytest.skip("Elasticsearch not reachable (run `make up`)")
    yield client
    client.close()


@pytest.fixture
def served(
    db: Engine, es: Elasticsearch, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> Iterator[str]:
    """Fixture pipeline into PostGIS plus a throwaway search alias; yields the alias."""
    from poi.ingest import ingest_source
    from poi.match.stage import match_region
    from poi.normalize.stage import normalize_region
    from poi.resolve.stage import resolve_region
    from poi.serve.index import build
    from poi.sources import ridb
    from poi.storage.local import LocalStore

    sierra = load_region("sierra")
    monkeypatch.setattr(ridb, "POLITE_DELAY_S", 0)
    store = LocalStore(settings.raw_store_path)
    with respx.mock:
        respx.post(settings.overpass_url).mock(
            return_value=httpx.Response(200, json=load_fixture("osm/overpass.json"))
        )
        respx.get(url__startswith=settings.ridb_base_url).mock(
            return_value=httpx.Response(200, json=load_fixture("ridb/facilities.json"))
        )
        respx.get(url__startswith=settings.nps_base_url).mock(
            return_value=httpx.Response(200, json=load_fixture("nps/campgrounds.json"))
        )
        for source in ("osm", "ridb", "nps"):
            ingest_source(db, store, settings, sierra, source)
    normalize_region(db, sierra, date(2026, 11, 24))
    match_region(db, sierra)
    resolve_region(db, sierra)
    alias = f"test-campgrounds-{uuid.uuid4().hex[:8]}"
    build(db, es, sierra, alias=alias)
    yield alias
    for index in es.indices.get(index=f"{alias}-v*"):  # deletes need explicit names
        es.indices.delete(index=index)


def entity_id(db: Engine, name: str) -> str:
    with db.connect() as conn:
        return str(
            conn.execute(
                text("SELECT id::text FROM entities WHERE name = :n AND status = 'active'"),
                {"n": name},
            ).scalar_one()
        )


class TrigramEmbedder:
    """Hashed character trigrams: similar spellings get similar vectors."""

    model_name = "fake-trigrams"

    def embed(self, texts: Sequence[str]) -> npt.NDArray[np.float32]:
        out = np.zeros((len(texts), 64), dtype=np.float32)
        for row, t in enumerate(texts):
            s = f"  {t.lower()} "
            for i in range(len(s) - 2):
                out[row, sum(map(ord, s[i : i + 3])) % 64] += 1
        return out
