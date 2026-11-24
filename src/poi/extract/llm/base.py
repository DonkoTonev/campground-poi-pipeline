"""What the extraction code needs from any LLM provider, and nothing more."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class LLMRequest:
    model: str
    system: str  # static instructions: the cacheable prefix
    user: str  # the wrapped, untrusted document
    schema: type[Any]  # pydantic model of the expected output
    max_output_tokens: int = 4096
    key: str = ""  # caller's id for the request (document id, gold id, ...)


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0  # uncached input
    output_tokens: int = 0  # visible output (excluding thinking)
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    thinking_tokens: int = 0


@dataclass(frozen=True)
class LLMResponse:
    provider: str
    model: str
    parsed: dict[str, Any] | None  # validated against the schema, or None
    text: str
    usage: Usage
    stop_reason: str  # provider's own value, e.g. end_turn / STOP / refusal / SAFETY
    latency_ms: int
    outcome: str  # ok | invalid_output | refusal | truncated | error
    error: str | None = None
    replayed: bool = False
    meta: dict[str, Any] = field(default_factory=dict)


class LLMClient(Protocol):
    provider: str

    def extract(self, request: LLMRequest) -> LLMResponse: ...

    def count_tokens(self, request: LLMRequest) -> int: ...
