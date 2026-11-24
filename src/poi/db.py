"""Database engine (SQLAlchemy 2 Core over psycopg 3). Schema changes go through Alembic only."""

from __future__ import annotations

from functools import lru_cache

from sqlalchemy import Engine, create_engine, text

from poi.settings import get_settings


@lru_cache
def get_engine(url: str | None = None) -> Engine:
    return create_engine(url or get_settings().database_url, pool_pre_ping=True)


def check_extensions(engine: Engine) -> dict[str, str]:
    """Return installed versions of the extensions the pipeline depends on."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT extname, extversion FROM pg_extension "
                "WHERE extname IN ('postgis', 'pg_trgm') ORDER BY extname"
            )
        )
        return {name: version for name, version in rows}
