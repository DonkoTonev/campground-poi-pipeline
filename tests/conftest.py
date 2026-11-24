from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, make_url, text

from poi.settings import PROJECT_ROOT, Settings, get_settings

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
