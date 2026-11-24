import pytest
from sqlalchemy import Engine

from poi.db import check_extensions
from poi.runs import track_run

pytestmark = pytest.mark.integration


def test_required_extensions_installed(db: Engine) -> None:
    assert set(check_extensions(db)) == {"postgis", "pg_trgm"}


def test_track_run_records_failure(db: Engine) -> None:
    with pytest.raises(ValueError), track_run(db, "test", "sierra") as run:
        run.stats["n"] = 1
        raise ValueError("boom")
    with db.connect() as conn:
        status, stats = conn.exec_driver_sql(
            "SELECT status, stats FROM runs WHERE id = %s", (run.id,)
        ).one()
    assert status == "failed"
    assert stats == {"n": 1}
