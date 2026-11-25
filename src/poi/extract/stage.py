"""`poi extract`: run the ladder over reduced documents, record every call, promote results.

Extracted campgrounds become `source_records` of source `web` (crawled official websites) or
`ridb_description` (RIDB facility text): one more source for normalize → match → resolve,
with its own provenance, so its contribution and cost can be reported separately.
"""

from __future__ import annotations

import gzip
import json
import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from zoneinfo import ZoneInfo

from shapely.geometry import Point
from sqlalchemy import Connection, Engine, text

from poi.extract import pricing
from poi.extract.ladder import Models, run_ladder
from poi.extract.llm.base import LLMClient, LLMRequest, LLMResponse, Usage
from poi.extract.llm.replay import Mode, ReplayClient, request_hash
from poi.extract.llm.throttle import DailyQuotaExhaustedError, RateLimiter
from poi.extract.prompt import INSTRUCTIONS, PROMPT_VERSION, wrap_document
from poi.extract.schema import Extraction
from poi.ingest import upsert_source_record
from poi.log import get_logger
from poi.runs import track_run
from poi.settings import PROJECT_ROOT, Region, Settings, load_yaml
from poi.sources.base import SourceRecord
from poi.storage import make_store

log = get_logger(__name__)
RECORDINGS = PROJECT_ROOT / "data" / "cache" / "llm"
SYNTHETIC_RECORDINGS = PROJECT_ROOT / "eval" / "recordings" / "synthetic"
SOURCE_FOR_KIND = {"web_bundle": "web", "ridb_description": "ridb_description"}
PACIFIC = ZoneInfo("America/Los_Angeles")  # Gemini free-tier daily quotas reset at midnight PT
ASSUMED_OUTPUT_TOKENS = 700  # dry-run assumption per document, replaced by measurements
ASSUMED_ESCALATION_SHARE = 0.2


@dataclass(frozen=True)
class Setup:
    client: LLMClient
    provider: str
    models: Models
    max_output_tokens: int
    max_ungrounded_share: float


def calls_today(engine: Engine, provider: str, model: str) -> int:
    midnight = datetime.now(PACIFIC).replace(hour=0, minute=0, second=0, microsecond=0)
    with engine.connect() as conn:
        n: int = conn.execute(
            text(
                "SELECT count(*) FROM llm_calls WHERE provider = :p AND model = :m "
                "AND NOT replayed AND created_at >= :t"
            ),
            {"p": provider, "m": model, "t": midnight.astimezone(UTC)},
        ).scalar_one()
    return n


def build_client(
    engine: Engine, settings: Settings, mode: Mode, provider: str | None = None
) -> Setup:
    cfg = cast(dict[str, Any], load_yaml("llm.yaml", settings))
    provider = provider or cfg["provider"]
    pcfg = cfg[provider]
    models = Models(pcfg["cheap"], pcfg["strong"])
    inner: LLMClient | None = None
    if mode != "replay":
        if provider == "gemini":
            try:
                from google import genai
            except ImportError as exc:
                raise RuntimeError(
                    "the Gemini provider needs the optional extra: uv sync --extra gemini"
                ) from exc

            from poi.extract.llm.gemini import GeminiClient

            if settings.gemini_api_key is None:
                raise RuntimeError("GEMINI_API_KEY is not set in .env")
            # Free-tier quotas: one limiter per run, sized for the cheap model (most calls).
            limits = pcfg["limits"][models.cheap]
            limiter = RateLimiter(
                limits["rpm"],
                limits["tpm"],
                limits["rpd"],
                used_today=calls_today(engine, provider, models.cheap),
            )
            inner = GeminiClient(
                genai.Client(api_key=settings.gemini_api_key.get_secret_value()),
                limiter,
                pcfg["thinking_level"],
            )
        elif provider == "anthropic":
            import anthropic

            from poi.extract.llm.anthropic import AnthropicClient

            if settings.anthropic_api_key is None:
                raise RuntimeError("ANTHROPIC_API_KEY is not set in .env")
            inner = AnthropicClient(
                anthropic.Anthropic(api_key=settings.anthropic_api_key.get_secret_value())
            )
    client = ReplayClient(
        provider, inner, mode, RECORDINGS / provider, read_dirs=(SYNTHETIC_RECORDINGS / provider,)
    )
    return Setup(
        client,
        provider,
        models,
        cfg["max_output_tokens"],
        cfg["escalation"]["max_ungrounded_share"],
    )


