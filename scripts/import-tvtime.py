#!/usr/bin/env python3
"""Three-phase TV Time GDPR-export importer CLI.

    inspect  <zip-or-dir>            list files, headers, row counts
    dry-run  <zip-or-dir> [--offline]  report the import plan; with network,
                                       resolve every show against TVmaze
    commit   <zip-or-dir>            import into the DB (idempotent)

Defaults: export = baselines/import/gdpr-data.zip, DB = baselines/tvtracker.db
(override with --db). ~500 TVmaze lookups run rate-limited (~5 min).
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tvtracker import db, importer, tmdb, tvmaze  # noqa: E402

DEFAULT_EXPORT = REPO_ROOT / "baselines" / "import" / "gdpr-data.zip"
DEFAULT_DB = REPO_ROOT / "baselines" / "tvtracker.db"

# Never print contents of these — account tokens and personal data.
SENSITIVE = {"access_token.csv", "refresh_token.csv", "device_token.csv",
             "ip_address.csv", "user_personal_data.csv", "auth-prod-login.csv",
             "_appsflyer_ids.csv", "ad_identifier.csv", "device_data.csv"}


def cmd_inspect(source: Path) -> int:
    if source.is_dir():
        names = sorted(p.name for p in source.glob("*.csv"))
        opener = lambda n: open(source / n, "rb")  # noqa: E731
    else:
        zf = zipfile.ZipFile(source)
        names = sorted(n for n in zf.namelist() if n.endswith(".csv"))
        opener = zf.open
    for name in names:
        with opener(name) as f:
            text = io.TextIOWrapper(f, encoding="utf-8", errors="replace")
            reader = csv.reader(text)
            header = next(reader, [])
            count = sum(1 for _ in reader)
        flag = "  [SENSITIVE — contents never printed]" if name in SENSITIVE else ""
        print(f"{name}: {count} rows{flag}")
        print(f"    {', '.join(header[:10])}{', …' if len(header) > 10 else ''}")
    return 0


def cmd_dry_run(source: Path, offline: bool) -> int:
    data = importer.parse_export(source)
    tvm = None if offline else tvmaze.TVMazeClient()
    if not offline:
        print(f"resolving {len(data.shows)} shows against TVmaze "
              f"(rate-limited, expect ~{len(data.shows) // 100 + 1} min)…")
    report = importer.dry_run(data, tvm=tvm)

    print(f"""
== parse plan ==
shows: {report['shows_total']} ({report['shows_active']} active, {report['shows_archived']} archived)
episode watches (unique per show/season/episode): {report['episode_watches']}
unparseable episode rows skipped: {report['skipped_rows']}
movies: {report['movies_total']} ({report['movies_watched']} watched, {report['movies_watchlist']} watchlist)""")

    if report["validation"]:
        print(f"\n== validation vs TV Time's own nb_episodes_seen "
              f"({len(report['validation'])} shows differ by >2) ==")
        for v in report["validation"][:15]:
            print(f"  {v['show']}: ours {v['ours']} vs tvtime {v['tvtime']}")
    else:
        print("\nvalidation: per-show watch counts agree with TV Time's "
              "nb_episodes_seen (±2)")

    res = report["resolution"]
    if res:
        print(f"""
== TVmaze resolution ==
by TheTVDB id: {res['id']}   by name (fuzzy): {res['fuzzy']}
ambiguous: {res['ambiguous']}   unmatched: {res['unmatched']}""")
        for u in res["unresolved_shows"]:
            print(f"  {u['status']}: {u['name']} (thetvdb {u['s_id']}, "
                  f"score {u['score']})")
        if res["episode_mismatches"]:
            print(f"\nepisode numbering mismatches ({len(res['episode_mismatches'])} shows):")
            for m in res["episode_mismatches"][:15]:
                print(f"  {m['show']}: export has {m['missing']} not on TVmaze")
    else:
        print("\n(offline: TVmaze resolution skipped — rerun without --offline)")
    return 0


def cmd_commit(source: Path, db_path: Path) -> int:
    data = importer.parse_export(source)
    conn = db.connect(db_path)
    try:
        tmdbc = tmdb.TMDBClient()
        try:
            tmdbc.api_key
        except tmdb.TMDBKeyMissing as e:
            print(f"note: {e}\n      movies will all land in staging.")
            tmdbc = None
        print(f"importing {len(data.shows)} shows + {len(data.movies)} movies "
              f"into {db_path} (rate-limited)…")
        summary = importer.commit(conn, data, tvmaze.TVMazeClient(), tmdbc)
        print(f"""
== committed ==
{json.dumps(summary, indent=2)}

Leftovers (staged ambiguous/unmatched) → resolve at /import in the web UI.""")
    finally:
        conn.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["inspect", "dry-run", "commit"])
    parser.add_argument("source", nargs="?", default=DEFAULT_EXPORT, type=Path)
    parser.add_argument("--offline", action="store_true",
                        help="dry-run without network (parse plan only)")
    parser.add_argument("--db", default=DEFAULT_DB, type=Path)
    args = parser.parse_args()

    if not args.source.exists():
        print(f"export not found: {args.source}", file=sys.stderr)
        return 1
    if args.phase == "inspect":
        return cmd_inspect(args.source)
    if args.phase == "dry-run":
        return cmd_dry_run(args.source, args.offline)
    return cmd_commit(args.source, args.db)


if __name__ == "__main__":
    sys.exit(main())
