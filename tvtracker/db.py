"""SQLite layer for tv-tracker.

ALL SQL in the project lives here — the server and the importer share these
functions and never write their own statements. One connection per request
(no module-level connection), WAL mode, foreign keys on.

Conventions:
- Timestamps (`watched_at`, `added_at`, ...) are ISO-8601 UTC strings
  ("2026-07-06T14:00:00+00:00"). Airdates are "YYYY-MM-DD" strings or NULL.
  ISO strings compare correctly as text, so date logic is plain SQL.
- `watched_at IS NULL` means unwatched; there is no separate boolean.
- Refresh upserts episodes by `tvmaze_episode_id` and must never clobber
  `watched_at` (see upsert_episode).
"""

from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE shows (
    id                INTEGER PRIMARY KEY,
    tvmaze_id         INTEGER NOT NULL UNIQUE,
    name              TEXT    NOT NULL,
    status            TEXT    NOT NULL DEFAULT 'active'
                      CHECK (status IN ('active', 'archived')),
    tvmaze_status     TEXT,
    runtime_min       INTEGER,
    image_url         TEXT,
    premiered         TEXT,
    added_at          TEXT    NOT NULL,
    last_refreshed_at TEXT
);

CREATE TABLE episodes (
    id                INTEGER PRIMARY KEY,
    show_id           INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
    tvmaze_episode_id INTEGER NOT NULL UNIQUE,
    season            INTEGER NOT NULL,
    number            INTEGER NOT NULL,
    name              TEXT,
    airdate           TEXT,
    runtime_min       INTEGER,
    watched_at        TEXT,
    UNIQUE (show_id, season, number)
);
CREATE INDEX idx_episodes_show ON episodes(show_id, season, number);
CREATE INDEX idx_episodes_unwatched ON episodes(show_id, airdate)
    WHERE watched_at IS NULL;

CREATE TABLE movies (
    id          INTEGER PRIMARY KEY,
    tmdb_id     INTEGER UNIQUE,
    title       TEXT    NOT NULL,
    year        INTEGER,
    runtime_min INTEGER,
    poster_url  TEXT,
    status      TEXT    NOT NULL DEFAULT 'watchlist'
                CHECK (status IN ('watchlist', 'watched')),
    watched_at  TEXT,
    added_at    TEXT    NOT NULL
);

CREATE TABLE import_staging (
    id               INTEGER PRIMARY KEY,
    batch_id         TEXT    NOT NULL,
    kind             TEXT    NOT NULL CHECK (kind IN ('episode', 'movie', 'show')),
    raw_show_name    TEXT,
    season           INTEGER,
    number           INTEGER,
    raw_title        TEXT,
    watched_at       TEXT,
    raw_json         TEXT    NOT NULL,
    match_status     TEXT    NOT NULL DEFAULT 'unmatched'
                     CHECK (match_status IN
                            ('matched', 'ambiguous', 'unmatched', 'resolved', 'skipped')),
    match_confidence REAL,
    matched_show_id  INTEGER REFERENCES shows(id) ON DELETE SET NULL,
    matched_movie_id INTEGER REFERENCES movies(id) ON DELETE SET NULL,
    note             TEXT
);
CREATE INDEX idx_staging_status ON import_staging(match_status);

CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def utcnow() -> str:
    """Current UTC time as the ISO-8601 string format used everywhere in the DB."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def today() -> str:
    """Today's date (local) as YYYY-MM-DD, for airdate comparisons."""
    return datetime.date.today().isoformat()


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a connection (one per request), apply pragmas, ensure schema."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < 1:
        conn.executescript(_SCHEMA)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()


# ---------------------------------------------------------------------------
# Shows
# ---------------------------------------------------------------------------

def upsert_show(
    conn: sqlite3.Connection,
    *,
    tvmaze_id: int,
    name: str,
    tvmaze_status: str | None = None,
    runtime_min: int | None = None,
    image_url: str | None = None,
    premiered: str | None = None,
    status: str = "active",
) -> int:
    """Insert a show or refresh its TVmaze metadata. Returns the show row id.

    On conflict (show already present) the watch-state columns `status` and
    `added_at` are left alone — only metadata from TVmaze is updated.
    """
    now = utcnow()
    conn.execute(
        """
        INSERT INTO shows (tvmaze_id, name, status, tvmaze_status, runtime_min,
                           image_url, premiered, added_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (tvmaze_id) DO UPDATE SET
            name          = excluded.name,
            tvmaze_status = excluded.tvmaze_status,
            runtime_min   = excluded.runtime_min,
            image_url     = excluded.image_url,
            premiered     = excluded.premiered
        """,
        (tvmaze_id, name, status, tvmaze_status, runtime_min, image_url, premiered, now),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM shows WHERE tvmaze_id = ?", (tvmaze_id,)
    ).fetchone()
    return row["id"]


