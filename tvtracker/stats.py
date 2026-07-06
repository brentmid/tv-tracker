"""Watch-history stats.

Aggregates the rows db.py hands over; no SQL here. Runtime for an episode
resolves as: the episode's own runtime, else the show's average runtime,
else EPISODE_FALLBACK_MIN — the stats page states this fallback whenever
it was used. Movie hours only count movies whose runtime is known; the
page states how many were skipped.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict

from tvtracker import db

EPISODE_FALLBACK_MIN = 40


def episode_minutes(row: sqlite3.Row) -> tuple[int, bool]:
    """(minutes, used_fallback) for one watched-episode row."""
    if row["episode_runtime_min"]:
        return row["episode_runtime_min"], False
    if row["show_runtime_min"]:
        return row["show_runtime_min"], False
    return EPISODE_FALLBACK_MIN, True


def compute_stats(conn: sqlite3.Connection) -> dict:
    """Everything the stats page renders, as plain data (unit-testable)."""
    episode_rows = db.watched_episode_rows(conn)
    watched_movies = db.list_movies(conn, "watched")

    total_min = 0
    fallback_count = 0
    per_show_min: dict[int, dict] = {}
    per_year = defaultdict(lambda: {"episodes": 0, "minutes": 0, "movies": 0})

    for row in episode_rows:
        minutes, used_fallback = episode_minutes(row)
        total_min += minutes
        fallback_count += used_fallback
        entry = per_show_min.setdefault(
            row["show_id"], {"name": row["show_name"], "episodes": 0,
                             "minutes": 0, "image_url": row["show_image_url"]})
        entry["episodes"] += 1
        entry["minutes"] += minutes
        year = (row["watched_at"] or "")[:4] or "unknown"
        per_year[year]["episodes"] += 1
        per_year[year]["minutes"] += minutes

    movie_min = 0
    movies_without_runtime = 0
    for movie in watched_movies:
        if movie["runtime_min"]:
            movie_min += movie["runtime_min"]
        else:
            movies_without_runtime += 1
        year = (movie["watched_at"] or "")[:4] or "unknown"
        per_year[year]["movies"] += 1

    top_shows = sorted(
        per_show_min.values(), key=lambda e: (-e["minutes"], e["name"]))[:10]

    return {
        "episodes_watched": len(episode_rows),
        "shows_with_watches": len(per_show_min),
        "movies_watched": len(watched_movies),
        "tv_minutes": total_min,
        "movie_minutes": movie_min,
        "fallback_episode_count": fallback_count,
        "movies_without_runtime": movies_without_runtime,
        # newest year first; "unknown" (no timestamp) sorts last
        "per_year": sorted(
            [{"year": y, **v} for y, v in per_year.items()],
            key=lambda e: (e["year"] != "unknown", e["year"]), reverse=True),
        "top_shows": top_shows,
    }


def fmt_hours(minutes: int) -> str:
    """1234 -> \"20.6 h\" (one decimal, days added past 48h)."""
    hours = minutes / 60
    if hours >= 48:
        return f"{hours:,.0f} h ({hours / 24:,.1f} days)"
    return f"{hours:,.1f} h"
