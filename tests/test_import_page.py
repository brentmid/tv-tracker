"""Tests for the /import page and POST /api/import/resolve, on a DB
pre-populated by a real (fixture-fed) importer.commit run."""

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
from tvtracker import db, importer, tmdb, tvmaze  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
EXPORT_DIR = FIXTURES / "tvtime_export"
SHOW_82 = json.loads((FIXTURES / "tvmaze/show_82_episodes.json").read_text())

# A plausible TVmaze record for "Old Gone Show" used at resolve time.
OLD_GONE = {
    "id": 555000, "name": "Old Gone Show", "status": "Ended",
    "averageRuntime": 30, "premiered": "2018-01-01", "image": None,
    "_embedded": {"episodes": [
        {"id": 555001, "season": 1, "number": 1, "name": "Only One",
         "airdate": "2018-01-05", "runtime": 30},
        {"id": 555002, "season": 1, "number": 2, "name": "Second",
         "airdate": "2018-01-12", "runtime": 30},
    ]},
}


class NoSleep:
    def __init__(self):
        self.now = 0.0

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def make_clients():
    def tv_fetch(url):
        if "thetvdb=121361" in url:
            return 200, SHOW_82
        if "thetvdb=" in url:
            return 404, None
        if "/shows/82?embed=episodes" in url:
            return 200, SHOW_82
        if "/shows/555000?embed=episodes" in url:
            return 200, OLD_GONE
        if "/search/shows" in url and "Twin" in url:
            return 200, [
                {"score": 0.9, "show": {"id": 900, "name": "Twin Detectives"}},
                {"score": 0.8, "show": {"id": 901, "name": "Twin Detectives!"}},
            ]
        if "/search/shows" in url:
            return 200, []
        raise AssertionError(f"unexpected TVmaze URL: {url}")

    def tmdb_fetch(url):
        if "/movie/424242" in url:
            return 200, {"id": 424242, "title": "Mystery Flick",
                         "release_date": "2019-08-01", "runtime": 91,
                         "poster_path": None}
        if "/search/movie" in url and "query=Inception" in url:
            return 200, json.loads(
                (FIXTURES / "tmdb/search_movie.json").read_text())
        if "/search/movie" in url and "query=The%20Lobster" in url:
            return 200, {"results": [{"id": 999, "title": "The Lobster",
                                      "release_date": "2015-10-28",
                                      "poster_path": None}]}
        if "/search/movie" in url:
            return 200, {"results": []}   # Mystery Flick stays unresolved
        raise AssertionError(f"unexpected TMDB URL: {url}")

    ns = NoSleep()
    tvm = tvmaze.TVMazeClient(
        fetch=tv_fetch,
        limiter=tvmaze.TokenBucket(clock=ns.clock, sleep=ns.sleep),
        sleep=ns.sleep,
    )
    return tvm, tmdb.TMDBClient(api_key="testkey", fetch=tmdb_fetch)


@pytest.fixture
def srv(tmp_path):
    db_path = tmp_path / "test.db"
    tvm, tmdbc = make_clients()
    conn = db.connect(db_path)
    importer.commit(conn, importer.parse_export(EXPORT_DIR), tvm, tmdbc,
                    progress=lambda *_: None)
    conn.close()
    httpd = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        tvserver.make_handler(db_path, tvmaze_client=tvm, tmdb_client=tmdbc))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield base, db_path
    httpd.shutdown()
    httpd.server_close()


def get_html(url):
    with urllib.request.urlopen(url, timeout=5) as res:
        return res.read().decode()