def record_call(
    conn: Connection,
    run_id: int | None,
    document_id: int | None,
    purpose: str,
    provider: str,
    step: str | None,
    request: LLMRequest,
    response: LLMResponse,
    is_batch: bool = False,
) -> None:
    billed = (
        0.0
        if response.replayed
        else pricing.billed(
            provider,
            response.model,
            response.usage,
            batch=is_batch,
            cache_ttl="1h" if is_batch else "5m",
        )
    )
    conn.execute(
        text(
            """
            INSERT INTO llm_calls (run_id, document_id, purpose, provider, model, prompt_version,
                request_hash, ladder_step, is_batch, input_tokens, output_tokens,
                cache_read_tokens, cache_write_tokens, thinking_tokens, cost_usd, est_haiku_usd,
                latency_ms, stop_reason, outcome, replayed)
            VALUES (:run, :doc, :purpose, :provider, :model, :pv, :hash, :step, :batch, :inp,
                :out, :cr, :cw, :th, :cost, :haiku, :lat, :stop, :outcome, :replayed)
            """
        ),
        {
            "run": run_id,
            "doc": document_id,
            "purpose": purpose,
            "provider": provider,
            "model": response.model,
            "pv": PROMPT_VERSION,
            "hash": request_hash(provider, request),
            "step": step,
            "batch": is_batch,
            "inp": response.usage.input_tokens,
            "out": response.usage.output_tokens,
            "cr": response.usage.cache_read_tokens,
            "cw": response.usage.cache_write_tokens,
            "th": response.usage.thinking_tokens,
            "cost": billed,
            "haiku": pricing.haiku_equivalent(response.usage),
            "lat": response.latency_ms,
            "stop": response.stop_reason,
            "outcome": response.outcome,
            "replayed": response.replayed,
        },
    )


def pending_documents(
    conn: Connection, provider: str, kind: str | None, limit: int | None
) -> list[Any]:
    """Documents without a current extraction (ladder step 0: unchanged content is skipped)."""
    return list(
        conn.execute(
            text(
                """
                SELECT d.id, d.kind, d.storage_key, d.content_hash, d.deterministic,
                       d.record_refs, d.est_tokens, s.seed_url
                FROM documents d LEFT JOIN sites s ON s.id = d.site_id
                LEFT JOIN extractions x ON x.document_id = d.id
                    AND x.prompt_version = :pv AND x.provider = :provider
                    AND x.content_hash = d.content_hash AND x.status <> 'failed'
                WHERE x.id IS NULL AND (CAST(:kind AS text) IS NULL OR d.kind = :kind)
                ORDER BY d.kind DESC, d.id LIMIT :limit
                """
            ),
            {"pv": PROMPT_VERSION, "provider": provider, "kind": kind, "limit": limit or 1_000_000},
        )
    )