def get_show(conn: sqlite3.Connection, show_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM shows WHERE id = ?", (show_id,)).fetchone()


def get_show_by_tvmaze_id(conn: sqlite3.Connection, tvmaze_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM shows WHERE tvmaze_id = ?", (tvmaze_id,)
    ).fetchone()


def list_shows(conn: sqlite3.Connection, status: str | None = None) -> list[sqlite3.Row]:
    if status is None:
        return conn.execute("SELECT * FROM shows ORDER BY name COLLATE NOCASE").fetchall()
    return conn.execute(
        "SELECT * FROM shows WHERE status = ? ORDER BY name COLLATE NOCASE", (status,)
    ).fetchall()


def set_show_status(conn: sqlite3.Connection, show_id: int, status: str) -> None:
    if status not in ("active", "archived"):
        raise ValueError(f"bad show status: {status}")
    conn.execute("UPDATE shows SET status = ? WHERE id = ?", (status, show_id))
    conn.commit()


def touch_show_refreshed(conn: sqlite3.Connection, show_id: int) -> None:
    conn.execute(
        "UPDATE shows SET last_refreshed_at = ? WHERE id = ?", (utcnow(), show_id)
    )
    conn.commit()


def delete_show(conn: sqlite3.Connection, show_id: int) -> None:
    conn.execute("DELETE FROM shows WHERE id = ?", (show_id,))
    conn.commit()


# ---------------------------------------------------------------------------
# Episodes
# ---------------------------------------------------------------------------

def upsert_episode(
    conn: sqlite3.Connection,
    *,
    show_id: int,
    tvmaze_episode_id: int,
    season: int,
    number: int,
    name: str | None = None,
    airdate: str | None = None,
    runtime_min: int | None = None,
    commit: bool = True,
) -> int:
    """Insert or refresh one episode's metadata. NEVER touches watched_at.

    This is the refresh path: air dates, names and runtimes update freely,
    but watch state survives every refresh. Bulk callers (the importer)
    pass commit=False and commit once per batch.
    """
    conn.execute(
        """
        INSERT INTO episodes (show_id, tvmaze_episode_id, season, number,
                              name, airdate, runtime_min)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (tvmaze_episode_id) DO UPDATE SET
            season      = excluded.season,
            number      = excluded.number,
            name        = excluded.name,
            airdate     = excluded.airdate,
            runtime_min = excluded.runtime_min
        """,
        (show_id, tvmaze_episode_id, season, number, name, airdate, runtime_min),
    )
    if commit:
        conn.commit()
    row = conn.execute(
        "SELECT id FROM episodes WHERE tvmaze_episode_id = ?", (tvmaze_episode_id,)
    ).fetchone()
    return row["id"]


def get_episode(conn: sqlite3.Connection, episode_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM episodes WHERE id = ?", (episode_id,)).fetchone()


def list_episodes(conn: sqlite3.Connection, show_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM episodes WHERE show_id = ? ORDER BY season, number",
        (show_id,),
    ).fetchall()


def find_episode(
    conn: sqlite3.Connection, show_id: int, season: int, number: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM episodes WHERE show_id = ? AND season = ? AND number = ?",
        (show_id, season, number),
    ).fetchone()


def set_episode_watched(
    conn: sqlite3.Connection,
    episode_id: int,
    watched: bool,
    watched_at: str | None = None,
) -> None:
    """Mark one episode watched (with optional explicit timestamp) or unwatched."""
    value = (watched_at or utcnow()) if watched else None
    conn.execute("UPDATE episodes SET watched_at = ? WHERE id = ?", (value, episode_id))
    conn.commit()


def watch_season(conn: sqlite3.Connection, show_id: int, season: int) -> int:
    """Mark every currently-unwatched episode of a season watched. Returns count."""
    cur = conn.execute(
        """
        UPDATE episodes SET watched_at = ?
        WHERE show_id = ? AND season = ? AND watched_at IS NULL
        """,
        (utcnow(), show_id, season),
    )
    conn.commit()
    return cur.rowcount


def watch_all(conn: sqlite3.Connection, show_id: int) -> int:
    """Mark every currently-unwatched episode of a show watched. Returns count."""
    cur = conn.execute(
        "UPDATE episodes SET watched_at = ? WHERE show_id = ? AND watched_at IS NULL",
        (utcnow(), show_id),
    )
    conn.commit()
    return cur.rowcount


# ---------------------------------------------------------------------------
# Watch Next queue
# ---------------------------------------------------------------------------

def watch_next(
    conn: sqlite3.Connection, as_of: str | None = None
) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
    """The home-page queue.

    Returns (queue, waiting):
    - queue: one row per active show that has an unwatched episode already
      aired (airdate <= as_of): the earliest such episode by (season, number),
      plus show columns and the show's unwatched-aired count. Sorted oldest
      airdate first, so the queue surfaces what's been waiting longest; ties
      by show name.
    - waiting: active shows whose unwatched episodes are all unaired (or have
      no airdate), with the next upcoming airdate when known — soonest first.

    `as_of` defaults to today (local); injectable for tests.
    """
    as_of = as_of or today()
    queue = conn.execute(
        """
        SELECT s.*,
               e.id      AS episode_id,
               e.season  AS episode_season,
               e.number  AS episode_number,
               e.name    AS episode_name,
               e.airdate AS episode_airdate,
               (SELECT COUNT(*) FROM episodes e2
                 WHERE e2.show_id = s.id
                   AND e2.watched_at IS NULL
                   AND e2.airdate IS NOT NULL
                   AND e2.airdate <= :as_of) AS unwatched_aired_count
        FROM shows s
        JOIN episodes e ON e.id = (
            SELECT e3.id FROM episodes e3
            WHERE e3.show_id = s.id
              AND e3.watched_at IS NULL
              AND e3.airdate IS NOT NULL
              AND e3.airdate <= :as_of
            ORDER BY e3.season, e3.number
            LIMIT 1
        )
        WHERE s.status = 'active'
        ORDER BY e.airdate, s.name COLLATE NOCASE
        """,
        {"as_of": as_of},
    ).fetchall()

    waiting = conn.execute(
        """
        SELECT s.*,
               (SELECT MIN(e.airdate) FROM episodes e
                 WHERE e.show_id = s.id
                   AND e.watched_at IS NULL
                   AND e.airdate IS NOT NULL
                   AND e.airdate > :as_of) AS next_airdate
        FROM shows s
        WHERE s.status = 'active'
          AND EXISTS (SELECT 1 FROM episodes e
                       WHERE e.show_id = s.id AND e.watched_at IS NULL)
          AND NOT EXISTS (SELECT 1 FROM episodes e
                           WHERE e.show_id = s.id
                             AND e.watched_at IS NULL
                             AND e.airdate IS NOT NULL
                             AND e.airdate <= :as_of)
        ORDER BY next_airdate IS NULL, next_airdate, s.name COLLATE NOCASE
        """,
        {"as_of": as_of},
    ).fetchall()

    return queue, waiting


# ---------------------------------------------------------------------------
# Movies
# ---------------------------------------------------------------------------

def upsert_movie(
    conn: sqlite3.Connection,
    *,
    tmdb_id: int | None,
    title: str,
    year: int | None = None,
    runtime_min: int | None = None,
    poster_url: str | None = None,
    status: str = "watchlist",
    watched_at: str | None = None,
) -> int:
    """Insert a movie or refresh its TMDB metadata. Returns the movie row id.

    Movies without a tmdb_id (unresolved imports) always insert a new row.
    On conflict, watch state (`status`, `watched_at`, `added_at`) is preserved.
    """
    now = utcnow()
    if tmdb_id is None:
        cur = conn.execute(
            """
            INSERT INTO movies (tmdb_id, title, year, runtime_min, poster_url,
                                status, watched_at, added_at)
            VALUES (NULL, ?, ?, ?, ?, ?, ?, ?)
            """,
            (title, year, runtime_min, poster_url, status, watched_at, now),
        )
        conn.commit()
        return cur.lastrowid
    conn.execute(
        """
        INSERT INTO movies (tmdb_id, title, year, runtime_min, poster_url,
                            status, watched_at, added_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (tmdb_id) DO UPDATE SET
            title       = excluded.title,
            year        = excluded.year,
            runtime_min = excluded.runtime_min,
            poster_url  = excluded.poster_url
        """,
        (tmdb_id, title, year, runtime_min, poster_url, status, watched_at, now),
    )
    conn.commit()
    row = conn.execute("SELECT id FROM movies WHERE tmdb_id = ?", (tmdb_id,)).fetchone()
    return row["id"]


def get_movie(conn: sqlite3.Connection, movie_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM movies WHERE id = ?", (movie_id,)).fetchone()


def list_movies(conn: sqlite3.Connection, status: str | None = None) -> list[sqlite3.Row]:
    if status == "watchlist":
        return conn.execute(
            "SELECT * FROM movies WHERE status = 'watchlist' "
            "ORDER BY added_at DESC, title COLLATE NOCASE"
        ).fetchall()
    if status == "watched":
        return conn.execute(
            "SELECT * FROM movies WHERE status = 'watched' "
            "ORDER BY watched_at DESC, title COLLATE NOCASE"
        ).fetchall()
    return conn.execute(
        "SELECT * FROM movies ORDER BY title COLLATE NOCASE"
    ).fetchall()


def set_movie_watched(
    conn: sqlite3.Connection,
    movie_id: int,
    watched: bool,
    watched_at: str | None = None,
) -> None:
    if watched:
        conn.execute(
            "UPDATE movies SET status = 'watched', watched_at = ? WHERE id = ?",
            (watched_at or utcnow(), movie_id),
        )
    else:
        conn.execute(
            "UPDATE movies SET status = 'watchlist', watched_at = NULL WHERE id = ?",
            (movie_id,),
        )
    conn.commit()


def delete_movie(conn: sqlite3.Connection, movie_id: int) -> None:
    conn.execute("DELETE FROM movies WHERE id = ?", (movie_id,))
    conn.commit()


# ---------------------------------------------------------------------------
# Import staging
# ---------------------------------------------------------------------------

def add_staging_row(
    conn: sqlite3.Connection,
    *,
    batch_id: str,
    kind: str,
    raw_json: str,
    raw_show_name: str | None = None,
    season: int | None = None,
    number: int | None = None,
    raw_title: str | None = None,
    watched_at: str | None = None,
    match_status: str = "unmatched",
    match_confidence: float | None = None,
    matched_show_id: int | None = None,
    matched_movie_id: int | None = None,
    note: str | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO import_staging
            (batch_id, kind, raw_show_name, season, number, raw_title, watched_at,
             raw_json, match_status, match_confidence, matched_show_id,
             matched_movie_id, note)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (batch_id, kind, raw_show_name, season, number, raw_title, watched_at,
         raw_json, match_status, match_confidence, matched_show_id,
         matched_movie_id, note),
    )
    return cur.lastrowid


def list_staging(
    conn: sqlite3.Connection, match_status: str | None = None
) -> list[sqlite3.Row]:
    if match_status is None:
        return conn.execute(
            "SELECT * FROM import_staging ORDER BY id"
        ).fetchall()
    return conn.execute(
        "SELECT * FROM import_staging WHERE match_status = ? ORDER BY id",
        (match_status,),
    ).fetchall()


def get_staging_row(conn: sqlite3.Connection, staging_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM import_staging WHERE id = ?", (staging_id,)
    ).fetchone()


def resolve_staging_row(
    conn: sqlite3.Connection,
    staging_id: int,
    *,
    match_status: str,
    matched_show_id: int | None = None,
    matched_movie_id: int | None = None,
    note: str | None = None,
) -> None:
    if match_status not in ("resolved", "skipped", "matched"):
        raise ValueError(f"bad resolution status: {match_status}")
    conn.execute(
        """
        UPDATE import_staging
        SET match_status = ?, matched_show_id = ?, matched_movie_id = ?,
            note = COALESCE(?, note)
        WHERE id = ?
        """,
        (match_status, matched_show_id, matched_movie_id, note, staging_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Stats source rows (aggregation logic lives in tvtracker/stats.py)
# ---------------------------------------------------------------------------

def watched_episode_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """One row per watched episode with everything stats needs: the episode's
    own runtime, the show's runtime (fallback), show identity, and watch time.
    """
    return conn.execute(
        """
        SELECT e.watched_at,
               e.runtime_min      AS episode_runtime_min,
               s.runtime_min      AS show_runtime_min,
               s.id               AS show_id,
               s.name             AS show_name
        FROM episodes e
        JOIN shows s ON s.id = e.show_id
        WHERE e.watched_at IS NOT NULL
        """
    ).fetchall()


# ---------------------------------------------------------------------------
# Meta
# ---------------------------------------------------------------------------

def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()
