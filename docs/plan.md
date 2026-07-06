# tv-tracker — TV Time Replacement (Final Plan)

**Status:** FINAL — approved research + design complete (2026-07-06). If this session dies before implementation starts, resume from this file. Milestone M0 copies this plan into `~/bin/tv-tracker/docs/plan.md`, the verbatim prompt into `docs/original-prompt.md`, and starts `docs/progress.md`; from then on the repo is the source of truth and "keep going" from that directory resumes work.

## Context

TV Time (Whip Media) announced 2026-07-01 that the app shuts down **2026-07-15** and all account data is deleted. Brent uses it to track show/episode watch history and movies. We're building a basic self-hosted replacement on the Mac Studio: a web UI to keep a "Watch Next" queue of shows, check off episodes, add/archive shows, track movies, refresh upcoming air dates, and show watch-history stats — seeded by Brent's TV Time GDPR export.

## Original prompt (verbatim — copy to tv-tracker/docs/original-prompt.md)

> the "TV Time" app is going out of business in about a week. I want to rebuild a basic web interface that lives on this mac studio that will manage my watch history for shows and movies the same way TV Time did. I am able to export my existing data from the app for you to consume to start up. This can be very basic. Keep a queue of the shows I'm watching and let me check off episodes as we go. Add new shows to the queue. Archive shows I stop watching. Maintain a movie list and check them off as i go. Keep the latest release dates for show episodes up to date by searching the web. Give me a stats page for watch history. Do some light research on what TV time does today so we can build a plan. Keep very detailed notes on the plan in a new subdirectory with it's own git repo - name the subdirectory "tv-tracker". Use the same "local webserver" pattern we used for portfolio-agent. Follow conventions and patterns from other projects as needed - claude.md structure, gitignore, etc. I'm on a flaky airplane connection so be sure to keep detailed notes on the local filesystem as we go so we don't lose progress if this session goes down. ask me any clarifying questions - and keep updating local files as we go - i can't stress this enough - if this connection is severed, i want to pick up later by going to that directory and saying "keep going" - make sure to keep this original prompt as well in a local file

## Decisions (Brent, 2026-07-06)

1. **Export file**: ARRIVED — `~/bin/gdpr-data.zip` (601 KB, 52 CSVs), inspected 2026-07-06 (extraction in session scratchpad, delete after use). **M0 must move it to `tv-tracker/baselines/import/gdpr-data.zip` immediately**: the zip contains `access_token.csv`, `refresh_token.csv`, `ip_address.csv` etc., its current `~/bin` root location is NOT covered by the Dropbox-rsync excludes (verified: no matching pattern, and NOT yet mirrored to `~/Dropbox/<hostname>-bin/` as of inspection). `baselines/` is already excluded. Importer stays M9 (last), but is now designed against the REAL format (see "Real export format" below).
2. **Access**: bind localhost + Tailscale IP (portfolio-agent pattern). Plain HTTP; tailnet is the access control. Never 0.0.0.0.
3. **Metadata**: TVmaze (free, keyless) for TV; TMDB (free key — **Brent registers when online**) for movies.
4. **Air-date refresh**: manual button for MVP; daily LaunchAgent (TVmaze `/updates/shows` feed) is post-MVP.
5. Defaults settled without blocking: archived shows are frozen (refresh skips them; unarchive re-enables). Import preserves original watch timestamps into `watched_at` so per-year stats are accurate. Rewatches collapse to latest watch for MVP (raw rows retained in staging, nothing lost).

## Real export format (inspected 2026-07-06 — supersedes all research guesses)

Zip of 52 CSVs. Load-bearing files:

- **`tracking-prod-records-v2.csv`** (8,572 rows) — TWO row kinds distinguished by `key` prefix:
  - `watch-episode-*` rows (8,056): one per watched episode. Fields: `series_name`, `s_id` (**= TheTVDB series id — VERIFIED**: GoT s_id 121361 → TVmaze lookup returns Game of Thrones; Deadwood 72023 → Deadwood), `ep_id`/`episode_id` (= TVDB episode id, verified GoT S1E1 = 3254641), `season_number`+`episode_number` (also `s_no`/`ep_no` duplicates), `created_at` (watch timestamp, present on 100% of rows), `rewatch_count`, `is_special`, `runtime` (seconds).
  - `user-series-*` rows (512): one per show — `series_name`, `s_id`, `is_followed` (498 true), **`is_archived` (22 true — maps directly to our archive feature)**, `is_for_later`, `followed_at` (µs epoch), `ep_watch_count`.
