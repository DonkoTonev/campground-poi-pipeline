"""Scrapy spider for official campground websites.

One request per domain at a time, robots.txt obeyed, honest User-Agent (settings). Each site
gets a page budget spent on its most relevant links; redirects are followed only inside the
site's scope (an off-domain redirect is recorded, and a booking engine becomes an attribute);
a blocked or captcha page stops the whole site. Every attempt is recorded in `pages`.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import scrapy
from scrapy.exceptions import IgnoreRequest
from scrapy.http import Request, Response
from sqlalchemy import Engine, text
from twisted.python.failure import Failure

from poi.crawl.detect import analyse
from poi.crawl.urls import (
    Scope,
    booking_platform,
    link_relevance,
    registrable_domain,
    request_url,
    scope_for,
)
from poi.fetch import store_raw
from poi.normalize.contact import canonical_url
from poi.storage import RawStore

MAX_REDIRECT_HOPS = 5
HTML_TYPES = ("text/html", "application/xhtml")


@dataclass(frozen=True)
class SiteTask:
    id: int
    seed: str
    scope: Scope


@dataclass(frozen=True)
class Previous:
    etag: str | None
    last_modified: str | None
    content_hash: str | None


class Recorder:
    """Writes pages (and raw bodies) as they arrive. Small volumes: synchronous is fine."""

    def __init__(self, engine: Engine, store: RawStore, run_id: int) -> None:
        self.engine, self.store, self.run_id = engine, store, run_id
        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT site_id, url, etag, last_modified, content_hash FROM pages")
            )
            self.previous = {
                (r.site_id, r.url): Previous(r.etag, r.last_modified, r.content_hash) for r in rows
            }

    def save(self, site_id: int, url: str, **fields: Any) -> None:
        body: bytes | None = fields.pop("body", None)
        headers: dict[str, str] = fields.pop("headers", {})
        raw_id = None
        if body is not None:
            raw_id = store_raw(
                self.engine,
                self.store,
                source="web",
                request_key_=hashlib.sha256(url.encode()).hexdigest(),
                request={"method": "GET", "url": url},
                body=body,
                status=fields.get("http_status") or 0,
                headers=headers,
                run_id=self.run_id,
                suffix="html" if "html" in (fields.get("content_type") or "") else "bin",
            )
        row = {
            "site_id": site_id,
            "url": url,
            "final_url": None,
            "depth": 0,
            "relevance": None,
            "http_status": None,
            "content_type": None,
            "content_hash": None,
            "bytes": None,
            "render_mode": "static",
            "raw_object_id": raw_id,
            "etag": headers.get("etag"),
            "last_modified": headers.get("last-modified"),
            "outcome": "error",
            "flags": [],
            "text_chars": None,
            "title": None,
            "error": None,
            "run_id": self.run_id,
        } | fields
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO pages (site_id, url, final_url, depth, relevance, http_status,
                        content_type, content_hash, bytes, render_mode, raw_object_id, etag,
                        last_modified, outcome, flags, text_chars, title, error, run_id)
                    VALUES (:site_id, :url, :final_url, :depth, :relevance, :http_status,
                        :content_type, :content_hash, :bytes, :render_mode, :raw_object_id,
                        :etag, :last_modified, :outcome, :flags, :text_chars, :title, :error,
                        :run_id)
                    ON CONFLICT (site_id, url) DO UPDATE SET
                        final_url = EXCLUDED.final_url, depth = EXCLUDED.depth,
                        relevance = EXCLUDED.relevance, http_status = EXCLUDED.http_status,
                        content_type = EXCLUDED.content_type,
                        content_hash = coalesce(EXCLUDED.content_hash, pages.content_hash),
                        bytes = coalesce(EXCLUDED.bytes, pages.bytes),
                        render_mode = EXCLUDED.render_mode,
                        raw_object_id = coalesce(EXCLUDED.raw_object_id, pages.raw_object_id),
                        etag = coalesce(EXCLUDED.etag, pages.etag),
                        last_modified = coalesce(EXCLUDED.last_modified, pages.last_modified),
                        outcome = EXCLUDED.outcome, flags = EXCLUDED.flags,
                        text_chars = coalesce(EXCLUDED.text_chars, pages.text_chars),
                        title = coalesce(EXCLUDED.title, pages.title), error = EXCLUDED.error,
                        run_id = EXCLUDED.run_id, fetched_at = now()
                    """
                ),
                row,
            )

    def set_booking(self, site_id: int, platform: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text("UPDATE sites SET booking_platform = :p WHERE id = :id"),
                {"p": platform, "id": site_id},
            )


