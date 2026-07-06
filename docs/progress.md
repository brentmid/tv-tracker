# tv-tracker progress

Living checklist. **Update after every work chunk** — this file (plus `docs/plan.md`) is how a fresh Claude session resumes with "keep going". Keep newest notes at the top of the Session log.

## Milestones

- [x] Plan approved (2026-07-06) — full plan in `docs/plan.md`
- [x] **M0 Repo bootstrap** — COMPLETE (first commit `397da02`, GPG-signed)
  - [x] `gdpr-data.zip` moved to `baselines/import/` (was rsync-exposed at ~/bin root; dry-run verified excluded in new location; scratchpad extraction deleted)
  - [x] Directory skeleton created
  - [x] docs/{plan,original-prompt,progress}.md written
  - [x] .gitignore, pre-commit hook (executable), CLAUDE.md, README, requirements.txt
  - [x] git init (branch main) + hook symlinked to `.git/hooks/pre-commit` + rejection test passed (fake.env blocked, then unstaged/removed)
  - [x] First commit made 2026-07-06 (was deferred for 1Password; Brent unlocked it). Hook's GDPR-filename rule was scoped to `tests/` diffs only — docs and the hook itself legitimately name those files; rejection re-tested after narrowing.
- [x] **M1 venv + DB layer** — `.venv` (Python 3.14.6), `tvtracker/db.py` (full schema v1 + all queries: shows/episodes upserts, watch/unwatch/season/all, watch_next queue+waiting, movies, import_staging, meta), `tests/{conftest,test_db}.py` — 23 tests green offline. `network` pytest marker registered (hook excludes it).
- [x] **M2 Server skeleton** — `server/server.py` (ThreadingHTTPServer, regex route table, `make_handler(db_path)` closure, per-request DB conn, `render_page()` base template with nav + TVmaze/TMDB attribution footer), `assets/{style.css,app.js}` (dark OLED mobile-first; `post()`/`act()` fetch helpers), `tests/test_server.py` (port-0 boot, healthz, chrome, asset content-types, 404s/traversal). 32 tests green; real boot on 8431 curl-verified.
- [x] **M3 Core TV UI** — real queue page (next-up card + "+N more" badge + waiting section), `/show/<id>` detail (season-grouped episodes, watch/unwatch per episode, mark-season/mark-all, archive/unarchive), `/archive` page, POST endpoints (`/api/episodes/<id>/watch|unwatch`, `/api/shows/<id>/watch-season|watch-all|archive|unarchive`), `scripts/dev-seed.py` (refuses non-empty DB without `--force`). 43 tests green; seeded baselines DB + curl-verified all pages. Found+fixed: `upsert_episode` didn't commit (rollback on close); now commits by default with `commit=False` for bulk import. **Browser pass at phone width still pending Brent's eyeballs** — dev data is seeded, run `.venv/bin/python server/server.py` and open http://127.0.0.1:8431.
- [x] **M4 API clients + add-show** — `tvtracker/tvmaze.py` (TokenBucket 20/10s with injectable clock/sleep, module-level `SHARED_LIMITER`, 429 sleep+retry-once, `show_fields`/`episode_fields`/`embedded_episodes` mappers; unkeyable specials with `number: null` → skipped+counted), `tvtracker/tmdb.py` (key from env else `baselines/tmdb_api_key`, `TMDBKeyMissing` with registration URL, `movie_fields` mapper), fixtures under `tests/fixtures/{tvmaze,tmdb}/`, `/add` page + `/api/search/shows` proxy (flags `already_added`) + `POST /api/shows` (idempotent upsert). 60 tests green. **Live-verified**: real TVmaze search + added Severance (44933, 19 eps) into the seeded dev DB.
- [x] **M5 Movies page** — `/movies` (watchlist + watched sections, inline TMDB search-to-add), `/api/search/movies` proxy (503 + instructions when no key configured), `POST /api/movies` (uses `/movie/:id` detail so runtime lands), `/api/movies/<id>/watch|unwatch|delete`. `TMDBClient` gained injectable `key_loader`. 67 tests green offline. Live TMDB verification pending Brent's API key → `baselines/tmdb_api_key`.
- [x] **M6 Stats page** — `tvtracker/stats.py` (`compute_stats`: totals, TV/movie hours with episode→show→40-min runtime fallback chain, per-year newest-first, top-10 shows by hours; fallback usage disclosed on page; runtime-less movies counted but add no hours), `db.watched_episode_rows()` fetcher (SQL stays in db.py), `/stats` route. Unit tests with hand-computed numbers; 72 tests green.
- [x] **M7 Air-date refresh** — `sync_show_from_tvmaze()` shared by add-show and refresh (batch episode upsert, `touch_show_refreshed`); `POST /api/shows/<id>/refresh` (409 on archived — frozen), `POST /api/refresh-all` (active shows only, per-show error collection, `meta.last_refresh_all`); Refresh button on show page (active only), Refresh-all + last-refresh line on queue. Changed-fixture test proves airdate/name/status updates land, new episodes appear, `watched_at` survives, no dupes. 78 tests green; live per-show refresh verified.
- [x] **M8 LaunchAgent** — `launchagents/net.midwood.tv-tracker.server.plist` (template, placeholder paths + `YOUR_TAILSCALE_IP`), `scripts/install-launchagents.sh` (portfolio-agent pattern: sed substitution, `tailscale ip -4`, bootout/bootstrap, `--uninstall`, plutil lint). Installed + **kickstarted from the real launchd sandbox**: state running, `/healthz` over `<tailscale-ip>:8431` OK, `lsof` shows Tailscale-IP-only bind. Server is now live at http://\<tailscale-ip\>:8431 (phone-over-tailnet check = open it on the phone). Also added tv-tracker to the `~/bin/CLAUDE.md` project index (per Brent).
- [x] **M9 Importer — code + offline verification done; real import pending Brent's go**
  - [x] `tvtracker/matching.py` (normalize/similarity/classify, ≥0.92 auto with near-tie demotion to ambiguous, 0.75–0.92 ambiguous)
  - [x] `tvtracker/importer.py` (parse zip-or-dir, ShowPlan/MoviePlan, rewatch→latest-ts collapse, legacy archive union, status rule followed∧¬archived→active else archived; resolve by TVDB id → fuzzy fallback; `apply_show`/`apply_movie` shared with resolve route; `commit` idempotent via fixed `BATCH_ID` staging replace)
  - [x] **Real-format corrections vs plan**: movie `watch_date` empty on ALL watch rows → `created_at` is the watch ts; timestamps `"YYYY-MM-DD HH:MM:SS"` (treated as UTC); movie types watch/follow/towatch + 1 stray `rewatch_count` row (ignored)
  - [x] `scripts/import-tvtime.py` (inspect / dry-run [--offline] / commit; sensitive files never printed)
  - [x] `/import` page + `POST /api/import/resolve` (show: link tvmaze_id → apply staged watches, or skip-by-note; movie: link tmdb_id or skip)
  - [x] Synthetic fixtures `tests/fixtures/tvtime_export/` (real headers, watch-tracking columns only) — 99 tests green
  - [x] Offline dry-run vs REAL zip: 512 shows (476 active/36 archived), 8,049 unique episode watches, 429 movies (215 watched). 14 nb_episodes_seen validation gaps = TV Time's own stale counters; The Borgias has a counter of 20 but zero watch rows in the export (nothing importable)
  - [ ] Live dry-run (running in background) → review report with Brent
  - [ ] ~~Wipe dev DB~~ APPROVED + DONE 2026-07-06 → `commit` → resolve stragglers at `/import` → validate counts
  - [ ] **TODO after commit (from Brent)**: The Borgias has nb_episodes_seen=20 but ZERO watch rows in the export — Brent confirms he watched the whole series. After the import, add The Borgias via TVmaze (thetvdb id in export s_id; use `/add` or lookup) and mark ALL episodes watched, then archive it (it ended in 2013 and isn't followed).
- [ ] M10 Post-MVP (file as GitHub issues when repo is pushed): daily-refresh LaunchAgent, sqlite backup script, GitHub private repo push, rewatch support

## Waiting on Brent

- TMDB API key (free registration at themoviedb.org when online; ~2 min). Goes in `baselines/tmdb_api_key` (single line, gitignored) — needed before M5 live verification and movie import matching in M9; everything else proceeds without it.

## Key facts (so a fresh session doesn't re-derive them)

- Port **8431**; bind env `TV_TRACKER_BIND` (default 127.0.0.1), port env `TV_TRACKER_PORT`. Plain HTTP; Tailscale = access control.
- DB: SQLite at `baselines/tvtracker.db` (WAL). All SQL in `tvtracker/db.py`.
- Export zip: `baselines/import/gdpr-data.zip`. `s_id`/`tv_show_id` = **TheTVDB series ids** (verified via TVmaze `/lookup/shows?thetvdb=`). Episode watches in `tracking-prod-records-v2.csv` (`key` prefix `watch-episode-`, 8,056 rows, `created_at` = watch ts); per-show state in same file (`key` prefix `user-series-`, 512 rows, `is_archived`/`is_followed`). Movies in `tracking-prod-records.csv` filtered `entity_type=movie` (`watch`=watched with `watch_date` unix ts on ~302/518, `follow`+`towatch`=watchlist); movies have NO external ids → TMDB name+year match.
- TVmaze rate limit ~20 calls/10s; lookups must follow redirects.
- Commits: GPG-signed (1Password must be unlocked; if signing fails, STOP and ask Brent — never --no-gpg-sign). Never --no-verify.

## Session log

- **2026-07-06 (session 2)**: 1Password unlocked → made the deferred M0 first commit (`397da02`; hook false-positive on docs naming GDPR filenames fixed by scoping that rule to `tests/`). Built M1: venv + `tvtracker/db.py` + 23-test suite, all green. Key db.py semantics: upserts preserve watch state (`watched_at`, show `status`, movie `status/watched_at`) while refreshing metadata; `watch_next()` returns `(queue, waiting)` with injectable `as_of`; next-up episode chosen by (season, number) order among aired unwatched, queue sorted oldest pending airdate first. Built M2: server skeleton (route pattern above), 32 tests green, real 8431 boot verified. `/` is a stub page proving template+DB plumbing — M3 replaces it with the real queue UI. Next: M3 core TV UI.
- **2026-07-06 (session 1)**: Researched TV Time shutdown (2026-07-15) + APIs; explored portfolio-agent conventions; plan approved. Export zip arrived mid-flight via AirDrop→laptop; inspected real format (52 CSVs) and confirmed TVDB ids — importer is ID-based, not fuzzy. Moved zip into `baselines/import/`. Started M0.