def dry_run(
    engine: Engine, settings: Settings, setup: Setup, kind: str | None, limit: int | None
) -> dict[str, Any]:
    """Free: counts tokens with the provider's count endpoint and prices them. No generation."""
    store = make_store(settings)
    with engine.connect() as conn:
        docs = pending_documents(conn, setup.provider, kind, limit)
    per_kind: Counter[str] = Counter()
    tokens: Counter[str] = Counter()
    for doc in docs:
        body = gzip.decompress(store.get(doc.storage_key)).decode()
        request = LLMRequest(
            setup.models.cheap, INSTRUCTIONS, wrap_document(doc.kind, str(doc.id), body), Extraction
        )
        tokens[doc.kind] += setup.client.count_tokens(request)
        per_kind[doc.kind] += 1
        time.sleep(0.2)  # token counting is free but rate limited too
    n_docs = sum(per_kind.values())
    input_tokens = sum(tokens.values())
    instructions = setup.client.count_tokens(
        LLMRequest(
            setup.models.cheap, INSTRUCTIONS, wrap_document("web_bundle", "0", ""), Extraction
        )
    )  # the cacheable prefix: instructions, schema, empty wrapper
    output_tokens = n_docs * ASSUMED_OUTPUT_TOKENS
    strong_calls = round(n_docs * ASSUMED_ESCALATION_SHARE)
    usage_strong = Usage(
        input_tokens=round(input_tokens * ASSUMED_ESCALATION_SHARE),
        output_tokens=strong_calls * ASSUMED_OUTPUT_TOKENS,
    )
    cfg = cast(dict[str, Any], load_yaml("llm.yaml", settings))
    limits = cfg.get(setup.provider, {}).get("limits", {}).get(setup.models.cheap, {})
    used = calls_today(engine, setup.provider, setup.models.cheap)
    return {
        "provider": setup.provider,
        "models": {"cheap": setup.models.cheap, "strong": setup.models.strong},
        "prompt_version": PROMPT_VERSION,
        "documents": dict(per_kind),
        "input_tokens_counted": dict(tokens),
        "input_tokens_total": input_tokens,
        "output_tokens_assumed": output_tokens,
        "assumptions": f"{ASSUMED_OUTPUT_TOKENS} output tokens per document; "
        f"{ASSUMED_ESCALATION_SHARE:.0%} of documents escalate to the strong model",
        "requests": {"cheap": n_docs, "strong_reserve": strong_calls},
        "rate_limits": limits,
        "requests_used_today": used,
        "min_minutes_at_rpm": round((n_docs + strong_calls) / max(limits.get("rpm", 60), 1), 1),
        "instruction_tokens": instructions,
        "estimate_usd": _estimate(
            setup, input_tokens, output_tokens, n_docs, instructions, usage_strong
        ),
    }


def _estimate(
    setup: Setup,
    input_tokens: int,
    output_tokens: int,
    n_docs: int,
    instructions: int,
    usage_strong: Usage,
) -> dict[str, Any]:
    """List-price estimates for the planned run: sync without and with prompt caching
    (instructions written once, then read), batch with caching, plus the escalation reserve."""
    cheap, strong = setup.models.cheap, setup.models.strong
    uncached = Usage(input_tokens=input_tokens, output_tokens=output_tokens)
    cached = Usage(
        input_tokens=max(input_tokens - n_docs * instructions, 0),
        cache_write_tokens=instructions if n_docs else 0,
        cache_read_tokens=max(n_docs - 1, 0) * instructions,
        output_tokens=output_tokens,
    )
    reserve = pricing.billed(setup.provider, strong, usage_strong)
    out = {
        "sync_no_cache": pricing.billed(setup.provider, cheap, uncached),
        "sync_cached": pricing.billed(setup.provider, cheap, cached),
        "batch_cached": pricing.billed(setup.provider, cheap, cached, batch=True, cache_ttl="1h"),
        "escalation_reserve": reserve,
    }
    out["worst_case_total"] = round(out["sync_no_cache"] + reserve, 6)
    if setup.provider != "anthropic":
        out["claude_haiku_equivalent"] = pricing.haiku_equivalent(uncached)
    return out


def _would_exceed(engine: Engine, run_id: int, setup: Setup, doc: Any, limit: float) -> bool:
    """Hard stop: spent so far in this run + a pessimistic projection for the next document
    (cheap and strong call, no caching) must stay under the limit."""
    with engine.connect() as conn:
        spent = float(
            conn.execute(
                text("SELECT coalesce(sum(cost_usd), 0) FROM llm_calls WHERE run_id = :r"),
                {"r": run_id},
            ).scalar_one()
        )
    projected = Usage(input_tokens=doc.est_tokens + 2_000, output_tokens=ASSUMED_OUTPUT_TOKENS)
    nxt = pricing.billed(setup.provider, setup.models.cheap, projected) + pricing.billed(
        setup.provider, setup.models.strong, projected
    )
    return spent + nxt > limit


