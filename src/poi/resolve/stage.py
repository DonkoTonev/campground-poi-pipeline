"""`poi resolve`: clusters → golden entities with stable ids, provenance and quality.

Reads the latest `poi match` clusters. Idempotent: running twice without new data changes
nothing (same ids, same content hashes, no change-log rows).
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Connection, Engine, text

from poi.log import get_logger
from poi.resolve import ids, overrides, quality
from poi.resolve.survivorship import (
    Candidate,
    FieldResult,
    NameCandidate,
    PointCandidate,
    Rules,
    choose,
    choose_amenities,
    choose_geometry,
    choose_name,
)
from poi.runs import track_run
from poi.settings import Region, load_yaml

log = get_logger(__name__)

SCALAR_FIELDS = (
    "category",
    "operator_type",
    "operator_name",
    "phone",
    "website",
    "reservation_url",
    "email",
    "state_code",
    "reservable",
    "first_come_first_served",
    "num_sites",
    "max_rv_length_ft",
)


@dataclass
class Member:
    poi_id: int
    ref: str
    source: str
    row: Any  # poi_records row (mapping)
    missed_runs: int
    last_seen_at: Any
    flags: list[tuple[str, str]]  # (rule, severity) from normalization


def _members(conn: Connection, region: str) -> dict[int, list[Member]]:
    """cluster_key -> members, for the latest match run of the region."""
    flags: dict[int, list[tuple[str, str]]] = defaultdict(list)
    for f in conn.execute(
        text(
            "SELECT subject_id, rule, severity FROM quality_flags "
            "WHERE stage = 'normalize' AND subject_type = 'poi_record'"
        )
    ):
        flags[f.subject_id].append((f.rule, f.severity))
    rows = conn.execute(
        text(
            """
            SELECT cm.cluster_key, p.*, ST_Y(p.geom) AS lat, ST_X(p.geom) AS lon,
                   p.shape IS NOT NULL AS has_shape, s.missed_runs, s.last_seen_at
            FROM cluster_members cm
            JOIN poi_records p ON p.id = cm.poi_record_id
            JOIN source_records s ON s.id = p.source_record_id
            WHERE cm.region = :r ORDER BY cm.cluster_key, p.id
            """
        ),
        {"r": region},
    ).mappings()
    clusters: dict[int, list[Member]] = defaultdict(list)
    for r in rows:
        clusters[r["cluster_key"]].append(
            Member(
                r["id"],
                f"{r['source']}:{r['source_id']}",
                r["source"],
                r,
                r["missed_runs"],
                r["last_seen_at"],
                flags[r["id"]],
            )
        )
    return clusters


def _previous(conn: Connection, region: str) -> dict[str, frozenset[str]]:
    rows = conn.execute(
        text(
            "SELECT e.id::text AS id, array_agg(m.record_ref) AS refs FROM entities e "
            "JOIN entity_members m ON m.entity_id = e.id "
            "WHERE e.region = :r AND e.status = 'active' GROUP BY e.id"
        ),
        {"r": region},
    )
    return {r.id: frozenset(r.refs) for r in rows}


@dataclass
class Golden:
    values: dict[str, Any]
    fields: dict[str, FieldResult]
    alt_names: list[str]
    flags: list[tuple[str, str, str, dict[str, Any]]]  # (field, rule, severity, details)


def build_golden(
    members: list[Member], rules: Rules, field_overrides: list[overrides.FieldOverride]
) -> Golden:
    values: dict[str, Any] = {}
    fields: dict[str, FieldResult] = {}
    flags: list[tuple[str, str, str, dict[str, Any]]] = []

    name, alt_names = choose_name(
        [
            NameCandidate(
                m.row["name"], m.row["match_name"], m.source, m.ref, tuple(m.row["alt_names"])
            )
            for m in members
            if m.row["name"]
        ],
        rules,
    )
    if name:
        fields["name"] = name
    geometry = choose_geometry(
        [
            PointCandidate(m.row["lat"], m.row["lon"], m.row["has_shape"], m.source, m.ref)
            for m in members
            if m.row["lat"] is not None
        ],
        rules,
    )
    if geometry:
        fields["geometry"] = geometry
        if geometry.conflict:
            flags.append(
                (
                    "geometry",
                    "geometry_conflict",
                    "warning",
                    {"alternatives": geometry.alternatives},
                )
            )

    def cands(getter: Any) -> list[Candidate]:
        return [Candidate(getter(m.row), m.source, m.ref) for m in members]

    for fld in SCALAR_FIELDS:
        result = choose(fld, cands(lambda r, f=fld: r[f]), rules)
        if result:
            fields[fld] = result
    price = choose(
        "price",
        cands(
            lambda r: (
                None
                if r["price_min"] is None
                else {
                    "min": float(r["price_min"]),
                    "max": float(r["price_max"]),
                    "currency": r["currency"],
                }
            )
        ),
        rules,
    )
    if price:
        fields["price"] = price
    season = choose(
        "season",
        cands(
            lambda r: (
                None
                if r["months_open"] is None
                else {"months_open": r["months_open"], "ranges": r["season"]}
            )
        ),
        rules,
    )
    if season:
        fields["season"] = season
    amenities, amenity_results = choose_amenities(
        [(m.source, m.ref, m.row["amenities"]) for m in members], rules
    )
    values["amenities"] = amenities
    for amenity, result in amenity_results.items():
        if result.conflict:
            flags.append(
                (
                    "amenities",
                    "amenity_conflict",
                    "info",
                    {
                        "amenity": amenity,
                        "chosen": result.value,
                        "alternatives": result.alternatives,
                    },
                )
            )

    for fld, result in fields.items():
        if result.conflict and rules.spec(fld).get("high_impact"):
            flags.append(
                (
                    fld,
                    "high_impact_conflict",
                    "warning",
                    {"chosen": result.value, "alternatives": result.alternatives},
                )
            )

    refs = {m.ref for m in members}
    for ov in field_overrides:
        if ov.record not in refs:
            continue
        previous = fields.get(ov.field)
        if ov.field.startswith("amenities.") and previous is None:
            amenity = ov.field.split(".", 1)[1]
            previous = FieldResult(values["amenities"].get(amenity), None, None, "survivorship")
        fields[ov.field] = FieldResult(
            value=ov.value,
            source="override",
            record_ref=ov.record,
            reason=f"manual override ({ov.date}): {ov.reason}",
            alternatives=[
                {"value": previous.value, "source": previous.source, "record": previous.record_ref}
            ]
            if previous
            else [],
        )
        # A resolved conflict is no longer an open review item.
        golden_field = "amenities" if ov.field.startswith("amenities.") else ov.field
        flags = [
            f
            for f in flags
            if f[0] != golden_field
            or (golden_field == "amenities" and f[3].get("amenity") != ov.field.split(".", 1)[1])
        ]
    for fld, result in fields.items():
        if fld.startswith("amenities."):
            values["amenities"][fld.split(".", 1)[1]] = result.value
        else:
            values[fld] = result.value
    values["season_note"] = next(
        (m.row["season_note"] for m in members if m.source == "nps" and m.row["season_note"]),
        None,
    )
    return Golden(values, fields, alt_names, flags)


def _hash(values: dict[str, Any], alt_names: list[str], parent: str | None) -> str:
    canon = json.dumps(
        {"v": values, "alt": alt_names, "parent": parent}, sort_keys=True, default=str
    )
    return hashlib.sha256(canon.encode()).hexdigest()


def _parents(conn: Connection, region: str, entity_of: dict[int, str]) -> dict[str, str]:
    rows = conn.execute(
        text("SELECT child_id, parent_id FROM record_relations WHERE region = :r"), {"r": region}
    )
    out: dict[str, str] = {}
    for r in rows:
        child, parent = entity_of.get(r.child_id), entity_of.get(r.parent_id)
        if child and parent and child != parent:
            out[child] = parent
    return out


def resolve_region(engine: Engine, region: Region) -> dict[str, Any]:
    rules = Rules(load_yaml("survivorship.yaml"))
    qcfg = rules.raw["quality"]
    ovr = overrides.load()
    with track_run(engine, "resolve", region.name) as run, engine.begin() as conn:
        clusters = _members(conn, region.name)
        keys = sorted(clusters)
        previous = _previous(conn, region.name)
        assignment = ids.assign(previous, [frozenset(m.ref for m in clusters[k]) for k in keys])
        entity_of = {
            m.poi_id: assignment.cluster_ids[i] for i, k in enumerate(keys) for m in clusters[k]
        }
        parents = _parents(conn, region.name, entity_of)
        old_hash: dict[str, str] = dict(
            conn.execute(
                text("SELECT id::text, content_hash FROM entities WHERE region = :r"),
                {"r": region.name},
            ).all()
        )

        conn.execute(
            text(
                "DELETE FROM quality_flags WHERE stage = 'resolve' AND entity_id IN "
                "(SELECT id FROM entities WHERE region = :r)"
            ),
            {"r": region.name},
        )
        conn.execute(
            text(
                "DELETE FROM entity_fields WHERE entity_id IN "
                "(SELECT id FROM entities WHERE region = :r)"
            ),
            {"r": region.name},
        )
        conn.execute(
            text(
                "DELETE FROM entity_members WHERE entity_id IN "
                "(SELECT id FROM entities WHERE region = :r)"
            ),
            {"r": region.name},
        )

        changes: Counter[str] = Counter()
        flag_counts: Counter[str] = Counter()
        built = []
        for i, key in enumerate(keys):
            members = clusters[key]
            eid = assignment.cluster_ids[i]
            golden = build_golden(members, rules, ovr.fields)
            missed = max(m.missed_runs for m in members)
            possibly_closed = (
                min(m.missed_runs for m in members) >= qcfg["possibly_closed_after_missed_runs"]
            )
            if possibly_closed:
                golden.flags.append(
                    ("status", "possibly_closed", "warning", {"missed_runs": missed})
                )
            q = quality.score(
                quality.QualityInputs(
                    golden=golden.values,
                    n_sources=len({m.source for m in members}),
                    missed_runs=missed,
                    stale_season=any(r == "stale_season" for m in members for r, _ in m.flags),
                    errors=sum(s == "error" for m in members for _, s in m.flags),
                    warnings=sum(s == "warning" for m in members for _, s in m.flags)
                    + sum(f[2] == "warning" for f in golden.flags),
                ),
                qcfg["weights"],
                qcfg["key_fields"],
            )
            parent = parents.get(eid)
            content_hash = _hash(golden.values, golden.alt_names, parent)
            built.append((eid, members, golden, q, parent, content_hash, possibly_closed))

        # Insert/update all entities first (parents may reference later ones), then the rest.
        for eid, members, golden, q, _, content_hash, possibly_closed in built:
            geo = golden.values.get("geometry") or {}
            shape_ref = (
                golden.fields["geometry"].record_ref if "geometry" in golden.fields else None
            )
            shape_id = next((m.poi_id for m in members if m.ref == shape_ref), None)
            conn.execute(
                text(
                    """
                    INSERT INTO entities (id, region, status, name, alt_names, category,
                        operator_type, state_code, geom, shape, months_open, golden, sources,
                        quality_score, quality, possibly_closed, last_seen_at, content_hash,
                        created_run_id, updated_run_id)
                    VALUES (CAST(:id AS uuid), :region, 'active', :name, :alt, :category, :op,
                        :state, CASE WHEN CAST(:lat AS float8) IS NULL THEN NULL
                            ELSE ST_SetSRID(ST_MakePoint(:lon, :lat), 4326) END,
                        (SELECT shape FROM poi_records WHERE id = :shape_id),
                        :months, CAST(:golden AS jsonb), :sources, :qscore,
                        CAST(:quality AS jsonb), :closed, :seen, :hash, :run, :run)
                    ON CONFLICT (id) DO UPDATE SET
                        status = 'active', name = EXCLUDED.name, alt_names = EXCLUDED.alt_names,
                        category = EXCLUDED.category, operator_type = EXCLUDED.operator_type,
                        state_code = EXCLUDED.state_code, geom = EXCLUDED.geom,
                        shape = EXCLUDED.shape, months_open = EXCLUDED.months_open,
                        golden = EXCLUDED.golden, sources = EXCLUDED.sources,
                        quality_score = EXCLUDED.quality_score, quality = EXCLUDED.quality,
                        possibly_closed = EXCLUDED.possibly_closed,
                        last_seen_at = EXCLUDED.last_seen_at,
                        content_hash = EXCLUDED.content_hash,
                        updated_run_id = CASE WHEN entities.content_hash = EXCLUDED.content_hash
                            THEN entities.updated_run_id ELSE EXCLUDED.updated_run_id END
                    """
                ),
                {
                    "id": eid,
                    "region": region.name,
                    "name": golden.values.get("name"),
                    "alt": golden.alt_names,
                    "category": golden.values.get("category"),
                    "op": golden.values.get("operator_type"),
                    "state": golden.values.get("state_code"),
                    "lat": geo.get("lat"),
                    "lon": geo.get("lon"),
                    "shape_id": shape_id,
                    "months": (golden.values.get("season") or {}).get("months_open"),
                    "golden": json.dumps(golden.values, default=str),
                    "sources": sorted({m.source for m in members}),
                    "qscore": q["score"],
                    "quality": json.dumps(q),
                    "closed": possibly_closed,
                    "seen": max(m.last_seen_at for m in members),
                    "hash": content_hash,
                    "run": run.id,
                },
            )
            if eid not in old_hash:
                changes["created"] += 1
                _change(conn, run.id, eid, "created", {"members": [m.ref for m in members]})
            elif old_hash[eid] != content_hash:
                changes["updated"] += 1
                _change(conn, run.id, eid, "updated", {})

        for eid, members, golden, _, parent, _, _ in built:
            conn.execute(
                text(
                    "UPDATE entities SET parent_id = CAST(:p AS uuid) WHERE id = CAST(:id AS uuid)"
                ),
                {"p": parent, "id": eid},
            )
            conn.execute(
                text(
                    "INSERT INTO entity_members (entity_id, poi_record_id, record_ref) "
                    "VALUES (CAST(:e AS uuid), :p, :ref)"
                ),
                [{"e": eid, "p": m.poi_id, "ref": m.ref} for m in members],
            )
            if golden.fields:
                conn.execute(
                    text(
                        "INSERT INTO entity_fields (entity_id, field, value, source, record_ref, "
                        "reason, alternatives) VALUES (CAST(:e AS uuid), :f, CAST(:v AS jsonb), "
                        ":s, :ref, :reason, CAST(:alts AS jsonb))"
                    ),
                    [
                        {
                            "e": eid,
                            "f": fld,
                            "v": json.dumps(r.value, default=str),
                            "s": r.source,
                            "ref": r.record_ref,
                            "reason": r.reason,
                            "alts": json.dumps(r.alternatives, default=str),
                        }
                        for fld, r in golden.fields.items()
                    ],
                )
            for fld, rule, severity, details in golden.flags:
                flag_counts[rule] += 1
                conn.execute(
                    text(
                        "INSERT INTO quality_flags (run_id, stage, subject_type, entity_id, field, "
                        "rule, severity, details) VALUES (:run, 'resolve', 'entity', "
                        "CAST(:e AS uuid), :f, :rule, :sev, CAST(:d AS jsonb))"
                    ),
                    {
                        "run": run.id,
                        "e": eid,
                        "f": fld,
                        "rule": rule,
                        "sev": severity,
                        "d": json.dumps(details, default=str),
                    },
                )

        for old, new in assignment.redirects.items():
            conn.execute(
                text(
                    "UPDATE entities SET status = 'merged', updated_run_id = :run "
                    "WHERE id = CAST(:id AS uuid)"
                ),
                {"run": run.id, "id": old},
            )
            conn.execute(
                text(
                    "INSERT INTO entity_redirects (old_id, new_id, reason, run_id) VALUES "
                    "(CAST(:old AS uuid), CAST(:new AS uuid), 'merged', :run) "
                    "ON CONFLICT (old_id) DO UPDATE SET new_id = EXCLUDED.new_id, "
                    "run_id = EXCLUDED.run_id"
                ),
                {"old": old, "new": new, "run": run.id},
            )
            # Keep lookups one hop: anything that pointed at `old` now points at `new`.
            conn.execute(
                text(
                    "UPDATE entity_redirects SET new_id = CAST(:new AS uuid) "
                    "WHERE new_id = CAST(:old AS uuid)"
                ),
                {"old": old, "new": new},
            )
            changes["merged"] += 1
            _change(conn, run.id, old, "merged", {"into": new})
        for old in assignment.retired:
            conn.execute(
                text(
                    "UPDATE entities SET status = 'retired', updated_run_id = :run "
                    "WHERE id = CAST(:id AS uuid)"
                ),
                {"run": run.id, "id": old},
            )
            changes["retired"] += 1
            _change(conn, run.id, old, "retired", {})

        scores = [q["score"] for _, _, _, q, _, _, _ in built]
        run.stats.update(
            entities=len(built),
            multi_source=sum(len({m.source for m in b[1]}) > 1 for b in built),
            with_parent=len(parents),
            changes=dict(changes),
            flags=dict(flag_counts),
            mean_quality=round(sum(scores) / len(scores), 3) if scores else None,
        )
        return dict(run.stats)


def _change(conn: Connection, run_id: int, eid: str, change: str, details: dict[str, Any]) -> None:
    conn.execute(
        text(
            "INSERT INTO entity_changes (run_id, entity_id, change, details) "
            "VALUES (:run, CAST(:e AS uuid), :c, CAST(:d AS jsonb))"
        ),
        {"run": run_id, "e": eid, "c": change, "d": json.dumps(details)},
    )
