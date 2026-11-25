"""`poi report quality` and `poi report changes`: Markdown reports in reports/.

Every number comes from the database at report time; nothing is typed in by hand.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import Connection, text

from poi.settings import PROJECT_ROOT

REPORTS = PROJECT_ROOT / "reports"

# Field -> (SQL predicate on poi_records, SQL predicate on entities, next action if missing)
FIELDS: dict[str, tuple[str, str, str]] = {
    "name": ("name IS NOT NULL", "name IS NOT NULL",
             "unnamed OSM sites: review, or attach as children of a nearby campground"),
    "phone": ("phone IS NOT NULL", "golden ? 'phone'",
              "extract from official websites (crawler + LLM stage)"),
    "website": ("website IS NOT NULL", "golden ? 'website'",
                "official sites linked from RIDB facility pages; OSM `website` tags"),
    "reservations": ("reservable IS NOT NULL OR reservation_url IS NOT NULL",
                     "golden ? 'reservable' OR golden ? 'reservation_url'",
                     "recreation.gov ids via OSM/website links; website extraction"),
    "season dates": ("months_open IS NOT NULL", "months_open IS NOT NULL",
                     "extract seasons from official websites (crawler + LLM stage)"),
    "number of sites": ("num_sites IS NOT NULL", "golden ? 'num_sites'",
                        "website extraction; RIDB campsite listings"),
    "drinking water": ("amenities->>'drinking_water' <> 'unknown'",
                       "golden->'amenities'->>'drinking_water' <> 'unknown'",
                       "website extraction (amenity lists)"),
    "showers": ("amenities->>'showers' <> 'unknown'",
                "golden->'amenities'->>'showers' <> 'unknown'",
                "website extraction (amenity lists)"),
    "max RV length": ("max_rv_length_ft IS NOT NULL", "golden ? 'max_rv_length_ft'",
                      "website extraction"),
    "price": ("price_min IS NOT NULL", "golden ? 'price'", "website extraction; RIDB fees"),
    "polygon": ("shape IS NOT NULL", "shape IS NOT NULL",
                "map campground outlines in OSM (improves matching and geometry)"),
}  # fmt: skip


def _scalar(conn: Connection, sql: str, **params: Any) -> Any:
    return conn.execute(text(sql), params).scalar_one()


def _pct(n: int, d: int) -> str:
    return "n/a" if d == 0 else f"{100 * n / d:.0f}% ({n})"


def quality_report(conn: Connection, region: str) -> str:
    r = {"r": region}
    sources = conn.execute(
        text(
            "SELECT source, count(*) AS n, count(*) FILTER (WHERE is_campground) AS camps "
            "FROM poi_records WHERE region = :r GROUP BY source ORDER BY source"
        ),
        r,
    ).all()
    n_records = sum(s.n for s in sources)
    n_camps = sum(s.camps for s in sources)
    n_entities = _scalar(
        conn, "SELECT count(*) FROM entities WHERE region = :r AND status = 'active'", **r
    )
    multi = _scalar(
        conn,
        "SELECT count(*) FROM entities WHERE region = :r AND status = 'active' "
        "AND cardinality(sources) > 1",
        **r,
    )
    lines = [
        "# Data quality report",
        "",
        f"Region `{region}`, generated {date.today().isoformat()} from the database.",
        "",
        "## Pipeline",
        "",
        "| source | records | campgrounds |",
        "|---|---|---|",
        *(f"| {s.source} | {s.n} | {s.camps} |" for s in sources),
        f"| **total** | **{n_records}** | **{n_camps}** |",
        "",
        f"{n_camps} campground records resolve to **{n_entities} entities** "
        f"({n_camps - n_entities} duplicates merged); {multi} entities are confirmed by more "
        f"than one source. {n_records - n_camps} records were excluded as non-campgrounds.",
        "",
        "## Field coverage",
        "",
        "Share of campground records per source, and of golden entities, with a value.",
        "",
        "| field | " + " | ".join(s.source for s in sources) + " | golden |",
        "|---|" + "---|" * (len(sources) + 1),
    ]
    gaps: list[tuple[float, str, str, str, str]] = []
    for label, (rec_sql, ent_sql, action) in FIELDS.items():
        cells = []
        for s in sources:
            n = _scalar(
                conn,
                f"SELECT count(*) FROM poi_records WHERE region = :r AND is_campground "  # noqa: S608
                f"AND source = :s AND ({rec_sql})",
                r=region,
                s=s.source,
            )
            cells.append(_pct(n, s.camps))
        golden = _scalar(
            conn,
            "SELECT count(*) FROM entities WHERE region = :r AND status = 'active' "  # noqa: S608
            f"AND ({ent_sql})",
            **r,
        )
        lines.append(f"| {label} | " + " | ".join(cells) + f" | {_pct(golden, n_entities)} |")
        missing = 1 - golden / n_entities if n_entities else 0
        gaps.append(
            (
                missing,
                label,
                _pct(n_entities - golden, n_entities),
                action,
                _segments(conn, region, ent_sql),
            )
        )

    flags = conn.execute(
        text(
            "SELECT q.stage, q.rule, q.severity, count(*) AS n FROM quality_flags q "
            "LEFT JOIN poi_records p ON p.id = q.subject_id "
            "LEFT JOIN entities e ON e.id = q.entity_id "
            "WHERE coalesce(p.region, e.region) = :r GROUP BY 1, 2, 3 ORDER BY 1, 4 DESC"
        ),
        r,
    ).all()
    lines += [
        "",
        "## Validation flags and conflicts",
        "",
        "Every rule violation is a flag; nothing is fixed silently.",
        "",
        "| stage | rule | severity | count |",
        "|---|---|---|---|",
        *(f"| {f.stage} | {f.rule} | {f.severity} | {f.n} |" for f in flags),
    ]

    fresh = conn.execute(
        text(
            "SELECT min(last_seen_at)::date AS oldest, max(last_seen_at)::date AS newest, "
            "count(*) FILTER (WHERE possibly_closed) AS closed, "
            "round(avg(quality_score)::numeric, 3) AS mean_q, "
            "percentile_cont(0.1) WITHIN GROUP (ORDER BY quality_score) AS p10, "
            "percentile_cont(0.5) WITHIN GROUP (ORDER BY quality_score) AS p50 "
            "FROM entities WHERE region = :r AND status = 'active'"
        ),
        r,
    ).one()
    stale = _scalar(
        conn,
        "SELECT count(DISTINCT q.subject_id) FROM quality_flags q JOIN poi_records p "
        "ON p.id = q.subject_id WHERE p.region = :r AND q.rule = 'stale_season'",
        **r,
    )
    by_sources = conn.execute(
        text(
            "SELECT cardinality(sources) AS k, count(*) AS n, "
            "round(avg(quality_score)::numeric, 3) AS q FROM entities "
            "WHERE region = :r AND status = 'active' GROUP BY 1 ORDER BY 1"
        ),
        r,
    ).all()
    lines += [
        "",
        "## Freshness and quality score",
        "",
        f"Records last seen between {fresh.oldest} and {fresh.newest}. "
        f"Possibly closed (missing from their sources for several runs): {fresh.closed}. "
        f"Records with season dates stated for a past year: {stale}.",
        "",
        f"Quality score (completeness, corroboration, freshness, validity): mean "
        f"{fresh.mean_q}, median {fresh.p50:.3f}, 10th percentile {fresh.p10:.3f}.",
        "",
        "| sources per entity | entities | mean quality |",
        "|---|---|---|",
        *(f"| {b.k} | {b.n} | {b.q} |" for b in by_sources),
        "",
        "## Top data gaps and next actions",
        "",
        "| gap | entities missing it | missing by segment | suggested next action |",
        "|---|---|---|---|",
        *(
            f"| {label} | {missing} | {segment} | {action} |"
            for share, label, missing, action, segment in sorted(gaps, reverse=True)
            if share > 0.3
        ),
        "",
    ]
    return "\n".join(lines)


SEGMENT_SQL = """
    CASE WHEN 'nps' = ANY(sources) THEN 'NPS'
         WHEN 'ridb' = ANY(sources) THEN coalesce(golden->>'operator_name', 'RIDB')
         ELSE 'OSM only' END
