"""`poi demo`: the whole pipeline on the committed snapshot. No API keys, no crawling.

Loads data/demo/source_records.jsonl.gz (structured OSM, RIDB and NPS records, contact data
scrubbed) into its own database, then normalize → match → resolve → index. The LLM stage is
not part of the demo until recorded extraction outputs are committed.
"""

from __future__ import annotations

import gzip
import json
from datetime import date
from typing import Any

from alembic import command
from alembic.config import Config
from elasticsearch import Elasticsearch
from shapely import wkt
from sqlalchemy import Engine, create_engine, make_url, text

from poi.ingest import upsert_source_record
from poi.match.stage import match_region
from poi.normalize.stage import normalize_region
from poi.resolve.stage import resolve_region
from poi.runs import track_run
from poi.serve.index import build
from poi.settings import PROJECT_ROOT, Settings, load_region
from poi.sources.base import SourceRecord

SNAPSHOT = PROJECT_ROOT / "data" / "demo" / "source_records.jsonl.gz"
SNAPSHOT_DATE = date(2026, 11, 24)  # seasons and staleness are judged as of the snapshot


def ensure_database(url: str) -> None:
    target = make_url(url)
    admin = create_engine(target.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname = :d"), {"d": target.database}
        ).scalar()
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{target.database}"'))
    admin.dispose()
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    cfg.attributes["url"] = url
    command.upgrade(cfg, "head")


def load_snapshot(engine: Engine, region: str) -> int:
    with gzip.open(SNAPSHOT, "rt", encoding="utf-8") as fh:
        rows = [json.loads(line) for line in fh]
    with track_run(engine, "demo_load", region, {"snapshot": SNAPSHOT.name}) as run:
        with engine.begin() as conn:
            for row in rows:
                record = SourceRecord(
                    source=row["source"],
                    source_id=row["source_id"],
                    name=row["name"],
                    payload=row["payload"],
                    point=wkt.loads(row["geom"]) if row["geom"] else None,
                    shape=wkt.loads(row["shape"]) if row["shape"] else None,
                )
                upsert_source_record(conn, record, None, row["region"], run.id)
        run.stats["records"] = len(rows)
    return len(rows)


def summary(engine: Engine, region: str) -> dict[str, Any]:
    with engine.connect() as conn:
        by_source: dict[str, int] = dict(
            conn.execute(
                text("SELECT source, count(*) FROM poi_records WHERE region = :r GROUP BY 1"),
                {"r": region},
            ).all()
        )
        entities, multi = conn.execute(
            text(
                "SELECT count(*), count(*) FILTER (WHERE cardinality(sources) > 1) FROM entities "
                "WHERE region = :r AND status = 'active'"
            ),
            {"r": region},
        ).one()
        example = conn.execute(
            text(
                "SELECT id::text FROM entities WHERE name = 'Upper Pines Campground' "
                "AND status = 'active'"
            )
        ).scalar()
    return {
        "records_by_source": by_source,
        "entities": entities,
        "multi_source_entities": multi,
        "example_id": example,
    }


def run_demo(settings: Settings) -> dict[str, Any]:
    region = load_region(settings=settings)
    ensure_database(settings.database_url)
    engine = create_engine(settings.database_url)
    load_snapshot(engine, region.name)
    normalize_region(engine, region, SNAPSHOT_DATE)
    match_region(engine, region)
    resolve_region(engine, region)
    build(engine, Elasticsearch(settings.elasticsearch_url), region)
    return summary(engine, region.name)
