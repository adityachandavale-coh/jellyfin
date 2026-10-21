"""On-the-fly transcoding.

ffmpeg writes a *progressive* fragmented MP4 (or WebM) straight to its stdout
and Lumen pipes that into the HTTP response. A plain ``<video src>`` plays it
with no JavaScript player library involved.

The trade-off versus HLS is seeking: a progressive stream has no index, so the
client can only seek inside what it has buffered. Seeking further restarts
ffmpeg at a new offset (``?t=``) and the UI tracks the offset so the scrub bar
still shows absolute time. In exchange there is no segment cache to manage, no
playlist to serve, and no third-party player to vendor.
"""

import logging
import os
import subprocess
import threading
import time
import uuid

log = logging.getLogger("lumen.transcode")

# Kept per session so a failure can be reported instead of a silent black screen.
STDERR_LINES = 24


class TranscodeError(Exception):
    pass


class Session:
    def __init__(self, item_id, path, mode, container, start):
        self.id = uuid.uuid4().hex[:12]
        self.item_id = item_id
        self.path = path
        self.mode = mode
        self.container = container
        self.start = start
        self.proc = None
        self.started_at = time.time()
        self.bytes_sent = 0
        self.stderr_tail = []
        self._stderr_thread = None
        # Used by the reaper to spot a stream nobody is reading any more.
        self._last_bytes = 0
        self._last_change = time.time()

    def describe(self):
        return {
            "id": self.id,
            "item_id": self.item_id,
            "mode": self.mode,
            "container": self.container,
            "start": self.start,
            "running_for": round(time.time() - self.started_at, 1),
            "bytes_sent": self.bytes_sent,
            "file": os.path.basename(self.path),
        }


class TranscodeManager:
    def __init__(self, config):
        self.config = config
        self._sessions = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._reaper = threading.Thread(
            target=self._reap_loop, name="lumen-transcode-reaper", daemon=True
        )
        self._reaper.start()

    def _reap_loop(self):
        """Kill transcodes whose output nobody has read for a while.

        Normally a session ends when the client disconnects and the read loop
        in the request handler finishes. But a client that disappears without
        closing the TCP connection leaves ffmpeg blocked on a full stdout
        pipe, holding a process and a thread indefinitely. Watching
        ``bytes_sent`` catches that; a long pause is also reaped, and resuming
        simply restarts ffmpeg at the current offset.
        """
        while not self._stop.wait(30):
            timeout = max(60, self.config.session_idle_timeout)
            now = time.time()
            for session in list(self._sessions.values()):
                if session.bytes_sent != session._last_bytes:
                    session._last_bytes = session.bytes_sent
                    session._last_change = now
                    continue
                if now - session._last_change > timeout:
                    log.info("reaping idle transcode %s (%ds without a read)",
                             session.id, int(now - session._last_change))
                    self.stop(session)

    # -- introspection --------------------------------------------------

    def active(self):
        with self._lock:
            return [s.describe() for s in self._sessions.values()]

    def count(self):
        with self._lock:
            return len(self._sessions)

    # -- lifecycle --------------------------------------------------------

    def start(self, item_id, path, mode, container, start=0.0):
        """Spawn ffmpeg and return a running :class:`Session`."""
        if not self.config.transcode_enabled:
            raise TranscodeError("transcoding is disabled in the config")

        with self._lock:
            if len(self._sessions) >= max(1, self.config.max_sessions):
                raise TranscodeError(
                    "too many concurrent transcodes (%d); try again shortly"
                    % self.config.max_sessions
                )

        session = Session(item_id, path, mode, container, start)
        cmd = self.build_command(path, mode, container, start)
        log.info("transcode %s: %s", session.id, " ".join(cmd))

        try:
            session.proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                bufsize=0,
            )
        except OSError as exc:
            raise TranscodeError(
                "could not run %r: %s. Install ffmpeg or set transcode.enabled=false"
                % (self.config.ffmpeg, exc)
            )

        # ffmpeg writes progress to stderr; if nobody drains that pipe it
        # fills and ffmpeg blocks forever.
        session._stderr_thread = threading.Thread(
            target=self._drain_stderr, args=(session,),
            name="lumen-ffmpeg-err-%s" % session.id, daemon=True,
        )
        session._stderr_thread.start()

        with self._lock:
            self._sessions[session.id] = session
        return session

    def stop(self, session):
        """Terminate ffmpeg and forget the session. Safe to call twice."""
        with self._lock:
            self._sessions.pop(session.id, None)
        proc = session.proc
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                log.warning("ffmpeg %s ignored SIGTERM; killing", session.id)
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    log.error("ffmpeg %s will not die", session.id)
        for pipe in (proc.stdout, proc.stderr):
            try:
                if pipe:
                    pipe.close()
            except OSError:
                pass
        log.info("transcode %s stopped after %.1fs, %d bytes",
                 session.id, time.time() - session.started_at, session.bytes_sent)

    def stop_all(self):
        self._stop.set()
        for session in list(self._sessions.values()):
            self.stop(session)

    def _drain_stderr(self, session):
        pipe = session.proc.stderr
        if pipe is None:
            return
        try:
            for raw in iter(pipe.readline, b""):
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                session.stderr_tail.append(line)
                if len(session.stderr_tail) > STDERR_LINES:
                    session.stderr_tail.pop(0)
        except (OSError, ValueError):
            pass

    # -- command construction ---------------------------------------------

    def build_command(self, path, mode, container, start=0.0):
        """Assemble the ffmpeg invocation. Separated out so it can be tested."""
        cfg = self.config
        cmd = [cfg.ffmpeg, "-hide_banner", "-loglevel", "warning", "-nostdin"]

        # Seeking before -i is the fast path: ffmpeg jumps to the nearest
        # keyframe rather than decoding everything up to that point.
        if start and start > 0:
            cmd += ["-ss", "%.3f" % start]

        cmd += ["-i", path]
        # First video and first audio track; the "?" makes audio optional so a
        # silent file still plays.
        cmd += ["-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn"]

        if mode == "remux":
            cmd += ["-c", "copy"]
        else:
            height = max(240, cfg.transcode_max_height)
            cmd += [
                "-c:v", "libx264",
                "-preset", cfg.transcode_preset,
                "-crf", str(cfg.transcode_crf),
                "-profile:v", "high",
                "-pix_fmt", "yuv420p",
                # Downscale only when the source is taller; -2 keeps the width
                # even, which h264 requires.
                "-vf", "scale=-2:'min(ih,%d)'" % height,
                "-c:a", "aac",
                "-b:a", "192k",
                "-ac", "2",
            ]

        if container == "webm":
            cmd += ["-f", "webm"]
        else:
            cmd += [
                "-movflags", "frag_keyframe+empty_moov+default_base_moof",
                "-f", "mp4",
            ]

        cmd += ["-"]
        return cmd

    @staticmethod
    def content_type(container):
        return "video/webm" if container == "webm" else "video/mp4"


def ffmpeg_available(ffmpeg="ffmpeg"):
    try:
        subprocess.run(
            [ffmpeg, "-version"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=10, check=True,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False
