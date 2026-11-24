"""Crawl seeds: website URLs of active campground entities, screened before any request."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import asdict
from typing import Any

from sqlalchemy import Connection, text

from poi.crawl.urls import registrable_domain, scope_for, screen_seed


def candidate_urls(conn: Connection, region: str) -> dict[str, list[str]]:
    """raw URL -> member record refs. Raw values too: a URL the normalizer rejected must
    still be screened (and recorded as skipped), not silently forgotten."""
    rows = conn.execute(
        text(
            """
            SELECT u.url, m.record_ref
            FROM entities e
            JOIN entity_members m ON m.entity_id = e.id
            JOIN poi_records p ON p.id = m.poi_record_id,
            LATERAL (VALUES (p.website), (p.raw->>'website')) AS u(url)
            WHERE e.region = :r AND e.status = 'active' AND u.url IS NOT NULL AND u.url <> ''
            """
        ),
        {"r": region},
    )
    out: dict[str, set[str]] = defaultdict(set)
    for url, ref in rows:
        out[url].add(ref)
    return {u: sorted(refs) for u, refs in out.items()}


def upsert_sites(
    conn: Connection,
    region: str,
    cfg: dict[str, Any],
    extra_seeds: list[str] | None = None,
    allow_private: bool = False,
) -> Counter[str]:
    """Screen every candidate; one `sites` row per canonical seed (or per rejected raw URL)."""
    merged: dict[str, dict[str, Any]] = {}
    candidates = candidate_urls(conn, region) | {u: [] for u in extra_seeds or []}
    for raw, refs in candidates.items():
        decision = screen_seed(raw, cfg, allow_private)
        key = decision.url or raw
        site = merged.setdefault(
            key,
            {
                "seed": key,
                "refs": set(),
                "status": "pending" if decision.url else "skipped",
                "reason": decision.reason,
                "platform": decision.booking_platform,
            },
        )
        site["refs"] |= set(refs)
    outcome: Counter[str] = Counter()
    for site in merged.values():
        crawlable = site["status"] == "pending"
        scope = asdict(scope_for(site["seed"], cfg)) if crawlable else {}
        conn.execute(
            text(
                """
                INSERT INTO sites (region, seed_url, registrable_domain, scope, record_refs,
                                   status, skip_reason, booking_platform)
                VALUES (:region, :seed, :domain, CAST(:scope AS jsonb), :refs, :status, :reason,
                        :platform)
                ON CONFLICT (seed_url) DO UPDATE SET
                    record_refs = EXCLUDED.record_refs, scope = EXCLUDED.scope,
                    skip_reason = EXCLUDED.skip_reason,
                    booking_platform = EXCLUDED.booking_platform,
                    status = CASE WHEN EXCLUDED.status = 'skipped' THEN 'skipped'
                                  ELSE sites.status END,
                    updated_at = now()
                """
            ),
            {
                "region": region,
                "seed": site["seed"],
                "domain": registrable_domain(site["seed"]) if crawlable else None,
                "scope": json.dumps(scope),
                "refs": sorted(site["refs"]),
                "status": site["status"],
                "reason": site["reason"],
                "platform": site["platform"],
            },
        )
        outcome[site["reason"] or "crawlable"] += 1
    return outcome