class SiteSpider(scrapy.Spider):
    name = "official_sites"

    def __init__(
        self, sites: list[SiteTask], cfg: dict[str, Any], recorder: Recorder, render: bool
    ) -> None:
        super().__init__()
        self.sites = {s.id: s for s in sites}
        self.cfg = cfg
        self.recorder = recorder
        self.render = render
        self.blocked: set[int] = set()
        self.blocked_hosts: set[str] = set()  # read by BlockedHostMiddleware
        # A seed that moved within its own domain moves the site's scope with it.
        self.scopes: dict[int, Scope] = {s.id: s.scope for s in sites}
        self.scheduled: dict[int, int] = defaultdict(int)
        self.seen: dict[int, set[str]] = defaultdict(set)

    # -- requests --------------------------------------------------------------------------

    def _request(
        self,
        site: SiteTask,
        url: str,
        depth: int,
        relevance: float | None,
        hops: int = 0,
        render: bool = False,
        fetch: str | None = None,
    ) -> Request:
        """`url` is the page identity (canonical); `fetch` the URL actually requested."""
        headers = {}
        prev = self.recorder.previous.get((site.id, url))
        if prev and not render:  # conditional re-crawl: unchanged pages cost one 304
            if prev.etag:
                headers["If-None-Match"] = prev.etag
            if prev.last_modified:
                headers["If-Modified-Since"] = prev.last_modified
        meta: dict[str, Any] = {
            "site_id": site.id,
            "url": url,
            # Not "depth": Scrapy's DepthMiddleware owns that key and increments it for every
            # request yielded from a callback, including a followed redirect.
            "poi_depth": depth,
            "relevance": relevance,
            "hops": hops,
            "dont_redirect": True,
            "handle_httpstatus_all": True,
        }
        if render:
            meta["playwright"] = True
        return Request(
            fetch or url,
            callback=self.parse,
            errback=self.on_error,
            headers=headers,
            meta=meta,
            priority=int((relevance or 0) * 10),
            dont_filter=render or hops > 0,
        )

    async def start(self) -> AsyncIterator[Request]:
        # Scrapy consumes this lazily, so seeds on a host that blocked us in the meantime are
        # recorded without ever being scheduled (the middleware catches anything in flight).
        for site in self.sites.values():
            host = urlsplit(site.seed).hostname or ""
            if host in self.blocked_hosts:
                self.recorder.save(
                    site.id,
                    site.seed,
                    depth=0,
                    outcome="domain_blocked",
                    error=f"domain blocked: {host}",
                )
                continue
            self.scheduled[site.id] += 1
            self.seen[site.id].add(site.seed)
            yield self._request(site, site.seed, 0, None)

    # -- responses -------------------------------------------------------------------------

    def parse(self, response: Response) -> Iterator[Request]:
        meta = response.meta
        site = self.sites[meta["site_id"]]
        if site.id in self.blocked:
            return
        url, depth, status = meta["url"], meta["poi_depth"], response.status
        headers = {k.decode().lower(): v[0].decode("latin-1") for k, v in response.headers.items()}
        ctype = headers.get("content-type", "")
        base = {
            "depth": depth,
            "relevance": meta["relevance"],
            "http_status": status,
            "content_type": ctype,
            "final_url": response.url,
            "render_mode": "playwright" if meta.get("playwright") else "static",
        }

        if 300 <= status < 400 and "location" in headers:
            actual = request_url(response.urljoin(headers["location"]))
            target = canonical_url(actual) if actual else None
            scope = self.scopes[site.id]
            same_page = target == url  # www, scheme or trailing-slash variants
            moved_seed = (
                depth == 0 and target is not None and registrable_domain(target) == scope.domain
            )
            follow = same_page or moved_seed or (target is not None and scope.allows(target))
            if actual and follow and meta["hops"] < MAX_REDIRECT_HOPS:
                if moved_seed and not same_page and not scope.allows(target or ""):
                    self.scopes[site.id] = scope_for(target or actual, self.cfg)
                yield self._request(
                    site, url, depth, meta["relevance"], meta["hops"] + 1, fetch=actual
                )
                return
            platform = booking_platform(target or "", self.cfg)
            if platform:
                self.recorder.set_booking(site.id, platform)
            self.recorder.save(
                site.id,
                url,
                **(base | {"final_url": target}),
                outcome="off_domain_redirect",
                flags=["off_domain_redirect"] + ([f"booking:{platform}"] if platform else []),
            )
            return
        if status == 304:
            self.recorder.save(site.id, url, **base, outcome="unchanged")
            return

        body = response.body
        digest = hashlib.sha256(body).hexdigest()
        prev = self.recorder.previous.get((site.id, url))
        if not ctype.startswith(HTML_TYPES):
            self.recorder.save(
                site.id,
                url,
                **base,
                outcome=f"http_{status}" if status != 200 else "fetched",
                flags=["non_html"],
                body=body,
                headers=headers,
                content_hash=digest,
                bytes=len(body),
            )
            return

        signals = analyse(response.text, status, self.cfg["render"]["min_static_text_chars"])
        flags = list(signals.flags)
        if "blocked" in flags:
            # Stop the whole domain: never try to get around a block.
            self.blocked.add(site.id)
            self.blocked_hosts.add(urlsplit(response.url).hostname or "")
            self.recorder.save(
                site.id,
                url,
                **base,
                outcome="blocked",
                flags=flags,
                title=signals.title,
                text_chars=signals.text_chars,
            )
            return
        unchanged = prev is not None and prev.content_hash == digest and status == 200
        self.recorder.save(
            site.id,
            url,
            **base,
            outcome="unchanged"
            if unchanged
            else ("fetched" if status == 200 else f"http_{status}"),
            flags=flags,
            title=signals.title,
            text_chars=signals.text_chars,
            body=None if unchanged else body,
            headers=headers,
            content_hash=digest,
            bytes=len(body),
        )
        if "needs_render" in flags and self.render and not meta.get("playwright"):
            yield self._request(
                site, url, depth, meta["relevance"], render=True, fetch=response.url
            )
        if status != 200 or depth >= self.cfg["scope"]["max_depth"]:
            return
        if {"parked", "soft_404"} & set(flags):
            return
        yield from self._follow(site, response, signals.links, depth)

    def _follow(
        self, site: SiteTask, response: Response, links: list[tuple[str, str]], depth: int
    ) -> Iterator[Request]:
        budget = self.cfg["scope"]["max_pages_per_site"] - self.scheduled[site.id]
        if budget <= 0:
            return
        scored: dict[str, tuple[float, str]] = {}
        for href, anchor in links:
            if href.startswith(("mailto:", "tel:", "javascript:", "#")):
                continue
            fetch = request_url(response.urljoin(href))
            target = canonical_url(fetch) if fetch else None
            if not fetch or not target or target in self.seen[site.id]:
                continue
            if not self.scopes[site.id].allows(target):
                continue
            score = link_relevance(target, anchor, self.cfg)
            if score is not None and score >= scored.get(target, (score, fetch))[0]:
                scored[target] = (score, fetch)
        ranked = sorted(scored.items(), key=lambda kv: (-kv[1][0], kv[0]))[:budget]
        for target, (score, fetch) in ranked:
            self.seen[site.id].add(target)
            self.scheduled[site.id] += 1
            yield self._request(site, target, depth + 1, score, fetch=fetch)

    def on_error(self, failure: Failure) -> None:
        request = failure.request  # type: ignore[attr-defined]
        meta = request.meta
        site_id, url = meta["site_id"], meta["url"]
        message = str(failure.value)
        if isinstance(failure.value, IgnoreRequest) and "robots.txt" in message:
            outcome = "robots_blocked"
        elif isinstance(failure.value, IgnoreRequest) and "domain blocked" in message:
            outcome = "domain_blocked"
        elif "DNSLookupError" in type(failure.value).__name__ or "DNS lookup" in message:
            outcome = "dns_error"
        elif "TimeoutError" in type(failure.value).__name__:
            outcome = "timeout"
        elif "maxsize" in message.lower() or "larger than" in message.lower():
            outcome = "too_large"
        else:
            outcome = "error"
        self.recorder.save(
            site_id,
            url,
            depth=meta["poi_depth"],
            relevance=meta["relevance"],
            outcome=outcome,
            error=message[:500],
        )
