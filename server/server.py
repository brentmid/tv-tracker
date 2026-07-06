#!/usr/bin/env python3
"""tv-tracker HTTP server.

Stdlib-only, modeled on ~/bin/portfolio-agent/server/server.py: a
ThreadingHTTPServer with a regex route table, server-rendered HTML via
string.Template, and static assets from server/assets/.

Binds to 127.0.0.1 by default — the app holds real watch history and has
no authentication. The LaunchAgent install script overrides this via
`TV_TRACKER_BIND` and binds to the machine's Tailscale IP instead, so
remote access goes through the authenticated tailnet and the server stays
invisible to the physical LAN. Do not bind to 0.0.0.0 without adding
application-level auth first.

Env:
    TV_TRACKER_PORT  (default 8431)
    TV_TRACKER_BIND  (default 127.0.0.1)
    TV_TRACKER_DB    (default <repo>/baselines/tvtracker.db)
"""
from __future__ import annotations

import html
import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from string import Template
from urllib.parse import parse_qs, urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tvtracker import db, importer, stats, tmdb, tvmaze  # noqa: E402

ASSETS_DIR = Path(__file__).resolve().parent / "assets"
DEFAULT_DB_PATH = REPO_ROOT / "baselines" / "tvtracker.db"

CONTENT_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".webmanifest": "application/manifest+json",
}

PAGE_TEMPLATE = Template("""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<meta name="theme-color" content="#000000">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black">
<meta name="apple-mobile-web-app-title" content="TV Tracker">
<title>$title — tv-tracker</title>
<link rel="stylesheet" href="/assets/style.css">
<link rel="icon" href="/assets/favicon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/assets/apple-touch-icon.png">
<link rel="manifest" href="/assets/manifest.webmanifest">
</head>
<body>
<nav class="topnav">
  <a href="/" class="$nav_queue">Queue</a>
  <a href="/not-started" class="$nav_notstarted">Not started</a>
  <a href="/finished" class="$nav_finished">Finished</a>
  <a href="/add" class="$nav_add">Add</a>
  <a href="/archive" class="$nav_archive">Archive</a>
  <a href="/movies" class="$nav_movies">Movies</a>
  <a href="/stats" class="$nav_stats">Stats</a>
</nav>
<main>
$content
</main>
<footer>
  <p>TV data from <a href="https://www.tvmaze.com">TVmaze</a>
  (<a href="https://creativecommons.org/licenses/by-sa/4.0/">CC BY-SA</a>).
  Movie data from <a href="https://www.themoviedb.org">TMDB</a> —
  this product uses the TMDB API but is not endorsed or certified by TMDB.</p>
</footer>
<script src="/assets/app.js"></script>
</body>
</html>
""")

NAV_ITEMS = ("queue", "notstarted", "finished", "add", "archive", "movies", "stats")


def render_page(title: str, content: str, active_nav: str = "") -> str:
    """Wrap page content in the base chrome (nav, footer, dark theme).

    `title` is plain text (escaped here); `content` is trusted HTML built
    by the page methods, which escape user/API data as they build it.
    """
    subs = {f"nav_{item}": "active" if item == active_nav else "" for item in NAV_ITEMS}
    return PAGE_TEMPLATE.substitute(
        title=html.escape(title), content=content, **subs
    )


