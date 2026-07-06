"""Tests for tvtracker/db.py — schema, queue semantics, refresh invariants."""

from tvtracker import db

TODAY = "2026-07-06"


def add_show(conn, tvmaze_id, name, status="active"):
    return db.upsert_show(conn, tvmaze_id=tvmaze_id, name=name, status=status)


def add_ep(conn, show_id, tvmaze_episode_id, season, number,
           airdate=None, watched_at=None):
    ep_id = db.upsert_episode(
        conn, show_id=show_id, tvmaze_episode_id=tvmaze_episode_id,
        season=season, number=number, airdate=airdate,
    )
    if watched_at:
        db.set_episode_watched(conn, ep_id, True, watched_at)
    return ep_id


# ---------------------------------------------------------------------------
# Schema / connection basics
# ---------------------------------------------------------------------------

def test_connect_applies_schema_and_wal(conn):
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"shows", "episodes", "movies", "import_staging", "meta"} <= tables


def test_reconnect_does_not_rerun_migration(tmp_path):
    path = tmp_path / "test.db"
    c1 = db.connect(path)
    db.upsert_show(c1, tvmaze_id=1, name="X")
    c1.close()
    c2 = db.connect(path)  # would raise "table exists" if migration reran
    assert db.get_show_by_tvmaze_id(c2, 1)["name"] == "X"
    c2.close()


# ---------------------------------------------------------------------------
# Shows
# ---------------------------------------------------------------------------

def test_upsert_show_updates_metadata_but_preserves_watch_state(conn):
    sid = db.upsert_show(conn, tvmaze_id=42, name="Old Name", tvmaze_status="Running")
    db.set_show_status(conn, sid, "archived")
    sid2 = db.upsert_show(conn, tvmaze_id=42, name="New Name", tvmaze_status="Ended")
    assert sid2 == sid
    show = db.get_show(conn, sid)
    assert show["name"] == "New Name"
    assert show["tvmaze_status"] == "Ended"
    assert show["status"] == "archived"  # upsert must not reactivate


def test_archive_unarchive_roundtrip(conn):
    sid = add_show(conn, 1, "Show")
    db.set_show_status(conn, sid, "archived")
    assert db.get_show(conn, sid)["status"] == "archived"
    db.set_show_status(conn, sid, "active")
    assert db.get_show(conn, sid)["status"] == "active"


def test_list_shows_filters_by_status(conn):
    add_show(conn, 1, "Beta")
    add_show(conn, 2, "alpha")
    sid = add_show(conn, 3, "Gone")
    db.set_show_status(conn, sid, "archived")
    active = db.list_shows(conn, "active")
    assert [s["name"] for s in active] == ["alpha", "Beta"]  # case-insensitive sort
    assert [s["name"] for s in db.list_shows(conn, "archived")] == ["Gone"]
    assert len(db.list_shows(conn)) == 3


def test_delete_show_cascades_to_episodes(conn):
    sid = add_show(conn, 1, "Show")
    add_ep(conn, sid, 100, 1, 1, airdate="2020-01-01")
    db.delete_show(conn, sid)
    assert conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# Episodes: refresh upsert must never clobber watched_at
# ---------------------------------------------------------------------------

def test_upsert_episode_preserves_watched_at(conn):
    sid = add_show(conn, 1, "Show")
    ep_id = add_ep(conn, sid, 100, 1, 1, airdate="2026-01-01",
                   watched_at="2026-01-02T00:00:00+00:00")
    # Refresh comes through with a changed airdate and name.
    ep_id2 = db.upsert_episode(
        conn, show_id=sid, tvmaze_episode_id=100, season=1, number=1,
        name="Retitled", airdate="2026-02-01",
    )
    assert ep_id2 == ep_id
    ep = db.get_episode(conn, ep_id)
    assert ep["airdate"] == "2026-02-01"
    assert ep["name"] == "Retitled"
    assert ep["watched_at"] == "2026-01-02T00:00:00+00:00"


def test_watch_unwatch_episode(conn):
    sid = add_show(conn, 1, "Show")
    ep_id = add_ep(conn, sid, 100, 1, 1)
    db.set_episode_watched(conn, ep_id, True)
    assert db.get_episode(conn, ep_id)["watched_at"] is not None
    db.set_episode_watched(conn, ep_id, False)
    assert db.get_episode(conn, ep_id)["watched_at"] is None


