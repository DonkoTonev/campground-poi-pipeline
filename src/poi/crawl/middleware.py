"""Downloader middlewares: stop blocked hosts; honour Retry-After with capped backoff."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any
from urllib.parse import urlsplit

from scrapy import Request, Spider
from scrapy.exceptions import IgnoreRequest
from scrapy.http import Response

from poi.crawl.detect import BLOCKED
from poi.http import parse_retry_after

log = logging.getLogger(__name__)
RETRY_STATUSES = (429, 503)
BLOCK_STATUSES = (403, 429, 503)


class RetryAfterMiddleware:
    """Scrapy's RetryMiddleware ignores Retry-After; this one waits as the server asked.

    A Retry-After above `max_wait_s` means "come back much later": the page is given up
    (recorded with its status) instead of stalling the crawl. Without the header, the wait is
    exponential (base_s * 2^attempt). Waiting happens per request; with one concurrent request
    per domain that is the polite behaviour.
    """

    def __init__(self, max_attempts: int, max_wait_s: float, base_s: float = 2.0) -> None:
        self.max_attempts = max_attempts
        self.max_wait_s = max_wait_s
        self.base_s = base_s

    @classmethod
    def from_crawler(cls, crawler: Any) -> RetryAfterMiddleware:
        s = crawler.settings
        return cls(
            s.getint("POI_RETRY_AFTER_ATTEMPTS", 3),
            s.getfloat("POI_MAX_RETRY_AFTER_S", 120.0),
            s.getfloat("POI_RETRY_BASE_S", 2.0),
        )

    async def process_response(
        self, request: Request, response: Response, spider: Spider
    ) -> Request | Response:
        if response.status not in RETRY_STATUSES:
            return response
        attempt = int(request.meta.get("retry_after_attempt", 0))
        header = response.headers.get(b"Retry-After")
        wait = parse_retry_after(header.decode("latin-1") if header else None)
        if wait is None:
            wait = self.base_s * 2**attempt
        if attempt >= self.max_attempts or wait > self.max_wait_s:
            return response  # recorded by the spider with its status
        log.info("retry-after: waiting %.1fs for %s (attempt %d)", wait, request.url, attempt + 1)
        await asyncio.sleep(wait)
        retry = request.replace(dont_filter=True)
        retry.meta["retry_after_attempt"] = attempt + 1
        return retry


class BlockedHostMiddleware:
    """Once a host answers with a captcha or block page, no further request goes to it.

    A per-host lock is held from `process_request` until that request's response (or error)
    comes back, so the next request to the host is checked only after the previous answer has
    been seen. Without it, requests queued in Scrapy's per-domain slot pass `process_request`
    before the block page arrives and still go out. Ordered late (950) so its
    `process_response` runs first and always releases the lock, even when a later middleware
    turns the response into a retry.
    """

    def __init__(self) -> None:
        self.locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def process_request(self, request: Request, spider: Spider) -> None:
        host = urlsplit(request.url).hostname or ""
        await self.locks[host].acquire()
        request.meta["_host_lock"] = host
        if host in getattr(spider, "blocked_hosts", set()):
            self._release(request)
            raise IgnoreRequest(f"domain blocked: {host}")

    def process_response(self, request: Request, response: Response, spider: Spider) -> Response:
        if response.status in BLOCK_STATUSES and BLOCKED.search(
            response.body[:20000].decode("utf-8", "replace")
        ):
            blocked: set[str] = spider.blocked_hosts  # type: ignore[attr-defined]
            blocked.add(urlsplit(response.url).hostname or "")
        self._release(request)
        return response

    def process_exception(self, request: Request, exception: Exception, spider: Spider) -> None:
        self._release(request)

    def _release(self, request: Request) -> None:
        host = request.meta.pop("_host_lock", None)
        if host is not None and self.locks[host].locked():
            self.locks[host].release()
