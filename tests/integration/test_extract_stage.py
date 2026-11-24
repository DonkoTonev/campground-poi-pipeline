"""`poi extract` end to end on fixtures: Anthropic batch path through the real SDK (mock
transport, nothing leaves the machine), ledger, extractions, promotion, matching."""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any

import anthropic
import httpx
import httpx2
import pytest
import respx
from sqlalchemy import Engine, text

from conftest import load_fixture
from poi.extract import stage as extract_stage
from poi.extract.ladder import Models
from poi.extract.llm.anthropic import AnthropicClient
from poi.extract.llm.replay import ReplayClient
from poi.extract.stage import Setup, extract_region
from poi.ingest import ingest_source
from poi.match.stage import match_region
from poi.normalize.stage import normalize_region
from poi.reduce.stage import reduce_region
from poi.resolve.stage import resolve_region
from poi.settings import Settings, load_region
from poi.sources import ridb
from poi.storage.local import LocalStore

pytestmark = pytest.mark.integration
SIERRA = load_region("sierra")


def _answer(document: str) -> dict[str, Any]:
    """What a well-behaved model would return for a RIDB description document."""
    name = re.search(r"^# (.+?) \(Recreation\.gov", document, re.M)
    return {
        "page_kind": "campground_detail",
        "campgrounds": [{"name": name.group(1) if name else "Unknown", "phone": "760-555-0100"}],
    }  # phone not in the text: hallucinated


def _message(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "msg",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-5-5",
        "content": [{"type": "text", "text": json.dumps(payload)}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": 900,
            "output_tokens": 120,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
    }


class FakeAnthropicAPI:
    """Just enough of the Messages + Batches API, answering from the request content."""

    def __init__(self) -> None:
        self.batch_requests: list[dict[str, Any]] = []
        self.sync_calls = 0

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        url = str(request.url)
        batch = {
            "id": "msgbatch_1",
            "type": "message_batch",
            "processing_status": "ended",
            "request_counts": {
                "processing": 0,
                "succeeded": 0,
                "errored": 0,
                "canceled": 0,
                "expired": 0,
            },
            "created_at": "2026-11-24T00:00:00Z",
            "expires_at": "2026-11-25T00:00:00Z",
            "ended_at": "2026-11-24T00:10:00Z",
            "cancel_initiated_at": None,
            "archived_at": None,
            "results_url": "https://api.anthropic.com/v1/messages/batches/msgbatch_1/results",
        }
        if url.endswith("/v1/messages/batches") and request.method == "POST":
            self.batch_requests = json.loads(request.content)["requests"]
            return httpx2.Response(200, json=batch)
        if url.endswith("/results"):
            lines = [
                json.dumps(
                    {
                        "custom_id": r["custom_id"],
                        "result": {
                            "type": "succeeded",
                            "message": _message(_answer(r["params"]["messages"][0]["content"])),
                        },
                    }
                )
                for r in self.batch_requests
            ]
            return httpx2.Response(200, text="\n".join(lines))
        if "/batches/" in url:
            return httpx2.Response(200, json=batch)
        self.sync_calls += 1  # escalation to the strong model
        body = json.loads(request.content)
        return httpx2.Response(200, json=_message(_answer(body["messages"][0]["content"])))


@respx.mock
def test_extract_batch_ledger_promotion_and_matching(
    db: Engine, settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(ridb, "POLITE_DELAY_S", 0)
    store = LocalStore(settings.raw_store_path)
    respx.post(settings.overpass_url).mock(
        return_value=httpx.Response(200, json=load_fixture("osm/overpass.json"))
    )
    respx.get(url__startswith=settings.ridb_base_url).mock(
        return_value=httpx.Response(200, json=load_fixture("ridb/facilities.json"))
    )
    respx.get(url__startswith=settings.nps_base_url).mock(
        return_value=httpx.Response(200, json=load_fixture("nps/campgrounds.json"))
    )
    for source in ("osm", "ridb", "nps"):
        ingest_source(db, store, settings, SIERRA, source)
    normalize_region(db, SIERRA, date(2026, 11, 24))
    match_region(db, SIERRA)
    resolve_region(db, SIERRA)
    reduce_region(db, settings, SIERRA)

    api = FakeAnthropicAPI()
    http = anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(api))
    inner = AnthropicClient(
        anthropic.Anthropic(api_key="test", http_client=http, max_retries=0), sleep=lambda _: None
    )
    setup = Setup(
        ReplayClient("anthropic", inner, "record", tmp_path / "rec"),
        "anthropic",
        Models("claude-haiku-5-5", "claude-sonnet-5-5"),
        2000,
        0.25,
    )
    monkeypatch.setattr(extract_stage, "build_client", lambda *a, **k: setup)

    # A tiny budget stops before the first document.
    stopped = extract_region(db, settings, SIERRA, mode="record", max_cost_usd=1e-9)
    assert stopped["stopped_max_cost"] == 1 and api.batch_requests == []

    stats = extract_region(db, settings, SIERRA, mode="record", kind="ridb_description", batch=True)
    n_docs = len(api.batch_requests)
    assert n_docs >= 3 and stats["batched"] == n_docs
    # The fake model always returns a phone that is not in the text: 1 of 1 checkable
    # values is ungrounded, so every document escalates once (synchronously) to Sonnet.
    assert api.sync_calls == n_docs

    with db.connect() as conn:
        ledger = conn.execute(
            text(
                "SELECT ladder_step, is_batch, replayed, cost_usd FROM llm_calls "
                "WHERE purpose = 'extract'"
            )
        ).all()
        extracted = (
            conn.execute(
                text("SELECT payload FROM source_records WHERE source = 'ridb_description'")
            )
            .scalars()
            .all()
        )
    batch_rows = [r for r in ledger if r.is_batch]
    assert len(batch_rows) == n_docs
    # 900 input + 120 output tokens on Haiku at batch price: (90 + 60) / 1e6 / 2
    assert all(float(r.cost_usd) == pytest.approx(0.000075) for r in batch_rows)
    assert all(
        r.replayed and float(r.cost_usd) == 0
        for r in ledger
        if not r.is_batch and r.ladder_step == "cheap"
    )
    assert extracted and all("phone" not in p for p in extracted)  # grounding dropped it
    assert all(p["_provenance"]["kind"] == "ridb_description" for p in extracted)

    # The extracted records join their RIDB facility through the shared recreation.gov id.
    normalize_region(db, SIERRA, date(2026, 11, 24))
    match_region(db, SIERRA)
    with db.connect() as conn:
        joined = conn.execute(
            text(
                "SELECT count(*) FROM cluster_members a JOIN cluster_members b "
                "ON a.cluster_key = b.cluster_key JOIN poi_records pa ON pa.id = a.poi_record_id "
                "JOIN poi_records pb ON pb.id = b.poi_record_id "
                "WHERE pa.source = 'ridb_description' AND pb.source = 'ridb'"
            )
        ).scalar_one()
    assert joined == len(extracted)
