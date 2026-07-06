"""Tests for server/server.py — boots a real ThreadingHTTPServer on port 0."""

import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

import server as tvserver  # noqa: E402
from tvtracker import db  # noqa: E402


@pytest.fixture
def srv(tmp_path):
    """A live server on 127.0.0.1:<ephemeral> backed by a tmp DB."""
    db_path = tmp_path / "test.db"
    db.connect(db_path).close()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), tvserver.make_handler(db_path))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield base, db_path
    httpd.shutdown()
    httpd.server_close()


def get(url):
    with urllib.request.urlopen(url, timeout=5) as res:
        return res.status, res.headers.get("Content-Type", ""), res.read()


def test_healthz(srv):
    base, _ = srv
    code, ctype, body = get(base + "/healthz")
    assert code == 200
    assert ctype == "application/json"
    payload = json.loads(body)
    assert payload["ok"] is True
    assert payload["shows"] == 0


def test_healthz_counts_shows(srv):
    base, db_path = srv
    conn = db.connect(db_path)
    db.upsert_show(conn, tvmaze_id=1, name="Show")
    conn.close()
    _, _, body = get(base + "/healthz")
    assert json.loads(body)["shows"] == 1


def test_queue_page_renders_chrome(srv):
    base, _ = srv
    code, ctype, body = get(base + "/")
    html = body.decode()
    assert code == 200
    assert ctype.startswith("text/html")
    assert "Watch Next" in html
    assert "/assets/style.css" in html
    assert "TVmaze" in html and "TMDB" in html  # attribution footer
    assert 'href="/movies"' in html  # nav present


def test_assets_served_with_content_type(srv):
    base, _ = srv
    code, ctype, body = get(base + "/assets/style.css")
    assert code == 200
    assert ctype == "text/css; charset=utf-8"
    assert b"--bg" in body
    code, ctype, _ = get(base + "/assets/app.js")
    assert code == 200
    assert ctype == "application/javascript; charset=utf-8"


def test_home_screen_app_assets(srv):
    base, _ = srv
    code, ctype, body = get(base + "/assets/apple-touch-icon.png")
    assert code == 200
    assert ctype == "image/png"
    assert body.startswith(b"\x89PNG")
    code, ctype, _ = get(base + "/assets/favicon.svg")
    assert (code, ctype) == (200, "image/svg+xml")
    code, ctype, body = get(base + "/assets/manifest.webmanifest")
    assert (code, ctype) == (200, "application/manifest+json")
    manifest = json.loads(body)
    assert manifest["display"] == "standalone"
    assert manifest["icons"][0]["src"] == "/assets/apple-touch-icon.png"

    page = get(base + "/")[2].decode()
    for tag in ('rel="apple-touch-icon"', 'rel="manifest"',
                'rel="icon"', 'apple-mobile-web-app-capable'):
        assert tag in page


@pytest.mark.parametrize("path", [
    "/nope",
    "/assets/missing.css",
    "/assets/..%2Fserver.py",   # encoded traversal — regex rejects the slash
    "/assets/..",               # bare dot-dot resolves outside assets dir
])
def test_404s(srv, path):
    base, _ = srv
    with pytest.raises(urllib.error.HTTPError) as exc:
        get(base + path)
    assert exc.value.code == 404


def test_render_page_marks_active_nav():
    html = tvserver.render_page("T", "<p>x</p>", active_nav="movies")
    assert '<a href="/movies" class="active">' in html
    assert '<a href="/" class="">' in html
