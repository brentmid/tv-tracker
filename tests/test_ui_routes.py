"""Tests for the M3 UI routes: queue, show detail, archive, watch mutations.

Uses the same live-server fixture approach as test_server.py, seeded with
a small known dataset.
"""

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

TODAY_PAST = "2020-01-01"     # always aired
TODAY_FUTURE = "2099-01-01"   # never aired


@pytest.fixture
def srv(tmp_path):
    """Live server + seeded DB. Returns (base_url, db_path, ids dict)."""
    db_path = tmp_path / "test.db"
    conn = db.connect(db_path)
    show = db.upsert_show(conn, tvmaze_id=1, name="Alpha & Sons",
                          tvmaze_status="Running")
    e1 = db.upsert_episode(conn, show_id=show, tvmaze_episode_id=101,
                           season=1, number=1, name="One <b>", airdate=TODAY_PAST)
    e2 = db.upsert_episode(conn, show_id=show, tvmaze_episode_id=102,
                           season=1, number=2, name="Two", airdate=TODAY_PAST)
    e3 = db.upsert_episode(conn, show_id=show, tvmaze_episode_id=103,
                           season=2, number=1, name="Future", airdate=TODAY_FUTURE)
    waiting = db.upsert_show(conn, tvmaze_id=2, name="Waiting Show",
                             tvmaze_status="Running")
    db.upsert_episode(conn, show_id=waiting, tvmaze_episode_id=201,
                      season=1, number=1, name="Later", airdate=TODAY_FUTURE)
    conn.close()

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), tvserver.make_handler(db_path))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    ids = {"show": show, "waiting": waiting, "e1": e1, "e2": e2, "e3": e3}
    yield base, db_path, ids
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


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

def test_queue_page_shows_next_episode_and_waiting_section(srv):
    base, _, ids = srv
    page = get_html(base + "/")
    assert "Alpha &amp; Sons" in page            # escaped show name
    assert "S01E01" in page                       # earliest unwatched aired
    assert "One &lt;b&gt;" in page                # escaped episode name
    assert "+1 more" in page                      # e2 also aired
    assert "Waiting for new episodes" in page
    assert "Waiting Show" in page
    assert f"/api/episodes/{ids['e1']}/watch" in page


def test_show_page_groups_seasons_and_escapes(srv):
    base, _, ids = srv
    page = get_html(base + f"/show/{ids['show']}")
    assert "Alpha &amp; Sons" in page
    assert "Season 1" in page and "Season 2" in page
    assert "0/3 watched" in page
    assert f"/api/shows/{ids['show']}/watch-season" in page
    assert f"/api/shows/{ids['show']}/archive" in page


def test_show_page_404_for_unknown_id(srv):
    base, _, _ = srv
    with pytest.raises(urllib.error.HTTPError) as exc:
        get_html(base + "/show/9999")
    assert exc.value.code == 404


def test_archive_page_empty_then_lists_archived(srv):
    base, _, ids = srv
    assert "No archived shows" in get_html(base + "/archive")
    post(base + f"/api/shows/{ids['show']}/archive")
    page = get_html(base + "/archive")
    assert "Alpha &amp; Sons" in page
    assert f"/api/shows/{ids['show']}/unarchive" in page


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------

def test_watch_unwatch_episode_roundtrip(srv):
    base, db_path, ids = srv
    code, payload = post(base + f"/api/episodes/{ids['e1']}/watch")
    assert code == 200 and payload["ok"]
    conn = db.connect(db_path)
    assert db.get_episode(conn, ids["e1"])["watched_at"] is not None
    conn.close()
    # queue moves on to e2
    assert "S01E02" in get_html(base + "/")
    post(base + f"/api/episodes/{ids['e1']}/unwatch")
    conn = db.connect(db_path)
    assert db.get_episode(conn, ids["e1"])["watched_at"] is None
    conn.close()


def test_watch_episode_404(srv):
    base, _, _ = srv
    with pytest.raises(urllib.error.HTTPError) as exc:
        post(base + "/api/episodes/9999/watch")
    assert exc.value.code == 404


def test_watch_season(srv):
    base, db_path, ids = srv
    code, payload = post(base + f"/api/shows/{ids['show']}/watch-season",
                         {"season": 1})
    assert code == 200 and payload["marked"] == 2
    conn = db.connect(db_path)
    eps = db.list_episodes(conn, ids["show"])
    assert [bool(e["watched_at"]) for e in eps] == [True, True, False]
    conn.close()


def test_watch_season_requires_int_season(srv):
    base, _, ids = srv
    with pytest.raises(urllib.error.HTTPError) as exc:
        post(base + f"/api/shows/{ids['show']}/watch-season", {"season": "1"})
    assert exc.value.code == 400


def test_watch_all_marks_everything_even_unaired(srv):
    base, db_path, ids = srv
    code, payload = post(base + f"/api/shows/{ids['show']}/watch-all")
    assert code == 200 and payload["marked"] == 3
    conn = db.connect(db_path)
    assert all(e["watched_at"] for e in db.list_episodes(conn, ids["show"]))
    conn.close()
    # fully-watched show leaves the queue entirely
    page = get_html(base + "/")
    assert "Alpha &amp; Sons" not in page


def test_archive_removes_from_queue_unarchive_restores(srv):
    base, _, ids = srv
    post(base + f"/api/shows/{ids['show']}/archive")
    assert "Alpha &amp; Sons" not in get_html(base + "/")
    post(base + f"/api/shows/{ids['show']}/unarchive")
    assert "Alpha &amp; Sons" in get_html(base + "/")


def test_archive_404_for_unknown_show(srv):
    base, _, _ = srv
    with pytest.raises(urllib.error.HTTPError) as exc:
        post(base + "/api/shows/9999/archive")
    assert exc.value.code == 404
