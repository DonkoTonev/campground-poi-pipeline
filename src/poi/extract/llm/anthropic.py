"""Claude adapter (primary provider): structured outputs, prompt caching, Batch API.

Tested against API-shaped fixtures through the real SDK (an `httpx2.MockTransport`), so the
tests check exactly what would be sent. Design:
- the output schema travels in the system prompt, not as `output_config.format`: constrained
  decoding allows at most 24 optional and 16 union-typed parameters per request, and the
  extraction contract has about 60 optional fields (the API answers "compiled grammar is too
  large"). Making every field required would make the model write all of them for every
  campground, and output tokens cost 5x input. The reply is validated locally against the
  same pydantic model after checking the stop reason; an invalid reply is an
  `invalid_output` outcome, which the ladder escalates;
- instructions plus schema are one system block with a `cache_control` breakpoint, so every
  request after the first reads them from cache;
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
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request
from pydantic import ValidationError

from poi.extract.llm.base import LLMRequest, LLMResponse, Usage

THINKING_OFF: dict[str, dict[str, str]] = {
    "claude-haiku-5-5": {"type": "disabled"},
    "claude-sonnet-5-5": {"type": "between_tools"},
}


def _lean(node: Any) -> Any:
    """The schema without `null` branches (unknown fields are omitted, never null) and
    without titles: fewer tokens, same contract (validation stays local and complete)."""
    if isinstance(node, dict):
        node = {k: _lean(v) for k, v in node.items() if k != "title"}
        if "anyOf" in node:
            branches = [b for b in node["anyOf"] if b != {"type": "null"}]
            if len(branches) == 1:
                rest = {k: v for k, v in node.items() if k != "anyOf"}
                return {**branches[0], **rest}
            node["anyOf"] = branches
        return node
    if isinstance(node, list):
        return [_lean(v) for v in node]
    return node


def schema_text(schema: type[Any]) -> str:
    return (
        "Reply with exactly one JSON object and nothing else: no prose, no Markdown fences. "
        "It must validate against this JSON Schema. Leave out every field the document does "
        "not state (never write null):\n"
        + json.dumps(_lean(schema.model_json_schema()), separators=(",", ":"), sort_keys=True)
    )


def _system(request: LLMRequest, ttl: str | None = None) -> list[dict[str, Any]]:
    cache: dict[str, str] = {"type": "ephemeral"}
    if ttl:
        cache["ttl"] = ttl
    text = f"{request.system}\n\n{schema_text(request.schema)}"
    return [{"type": "text", "text": text, "cache_control": cache}]


def _json_text(text: str) -> str:
    """Tolerate a Markdown fence around the object (the prompt asks for none)."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        stripped = stripped.rsplit("```", 1)[0]
    return stripped.strip()


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
            parsed = request.schema.model_validate(json.loads(_json_text(text))).model_dump(
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
        )
        return int(result.input_tokens)

    def extract(self, request: LLMRequest) -> LLMResponse:
        """JSON reply (schema in the system prompt, same request shape as a batch entry),
        validated locally after checking the stop reason: a refusal must be reported as a
        refusal, not as a schema error."""
        start = time.monotonic()
        try:
            params = self._params(request)
            message = self.client.messages.create(
                model=params["model"],
                max_tokens=params["max_tokens"],
                system=params["system"],
                messages=params["messages"],
                thinking=params["thinking"],
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
        """One batch, 50% price, same request shape as a sync call; results are validated
        on collection."""
        batch = self.client.messages.batches.create(
            requests=[
                Request(
                    custom_id=r.key,
                    params=cast(MessageCreateParamsNonStreaming, self._params(r, ttl="1h")),
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
