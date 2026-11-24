"""Manual decisions in `data/overrides.yaml`, applied on every run.

Records are referenced as "source:source_id", which survives database rebuilds; an entity is
addressed through any of its member records, so overrides survive entity merges too.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from poi.settings import PROJECT_ROOT

OVERRIDES_PATH = PROJECT_ROOT / "data" / "overrides.yaml"
HEADER = """\
# Manual decisions, applied on every run. Written by `poi review` or by hand.
# Records are referenced as "source:source_id" (stable across database rebuilds).
#
# match:  {a: "osm:way/1", b: "ridb:232447", decision: match | non_match,
#          reason: "...", date: "YYYY-MM-DD"}
# fields: {record: "nps:<id>", field: phone, value: "+12095550100",
#          reason: "...", date: "YYYY-MM-DD"}
#         (the override applies to the entity that contains the record)
"""


@dataclass(frozen=True)
class MatchOverride:
    a: str
    b: str
    decision: str  # match | non_match
    reason: str
    date: str


@dataclass(frozen=True)
class FieldOverride:
    record: str
    field: str
    value: Any
    reason: str
    date: str


@dataclass
class Overrides:
    match: list[MatchOverride]
    fields: list[FieldOverride]


def load(path: Path | None = None) -> Overrides:
    path = path or OVERRIDES_PATH
    if not path.exists():
        return Overrides([], [])
    data = yaml.safe_load(path.read_text()) or {}
    return Overrides(
        [MatchOverride(**{k: str(v) for k, v in m.items()}) for m in data.get("match") or []],
        [FieldOverride(**{**f, "date": str(f["date"])}) for f in data.get("fields") or []],
    )


def save(overrides: Overrides, path: Path | None = None) -> None:
    path = path or OVERRIDES_PATH
    body = yaml.safe_dump(
        {
            "match": [asdict(m) for m in overrides.match],
            "fields": [asdict(f) for f in overrides.fields],
        },
        sort_keys=False,
        allow_unicode=True,
    )
    tmp = path.with_suffix(".tmp")
    tmp.write_text(HEADER + body)
    tmp.replace(path)


def add_match(overrides: Overrides, a: str, b: str, decision: str, reason: str) -> None:
    """Record a pair decision; a later decision on the same pair replaces the earlier one."""
    pair = {a, b}
    overrides.match = [m for m in overrides.match if {m.a, m.b} != pair]
    overrides.match.append(MatchOverride(a, b, decision, reason, date.today().isoformat()))


def add_field(overrides: Overrides, record: str, field: str, value: Any, reason: str) -> None:
    overrides.fields = [f for f in overrides.fields if (f.record, f.field) != (record, field)]
    overrides.fields.append(FieldOverride(record, field, value, reason, date.today().isoformat()))
