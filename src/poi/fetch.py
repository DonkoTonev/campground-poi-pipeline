"""Fetch once, process many times.

Every external response is gzipped into the raw store and registered in `raw_objects`.
A later request with the same `request_key` is served from the store unless `refresh=True`,
so parsers can be changed and re-run without touching the network.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import Engine, text

from poi import http
from poi.log import get_logger
from poi.storage import RawStore

log = get_logger(__name__)


@dataclass
class FetchContext:
    engine: Engine
    store: RawStore
    client: httpx.Client
    run_id: int
    refresh: bool = False
    stats: dict[str, int] = field(default_factory=lambda: {"requests": 0, "cache_hits": 0})


@dataclass(frozen=True)
class RawResponse:
    raw_object_id: int
    body: bytes
    from_cache: bool


def request_key(source: str, method: str, url: str, params: Mapping[str, Any] | None) -> str:
    """Stable identity of a logical request. Secrets must never be part of `params`."""
    canon = json.dumps(
        {"source": source, "method": method.upper(), "url": url, "params": params or {}},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canon.encode()).hexdigest()


def fetch(
    ctx: FetchContext,
    source: str,
    method: str,
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    secret_headers: Mapping[str, str] | None = None,
) -> RawResponse:
    """Return the response body for a request, from the raw store when already fetched.

    `params` go into the request key and the `raw_objects.request` column; `secret_headers`
    (API keys) are sent but never stored. GET sends params as a query string, POST as a form.
    """
    key = request_key(source, method, url, params)
    if not ctx.refresh:
        with ctx.engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT id, storage_key FROM raw_objects WHERE request_key = :k "
                    "AND http_status = 200 ORDER BY fetched_at DESC LIMIT 1"
                ),
                {"k": key},
            ).first()
        if row is not None:
            ctx.stats["cache_hits"] += 1
            return RawResponse(row.id, gzip.decompress(ctx.store.get(row.storage_key)), True)

    is_get = method.upper() == "GET"
    resp = http.request(
        ctx.client,
        method,
        url,
        params=params if is_get else None,
        data=None if is_get else params,
        headers=secret_headers,
    )
    ctx.stats["requests"] += 1
    body = resp.content
    raw_id = store_raw(
        ctx.engine,
        ctx.store,
        source=source,
        request_key_=key,
        request={"method": method.upper(), "url": url, "params": params},
        body=body,
        status=resp.status_code,
        headers={k.lower(): v for k, v in resp.headers.items()},
        run_id=ctx.run_id,
        suffix="json",
    )
    log.info("fetch.stored", source=source, url=url, bytes=len(body), raw_object_id=raw_id)
    return RawResponse(raw_id, body, False)


def store_raw(
    engine: Engine,
    store: RawStore,
    *,
    source: str,
    request_key_: str,
    request: dict[str, Any],
    body: bytes,
    status: int,
    headers: Mapping[str, str],
    run_id: int,
    suffix: str,
) -> int:
    """Gzip a response body into the raw store and register it; returns the raw object id.
    Keyed by request hash + fetch time, so re-fetches never overwrite earlier versions."""
    fetched_at = datetime.now(UTC)
    storage_key = (
        f"{source}/{fetched_at:%Y/%m/%d}/{request_key_[:16]}-{fetched_at:%H%M%S%f}.{suffix}.gz"
    )
    store.put(storage_key, gzip.compress(body))
    with engine.begin() as conn:
        raw_id: int = conn.execute(
            text(
                """
                INSERT INTO raw_objects (source, request_key, request, storage_key, sha256, bytes,
                    content_type, http_status, etag, last_modified, fetched_at, run_id)
                VALUES (:source, :key, CAST(:request AS jsonb), :storage_key, :sha, :bytes,
                    :ctype, :status, :etag, :lm, :fetched_at, :run_id)
                RETURNING id
                """
            ),
            {
                "source": source,
                "key": request_key_,
                "request": json.dumps(request),
                "storage_key": storage_key,
                "sha": hashlib.sha256(body).hexdigest(),
                "bytes": len(body),
                "ctype": headers.get("content-type"),
                "status": status,
                "etag": headers.get("etag"),
                "lm": headers.get("last-modified"),
                "fetched_at": fetched_at,
                "run_id": run_id,
            },
        ).scalar_one()
    return raw_id
