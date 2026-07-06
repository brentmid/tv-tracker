# tv-tracker

Personal self-hosted replacement for the TV Time app (which shut down 2026-07-15): a small web app on the Mac Studio that tracks show and movie watch history.

## Status

Under construction — see `docs/progress.md` for the live milestone checklist and `docs/plan.md` for the full plan.

## Architecture

- **Server**: single-file stdlib Python HTTP server (`server/server.py`) on port **8431**, bound to localhost + the Tailscale IP (the tailnet is the access control — no LAN exposure, no auth layer). Started by LaunchAgent `net.midwood.tv-tracker.server`.
- **Storage**: SQLite at `baselines/tvtracker.db` (gitignored). All SQL in `tvtracker/db.py`.
- **Metadata**: TVmaze (TV shows/episodes/air dates; keyless, CC BY-SA) and TMDB (movies; free API key in `baselines/tmdb_api_key`).
- **Pages**: `/` Watch Next queue · `/show/<id>` episode checkoff · `/archive` · `/movies` · `/stats` · `/add` search · `/import` resolution.
- **Importer**: `scripts/import-tvtime.py` (inspect / dry-run / commit) seeds the DB from the TV Time GDPR export at `baselines/import/gdpr-data.zip`.

## Setup

```sh
python3.14 -m venv .venv && .venv/bin/pip install -r requirements.txt
ln -s ../../scripts/pre-commit-check.sh .git/hooks/pre-commit
.venv/bin/pytest                      # offline test suite
.venv/bin/python server/server.py    # dev server on 127.0.0.1:8431
scripts/install-launchagents.sh      # install the LaunchAgent (Tailscale bind)
```

TMDB key: register at themoviedb.org → put the v3 API key in `baselines/tmdb_api_key` (single line).

## Repo visibility

Private (not yet pushed). Built to be safely promotable to public: real data lives only under gitignored `baselines/`, enforced by the pre-commit hook. Run the pre-publication audit in `CLAUDE.md` before any visibility change.
