"""Candidate generation in PostGIS: spatial, name-trigram, shared-id and website blocking.

The index-backed blocking passes are unioned; each pair remembers which passes found it.
Distances are polygon-aware (0 when a point lies inside the other record's polygon).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Connection, text

BLOCKING_SQL = text(
    """
    WITH r AS (
        SELECT id, geom, shape, coalesce(shape, geom) AS g, coalesce(precision_m, 0) AS prec,
               match_name, ids, website
        FROM poi_records WHERE region = :region AND is_campground
    ),
    spatial AS (
        SELECT a.id AS a_id, b.id AS b_id, 'spatial' AS how
        FROM r a JOIN r b ON a.id < b.id
         AND ST_DWithin(a.g::geography, b.g::geography,
                        greatest(:radius_m, :precision_factor * greatest(a.prec, b.prec)))
    ),
    by_name AS (  -- for records whose coordinates can't be trusted for spatial blocking
        SELECT a.id, b.id, 'name'
        FROM r a JOIN r b ON a.id < b.id AND a.match_name % b.match_name
        WHERE a.g IS NULL OR b.g IS NULL OR greatest(a.prec, b.prec) > :low_precision_m
    ),
    by_id AS (
        SELECT a.id, b.id, 'shared_id'
        FROM r a JOIN r b ON a.id < b.id
         AND (a.ids->>'recgov_facility_id' = b.ids->>'recgov_facility_id'
              OR a.ids->>'nps_id' = b.ids->>'nps_id'
              OR a.ids->>'wikidata' = b.ids->>'wikidata')
    ),
    by_website AS (  -- an extracted website record and the record that linked to the site
        SELECT a.id, b.id, 'shared_website'
        FROM r a JOIN r b ON a.id < b.id AND a.website = b.website
        WHERE a.g IS NULL OR b.g IS NULL
    ),
    pairs AS (
        SELECT a_id, b_id, array_agg(DISTINCT how ORDER BY how) AS blocked_by
        FROM (SELECT * FROM spatial UNION ALL SELECT * FROM by_name UNION ALL SELECT * FROM by_id
              UNION ALL SELECT * FROM by_website) u
        GROUP BY a_id, b_id
    )
    SELECT p.a_id, p.b_id, p.blocked_by,
           CASE WHEN a.g IS NOT NULL AND b.g IS NOT NULL
                THEN ST_Distance(a.g::geography, b.g::geography) END AS distance_m,
           coalesce(a.shape IS NOT NULL AND b.geom IS NOT NULL
                    AND ST_Contains(a.shape, b.geom), false) AS a_contains_b,
           coalesce(b.shape IS NOT NULL AND a.geom IS NOT NULL
                    AND ST_Contains(b.shape, a.geom), false) AS b_contains_a
    FROM pairs p JOIN r a ON a.id = p.a_id JOIN r b ON b.id = p.b_id
    """
)

LOW_PRECISION_M = 500.0

# Same distance and containment columns as BLOCKING_SQL, for pairs another source proposed.
PAIR_GEOMETRY_SQL = text(
    """
    WITH p AS (SELECT * FROM unnest(CAST(:a AS bigint[]), CAST(:b AS bigint[])) AS t(a_id, b_id)),
    r AS (SELECT id, geom, shape, coalesce(shape, geom) AS g FROM poi_records)
    SELECT p.a_id, p.b_id,
           CASE WHEN a.g IS NOT NULL AND b.g IS NOT NULL
                THEN ST_Distance(a.g::geography, b.g::geography) END AS distance_m,
           coalesce(a.shape IS NOT NULL AND b.geom IS NOT NULL
                    AND ST_Contains(a.shape, b.geom), false) AS a_contains_b,
           coalesce(b.shape IS NOT NULL AND a.geom IS NOT NULL
                    AND ST_Contains(b.shape, a.geom), false) AS b_contains_a
    FROM p JOIN r a ON a.id = p.a_id JOIN r b ON b.id = p.b_id
    """
)


def candidate_rows(conn: Connection, region: str, blocking: dict[str, Any]) -> list[Any]:
    conn.execute(
        text("SELECT set_config('pg_trgm.similarity_threshold', :t, true)"),
        {"t": str(blocking["name_similarity"])},
    )
    return list(
        conn.execute(
            BLOCKING_SQL,
            {
                "region": region,
                "radius_m": blocking["radius_m"],
                "precision_factor": blocking["precision_factor"],
                "low_precision_m": LOW_PRECISION_M,
            },
        )
    )


def pair_geometry(conn: Connection, pairs: list[tuple[int, int]]) -> list[Any]:
    if not pairs:
        return []
    return list(
        conn.execute(PAIR_GEOMETRY_SQL, {"a": [a for a, _ in pairs], "b": [b for _, b in pairs]})
    )
