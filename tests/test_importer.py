"""Tests for tvtracker/importer.py against the synthetic export fixtures.

Fixture scenario (tests/fixtures/tvtime_export/):
- Game of Thrones (thetvdb 121361): followed+unarchived -> active. Watches
  S01E01 twice (rewatch, latest 2021), S01E02, and S09E09 (numbering
  mismatch — not on TVmaze). Resolves by id to fixture show 82.
- Old Gone Show (555): unfollowed + legacy-archived -> archived. TVmaze
  lookup 404s and search has no candidates -> unmatched.
- Twin Detectives (666): no user-series row -> archived. Lookup 404s,
  search returns two near-identical titles -> ambiguous.
- Movies: Inception watched (watch+follow rows collapse), The Lobster
  watchlist, Mystery Flick (no year) unmatched on TMDB.
"""

import json
import zipfile
from pathlib import Path

import pytest

from tvtracker import db, importer, tmdb, tvmaze

FIXTURES = Path(__file__).resolve().parent / "fixtures"
EXPORT_DIR = FIXTURES / "tvtime_export"
SHOW_82 = json.loads((FIXTURES / "tvmaze/show_82_episodes.json").read_text())
TMDB_SEARCH = json.loads((FIXTURES / "tmdb/search_movie.json").read_text())


class NoSleep:
    def __init__(self):
        self.now = 0.0

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def fake_tvmaze():
    def fetch(url):
        if "thetvdb=121361" in url:
            return 200, SHOW_82
        if "thetvdb=" in url:
            return 404, None
        if "/shows/82?embed=episodes" in url:
            return 200, SHOW_82
        if "/search/shows" in url and "Twin%20Detectives" in url:
            return 200, [
                {"score": 0.9, "show": {"id": 900, "name": "Twin Detectives"}},
                {"score": 0.8, "show": {"id": 901, "name": "Twin Detectives!"}},
            ]
        if "/search/shows" in url:
            return 200, []
        raise AssertionError(f"unexpected TVmaze URL: {url}")

    ns = NoSleep()
    return tvmaze.TVMazeClient(
        fetch=fetch,
        limiter=tvmaze.TokenBucket(clock=ns.clock, sleep=ns.sleep),
        sleep=ns.sleep,
    )


def fake_tmdb():
    def fetch(url):
        if "/search/movie" in url and "query=Inception" in url:
            return 200, TMDB_SEARCH
        if "/search/movie" in url and "query=The%20Lobster" in url:
            return 200, {"results": [{"id": 999, "title": "The Lobster",
                                      "release_date": "2015-10-28",
                                      "poster_path": None}]}
        if "/search/movie" in url:
            return 200, {"results": []}
        raise AssertionError(f"unexpected TMDB URL: {url}")

    return tmdb.TMDBClient(api_key="testkey", fetch=fetch)


# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------

def test_ts_to_iso():
    assert importer.ts_to_iso("2019-09-30 00:40:22") == "2019-09-30T00:40:22+00:00"
    assert importer.ts_to_iso("") is None
    assert importer.ts_to_iso(None) is None


def test_parse_export_shows():
    data = importer.parse_export(EXPORT_DIR)
    assert set(data.shows) == {"121361", "555", "666"}
    assert data.skipped_rows == 1  # the Broken Row entry

    got = data.shows["121361"]
    assert got.name == "Game of Thrones"
    assert got.status == "active"
    assert set(got.watches) == {(1, 1), (1, 2), (9, 9)}
    # rewatch collapses to the LATEST timestamp
    assert got.watches[(1, 1)]["watched_at"] == "2021-05-05T12:00:00+00:00"
    assert len(got.watches[(1, 1)]["raw"]) == 2  # both raw rows retained

    old = data.shows["555"]
    assert old.followed is False
    assert old.archived is True          # legacy followed_tv_show union
    assert old.status == "archived"

    twin = data.shows["666"]             # no user-series row
    assert twin.status == "archived"     # not followed -> archived


def test_parse_export_movies():
    data = importer.parse_export(EXPORT_DIR)
    by_name = {m.name: m for m in data.movies.values()}
    assert set(by_name) == {"Inception", "The Lobster", "Mystery Flick"}

    inception = by_name["Inception"]     # watch + follow rows collapsed
    assert inception.watched is True
    assert inception.watched_at == "2022-03-03T20:00:00+00:00"  # created_at
    assert inception.year == 2010
    assert inception.runtime_min == 148  # 8880 s
    assert len(inception.raws) == 2

    assert by_name["The Lobster"].watched is False
    assert by_name["The Lobster"].runtime_min == 118
    assert by_name["Mystery Flick"].year is None  # 0001 release date


def test_parse_export_from_zip(tmp_path):
    zip_path = tmp_path / "export.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for f in EXPORT_DIR.iterdir():
            zf.write(f, f.name)
    data = importer.parse_export(zip_path)
    assert set(data.shows) == {"121361", "555", "666"}
    assert len(data.movies) == 3


# ---------------------------------------------------------------------------
# Dry-run
# ---------------------------------------------------------------------------