def make_handler(
    db_path: str | Path = DEFAULT_DB_PATH,
    assets_dir: Path = ASSETS_DIR,
    tvmaze_client: tvmaze.TVMazeClient | None = None,
    tmdb_client: tmdb.TMDBClient | None = None,
) -> type[BaseHTTPRequestHandler]:
    """Build a Handler class closed over its dependencies (tests inject a
    tmp-path DB and fixture-fed API clients). One DB connection per
    request, opened lazily."""
    tvm = tvmaze_client or tvmaze.TVMazeClient()
    tmdbc = tmdb_client or tmdb.TMDBClient()  # key resolved lazily on first use

    def sync_show_from_tvmaze(conn, tvmaze_id: int) -> dict | None:
        """Fetch a show + episodes from TVmaze and upsert everything.
        Used by add-show and refresh; watch state survives (db upserts
        never clobber watched_at / show status). None if TVmaze 404s."""
        show = tvm.show_with_episodes(tvmaze_id)
        if show is None:
            return None
        show_id = db.upsert_show(conn, **tvmaze.show_fields(show))
        count = skipped = 0
        for ep in tvmaze.embedded_episodes(show):
            fields = tvmaze.episode_fields(ep)
            if fields is None:
                skipped += 1
                continue
            db.upsert_episode(conn, show_id=show_id, commit=False, **fields)
            count += 1
        conn.commit()
        db.touch_show_refreshed(conn, show_id)
        return {"show_id": show_id, "episodes": count, "skipped": skipped}

    class Handler(BaseHTTPRequestHandler):
        # Routes are (compiled regex, method name). The first match wins;
        # regex groups become positional args to the method.
        GET_ROUTES = [
            (re.compile(r"^/$"), "page_queue"),
            (re.compile(r"^/show/(\d+)$"), "page_show"),
            (re.compile(r"^/not-started$"), "page_not_started"),
            (re.compile(r"^/finished$"), "page_finished"),
            (re.compile(r"^/archive$"), "page_archive"),
            (re.compile(r"^/add$"), "page_add"),
            (re.compile(r"^/movies$"), "page_movies"),
            (re.compile(r"^/stats$"), "page_stats"),
            (re.compile(r"^/import$"), "page_import"),
            (re.compile(r"^/api/search/shows$"), "api_search_shows"),
            (re.compile(r"^/api/search/movies$"), "api_search_movies"),
            (re.compile(r"^/healthz$"), "api_healthz"),
            (re.compile(r"^/assets/([A-Za-z0-9._-]+)$"), "serve_asset"),
        ]
        POST_ROUTES = [
            (re.compile(r"^/api/shows$"), "api_add_show"),
            (re.compile(r"^/api/episodes/(\d+)/(watch|unwatch)$"), "api_episode_watch"),
            (re.compile(r"^/api/shows/(\d+)/watch-season$"), "api_watch_season"),
            (re.compile(r"^/api/shows/(\d+)/watch-all$"), "api_watch_all"),
            (re.compile(r"^/api/shows/(\d+)/(archive|unarchive)$"), "api_show_archive"),
            (re.compile(r"^/api/movies$"), "api_add_movie"),
            (re.compile(r"^/api/movies/(\d+)/(watch|unwatch|delete)$"), "api_movie_action"),
            (re.compile(r"^/api/shows/(\d+)/refresh$"), "api_refresh_show"),
            (re.compile(r"^/api/refresh-all$"), "api_refresh_all"),
            (re.compile(r"^/api/import/resolve$"), "api_import_resolve"),
        ]

        # -- plumbing ------------------------------------------------------

        def do_GET(self):  # noqa: N802 — stdlib name
            self._dispatch(self.GET_ROUTES)

        def do_POST(self):  # noqa: N802 — stdlib name
            self._dispatch(self.POST_ROUTES)

        def _dispatch(self, routes):
            parsed = urlparse(self.path)
            self.query = parse_qs(parsed.query)
            self._conn = None
            try:
                for pattern, method_name in routes:
                    m = pattern.match(parsed.path)
                    if m:
                        getattr(self, method_name)(*m.groups())
                        return
                self.send_error(404, "not found")
            except BrokenPipeError:
                pass
            except Exception as e:  # noqa: BLE001 — last-resort handler
                print(f"error handling {self.command} {self.path}: {e!r}",
                      file=sys.stderr)
                try:
                    self.send_error(500, "internal error")
                except Exception:  # noqa: BLE001
                    pass
            finally:
                if self._conn is not None:
                    self._conn.close()

        def conn(self):
            """The request's DB connection (opened on first use)."""
            if self._conn is None:
                self._conn = db.connect(db_path)
            return self._conn

        def read_json_body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            return json.loads(self.rfile.read(length))

        def send_html(self, html: str, code: int = 200):
            body = html.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def send_json(self, payload: dict, code: int = 200):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):  # noqa: A002 — stdlib name
            pass  # keep launchd logs quiet; errors go to stderr explicitly

        # -- routes ----------------------------------------------------------

        QUEUE_SORT_LABELS = [
            ("recent", "Recently watched"),
            ("oldest", "Waiting longest"),
            ("newest", "Newest episodes"),
            ("name", "A–Z"),
        ]

        def page_queue(self):
            conn = self.conn()
            sort = (self.query.get("sort") or ["recent"])[0]
            if sort not in db.QUEUE_SORTS:
                sort = "recent"
            queue, waiting = db.watch_next(conn, sort=sort)
            last_refresh = db.get_meta(conn, "last_refresh_all")
            last_txt = (f"air dates refreshed {html.escape(last_refresh[:16])}Z"
                        if last_refresh else "air dates never refreshed")
            options = "".join(
                f'<option value="{value}"{" selected" if value == sort else ""}>'
                f"{label}</option>"
                for value, label in self.QUEUE_SORT_LABELS)
            parts = [f"""\
<h1>Watch Next</h1>
<p class="muted">{last_txt} ·
  <button onclick="refreshBtn('/api/refresh-all', this)">Refresh all</button></p>
<p style="display:flex;gap:8px">
  <select onchange="location.href='/?sort='+this.value">{options}</select>
  <input type="search" placeholder="Filter shows…"
   oninput="filterCards(this.value)" style="flex:1">
</p>"""]
            if not queue and not waiting:
                parts.append(
                    '<p class="muted">Nothing here yet — '
                    '<a href="/add">add a show</a> or run the importer.</p>'
                )
            for row in queue:
                name = html.escape(row["name"])
                ep_label = f"S{row['episode_season']:02d}E{row['episode_number']:02d}"
                ep_name = html.escape(row["episode_name"] or "")
                more = row["unwatched_aired_count"] - 1
                more_txt = f" · +{more} more" if more > 0 else ""
                parts.append(f"""\
<div class="card">
  <div class="grow">
    <div class="title"><a href="/show/{row['id']}">{name}</a></div>
    <div class="sub">{ep_label} · {ep_name} · {row['episode_airdate']}{more_txt}</div>
  </div>
  <button class="primary" onclick="act('/api/episodes/{row['episode_id']}/watch')">✓</button>
</div>""")
            if waiting:
                parts.append('<h2 class="muted">Waiting for new episodes</h2>')
                for row in waiting:
                    name = html.escape(row["name"])
                    next_txt = (f"next episode {row['next_airdate']}"
                                if row["next_airdate"] else "no airdate announced")
                    parts.append(f"""\
<div class="card">
  <div class="grow">
    <div class="title"><a href="/show/{row['id']}">{name}</a></div>
    <div class="sub">{next_txt}</div>
  </div>
</div>""")
            self.send_html(render_page("Queue", "\n".join(parts), active_nav="queue"))

        def page_not_started(self):
            rows = db.not_started(self.conn())
            parts = [f"""\
<h1>Not started</h1>
<p class="muted">{len(rows)} shows you follow but haven't begun ·
 sorted by their latest episode's release date</p>
<p><input type="search" placeholder="Filter shows…"
   oninput="filterCards(this.value)"></p>"""]
            if not rows:
                parts.append('<p class="muted">Nothing here — everything '
                             'you follow is started or waiting.</p>')
            for row in rows:
                name = html.escape(row["name"])
                ep_label = f"S{row['episode_season']:02d}E{row['episode_number']:02d}"
                latest = row["latest_airdate"] or "no date"
                parts.append(f"""\
<div class="card">
  <div class="grow">
    <div class="title"><a href="/show/{row['id']}">{name}</a></div>
    <div class="sub">latest episode {latest} · {row['aired_count']} aired
     · start at {ep_label}</div>
  </div>
  <button class="primary" onclick="act('/api/episodes/{row['episode_id']}/watch')">✓</button>
  <button onclick="act('/api/shows/{row['id']}/archive')">Archive</button>
</div>""")
            self.send_html(render_page("Not started", "\n".join(parts),
                                       active_nav="notstarted"))

        def page_show(self, show_id: str):
            conn = self.conn()
            show = db.get_show(conn, int(show_id))
            if show is None:
                self.send_error(404, "no such show")
                return
            episodes = db.list_episodes(conn, show["id"])
            name = html.escape(show["name"])
            archived = show["status"] == "archived"
            arch_action = "unarchive" if archived else "archive"
            watched_count = sum(1 for e in episodes if e["watched_at"])
            parts = [f"""\
<h1>{name}</h1>
<p class="muted">{html.escape(show["tvmaze_status"] or "")}
 · {watched_count}/{len(episodes)} watched{" · ARCHIVED" if archived else ""}</p>
<p>
  <button onclick="act('/api/shows/{show['id']}/{arch_action}')">{arch_action.title()}</button>
  <button onclick="act('/api/shows/{show['id']}/watch-all')">Mark all watched</button>
  {"" if archived else
   f"<button onclick=\"refreshBtn('/api/shows/{show['id']}/refresh', this)\">Refresh</button>"}
</p>"""]
            season = None
            for ep in episodes:
                if ep["season"] != season:
                    season = ep["season"]
                    parts.append(f"""\
<h2>Season {season}
  <button onclick="act('/api/shows/{show['id']}/watch-season', {{season: {season}}})">
  Mark season watched</button></h2>""")
                ep_label = f"S{ep['season']:02d}E{ep['number']:02d}"
                ep_name = html.escape(ep["name"] or "")
                if ep["watched_at"]:
                    btn = (f"<button onclick=\"act('/api/episodes/{ep['id']}/unwatch')\""
                           f" title=\"tap to unwatch\">✓</button>")
                    cls = "sub"
                else:
                    btn = (f"<button class=\"primary\" "
                           f"onclick=\"act('/api/episodes/{ep['id']}/watch')\">watch</button>")
                    cls = "title"
                parts.append(f"""\
<div class="card">
  <div class="grow">
    <div class="{cls}">{ep_label} · {ep_name}</div>
    <div class="sub">{ep["airdate"] or "no airdate"}</div>
  </div>
  {btn}
</div>""")
            self.send_html(render_page(show["name"], "\n".join(parts)))

        def page_finished(self):
            rows = db.finished(self.conn())
            parts = [f"""\
<h1>Finished</h1>
<p class="muted">{len(rows)} shows fully watched, nothing scheduled ·
 new episodes move a show back to the queue automatically</p>
<p><input type="search" placeholder="Filter shows…"
   oninput="filterCards(this.value)"></p>"""]
            if not rows:
                parts.append('<p class="muted">Nothing finished yet.</p>')
            for row in rows:
                name = html.escape(row["name"])
                finished_on = (row["last_watched_at"] or "")[:10] or "?"
                parts.append(f"""\
<div class="card">
  <div class="grow">
    <div class="title"><a href="/show/{row['id']}">{name}</a></div>
    <div class="sub">{row["episode_count"]} episodes · finished {finished_on}</div>
  </div>
</div>""")
            self.send_html(render_page("Finished", "\n".join(parts),
                                       active_nav="finished"))

        def page_archive(self):
            shows = db.list_shows(self.conn(), "archived")
            parts = ["<h1>Archive</h1>",
                     '<p class="muted">Shows you stopped watching. '
                     'Fully-watched shows live in Finished instead.</p>']
            if not shows:
                parts.append('<p class="muted">No archived shows.</p>')
            for show in shows:
                name = html.escape(show["name"])
                parts.append(f"""\
<div class="card">
  <div class="grow"><div class="title"><a href="/show/{show['id']}">{name}</a></div></div>
  <button onclick="act('/api/shows/{show['id']}/unarchive')">Unarchive</button>
</div>""")
            self.send_html(render_page("Archive", "\n".join(parts), active_nav="archive"))

        def page_add(self):
            content = """\
<h1>Add a show</h1>
<p><input type="search" id="q" placeholder="Search TVmaze…" autofocus
   onkeydown="if(event.key==='Enter')searchShows()"></p>
<div id="results"></div>"""
            self.send_html(render_page("Add", content, active_nav="add"))

        def api_search_shows(self):
            query = (self.query.get("q") or [""])[0].strip()
            if not query:
                self.send_json({"error": "q required"}, 400)
                return
            try:
                results = tvm.search_shows(query)
            except tvmaze.TVMazeError as e:
                self.send_json({"error": str(e)}, 502)
                return
            conn = self.conn()
            trimmed = []
            for item in results:
                fields = tvmaze.show_fields(item["show"])
                existing = db.get_show_by_tvmaze_id(conn, fields["tvmaze_id"])
                fields["already_added"] = existing is not None
                trimmed.append(fields)
            self.send_json({"results": trimmed})

        def api_add_show(self):
            body = self.read_json_body()
            tvmaze_id = body.get("tvmaze_id")
            if not isinstance(tvmaze_id, int):
                self.send_json({"error": "tvmaze_id (int) required"}, 400)
                return
            try:
                result = sync_show_from_tvmaze(self.conn(), tvmaze_id)
            except tvmaze.TVMazeError as e:
                self.send_json({"error": str(e)}, 502)
                return
            if result is None:
                self.send_json({"error": f"no TVmaze show {tvmaze_id}"}, 404)
                return
            self.send_json({"ok": True, **result})

        def page_movies(self):
            conn = self.conn()
            watchlist = db.list_movies(conn, "watchlist")
            watched = db.list_movies(conn, "watched")
            parts = ["""\
<h1>Movies</h1>
<p><input type="search" id="q" placeholder="Search TMDB to add…"
   onkeydown="if(event.key==='Enter')searchMovies()"></p>
<div id="results"></div>
<p><input type="search" placeholder="Filter your movies…"
   oninput="filterCards(this.value)"></p>"""]

            def movie_card(m, buttons):
                title = html.escape(m["title"])
                year = f" ({m['year']})" if m["year"] else ""
                runtime = f" · {m['runtime_min']} min" if m["runtime_min"] else ""
                return f"""\
<div class="card">
  <div class="grow">
    <div class="title">{title}{year}</div>
    <div class="sub">{m["status"]}{runtime}</div>
  </div>
  {buttons}
</div>"""

            parts.append(f"<h2>Watchlist ({len(watchlist)})</h2>")
            if not watchlist:
                parts.append('<p class="muted">Nothing on the watchlist.</p>')
            for m in watchlist:
                parts.append(movie_card(m, f"""\
  <button class="primary" onclick="act('/api/movies/{m['id']}/watch')">✓</button>
  <button class="danger" onclick="act('/api/movies/{m['id']}/delete')">✕</button>"""))

            parts.append(f"<h2>Watched ({len(watched)})</h2>")
            for m in watched:
                parts.append(movie_card(m, f"""\
  <button onclick="act('/api/movies/{m['id']}/unwatch')" title="back to watchlist">↩</button>"""))

            self.send_html(render_page("Movies", "\n".join(parts), active_nav="movies"))

        def page_stats(self):
            data = stats.compute_stats(self.conn())
            parts = [f"""\
<h1>Stats</h1>
<div class="card"><div class="grow">
  <div class="title">{data["episodes_watched"]:,} episodes
   · {data["shows_with_watches"]} shows
   · {data["movies_watched"]} movies</div>
  <div class="sub">TV time {stats.fmt_hours(data["tv_minutes"])}
   · movie time {stats.fmt_hours(data["movie_minutes"])}</div>
</div></div>"""]
            notes = []
            if data["fallback_episode_count"]:
                notes.append(
                    f"{data['fallback_episode_count']:,} episodes had no runtime "
                    f"on record and were counted as {stats.EPISODE_FALLBACK_MIN} min")
            if data["movies_without_runtime"]:
                notes.append(
                    f"{data['movies_without_runtime']} watched movies have no "
                    f"runtime on record and add no movie time")
            if notes:
                parts.append(f'<p class="muted">{html.escape("; ".join(notes))}.</p>')

            if data["top_shows"]:
                parts.append("<h2>Top shows by hours</h2>")
                for i, entry in enumerate(data["top_shows"], 1):
                    parts.append(f"""\
<div class="card"><div class="grow">
  <div class="title">{i}. {html.escape(entry["name"])}</div>
  <div class="sub">{stats.fmt_hours(entry["minutes"])} · {entry["episodes"]} episodes</div>
</div></div>""")

            if data["per_year"]:
                parts.append("<h2>By year</h2>")
                for entry in data["per_year"]:
                    parts.append(f"""\
<div class="card"><div class="grow">
  <div class="title">{entry["year"]}</div>
  <div class="sub">{entry["episodes"]} episodes · {stats.fmt_hours(entry["minutes"])}
   · {entry["movies"]} movies</div>
</div></div>""")

            self.send_html(render_page("Stats", "\n".join(parts), active_nav="stats"))

        def page_import(self):
            conn = self.conn()
            shows = db.list_unresolved_staging_shows(conn)
            movie_groups = db.list_unresolved_staging_movie_groups(conn)
            parts = ["<h1>Import — needs a decision</h1>"]
            if not shows and not movie_groups:
                parts.append('<p class="muted">Nothing unresolved. '
                             'Run <code>scripts/import-tvtime.py commit</code> '
                             'to import, or you\'re all done.</p>')
            if shows:
                parts.append(f"<h2>Shows ({len(shows)})</h2>")
            for row in shows:
                name = html.escape(row["raw_show_name"] or "?")
                parts.append(f"""\
<div class="card" id="staging-{row['id']}">
  <div class="grow">
    <div class="title">{name}</div>
    <div class="sub">{row['match_status']} · {html.escape(row['note'] or '')}</div>
    <div class="resolve-results"></div>
  </div>
  <button onclick="importSearch({row['id']}, 'show', this)">Find</button>
  <button onclick="resolveImport({row['id']}, {{skip: true}}, this)">Skip</button>
</div>""")
            if movie_groups:
                parts.append(f"<h2>Movies ({len(movie_groups)})</h2>")
            for row in movie_groups:
                name = html.escape(row["raw_title"] or "?")
                parts.append(f"""\
<div class="card" id="staging-{row['id']}">
  <div class="grow">
    <div class="title">{name}</div>
    <div class="sub">{row['match_status']}</div>
    <div class="resolve-results"></div>
  </div>
  <button onclick="importSearch({row['id']}, 'movie', this)">Find</button>
  <button onclick="resolveImport({row['id']}, {{skip: true}}, this)">Skip</button>
</div>""")
            self.send_html(render_page("Import", "\n".join(parts)))

        def api_import_resolve(self):
            body = self.read_json_body()
            staging_id = body.get("staging_id")
            if not isinstance(staging_id, int):
                self.send_json({"error": "staging_id (int) required"}, 400)
                return
            conn = self.conn()
            row = db.get_staging_row(conn, staging_id)
            if row is None:
                self.send_json({"error": "no such staging row"}, 404)
                return
            if row["match_status"] not in ("ambiguous", "unmatched"):
                self.send_json({"error": "row already resolved"}, 409)
                return

            if body.get("skip"):
                count = db.set_staging_status_by_note(conn, row["note"], "skipped")
                self.send_json({"ok": True, "skipped_rows": count})
                return

            if row["kind"] == "show":
                tvmaze_id = body.get("tvmaze_id")
                if not isinstance(tvmaze_id, int):
                    self.send_json({"error": "tvmaze_id (int) required"}, 400)
                    return
                state = json.loads(row["raw_json"])
                followed = state.get("is_followed") == "true"
                archived = state.get("is_archived") == "true"
                status = "active" if followed and not archived else "archived"
                watches = {
                    (e["season"], e["number"]): {"watched_at": e["watched_at"],
                                                 "raw": []}
                    for e in db.staging_rows_by_note(conn, row["note"], "episode")
                    if e["season"] is not None and e["number"] is not None
                }
                try:
                    result = importer.apply_show(
                        conn, tvm, {"id": tvmaze_id}, status, watches)
                except tvmaze.TVMazeError as e:
                    self.send_json({"error": str(e)}, 502)
                    return
                if result["show_id"] is None:
                    self.send_json({"error": f"no TVmaze show {tvmaze_id}"}, 404)
                    return
                db.set_staging_status_by_note(
                    conn, row["note"], "resolved",
                    matched_show_id=result["show_id"])
                self.send_json({"ok": True, **{k: v for k, v in result.items()
                                               if k != "missing"},
                                "missing": [list(sn) for sn in result["missing"]]})
                return

            if row["kind"] == "movie":
                tmdb_id = body.get("tmdb_id")
                if not isinstance(tmdb_id, int):
                    self.send_json({"error": "tmdb_id (int) required"}, 400)
                    return
                raws = [json.loads(r["raw_json"])
                        for r in db.staging_rows_by_note(conn, row["note"], "movie")]
                watched = any(r.get("type") == "watch" for r in raws)
                runtime_s = next((r.get("runtime") for r in raws
                                  if (r.get("runtime") or "").isdigit()
                                  and r.get("runtime") != "0"), None)
                try:
                    movie = tmdbc.movie(tmdb_id)
                except tmdb.TMDBError as e:
                    self.send_json({"error": str(e)}, 502)
                    return
                if movie is None:
                    self.send_json({"error": f"no TMDB movie {tmdb_id}"}, 404)
                    return
                movie_id = importer.apply_movie(
                    conn, tmdb.movie_fields(movie), watched, row["watched_at"],
                    int(runtime_s) // 60 if runtime_s else None)
                db.set_staging_status_by_note(
                    conn, row["note"], "resolved", matched_movie_id=movie_id)
                self.send_json({"ok": True, "movie_id": movie_id})
                return

            self.send_json({"error": f"cannot resolve kind {row['kind']}"}, 400)

        def api_search_movies(self):
            query = (self.query.get("q") or [""])[0].strip()
            if not query:
                self.send_json({"error": "q required"}, 400)
                return
            try:
                results = tmdbc.search_movies(query)
            except tmdb.TMDBKeyMissing as e:
                self.send_json({"error": str(e)}, 503)
                return
            except tmdb.TMDBError as e:
                self.send_json({"error": str(e)}, 502)
                return
            conn = self.conn()
            trimmed = []
            for movie in results[:20]:
                fields = tmdb.movie_fields(movie)
                existing = conn.execute(
                    "SELECT 1 FROM movies WHERE tmdb_id = ?", (fields["tmdb_id"],)
                ).fetchone()
                fields["already_added"] = existing is not None
                trimmed.append(fields)
            self.send_json({"results": trimmed})

        def api_add_movie(self):
            body = self.read_json_body()
            tmdb_id = body.get("tmdb_id")
            if not isinstance(tmdb_id, int):
                self.send_json({"error": "tmdb_id (int) required"}, 400)
                return
            try:
                movie = tmdbc.movie(tmdb_id)  # detail call: has runtime
            except tmdb.TMDBKeyMissing as e:
                self.send_json({"error": str(e)}, 503)
                return
            except tmdb.TMDBError as e:
                self.send_json({"error": str(e)}, 502)
                return
            if movie is None:
                self.send_json({"error": f"no TMDB movie {tmdb_id}"}, 404)
                return
            movie_id = db.upsert_movie(self.conn(), **tmdb.movie_fields(movie))
            self.send_json({"ok": True, "movie_id": movie_id})

        def api_movie_action(self, movie_id: str, action: str):
            conn = self.conn()
            if db.get_movie(conn, int(movie_id)) is None:
                self.send_json({"error": "no such movie"}, 404)
                return
            if action == "delete":
                db.delete_movie(conn, int(movie_id))
            else:
                db.set_movie_watched(conn, int(movie_id), action == "watch")
            self.send_json({"ok": True})

        def api_refresh_show(self, show_id: str):
            conn = self.conn()
            show = db.get_show(conn, int(show_id))
            if show is None:
                self.send_json({"error": "no such show"}, 404)
                return
            if show["status"] == "archived":
                self.send_json({"error": "archived shows are frozen — "
                                         "unarchive to refresh"}, 409)
                return
            try:
                result = sync_show_from_tvmaze(conn, show["tvmaze_id"])
            except tvmaze.TVMazeError as e:
                self.send_json({"error": str(e)}, 502)
                return
            if result is None:
                self.send_json({"error": "show vanished from TVmaze"}, 502)
                return
            self.send_json({"ok": True, **result})

        def api_refresh_all(self):
            conn = self.conn()
            refreshed, errors = [], []
            for show in db.list_shows(conn, "active"):
                try:
                    result = sync_show_from_tvmaze(conn, show["tvmaze_id"])
                except tvmaze.TVMazeError as e:
                    errors.append({"show": show["name"], "error": str(e)})
                    continue
                if result is None:
                    errors.append({"show": show["name"],
                                   "error": "gone from TVmaze"})
                    continue
                refreshed.append(show["name"])
            db.set_meta(conn, "last_refresh_all", db.utcnow())
            self.send_json({"ok": not errors, "refreshed": len(refreshed),
                            "errors": errors})

        def api_episode_watch(self, episode_id: str, action: str):
            conn = self.conn()
            if db.get_episode(conn, int(episode_id)) is None:
                self.send_json({"error": "no such episode"}, 404)
                return
            db.set_episode_watched(conn, int(episode_id), action == "watch")
            self.send_json({"ok": True})

        def api_watch_season(self, show_id: str):
            body = self.read_json_body()
            season = body.get("season")
            if not isinstance(season, int):
                self.send_json({"error": "season (int) required"}, 400)
                return
            count = db.watch_season(self.conn(), int(show_id), season)
            self.send_json({"ok": True, "marked": count})

        def api_watch_all(self, show_id: str):
            count = db.watch_all(self.conn(), int(show_id))
            self.send_json({"ok": True, "marked": count})

        def api_show_archive(self, show_id: str, action: str):
            conn = self.conn()
            if db.get_show(conn, int(show_id)) is None:
                self.send_json({"error": "no such show"}, 404)
                return
            status = "archived" if action == "archive" else "active"
            db.set_show_status(conn, int(show_id), status)
            self.send_json({"ok": True, "status": status})

        def api_healthz(self):
            shows = self.conn().execute("SELECT COUNT(*) FROM shows").fetchone()[0]
            self.send_json({"ok": True, "shows": shows})

        def serve_asset(self, filename: str):
            # Route regex forbids "/" and a bare ".." can't resolve outside
            # assets_dir, but belt-and-braces:
            target = (assets_dir / filename).resolve()
            if target.parent != assets_dir.resolve() or not target.is_file():
                self.send_error(404, "not found")
                return
            body = target.read_bytes()
            self.send_response(200)
            self.send_header(
                "Content-Type",
                CONTENT_TYPES.get(target.suffix, "application/octet-stream"),
            )
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=300")
            self.end_headers()
            self.wfile.write(body)

    return Handler


def main() -> int:  # pragma: no cover — exercised via LaunchAgent, not tests
    bind = os.environ.get("TV_TRACKER_BIND", "127.0.0.1")
    port = int(os.environ.get("TV_TRACKER_PORT", "8431"))
    db_path = Path(os.environ.get("TV_TRACKER_DB", DEFAULT_DB_PATH))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db.connect(db_path).close()  # fail fast + run migrations before serving

    server = ThreadingHTTPServer((bind, port), make_handler(db_path))
    print(f"tv-tracker serving on http://{bind}:{port} (db: {db_path})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
