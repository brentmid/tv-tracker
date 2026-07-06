"""TV Time GDPR-export importer: parse → resolve → commit.

Used by scripts/import-tvtime.py (three-phase CLI) and by the /import
resolve route. Real formats (inspected 2026-07-06, supersede the plan doc
where they disagree):

- tracking-prod-records-v2.csv, key prefix `watch-episode-`: one row per
  watched episode. `s_id` = TheTVDB series id, `season_number`/
  `episode_number` ints-as-strings, `created_at` "YYYY-MM-DD HH:MM:SS".
- same file, key prefix `user-series-`: per-show state, `is_followed`/
  `is_archived`/`is_for_later` "true"/"false".
- tracking-prod-records.csv, `entity_type=movie`: `type` watch|follow|
  towatch (one stray `rewatch_count` row — ignored). `watch_date` is EMPTY
  on every watch row in the real export — `created_at` is the watch time.
  `release_date` "YYYY-MM-DD 00:00:00", "0001-01-01" = unknown. `runtime`
  in seconds. NO external movie ids → TMDB name+year matching.
- followed_tv_show.csv: legacy per-show rows, `archived` "0"/"1" — archive
  flags are unioned with v2's.
- user_tv_show_data.csv: `nb_episodes_seen` per show — validation only.

Show status rule (plan): followed & not archived → active; everything
else with watch history → archived (finished/abandoned shows; Brent can
unarchive from /archive).

Rewatches collapse to the latest watch timestamp per (show, season,
number); every raw row is preserved verbatim in import_staging.
"""

from __future__ import annotations

import csv
import io
import json
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from tvtracker import db, matching, tmdb, tvmaze

V2_FILE = "tracking-prod-records-v2.csv"
V1_FILE = "tracking-prod-records.csv"
LEGACY_FOLLOW_FILE = "followed_tv_show.csv"
VALIDATION_FILE = "user_tv_show_data.csv"
BATCH_ID = "tvtime-gdpr"  # fixed: reruns replace this batch's staging rows


def ts_to_iso(ts: str) -> str | None:
    """'2019-09-30 00:40:22' -> '2019-09-30T00:40:22+00:00' (export is UTC)."""
    ts = (ts or "").strip()
    if not ts:
        return None
    return ts.replace(" ", "T") + "+00:00"


# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------

@dataclass
class ShowPlan:
    s_id: str                      # TheTVDB series id (string as exported)
    name: str
    followed: bool = False
    archived: bool = False
    # (season, number) -> {"watched_at": iso, "raw": [row, ...]}
    watches: dict = field(default_factory=dict)
    state_raw: dict | None = None  # the user-series row, verbatim

    @property
    def status(self) -> str:
        return "active" if self.followed and not self.archived else "archived"


@dataclass
class MoviePlan:
    name: str
    year: int | None
    watched: bool = False
    watched_at: str | None = None
    runtime_min: int | None = None
    raws: list = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{matching.normalize(self.name)}|{self.year or ''}"


@dataclass
class ExportData:
    shows: dict            # s_id -> ShowPlan
    movies: dict           # MoviePlan.key -> MoviePlan
    nb_seen: dict          # s_id -> int (validation)
    skipped_rows: int      # unparseable episode rows (missing season/number)


