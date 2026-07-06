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
- [ ] M2 Server skeleton (`server/server.py`, `/healthz`, assets, `tests/test_server.py`)
- [ ] M3 Core TV UI (queue, show detail, checkoff, archive, `scripts/dev-seed.py`)
- [ ] M4 API clients + add-show (`tvtracker/tvmaze.py`, `tvtracker/tmdb.py`, `/add`)
- [ ] M5 Movies page
- [ ] M6 Stats page (`tvtracker/stats.py`)
- [ ] M7 Air-date refresh (manual buttons)
- [ ] M8 LaunchAgent (template + installer + kickstart verification)
- [ ] M9 Importer (3-phase CLI + `/import` page) → run real import → validate counts
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

- **2026-07-06 (session 2)**: 1Password unlocked → made the deferred M0 first commit (`397da02`; hook false-positive on docs naming GDPR filenames fixed by scoping that rule to `tests/`). Built M1: venv + `tvtracker/db.py` + 23-test suite, all green. Key db.py semantics: upserts preserve watch state (`watched_at`, show `status`, movie `status/watched_at`) while refreshing metadata; `watch_next()` returns `(queue, waiting)` with injectable `as_of`; next-up episode chosen by (season, number) order among aired unwatched, queue sorted oldest pending airdate first. Next: M2 server skeleton.
- **2026-07-06 (session 1)**: Researched TV Time shutdown (2026-07-15) + APIs; explored portfolio-agent conventions; plan approved. Export zip arrived mid-flight via AirDrop→laptop; inspected real format (52 CSVs) and confirmed TVDB ids — importer is ID-based, not fuzzy. Moved zip into `baselines/import/`. Started M0.
