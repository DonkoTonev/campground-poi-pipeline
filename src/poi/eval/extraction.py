"""Extraction evaluation and representation ablation on the gold set.

`poi eval extraction`: the production path (reduce → bundle → ladder → grounding) on each
gold case; per-field accuracy, page-kind accuracy, campground recall, empty-field rate,
hallucination-bait violations, person-name leaks, tokens and cost per page.

`poi eval ablation`: the same cases sent as raw HTML, cleaned HTML, Markdown and plain text
to the cheap model only (one call each, no escalation), to measure tokens against accuracy.

Both replay recorded responses by default (free). `--live` calls the provider for requests
not recorded yet and needs `--max-cost-usd`: a BudgetGuard stops before any call that could
pass it. `--dry-run` counts and prices those requests for free. Recordings exist only after a
live run, so on a fresh clone the replay mode stops with `MissingRecordingError`.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import yaml
from rapidfuzz import fuzz
from sqlalchemy import Engine

from poi.extract.grounding import ground
from poi.extract.ladder import run_ladder
from poi.extract.llm.base import LLMRequest, LLMResponse, Usage
from poi.extract.pricing import billed, haiku_equivalent
from poi.extract.prompt import INSTRUCTIONS, PROMPT_VERSION, wrap_document
from poi.extract.schema import Extraction
from poi.extract.stage import ASSUMED_OUTPUT_TOKENS, Setup, record_call
from poi.reduce import bundle
from poi.reduce.html import clean_html, represent, to_text
from poi.reduce.tokens import estimate
from poi.settings import PROJECT_ROOT

GOLD = PROJECT_ROOT / "eval" / "gold" / "extraction" / "synthetic.yaml"
REPORT = PROJECT_ROOT / "reports" / "extraction_eval.md"
ABLATION_REPORT = PROJECT_ROOT / "reports" / "ablation.md"
FILLER = (
    "<p>Chapter {i}. Our family has welcomed travellers to these woods for many decades, "
    "and every summer brings new stories around the campfire.</p>"
)
REPRESENTATIONS = ("raw_html", "cleaned_html", "markdown", "text")


@dataclass(frozen=True)
class Case:
    id: str
    html: str
    expected: dict[str, Any]


def load_cases() -> list[Case]:
    cases = []
    for raw in yaml.safe_load(GOLD.read_text()):
        html = raw["html"]
        if raw.get("long_filler"):
            html = html.replace(
                "{filler}", "\n".join(FILLER.format(i=i) for i in range(raw["long_filler"]))
            )
        page = f"<html><head><title>{raw['title']}</title></head><body>{html}</body></html>"
        cases.append(Case(raw["id"], page, raw["expected"]))
    return cases


def document_for(case: Case, representation: str = "markdown") -> str:
    """The document the model sees. `markdown` is exactly the production path."""
    url = f"https://{case.id.replace('_', '-')}.example.com"
    if representation == "markdown":
        reps = represent(case.html)
        return bundle.build(
            url.split("//")[1], [bundle.PageInput("P1", url, reps.markdown)], 12_000
        ).text
    body = {
        "raw_html": case.html,
        "cleaned_html": clean_html(case.html),
        "text": to_text(represent(case.html).markdown),
    }[representation]
    return f"# Website of {url.split('//')[1]}\n\n## [P1] {url}\n\n{body}"


# --- scoring ----------------------------------------------------------------------------


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.lower()).strip()


def value_ok(fld: str, expected: Any, got: Any) -> bool:
    if got is None:
        return False
    if fld == "name":
        return fuzz.ratio(_norm(expected), _norm(str(got))) >= 90
    if fld == "phone":
        return re.sub(r"\D", "", str(expected))[-10:] == re.sub(r"\D", "", str(got))[-10:]
    if fld == "season":
        if not got:
            return False
        return all(got[0].get(k) == v for k, v in expected[0].items())
    if isinstance(expected, dict):
        return isinstance(got, dict) and all(  # same rules per value (12 == 12.0)
            value_ok(k, v, got.get(k)) for k, v in expected.items()
        )
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        try:
            return float(got) == float(expected)
        except (TypeError, ValueError):
            return False
    return str(got).lower() == str(expected).lower()


@dataclass
class Score:
    fields: Counter[str] = field(default_factory=Counter)  # field -> correct
    totals: Counter[str] = field(default_factory=Counter)  # field -> scored
    empty: int = 0  # expected a value, got none
    page_kind_ok: int = 0
    cases: int = 0
    campgrounds_expected: int = 0
    campgrounds_found: int = 0
    bait_violations: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def field_accuracy(self) -> float:
        return sum(self.fields.values()) / max(sum(self.totals.values()), 1)


def score_case(
    case: Case, page_kind: str | None, campgrounds: list[dict[str, Any]], s: Score
) -> None:
    exp = case.expected
    s.cases += 1
    s.page_kind_ok += page_kind == exp["page_kind"]
    for want in exp.get("campgrounds", []):
        s.campgrounds_expected += 1
        best = max(
            campgrounds,
            key=lambda c: fuzz.ratio(_norm(want["name"]), _norm(c.get("name", ""))),
            default=None,
        )
        if best is None or not value_ok("name", want["name"], best.get("name")):
            for fld in want:
                s.totals[fld] += 1
                s.empty += 1
            s.errors.append(f"{case.id}: campground '{want['name']}' not found")
            continue
        s.campgrounds_found += 1
        for fld, expected in want.items():
            s.totals[fld] += 1
            got = best.get(fld)
            if fld == "amenities":
                got = best.get("amenities") or {}
            if got in (None, [], {}):
                s.empty += 1
            if value_ok(fld, expected, got):
                s.fields[fld] += 1
            else:
                s.errors.append(f"{case.id}: {fld} expected {expected!r}, got {got!r}")
    for camp in campgrounds:
        for fld in exp.get("absent", []):
            if camp.get(fld) not in (None, [], {}):
                s.bait_violations.append(f"{case.id}: {fld}={camp.get(fld)!r}")
        blob = " ".join(
            str(v) for k, v in camp.items() if not k.startswith("_") and k != "evidence"
        )
        for person in exp.get("no_person_names", []):
            if re.search(rf"\b{person}\b", blob):
                s.bait_violations.append(f"{case.id}: person name '{person}' extracted")
    if len(campgrounds) > exp.get("max_campgrounds", 99):
        s.bait_violations.append(f"{case.id}: {len(campgrounds)} campgrounds (injected?)")


# --- runs -------------------------------------------------------------------------------


@dataclass
class Spend:
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    calls: int = 0
    escalations: int = 0
    haiku_usd: float = 0.0
    doc_tokens_est: int = 0

    def add(self, response: LLMResponse) -> None:
        u = response.usage
        self.input_tokens += u.input_tokens + u.cache_read_tokens
        self.output_tokens += u.output_tokens
        self.thinking_tokens += u.thinking_tokens
        self.calls += 1
        self.haiku_usd += haiku_equivalent(u)


def ablation_request(setup: Setup, case: Case, rep: str, text: str) -> LLMRequest:
    return LLMRequest(
        setup.models.cheap,
        INSTRUCTIONS,
        wrap_document("web_bundle", f"gold:{case.id}:{rep}", text),
        Extraction,
        setup.max_output_tokens,
        key=f"ablation:{case.id}:{rep}",
    )


def ladder_requests(setup: Setup, case: Case) -> tuple[LLMRequest, LLMRequest]:
    """The cheap request `run_ladder` sends for a gold case, and the escalation it may send."""
    user = wrap_document("web_bundle", f"gold:{case.id}", document_for(case))
    doc_id = f"gold:{case.id}"
    return tuple(  # type: ignore[return-value]
        LLMRequest(model, INSTRUCTIONS, user, Extraction, setup.max_output_tokens,
                   key=f"{doc_id}:{step}")
        for step, model in (("cheap", setup.models.cheap), ("strong", setup.models.strong))
    )  # fmt: skip


def dry_run(setup: Setup, which: str) -> dict[str, Any]:
    """Free: counts the input tokens of every request the eval would send (already recorded
    requests cost nothing) and prices each one on its own. `which`: extraction | ablation."""
    planned: list[tuple[LLMRequest, bool]] = []  # (request, only if escalated)
    for case in load_cases():
        if which == "extraction":
            cheap, strong = ladder_requests(setup, case)
            planned += [(cheap, False), (strong, True)]
        else:
            planned += [
                (ablation_request(setup, case, rep, document_for(case, rep)), False)
                for rep in REPRESENTATIONS
            ]
    sure = reserve = 0.0
    tokens = recorded = 0
    for request, if_escalated in planned:
        if setup.client.has(request):  # type: ignore[attr-defined]
            recorded += 1
            continue
        counted = setup.client.count_tokens(request)
        tokens += counted
        usage = Usage(input_tokens=counted, output_tokens=ASSUMED_OUTPUT_TOKENS)
        cost = billed(setup.provider, request.model, usage)
        if if_escalated:
            reserve += cost
        else:
            sure += cost
    return {
        "eval": which,
        "provider": setup.provider,
        "models": {"cheap": setup.models.cheap, "strong": setup.models.strong},
        "cases": len(load_cases()),
        "requests_planned": len(planned),
        "already_recorded": recorded,
        "input_tokens_counted": tokens,
        "assumptions": f"{ASSUMED_OUTPUT_TOKENS} output tokens per request, no caching"
        + ("; worst case: every case escalates" if which == "extraction" else ""),
        "estimate_usd": {
            "calls": round(sure, 6),
            "escalation_reserve": round(reserve, 6),
            "worst_case_total": round(sure + reserve, 6),
        },
    }


def evaluate(engine: Engine, setup: Setup) -> dict[str, Any]:
    s, spend = Score(), Spend()
    with engine.begin() as conn:
        for case in load_cases():
            text = document_for(case)
            spend.doc_tokens_est += estimate(text)

            def on_call(step: str, request: LLMRequest, response: LLMResponse) -> None:
                spend.add(response)
                spend.escalations += step == "strong"
                record_call(conn, None, None, "eval", setup.provider, step, request, response)

            result = run_ladder(
                setup.client,
                setup.models,
                "web_bundle",
                f"gold:{case.id}",
                text,
                [],
                setup.max_output_tokens,
                setup.max_ungrounded_share,
                on_call,
            )
            score_case(case, result.page_kind, result.campgrounds, s)
    out = _summary(s, spend)
    _write_eval_report(out, s, setup)
    return out


def ablation(engine: Engine, setup: Setup) -> dict[str, Any]:
    rows: dict[str, dict[str, Any]] = {}
    with engine.begin() as conn:
        for rep in REPRESENTATIONS:
            s, spend = Score(), Spend()
            for case in load_cases():
                text = document_for(case, rep)
                spend.doc_tokens_est += estimate(text)
                request = ablation_request(setup, case, rep, text)
                response = setup.client.extract(request)
                spend.add(response)
                record_call(
                    conn, None, None, "ablation", setup.provider, "cheap", request, response
                )
                camps = []
                parsed: dict[str, Any] = response.parsed or {}
                for camp in parsed.get("campgrounds", []):
                    grounded, _ = ground(camp, text)
                    camps.append(grounded)
                score_case(case, parsed.get("page_kind"), camps, s)
            rows[rep] = _summary(s, spend)
    _write_ablation_report(rows, setup)
    return rows


def _summary(s: Score, spend: Spend) -> dict[str, Any]:
    n = max(s.cases, 1)
    return {
        "cases": s.cases,
        "prompt_version": PROMPT_VERSION,
        "page_kind_accuracy": round(s.page_kind_ok / n, 3),
        "campground_recall": round(s.campgrounds_found / max(s.campgrounds_expected, 1), 3),
        "field_accuracy": round(s.field_accuracy, 3),
        "fields_scored": sum(s.totals.values()),
        "empty_field_rate": round(s.empty / max(sum(s.totals.values()), 1), 3),
        "bait_violations": len(s.bait_violations),
        "per_field": {f: f"{s.fields[f]}/{s.totals[f]}" for f in sorted(s.totals)},
        "calls": spend.calls,
        "escalations": spend.escalations,
        "doc_tokens_est_per_page": spend.doc_tokens_est // n,
        "input_tokens_per_page": spend.input_tokens // n,
        "output_tokens_per_page": spend.output_tokens // n,
        "thinking_tokens": spend.thinking_tokens,
        "est_haiku_usd_per_page": round(spend.haiku_usd / n, 6),
        "errors": s.errors[:30],
        "violations": s.bait_violations,
    }


def _write_eval_report(out: dict[str, Any], s: Score, setup: Setup) -> None:
    lines = [
        "# Extraction evaluation",
        "",
        f"{out['cases']} synthetic gold cases (expected values correct by construction), "
        f"prompt `{out['prompt_version']}`, provider `{setup.provider}` "
        f"({setup.models.cheap} → {setup.models.strong}), {date.today().isoformat()}.",
        "",
        "| metric | value |",
        "|---|---|",
        f"| page kind accuracy | {out['page_kind_accuracy']:.0%} |",
        f"| campground recall | {out['campground_recall']:.0%} |",
        f"| field accuracy | {out['field_accuracy']:.0%} ({out['fields_scored']} fields) |",
        f"| empty-field rate | {out['empty_field_rate']:.0%} |",
        f"| hallucination / privacy bait violations | {out['bait_violations']} |",
        f"| escalations to the strong model | {out['escalations']} of {out['cases']} |",
        f"| input tokens per page (real, {setup.provider}) | {out['input_tokens_per_page']:,} |",
        f"| output tokens per page | {out['output_tokens_per_page']:,} |",
        f"| same tokens on Claude Haiku 5.5 (estimate) | "
        f"${out['est_haiku_usd_per_page']:.6f} per page |",
        "",
        "## Per field",
        "",
        "| field | correct |",
        "|---|---|",
        *(f"| {f} | {v} |" for f, v in out["per_field"].items()),
        "",
        "## Errors",
        "",
        *(f"- {e}" for e in out["errors"]),
        *(f"- violation: {v}" for v in out["violations"]),
        "",
    ]
    REPORT.parent.mkdir(exist_ok=True)
    REPORT.write_text("\n".join(lines))


def _write_ablation_report(rows: dict[str, dict[str, Any]], setup: Setup) -> None:
    lines = [
        "# Representation ablation",
        "",
        f"Same gold cases, cheap model only ({setup.models.cheap}, provider `{setup.provider}`), "
        f"one call per case and representation, {date.today().isoformat()}. The input tokens "
        "are the provider's real counts (instructions included); the Haiku column prices the "
        "same token counts on Claude Haiku 5.5 and is an estimate.",
        "",
        "| representation | input tokens / page | field accuracy | page kind | bait violations "
        "| est. Haiku $ / page |",
        "|---|---|---|---|---|---|",
        *(
            f"| {rep} | {r['input_tokens_per_page']:,} | {r['field_accuracy']:.0%} | "
            f"{r['page_kind_accuracy']:.0%} | {r['bait_violations']} | "
            f"${r['est_haiku_usd_per_page']:.6f} |"
            for rep, r in rows.items()
        ),
        "",
        "Synthetic pages are small and clean, so raw HTML is far less bloated here than on real",
        "sites (real pages average about 36,000 estimated tokens of raw HTML; see",
        "reports/tokens.md). The token ratio on real pages is the one that matters for cost.",
        "",
    ]
    ABLATION_REPORT.parent.mkdir(exist_ok=True)
    ABLATION_REPORT.write_text("\n".join(lines))