def test_watch_season_only_touches_that_season(conn):
    sid = add_show(conn, 1, "Show")
    add_ep(conn, sid, 100, 1, 1)
    add_ep(conn, sid, 101, 1, 2)
    add_ep(conn, sid, 102, 2, 1)
    already = add_ep(conn, sid, 103, 1, 3, watched_at="2020-01-01T00:00:00+00:00")
    assert db.watch_season(conn, sid, 1) == 2  # not the already-watched one
    eps = db.list_episodes(conn, sid)
    by_key = {(e["season"], e["number"]): e for e in eps}
    assert by_key[(1, 1)]["watched_at"] is not None
    assert by_key[(1, 2)]["watched_at"] is not None
    assert by_key[(2, 1)]["watched_at"] is None
    # pre-existing timestamp untouched
    assert db.get_episode(conn, already)["watched_at"] == "2020-01-01T00:00:00+00:00"


def test_watch_all(conn):
    sid = add_show(conn, 1, "Show")
    add_ep(conn, sid, 100, 1, 1)
    add_ep(conn, sid, 101, 2, 5)
    assert db.watch_all(conn, sid) == 2
    assert all(e["watched_at"] for e in db.list_episodes(conn, sid))


# ---------------------------------------------------------------------------
# Watch Next queue
# ---------------------------------------------------------------------------

def test_queue_picks_earliest_unwatched_aired_episode(conn):
    sid = add_show(conn, 1, "Show")
    add_ep(conn, sid, 100, 1, 1, airdate="2026-01-01",
           watched_at="2026-01-05T00:00:00+00:00")
    add_ep(conn, sid, 101, 1, 2, airdate="2026-02-01")  # <- next up
    add_ep(conn, sid, 102, 1, 3, airdate="2026-03-01")
    queue, waiting = db.watch_next(conn, as_of=TODAY)
    assert len(queue) == 1 and not waiting
    row = queue[0]
    assert (row["episode_season"], row["episode_number"]) == (1, 2)
    assert row["unwatched_aired_count"] == 2


def test_queue_sort_oldest_pending_airdate_first(conn):
    # each show needs one watch to be "started" (else it's on Not started)
    s1 = add_show(conn, 1, "Newer Backlog")
    add_ep(conn, s1, 99, 1, 1, airdate="2026-05-25",
           watched_at="2026-06-01T00:00:00+00:00")
    add_ep(conn, s1, 100, 1, 2, airdate="2026-06-01")
    s2 = add_show(conn, 2, "Older Backlog")
    add_ep(conn, s2, 199, 1, 1, airdate="2023-12-25",
           watched_at="2024-01-01T00:00:00+00:00")
    add_ep(conn, s2, 200, 1, 2, airdate="2024-01-01")
    queue, _ = db.watch_next(conn, as_of=TODAY, sort="oldest")
    assert [r["name"] for r in queue] == ["Older Backlog", "Newer Backlog"]
    queue, _ = db.watch_next(conn, as_of=TODAY, sort="newest")
    assert [r["name"] for r in queue] == ["Newer Backlog", "Older Backlog"]


def test_queue_default_sort_recently_watched_first(conn):
    # TV Time's default: the show most recently watched (that still has
    # unwatched aired episodes) first; never-watched shows last, A-Z.
    a = add_show(conn, 1, "Watched Yesterday")
    add_ep(conn, a, 100, 1, 1, airdate="2026-01-01",
           watched_at="2026-07-05T20:00:00+00:00")
    add_ep(conn, a, 101, 1, 2, airdate="2026-01-08")
    b = add_show(conn, 2, "Watched Last Year")
    add_ep(conn, b, 200, 1, 1, airdate="2025-01-01",
           watched_at="2025-06-01T20:00:00+00:00")
    add_ep(conn, b, 201, 1, 2, airdate="2025-01-08")
    c = add_show(conn, 3, "Never Started")
    add_ep(conn, c, 300, 1, 1, airdate="2020-01-01")
    queue, _ = db.watch_next(conn, as_of=TODAY)  # default sort
    # never-watched shows are NOT in the queue — they live on Not started
    assert [r["name"] for r in queue] == \
        ["Watched Yesterday", "Watched Last Year"]
    assert queue[0]["last_watched_at"] == "2026-07-05T20:00:00+00:00"
    assert [r["name"] for r in db.not_started(conn, as_of=TODAY)] == \
        ["Never Started"]


