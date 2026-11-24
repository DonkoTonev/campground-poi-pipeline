"""Seasons: structured month-day ranges, holiday anchors resolved per year, staleness.

The same model serves structured sources now and LLM-extracted text later
("Memorial Day weekend through Labor Day, weather permitting").
"""

from __future__ import annotations

import calendar
import re
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from typing import Any

from poi.normalize.model import Flag, Severity

MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}
MIN_SEASONAL_CLOSURE_DAYS = 28  # shorter closures (storms, events) are not a season


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """n-th weekday (0=Mon) of a month; n=-1 means the last one."""
    if n > 0:
        first = date(year, month, 1)
        return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))
    last = date(year, month, calendar.monthrange(year, month)[1])
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def resolve_anchor(anchor: str, year: int) -> date:
    """US holiday anchors used by campground seasons."""
    match anchor:
        case "memorial_day":
            return _nth_weekday(year, 5, 0, -1)
        case "memorial_day_weekend":  # the Friday before Memorial Day
            return _nth_weekday(year, 5, 0, -1) - timedelta(days=3)
        case "independence_day":
            return date(year, 7, 4)
        case "labor_day":
            return _nth_weekday(year, 9, 0, 1)
        case "columbus_day" | "indigenous_peoples_day":
            return _nth_weekday(year, 10, 0, 2)
        case "thanksgiving":
            return _nth_weekday(year, 11, 3, 4)
    raise ValueError(f"unknown holiday anchor: {anchor}")


@dataclass(frozen=True)
class SeasonRange:
    """An open period. A bound is either a month-day or a holiday anchor."""

    start_month: int | None = None
    start_day: int | None = None
    end_month: int | None = None
    end_day: int | None = None
    start_anchor: str | None = None
    end_anchor: str | None = None
    year: int | None = None  # the year the source stated, if any
    approximate: bool = False
    raw: str | None = None

    def dates(self, year: int) -> tuple[date, date]:
        start = (
            resolve_anchor(self.start_anchor, year)
            if self.start_anchor
            else date(year, self.start_month or 1, self.start_day or 1)
        )
        if self.end_anchor:
            end = resolve_anchor(self.end_anchor, year)
        else:
            end_month = self.end_month or 12
            last = calendar.monthrange(year, end_month)[1]
            end = date(year, end_month, min(self.end_day or last, last))
        return start, end

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v not in (None, False)}


ALL_YEAR = SeasonRange(1, 1, 12, 31, raw="all year")


def months_open(ranges: list[SeasonRange], year: int) -> list[int]:
    """Months with at least one open day; ranges that wrap the new year are handled."""
    months: set[int] = set()
    for r in ranges:
        start, end = r.dates(year)
        if end < start:  # e.g. Nov-Mar
            spans = [(start, date(year, 12, 31)), (date(year, 1, 1), end)]
        else:
            spans = [(start, end)]
        for a, b in spans:
            months.update(range(a.month, b.month + 1))
    return sorted(months)


_OSM_RANGE = re.compile(
    r"^(?P<m1>[A-Za-z]{3})(?:\s+(?P<d1>\d{1,2}))?\s*-\s*(?P<m2>[A-Za-z]{3})(?:\s+(?P<d2>\d{1,2}))?"
    r"(?:\s*:?\s*(?:24/7|\d{2}:\d{2}-\d{2}:\d{2}))?$"
)


def parse_osm_opening_hours(value: str) -> list[SeasonRange] | None:
    """Season-level subset of OSM `opening_hours`: "24/7", "May-Oct", "May 15-Oct 15: 24/7".
    Returns None for anything else (daily hours, complex rules)."""
    text = value.strip()
    if text == "24/7":
        return [ALL_YEAR]
    ranges = []
    for part in (p.strip() for p in text.split(";")):
        m = _OSM_RANGE.match(part)
        if not m or m["m1"].lower() not in MONTHS or m["m2"].lower() not in MONTHS:
            return None
        ranges.append(
            SeasonRange(
                start_month=MONTHS[m["m1"].lower()],
                start_day=int(m["d1"]) if m["d1"] else None,
                end_month=MONTHS[m["m2"].lower()],
                end_day=int(m["d2"]) if m["d2"] else None,
                raw=part,
            )
        )
    return ranges or None


SEASONAL_NAME = re.compile(r"season|winter|snow", re.IGNORECASE)


def nps_season(
    operating_hours: list[dict[str, Any]], today: date
) -> tuple[list[SeasonRange], list[Flag]]:
    """Derive the open season from NPS operating hours.

    NPS encodes a seasonal campground as a long, fully "Closed" exception with explicit
    dates, and usually lists only the upcoming closure (last winter's is gone). So the
    closure is treated as recurring every year: the open season is the gap between the end
    of the closure and its start, e.g. closed Oct 19 - Apr 21 means open Apr 22 - Oct 18.
    Closures named like a season ("Seasonal Winter Closure") are preferred over other long
    closures (construction, fire). No long closure and daily standard hours: open all year.
    """
    closures: list[tuple[date, date, str]] = []
    for block in operating_hours:
        for exc in block.get("exceptions") or []:
            hours = exc.get("exceptionHours") or {}
            if not hours or any(v != "Closed" for v in hours.values()):
                continue
            try:
                begin = date.fromisoformat(exc["startDate"])
                end = date.fromisoformat(exc["endDate"])
            except (KeyError, ValueError):
                continue
            if (end - begin).days + 1 >= MIN_SEASONAL_CLOSURE_DAYS:
                closures.append((begin, end, exc.get("name") or ""))
    if not closures:
        has_hours = any(block.get("standardHours") for block in operating_hours)
        return ([ALL_YEAR] if has_hours else []), []

    seasonal = [c for c in closures if SEASONAL_NAME.search(c[2])] or closures
    begin, end, name = max(seasonal, key=lambda c: c[0])
    flags: list[Flag] = []
    if end.year < today.year:
        flags.append(
            Flag("season", "stale_season", Severity.WARNING, {"latest_stated": end.isoformat()})
        )
    if (end - begin).days >= 360:
        flags.append(Flag("season", "closed_all_season", Severity.WARNING, {"closure": name}))
        return [], flags
    opens, closes = end + timedelta(days=1), begin - timedelta(days=1)
    return [
        SeasonRange(
            opens.month,
            opens.day,
            closes.month,
            closes.day,
            year=opens.year,
            raw=f"nps closure: {name} {begin.isoformat()}..{end.isoformat()}",
        )
    ], flags
