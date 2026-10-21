"""HTTP server: REST API, media streaming, and the web UI."""

import http.cookies
import json
import logging
import mimetypes
import os
import posixpath
import re
import socket
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__, naming, probe, subtitles
from .auth import COOKIE_NAME, Auth, AuthError
from .scanner import Scanner
from .transcode import TranscodeManager, TranscodeError

log = logging.getLogger("lumen.server")

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
CHUNK = 256 * 1024

# Browsers refuse to send arbitrary headers cross-origin without a CORS
# preflight, so requiring one on writes is a cheap, effective CSRF guard.
CSRF_HEADER = "X-Lumen-Request"

STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".webmanifest": "application/manifest+json",
}

SORT_COLUMNS = {
    "title": "title COLLATE NOCASE ASC",
    "added": "added_at DESC",
    "year": "year DESC, title COLLATE NOCASE ASC",
    "size": "size DESC",
}

RANGE_RE = re.compile(r"^bytes=(?P<start>\d*)-(?P<end>\d*)$")


class App:
    """Everything the request handlers need, assembled once at startup."""

    def __init__(self, config, db):
        self.config = config
        self.db = db
        self.auth = Auth(db, config)
        self.scanner = Scanner(db, config)
        self.transcode = TranscodeManager(config)
        self.started_at = time.time()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Lumen/" + __version__
    sys_version = ""

    app = None          # injected by serve()

    # -- plumbing -------------------------------------------------------

    def log_message(self, fmt, *args):
        log.info("%s %s", self.address_string(), fmt % args)

    def log_error(self, fmt, *args):
        log.warning("%s %s", self.address_string(), fmt % args)

    def do_GET(self):
        self._dispatch(body_wanted=True)

    def do_HEAD(self):
        self._dispatch(body_wanted=False)

    def do_POST(self):
        self._dispatch(body_wanted=True)

    def _dispatch(self, body_wanted=True):
        self.body_wanted = body_wanted
        parsed = urllib.parse.urlparse(self.path)
        path = posixpath.normpath(urllib.parse.unquote(parsed.path))
        self.query = urllib.parse.parse_qs(parsed.query)
        try:
            self._route(path)
        except BrokenPipeError:
            pass
        except ConnectionResetError:
            pass
        except Exception:
            log.exception("unhandled error serving %s", self.path)
            try:
                self._json({"error": "internal server error"}, status=500)
            except OSError:
                pass

    def _route(self, path):
        method = self.command

        if path == "/healthz":
            return self._json({"status": "ok", "version": __version__})

        if path.startswith("/api/"):
            return self._route_api(method, path)

        if method not in ("GET", "HEAD"):
            return self._json({"error": "method not allowed"}, status=405)

        if path in ("/", "/index.html"):
            return self._send_static("index.html")
        if path.startswith("/static/"):
            return self._send_static(path[len("/static/"):])
        if path == "/favicon.ico":
            return self._send_static("favicon.svg")
        # Any other path is a UI route; let the single page handle it.
        return self._send_static("index.html")

    def _route_api(self, method, path):
        parts = [p for p in path.split("/") if p][1:]   # drop "api"

        if method == "POST" and not self.headers.get(CSRF_HEADER):
            return self._json(
                {"error": "missing %s header" % CSRF_HEADER}, status=403
            )

        # -- public ------------------------------------------------------
        if parts == ["auth", "login"] and method == "POST":
            return self._login()
        if parts == ["auth", "setup"] and method == "POST":
            return self._first_run_setup()
        if parts == ["auth", "me"] and method == "GET":
            user = self.current_user()
            return self._json({
                "user": None if not user else {
                    "username": user["username"], "is_admin": bool(user["is_admin"]),
                },
                "needs_setup": self.app.auth.user_count() == 0,
            })

        # -- token-authenticated media endpoints --------------------------
        # These accept either a session cookie or a signed ?token= so that
        # VLC, Kodi and Chromecast work with a plain URL.
        if len(parts) == 2 and parts[0] in ("stream", "download") and method in ("GET", "HEAD"):
            return self._serve_media(parts[1], download=parts[0] == "download")
        if len(parts) == 3 and parts[0] == "subtitles" and method == "GET":
            return self._serve_subtitle(parts[1], parts[2])

        # -- everything below needs a session -----------------------------
        user = self.current_user()
        if not user:
            return self._json({"error": "authentication required"}, status=401)

        if parts == ["auth", "logout"] and method == "POST":
            self.app.auth.end_session(self._cookie_token())
            self._clear_cookie = True
            return self._json({"ok": True})

        if parts == ["library"] and method == "GET":
            return self._library(user)
        if parts == ["series"] and method == "GET":
            return self._series()
        if parts == ["status"] and method == "GET":
            return self._status()
        if parts == ["scan"] and method == "POST":
            started = self.app.scanner.trigger()
            return self._json({"started": started, "status": self.app.scanner.status})
        if parts == ["progress"] and method == "GET":
            return self._continue_watching(user)

        if len(parts) == 2 and parts[0] == "items" and method == "GET":
            return self._item_detail(parts[1], user)
        if len(parts) == 3 and parts[0] == "items" and parts[2] == "playback":
            return self._playback_info(parts[1], user)
        if len(parts) == 2 and parts[0] == "progress" and method == "POST":
            return self._save_progress(parts[1], user)

        return self._json({"error": "not found"}, status=404)

    # -- response helpers -------------------------------------------------

    def _json(self, payload, status=200, extra_headers=None):
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._send_cookie_headers()
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.body_wanted:
            self.wfile.write(body)

    def _send_cookie_headers(self):
        token = getattr(self, "_set_cookie", None)
        if token:
            cookie = (
                "%s=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=%d"
                % (COOKIE_NAME, token, self.app.config.session_days * 86400)
            )
            self.send_header("Set-Cookie", cookie)
        if getattr(self, "_clear_cookie", False):
            self.send_header(
                "Set-Cookie",
                "%s=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0" % COOKIE_NAME,
            )

    def _read_json_body(self, limit=64 * 1024):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0 or length > limit:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8", "replace"))
        except ValueError:
            return {}

    def _send_static(self, relative):
        relative = relative.lstrip("/")
        full = os.path.normpath(os.path.join(WEB_DIR, relative))
        # A bare startswith() would also accept a sibling like "<webdir>evil";
        # the separator is what makes it a containment check.
        if full != WEB_DIR and not full.startswith(WEB_DIR + os.sep):
            log.warning("blocked static path escape: %r", relative)
            return self._json({"error": "not found"}, status=404)
        if not os.path.isfile(full):
            return self._json({"error": "not found"}, status=404)

        extension = os.path.splitext(full)[1].lower()
        content_type = STATIC_TYPES.get(extension) or (
            mimetypes.guess_type(full)[0] or "application/octet-stream"
        )
        with open(full, "rb") as handle:
            body = handle.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # The UI is tiny and versioned with the server; don't let a stale copy
        # survive an upgrade.
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if self.body_wanted:
            self.wfile.write(body)

    # -- auth ---------------------------------------------------------------

    def _cookie_token(self):
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            jar = http.cookies.SimpleCookie(raw)
        except http.cookies.CookieError:
            return None
        morsel = jar.get(COOKIE_NAME)
        return morsel.value if morsel else None

    def current_user(self):
        return self.app.auth.session_user(self._cookie_token())

    def _login(self):
        data = self._read_json_body()
        username = (data.get("username") or "").strip()
        password = data.get("password") or ""
        user = self.app.auth.verify_password(username, password)
        if not user:
            log.warning("failed login for %r from %s", username, self.address_string())
            # Slow down credential stuffing a little without holding a thread
            # for long enough to be a DoS vector itself.
            time.sleep(1.0)
            return self._json({"error": "invalid username or password"}, status=401)
        self._set_cookie = self.app.auth.start_session(user["id"])
        return self._json({
            "user": {"username": user["username"], "is_admin": bool(user["is_admin"])}
        })

    def _first_run_setup(self):
        """Create the first admin. Only possible while no users exist."""
        if self.app.auth.user_count() > 0:
            return self._json({"error": "setup already completed"}, status=409)
        data = self._read_json_body()
        try:
            user_id = self.app.auth.create_user(
                (data.get("username") or "").strip(),
                data.get("password") or "",
                is_admin=True,
            )
        except AuthError as exc:
            return self._json({"error": str(exc)}, status=400)
        self._set_cookie = self.app.auth.start_session(user_id)
        return self._json({"user": {"username": data.get("username"), "is_admin": True}})

    def _authorise_item(self, item_id):
        """Resolve an item for a media request via cookie *or* stream token."""
        user = self.current_user()
        if user:
            return self._get_item(item_id), user["id"]
        token = (self.query.get("token") or [None])[0]
        user_id = self.app.auth.check_stream_token(token, item_id) if token else None
        if user_id is None:
            return None, None
        return self._get_item(item_id), user_id

    def _get_item(self, item_id):
        try:
            item_id = int(item_id)
        except (TypeError, ValueError):
            return None
        return self.app.db.query_one("SELECT * FROM items WHERE id = ?", (item_id,))

    # -- library ------------------------------------------------------------

    def _library(self, user):
        q = self.query
        search = (q.get("search") or [""])[0].strip()
        kind = (q.get("kind") or [""])[0].strip()
        series = (q.get("series") or [""])[0].strip()
        sort = (q.get("sort") or ["title"])[0]
        try:
            limit = max(1, min(500, int((q.get("limit") or [100])[0])))
            offset = max(0, int((q.get("offset") or [0])[0]))
        except ValueError:
            limit, offset = 100, 0

        where, params = [], []
        if search:
            where.append("(title LIKE ? OR series LIKE ? OR path LIKE ?)")
            like = "%" + search + "%"
            params += [like, like, like]
        if kind in ("movie", "episode"):
            where.append("kind = ?")
            params.append(kind)
        if series:
            where.append("series = ?")
            params.append(series)

        clause = ("WHERE " + " AND ".join(where)) if where else ""
        order = SORT_COLUMNS.get(sort, SORT_COLUMNS["title"])
        # Episodes always read better grouped by series then number.
        if series or kind == "episode":
            order = "series COLLATE NOCASE ASC, season ASC, episode ASC"

        total = self.app.db.query_one(
            "SELECT COUNT(*) AS n FROM items %s" % clause, tuple(params)
        )["n"]
        rows = self.app.db.query(
            "SELECT * FROM items %s ORDER BY %s LIMIT ? OFFSET ?" % (clause, order),
            tuple(params) + (limit, offset),
        )
        progress = self._progress_map(user["id"], [r["id"] for r in rows])
        return self._json({
            "total": total,
            "limit": limit,
            "offset": offset,
            "items": [self._item_json(r, progress.get(r["id"])) for r in rows],
        })

    def _series(self):
        rows = self.app.db.query(
            "SELECT series, COUNT(*) AS episodes, "
            "COUNT(DISTINCT season) AS seasons "
            "FROM items WHERE kind = 'episode' AND series IS NOT NULL "
            "GROUP BY series ORDER BY series COLLATE NOCASE"
        )
        return self._json({"series": [dict(r) for r in rows]})

    def _item_json(self, row, progress=None):
        try:
            subs = json.loads(row["subtitles"] or "[]")
        except ValueError:
            subs = []
        return {
            "id": row["id"],
            "kind": row["kind"],
            "title": row["title"],
            "series": row["series"],
            "season": row["season"],
            "episode": row["episode"],
            "year": row["year"],
            "display": naming.display_name(row),
            "duration": row["duration"],
            "width": row["width"],
            "height": row["height"],
            "vcodec": row["vcodec"],
            "acodec": row["acodec"],
            "container": row["container"],
            "size": row["size"],
            "probed": bool(row["probed"]),
            "filename": os.path.basename(row["path"]),
            "subtitles": [
                {"index": s["index"], "language": s.get("language"),
                 "title": s.get("title"), "source": s.get("source")}
                for s in subs
            ],
            "progress": progress,
        }

    def _progress_map(self, user_id, item_ids):
        if not item_ids:
            return {}
        placeholders = ", ".join("?" for _ in item_ids)
        rows = self.app.db.query(
            "SELECT item_id, position, duration, finished FROM progress "
            "WHERE user_id = ? AND item_id IN (%s)" % placeholders,
            (user_id,) + tuple(item_ids),
        )
        return {
            r["item_id"]: {
                "position": r["position"],
                "duration": r["duration"],
                "finished": bool(r["finished"]),
            }
            for r in rows
        }

    def _item_detail(self, item_id, user):
        row = self._get_item(item_id)
        if not row:
            return self._json({"error": "not found"}, status=404)
        progress = self._progress_map(user["id"], [row["id"]]).get(row["id"])
        payload = self._item_json(row, progress)
        payload["path"] = row["path"]
        return self._json(payload)

    def _playback_info(self, item_id, user):
        row = self._get_item(item_id)
        if not row:
            return self._json({"error": "not found"}, status=404)
        caps = probe.parse_caps((self.query.get("caps") or [""])[0])
        mode, container, reason = probe.playback_plan(
            row, caps, self.app.config.transcode_enabled
        )
        token = self.app.auth.issue_stream_token(row["id"], user["id"])
        base = self._public_base()
        return self._json({
            "item_id": row["id"],
            "mode": mode,
            "container": container,
            "reason": reason,
            "seekable": mode == "direct",
            "duration": row["duration"],
            "token": token,
            "stream_url": "%s/api/stream/%d?token=%s" % (base, row["id"], token),
            "download_url": "%s/api/download/%d?token=%s" % (base, row["id"], token),
        })

    def _public_base(self):
        if self.app.config.public_url:
            return self.app.config.public_url
        host = self.headers.get("Host") or "%s:%d" % (
            self.app.config.host, self.app.config.port
        )
        return "http://" + host

    # -- progress ------------------------------------------------------------

    def _save_progress(self, item_id, user):
        row = self._get_item(item_id)
        if not row:
            return self._json({"error": "not found"}, status=404)
        data = self._read_json_body()
        try:
            position = float(data.get("position") or 0)
        except (TypeError, ValueError):
            position = 0.0
        duration = row["duration"]
        finished = bool(data.get("finished"))
        if duration and position >= duration - 15:
            finished = True
        self.app.db.execute(
            "INSERT INTO progress (user_id, item_id, position, duration, finished, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, item_id) DO UPDATE SET "
            "position = excluded.position, duration = excluded.duration, "
            "finished = excluded.finished, updated_at = excluded.updated_at",
            (user["id"], row["id"], max(0.0, position), duration,
             1 if finished else 0, time.time()),
        )
        return self._json({"ok": True, "finished": finished})

    def _continue_watching(self, user):
        rows = self.app.db.query(
            "SELECT i.*, p.position, p.finished FROM progress p "
            "JOIN items i ON i.id = p.item_id "
            "WHERE p.user_id = ? AND p.finished = 0 AND p.position > 30 "
            "ORDER BY p.updated_at DESC LIMIT 20",
            (user["id"],),
        )
        out = []
        for row in rows:
            payload = self._item_json(row, {
                "position": row["position"], "duration": row["duration"],
                "finished": bool(row["finished"]),
            })
            out.append(payload)
        return self._json({"items": out})

    def _status(self):
        return self._json({
            "version": __version__,
            "uptime": round(time.time() - self.app.started_at, 1),
            "library": {
                "items": self.app.db.query_one("SELECT COUNT(*) AS n FROM items")["n"],
                "movies": self.app.db.query_one(
                    "SELECT COUNT(*) AS n FROM items WHERE kind = 'movie'")["n"],
                "episodes": self.app.db.query_one(
                    "SELECT COUNT(*) AS n FROM items WHERE kind = 'episode'")["n"],
                "media_dirs": self.app.config.media_dirs,
            },
            "scan": self.app.scanner.status,
            "transcode": {
                "enabled": self.app.config.transcode_enabled,
                "sessions": self.app.transcode.active(),
                "max_sessions": self.app.config.max_sessions,
            },
        })

    # -- media delivery --------------------------------------------------------

    def _serve_subtitle(self, item_id, index):
        row, _user_id = self._authorise_item(item_id)
        if row is None:
            return self._json({"error": "not found or not authorised"}, status=404)
        try:
            tracks = json.loads(row["subtitles"] or "[]")
            track = tracks[int(index)]
        except (ValueError, IndexError, TypeError):
            return self._json({"error": "no such subtitle track"}, status=404)

        body = subtitles.to_webvtt(
            track, self.app.config.ffmpeg, video_path=row["path"]
        )
        if body is None:
            return self._json({"error": "could not convert subtitle"}, status=502)
        self.send_response(200)
        self.send_header("Content-Type", "text/vtt; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=3600")
        self.end_headers()
        if self.body_wanted:
            self.wfile.write(body)

    def _serve_media(self, item_id, download=False):
        row, _user_id = self._authorise_item(item_id)
        if row is None:
            return self._json({"error": "not found or not authorised"}, status=404)
        if not os.path.exists(row["path"]):
            log.warning("file vanished since the last scan: %s", row["path"])
            return self._json({"error": "file is no longer on disk"}, status=410)

        if download:
            return self._send_file(row["path"], attachment=True)

        requested = (self.query.get("mode") or ["auto"])[0]
        caps = probe.parse_caps((self.query.get("caps") or [""])[0])
        if requested in ("direct", "remux", "transcode"):
            mode = requested
            container = "mp4"
            if mode == "direct":
                container = row["container"]
            elif mode == "remux":
                _m, container, _r = probe.playback_plan(
                    row, caps, self.app.config.transcode_enabled
                )
        else:
            mode, container, _reason = probe.playback_plan(
                row, caps, self.app.config.transcode_enabled
            )

        if mode == "direct":
            return self._send_file(row["path"])
        return self._send_transcode(row, mode, container)

    def _send_file(self, path, attachment=False):
        """Serve a file from disk, honouring a single HTTP range."""
        try:
            size = os.path.getsize(path)
        except OSError:
            return self._json({"error": "file is unreadable"}, status=500)

        content_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
        start, end = 0, size - 1
        partial = False

        range_header = self.headers.get("Range")
        if range_header and size > 0:
            match = RANGE_RE.match(range_header.strip())
            if not match:
                # Multi-range and malformed values: ignore and send it whole.
                log.debug("ignoring unsupported Range %r", range_header)
            else:
                raw_start, raw_end = match.group("start"), match.group("end")
                if raw_start == "" and raw_end == "":
                    pass
                elif raw_start == "":
                    # "bytes=-500" means the last 500 bytes.
                    length = min(int(raw_end), size)
                    start, end, partial = size - length, size - 1, True
                else:
                    start = int(raw_start)
                    end = int(raw_end) if raw_end else size - 1
                    end = min(end, size - 1)
                    partial = True
                    if start >= size or start > end:
                        self.send_response(416)
                        self.send_header("Content-Range", "bytes */%d" % size)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return

        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if partial:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        if attachment:
            filename = os.path.basename(path).replace('"', "")
            self.send_header(
                "Content-Disposition", 'attachment; filename="%s"' % filename
            )
        self.end_headers()

        if not self.body_wanted:
            return

        remaining = length
        try:
            with open(path, "rb") as handle:
                handle.seek(start)
                while remaining > 0:
                    block = handle.read(min(CHUNK, remaining))
                    if not block:
                        break
                    self.wfile.write(block)
                    remaining -= len(block)
        except (BrokenPipeError, ConnectionResetError):
            # Normal: the user seeked or closed the tab.
            self.close_connection = True

    def _send_transcode(self, row, mode, container):
        """Pipe ffmpeg's stdout straight into the response body."""
        try:
            start = float((self.query.get("t") or [0])[0])
        except (TypeError, ValueError):
            start = 0.0
        if row["duration"]:
            start = max(0.0, min(start, max(0.0, row["duration"] - 1)))

        try:
            session = self.app.transcode.start(
                row["id"], row["path"], mode, container, start
            )
        except TranscodeError as exc:
            log.warning("transcode refused for item %s: %s", row["id"], exc)
            return self._json({"error": str(exc)}, status=503)

        # A progressive stream has no length, so the response must end by
        # closing the connection.
        self.send_response(200)
        self.send_header("Content-Type", self.app.transcode.content_type(container))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Lumen-Mode", mode)
        self.send_header("X-Lumen-Start", "%.3f" % start)
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        if not self.body_wanted:
            self.app.transcode.stop(session)
            return

        try:
            stdout = session.proc.stdout
            while True:
                block = stdout.read(CHUNK)
                if not block:
                    break
                self.wfile.write(block)
                session.bytes_sent += len(block)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            pass
        except OSError as exc:
            log.warning("transcode %s write failed: %s", session.id, exc)
        finally:
            if session.bytes_sent == 0 and session.stderr_tail:
                log.error("transcode %s produced nothing. ffmpeg said: %s",
                          session.id, " | ".join(session.stderr_tail[-5:]))
            self.app.transcode.stop(session)


def serve(config, db):
    """Build the app, bind the socket and serve until interrupted."""
    app = App(config, db)
    os.makedirs(config.cache_dir, exist_ok=True)

    handler = type("BoundHandler", (Handler,), {"app": app})

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True
        # Streaming responses live for as long as the user watches.
        timeout = None

    httpd = Server((config.host, config.port), handler)
    app.scanner.start_background()
    db.purge_expired_sessions()
    return app, httpd
