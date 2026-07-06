"""TMDB API client for movies.

Key resolution: `TMDB_API_KEY` env var, else `baselines/tmdb_api_key`
(single line, gitignored). Raises TMDBKeyMissing with instructions if
neither exists — callers surface that to the user rather than crashing.

Attribution requirement: the page footer must state the product uses the
TMDB API but is not endorsed or certified by TMDB (see server template).

Network goes through the same injectable `fetch(url) -> (status, json)`
pattern as tvmaze.py. TMDB's rate limits are generous (~50 rps); no
limiter needed for our single-user volume.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

BASE_URL = "https://api.themoviedb.org/3"
POSTER_BASE = "https://image.tmdb.org/t/p/w342"
KEY_FILE = Path(__file__).resolve().parent.parent / "baselines" / "tmdb_api_key"

Fetch = Callable[[str], tuple[int, Any]]


class TMDBError(Exception):
    """Non-2xx or malformed response."""


class TMDBKeyMissing(TMDBError):
    """No API key configured."""


def load_api_key(env: dict | None = None, key_file: Path = KEY_FILE) -> str:
    env = env if env is not None else os.environ
    key = (env.get("TMDB_API_KEY") or "").strip()
    if key:
        return key
    if key_file.is_file():
        key = key_file.read_text().strip()
        if key:
            return key
    raise TMDBKeyMissing(
        "No TMDB API key: set TMDB_API_KEY or put the key (one line) in "
        f"{key_file}. Free registration: https://www.themoviedb.org/settings/api"
    )


def _default_fetch(url: str) -> tuple[int, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": "tv-tracker/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            return res.status, json.loads(res.read())
    except urllib.error.HTTPError as e:
        return e.code, None


class TMDBClient:
    def __init__(
        self,
        api_key: str | None = None,
        fetch: Fetch | None = None,
        key_loader: Callable[[], str] = load_api_key,
    ):
        self._api_key = api_key  # lazy: only resolved on first request
        self._fetch = fetch or _default_fetch
        self._key_loader = key_loader

    @property
    def api_key(self) -> str:
        if self._api_key is None:
            self._api_key = self._key_loader()
        return self._api_key

    def _get(self, path: str, params: str = "") -> Any:
        url = f"{BASE_URL}{path}?api_key={self.api_key}{params}"
        status, data = self._fetch(url)
        if status == 404:
            return None
        if status != 200:
            raise TMDBError(f"TMDB GET {path} -> HTTP {status}")
        return data

    # -- endpoints ---------------------------------------------------------

    def search_movies(self, query: str, year: int | None = None) -> list[dict]:
        """/search/movie — result list, TMDB's own ranking."""
        params = f"&query={quote(query)}"
        if year:
            params += f"&primary_release_year={year}"
        data = self._get("/search/movie", params)
        return (data or {}).get("results") or []

    def movie(self, tmdb_id: int) -> dict | None:
        """/movie/:id — full record (has runtime, which search results lack)."""
        return self._get(f"/movie/{tmdb_id}")


# ---------------------------------------------------------------------------
# Response → DB-shape helper
# ---------------------------------------------------------------------------

def movie_fields(movie: dict) -> dict:
    """Map a TMDB movie record (search result or /movie/:id) to
    upsert_movie kwargs (minus status/watched_at)."""
    release = movie.get("release_date") or ""
    poster = movie.get("poster_path")
    return {
        "tmdb_id": movie["id"],
        "title": movie.get("title") or f"tmdb-{movie['id']}",
        "year": int(release[:4]) if len(release) >= 4 and release[:4].isdigit() else None,
        "runtime_min": movie.get("runtime"),
        "poster_url": f"{POSTER_BASE}{poster}" if poster else None,
    }
