"""Unit tests for tvtracker/stats.py with hand-computed expected numbers."""

from tvtracker import db, stats


def seed(conn):
    """Known dataset:

    Show A (show runtime 60): e1 45min watched 2024, e2 no-runtime watched 2024
        -> 45 + 60 (show fallback) = 105 min in 2024
    Show B (no show runtime): e3 no-runtime watched 2025 -> 40 min fallback
    Show C: episode watched=None -> contributes nothing
    Movies: M1 120min watched 2025, M2 no-runtime watched 2025, M3 watchlist
    """
    a = db.upsert_show(conn, tvmaze_id=1, name="Show A", runtime_min=60)
    b = db.upsert_show(conn, tvmaze_id=2, name="Show B")
    c = db.upsert_show(conn, tvmaze_id=3, name="Show C")
    e1 = db.upsert_episode(conn, show_id=a, tvmaze_episode_id=11, season=1,
                           number=1, runtime_min=45)
    db.set_episode_watched(conn, e1, True, "2024-03-01T20:00:00+00:00")
    e2 = db.upsert_episode(conn, show_id=a, tvmaze_episode_id=12, season=1,
                           number=2)
    db.set_episode_watched(conn, e2, True, "2024-03-02T20:00:00+00:00")
    e3 = db.upsert_episode(conn, show_id=b, tvmaze_episode_id=21, season=1,
                           number=1)
    db.set_episode_watched(conn, e3, True, "2025-01-01T20:00:00+00:00")
    db.upsert_episode(conn, show_id=c, tvmaze_episode_id=31, season=1, number=1)

    m1 = db.upsert_movie(conn, tmdb_id=1, title="Movie 1", runtime_min=120)
    db.set_movie_watched(conn, m1, True, "2025-06-01T20:00:00+00:00")
    m2 = db.upsert_movie(conn, tmdb_id=2, title="Movie 2")
    db.set_movie_watched(conn, m2, True, "2025-06-02T20:00:00+00:00")
    db.upsert_movie(conn, tmdb_id=3, title="Movie 3")


def test_compute_stats_known_numbers(conn):
    seed(conn)
    data = stats.compute_stats(conn)

    assert data["episodes_watched"] == 3
    assert data["shows_with_watches"] == 2      # C has no watches
    assert data["movies_watched"] == 2          # M3 still on watchlist
    assert data["tv_minutes"] == 45 + 60 + 40   # ep -> show -> 40 fallback chain
    assert data["movie_minutes"] == 120         # runtime-less movie adds nothing
    assert data["fallback_episode_count"] == 1  # only e3 used the 40-min default
    assert data["movies_without_runtime"] == 1

    # newest year first
    assert [y["year"] for y in data["per_year"]] == ["2025", "2024"]
    y2025, y2024 = data["per_year"]
    assert y2024 == {"year": "2024", "episodes": 2, "minutes": 105, "movies": 0}
    assert y2025 == {"year": "2025", "episodes": 1, "minutes": 40, "movies": 2}

    # top shows by minutes descending
    assert [(s["name"], s["minutes"], s["episodes"]) for s in data["top_shows"]] == [
        ("Show A", 105, 2), ("Show B", 40, 1)]


def test_compute_stats_empty_db(conn):
    data = stats.compute_stats(conn)
    assert data["episodes_watched"] == 0
    assert data["tv_minutes"] == 0
    assert data["per_year"] == []
    assert data["top_shows"] == []


def test_top_shows_caps_at_ten_and_ties_by_name(conn):
    for i in range(12):
        sid = db.upsert_show(conn, tvmaze_id=100 + i, name=f"Show {i:02d}",
                             runtime_min=30)
        eid = db.upsert_episode(conn, show_id=sid, tvmaze_episode_id=1000 + i,
                                season=1, number=1)
        db.set_episode_watched(conn, eid, True, "2024-01-01T00:00:00+00:00")
    data = stats.compute_stats(conn)
    assert len(data["top_shows"]) == 10
    # all equal minutes -> alphabetical
    assert data["top_shows"][0]["name"] == "Show 00"


def test_fmt_hours():
    assert stats.fmt_hours(90) == "1.5 h"
    assert stats.fmt_hours(0) == "0.0 h"
    assert stats.fmt_hours(60 * 100) == "100 h (4.2 days)"


def test_stats_page_renders(conn, tmp_path):
    import sys
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
    import server as tvserver

    db_path = tmp_path / "stats.db"
    c = db.connect(db_path)
    seed(c)
    c.close()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), tvserver.make_handler(db_path))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{httpd.server_address[1]}/stats", timeout=5) as res:
            page = res.read().decode()
        assert "3 episodes" in page
        assert "2 movies" in page
        assert "Top shows by hours" in page
        assert "Show A" in page
        assert "counted as 40 min" in page      # fallback disclosure
        assert "no\nruntime on record" in page or "no runtime on record" in page
    finally:
        httpd.shutdown()
        httpd.server_close()
