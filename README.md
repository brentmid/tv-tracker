# TV Tracker

A self-hosted, single-user replacement for the TV Time app (shut down July 2026): a small, dependency-free Python web app that tracks your TV show and movie watch history, tells you what to watch next, and keeps episode air dates current.

Built in a day from the TV Time GDPR export — if you have yours, the included importer reconstructs your entire watch history with original timestamps.

## Features

- **Watch Next queue** — every show you're mid-way through, with its next unwatched episode one tap from watched. Sortable: recently watched (TV Time's classic default), highest % complete, longest waiting, newest, A–Z.
- **Not started** — shows you follow but haven't begun, sorted by latest release or biggest backlog. One tap starts the show and promotes it to the queue.
- **Finished** — automatically derived, never manually managed: watch the last episode and the show moves here; if new episodes are ever scheduled, it moves back to the queue on the next refresh.
- **Waiting for new episodes** — caught-up-but-airing shows (watched everything aired, future episodes scheduled) sit in their own queue section.
- **Archive** — shows you deliberately stopped watching, sorted by how close you got (highest % watched first).
- **Movies** — watchlist and watched list with TMDB search-to-add and watch dates.
- **Stats** — totals, hours watched (episode → show → 40-min runtime fallback, disclosed when used), per-year history, top 10 shows by hours.
- **Per-episode / season / show check-off**, episode air-date refresh (per show or all), poster art everywhere, client-side filtering on every long list.
- **TV Time importer** — three-phase CLI (`inspect` / `dry-run` / `commit`) that resolves shows by TheTVDB id against TVmaze, matches movies against TMDB by name+year, preserves original watch timestamps, and stages anything ambiguous for one-click resolution in the web UI (`/import`). Idempotent; every raw export row is retained verbatim in a staging table.
- **Home-screen app** — web manifest + apple-touch-icon; add to your phone's home screen and it opens standalone, full-screen, dark.

## Architecture

Deliberately boring: **Python standard library only** at runtime — no framework, no ORM, no build step, no CDN. The only dependency in `requirements.txt` is pytest.

```
server/server.py      ThreadingHTTPServer + regex route table; server-rendered
                      HTML via string.Template; one SQLite connection per request
server/assets/        one CSS file (dark, OLED-black, mobile-first), one JS file
                      (vanilla fetch helpers), icons, manifest
tvtracker/db.py       ALL SQL lives here — schema (PRAGMA user_version versioned,
                      WAL), queries, and the derived queue/not-started/finished
                      state machine
tvtracker/tvmaze.py   TVmaze client: token-bucket rate limiter (20 calls/10 s),
                      sleep-and-retry-once on 429, injectable fetch for offline tests
tvtracker/tmdb.py     TMDB client: key from env or file, injectable fetch
tvtracker/matching.py normalized-name fuzzy matching for importer fallbacks
                      (≥0.92 auto-match, 0.75–0.92 flagged ambiguous)
tvtracker/importer.py GDPR-export parser + resolver + idempotent committer
tvtracker/stats.py    watch-history aggregation
scripts/              importer CLI, LaunchAgent installer, dev seeding,
                      icon generator, pre-commit hook
tests/                offline test suite (fixture-fed fake API clients)
baselines/            gitignored runtime data: SQLite DB, API key, logs, export
```

Key design decisions:

- **"Finished" is derived, not stored.** A show is finished when it's active and has zero unwatched episodes. Watching the last episode finishes it; an air-date refresh that brings new episodes un-finishes it automatically. No state to maintain, no way for it to drift.
- **Refresh never clobbers watch state.** Episode upserts key on the TVmaze episode id and update metadata only — `watched_at` survives every refresh, and re-running the importer can't destroy manual changes.
- **External data is cached, not proxied.** Episode lists live in SQLite; the network is touched only on add-show, search, explicit refresh, and import. Poster images load in the browser directly from the TVmaze/TMDB CDNs.
- **Everything is testable offline.** Both API clients take an injectable `fetch`; the rate limiter takes an injectable clock. The full suite (110 tests) runs with no network.

## Requirements

- Python 3.12+ (developed on 3.14); pytest for the test suite
- A [TMDB API key](https://www.themoviedb.org/settings/api) (free) for movie features — TV-only use works without it
- macOS for the included LaunchAgent installer (the server itself is portable)
- Optional: [Tailscale](https://tailscale.com) for secure phone access (see Security)

## Quick start

```sh
git clone https://github.com/brentmid/tv-tracker.git && cd tv-tracker
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/pytest -m 'not network'     # offline test suite
.venv/bin/python server/server.py     # http://127.0.0.1:8431
```

Add shows via `/add` (TVmaze search, no key needed). For movies, put a TMDB v3 API key in `baselines/tmdb_api_key` (single line) or export `TMDB_API_KEY`.

Environment variables: `TV_TRACKER_PORT` (default 8431), `TV_TRACKER_BIND` (default 127.0.0.1), `TV_TRACKER_DB` (default `baselines/tvtracker.db`).

## Importing your TV Time history

Request your GDPR export from TV Time support, then:

```sh
.venv/bin/python scripts/import-tvtime.py inspect  path/to/gdpr-data.zip
.venv/bin/python scripts/import-tvtime.py dry-run  path/to/gdpr-data.zip   # report only
.venv/bin/python scripts/import-tvtime.py commit   path/to/gdpr-data.zip
```

`dry-run` resolves every show against TVmaze (rate-limited; ~5 min for 500 shows) and reports match quality, episode-numbering mismatches, and count validation against TV Time's own per-show counters before anything is written. `commit` is idempotent — rerunning replaces the staging batch and upserts the rest. Whatever can't be auto-matched (typically a handful of movies) is resolved with two clicks per title at `/import`.

⚠️ The export zip contains far more than watch history — access tokens, your IP history, personal data. Keep it out of version control and backups (here it lives under gitignored `baselines/import/`).

## Deployment (macOS LaunchAgent)

```sh
scripts/install-launchagents.sh        # installs net.midwood.tv-tracker.server
scripts/install-launchagents.sh --uninstall
```

The installer substitutes the repo path and your Tailscale IP into the plist template, lints it, and bootstraps it. The server then runs at boot, restarts on crash, and logs to `baselines/logs/`.

## Security model

**There is no authentication.** The server binds to `127.0.0.1` by default; the LaunchAgent deliberately binds to the machine's **Tailscale IP** instead, so the only things that can reach it are devices authenticated to your tailnet. It is never exposed to the LAN and must not be bound to `0.0.0.0` — if you don't use Tailscale, keep it on localhost or put an authenticating proxy in front.

Runtime data (watch history DB, TMDB key, logs, the GDPR export) lives under gitignored `baselines/`. A pre-commit hook (`scripts/pre-commit-check.sh`) rejects gitignored files, credential-shaped filenames and diff content, and runs the offline test suite.

## Development

```sh
.venv/bin/pytest -m 'not network'                        # full offline suite
.venv/bin/python scripts/dev-seed.py path/to/dev.db      # synthetic data for UI work
ln -s ../../scripts/pre-commit-check.sh .git/hooks/pre-commit
```

## Data sources & attribution

- TV data from [TVmaze](https://www.tvmaze.com), licensed [CC BY-SA](https://creativecommons.org/licenses/by-sa/4.0/).
- Movie data from [TMDB](https://www.themoviedb.org). This product uses the TMDB API but is not endorsed or certified by TMDB.

## License

[MIT](LICENSE)
