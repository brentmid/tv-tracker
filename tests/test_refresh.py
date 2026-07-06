"""Tests for M7 air-date refresh: changed-fixture upsert preserving watch
state, archived shows frozen, refresh-all + meta timestamp."""

import copy
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
BASE_SHOW = json.loads((FIXTURES / "tvmaze/show_82_episodes.json").read_text())

# The "TVmaze changed things" fixture: S02E01 got scheduled (airdate was ""),
# an episode was retitled, the show ended, and S02E02 is brand new.
CHANGED_SHOW = copy.deepcopy(BASE_SHOW)
CHANGED_SHOW["status"] = "Ended"
for ep in CHANGED_SHOW["_embedded"]["episodes"]:
    if ep["id"] == 4955:
        ep["airdate"] = "2030-06-01"
    if ep["id"] == 4952:
        ep["name"] = "Winter Is Coming (Remastered)"
CHANGED_SHOW["_embedded"]["episodes"].append(
    {"id": 4956, "name": "The New One", "season": 2, "number": 2,
     "type": "regular", "airdate": "2030-06-08", "runtime": 65})


class NoSleep:
    def __init__(self):
        self.now = 0.0

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture
def srv(tmp_path):
    """Server whose fake TVmaze serves BASE_SHOW first, CHANGED_SHOW after."""
    db_path = tmp_path / "test.db"
    db.connect(db_path).close()
    responses = [BASE_SHOW, CHANGED_SHOW]

    def fetch(url):
        assert "/shows/82?embed=episodes" in url, f"unexpected URL: {url}"
        return 200, responses.pop(0) if len(responses) > 1 else responses[0]

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


def post(url, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as res:
        return res.status, json.loads(res.read())


def get_html(url):
    with urllib.request.urlopen(url, timeout=5) as res:
        return res.read().decode()


def wait_until(predicate, timeout=5.0):
    """Poll for an async refresh-all worker to finish."""
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def refresh_all_done(db_path):
    def check():
        conn = db.connect(db_path)
        try:
            return db.get_meta(conn, "last_refresh_all") is not None
        finally:
            conn.close()
    return check


def episode_exists(db_path, show_id, season, number):
    def check():
        conn = db.connect(db_path)
        try:
            return db.find_episode(conn, show_id, season, number) is not None
        finally:
            conn.close()
    return check


def add_and_watch_first(base, db_path):
    """Add the show and mark S01E01 watched; returns (show_row_id, ep_id)."""
    _, payload = post(base + "/api/shows", {"tvmaze_id": 82})
    show_id = payload["show_id"]
    conn = db.connect(db_path)
    ep = db.find_episode(conn, show_id, 1, 1)
    db.set_episode_watched(conn, ep["id"], True, "2020-05-05T00:00:00+00:00")
    conn.close()
    return show_id, ep["id"]


def test_refresh_applies_changes_but_preserves_watched_at(srv):
    base, db_path = srv
    show_id, watched_ep = add_and_watch_first(base, db_path)

    code, payload = post(base + f"/api/shows/{show_id}/refresh")
    assert code == 200
    assert payload["episodes"] == 5  # 4 keyable before + 1 brand new

    conn = db.connect(db_path)
    # watch state untouched by the refresh
    assert (db.get_episode(conn, watched_ep)["watched_at"]
            == "2020-05-05T00:00:00+00:00")
    # metadata changes landed
    assert db.get_episode(conn, watched_ep)["name"] == "Winter Is Coming (Remastered)"
    assert db.find_episode(conn, show_id, 2, 1)["airdate"] == "2030-06-01"
    assert db.find_episode(conn, show_id, 2, 2)["name"] == "The New One"
    show = db.get_show(conn, show_id)
    assert show["tvmaze_status"] == "Ended"
    assert show["last_refreshed_at"] is not None
    # no duplicate rows: 5 keyable episodes exactly
    assert len(db.list_episodes(conn, show_id)) == 5
    conn.close()


def test_refresh_archived_show_409_and_frozen(srv):
    base, db_path = srv
    show_id, _ = add_and_watch_first(base, db_path)
    post(base + f"/api/shows/{show_id}/archive")
    with pytest.raises(urllib.error.HTTPError) as exc:
        post(base + f"/api/shows/{show_id}/refresh")
    assert exc.value.code == 409
    conn = db.connect(db_path)
    # the changed fixture was never applied: S02E02 does not exist
    assert db.find_episode(conn, show_id, 2, 2) is None
    conn.close()


def test_refresh_all_skips_archived_and_sets_meta(srv):
    base, db_path = srv
    show_id, watched_ep = add_and_watch_first(base, db_path)
    post(base + f"/api/shows/{show_id}/archive")

    code, payload = post(base + "/api/refresh-all")
    assert code == 200
    assert payload == {"ok": True, "started": True, "shows": 0}  # all archived
    assert wait_until(refresh_all_done(db_path))

    conn = db.connect(db_path)
    assert db.find_episode(conn, show_id, 2, 2) is None  # still frozen
    conn.close()

    post(base + f"/api/shows/{show_id}/unarchive")
    _, payload = post(base + "/api/refresh-all")
    assert payload["shows"] == 1
    # wait on the actual work product (meta timestamps have second
    # granularity, so back-to-back runs can't be told apart by them)
    assert wait_until(episode_exists(db_path, show_id, 2, 2))
    conn = db.connect(db_path)
    assert (db.get_episode(conn, watched_ep)["watched_at"]
            == "2020-05-05T00:00:00+00:00")
    conn.close()


def test_refresh_all_second_start_while_running_409s(srv, monkeypatch):
    base, db_path = srv
    conn = db.connect(db_path)
    db.set_meta(conn, "refresh_all_started_at", db.utcnow())  # fake in-flight
    conn.close()
    with pytest.raises(urllib.error.HTTPError) as exc:
        post(base + "/api/refresh-all")
    assert exc.value.code == 409
    page = get_html(base + "/")
    assert "refreshing air dates now" in page
    assert "/api/refresh-all" not in page   # button hidden while running


def test_refresh_unknown_show_404(srv):
    base, _ = srv
    with pytest.raises(urllib.error.HTTPError) as exc:
        post(base + "/api/shows/9999/refresh")
    assert exc.value.code == 404


def test_queue_page_shows_refresh_all_button(srv):
    base, db_path = srv
    page = get_html(base + "/")
    assert "/api/refresh-all" in page
    assert "air dates never refreshed" in page
    post(base + "/api/refresh-all")
    assert wait_until(refresh_all_done(db_path))
    assert "air dates refreshed 20" in get_html(base + "/")


def test_show_page_refresh_button_only_when_active(srv):
    base, db_path = srv
    show_id, _ = add_and_watch_first(base, db_path)
    assert f"/api/shows/{show_id}/refresh" in get_html(base + f"/show/{show_id}")
    post(base + f"/api/shows/{show_id}/archive")
    assert f"/api/shows/{show_id}/refresh" not in get_html(base + f"/show/{show_id}")
