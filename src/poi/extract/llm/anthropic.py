"""Claude adapter (primary provider): structured outputs, prompt caching, Batch API.

Tested against API-shaped fixtures through the real SDK (an `httpx2.MockTransport`), so the
tests check exactly what would be sent. Design:
- structured outputs via `output_config.format` with the SDK's `transform_schema` of the
  pydantic model (`additionalProperties: false`); the reply is validated locally after
  checking the stop reason (a refusal is a refusal, not a schema error);
- the static instructions are one system block with a `cache_control` breakpoint, so every
  request after the first reads them from cache (min. cacheable prefix 512 tokens on
  Haiku/Sonnet 5.5; the instructions are longer than that);
- thinking off: Haiku 5.5 accepts `{"type": "disabled"}`; Sonnet 5.5 rejects it and uses
  `{"type": "between_tools"}` (no thinking without tool calls);
- batches: 50% price, 1-hour cache TTL (requests in a batch run far apart in time).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable
from typing import Any, cast

import anthropic
from anthropic import transform_schema
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request
from pydantic import ValidationError

from poi.extract.llm.base import LLMRequest, LLMResponse, Usage

THINKING_OFF: dict[str, dict[str, str]] = {
    "claude-haiku-5-5": {"type": "disabled"},
    "claude-sonnet-5-5": {"type": "between_tools"},
}


def _system(request: LLMRequest, ttl: str | None = None) -> list[dict[str, Any]]:
    cache: dict[str, str] = {"type": "ephemeral"}
    if ttl:
        cache["ttl"] = ttl
    return [{"type": "text", "text": request.system, "cache_control": cache}]


def _usage(usage: Any) -> Usage:
    return Usage(
        input_tokens=usage.input_tokens or 0,
        output_tokens=usage.output_tokens or 0,
        cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
    )


def _outcome(stop_reason: str | None) -> str:
    return {"refusal": "refusal", "max_tokens": "truncated"}.get(stop_reason or "", "ok")


def _from_message(
    provider: str,
    request: LLMRequest,
    message: Any,
    latency_ms: int,
    meta: dict[str, Any] | None = None,
) -> LLMResponse:
    """Shared by sync calls and batch results: stop reason first, then local validation."""
    text = next((b.text for b in message.content if b.type == "text"), "")
    outcome = _outcome(message.stop_reason)
    parsed = None
    error = None
    if outcome == "ok":
        try:
            parsed = request.schema.model_validate(json.loads(text)).model_dump(
                mode="json", exclude_none=True, exclude_defaults=True
            )
        except (json.JSONDecodeError, ValidationError) as exc:
            outcome, error = "invalid_output", str(exc)[:500]
    return LLMResponse(
        provider,
        request.model,
        parsed,
        text,
        _usage(message.usage),
        message.stop_reason or "",
        latency_ms,
        outcome,
        error,
        meta=meta or {},
    )


class AnthropicClient:
    provider = "anthropic"

    def __init__(
        self, client: anthropic.Anthropic, sleep: Callable[[float], None] = time.sleep
    ) -> None:
        self.client = client
        self.sleep = sleep

    def _params(self, request: LLMRequest, ttl: str | None = None) -> dict[str, Any]:
        return {
            "model": request.model,
            "max_tokens": request.max_output_tokens,
            "system": _system(request, ttl),
            "messages": [{"role": "user", "content": request.user}],
            "thinking": THINKING_OFF.get(request.model, {"type": "disabled"}),
        }

    def count_tokens(self, request: LLMRequest) -> int:
        params = self._params(request)
        result = self.client.messages.count_tokens(
            model=params["model"],
            system=params["system"],
            messages=params["messages"],
            output_config={
                "format": {"type": "json_schema", "schema": transform_schema(request.schema)}
            },
        )
        return int(result.input_tokens)

    def _format(self, request: LLMRequest) -> dict[str, Any]:
        return {"format": {"type": "json_schema", "schema": transform_schema(request.schema)}}

    def extract(self, request: LLMRequest) -> LLMResponse:
        """Structured output via `output_config.format` (the same request shape as a batch
        entry), validated locally after checking the stop reason: a refusal must be reported
        as a refusal, not as a schema error."""
        start = time.monotonic()
        try:
            params = self._params(request)
            message = self.client.messages.create(
                model=params["model"],
                max_tokens=params["max_tokens"],
                system=params["system"],
                messages=params["messages"],
                thinking=params["thinking"],
                output_config=cast(Any, self._format(request)),
            )
        except anthropic.APIStatusError as exc:  # the SDK already retried 429/5xx
            return LLMResponse(
                self.provider,
                request.model,
                None,
                "",
                Usage(),
                "error",
                int((time.monotonic() - start) * 1000),
                "error",
                f"{exc.status_code} {exc.message}",
            )
        return _from_message(
            self.provider, request, message, int((time.monotonic() - start) * 1000)
        )

    # -- Message Batches ---------------------------------------------------------------

    def submit_batch(self, requests: Iterable[LLMRequest]) -> str:
        """One batch, 50% price. Structured output is sent as `output_config.format` (the
        `parse` helper is not available for batches); results are validated on collection."""
        batch = self.client.messages.batches.create(
            requests=[
                Request(
                    custom_id=r.key,
                    params=cast(
                        MessageCreateParamsNonStreaming,
                        self._params(r, ttl="1h")
                        | {
                            "output_config": {
                                "format": {
                                    "type": "json_schema",
                                    "schema": transform_schema(r.schema),
                                }
                            }
                        },
                    ),
                )
                for r in requests
            ]
        )
        return str(batch.id)

    def wait_batch(self, batch_id: str, poll_s: float = 30.0, timeout_s: float = 86_400) -> None:
        waited = 0.0
        while self.client.messages.batches.retrieve(batch_id).processing_status != "ended":
            if waited >= timeout_s:
                raise TimeoutError(f"batch {batch_id} not finished after {timeout_s}s")
            self.sleep(poll_s)
            waited += poll_s

    def collect_batch(
        self, batch_id: str, requests: dict[str, LLMRequest]
    ) -> dict[str, LLMResponse]:
        """Results arrive in any order: key them by custom_id, never by position."""
        out: dict[str, LLMResponse] = {}
        for item in self.client.messages.batches.results(batch_id):
            request = requests[item.custom_id]
            if item.result.type != "succeeded":
                out[item.custom_id] = LLMResponse(
                    self.provider,
                    request.model,
                    None,
                    "",
                    Usage(),
                    item.result.type,
                    0,
                    "error",
                    item.result.type,
                    meta={"batch": batch_id},
                )
                continue
            out[item.custom_id] = _from_message(
                self.provider, request, item.result.message, 0, {"batch": batch_id}
            )
        return out
