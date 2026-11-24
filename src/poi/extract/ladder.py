"""The escalation ladder for one document.

0. skip      - same content and prompt version already extracted (decided by the stage);
1. determ.   - facts found without an LLM (tel: links, JSON-LD) fill and cross-check;
2. reduce    - the document is already reduced Markdown (`poi reduce`);
3. cheap     - cheap model with structured output, then grounding;
4. strong    - only if the cheap answer failed validation, was refused/truncated, or too
               much of it was ungrounded: one retry on the strong model;
5. review    - still not good enough: kept with status `needs_review` for a person.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from poi.extract.grounding import GroundingReport, ground, phone_from_facts
from poi.extract.llm.base import LLMClient, LLMRequest, LLMResponse
from poi.extract.prompt import INSTRUCTIONS, wrap_document
from poi.extract.schema import Extraction

NO_CAMPGROUND_KINDS = frozenset({"not_campground", "dead_or_parked"})


@dataclass
class LadderResult:
    status: str  # ok | needs_review | failed
    step: str  # cheap | strong | review | failed
    model: str | None
    page_kind: str | None
    campgrounds: list[dict[str, Any]]
    grounding: dict[str, Any]
    hallucination_rate: float | None
    calls: list[tuple[str, LLMResponse]] = field(default_factory=list)  # (step, response)


@dataclass(frozen=True)
class Models:
    cheap: str
    strong: str


def _ground_all(
    parsed: dict[str, Any], text: str, facts: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], GroundingReport]:
    total = GroundingReport()
    out = []
    single = len(parsed.get("campgrounds", [])) == 1
    fact_phone = phone_from_facts(facts)
    for camp in parsed.get("campgrounds", []):
        grounded, report = ground(camp, text, facts)
        # Step 1 fills what the model left out: a deterministic phone, for a one-campground
        # document only (on a multi-campground page we can't tell whose phone it is).
        methods = {k: "llm" for k in grounded if k not in ("evidence", "name")}
        if single and fact_phone and "phone" not in grounded:
            grounded["phone"] = fact_phone
            methods["phone"] = "deterministic:tel_link"
            report.confidence["phone"] = 0.95
        grounded["_methods"] = methods
        grounded["_confidence"] = report.confidence
        out.append(grounded)
        total.checked += report.checked
        total.dropped += report.dropped
        total.unsupported += report.unsupported
        total.good_quotes += report.good_quotes
        total.bad_quotes += report.bad_quotes
    return out, total


def needs_escalation(
    response: LLMResponse, report: GroundingReport | None, max_ungrounded_share: float
) -> str | None:
    if response.outcome != "ok" or response.parsed is None:
        return f"outcome:{response.outcome}"
    if report and report.checked and report.hallucination_rate > max_ungrounded_share:
        return f"ungrounded:{report.hallucination_rate:.2f}"
    return None


def run_ladder(
    client: LLMClient,
    models: Models,
    kind: str,
    document_id: str,
    text: str,
    facts: list[dict[str, Any]],
    max_output_tokens: int,
    max_ungrounded_share: float,
    on_call: Callable[[str, LLMRequest, LLMResponse], None] | None = None,
) -> LadderResult:
    user = wrap_document(kind, document_id, text)
    result: LadderResult | None = None
    calls: list[tuple[str, LLMResponse]] = []
    for step, model in (("cheap", models.cheap), ("strong", models.strong)):
        request = LLMRequest(
            model, INSTRUCTIONS, user, Extraction, max_output_tokens, key=f"{document_id}:{step}"
        )
        response = client.extract(request)
        calls.append((step, response))
        if on_call:
            on_call(step, request, response)
        report = None
        campgrounds: list[dict[str, Any]] = []
        if response.parsed is not None:
            campgrounds, report = _ground_all(response.parsed, text, facts)
        reason = needs_escalation(response, report, max_ungrounded_share)
        if response.parsed is not None:
            kind_out = response.parsed.get("page_kind")
            result = LadderResult(
                status="ok" if reason is None else "needs_review",
                step=step if reason is None else "review",
                model=model,
                page_kind=kind_out,
                campgrounds=[] if kind_out in NO_CAMPGROUND_KINDS else campgrounds,
                grounding={
                    "checked": report.checked if report else 0,
                    "dropped": report.dropped if report else [],
                    "unsupported": report.unsupported if report else [],
                    "good_quotes": report.good_quotes if report else 0,
                    "bad_quotes": report.bad_quotes if report else 0,
                    "escalation_reason": reason,
                },
                hallucination_rate=report.hallucination_rate if report else None,
            )
        if reason is None:
            break
    if result is None:  # neither model produced a valid answer
        return LadderResult("failed", "failed", None, None, [], {}, None, calls)
    result.calls = calls
    return result
