"""`poi normalize`: source_records → poi_records + quality_flags, idempotently.

Re-running replaces the region's normalize-stage flags and upserts records on
`source_record_id`, so rule changes can be applied to all data without re-fetching.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import date
from typing import Any

from sqlalchemy import Connection, Engine, text

from poi.log import get_logger
from poi.normalize.model import CanonicalRecord
from poi.normalize.pipeline import SourceRow, normalize
from poi.runs import track_run
from poi.settings import Region

log = get_logger(__name__)

COLUMNS = (
    "source", "source_id", "region", "name", "match_name", "alt_names", "category",
    "is_campground", "exclusion_reason", "operator_type", "operator_name", "phone", "phone_ext",
    "website", "reservation_url", "email", "address", "state_code", "precision_m", "ids",
    "amenities", "num_sites", "sites_by_type", "max_rv_length_ft", "reservable",
    "first_come_first_served", "price_min", "price_max", "currency", "season", "months_open",
    "season_note", "raw",
)  # fmt: skip
JSON_COLUMNS = frozenset({"address", "ids", "amenities", "sites_by_type", "season", "raw"})

# Built only from the constant COLUMNS tuple; all values are bound parameters.
UPSERT = text(
    f"""
    INSERT INTO poi_records (source_record_id, run_id, geom, shape, {", ".join(COLUMNS)})
    SELECT :source_record_id, :run_id, sr.geom, sr.shape,
        {", ".join(f"CAST(:{c} AS jsonb)" if c in JSON_COLUMNS else f":{c}" for c in COLUMNS)}
    FROM source_records sr WHERE sr.id = :source_record_id
    ON CONFLICT (source_record_id) DO UPDATE SET
        run_id = EXCLUDED.run_id, geom = EXCLUDED.geom, shape = EXCLUDED.shape,
        normalized_at = now(),
        {", ".join(f"{c} = EXCLUDED.{c}" for c in COLUMNS)}
    RETURNING id
    """
)


def _params(rec: CanonicalRecord, run_id: int) -> dict[str, Any]:
    params: dict[str, Any] = {"source_record_id": rec.source_record_id, "run_id": run_id}
    for col in COLUMNS:
        value = getattr(rec, col)
        params[col] = json.dumps(value, default=str) if col in JSON_COLUMNS else value
    params["category"] = rec.category.value
    params["operator_type"] = rec.operator_type.value
    return params


def _rows(conn: Connection, region: str) -> list[SourceRow]:
    result = conn.execute(
        text(
            "SELECT id, source, source_id, region, payload, ST_Y(geom) AS lat, ST_X(geom) AS lon "
            "FROM source_records WHERE region = :r ORDER BY id"
        ),
        {"r": region},
    )
    return [
        SourceRow(r.id, r.source, r.source_id, r.region, r.payload, r.lat, r.lon) for r in result
    ]


def normalize_region(engine: Engine, region: Region, today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    with track_run(engine, "normalize", region.name, {"today": today.isoformat()}) as run:
        rules: Counter[str] = Counter()
        by_source: Counter[str] = Counter()
        excluded: Counter[str] = Counter()
        with engine.begin() as conn:
            conn.execute(
                text(
                    "DELETE FROM quality_flags WHERE stage = 'normalize' AND subject_type = "
                    "'poi_record' AND subject_id IN (SELECT id FROM poi_records WHERE region = :r)"
                ),
                {"r": region.name},
            )
            for row in _rows(conn, region.name):
                rec = normalize(row, region, today)
                poi_id = conn.execute(UPSERT, _params(rec, run.id)).scalar_one()
                by_source[rec.source] += 1
                if rec.exclusion_reason:
                    excluded[rec.exclusion_reason] += 1
                for flag in rec.flags:
                    rules[flag.rule] += 1
                    conn.execute(
                        text(
                            "INSERT INTO quality_flags (run_id, stage, subject_type, subject_id, "
                            "field, rule, severity, details) VALUES (:run, 'normalize', "
                            "'poi_record', :sid, :field, :rule, :sev, CAST(:details AS jsonb))"
                        ),
                        {
                            "run": run.id,
                            "sid": poi_id,
                            "field": flag.field,
                            "rule": flag.rule,
                            "sev": flag.severity.value,
                            "details": json.dumps(flag.details, default=str),
                        },
                    )
        run.stats.update(
            records=sum(by_source.values()),
            by_source=dict(by_source),
            excluded=dict(excluded),
            flags=dict(rules.most_common()),
        )
        return dict(run.stats)
