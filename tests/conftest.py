from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine

from poi.settings import get_settings


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    """Engine for integration tests; skips cleanly when Postgres isn't up."""
    eng = create_engine(get_settings().database_url)
    try:
        eng.connect().close()
    except Exception:
        pytest.skip("Postgres not reachable (run `make up migrate`)")
    yield eng
    eng.dispose()