def _run_batch(
    engine: Engine, run_id: int, setup: Setup, docs: list[Any], bodies: dict[int, str]
) -> int:
    """Message Batches (50% price, 1-hour cache TTL) for the cheap step. Results are booked in
    the ledger as batch calls and stored as recordings, so the ladder then reads them like any
    other response; escalations run synchronously."""
    from poi.extract.llm.anthropic import AnthropicClient

    client = setup.client
    if not isinstance(client, ReplayClient) or not isinstance(client.inner, AnthropicClient):
        raise RuntimeError("--batch needs the Anthropic provider in record or live mode")
    requests = {
        str(doc.id): LLMRequest(
            setup.models.cheap,
            INSTRUCTIONS,
            wrap_document(doc.kind, str(doc.id), bodies[doc.id]),
            Extraction,
            setup.max_output_tokens,
            key=str(doc.id),
        )
        for doc in docs
    }
    todo = {k: r for k, r in requests.items() if client.mode == "live" or not client.has(r)}
    if not todo:
        return 0
    batch_id = client.inner.submit_batch(todo.values())
    log.info("extract.batch_submitted", batch_id=batch_id, requests=len(todo))
    client.inner.wait_batch(batch_id)
    results = client.inner.collect_batch(batch_id, todo)
    with engine.begin() as conn:
        for key, response in results.items():
            record_call(
                conn,
                run_id,
                int(key),
                "extract",
                setup.provider,
                "cheap",
                todo[key],
                response,
                is_batch=True,
            )
            if response.outcome != "error":
                client.save(todo[key], response)
    return len(results)


def promote(
    conn: Connection,
    region: str,
    run_id: int,
    doc: Any,
    extraction_id: int,
    campgrounds: list[dict[str, Any]],
) -> int:
    """Extracted campgrounds → source_records (one per campground), never deleted."""
    source = SOURCE_FOR_KIND[doc.kind]
    seen = []
    for i, camp in enumerate(campgrounds):
        payload = {k: v for k, v in camp.items() if not k.startswith("_")}
        payload["_provenance"] = {
            "document_id": doc.id,
            "extraction_id": extraction_id,
            "kind": doc.kind,
            "record_refs": list(doc.record_refs),
            "site": doc.seed_url,
            "methods": camp.get("_methods", {}),
            "confidence": camp.get("_confidence", {}),
        }
        if doc.kind == "web_bundle" and not payload.get("website") and doc.seed_url:
            payload["website"] = doc.seed_url
        point = None
        if payload.get("latitude") is not None and payload.get("longitude") is not None:
            point = Point(payload["longitude"], payload["latitude"])
        source_id = f"doc{doc.id}:{i}"
        seen.append(source_id)
        upsert_source_record(
            conn,
            SourceRecord(source, source_id, payload.get("name"), payload, point),
            None,
            region,
            run_id,
        )
    conn.execute(
        text(
            "UPDATE source_records SET missed_runs = missed_runs + 1 WHERE source = :s "
            "AND source_id LIKE :prefix AND NOT (source_id = ANY(:seen))"
        ),
        {"s": source, "prefix": f"doc{doc.id}:%", "seen": seen},
    )
    return len(seen)


