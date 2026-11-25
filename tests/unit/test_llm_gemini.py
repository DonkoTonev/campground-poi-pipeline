"""Gemini adapter (optional extra `gemini`), with a fake google-genai client."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("google.genai", reason="optional extra: uv sync --extra gemini")

from google.genai import errors
from test_llm import GOOD, _request

from poi.extract.llm.base import Usage
from poi.extract.llm.gemini import GeminiClient, retry_delay_s
from poi.extract.llm.throttle import DailyQuotaExhaustedError, RateLimiter
from poi.extract.prompt import INSTRUCTIONS


class FakeModels:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = outcomes
        self.configs: list[Any] = []

    def generate_content(self, model: str, contents: str, config: Any) -> Any:
        self.configs.append(config)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _gemini_response(payload: dict[str, Any], finish: str = "STOP") -> Any:
    return SimpleNamespace(
        text=json.dumps(payload),
        candidates=[SimpleNamespace(finish_reason=SimpleNamespace(value=finish))],
        usage_metadata=SimpleNamespace(
            prompt_token_count=2000,
            candidates_token_count=80,
            cached_content_token_count=1500,
            thoughts_token_count=0,
        ),
    )


def _gemini(outcomes: list[Any], slept: list[float]) -> tuple[GeminiClient, FakeModels]:
    models = FakeModels(outcomes)
    limiter = RateLimiter(rpm=100, tpm=10**9, rpd=1000, sleep=slept.append)
    client = GeminiClient(
        SimpleNamespace(models=models),
        limiter,  # type: ignore[arg-type]
        {"gemini-3.5-flash-lite": "MINIMAL"},
        sleep=slept.append,
    )
    return client, models


def _rate_limited(delay: str, quota: str = "GenerateRequestsPerMinutePerProjectPerModel") -> Any:
    details = {
        "error": {
            "code": 429,
            "status": "RESOURCE_EXHAUSTED",
            "message": "quota",
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                    "violations": [{"quotaId": quota}],
                },
                {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay},
            ],
        }
    }
    return errors.ClientError(429, details)


def test_gemini_config_usage_and_parsing() -> None:
    client, models = _gemini([_gemini_response(GOOD)], [])
    response = client.extract(_request("gemini-3.5-flash-lite"))
    config = models.configs[0]
    assert config.response_mime_type == "application/json" and config.temperature == 0
    assert config.response_json_schema["title"] == "Extraction"
    assert config.system_instruction == INSTRUCTIONS
    assert str(config.thinking_config.thinking_level).endswith("MINIMAL")
    assert response.outcome == "ok" and response.parsed is not None
    assert response.usage == Usage(input_tokens=500, output_tokens=80, cache_read_tokens=1500)


def test_gemini_429_waits_for_retry_info_then_succeeds() -> None:
    slept: list[float] = []
    client, _ = _gemini([_rate_limited("13s"), _gemini_response(GOOD)], slept)
    assert client.extract(_request("gemini-3.5-flash-lite")).outcome == "ok"
    assert 13.0 in slept
    assert (
        retry_delay_s({"error": {"details": [{"@type": "x.RetryInfo", "retryDelay": "1.5s"}]}})
        == 1.5
    )


def test_gemini_daily_quota_stops_the_run() -> None:
    client, _ = _gemini(
        [_rate_limited("60s", "GenerateRequestsPerDayPerProjectPerModel-FreeTier")], []
    )
    with pytest.raises(DailyQuotaExhaustedError):
        client.extract(_request("gemini-3.5-flash-lite"))


def test_gemini_safety_block_is_a_refusal() -> None:
    client, _ = _gemini([_gemini_response({}, finish="SAFETY")], [])
    assert client.extract(_request("gemini-3.5-flash-lite")).outcome == "refusal"