def test_queue_unknown_sort_falls_back_to_recent(conn):
    a = add_show(conn, 1, "Show")
    add_ep(conn, a, 99, 1, 1, airdate="2020-01-01",
           watched_at="2020-01-02T00:00:00+00:00")
    add_ep(conn, a, 100, 1, 2, airdate="2020-01-08")
    queue, _ = db.watch_next(conn, as_of=TODAY, sort="drop table shows")
    assert len(queue) == 1  # silently falls back, never interpolates input


def test_queue_excludes_unaired_and_undated_episodes(conn):
    # Show whose only unwatched episodes are in the future -> waiting section.
    sid = add_show(conn, 1, "Future Show")
    add_ep(conn, sid, 100, 1, 1, airdate="2026-01-01",
           watched_at="2026-01-02T00:00:00+00:00")
    add_ep(conn, sid, 101, 1, 2, airdate="2026-12-25")
    # Show with only an airdate-less unwatched episode -> also waiting.
    sid2 = add_show(conn, 2, "TBA Show")
    add_ep(conn, sid2, 200, 1, 1, airdate=None)
    queue, waiting = db.watch_next(conn, as_of=TODAY)
    assert not queue
    assert [w["name"] for w in waiting] == ["Future Show", "TBA Show"]
    assert waiting[0]["next_airdate"] == "2026-12-25"
    assert waiting[1]["next_airdate"] is None


def test_queue_boundary_airdate_today_counts_as_aired(conn):
    sid = add_show(conn, 1, "Tonight")
    add_ep(conn, sid, 99, 1, 1, airdate="2026-01-01",
           watched_at="2026-01-02T00:00:00+00:00")
    add_ep(conn, sid, 100, 1, 2, airdate=TODAY)
    queue, waiting = db.watch_next(conn, as_of=TODAY)
    assert len(queue) == 1 and not waiting
    # boundary also holds for the not-started join
    sid2 = add_show(conn, 2, "Tonight Fresh")
    add_ep(conn, sid2, 200, 1, 1, airdate=TODAY)
    assert [r["name"] for r in db.not_started(conn, as_of=TODAY)] == \
        ["Tonight Fresh"]


def test_queue_excludes_archived_and_fully_watched_shows(conn):
    archived = add_show(conn, 1, "Archived")
    add_ep(conn, archived, 100, 1, 1, airdate="2026-01-01")
    db.set_show_status(conn, archived, "archived")
    done = add_show(conn, 2, "Done")
    add_ep(conn, done, 200, 1, 1, airdate="2026-01-01",
           watched_at="2026-01-02T00:00:00+00:00")
    queue, waiting = db.watch_next(conn, as_of=TODAY)
    assert not queue and not waiting
    assert db.not_started(conn, as_of=TODAY) == []  # archived stays out


def test_queue_earliest_by_season_number_not_airdate(conn):
    # Specials/reordered airdates: next-up follows (season, number) order.
    sid = add_show(conn, 1, "Show")
    add_ep(conn, sid, 99, 1, 0, airdate="2025-12-01",
           watched_at="2025-12-02T00:00:00+00:00")  # started
    add_ep(conn, sid, 100, 1, 1, airdate="2026-03-01")  # aired later but is S1E1
    add_ep(conn, sid, 101, 1, 2, airdate="2026-01-01")
    queue, _ = db.watch_next(conn, as_of=TODAY)
    assert (queue[0]["episode_season"], queue[0]["episode_number"]) == (1, 1)


def test_not_started_sorted_by_latest_airdate_desc(conn):
    a = add_show(conn, 1, "Old Finished Show")
    add_ep(conn, a, 100, 1, 1, airdate="2010-01-01")
    add_ep(conn, a, 101, 1, 2, airdate="2012-06-01")   # latest 2012
    b = add_show(conn, 2, "Currently Airing")
    add_ep(conn, b, 200, 1, 1, airdate="2026-06-01")
    add_ep(conn, b, 201, 1, 2, airdate="2026-12-01")   # latest in the future
    c = add_show(conn, 3, "No Dates At All")
    add_ep(conn, c, 300, 1, 1, airdate=None)           # never aired -> excluded
    d = add_show(conn, 4, "Mid Show")
    add_ep(conn, d, 400, 1, 1, airdate="2020-05-05")   # latest 2020
    rows = db.not_started(conn, as_of=TODAY)
    assert [r["name"] for r in rows] == \
        ["Currently Airing", "Mid Show", "Old Finished Show"]
    assert rows[0]["latest_airdate"] == "2026-12-01"
    assert rows[0]["aired_count"] == 1                 # future ep not aired yet
    assert (rows[0]["episode_season"], rows[0]["episode_number"]) == (1, 1)


