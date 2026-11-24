"""Gemini adapter (Google AI Studio). Used for the live runs: free tier, rate-limited.

Structured output via `response_mime_type="application/json"` plus the JSON schema of the
pydantic model; the result is validated locally again. Thinking is set to the lowest level
the model allows. A 429 waits for the server's RetryInfo delay; a per-day quota violation
stops the run.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from typing import Any

from google import genai
from google.genai import errors, types
from pydantic import ValidationError

from poi.extract.llm.base import LLMRequest, LLMResponse, Usage
from poi.extract.llm.throttle import DailyQuotaExhaustedError, RateLimiter

MAX_ATTEMPTS = 4


def retry_delay_s(details: Any) -> float | None:
    """Google RPC errors carry `RetryInfo.retryDelay` like "13s" or "1.5s"."""
    for item in (details or {}).get("error", {}).get("details", []) or []:
        if str(item.get("@type", "")).endswith("RetryInfo"):
            m = re.match(r"([\d.]+)s", str(item.get("retryDelay", "")))
            if m:
                return float(m.group(1))
    return None


def is_daily_quota(details: Any) -> bool:
    for item in (details or {}).get("error", {}).get("details", []) or []:
        for violation in item.get("violations", []) or []:
            if "PerDay" in str(violation.get("quotaId", "")):
                return True
    return False


class GeminiClient:
    provider = "gemini"

    def __init__(
        self,
        client: genai.Client,
        limiter: RateLimiter,
        thinking_levels: dict[str, str],
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client = client
        self.limiter = limiter
        self.thinking_levels = thinking_levels
        self.sleep = sleep

    def _config(self, request: LLMRequest) -> types.GenerateContentConfig:
        level = self.thinking_levels.get(request.model)
        return types.GenerateContentConfig(
            system_instruction=request.system,
            temperature=0,
            max_output_tokens=request.max_output_tokens,
            response_mime_type="application/json",
            response_json_schema=request.schema.model_json_schema(),
            thinking_config=types.ThinkingConfig(thinking_level=level) if level else None,
        )

    def count_tokens(self, request: LLMRequest) -> int:
        """Free token count of system + document (output schema overhead not included)."""
        result = self.client.models.count_tokens(
            model=request.model, contents=request.system + "\n\n" + request.user
        )
        return int(result.total_tokens or 0)

    def extract(self, request: LLMRequest) -> LLMResponse:
        estimate = (len(request.system) + len(request.user)) // 4
        last_error = ""
        for attempt in range(MAX_ATTEMPTS):
            self.limiter.acquire(estimate)
            start = time.monotonic()
            try:
                response = self.client.models.generate_content(
                    model=request.model, contents=request.user, config=self._config(request)
                )
            except errors.ClientError as exc:
                if exc.code != 429:
                    return self._failed(request, start, f"{exc.code} {exc.message}")
                if is_daily_quota(exc.details):
                    raise DailyQuotaExhaustedError(str(exc.message)) from exc
                last_error = f"429 {exc.message}"
                self.sleep(retry_delay_s(exc.details) or 2.0 * 2**attempt)
                continue
            except errors.ServerError as exc:
                last_error = f"{exc.code} {exc.message}"
                self.sleep(2.0 * 2**attempt)
                continue
            return self._response(request, response, start)
        return self._failed(request, time.monotonic(), last_error or "retries exhausted")

    def _failed(self, request: LLMRequest, start: float, error: str) -> LLMResponse:
        return LLMResponse(
            self.provider,
            request.model,
            None,
            "",
            Usage(),
            "ERROR",
            int((time.monotonic() - start) * 1000),
            "error",
            error,
        )

    def _response(
        self, request: LLMRequest, response: types.GenerateContentResponse, start: float
    ) -> LLMResponse:
        meta = response.usage_metadata
        cached = (meta.cached_content_token_count or 0) if meta else 0
        usage = Usage(
            input_tokens=((meta.prompt_token_count or 0) - cached) if meta else 0,
            output_tokens=(meta.candidates_token_count or 0) if meta else 0,
            cache_read_tokens=cached,
            thinking_tokens=(meta.thoughts_token_count or 0) if meta else 0,
        )
        candidate = response.candidates[0] if response.candidates else None
        finish = candidate.finish_reason.value if candidate and candidate.finish_reason else "NONE"
        text = response.text or ""
        latency = int((time.monotonic() - start) * 1000)
        if finish in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION"):
            outcome = "refusal"
        elif finish == "MAX_TOKENS":
            outcome = "truncated"
        else:
            outcome = "ok"
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
            self.provider, request.model, parsed, text, usage, finish, latency, outcome, error
        )
