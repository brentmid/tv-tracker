"""Tests for the /movies page and movie API routes, offline via an injected
fixture-fed TMDBClient (including the no-key-configured path)."""

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
from tvtracker import db, tmdb  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def fixture(rel: str):
    return json.loads((FIXTURES / rel).read_text())


def build_server(tmp_path, tmdb_client):
    db_path = tmp_path / "test.db"
    db.connect(db_path).close()
    httpd = ThreadingHTTPServer(
        ("127.0.0.1", 0), tvserver.make_handler(db_path, tmdb_client=tmdb_client))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{httpd.server_address[1]}", db_path, httpd


@pytest.fixture
def srv(tmp_path):
    def fetch(url):
        if "/search/movie" in url:
            return 200, fixture("tmdb/search_movie.json")
        if "/movie/27205" in url:
            return 200, fixture("tmdb/movie_27205.json")
        if "/movie/404404" in url:
            return 404, None
        raise AssertionError(f"unexpected URL: {url}")

    base, db_path, httpd = build_server(
        tmp_path, tmdb.TMDBClient(api_key="testkey", fetch=fetch))
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


def test_movies_page_sections(srv):
    base, db_path = srv
    conn = db.connect(db_path)
    a = db.upsert_movie(conn, tmdb_id=1, title="To Watch & Enjoy", year=2024)
    b = db.upsert_movie(conn, tmdb_id=2, title="Seen It", year=2020, runtime_min=101)
    db.set_movie_watched(conn, b, True)
    conn.close()
    _, body = get(base + "/movies")
    page = body.decode()
    assert "Watchlist (1)" in page and "Watched (1)" in page
    assert "To Watch &amp; Enjoy (2024)" in page
    assert "Seen It (2020)" in page and "101 min" in page
    assert f"/api/movies/{a}/watch" in page and f"/api/movies/{a}/delete" in page
    assert f"/api/movies/{b}/unwatch" in page


def test_search_proxy_trims_and_flags_added(srv):
    base, db_path = srv
    conn = db.connect(db_path)
    db.upsert_movie(conn, tmdb_id=27205, title="Inception")
    conn.close()
    _, body = get(base + "/api/search/movies?q=inception")
    results = json.loads(body)["results"]
    assert [m["tmdb_id"] for m in results] == [27205, 64956, 12345]
    assert results[0]["already_added"] is True
    assert results[1]["already_added"] is False
    assert results[0]["poster_url"].startswith("https://image.tmdb.org/t/p/w342/")
    assert "overview" not in results[0]  # trimmed shape


def test_search_proxy_requires_query(srv):
    base, _ = srv
    with pytest.raises(urllib.error.HTTPError) as exc:
        get(base + "/api/search/movies?q=")
    assert exc.value.code == 400


def test_add_movie_uses_detail_for_runtime_and_is_idempotent(srv):
    base, db_path = srv
    code, payload = post(base + "/api/movies", {"tmdb_id": 27205})
    assert code == 200
    conn = db.connect(db_path)
    movie = db.get_movie(conn, payload["movie_id"])
    assert movie["title"] == "Inception"
    assert movie["runtime_min"] == 148     # from /movie/:id, not search
    assert movie["status"] == "watchlist"
    conn.close()

    post(base + "/api/movies", {"tmdb_id": 27205})
    conn = db.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM movies").fetchone()[0] == 1
    conn.close()


def test_add_movie_validation_and_404(srv):
    base, _ = srv
    with pytest.raises(urllib.error.HTTPError) as exc:
        post(base + "/api/movies", {"tmdb_id": "27205"})
    assert exc.value.code == 400
    with pytest.raises(urllib.error.HTTPError) as exc:
        post(base + "/api/movies", {"tmdb_id": 404404})
    assert exc.value.code == 404


def test_movie_watch_unwatch_delete_roundtrip(srv):
    base, db_path = srv
    _, payload = post(base + "/api/movies", {"tmdb_id": 27205})
    mid = payload["movie_id"]
    post(base + f"/api/movies/{mid}/watch")
    conn = db.connect(db_path)
    assert db.get_movie(conn, mid)["status"] == "watched"
    conn.close()
    post(base + f"/api/movies/{mid}/unwatch")
    conn = db.connect(db_path)
    assert db.get_movie(conn, mid)["status"] == "watchlist"
    conn.close()
    post(base + f"/api/movies/{mid}/delete")
    conn = db.connect(db_path)
    assert db.get_movie(conn, mid) is None
    conn.close()
    with pytest.raises(urllib.error.HTTPError) as exc:
        post(base + f"/api/movies/{mid}/watch")
    assert exc.value.code == 404


def test_search_without_key_returns_503_with_instructions(tmp_path):
    def no_fetch(url):
        raise AssertionError("network should not be reached without a key")

    def no_key():
        raise tmdb.TMDBKeyMissing("No TMDB API key: see themoviedb.org")

    client = tmdb.TMDBClient(api_key=None, fetch=no_fetch, key_loader=no_key)
    base, _, httpd = build_server(tmp_path, client)
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            get(base + "/api/search/movies?q=inception")
        assert exc.value.code == 503
        assert "TMDB API key" in json.loads(exc.value.read())["error"]
    finally:
        httpd.shutdown()
        httpd.server_close()
