"""`poi reduce`: crawled pages and RIDB descriptions → LLM input documents, with metrics.

Free and idempotent. For every crawled page it stores four representations (raw HTML,
cleaned HTML, Markdown, text) with their token counts, so the cost report can compare them;
for every site it builds one capped bundle; for every RIDB campground with a description, a
`ridb_description` document. Deterministic facts (JSON-LD, tel:, mailto:) ride along.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from collections import Counter
from typing import Any

from sqlalchemy import Connection, Engine, text

from poi.reduce import bundle, deterministic, ridb
from poi.reduce.html import fact_retention, represent
from poi.reduce.tokens import METHOD, estimate
from poi.runs import track_run
from poi.settings import Region, Settings
from poi.storage import RawStore, make_store

REDUCER_VERSION = "r1"
BUNDLE_CAP_TOKENS = 12_000


def _put(store: RawStore, key: str, content: str) -> str:
    store.put(key, gzip.compress(content.encode("utf-8")))
    return key


def _sha(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _site_pages(conn: Connection, region: str) -> dict[int, list[Any]]:
    rows = conn.execute(
        text(
            """
            SELECT s.id AS site_id, s.seed_url, s.record_refs, p.id AS page_id, p.url, p.depth,
                   p.relevance, r.storage_key
            FROM sites s
            JOIN pages p ON p.site_id = s.id
            JOIN raw_objects r ON r.id = p.raw_object_id
            WHERE s.region = :r AND s.status = 'ok' AND p.outcome IN ('fetched', 'unchanged')
              AND p.content_type LIKE 'text/html%'
            ORDER BY s.id, p.depth, p.relevance DESC NULLS FIRST, p.id
            """
        ),
        {"r": region},
    ).all()
    out: dict[int, list[Any]] = {}
    for row in rows:
        out.setdefault(row.site_id, []).append(row)
    return out


def _upsert_document(conn: Connection, **row: Any) -> None:
    conflict = (
        "(kind, site_id, reducer_version) WHERE site_id IS NOT NULL"
        if row.get("site_id")
        else "(kind, source_record_id, reducer_version) WHERE source_record_id IS NOT NULL"
    )
    conn.execute(
        text(
            f"""
            INSERT INTO documents (kind, site_id, source_record_id, record_refs, reducer_version,
                content_hash, chars, est_tokens, storage_key, page_refs, deterministic, stats,
                run_id)
            VALUES (:kind, :site_id, :source_record_id, :record_refs, :reducer_version,
                :content_hash, :chars, :est_tokens, :storage_key, CAST(:page_refs AS jsonb),
                CAST(:deterministic AS jsonb), CAST(:stats AS jsonb), :run_id)
            ON CONFLICT {conflict} DO UPDATE SET
                record_refs = EXCLUDED.record_refs, content_hash = EXCLUDED.content_hash,
                chars = EXCLUDED.chars, est_tokens = EXCLUDED.est_tokens,
                storage_key = EXCLUDED.storage_key, page_refs = EXCLUDED.page_refs,
                deterministic = EXCLUDED.deterministic, stats = EXCLUDED.stats,
                run_id = EXCLUDED.run_id, updated_at = now()
            """  # noqa: S608 - conflict target is one of two constants
        ),
        row,
    )


def reduce_region(engine: Engine, settings: Settings, region: Region) -> dict[str, Any]:
    store = make_store(settings)
    with track_run(engine, "reduce", region.name, {"reducer_version": REDUCER_VERSION}) as run:
        totals: Counter[str] = Counter()
        facts_by_method: Counter[str] = Counter()
        with engine.begin() as conn:
            sites = _site_pages(conn, region.name)
            for site_id, rows in sites.items():
                inputs: list[bundle.PageInput] = []
                facts: list[dict[str, Any]] = []
                for n, row in enumerate(rows, start=1):
                    ref = f"P{n}"
                    html = gzip.decompress(store.get(row.storage_key)).decode("utf-8", "replace")
                    reps = represent(html)
                    for name in ("raw_html", "cleaned_html", "markdown", "text"):
                        content: str = getattr(reps, name)
                        tokens = estimate(content)
                        totals[f"page_tokens:{name}"] += tokens
                        key = f"reduced/{REDUCER_VERSION}/page-{row.page_id}/{name}.txt.gz"
                        if name != "raw_html":  # the raw body is already in the raw store
                            _put(store, key, content)
                        else:
                            key = row.storage_key
                        conn.execute(
                            text(
                                """
                                INSERT INTO page_texts (page_id, representation,
                                    reducer_version, chars, est_tokens, text_hash, storage_key)
                                VALUES (:p, :rep, :v, :chars, :tok, :hash, :key)
                                ON CONFLICT (page_id, representation, reducer_version)
                                DO UPDATE SET chars = EXCLUDED.chars,
                                    est_tokens = EXCLUDED.est_tokens,
                                    text_hash = EXCLUDED.text_hash,
                                    storage_key = EXCLUDED.storage_key
                                """
                            ),
                            {
                                "p": row.page_id,
                                "rep": name,
                                "v": REDUCER_VERSION,
                                "chars": len(content),
                                "tok": tokens,
                                "hash": _sha(content),
                                "key": key,
                            },
                        )
                    totals["pages"] += 1
                    for kind, (total, kept) in fact_retention(
                        reps.cleaned_html, reps.markdown
                    ).items():
                        totals[f"facts_in_page:{kind}"] += total
                        totals[f"facts_kept:{kind}"] += kept
                    inputs.append(bundle.PageInput(ref, row.url, reps.markdown))
                    page_facts = deterministic.as_dicts(deterministic.extract(html, row.url, ref))
                    facts += page_facts
                    facts_by_method.update(f["method"] for f in page_facts)

                site_name = rows[0].seed_url.split("//", 1)[1].split("/", 1)[0]
                b = bundle.build(site_name, inputs, BUNDLE_CAP_TOKENS)
                totals["bundles"] += 1
                totals["bundle_tokens"] += b.tokens
                totals["bundle_tokens_before"] += b.tokens_before
                totals["boilerplate_lines_removed"] += b.boilerplate_lines_removed
                totals["bundles_truncated"] += b.truncated
                key = f"reduced/{REDUCER_VERSION}/site-{site_id}/bundle.md.gz"
                _upsert_document(
                    conn,
                    kind="web_bundle",
                    site_id=site_id,
                    source_record_id=None,
                    record_refs=list(rows[0].record_refs),
                    reducer_version=REDUCER_VERSION,
                    content_hash=_sha(b.text),
                    chars=len(b.text),
                    est_tokens=b.tokens,
                    storage_key=_put(store, key, b.text),
                    page_refs=json.dumps(b.page_refs),
                    deterministic=json.dumps(facts, default=str),
                    stats=json.dumps(
                        {
                            "pages": len(inputs),
                            "tokens_before": b.tokens_before,
                            "boilerplate_lines_removed": b.boilerplate_lines_removed,
                            "truncated": b.truncated,
                            "token_method": METHOD,
                        }
                    ),
                    run_id=run.id,
                )

            ridb_rows = conn.execute(
                text(
                    """
                    SELECT p.source_record_id, p.source || ':' || p.source_id AS ref, s.payload
                    FROM poi_records p JOIN source_records s ON s.id = p.source_record_id
                    JOIN entity_members m ON m.poi_record_id = p.id
                    JOIN entities e ON e.id = m.entity_id AND e.status = 'active'
                    WHERE p.region = :r AND p.source = 'ridb' AND p.is_campground
                    """
                ),
                {"r": region.name},
            ).all()
            for row in ridb_rows:
                doc = ridb.document(row.payload)
                if doc is None:
                    totals["ridb_without_description"] += 1
                    continue
                totals["ridb_documents"] += 1
                totals["ridb_tokens"] += estimate(doc)
                key = f"reduced/{REDUCER_VERSION}/ridb-{row.source_record_id}/description.md.gz"
                _upsert_document(
                    conn,
                    kind="ridb_description",
                    site_id=None,
                    source_record_id=row.source_record_id,
                    record_refs=[row.ref],
                    reducer_version=REDUCER_VERSION,
                    content_hash=_sha(doc),
                    chars=len(doc),
                    est_tokens=estimate(doc),
                    storage_key=_put(store, key, doc),
                    page_refs=json.dumps({"R1": f"ridb:{row.ref.split(':', 1)[1]}"}),
                    deterministic="[]",
                    stats=json.dumps({"token_method": METHOD}),
                    run_id=run.id,
                )

        run.stats.update(totals)
        run.stats["deterministic_facts"] = dict(facts_by_method)
        run.stats["token_method"] = METHOD
        return dict(run.stats)
