"""TVmaze API client (keyless, CC BY-SA — attribution lives in the page footer).

Rate limit: TVmaze allows ~20 calls per 10 seconds per IP. Every call goes
through a token bucket; the module-level `SHARED_LIMITER` is used by default
so the server and any script in the same process can't jointly exceed it.
On a 429 the client sleeps and retries once.

Lookups by TheTVDB id (`/lookup/shows?thetvdb=`) answer with a redirect to
the canonical show URL — the default fetch (urllib) follows redirects
automatically; injected test fetches must behave as if redirects were
followed.

All network goes through one injectable `fetch(url) -> (status, parsed_json)`
so tests run offline against fixture data.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Callable

BASE_URL = "https://api.tvmaze.com"
USER_AGENT = "tv-tracker/1.0 (personal use; +https://github.com/brentmid)"

RATE_LIMIT_CALLS = 20
RATE_LIMIT_PERIOD = 10.0

Fetch = Callable[[str], tuple[int, Any]]


class TVMazeError(Exception):
    """Non-2xx (after the single 429 retry) or malformed response."""


class TokenBucket:
    """Classic token bucket: `capacity` tokens refilled evenly over `period`
    seconds. take() blocks (via the injected sleep) until a token is free.
    Clock and sleep are injectable so tests never actually wait.
    """

    def __init__(
        self,
        capacity: int = RATE_LIMIT_CALLS,
        period: float = RATE_LIMIT_PERIOD,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.capacity = capacity
        self.period = period
        self._clock = clock
        self._sleep = sleep
        self._tokens = float(capacity)
        self._last = clock()

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(
            self.capacity,
            self._tokens + (now - self._last) * (self.capacity / self.period),
        )
        self._last = now

    def take(self) -> None:
        self._refill()
        if self._tokens < 1:
            wait = (1 - self._tokens) * (self.period / self.capacity)
            self._sleep(wait)
            self._refill()
            # After sleeping the full deficit the bucket must have >= 1 token
            # unless the injected clock is frozen (tests) — proceed either way.
        self._tokens = max(0.0, self._tokens - 1)


def _default_fetch(url: str) -> tuple[int, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15) as res:  # follows redirects
            return res.status, json.loads(res.read())
    except urllib.error.HTTPError as e:
        return e.code, None


SHARED_LIMITER = TokenBucket()


class TVMazeClient:
    def __init__(
        self,
        fetch: Fetch | None = None,
        limiter: TokenBucket | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._fetch = fetch or _default_fetch
        self._limiter = limiter if limiter is not None else SHARED_LIMITER
        self._sleep = sleep

    def _get(self, path: str) -> Any:
        url = BASE_URL + path
        self._limiter.take()
        status, data = self._fetch(url)
        if status == 429:  # over the shared IP budget — back off and retry once
            self._sleep(RATE_LIMIT_PERIOD)
            self._limiter.take()
            status, data = self._fetch(url)
        if status == 404:
            return None
        if status != 200:
            raise TVMazeError(f"TVmaze GET {path} -> HTTP {status}")
        return data

    # -- endpoints ---------------------------------------------------------

    def search_shows(self, query: str) -> list[dict]:
        """/search/shows — list of {score, show} dicts, best match first."""
        from urllib.parse import quote

        return self._get(f"/search/shows?q={quote(query)}") or []

    def show_with_episodes(self, tvmaze_id: int) -> dict | None:
        """/shows/:id?embed=episodes — full show record, episodes embedded."""
        return self._get(f"/shows/{tvmaze_id}?embed=episodes")

    def lookup_by_thetvdb(self, thetvdb_id: int) -> dict | None:
        """/lookup/shows?thetvdb= — canonical show record or None."""
        return self._get(f"/lookup/shows?thetvdb={thetvdb_id}")


# ---------------------------------------------------------------------------
# Response → DB-shape helpers (single place that knows TVmaze's field names)
# ---------------------------------------------------------------------------

def show_fields(show: dict) -> dict:
    """Map a TVmaze show record to upsert_show kwargs (minus status)."""
    image = show.get("image") or {}
    return {
        "tvmaze_id": show["id"],
        "name": show.get("name") or f"tvmaze-{show['id']}",
        "tvmaze_status": show.get("status"),
        "runtime_min": show.get("averageRuntime") or show.get("runtime"),
        "image_url": image.get("medium"),
        "premiered": show.get("premiered"),
    }


def episode_fields(episode: dict) -> dict | None:
    """Map an embedded episode record to upsert_episode kwargs (minus show_id).

    Returns None for rows we can't key (missing season/number) — e.g. some
    specials; callers count and report skips rather than failing.
    """
    if episode.get("season") is None or episode.get("number") is None:
        return None
    return {
        "tvmaze_episode_id": episode["id"],
        "season": episode["season"],
        "number": episode["number"],
        "name": episode.get("name"),
        "airdate": episode.get("airdate") or None,
        "runtime_min": episode.get("runtime"),
    }


def embedded_episodes(show: dict) -> list[dict]:
    return (show.get("_embedded") or {}).get("episodes") or []