def _read_csv(source: Path, name: str) -> list[dict]:
    """Rows of one CSV from a zip or an extracted directory."""
    if source.is_dir():
        path = source / name
        if not path.is_file():
            return []
        with open(path, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    with zipfile.ZipFile(source) as zf:
        if name not in zf.namelist():
            return []
        with zf.open(name) as f:
            return list(csv.DictReader(io.TextIOWrapper(f, encoding="utf-8")))


def parse_export(source: str | Path) -> ExportData:
    source = Path(source)
    shows: dict[str, ShowPlan] = {}
    skipped = 0

    def show_for(s_id: str, name: str) -> ShowPlan:
        plan = shows.get(s_id)
        if plan is None:
            plan = shows[s_id] = ShowPlan(s_id=s_id, name=name)
        elif name and not plan.name:
            plan.name = name
        return plan

    for row in _read_csv(source, V2_FILE):
        key = row.get("key") or ""
        if key.startswith("watch-episode-"):
            s_id = (row.get("s_id") or "").strip()
            season_s = (row.get("season_number") or row.get("s_no") or "").strip()
            number_s = (row.get("episode_number") or row.get("ep_no") or "").strip()
            if not (s_id and season_s.isdigit() and number_s.isdigit()):
                skipped += 1
                continue
            plan = show_for(s_id, (row.get("series_name") or "").strip())
            watched_at = ts_to_iso(row.get("created_at") or "")
            slot = plan.watches.setdefault(
                (int(season_s), int(number_s)), {"watched_at": None, "raw": []})
            slot["raw"].append(row)
            if watched_at and (slot["watched_at"] is None
                               or watched_at > slot["watched_at"]):
                slot["watched_at"] = watched_at   # rewatches -> latest
        elif key.startswith("user-series-"):
            s_id = (row.get("s_id") or "").strip()
            if not s_id:
                continue
            plan = show_for(s_id, (row.get("series_name") or "").strip())
            plan.followed = (row.get("is_followed") or "") == "true"
            plan.archived = plan.archived or (row.get("is_archived") or "") == "true"
            plan.state_raw = row

    # Legacy archive flags union in (ids are the same TheTVDB ids).
    for row in _read_csv(source, LEGACY_FOLLOW_FILE):
        s_id = (row.get("tv_show_id") or "").strip()
        if s_id in shows and (row.get("archived") or "") == "1":
            shows[s_id].archived = True

    movies: dict[str, MoviePlan] = {}
    for row in _read_csv(source, V1_FILE):
        if (row.get("entity_type") or "") != "movie":
            continue
        kind = row.get("type") or ""
        if kind not in ("watch", "follow", "towatch"):
            continue  # e.g. the stray 'rewatch_count' row
        name = (row.get("movie_name") or "").strip()
        if not name:
            continue
        release = (row.get("release_date") or "").strip()
        year = None
        if len(release) >= 4 and release[:4].isdigit() and release[:4] != "0001":
            year = int(release[:4])
        runtime_s = (row.get("runtime") or "").strip()
        runtime_min = int(runtime_s) // 60 if runtime_s.isdigit() and runtime_s != "0" else None

        probe = MoviePlan(name=name, year=year)
        plan = movies.setdefault(probe.key, probe)
        plan.raws.append(row)
        if runtime_min and not plan.runtime_min:
            plan.runtime_min = runtime_min
        if kind == "watch":
            plan.watched = True
            watched_at = ts_to_iso(row.get("watch_date") or "") or \
                ts_to_iso(row.get("created_at") or "")
            if watched_at and (plan.watched_at is None
                               or watched_at > plan.watched_at):
                plan.watched_at = watched_at

    nb_seen = {}
    for row in _read_csv(source, VALIDATION_FILE):
        s_id = (row.get("tv_show_id") or "").strip()
        seen = (row.get("nb_episodes_seen") or "").strip()
        if s_id and seen.isdigit():
            nb_seen[s_id] = int(seen)

    return ExportData(shows=shows, movies=movies, nb_seen=nb_seen,
                      skipped_rows=skipped)


# ---------------------------------------------------------------------------
# Resolve (network)
# ---------------------------------------------------------------------------

def resolve_show(plan: ShowPlan, tvm: tvmaze.TVMazeClient):
    """(tvmaze_show | None, method, score). Primary: TheTVDB-id lookup.
    Fallback: name search + fuzzy best_match."""
    show = tvm.lookup_by_thetvdb(int(plan.s_id))
    if show is not None:
        return show, "id", 1.0
    candidates = [(item["show"], item["show"]["name"])
                  for item in tvm.search_shows(plan.name)]
    best, score, status = matching.best_match(plan.name, candidates)
    if status == "matched":
        return best, "fuzzy", score
    return None, status, score  # 'ambiguous' or 'unmatched'


def resolve_movie(plan: MoviePlan, tmdbc: tmdb.TMDBClient):
    """(tmdb_movie | None, status, score) via TMDB name+year search."""
    results = tmdbc.search_movies(plan.name, year=plan.year)
    if not results and plan.year:
        results = tmdbc.search_movies(plan.name)  # year filter too strict
    candidates = [(m, m.get("title") or "") for m in results]
    best, score, status = matching.best_match(plan.name, candidates)
    if status == "matched":
        return best, "matched", score
    return None, status, score


# ---------------------------------------------------------------------------
# Apply (shared with the /import resolve route)
# ---------------------------------------------------------------------------

def apply_show(conn, tvm: tvmaze.TVMazeClient, tvmaze_show: dict,
               plan_status: str, watches: dict) -> dict:
    """Insert one resolved show with its full TVmaze episode list, then mark
    the export's watches. Returns {show_id, applied, missing:[(s,n)...]}.
    """
    full = tvm.show_with_episodes(tvmaze_show["id"])
    if full is None:  # vanished between lookup and fetch — treat as missing
        return {"show_id": None, "applied": 0, "missing": sorted(watches)}
    # upsert_show: fresh inserts take plan_status; on conflict the existing
    # row's status is preserved (deliberate — don't clobber manual choices).
    show_id = db.upsert_show(conn, status=plan_status,
                             **tvmaze.show_fields(full))
    for ep in tvmaze.embedded_episodes(full):
        ep_fields = tvmaze.episode_fields(ep)
        if ep_fields is not None:
            db.upsert_episode(conn, show_id=show_id, commit=False, **ep_fields)
    conn.commit()
    db.touch_show_refreshed(conn, show_id)

    applied, missing = 0, []
    for (season, number), slot in sorted(watches.items()):
        row = db.find_episode(conn, show_id, season, number)
        if row is None:
            missing.append((season, number))
            continue
        if row["watched_at"] is None or \
                (slot["watched_at"] or "") > row["watched_at"]:
            db.set_episode_watched(conn, row["id"], True,
                                   slot["watched_at"] or db.utcnow())
        applied += 1
    return {"show_id": show_id, "applied": applied, "missing": missing}


def apply_movie(conn, movie_fields: dict, plan_watched: bool,
                plan_watched_at: str | None, plan_runtime_min: int | None) -> int:
    """Insert one resolved movie with the export's watch state."""
    if not movie_fields.get("runtime_min") and plan_runtime_min:
        movie_fields = {**movie_fields, "runtime_min": plan_runtime_min}
    movie_id = db.upsert_movie(
        conn,
        status="watched" if plan_watched else "watchlist",
        watched_at=plan_watched_at if plan_watched else None,
        **movie_fields,
    )
    if plan_watched:  # upsert preserves prior state; enforce the export's
        db.set_movie_watched(conn, movie_id, True,
                             plan_watched_at or db.utcnow())
    return movie_id


# ---------------------------------------------------------------------------
# Phases
# ---------------------------------------------------------------------------

def dry_run(data: ExportData, tvm: tvmaze.TVMazeClient | None = None,
            progress=print) -> dict:
    """Report what commit would do. With a client, resolves every show
    against TVmaze (rate-limited, ~5 min for ~500 shows); without (offline),
    reports the parse plan and export-internal validation only."""
    shows = list(data.shows.values())
    report = {
        "shows_total": len(shows),
        "shows_active": sum(1 for s in shows if s.status == "active"),
        "shows_archived": sum(1 for s in shows if s.status == "archived"),
        "episode_watches": sum(len(s.watches) for s in shows),
        "skipped_rows": data.skipped_rows,
        "movies_total": len(data.movies),
        "movies_watched": sum(1 for m in data.movies.values() if m.watched),
        "movies_watchlist": sum(1 for m in data.movies.values() if not m.watched),
        "validation": [],
        "resolution": None,
    }
    # Export-internal validation: our per-show watch count vs TV Time's own
    # nb_episodes_seen (both offline facts; big gaps = parse bug).
    for s in shows:
        expected = data.nb_seen.get(s.s_id)
        if expected is not None and abs(expected - len(s.watches)) > 2:
            report["validation"].append(
                {"show": s.name, "ours": len(s.watches), "tvtime": expected})

    if tvm is not None:
        res = {"id": 0, "fuzzy": 0, "ambiguous": 0, "unmatched": 0,
               "episode_mismatches": []}
        unresolved = []
        for i, plan in enumerate(shows, 1):
            show, method, score = resolve_show(plan, tvm)
            if show is None:
                res[method] += 1
                unresolved.append({"name": plan.name, "s_id": plan.s_id,
                                   "status": method, "score": round(score, 3)})
            else:
                res[method] += 1
                full = tvm.show_with_episodes(show["id"])
                have = {(e["season"], e["number"])
                        for e in tvmaze.embedded_episodes(full or {})
                        if e.get("season") is not None and e.get("number") is not None}
                miss = [sn for sn in plan.watches if sn not in have]
                if miss:
                    res["episode_mismatches"].append(
                        {"show": plan.name, "missing": sorted(miss)})
            if i % 25 == 0:
                progress(f"  resolved {i}/{len(shows)} shows…")
        res["unresolved_shows"] = unresolved
        report["resolution"] = res
    return report


def commit(conn, data: ExportData, tvm: tvmaze.TVMazeClient,
           tmdbc: tmdb.TMDBClient | None, progress=print) -> dict:
    """Import everything: resolved shows/movies land in the live tables,
    every raw row lands in import_staging, leftovers stay unmatched there
    for the /import page. Idempotent: reruns replace the staging batch and
    re-upsert (watch state preserved by the db layer)."""
    conn.execute("DELETE FROM import_staging WHERE batch_id = ?", (BATCH_ID,))

    summary = {"shows_imported": 0, "shows_staged": 0, "episodes_marked": 0,
               "episode_mismatches": 0, "movies_imported": 0,
               "movies_staged": 0}

    shows = list(data.shows.values())
    for i, plan in enumerate(shows, 1):
        note = f"thetvdb:{plan.s_id}"
        show, method, score = resolve_show(plan, tvm)
        if show is not None:
            result = apply_show(conn, tvm, show, plan.status, plan.watches)
            if result["show_id"] is None:
                show = None  # vanished mid-flight: stage it instead
        if show is not None:
            summary["shows_imported"] += 1
            summary["episodes_marked"] += result["applied"]
            db.add_staging_row(
                conn, batch_id=BATCH_ID, kind="show",
                raw_show_name=plan.name,
                raw_json=json.dumps(plan.state_raw or {"s_id": plan.s_id}),
                match_status="matched", match_confidence=score,
                matched_show_id=result["show_id"], note=note)
            for (season, number), slot in sorted(plan.watches.items()):
                mismatch = (season, number) in result["missing"]
                summary["episode_mismatches"] += mismatch
                for raw in slot["raw"]:
                    db.add_staging_row(
                        conn, batch_id=BATCH_ID, kind="episode",
                        raw_show_name=plan.name, season=season, number=number,
                        watched_at=slot["watched_at"], raw_json=json.dumps(raw),
                        match_status="unmatched" if mismatch else "matched",
                        matched_show_id=result["show_id"], note=note)
        else:
            summary["shows_staged"] += 1
            db.add_staging_row(
                conn, batch_id=BATCH_ID, kind="show",
                raw_show_name=plan.name,
                raw_json=json.dumps(plan.state_raw or {"s_id": plan.s_id}),
                match_status=method if method == "ambiguous" else "unmatched",
                match_confidence=score, note=note)
            for (season, number), slot in sorted(plan.watches.items()):
                for raw in slot["raw"]:
                    db.add_staging_row(
                        conn, batch_id=BATCH_ID, kind="episode",
                        raw_show_name=plan.name, season=season, number=number,
                        watched_at=slot["watched_at"], raw_json=json.dumps(raw),
                        match_status="unmatched", note=note)
        if i % 25 == 0:
            conn.commit()
            progress(f"  shows {i}/{len(shows)}…")
    conn.commit()

    movies = list(data.movies.values())
    for i, plan in enumerate(movies, 1):
        note = f"movie:{plan.key}"
        best, status, score = (None, "unmatched", 0.0)
        if tmdbc is not None:
            try:
                best, status, score = resolve_movie(plan, tmdbc)
            except tmdb.TMDBKeyMissing:
                tmdbc = None  # stage all movies; resolvable later via /import
        if best is not None:
            movie_id = apply_movie(conn, tmdb.movie_fields(best), plan.watched,
                                   plan.watched_at, plan.runtime_min)
            summary["movies_imported"] += 1
            for raw in plan.raws:
                db.add_staging_row(
                    conn, batch_id=BATCH_ID, kind="movie",
                    raw_title=plan.name, watched_at=plan.watched_at,
                    raw_json=json.dumps(raw), match_status="matched",
                    match_confidence=score, matched_movie_id=movie_id, note=note)
        else:
            summary["movies_staged"] += 1
            for raw in plan.raws:
                db.add_staging_row(
                    conn, batch_id=BATCH_ID, kind="movie",
                    raw_title=plan.name, watched_at=plan.watched_at,
                    raw_json=json.dumps(raw),
                    match_status=status if status == "ambiguous" else "unmatched",
                    match_confidence=score or None, note=note)
        if i % 25 == 0:
            conn.commit()
            progress(f"  movies {i}/{len(movies)}…")
    conn.commit()
    db.set_meta(conn, "import_committed_at", db.utcnow())
    return summary