def test_dry_run_offline_validation():
    data = importer.parse_export(EXPORT_DIR)
    report = importer.dry_run(data, tvm=None, progress=lambda *_: None)
    assert report["shows_total"] == 3
    assert report["shows_active"] == 1
    assert report["shows_archived"] == 2
    assert report["episode_watches"] == 5   # 3 GoT + 1 + 1 (rewatch collapsed)
    assert report["movies_total"] == 3
    assert report["movies_watched"] == 1
    assert report["resolution"] is None
    # Twin Detectives: export claims 5 seen, we parsed 1 -> flagged
    assert report["validation"] == [
        {"show": "Twin Detectives", "ours": 1, "tvtime": 5}]


def test_dry_run_online_resolution():
    data = importer.parse_export(EXPORT_DIR)
    report = importer.dry_run(data, tvm=fake_tvmaze(), progress=lambda *_: None)
    res = report["resolution"]
    assert res["id"] == 1
    assert res["fuzzy"] == 0
    assert res["ambiguous"] == 1
    assert res["unmatched"] == 1
    assert {u["name"] for u in res["unresolved_shows"]} == \
        {"Old Gone Show", "Twin Detectives"}
    assert res["episode_mismatches"] == [
        {"show": "Game of Thrones", "missing": [(9, 9)]}]


# ---------------------------------------------------------------------------
# Commit
# ---------------------------------------------------------------------------

def committed_conn(conn):
    data = importer.parse_export(EXPORT_DIR)
    summary = importer.commit(conn, data, fake_tvmaze(), fake_tmdb(),
                              progress=lambda *_: None)
    return conn, summary


def test_commit_shows_and_watches(conn):
    conn, summary = committed_conn(conn)
    assert summary["shows_imported"] == 1
    assert summary["shows_staged"] == 2
    assert summary["episodes_marked"] == 2       # S01E01 + S01E02
    assert summary["episode_mismatches"] == 1    # S09E09

    show = db.get_show_by_tvmaze_id(conn, 82)
    assert show["status"] == "active"
    eps = {(e["season"], e["number"]): e for e in db.list_episodes(conn, show["id"])}
    assert eps[(1, 1)]["watched_at"] == "2021-05-05T12:00:00+00:00"  # latest rewatch
    assert eps[(1, 2)]["watched_at"] == "2019-01-02T10:00:00+00:00"
    assert eps[(1, 3)]["watched_at"] is None     # never watched
    # unresolved shows are NOT in the live tables
    assert len(db.list_shows(conn)) == 1


def test_commit_movies(conn):
    conn, summary = committed_conn(conn)
    assert summary["movies_imported"] == 2       # Inception + The Lobster
    assert summary["movies_staged"] == 1         # Mystery Flick

    movies = {m["title"]: m for m in db.list_movies(conn)}
    assert movies["Inception"]["status"] == "watched"
    assert movies["Inception"]["watched_at"] == "2022-03-03T20:00:00+00:00"
    assert movies["Inception"]["tmdb_id"] == 27205
    assert movies["The Lobster"]["status"] == "watchlist"
    assert movies["The Lobster"]["runtime_min"] == 118  # export runtime kept


def test_commit_staging_rows(conn):
    conn, _ = committed_conn(conn)
    # every raw row persisted: 6 episode + 2 show-state + 1 synthetic show
    # row for 666 (no user-series row) + 6 movie raws
    shows = db.staging_rows_by_note(conn, "thetvdb:121361", "show")
    assert len(shows) == 1 and shows[0]["match_status"] == "matched"
    eps = db.staging_rows_by_note(conn, "thetvdb:121361", "episode")
    assert len(eps) == 4                          # incl. both rewatch raws
    mismatch = [e for e in eps if e["season"] == 9]
    assert mismatch[0]["match_status"] == "unmatched"
    assert json.loads(eps[0]["raw_json"])["key"].startswith("watch-episode-")

    unresolved = db.list_unresolved_staging_shows(conn)
    assert {r["raw_show_name"]: r["match_status"] for r in unresolved} == \
        {"Old Gone Show": "unmatched", "Twin Detectives": "ambiguous"}
    groups = db.list_unresolved_staging_movie_groups(conn)
    assert [g["raw_title"] for g in groups] == ["Mystery Flick"]


def test_commit_is_idempotent(conn):
    conn, _ = committed_conn(conn)
    staging_before = len(db.list_staging(conn))
    shows_before = len(db.list_shows(conn))

    # Brent unwatches an episode between runs; rerun must not re-mark it…
    show = db.get_show_by_tvmaze_id(conn, 82)
    ep = db.find_episode(conn, show["id"], 1, 1)
    db.set_episode_watched(conn, ep["id"], False)

    data = importer.parse_export(EXPORT_DIR)
    importer.commit(conn, data, fake_tvmaze(), fake_tmdb(),
                    progress=lambda *_: None)
    assert len(db.list_staging(conn)) == staging_before  # batch replaced
    assert len(db.list_shows(conn)) == shows_before
    # …wait, the export IS the source of truth on rerun: the watch comes back
    ep = db.find_episode(conn, show["id"], 1, 1)
    assert ep["watched_at"] == "2021-05-05T12:00:00+00:00"


def test_commit_without_tmdb_stages_all_movies(conn):
    data = importer.parse_export(EXPORT_DIR)
    summary = importer.commit(conn, data, fake_tvmaze(), None,
                              progress=lambda *_: None)
    assert summary["movies_imported"] == 0
    assert summary["movies_staged"] == 3
    assert len(db.list_movies(conn)) == 0
