"""`poi crawl`: screen seeds, crawl official sites with Scrapy, record every outcome."""

from __future__ import annotations

from collections import Counter
from typing import Any

from scrapy.crawler import CrawlerProcess
from sqlalchemy import Engine, text

from poi.crawl.seeds import upsert_sites
from poi.crawl.spider import Recorder, SiteSpider, SiteTask
from poi.crawl.urls import Scope
from poi.runs import track_run
from poi.settings import PROJECT_ROOT, Region, Settings, load_yaml
from poi.storage import make_store

RETRY_HTTP_CODES = [500, 502, 504, 522, 524, 408]  # 429/503 go through RetryAfterMiddleware


def scrapy_settings(
    settings: Settings, cfg: dict[str, Any], render: bool, http_cache: bool
) -> dict[str, Any]:
    polite = cfg["politeness"]
    out: dict[str, Any] = {
        "BOT_NAME": "campground-poi-pipeline",
        "USER_AGENT": settings.user_agent,
        "ROBOTSTXT_OBEY": True,
        "CONCURRENT_REQUESTS": 8,
        "CONCURRENT_REQUESTS_PER_DOMAIN": polite["concurrent_per_domain"],
        "DOWNLOAD_DELAY": polite["download_delay_s"],
        "AUTOTHROTTLE_ENABLED": True,
        "AUTOTHROTTLE_START_DELAY": polite["download_delay_s"],
        "AUTOTHROTTLE_TARGET_CONCURRENCY": 1.0,
        "DOWNLOAD_TIMEOUT": polite["timeout_s"],
        "DOWNLOAD_MAXSIZE": polite["max_bytes"],
        "DOWNLOAD_WARNSIZE": polite["max_bytes"] // 2,
        "RETRY_TIMES": polite["retry_times"],
        "RETRY_HTTP_CODES": RETRY_HTTP_CODES,
        "POI_RETRY_AFTER_ATTEMPTS": polite["retry_times"],
        "POI_MAX_RETRY_AFTER_S": polite["max_retry_after_s"],
        "DOWNLOADER_MIDDLEWARES": {
            "poi.crawl.middleware.BlockedHostMiddleware": 950,
            "poi.crawl.middleware.RetryAfterMiddleware": 560,
        },
        "TELNETCONSOLE_ENABLED": False,
        "LOG_LEVEL": "INFO",
    }
    if http_cache:  # development only: never hits the sites twice while iterating on parsers
        out |= {
            "HTTPCACHE_ENABLED": True,
            "HTTPCACHE_DIR": str(PROJECT_ROOT / "data" / "cache" / "http"),
        }
    if render:
        handler = "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler"
        out |= {
            "DOWNLOAD_HANDLERS": {"http": handler, "https": handler},
            "PLAYWRIGHT_BROWSER_TYPE": "chromium",
            "PLAYWRIGHT_LAUNCH_OPTIONS": {"headless": True},
            # Only the document and its scripts: no images, fonts, media or stylesheets.
            "PLAYWRIGHT_ABORT_REQUEST": "poi.crawl.stage.abort_heavy_request",
        }
    return out


def abort_heavy_request(request: Any) -> bool:
    return request.resource_type in ("image", "media", "font", "stylesheet")


def _tasks(engine: Engine, region: str, max_sites: int | None) -> list[SiteTask]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT id, seed_url, scope FROM sites WHERE region = :r AND status <> 'skipped' "
                "ORDER BY id LIMIT :n"
            ),
            {"r": region, "n": max_sites or 1_000_000},
        ).all()
    return [
        SiteTask(
            r.id,
            r.seed_url,
            Scope(
                r.scope["domain"], r.scope["path_prefix"], tuple(tuple(q) for q in r.scope["query"])
            ),
        )
        for r in rows
    ]


SITE_STATUS_SQL = text(
    """
    WITH seed AS (
        SELECT s.id, p.outcome, p.flags
        FROM sites s JOIN pages p ON p.site_id = s.id AND p.depth = 0 AND p.url = s.seed_url
        WHERE s.region = :r
    ), agg AS (
        SELECT site_id,
               count(*) FILTER (WHERE outcome IN ('fetched', 'unchanged')) AS ok_pages,
               bool_or(outcome = 'blocked') AS blocked
        FROM pages GROUP BY site_id
    )
    UPDATE sites s SET
        pages_fetched = coalesce(agg.ok_pages, 0),
        last_run_id = :run,
        updated_at = now(),
        status = CASE
            WHEN agg.blocked OR seed.outcome = 'domain_blocked' THEN 'blocked'
            WHEN seed.outcome = 'robots_blocked' THEN 'robots_blocked'
            WHEN 'parked' = ANY(seed.flags) THEN 'parked'
            WHEN seed.outcome = 'off_domain_redirect' THEN 'off_domain_redirect'
            WHEN 'soft_404' = ANY(seed.flags) THEN 'soft_404'
            WHEN seed.outcome IN ('dns_error', 'timeout', 'error', 'http_404', 'http_410')
                THEN 'dead'
            WHEN coalesce(agg.ok_pages, 0) > 0 THEN 'ok'
            ELSE coalesce(seed.outcome, 'not_fetched') END
    FROM seed LEFT JOIN agg ON agg.site_id = seed.id
    WHERE s.id = seed.id AND s.region = :r AND s.status <> 'skipped'
    """
)


def crawl_region(
    engine: Engine,
    settings: Settings,
    region: Region,
    *,
    plan_only: bool = False,
    render: bool = False,
    max_sites: int | None = None,
    extra_seeds: list[str] | None = None,
    allow_private: bool = False,
    http_cache: bool = False,
) -> dict[str, Any]:
    cfg = load_yaml("crawl.yaml", settings)
    params = {"plan_only": plan_only, "render": render, "max_sites": max_sites}
    with track_run(engine, "crawl", region.name, params) as run:
        with engine.begin() as conn:
            seeds = upsert_sites(conn, region.name, cfg, extra_seeds, allow_private)
        run.stats["seeds"] = dict(seeds)
        if plan_only:
            return dict(run.stats)
        tasks = _tasks(engine, region.name, max_sites)
        recorder = Recorder(engine, make_store(settings), run.id)
        process = CrawlerProcess(
            scrapy_settings(settings, cfg, render, http_cache), install_root_handler=False
        )
        process.crawl(SiteSpider, sites=tasks, cfg=cfg, recorder=recorder, render=render)
        process.start()
        with engine.begin() as conn:
            conn.execute(SITE_STATUS_SQL, {"r": region.name, "run": run.id})
            site_rows = conn.execute(
                text("SELECT status, count(*) AS n FROM sites WHERE region = :r GROUP BY 1"),
                {"r": region.name},
            ).all()
            pages = conn.execute(
                text(
                    "SELECT outcome, render_mode, count(*) AS n, "
                    "count(*) FILTER (WHERE content_type LIKE 'text/html%') AS html "
                    "FROM pages WHERE run_id = :run GROUP BY 1, 2"
                ),
                {"run": run.id},
            ).all()
        outcomes: Counter[str] = Counter()
        for p in pages:
            outcomes[p.outcome] += p.n
        html_pages = sum(p.html for p in pages)
        rendered = sum(p.n for p in pages if p.render_mode == "playwright")
        run.stats.update(
            sites_crawled=len(tasks),
            site_status={r.status: r.n for r in site_rows},
            page_outcomes=dict(outcomes),
            rendered_pages=rendered,
            render_rate=round(rendered / html_pages, 3) if html_pages else 0.0,
        )
        return dict(run.stats)
