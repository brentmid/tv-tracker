# CLAUDE.md — tv-tracker

Guidance for Claude Code when working in this repo.

**Resuming after a dead session:** read `docs/progress.md` first (current state + next step), then `docs/plan.md` (full approved plan). `docs/original-prompt.md` holds the founding requirements verbatim.

## Project purpose

A personal, self-hosted replacement for the TV Time app (shut down 2026-07-15). A small stdlib-Python web app on the Mac Studio tracks Brent's show and movie watch history: a "Watch Next" queue of active shows sorted by next unwatched aired episode, per-episode/season/show check-off, add-show via TVmaze search, archive/unarchive, a movie watchlist, a manual air-date refresh, and a stats page. Seeded from Brent's TV Time GDPR export (`baselines/import/gdpr-data.zip`). Single user, no auth — the Tailscale-only bind is the access control.

## Domain patterns

- **Queue semantics**: a show is `active` or `archived`. The queue shows each active show's earliest unwatched episode whose `airdate <= today`; shows with only unaired episodes remaining appear in a separate "waiting for new episodes" section. Archived shows are frozen: refresh skips them, unarchive re-enables.
- **Episode data is cached in SQLite, not fetched per view.** Network happens only on: add-show, search, explicit refresh, import. Refresh upserts by `tvmaze_episode_id` and must never clobber `watched_at`.
- **All SQL lives in `tvtracker/db.py`** — server and importer share it. One connection per request; WAL mode.
- **External APIs**: TVmaze (keyless; ~20 calls/10s rate limit — use the shared token-bucket limiter in `tvtracker/tvmaze.py`; lookups by TVDB id require following redirects) and TMDB (key from `TMDB_API_KEY` env else `baselines/tmdb_api_key`). Both clients take an injectable `fetch` for offline tests. Keep the TVmaze CC BY-SA + TMDB attribution lines in the page footer.
- **Import staging**: raw export rows persist verbatim (`raw_json`) in `import_staging`; unmatched/ambiguous rows are resolved via the `/import` page, never by hand-editing the DB. The export's `s_id`/`tv_show_id` fields are TheTVDB series ids — resolve via TVmaze `/lookup/shows?thetvdb=`.

## Git hygiene rules (non-negotiable)

**This repo is private today but must stay safe to promote to public at any time.** Git history is immutable — if sensitive data lands in a commit it's there forever.

### Never commit:

- `baselines/` — ALL runtime data: the SQLite DB (real watch history), server logs, the TMDB API key, and above all `baselines/import/gdpr-data.zip`, which contains `access_token.csv`, `refresh_token.csv`, `ip_address.csv`, `user_personal_data.csv` and other sensitive TV Time account data.
- Any file matching `*credentials*`, `*secret*`, `*token*`, `*api_key*`, `.env*`, `*.key`, `*.pem`.
- Test fixtures containing real GDPR-export rows with account data. Fixture rows derived from the export must be reduced to the watch-tracking columns only (names, ids, dates) — never rows from the token/personal-data files.
- The real installed LaunchAgent plist (lives in `~/Library/LaunchAgents/`, not here).

### Safe to commit:

- All code under `server/`, `tvtracker/`, `scripts/`, `tests/`.
- Sanitized fixtures in `tests/fixtures/` (TVmaze/TMDB response JSON is public API data — fine; export-derived fixtures per the rule above).
- Documentation (`README.md`, `CLAUDE.md`, `docs/`).
- The LaunchAgent plist *template* in `launchagents/` with placeholder paths.

### Pre-commit hook

`.git/hooks/pre-commit` → symlink to `scripts/pre-commit-check.sh`. Rejects staged gitignore-matched files, credential-ish filenames, TMDB-key/JWT-looking strings in diffs, references to the sensitive GDPR files, and runs the offline test suite. **If the hook blocks a commit, fix the issue — never bypass with `--no-verify`.**

### Before promoting to public

`gitleaks detect` over full history; grep `git log -p` for key/token patterns; manual review of every tracked file and every fixture.

## Model selection

No `claude -p` / Agent SDK usage in this project (the server is plain Python; metadata comes from TVmaze/TMDB, not an LLM). If that ever changes, default to Opus and budget against the $100/mo Agent SDK credit per `~/bin/CLAUDE.md`.

## Patterns this project builds on

- **portfolio-agent server pattern**: stdlib `http.server` single-file server on a dedicated port (**8431** here; 8429 = messages-icon, 8430 = portfolio-agent), bind `127.0.0.1` by default with the LaunchAgent overriding to the Tailscale IP via `TV_TRACKER_BIND`. Plain HTTP; never bind 0.0.0.0. See `~/bin/portfolio-agent/server/server.py`.
- **portfolio-agent LaunchAgent pattern**: plist template + `scripts/install-launchagents.sh` (sed-substitutes paths and `tailscale ip -4`, launchctl bootout/bootstrap). Verify with `launchctl kickstart` from the real launchd sandbox, per the ~/bin LaunchAgent gotchas.
- **portfolio-agent pre-commit/gitignore pattern**: publishable-from-day-one hygiene; hook + ignore rules in the first commit.
- **Backup safety checklist**: `~/bin/edr/docs/backup-safety-checklist.md`. `baselines/` is covered by the existing Dropbox-rsync excludes (verified by dry-run 2026-07-06). Any NEW runtime file outside `baselines/` needs a fresh check.

## Issue tracking

Not on GitHub yet (built offline on a plane). Post-MVP work is listed in `docs/progress.md` under M10 until the repo is pushed to a private GitHub repo (`gh repo create brentmid/tv-tracker --private`); after that, GitHub Issues is canonical, same label convention as sibling repos (`feature`, `chore`, `bug`, `architecture`, `epic`, `spec`).
