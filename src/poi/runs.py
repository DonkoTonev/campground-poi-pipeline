"""Run bookkeeping: every stage invocation writes one `runs` row with params and stats."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Engine, text

from poi.log import get_logger

log = get_logger(__name__)


def git_sha() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return out.stdout.strip() or None


@dataclass
class Run:
    id: int
    stage: str
    region: str
    stats: dict[str, Any] = field(default_factory=dict)


@contextmanager
def track_run(
    engine: Engine, stage: str, region: str, params: dict[str, Any] | None = None
) -> Iterator[Run]:
    """Open a run row, yield it, and close it as `succeeded` or `failed` with its stats."""
    with engine.begin() as conn:
        run_id = conn.execute(
            text(
                "INSERT INTO runs (stage, region, git_sha, params) "
                "VALUES (:stage, :region, :sha, CAST(:params AS jsonb)) RETURNING id"
            ),
            {
                "stage": stage,
                "region": region,
                "sha": git_sha(),
                "params": json.dumps(params or {}),
            },
        ).scalar_one()
    run = Run(id=run_id, stage=stage, region=region)
    log.info("run.start", run_id=run_id, stage=stage, region=region)
    status = "failed"
    try:
        yield run
        status = "succeeded"
    finally:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE runs SET status = :status, finished_at = now(), "
                    "stats = CAST(:stats AS jsonb) WHERE id = :id"
                ),
                {"status": status, "stats": json.dumps(run.stats, default=str), "id": run_id},
            )
        log.info("run.end", run_id=run_id, stage=stage, status=status, **run.stats)
