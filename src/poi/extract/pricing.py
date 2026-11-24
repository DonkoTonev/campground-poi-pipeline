"""Cost of a call from its token usage and `config/pricing.yaml`.

`billed` is the list price of the call for its provider (0 for a free tier).
`haiku_equivalent` prices the same token counts on Claude Haiku 5.5 without caching: for Claude
calls it is a no-cache reference point; for other providers it is an estimate (another
tokenizer counted the tokens).
"""

from __future__ import annotations

from typing import Any, cast

from poi.extract.llm.base import Usage
from poi.settings import load_yaml

HAIKU = "claude-haiku-5-5"


def _rates(provider: str, model: str, cfg: dict[str, Any]) -> dict[str, float] | None:
    if provider == "anthropic":
        rates: dict[str, float] | None = cfg["anthropic"]["models"].get(model)
        return rates
    if provider == "gemini" and cfg["gemini"]["tier"] != "free":
        paid: dict[str, float] | None = cfg["gemini"]["paid_reference"].get(model)
        return paid
    return None


def billed(
    provider: str,
    model: str,
    usage: Usage,
    batch: bool = False,
    cache_ttl: str = "5m",
    cfg: dict[str, Any] | None = None,
) -> float:
    cfg = cfg if cfg is not None else cast(dict[str, Any], load_yaml("pricing.yaml"))
    rates = _rates(provider, model, cfg)
    if rates is None:
        return 0.0
    prompt = usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens
    long = rates.get("long_context_tokens")
    mult = rates.get("long_context_multiplier", 1) if long and prompt > long else 1
    cost = (
        usage.input_tokens * rates["input"] * mult
        + (usage.output_tokens + usage.thinking_tokens) * rates["output"] * mult
        + usage.cache_read_tokens * rates.get("cache_read", rates["input"])
        + usage.cache_write_tokens * rates.get(f"cache_write_{cache_ttl}", rates["input"])
    ) / 1_000_000
    if batch and provider == "anthropic":
        cost *= cfg["anthropic"]["batch_discount"]
    return round(cost, 8)


def haiku_equivalent(usage: Usage, cfg: dict[str, Any] | None = None) -> float:
    """Same token counts priced on Haiku 5.5 without caching (labelled as an estimate)."""
    as_uncached = Usage(
        input_tokens=usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens,
        output_tokens=usage.output_tokens + usage.thinking_tokens,
    )
    return billed("anthropic", HAIKU, as_uncached, cfg=cfg)