"""


def _segments(conn: Connection, region: str, ent_sql: str) -> str:
    """Share missing per source-backing segment, e.g. "OSM only 100% · USDA Forest Service
    100% · NPS 0%": tells which source would close the gap."""
    rows = conn.execute(
        text(
            f"""
            SELECT {SEGMENT_SQL} AS seg, count(*) AS n,
                   count(*) FILTER (WHERE NOT coalesce(({ent_sql}), false)) AS missing
            FROM entities WHERE region = :r AND status = 'active'
            GROUP BY 1 ORDER BY count(*) DESC
            """  # noqa: S608
        ),
        {"r": region},
    ).all()
    return " · ".join(f"{r.seg} {100 * r.missing / r.n:.0f}% of {r.n}" for r in rows)


def changes_report(conn: Connection, region: str) -> str:
    runs = conn.execute(
        text(
            "SELECT id, started_at FROM runs WHERE stage = 'resolve' AND region = :r "
            "AND status = 'succeeded' ORDER BY id DESC LIMIT 2"
        ),
        {"r": region},
    ).all()
    if not runs:
        return "# Changes\n\nNo resolve run yet.\n"
    latest = runs[0]
    rows = conn.execute(
        text(
            "SELECT c.change, e.name, c.details FROM entity_changes c "
            "JOIN entities e ON e.id = c.entity_id WHERE c.run_id = :run ORDER BY c.change, e.name"
        ),
        {"run": latest.id},
    ).all()
    since = (
        f"run {runs[1].id} ({runs[1].started_at:%Y-%m-%d %H:%M})"
        if len(runs) > 1
        else "nothing (first run)"
    )
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.change] = counts.get(row.change, 0) + 1
    lines = [
        "# Changes",
        "",
        f"Region `{region}`: resolve run {latest.id} ({latest.started_at:%Y-%m-%d %H:%M}) "
        f"compared with {since}.",
        "",
        "| change | entities |",
        "|---|---|",
        *(f"| {k} | {counts.get(k, 0)} |" for k in ("created", "updated", "merged", "retired")),
        "",
    ]
    for change in ("merged", "retired", "updated", "created"):
        items = [row for row in rows if row.change == change]
        if items:
            lines += [f"## {change.title()} ({len(items)})", ""]
            lines += [f"- {row.name or '(unnamed)'}" for row in items[:25]]
            if len(items) > 25:
                lines.append(f"- … and {len(items) - 25} more")
            lines.append("")
    return "\n".join(lines)
