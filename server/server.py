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

from tvtracker import db  # noqa: E402

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
    """Wrap page content in the base chrome (nav, footer, dark theme)."""
    subs = {f"nav_{item}": "active" if item == active_nav else "" for item in NAV_ITEMS}
    return PAGE_TEMPLATE.substitute(title=title, content=content, **subs)


def make_handler(
    db_path: str | Path = DEFAULT_DB_PATH,
    assets_dir: Path = ASSETS_DIR,
) -> type[BaseHTTPRequestHandler]:
    """Build a Handler class closed over its dependencies (tests inject a
    tmp-path DB). One DB connection per request, opened lazily."""

    class Handler(BaseHTTPRequestHandler):
        # Routes are (compiled regex, method name). The first match wins;
        # regex groups become positional args to the method.
        GET_ROUTES = [
            (re.compile(r"^/$"), "page_queue"),
            (re.compile(r"^/healthz$"), "api_healthz"),
            (re.compile(r"^/assets/([A-Za-z0-9._-]+)$"), "serve_asset"),
        ]
        POST_ROUTES: list[tuple[re.Pattern, str]] = []

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
            # Real queue UI lands in M3; this proves template + DB plumbing.
            queue, waiting = db.watch_next(self.conn())
            content = (
                f"<h1>Watch Next</h1>"
                f"<p class=\"muted\">{len(queue)} in queue, "
                f"{len(waiting)} waiting for new episodes.</p>"
            )
            self.send_html(render_page("Queue", content, active_nav="queue"))

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
