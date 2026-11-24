"""Elasticsearch index of active entities. Postgres stays the system of record.

Each `poi index` builds a new versioned index (`campgrounds-<region>-v<run>`), then swaps the
alias `campgrounds-<region>` to it atomically: searches never see a half-built index, and a
bad build can be rolled back by pointing the alias at the previous version.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from elasticsearch import Elasticsearch, helpers
from sqlalchemy import Engine, text

from poi.runs import track_run
from poi.settings import Region, get_settings

KEEP_OLD_VERSIONS = 1

SETTINGS = {
    "number_of_shards": 1,
    "number_of_replicas": 0,
    "analysis": {
        "filter": {"autocomplete": {"type": "edge_ngram", "min_gram": 1, "max_gram": 15}},
        "analyzer": {
            "autocomplete": {
                "tokenizer": "standard",
                "filter": ["lowercase", "asciifolding", "autocomplete"],
            },
            "folded": {"tokenizer": "standard", "filter": ["lowercase", "asciifolding"]},
        },
    },
}

MAPPINGS = {
    "dynamic": "strict",
    "properties": {
        "id": {"type": "keyword"},
        "name": {
            "type": "text",
            "analyzer": "folded",
            "fields": {
                "autocomplete": {
                    "type": "text",
                    "analyzer": "autocomplete",
                    "search_analyzer": "folded",
                },
                "keyword": {"type": "keyword"},
            },
        },
        "alt_names": {"type": "text", "analyzer": "folded"},
        "location": {"type": "geo_point"},
        "category": {"type": "keyword"},
        "operator_type": {"type": "keyword"},
        "state_code": {"type": "keyword"},
        "status": {"type": "keyword"},
        "amenities": {"type": "keyword"},  # available: golden value "yes" or "seasonal"
        "months_open": {"type": "integer"},
        "quality_score": {"type": "float"},
        "sources": {"type": "keyword"},
        "parent_id": {"type": "keyword"},
    },
}


def alias_name(region: str, prefix: str | None = None) -> str:
    return f"{prefix or get_settings().index_prefix}-{region}"


def documents(engine: Engine, region: str) -> Iterator[dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT id::text AS id, name, alt_names, category, operator_type, state_code,
                       ST_Y(geom) AS lat, ST_X(geom) AS lon, months_open, quality_score,
                       sources, possibly_closed, parent_id::text AS parent_id,
                       golden->'amenities' AS amenities
                FROM entities WHERE region = :r AND status = 'active'
                """
            ),
            {"r": region},
        )
        for r in rows:
            yield {
                "id": r.id,
                "name": r.name,
                "alt_names": r.alt_names,
                "location": {"lat": r.lat, "lon": r.lon} if r.lat is not None else None,
                "category": r.category,
                "operator_type": r.operator_type,
                "state_code": r.state_code,
                "status": "possibly_closed" if r.possibly_closed else "active",
                # "seasonal" counts as available: the campground is open in season anyway.
                # "partial" (only some sites) does not: a filter must not over-promise.
                "amenities": sorted(
                    k for k, v in (r.amenities or {}).items() if v in ("yes", "seasonal")
                ),
                "months_open": r.months_open or [],
                "quality_score": r.quality_score,
                "sources": r.sources,
                "parent_id": r.parent_id,
            }


def build(
    engine: Engine, es: Elasticsearch, region: Region, alias: str | None = None
) -> dict[str, Any]:
    alias = alias or alias_name(region.name)
    with track_run(engine, "index", region.name) as run:
        index = f"{alias}-v{run.id}"
        es.indices.create(index=index, settings=SETTINGS, mappings=MAPPINGS)
        ok, errors = helpers.bulk(
            es,
            (
                {"_index": index, "_id": d["id"], "_source": d}
                for d in documents(engine, region.name)
            ),
            raise_on_error=False,
        )
        es.indices.refresh(index=index)
        previous = (
            list(es.indices.get_alias(name=alias).keys())
            if es.indices.exists_alias(name=alias)
            else []
        )
        actions: list[dict[str, Any]] = [{"remove": {"index": i, "alias": alias}} for i in previous]
        actions.append({"add": {"index": index, "alias": alias}})
        es.indices.update_aliases(actions=actions)  # atomic swap
        versions = sorted(
            es.indices.get(index=f"{alias}-v*"),
            key=lambda i: int(i.rsplit("-v", 1)[1]),
        )
        for old in versions[: -(KEEP_OLD_VERSIONS + 1)]:
            es.indices.delete(index=old)
        run.stats.update(
            index=index,
            documents=ok,
            errors=len(errors) if isinstance(errors, list) else errors,
            previous=previous,
        )
        return dict(run.stats)