- **`tracking-prod-records.csv`** (1,092 rows) — mixed entities, filter on `entity_type`:
  - `entity_type=movie`: `type=watch` (watched; `watch_date` unix ts on ~302 of 518 watch-type rows — note `type=watch` also includes some `entity_type=episode` rows, must filter), `type=follow` (356) and `type=towatch` (142) = watchlist. Movie fields: `movie_name`, `release_date`, `runtime` (seconds), `country`. **NO external ids for movies** → TMDB name+release-year matching, ambiguous → staging.
- **Supporting/validation files**: `followed_tv_show.csv` (403 rows, legacy: `tv_show_id` = TVDB id, `archived` flag — union its archive flags with v2's), `user_tv_show_data.csv` (509 rows: `nb_episodes_seen` per show — use to validate import counts), `show_seen_episode_latest.csv` (216, latest ep per show), `rewatched_episode.csv` (1), `tv_show_rate.csv`, `ratings-v2-prod-votes.csv` (small).
- Sensitive files in the zip (why it must live under gitignored+rsync-excluded `baselines/`): `access_token.csv`, `refresh_token.csv`, `ip_address.csv`, `device_token.csv`, `user_personal_data.csv`.

**Consequence: importer is ID-based, not fuzzy.** Primary path: per show, TVmaze `GET /lookup/shows?thetvdb=<s_id>` (follow redirect) → exact show; episodes matched by (season, number) within it. Name-fuzzy matching is only the fallback for shows TVmaze can't resolve by TVDB id, and for all movies (TMDB name+year). Volume: 512 show lookups ≈ ~5 min at <2 req/s; 8,056 episode marks are local DB writes.

## Other research findings

- **TVmaze API**: keyless, CC BY-SA (attribution in footer), ~20 calls/10s/IP; `/search/shows`, `/shows/:id?embed=episodes`, `/lookup/shows?thetvdb=` (**verified working from this plane wifi**), `/updates/shows?since=day`. **TMDB**: free key, attribution + "not endorsed" wording required. **TheTVDB**: went paid — skip.
- **Ports in ~/bin**: 8429 messages-icon, 8430 portfolio-agent → **tv-tracker = 8431**.

## Architecture (follows portfolio-agent conventions)

- **Server**: Python stdlib only — `http.server.ThreadingHTTPServer` + `BaseHTTPRequestHandler`, single file `server/server.py`, regex route table, static assets in `server/assets/`. Env: `TV_TRACKER_PORT` (default 8431), `TV_TRACKER_BIND` (default 127.0.0.1; installer substitutes `tailscale ip -4`). `/healthz` JSON route.
- **HTTP client**: stdlib `urllib.request` wrapped in one injectable `_fetch_json(url)` — `requirements.txt` is `pytest` only.
- **Storage**: **SQLite** (stdlib `sqlite3`, WAL, one connection per request) at `baselines/tvtracker.db`. `baselines/` is the ~/bin gitignored runtime-dir convention and is **already rsync-excluded** from the Dropbox mirror. Schema versioned via `PRAGMA user_version`; all SQL lives in `tvtracker/db.py`.
- **Frontend**: server-rendered HTML (`string.Template` in server.py) + one vanilla `app.js` using `fetch()` for POST mutations. Dark theme (OLED), mobile-first (phone over Tailscale is the primary client), no frameworks/CDN. Footer: TVmaze CC BY-SA + TMDB attribution.
- **Shared package** `tvtracker/`: `db.py`, `tvmaze.py` (rate-limited client, injectable fetch), `tmdb.py` (key from `TMDB_API_KEY` env else `baselines/tmdb_api_key`), `matching.py` (name normalization + `difflib.SequenceMatcher`; ≥0.92 auto, 0.75–0.92 ambiguous), `stats.py`.
- **Secrets**: TMDB key in gitignored `baselines/tmdb_api_key`, env-overridable. Never committed.

### SQLite schema (summary — full DDL in docs/plan.md at M0)

- `shows(id, tvmaze_id UNIQUE, name, status 'active'|'archived', tvmaze_status, runtime_min, image_url, premiered, added_at, last_refreshed_at)`
- `episodes(id, show_id FK CASCADE, tvmaze_episode_id UNIQUE, season, number, name, airdate, runtime_min, watched_at NULL=unwatched, UNIQUE(show_id, season, number))`
- `movies(id, tmdb_id UNIQUE, title, year, runtime_min, poster_url, status 'watchlist'|'watched', watched_at, added_at)`
- `import_staging(id, batch_id, kind 'episode'|'movie', raw_show_name, season, number, raw_title, watched_at, raw_json, match_status 'matched'|'ambiguous'|'unmatched'|'resolved'|'skipped', match_confidence, matched_show_id, matched_movie_id, note)`
- `meta(key, value)`
- Watch Next query: per active show, min (season, number) episode with `watched_at IS NULL` and `airdate <= today`; queue sorted by that airdate; unaired-only shows in a "waiting for new episodes" section.

### Routes

Pages (GET): `/` queue · `/show/<id>` detail (per-episode/season/show checkoff, archive, refresh) · `/archive` · `/movies` · `/stats` · `/add` (TVmaze search) · `/import` (resolve unmatched) · `/assets/<f>` · `/healthz`.
API reads (GET): `/api/search/shows?q=` (TVmaze proxy), `/api/search/movies?q=` (TMDB proxy) — server-side proxies avoid CORS + centralize rate limiting.
API mutations (POST, JSON): `/api/shows` {tvmaze_id} add · `/api/shows/<id>/archive|unarchive|refresh` · `/api/refresh-all` · `/api/episodes/<id>/watch|unwatch` · `/api/shows/<id>/watch-season` {season} · `/api/shows/<id>/watch-all` · `/api/movies` {tmdb_id} · `/api/movies/<id>/watch|unwatch|delete` · `/api/import/resolve` {staging_id, tvmaze_id | skip}.
Refresh upserts episodes by `tvmaze_episode_id`, **never clobbering `watched_at`**.

### Directory tree

```
tv-tracker/
├── CLAUDE.md  README.md  requirements.txt  .gitignore
├── docs/          original-prompt.md · plan.md · progress.md (living checklist)
├── server/        server.py · assets/{style.css, app.js}
├── tvtracker/     __init__.py · db.py · tvmaze.py · tmdb.py · matching.py · stats.py
├── scripts/       import-tvtime.py · dev-seed.py · install-launchagents.sh · pre-commit-check.sh
├── launchagents/  net.midwood.tv-tracker.server.plist   (placeholder paths)
├── tests/         conftest.py · fixtures/ (tvmaze/tmdb JSON + synthetic tvtime_export/) · test_{db,matching,clients,server,importer}.py
└── baselines/     GITIGNORED: tvtracker.db · tmdb_api_key · logs/ · import/
```

- `.gitignore`: `.env*`, `*token*.json`, `*credentials*.json`, `*secret*`, `*.key`, `*.pem`, `baselines/`, `.venv/`, `__pycache__/`, `*.log`, `.DS_Store`, `.claude/`, `*.db`.
- `CLAUDE.md` mirrors portfolio-agent sections: Project purpose / domain patterns / Git hygiene rules (Never commit / Safe to commit / Pre-commit hook / Before promoting to public) / Model selection / Patterns this project builds on / Issue tracking (GitHub Issues once repo is pushed).
- `pre-commit-check.sh` copied/trimmed from `portfolio-agent/scripts/pre-commit-check.sh`: reject staged gitignore-matches (`git check-ignore`), credential-ish filenames, then `pytest -q`. Symlink to `.git/hooks/pre-commit`. Never `--no-verify`.
- LaunchAgent plist + `install-launchagents.sh` trimmed from `portfolio-agent/scripts/install-launchagents.sh` (sed placeholders, `tailscale ip -4`, bootout/bootstrap, `--uninstall`). Logs → `baselines/logs/`.

### Importer (`scripts/import-tvtime.py`) — three phases, designed against the real files

1. **inspect** `<zip-or-dir>`: list files, headers, row counts, sample rows (kept as a general sanity tool even though the format is now known).
2. **dry-run** `[--offline]`: parse `tracking-prod-records-v2.csv` (episode watches + per-show summary rows incl. `is_archived`) and `tracking-prod-records.csv` (`entity_type=movie`) → per show, resolve `s_id` via TVmaze `/lookup/shows?thetvdb=` (redirect-following; cache lookups in staging); fallback TVmaze name search + fuzzy for lookup misses; movies via TMDB name+release-year. Report matched/fallback-matched/ambiguous/unmatched + episode (season,number) mismatches vs TVmaze episode lists. `--offline` runs the parse/plan without network. Validate totals against `user_tv_show_data.csv` `nb_episodes_seen`.
3. **commit**: single transaction; all raw rows into `import_staging` (raw_json verbatim); resolved shows inserted with full TVmaze episode lists; `watched_at` from export `created_at`; show `status` from `is_archived` (union with legacy `followed_tv_show.csv` `archived` flag); movie watchlist from `follow`/`towatch`, watched from `watch` rows (`watch_date` when present, else `created_at`). Idempotent. Leftovers resolved in the `/import` page (search box per name, link/skip → `POST /api/import/resolve`).

Shows in the export that are neither followed nor archived but have watch history (511 named series vs 498 followed): import history, mark archived — they were finished/abandoned shows; Brent can unarchive from `/archive`.

## Milestones (each verifiable offline; commit + update docs/progress.md after each)

- **M0 Repo bootstrap**: **FIRST ACTION: `mv ~/bin/gdpr-data.zip ~/bin/tv-tracker/baselines/import/`** (sensitive zip out of rsync-exposed location; verify with rsync dry-run that it's excluded there; also delete the scratchpad extraction). Then `git init`; .gitignore + pre-commit hook + CLAUDE.md + README + docs/{original-prompt,plan,progress}.md in **first commit** (publishable-repo rule). GPG-signed commits (1Password must be unlocked — if signing fails, stop and ask Brent, never `--no-gpg-sign`). Verify: stage a dummy `fake.env` → hook rejects; `baselines/` untracked. Also run the backup-safety checklist (`~/bin/edr/docs/backup-safety-checklist.md`) — `baselines/` + `*.db` under existing rsync excludes; add pattern if inspection says otherwise.
- **M1 venv + DB layer**: `.venv` (Homebrew Python 3.14), `tvtracker/db.py` full schema + queries, `tests/test_db.py` (queue ordering, aired-only filter). Verify: pytest green offline.
- **M2 Server skeleton**: server.py routing, `/healthz`, assets, dark base template + attribution footer; `tests/test_server.py` boots on port 0. Verify: pytest + `curl localhost:8431/healthz`.
- **M3 Core TV UI**: queue page, show detail, watch/unwatch/season/all endpoints, archive/unarchive + page; `scripts/dev-seed.py` fixture seeding. Verify: seed → phone-width browser pass; route tests.
- **M4 API clients + add-show**: tvmaze.py/tmdb.py + rate limiter (20/10s token bucket, retry-once on 429), `/add` page + search proxy + add-show. Verify offline: fixture-driven `test_clients.py` incl. simulated 429. Online later: add one real show.
- **M5 Movies**: `/movies` page, TMDB search, watchlist/watched/delete. Verify: fixtures + seeded UI pass.
- **M6 Stats**: totals, time watched (episode runtime → show runtime → 40-min fallback, stated on page), per-year breakdown, top-10 shows by hours. Verify: unit tests with known expected numbers.
- **M7 Refresh air dates**: per-show + refresh-all + UI buttons; upsert preserves watched_at; `meta` last-refresh. Verify: changed-fixture upsert test.
- **M8 LaunchAgent**: plist template + installer. Verify (needs Tailscale/online): `launchctl print gui/$UID/net.midwood.tv-tracker.server`; `/healthz` from phone over tailnet; `lsof` shows Tailscale-IP bind, not 0.0.0.0. **Kickstart rule applies** — LaunchAgent isn't "working" until kickstarted from the launchd sandbox.
- **M9 Importer (last)**: three-phase CLI + matching.py + fixtures built as SUBSETS OF THE REAL FILES (real headers, a few sanitized rows) + `/import` page. Verify offline: importer test suite + `dry-run --offline` against the real zip (parse/plan without network). Then live: `dry-run` (with TVmaze lookups) → review report → `commit` → resolve stragglers in `/import` → validate counts vs `user_tv_show_data.csv`.
- **M10 Post-MVP (GitHub Issues, not built now)**: daily-refresh LaunchAgent (`/updates/shows`), dated `sqlite3 .backup` script, push to private GitHub (`gh repo create brentmid/tv-tracker --private`) when online, rewatch support.

## Online-required steps

TVmaze is reachable even from the plane wifi (verified), so most API work can proceed. Still deferred until convenient: TMDB key registration (Brent, ~2 min) · GitHub repo creation/push · M8 phone-over-Tailscale verification.

## Risks

1. ~~Export format unknown~~ RESOLVED — real files inspected; importer written against actual columns; raw_json still preserved in staging as backstop.
2. TVDB-id lookup misses on TVmaze (some of the 512 shows won't resolve) → fallback name search + fuzzy + `/import` resolution UI; expect a small manual tail.
3. TV Time vs TVmaze numbering drift (specials, split seasons) → dry-run flags nonexistent (season, number) rather than mis-marking; `is_special` rows handled explicitly.
4. Movies have no external ids → TMDB name+release-year; ambiguous titles land in staging for manual resolution.
5. Import rate limiting: ~512 lookups ≈ ~5 min at <2 req/s — fine for one-time, print progress.
6. SQLite under ThreadingHTTPServer → per-request connections + WAL.

## Verification (end-to-end)

Offline: full pytest suite green; `dev-seed.py` + manual browser pass of every page/mutation at phone width; pre-commit hook rejection test. Online later: add a real show via TVmaze, movie via TMDB, refresh-all against live API, phone access via Tailscale, LaunchAgent kickstart, then the real import.
