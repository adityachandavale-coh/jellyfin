"""Configuration loading.

Lumen reads a single INI file. Every value has a usable default so an empty
config file still starts the server; in practice only ``media_dirs`` needs to
be set.
"""

import configparser
import os
from dataclasses import dataclass, field

DEFAULT_EXTENSIONS = (
    ".mp4", ".m4v", ".mkv", ".webm", ".avi", ".mov",
    ".mpg", ".mpeg", ".ts", ".m2ts", ".wmv", ".flv",
)


@dataclass
class Config:
    host: str = "0.0.0.0"
    port: int = 8096
    # Used when building absolute URLs (stream links for external players).
    # Empty means "derive it from the request's Host header".
    public_url: str = ""

    database: str = "/var/lib/lumen/lumen.db"
    cache_dir: str = "/var/cache/lumen"

    media_dirs: list = field(default_factory=list)
    extensions: tuple = DEFAULT_EXTENSIONS
    scan_interval: int = 900
    scan_on_start: bool = True

    transcode_enabled: bool = True
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    transcode_crf: int = 23
    transcode_preset: str = "veryfast"
    transcode_max_height: int = 1080
    segment_seconds: int = 4
    session_idle_timeout: int = 900
    max_sessions: int = 4

    session_days: int = 30
    stream_token_seconds: int = 86400

    path: str = ""


def _split_list(raw):
    """Split a multi-line or comma-separated INI value into clean entries."""
    out = []
    for chunk in raw.replace(",", "\n").splitlines():
        chunk = chunk.strip()
        if chunk:
            out.append(chunk)
    return out


def load(path=None):
    """Load config from ``path``. A missing file is not an error."""
    cfg = Config()
    if path:
        cfg.path = os.path.abspath(path)

    parser = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    if path and os.path.exists(path):
        parser.read(path, encoding="utf-8")

    def get(section, option, fallback):
        if parser.has_option(section, option):
            return parser.get(section, option).strip()
        return fallback

    def get_int(section, option, fallback):
        raw = get(section, option, None)
        if raw in (None, ""):
            return fallback
        try:
            return int(raw)
        except ValueError:
            raise SystemExit(
                "config: [%s] %s must be a whole number, got %r" % (section, option, raw)
            )

    def get_bool(section, option, fallback):
        raw = get(section, option, None)
        if raw in (None, ""):
            return fallback
        return raw.lower() in ("1", "true", "yes", "on")

    cfg.host = get("server", "host", cfg.host)
    cfg.port = get_int("server", "port", cfg.port)
    cfg.public_url = get("server", "public_url", cfg.public_url).rstrip("/")

    cfg.database = get("paths", "database", cfg.database)
    cfg.cache_dir = get("paths", "cache", cfg.cache_dir)

    cfg.media_dirs = [
        os.path.abspath(os.path.expanduser(p))
        for p in _split_list(get("library", "media_dirs", ""))
    ]
    exts = _split_list(get("library", "extensions", ""))
    if exts:
        cfg.extensions = tuple(
            e if e.startswith(".") else "." + e for e in (x.lower() for x in exts)
        )
    cfg.scan_interval = get_int("library", "scan_interval", cfg.scan_interval)
    cfg.scan_on_start = get_bool("library", "scan_on_start", cfg.scan_on_start)

    cfg.transcode_enabled = get_bool("transcode", "enabled", cfg.transcode_enabled)
    cfg.ffmpeg = get("transcode", "ffmpeg", cfg.ffmpeg)
    cfg.ffprobe = get("transcode", "ffprobe", cfg.ffprobe)
    cfg.transcode_crf = get_int("transcode", "crf", cfg.transcode_crf)
    cfg.transcode_preset = get("transcode", "preset", cfg.transcode_preset)
    cfg.transcode_max_height = get_int("transcode", "max_height", cfg.transcode_max_height)
    cfg.segment_seconds = get_int("transcode", "segment_seconds", cfg.segment_seconds)
    cfg.session_idle_timeout = get_int(
        "transcode", "idle_timeout", cfg.session_idle_timeout
    )
    cfg.max_sessions = get_int("transcode", "max_sessions", cfg.max_sessions)

    cfg.session_days = get_int("auth", "session_days", cfg.session_days)
    cfg.stream_token_seconds = get_int(
        "auth", "stream_token_seconds", cfg.stream_token_seconds
    )

    return cfg
