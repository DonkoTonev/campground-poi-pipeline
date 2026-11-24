from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Protocol

from shapely.geometry.base import BaseGeometry

from poi.fetch import FetchContext
from poi.settings import Region, Settings


@dataclass(frozen=True)
class SourceRecord:
    """A source's own view of one POI, before any normalization.

    `payload` keeps the source's structured fields verbatim (trimmed of bulky media), so
    normalization can be re-run and audited later. Geometry is WGS84.
    """

    source: str
    source_id: str
    name: str | None
    payload: dict[str, Any]
    point: BaseGeometry | None = None
    shape: BaseGeometry | None = None


@dataclass(frozen=True)
class FetchedRecord:
    raw_object_id: int
    record: SourceRecord


class Source(Protocol):
    name: str

    def missing_config(self, settings: Settings) -> str | None:
        """Why this source can't run (e.g. missing API key), or None if it can."""
        ...

    def records(
        self, ctx: FetchContext, region: Region, settings: Settings
    ) -> Iterator[FetchedRecord]: ...


def in_bbox(lat: float, lon: float, bbox: tuple[float, float, float, float]) -> bool:
    south, west, north, east = bbox
    return south <= lat <= north and west <= lon <= east


def to_float(value: Any) -> float | None:
    """Sources send coordinates as numbers, numeric strings, '' or null."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
