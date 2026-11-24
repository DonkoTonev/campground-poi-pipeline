"""`poi ingest`: run source adapters and upsert `source_records`.

Idempotent: records are upserted on (source, source_id); `content_hash` tells changed from
unchanged. Records of a source that were not seen in a *complete* run get `missed_runs + 1`
(never deleted), which later drives the `possibly_closed` review flag.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Any

from sqlalchemy import Connection, Engine, text

from poi.fetch import FetchContext
from poi.http import make_client
from poi.log import get_logger
from poi.runs import track_run
from poi.settings import Region, Settings
from poi.sources import ALL_SOURCES
from poi.sources.base import SourceRecord
from poi.storage import RawStore

log = get_logger(__name__)

UPSERT = text(
    """
    INSERT INTO source_records AS sr (source, source_id, region, name, payload, geom, shape,
        content_hash, raw_object_id, first_seen_run_id, last_seen_run_id, last_changed_run_id)
    VALUES (:source, :source_id, :region, :name, CAST(:payload AS jsonb),
        ST_GeomFromText(:geom, 4326), ST_GeomFromText(:shape, 4326),
        :hash, :raw_id, :run_id, :run_id, :run_id)
    ON CONFLICT (source, source_id) DO UPDATE SET
        region = EXCLUDED.region,
        name = EXCLUDED.name,
        payload = EXCLUDED.payload,
        geom = EXCLUDED.geom,
        shape = EXCLUDED.shape,
        raw_object_id = EXCLUDED.raw_object_id,
        last_seen_run_id = EXCLUDED.last_seen_run_id,
        last_seen_at = now(),
        missed_runs = 0,
        last_changed_run_id = CASE WHEN sr.content_hash = EXCLUDED.content_hash
            THEN sr.last_changed_run_id ELSE EXCLUDED.last_changed_run_id END,
        content_hash = EXCLUDED.content_hash
    RETURNING (xmax = 0) AS inserted, (last_changed_run_id = :run_id) AS changed
    """
)


def content_hash(record: SourceRecord) -> str:
    canon = json.dumps(
        {
            "name": record.name,
            "payload": record.payload,
            "geom": record.point.wkt if record.point else None,
            "shape": record.shape.wkt if record.shape else None,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canon.encode()).hexdigest()


def upsert_source_record(
    conn: Connection, record: SourceRecord, raw_id: int | None, region: str, run_id: int
) -> str:
    """Insert or update one source record; returns "new", "changed" or "unchanged"."""
    row = conn.execute(
        UPSERT,
        {
            "source": record.source,
            "source_id": record.source_id,
            "region": region,
            "name": record.name,
            "payload": json.dumps(record.payload, default=str),
            "geom": record.point.wkt if record.point else None,
            "shape": record.shape.wkt if record.shape else None,
            "hash": content_hash(record),
            "raw_id": raw_id,
            "run_id": run_id,
        },
    ).one()
    if row.inserted:
        return "new"
    return "changed" if row.changed else "unchanged"


def ingest_source(
    engine: Engine,
    store: RawStore,
    settings: Settings,
    region: Region,
    source_name: str,
    refresh: bool = False,
) -> dict[str, Any]:
    source = ALL_SOURCES[source_name]
    if reason := source.missing_config(settings):
        log.warning("ingest.skipped", source=source_name, reason=reason)
        return {"skipped": reason}

    params = {"source": source_name, "refresh": refresh}
    with (
        track_run(engine, f"ingest:{source_name}", region.name, params) as run,
        make_client(settings) as client,
    ):
        ctx = FetchContext(engine, store, client, run.id, refresh)
        outcomes: Counter[str] = Counter()
        seen: set[str] = set()
        with engine.begin() as conn:
            for fetched in source.records(ctx, region, settings):
                rec = fetched.record
                if rec.source_id in seen:  # pagination overlap: count once
                    continue
                seen.add(rec.source_id)
                outcomes[
                    upsert_source_record(conn, rec, fetched.raw_object_id, region.name, run.id)
                ] += 1
            # Only reached if the whole source was read without error.
            missed = conn.execute(
                text(
                    "UPDATE source_records SET missed_runs = missed_runs + 1 "
                    "WHERE source = :s AND region = :r AND last_seen_run_id <> :run"
                ),
                {"s": source_name, "r": region.name, "run": run.id},
            ).rowcount
        run.stats.update(
            records=len(seen),
            new=outcomes["new"],
            changed=outcomes["changed"],
            unchanged=outcomes["unchanged"],
            missing=missed,
            **ctx.stats,
        )
        return dict(run.stats)