def post(url, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as res:
        return res.status, json.loads(res.read())


def staging_id_for(db_path, name, kind):
    conn = db.connect(db_path)
    rows = (db.list_unresolved_staging_shows(conn) if kind == "show"
            else db.list_unresolved_staging_movie_groups(conn))
    row = next(r for r in rows
               if (r["raw_show_name"] if kind == "show" else r["raw_title"]) == name)
    conn.close()
    return row["id"]


def test_import_page_lists_unresolved(srv):
    base, _ = srv
    page = get_html(base + "/import")
    assert "Shows (2)" in page
    assert "Old Gone Show" in page and "Twin Detectives" in page
    assert "Movies (1)" in page and "Mystery Flick" in page


def test_import_tab_shows_count_everywhere_until_resolved(srv):
    base, db_path = srv
    # 2 unresolved shows + 1 unresolved movie group = 3
    for path in ("/", "/movies", "/stats"):
        page = get_html(base + path)
        assert 'href="/import"' in page
        assert "Import (3)" in page
    assert '<a href="/import" class="active">' in get_html(base + "/import")

    # resolve/skip everything -> tab disappears from the chrome
    post(base + "/api/import/resolve",
         {"staging_id": staging_id_for(db_path, "Old Gone Show", "show"),
          "tvmaze_id": 555000})
    post(base + "/api/import/resolve",
         {"staging_id": staging_id_for(db_path, "Twin Detectives", "show"),
          "skip": True})
    assert "Import (1)" in get_html(base + "/")   # movie group still pending
    post(base + "/api/import/resolve",
         {"staging_id": staging_id_for(db_path, "Mystery Flick", "movie"),
          "tmdb_id": 424242})
    page = get_html(base + "/")
    assert 'href="/import"' not in page


def test_resolve_show_applies_watches_and_status(srv):
    base, db_path = srv
    sid = staging_id_for(db_path, "Old Gone Show", "show")
    code, payload = post(base + "/api/import/resolve",
                         {"staging_id": sid, "tvmaze_id": 555000})
    assert code == 200
    assert payload["applied"] == 1
    assert payload["missing"] == []

    conn = db.connect(db_path)
    show = db.get_show_by_tvmaze_id(conn, 555000)
    assert show is not None
    assert show["status"] == "archived"   # unfollowed + legacy archived
    ep = db.find_episode(conn, show["id"], 1, 1)
    assert ep["watched_at"] == "2018-06-01T09:00:00+00:00"
    assert db.find_episode(conn, show["id"], 1, 2)["watched_at"] is None
    # every staging row for that show got resolved
    assert all(r["match_status"] == "resolved"
               for r in db.staging_rows_by_note(conn, "thetvdb:555"))
    conn.close()
    assert "Old Gone Show" not in get_html(base + "/import")


def test_resolve_movie(srv):
    base, db_path = srv
    sid = staging_id_for(db_path, "Mystery Flick", "movie")
    code, payload = post(base + "/api/import/resolve",
                         {"staging_id": sid, "tmdb_id": 424242})
    assert code == 200
    conn = db.connect(db_path)
    movie = db.get_movie(conn, payload["movie_id"])
    assert movie["title"] == "Mystery Flick"
    assert movie["status"] == "watchlist"    # towatch row, never watched
    assert movie["runtime_min"] == 91
    assert all(r["match_status"] == "resolved"
               for r in db.staging_rows_by_note(conn, movie_note(conn, payload)))
    conn.close()


def movie_note(conn, payload):
    row = conn.execute(
        "SELECT note FROM import_staging WHERE matched_movie_id = ?",
        (payload["movie_id"],)).fetchone()
    return row["note"]


def test_skip_show_marks_all_rows(srv):
    base, db_path = srv
    sid = staging_id_for(db_path, "Twin Detectives", "show")
    code, payload = post(base + "/api/import/resolve",
                         {"staging_id": sid, "skip": True})
    assert code == 200
    assert payload["skipped_rows"] == 2   # show row + its 1 episode row
    conn = db.connect(db_path)
    assert all(r["match_status"] == "skipped"
               for r in db.staging_rows_by_note(conn, "thetvdb:666"))
    assert db.get_show_by_tvmaze_id(conn, 900) is None  # nothing imported
    conn.close()


def test_resolve_validation_errors(srv):
    base, db_path = srv
    with pytest.raises(urllib.error.HTTPError) as exc:
        post(base + "/api/import/resolve", {"staging_id": 999999, "skip": True})
    assert exc.value.code == 404

    sid = staging_id_for(db_path, "Old Gone Show", "show")
    with pytest.raises(urllib.error.HTTPError) as exc:
        post(base + "/api/import/resolve", {"staging_id": sid})  # no id, no skip
    assert exc.value.code == 400

    post(base + "/api/import/resolve", {"staging_id": sid, "skip": True})
    with pytest.raises(urllib.error.HTTPError) as exc:  # already resolved
        post(base + "/api/import/resolve", {"staging_id": sid, "skip": True})
    assert exc.value.code == 409
