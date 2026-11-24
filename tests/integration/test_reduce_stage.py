"""`poi reduce` end to end: a crawled page and RIDB descriptions become documents."""

from datetime import date

import httpx
import pytest
import respx
from sqlalchemy import Engine, text

from conftest import load_fixture
from poi.fetch import store_raw
from poi.ingest import ingest_source
from poi.match.stage import match_region
from poi.normalize.stage import normalize_region
from poi.reduce.stage import reduce_region
from poi.resolve.stage import resolve_region
from poi.runs import track_run
from poi.settings import Settings, load_region
from poi.sources import ridb
from poi.storage.local import LocalStore

pytestmark = pytest.mark.integration
SIERRA = load_region("sierra")
PAGE = b"""<html><body><nav>Home</nav><main><h1>Pine Test Campground</h1>
<p>40 sites. Open May 15 to October 1. Full hookups $55 per night.</p></main>
<footer><a href="tel:+17605550142">Call us</a></footer></body></html>"""


@respx.mock
def test_reduce_builds_web_and_ridb_documents(
    db: Engine, settings: Settings, monkeypatch: pytest.MonkeyPatch
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

    # One crawled site with one page, as the crawler would have recorded it.
    with track_run(db, "crawl", SIERRA.name) as run:
        raw_id = store_raw(
            db,
            store,
            source="web",
            request_key_="k" * 64,
            request={"url": "https://pine.example.com"},
            body=PAGE,
            status=200,
            headers={"content-type": "text/html"},
            run_id=run.id,
            suffix="html",
        )
        with db.begin() as conn:
            site_id = conn.execute(
                text(
                    "INSERT INTO sites (region, seed_url, status, record_refs) "
                    "VALUES ('sierra', 'https://pine.example.com', 'ok', '{osm:way/1}') "
                    "RETURNING id"
                )
            ).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO pages (site_id, url, depth, http_status, content_type, "
                    "raw_object_id, outcome, run_id) VALUES (:s, 'https://pine.example.com', "
                    "0, 200, 'text/html', :raw, 'fetched', :run)"
                ),
                {"s": site_id, "raw": raw_id, "run": run.id},
            )

    first = reduce_region(db, settings, SIERRA)
    second = reduce_region(db, settings, SIERRA)
    for key in ("pages", "bundles", "ridb_documents", "bundle_tokens", "ridb_tokens"):
        assert first[key] == second[key], key  # idempotent
    assert first["pages"] == 1 and first["bundles"] == 1
    assert first["ridb_documents"] >= 3  # Pines records from the RIDB fixture

    with db.connect() as conn:
        docs = conn.execute(
            text("SELECT kind, record_refs, deterministic, page_refs, est_tokens FROM documents")
        ).all()
        reps = conn.execute(text("SELECT count(*) FROM page_texts")).scalar_one()
    kinds = {d.kind for d in docs}
    assert kinds == {"web_bundle", "ridb_description"}  # distinct provenance
    assert len(docs) == 1 + first["ridb_documents"]  # no duplicates after the re-run
    web = next(d for d in docs if d.kind == "web_bundle")
    assert web.record_refs == ["osm:way/1"] and web.page_refs == {"P1": "https://pine.example.com"}
    assert {
        "field": "phone",
        "value": "+17605550142",
        "method": "tel_link",
        "page_ref": "P1",
    } in web.deterministic
    assert all(d.record_refs[0].startswith("ridb:") for d in docs if d.kind == "ridb_description")
    assert reps == 4  # raw, cleaned, markdown, text for the one page