def extract_region(
    engine: Engine,
    settings: Settings,
    region: Region,
    *,
    mode: Mode,
    provider: str | None = None,
    kind: str | None = None,
    limit: int | None = None,
    max_cost_usd: float | None = None,
    batch: bool = False,
) -> dict[str, Any]:
    setup = build_client(engine, settings, mode, provider)
    store = make_store(settings)
    params = {
        "provider": setup.provider,
        "mode": mode,
        "kind": kind,
        "limit": limit,
        "prompt_version": PROMPT_VERSION,
        "max_cost_usd": max_cost_usd,
        "batch": batch,
    }
    with track_run(engine, "extract", region.name, params) as run:
        with engine.connect() as conn:
            docs = pending_documents(conn, setup.provider, kind, limit)
        bodies = {doc.id: gzip.decompress(store.get(doc.storage_key)).decode() for doc in docs}
        stats: Counter[str] = Counter()
        if batch:
            stats["batched"] = _run_batch(engine, run.id, setup, docs, bodies)
        for doc in docs:
            body = bodies[doc.id]
            if max_cost_usd is not None and _would_exceed(engine, run.id, setup, doc, max_cost_usd):
                log.warning("extract.max_cost_reached", max_cost_usd=max_cost_usd)
                stats["stopped_max_cost"] += 1
                break
            with engine.begin() as conn:

                def on_call(
                    step: str,
                    request: LLMRequest,
                    response: LLMResponse,
                    conn: Connection = conn,
                    doc_id: int = doc.id,
                ) -> None:
                    record_call(
                        conn, run.id, doc_id, "extract", setup.provider, step, request, response
                    )

                try:
                    result = run_ladder(
                        setup.client,
                        setup.models,
                        doc.kind,
                        str(doc.id),
                        body,
                        list(doc.deterministic),
                        setup.max_output_tokens,
                        setup.max_ungrounded_share,
                        on_call,
                    )
                except DailyQuotaExhaustedError as exc:
                    log.warning("extract.daily_quota", error=str(exc))
                    stats["stopped_daily_quota"] += 1
                    break
                extraction_id = conn.execute(
                    text(
                        """
                        INSERT INTO extractions (document_id, prompt_version, content_hash,
                            provider, model, ladder_step, status, page_kind, output, grounding,
                            hallucination_rate, run_id)
                        VALUES (:doc, :pv, :hash, :provider, :model, :step, :status, :kind,
                            CAST(:output AS jsonb), CAST(:grounding AS jsonb), :hr, :run)
                        ON CONFLICT (document_id, prompt_version, provider) DO UPDATE SET
                            content_hash = EXCLUDED.content_hash, model = EXCLUDED.model,
                            ladder_step = EXCLUDED.ladder_step, status = EXCLUDED.status,
                            page_kind = EXCLUDED.page_kind, output = EXCLUDED.output,
                            grounding = EXCLUDED.grounding,
                            hallucination_rate = EXCLUDED.hallucination_rate,
                            run_id = EXCLUDED.run_id, created_at = now()
                        RETURNING id
                        """
                    ),
                    {
                        "doc": doc.id,
                        "pv": PROMPT_VERSION,
                        "hash": doc.content_hash,
                        "provider": setup.provider,
                        "model": result.model,
                        "step": result.step,
                        "status": result.status,
                        "kind": result.page_kind,
                        "output": json.dumps(result.campgrounds),
                        "grounding": json.dumps(result.grounding),
                        "hr": result.hallucination_rate,
                        "run": run.id,
                    },
                ).scalar_one()
                stats[f"status:{result.status}"] += 1
                stats[f"step:{result.step}"] += 1
                stats[f"kind:{doc.kind}"] += 1
                if result.status != "failed":
                    stats["campgrounds"] += promote(
                        conn, region.name, run.id, doc, extraction_id, result.campgrounds
                    )
        with engine.connect() as conn:
            totals = conn.execute(
                text(
                    "SELECT count(*) AS calls, count(*) FILTER (WHERE replayed) AS replayed, "
                    "coalesce(sum(input_tokens + cache_read_tokens), 0) AS inp, "
                    "coalesce(sum(output_tokens), 0) AS out, "
                    "coalesce(sum(thinking_tokens), 0) AS think, "
                    "coalesce(sum(cost_usd), 0) AS billed, "
                    "coalesce(sum(est_haiku_usd), 0) AS haiku FROM llm_calls WHERE run_id = :r"
                ),
                {"r": run.id},
            ).one()
        run.stats.update(stats)
        run.stats.update(
            calls=totals.calls,
            replayed=totals.replayed,
            input_tokens=totals.inp,
            output_tokens=totals.out,
            thinking_tokens=totals.think,
            billed_usd=float(totals.billed),
            est_haiku_usd=float(totals.haiku),
        )
        return dict(run.stats)
