"""Export the demo snapshot: structured source records only, contact data scrubbed.

Usage: uv run python scripts/export_demo.py [region]
Writes data/demo/source_records.jsonl.gz. OSM, RIDB and NPS structured records may be
redistributed with attribution (see data/demo/LICENSE.md). Crawled website content is never
exported. Phones become fictional 555-01xx numbers through a deterministic hash (equal
numbers stay equal, so phone-based matching behaves the same) and emails become example.com.
"""

from __future__ import annotations

import gzip
import json
import sys

from sqlalchemy import text

from poi.db import get_engine
from poi.privacy import scrub
from poi.settings import PROJECT_ROOT

OUT = PROJECT_ROOT / "data" / "demo" / "source_records.jsonl.gz"
SOURCES = ("osm", "ridb", "nps")


def main(region: str = "sierra") -> None:
    with get_engine().connect() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT source, source_id, name, payload, ST_AsText(geom) AS geom, "
                    "ST_AsText(shape) AS shape FROM source_records "
                    "WHERE region = :r AND source = ANY(:s) ORDER BY source, source_id"
                ),
                {"r": region, "s": list(SOURCES)},
            )
            .mappings()
            .all()
        )
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(OUT, "wt", encoding="utf-8") as fh:
        for row in rows:
            record = dict(row) | {"region": region}
            record["payload"] = scrub(record["payload"])
            fh.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
    print(
        f"wrote {OUT.relative_to(PROJECT_ROOT)}: {len(rows)} records, "
        f"{OUT.stat().st_size // 1024} KiB"
    )


if __name__ == "__main__":
    main(*sys.argv[1:])
