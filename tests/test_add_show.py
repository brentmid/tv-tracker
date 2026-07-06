"""Tests for the /add flow: search proxy + POST /api/shows, offline via an
injected fixture-fed TVMazeClient."""

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
from tvtracker import db, tvmaze  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def fixture(rel: str):
    return json.loads((FIXTURES / rel).read_text())


class NoSleep:
    def __init__(self):
        self.now = 0.0

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture
def srv(tmp_path):
    db_path = tmp_path / "test.db"
    db.connect(db_path).close()

    def fetch(url):
        if "/search/shows" in url:
            return 200, fixture("tvmaze/search_shows.json")
        if "/shows/82?embed=episodes" in url:
            return 200, fixture("tvmaze/show_82_episodes.json")
        if "/shows/404404?embed=episodes" in url:
            return 404, None
        raise AssertionError(f"unexpected URL: {url}")

    ns = NoSleep()
    client = tvmaze.TVMazeClient(
        fetch=fetch,
        limiter=tvmaze.TokenBucket(clock=ns.clock, sleep=ns.sleep),
        sleep=ns.sleep,
    )
    httpd = ThreadingHTTPServer(
        ("127.0.0.1", 0), tvserver.make_handler(db_path, tvmaze_client=client))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield base, db_path
    httpd.shutdown()
    httpd.server_close()


def get(url):
    with urllib.request.urlopen(url, timeout=5) as res:
        return res.status, res.read()


def post(url, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as res:
        return res.status, json.loads(res.read())


def test_add_page_renders(srv):
    base, _ = srv
    _, body = get(base + "/add")
    html = body.decode()
    assert "Search TVmaze" in html
    assert "searchShows()" in html


def test_search_proxy_trims_and_flags_already_added(srv):
    base, db_path = srv
    _, body = get(base + "/api/search/shows?q=game%20of%20thrones")
    results = json.loads(body)["results"]
    assert [r["tvmaze_id"] for r in results] == [82, 23482]
    assert results[0]["name"] == "Game of Thrones"
    assert results[0]["already_added"] is False
    assert "externals" not in json.loads(body)["results"][0]  # trimmed shape

    conn = db.connect(db_path)
    db.upsert_show(conn, tvmaze_id=82, name="Game of Thrones")
    conn.close()
    _, body = get(base + "/api/search/shows?q=game%20of%20thrones")
    assert json.loads(body)["results"][0]["already_added"] is True


def test_search_proxy_requires_query(srv):
    base, _ = srv
    with pytest.raises(urllib.error.HTTPError) as exc:
        get(base + "/api/search/shows?q=")
    assert exc.value.code == 400


def test_add_show_inserts_show_and_episodes(srv):
    base, db_path = srv
    code, payload = post(base + "/api/shows", {"tvmaze_id": 82})
    assert code == 200
    assert payload["ok"] is True
    assert payload["episodes"] == 4   # 5 in fixture, 1 unkeyable special
    assert payload["skipped"] == 1

    conn = db.connect(db_path)
    show = db.get_show_by_tvmaze_id(conn, 82)
    assert show["name"] == "Game of Thrones"
    assert show["last_refreshed_at"] is not None
    eps = db.list_episodes(conn, show["id"])
    assert [(e["season"], e["number"]) for e in eps] == [(1, 1), (1, 2), (1, 3), (2, 1)]
    assert all(e["watched_at"] is None for e in eps)
    conn.close()

    # adding again is idempotent (upserts, no dupes)
    code, payload = post(base + "/api/shows", {"tvmaze_id": 82})
    assert payload["episodes"] == 4
    conn = db.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM shows").fetchone()[0] == 1
    conn.close()


def test_add_show_validates_body_and_unknown_id(srv):
    base, _ = srv
    with pytest.raises(urllib.error.HTTPError) as exc:
        post(base + "/api/shows", {"tvmaze_id": "82"})
    assert exc.value.code == 400
    with pytest.raises(urllib.error.HTTPError) as exc:
        post(base + "/api/shows", {"tvmaze_id": 404404})
    assert exc.value.code == 404
