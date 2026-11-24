"""End-to-end crawl against a local fixture server (two hosts on one port).

`localhost` is a campground site full of traps; `127.0.0.1` blocks bots. The crawl runs in
a subprocess (Twisted's reactor cannot be restarted inside one test process) against the
test database, with the SSRF guard's test-only escape hatch enabled.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from collections import Counter
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest
import yaml
from sqlalchemy import Engine, text

from poi.settings import PROJECT_ROOT

pytestmark = pytest.mark.integration

CONTENT = "<p>" + "Our campground has 40 sites, drinking water and flush toilets. " * 30 + "</p>"
HOME = """<html><head><title>Pine Test Campground</title></head><body>
<a href="/rates">Rates</a>
<a href="/specials">Camping specials</a>
<a href="/amenities">Amenities</a>
<a href="/amenities?utm_source=newsletter">Amenities (tracked link)</a>
<a href="/busy">Busy season info</a>
<a href="/missing">Campsite map</a>
<a href="/old-page">Reservations</a>
<a href="/guide.pdf">Camp guide (PDF)</a>
<a href="/private/admin">Private camp area</a>
<a href="/blog/summer-news">Blog</a>
<a href="/sites?date=2026-07-04">Availability calendar</a>
<a href="https://elsewhere.example/camp">Partner camp</a>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    hits: ClassVar[Counter[str]] = Counter()

    def log_message(self, *args: object) -> None:  # keep test output quiet
        pass

    def _send(
        self,
        status: int,
        body: bytes,
        ctype: str = "text/html",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        host = (self.headers.get("Host") or "").split(":")[0]
        Handler.hits[f"{host}{self.path}"] += 1
        if host == "127.0.0.1":
            if self.path == "/robots.txt":
                return self._send(404, b"")
            return self._send(403, b"<title>Service unavailable</title>The request is blocked.")
        page = self.path
        if page == "/robots.txt":
            return self._send(200, b"User-agent: *\nDisallow: /private/\n", "text/plain")
        if page == "/old-home":  # the seed moved within its own site
            return self._send(301, b"", headers={"Location": "/"})
        if page == "/":
            return self._send(200, HOME.encode())
        if page == "/specials":  # trailing-slash variant of the same page
            return self._send(301, b"", headers={"Location": "/specials/"})
        if page == "/specials/":
            return self._send(200, f"<html><body>{CONTENT}</body></html>".encode())
        if page in ("/rates", "/amenities"):
            return self._send(
                200,
                f"<html><body>{CONTENT}</body></html>".encode(),
                headers={"ETag": f'"{page}-v1"'},
            )
        if page == "/busy":
            if Handler.hits["localhost/busy"] == 1:
                return self._send(429, b"slow down", headers={"Retry-After": "1"})
            return self._send(200, f"<html><body>{CONTENT}</body></html>".encode())
        if page == "/missing":
            return self._send(200, b"<html><body><h1>Sorry, page not found</h1></body></html>")
        if page == "/old-page":
            return self._send(302, b"", headers={"Location": "https://www.campspot.com/book/pine"})
        if page == "/guide.pdf":
            return self._send(200, b"%PDF-1.4 fake pdf", "application/pdf")
        return self._send(404, b"<html><body>Not found</body></html>")


@pytest.fixture
def server() -> Iterator[int]:
    Handler.hits = Counter()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd.server_address[1]
    httpd.shutdown()


def _test_config(tmp_path: Path) -> Path:
    config = tmp_path / "config"
    shutil.copytree(PROJECT_ROOT / "config", config)
    crawl = yaml.safe_load((config / "crawl.yaml").read_text())
    crawl["politeness"] |= {"download_delay_s": 0.05, "timeout_s": 10}
    crawl["scope"]["max_pages_per_site"] = 10
    (config / "crawl.yaml").write_text(yaml.safe_dump(crawl))
    return config


def test_crawl_end_to_end(db: Engine, server: int, tmp_path: Path) -> None:
    env = os.environ | {
        "DATABASE_URL": db.url.render_as_string(hide_password=False),
        "CONFIG_DIR": str(_test_config(tmp_path)),
        "RAW_STORE_PATH": str(tmp_path / "raw"),
        "CRAWL_ALLOW_PRIVATE_HOSTS": "true",
        "CONTACT_URL": "https://example.org/contact",
    }
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "poi.cli",
            "crawl",
            "--seed",
            f"http://localhost:{server}/old-home",
            "--seed",
            f"http://127.0.0.1:{server}/",
            "--seed",
            f"http://127.0.0.1:{server}/another-campground",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-3000:]

    with db.connect() as conn:
        sites = {
            r.seed_url.replace(f":{server}", ""): r
            for r in conn.execute(text("SELECT * FROM sites"))
        }
        pages = {
            r.url.split(f":{server}")[1]: r
            for r in conn.execute(
                text(
                    "SELECT p.* FROM pages p JOIN sites s ON s.id = p.site_id "
                    "WHERE s.seed_url LIKE 'http://localhost%'"
                )
            )
        }
    hits = Handler.hits

    home = sites["http://localhost/old-home"]
    assert home.status == "ok"
    assert home.booking_platform == "campspot"  # recorded, not followed
    assert pages["/old-home"].final_url.endswith(f":{server}/")  # moved seed followed
    assert pages["/specials"].outcome == "fetched"
    assert pages["/specials"].final_url.endswith("/specials/")
    # A block stops the whole domain: the second site on that host is never requested.
    assert sites["http://127.0.0.1"].status == "blocked"
    assert sites["http://127.0.0.1/another-campground"].status == "blocked"
    blocked_hits = [k for k in hits if k.startswith("127.0.0.1/") and k != "127.0.0.1/robots.txt"]
    assert sum(hits[k] for k in blocked_hits) == 1

    assert pages["/rates"].outcome == "fetched" and pages["/rates"].raw_object_id
    assert hits["localhost/amenities"] == 1  # tracking-parameter duplicate fetched once
    assert pages["/private/admin"].outcome == "robots_blocked"
    assert hits["localhost/private/admin"] == 0
    assert hits["localhost/blog/summer-news"] == 0  # excluded keyword
    assert not any(k.startswith("localhost/sites?") for k in hits)  # calendar trap
    assert hits["localhost/busy"] == 2 and pages["/busy"].outcome == "fetched"  # Retry-After
    assert "soft_404" in pages["/missing"].flags
    assert pages["/old-page"].outcome == "off_domain_redirect"
    assert "booking:campspot" in pages["/old-page"].flags
    assert pages["/guide.pdf"].flags == ["non_html"] and pages["/guide.pdf"].raw_object_id
