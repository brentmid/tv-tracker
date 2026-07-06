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

from tvtracker import db, tvmaze  # noqa: E402

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
<title>$title — tv-tracker</title>
<link rel="stylesheet" href="/assets/style.css">
</head>
<body>
<nav class="topnav">
  <a href="/" class="$nav_queue">Queue</a>
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

NAV_ITEMS = ("queue", "add", "archive", "movies", "stats")


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
) -> type[BaseHTTPRequestHandler]:
    """Build a Handler class closed over its dependencies (tests inject a
    tmp-path DB and a fixture-fed TVmaze client). One DB connection per
    request, opened lazily."""
    tvm = tvmaze_client or tvmaze.TVMazeClient()

    class Handler(BaseHTTPRequestHandler):
        # Routes are (compiled regex, method name). The first match wins;
        # regex groups become positional args to the method.
        GET_ROUTES = [
            (re.compile(r"^/$"), "page_queue"),
            (re.compile(r"^/show/(\d+)$"), "page_show"),
            (re.compile(r"^/archive$"), "page_archive"),
            (re.compile(r"^/add$"), "page_add"),
            (re.compile(r"^/api/search/shows$"), "api_search_shows"),
            (re.compile(r"^/healthz$"), "api_healthz"),
            (re.compile(r"^/assets/([A-Za-z0-9._-]+)$"), "serve_asset"),
        ]
        POST_ROUTES = [
            (re.compile(r"^/api/shows$"), "api_add_show"),
            (re.compile(r"^/api/episodes/(\d+)/(watch|unwatch)$"), "api_episode_watch"),
            (re.compile(r"^/api/shows/(\d+)/watch-season$"), "api_watch_season"),
            (re.compile(r"^/api/shows/(\d+)/watch-all$"), "api_watch_all"),
            (re.compile(r"^/api/shows/(\d+)/(archive|unarchive)$"), "api_show_archive"),
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

        def page_queue(self):
            queue, waiting = db.watch_next(self.conn())
            parts = ["<h1>Watch Next</h1>"]
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

        def page_archive(self):
            shows = db.list_shows(self.conn(), "archived")
            parts = ["<h1>Archive</h1>"]
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
                show = tvm.show_with_episodes(tvmaze_id)
            except tvmaze.TVMazeError as e:
                self.send_json({"error": str(e)}, 502)
                return
            if show is None:
                self.send_json({"error": f"no TVmaze show {tvmaze_id}"}, 404)
                return
            conn = self.conn()
            show_id = db.upsert_show(conn, **tvmaze.show_fields(show))
            skipped = 0
            for ep in tvmaze.embedded_episodes(show):
                fields = tvmaze.episode_fields(ep)
                if fields is None:
                    skipped += 1
                    continue
                db.upsert_episode(conn, show_id=show_id, commit=False, **fields)
            conn.commit()
            db.touch_show_refreshed(conn, show_id)
            episodes = len(db.list_episodes(conn, show_id))
            self.send_json({"ok": True, "show_id": show_id,
                            "episodes": episodes, "skipped": skipped})

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
