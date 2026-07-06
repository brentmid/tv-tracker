#!/usr/bin/env python3
"""Seed a dev database with synthetic shows/episodes/movies for UI work.

Usage:
    .venv/bin/python scripts/dev-seed.py [db_path]

Defaults to baselines/tvtracker.db. Refuses to touch a DB that already
has shows unless --force is given (protects the real imported history).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tvtracker import db  # noqa: E402

# (tvmaze_id, name, tvmaze_status, episodes: (tvmaze_ep_id, season, number,
#  name, airdate, watched))  — airdates straddle "today" so the queue,
# waiting section, and unaired filtering all light up.
SHOWS = [
    (1001, "Backlog Mountain", "Ended", [
        (90101, 1, 1, "Pilot", "2024-01-01", True),
        (90102, 1, 2, "The Climb", "2024-01-08", False),
        (90103, 1, 3, "Summit", "2024-01-15", False),
        (90104, 2, 1, "Descent", "2024-06-01", False),
    ]),
    (1002, "Currently Airing", "Running", [
        (90201, 1, 1, "First", "2026-06-22", True),
        (90202, 1, 2, "Second", "2026-06-29", False),
        (90203, 1, 3, "Third (future)", "2026-12-01", False),
    ]),
    (1003, "Waiting Room", "Running", [
        (90301, 1, 1, "Done", "2026-01-05", True),
        (90302, 2, 1, "Next Season (future)", "2026-11-20", False),
    ]),
    (1004, "No Dates Yet", "In Development", [
        (90401, 1, 1, "TBA", None, False),
    ]),
    (1005, "Old Favorite", "Ended", [
        (90501, 1, 1, "Only Episode", "2019-03-03", True),
    ]),
]

ARCHIVED = [
    (1006, "Gave Up On This", "Running", [
        (90601, 1, 1, "Meh", "2023-05-05", True),
        (90602, 1, 2, "Still Meh", "2023-05-12", False),
    ]),
]

MOVIES = [
    (501, "Seeded Heist", 2023, 128, "watchlist", None),
    (502, "Watched Already", 2020, 101, "watched", "2026-05-15T20:00:00+00:00"),
    (503, "Another To Watch", 2025, 95, "watchlist", None),
]


def seed(conn) -> None:
    for status, group in (("active", SHOWS), ("archived", ARCHIVED)):
        for tvmaze_id, name, tvmaze_status, episodes in group:
            show_id = db.upsert_show(
                conn, tvmaze_id=tvmaze_id, name=name,
                tvmaze_status=tvmaze_status, runtime_min=42, status=status,
            )
            for ep_id, season, number, ep_name, airdate, watched in episodes:
                eid = db.upsert_episode(
                    conn, show_id=show_id, tvmaze_episode_id=ep_id,
                    season=season, number=number, name=ep_name,
                    airdate=airdate, runtime_min=42,
                )
                if watched:
                    db.set_episode_watched(conn, eid, True)
    for tmdb_id, title, year, runtime, status, watched_at in MOVIES:
        db.upsert_movie(
            conn, tmdb_id=tmdb_id, title=title, year=year, runtime_min=runtime,
            status=status, watched_at=watched_at,
        )
    conn.commit()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("db_path", nargs="?",
                        default=REPO_ROOT / "baselines" / "tvtracker.db")
    parser.add_argument("--force", action="store_true",
                        help="seed even if the DB already has shows")
    args = parser.parse_args()

    conn = db.connect(args.db_path)
    existing = conn.execute("SELECT COUNT(*) FROM shows").fetchone()[0]
    if existing and not args.force:
        print(f"{args.db_path} already has {existing} shows — refusing to seed "
              f"(--force to override)", file=sys.stderr)
        return 1
    seed(conn)
    shows = conn.execute("SELECT COUNT(*) FROM shows").fetchone()[0]
    eps = conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
    movies = conn.execute("SELECT COUNT(*) FROM movies").fetchone()[0]
    conn.close()
    print(f"seeded {args.db_path}: {shows} shows, {eps} episodes, {movies} movies")
    return 0


if __name__ == "__main__":
    sys.exit(main())
