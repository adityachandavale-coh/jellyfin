"""Library scanner.

Walks the configured media roots and keeps the ``items`` table in sync. The
scan is incremental: a file is only re-probed when its size or mtime has
changed since the last pass, so repeat scans over a large library are cheap.
"""

import json
import logging
import os
import threading
import time

from . import naming, probe, subtitles

log = logging.getLogger("lumen.scanner")

SKIP_DIRS = ("@eadir", "lost+found", "$recycle.bin", "#recycle")


class Scanner:
    def __init__(self, db, config):
        self.db = db
        self.config = config
        self._lock = threading.Lock()
        self._running = False
        self._stop = threading.Event()
        self._thread = None
        self.status = {
            "running": False,
            "started_at": None,
            "finished_at": None,
            "scanned": 0,
            "added": 0,
            "updated": 0,
            "removed": 0,
            "errors": 0,
            "current": None,
            "last_error": None,
        }

    # -- lifecycle ------------------------------------------------------

    def start_background(self):
        """Run an initial scan (if configured) then rescan on an interval."""
        if self._thread:
            return
        self._thread = threading.Thread(
            target=self._loop, name="lumen-scanner", daemon=True
        )
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        if self.config.scan_on_start:
            self.scan()
        interval = max(60, self.config.scan_interval) if self.config.scan_interval else 0
        if not interval:
            return
        while not self._stop.wait(interval):
            self.scan()

    def trigger(self):
        """Kick off a scan in the background. Returns False if one is running."""
        with self._lock:
            if self._running:
                return False
        threading.Thread(target=self.scan, name="lumen-scan-once", daemon=True).start()
        return True

    # -- the scan itself ------------------------------------------------

    def scan(self):
        with self._lock:
            if self._running:
                return self.status
            self._running = True

        started = time.time()
        self.status.update({
            "running": True, "started_at": started, "finished_at": None,
            "scanned": 0, "added": 0, "updated": 0, "removed": 0, "errors": 0,
            "current": None, "last_error": None,
        })
        log.info("scan started over %d root(s)", len(self.config.media_dirs))

        try:
            for root in self.config.media_dirs:
                if not os.path.isdir(root):
                    log.warning("media dir missing or unreadable: %s", root)
                    self.status["errors"] += 1
                    self.status["last_error"] = "unreadable media dir: %s" % root
                    continue
                self._scan_root(root, started)
            self._remove_missing(started)
        except Exception as exc:                       # keep the thread alive
            log.exception("scan aborted")
            self.status["errors"] += 1
            self.status["last_error"] = str(exc)
        finally:
            self.status["running"] = False
            self.status["current"] = None
            self.status["finished_at"] = time.time()
            with self._lock:
                self._running = False
            log.info(
                "scan finished in %.1fs: %d scanned, %d added, %d updated, %d removed",
                self.status["finished_at"] - started, self.status["scanned"],
                self.status["added"], self.status["updated"], self.status["removed"],
            )
        return self.status

    def _scan_root(self, root, seen_at):
        extensions = self.config.extensions
        for dirpath, dirnames, filenames in os.walk(root, onerror=self._walk_error):
            # Skip hidden folders and the usual non-media junk.
            dirnames[:] = sorted(
                d for d in dirnames
                if not d.startswith(".") and d.lower() not in SKIP_DIRS
            )
            for filename in sorted(filenames):
                if self._stop.is_set():
                    return
                if filename.startswith("."):
                    continue
                if not filename.lower().endswith(extensions):
                    continue
                path = os.path.join(dirpath, filename)
                try:
                    self._ingest(path, root, seen_at)
                except Exception as exc:
                    log.warning("could not index %s: %s", path, exc)
                    self.status["errors"] += 1
                    self.status["last_error"] = "%s: %s" % (filename, exc)

    def _walk_error(self, exc):
        log.warning("cannot read directory: %s", exc)
        self.status["errors"] += 1

    def _ingest(self, path, root, seen_at):
        stat = os.stat(path)
        if stat.st_size == 0:
            return
        self.status["scanned"] += 1
        self.status["current"] = os.path.basename(path)

        row = self.db.query_one(
            "SELECT id, size, mtime, probed FROM items WHERE path = ?", (path,)
        )
        unchanged = (
            row is not None
            and row["size"] == stat.st_size
            and abs(row["mtime"] - stat.st_mtime) < 1.0
        )

        if unchanged:
            self.db.execute(
                "UPDATE items SET seen_at = ? WHERE id = ?", (seen_at, row["id"])
            )
            if row["probed"]:
                return
            # Previously failed to probe. Try once more: ffmpeg may now exist.

        info = naming.parse(path, root)
        details = probe.probe(path, self.config.ffprobe)

        subs = subtitles.find_sidecars(path)
        if details:
            subs = subs + details.get("embedded_subtitles", [])
        # Renumber so indexes are stable across sidecar + embedded tracks.
        for position, track in enumerate(subs):
            track["index"] = position

        fields = {
            "path": path,
            "root": root,
            "kind": info["kind"],
            "title": info["title"],
            "series": info["series"],
            "season": info["season"],
            "episode": info["episode"],
            "year": info["year"],
            "size": stat.st_size,
            "mtime": stat.st_mtime,
            "duration": (details or {}).get("duration"),
            "width": (details or {}).get("width"),
            "height": (details or {}).get("height"),
            "vcodec": (details or {}).get("vcodec"),
            "acodec": (details or {}).get("acodec"),
            "channels": (details or {}).get("channels"),
            "container": (details or {}).get("container") or info["container"],
            "subtitles": json.dumps(subs),
            "probed": 1 if details else 0,
            "seen_at": seen_at,
        }

        if row is None:
            fields["added_at"] = time.time()
            columns = ", ".join(fields)
            placeholders = ", ".join("?" for _ in fields)
            self.db.execute(
                "INSERT INTO items (%s) VALUES (%s)" % (columns, placeholders),
                tuple(fields.values()),
            )
            self.status["added"] += 1
        else:
            assignments = ", ".join("%s = ?" % k for k in fields)
            self.db.execute(
                "UPDATE items SET %s WHERE id = ?" % assignments,
                tuple(fields.values()) + (row["id"],),
            )
            if not unchanged:
                self.status["updated"] += 1

    def _remove_missing(self, seen_at):
        """Drop rows for files not seen in this pass.

        Only roots that were actually readable this time are considered, so an
        unmounted drive does not wipe its half of the library.
        """
        readable = [r for r in self.config.media_dirs if os.path.isdir(r)]
        if not readable:
            return
        placeholders = ", ".join("?" for _ in readable)
        stale = self.db.query(
            "SELECT id, path FROM items WHERE seen_at < ? AND root IN (%s)" % placeholders,
            (seen_at,) + tuple(readable),
        )
        gone = [(row["id"],) for row in stale if not os.path.exists(row["path"])]
        if gone:
            self.db.executemany("DELETE FROM items WHERE id = ?", gone)
            self.status["removed"] = len(gone)