# ---------------------------------------------------------------------------
# Movies
# ---------------------------------------------------------------------------

def test_movie_watchlist_watch_unwatch(conn):
    mid = db.upsert_movie(conn, tmdb_id=550, title="Fight Club", year=1999)
    assert db.get_movie(conn, mid)["status"] == "watchlist"
    db.set_movie_watched(conn, mid, True, "2026-07-01T00:00:00+00:00")
    m = db.get_movie(conn, mid)
    assert m["status"] == "watched"
    assert m["watched_at"] == "2026-07-01T00:00:00+00:00"
    db.set_movie_watched(conn, mid, False)
    m = db.get_movie(conn, mid)
    assert m["status"] == "watchlist" and m["watched_at"] is None


def test_upsert_movie_by_tmdb_id_preserves_watch_state(conn):
    mid = db.upsert_movie(conn, tmdb_id=550, title="Fight Club")
    db.set_movie_watched(conn, mid, True, "2026-07-01T00:00:00+00:00")
    mid2 = db.upsert_movie(conn, tmdb_id=550, title="Fight Club (1999)", runtime_min=139)
    assert mid2 == mid
    m = db.get_movie(conn, mid)
    assert m["title"] == "Fight Club (1999)"
    assert m["runtime_min"] == 139
    assert m["status"] == "watched"
    assert m["watched_at"] == "2026-07-01T00:00:00+00:00"


def test_movies_without_tmdb_id_always_insert(conn):
    a = db.upsert_movie(conn, tmdb_id=None, title="Obscure Film")
    b = db.upsert_movie(conn, tmdb_id=None, title="Obscure Film")
    assert a != b  # NULL tmdb_ids don't collide


def test_list_movies_by_status_and_delete(conn):
    a = db.upsert_movie(conn, tmdb_id=1, title="A")
    b = db.upsert_movie(conn, tmdb_id=2, title="B")
    db.set_movie_watched(conn, b, True)
    assert [m["id"] for m in db.list_movies(conn, "watchlist")] == [a]
    assert [m["id"] for m in db.list_movies(conn, "watched")] == [b]
    db.delete_movie(conn, a)
    assert len(db.list_movies(conn)) == 1


# ---------------------------------------------------------------------------
# Import staging
# ---------------------------------------------------------------------------

def test_staging_insert_list_resolve(conn):
    sid = add_show(conn, 1, "Show")
    row_id = db.add_staging_row(
        conn, batch_id="batch1", kind="episode", raw_show_name="Shw",
        season=1, number=2, raw_json='{"series_name": "Shw"}',
    )
    conn.commit()
    assert len(db.list_staging(conn, "unmatched")) == 1
    db.resolve_staging_row(conn, row_id, match_status="resolved", matched_show_id=sid)
    row = db.get_staging_row(conn, row_id)
    assert row["match_status"] == "resolved"
    assert row["matched_show_id"] == sid
    assert row["raw_json"] == '{"series_name": "Shw"}'  # raw payload untouched
    assert not db.list_staging(conn, "unmatched")


def test_staging_rejects_bad_resolution_status(conn):
    import pytest
    row_id = db.add_staging_row(conn, batch_id="b", kind="movie", raw_json="{}")
    with pytest.raises(ValueError):
        db.resolve_staging_row(conn, row_id, match_status="unmatched")


# ---------------------------------------------------------------------------
# Meta
# ---------------------------------------------------------------------------

def test_meta_get_set_overwrite(conn):
    assert db.get_meta(conn, "last_refresh") is None
    db.set_meta(conn, "last_refresh", "2026-07-06T00:00:00+00:00")
    db.set_meta(conn, "last_refresh", "2026-07-07T00:00:00+00:00")
    assert db.get_meta(conn, "last_refresh") == "2026-07-07T00:00:00+00:00"
