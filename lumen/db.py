"""SQLite storage.

One file holds the library index, users, sessions, playback progress and the
server's HMAC secret. Connections are per-thread because sqlite3 objects
cannot be shared across threads.
"""

import os
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id         INTEGER PRIMARY KEY,
    path       TEXT NOT NULL UNIQUE,
    root       TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'movie',
    title      TEXT NOT NULL,
    series     TEXT,
    season     INTEGER,
    episode    INTEGER,
    year       INTEGER,
    size       INTEGER NOT NULL DEFAULT 0,
    mtime      REAL NOT NULL DEFAULT 0,
    duration   REAL,
    width      INTEGER,
    height     INTEGER,
    vcodec     TEXT,
    acodec     TEXT,
    channels   INTEGER,
    container  TEXT,
    subtitles  TEXT,
    probed     INTEGER NOT NULL DEFAULT 0,
    added_at   REAL NOT NULL DEFAULT 0,
    seen_at    REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS items_title  ON items(title);
CREATE INDEX IF NOT EXISTS items_series ON items(series, season, episode);
CREATE INDEX IF NOT EXISTS items_kind   ON items(kind);

CREATE TABLE IF NOT EXISTS users (
    id         INTEGER PRIMARY KEY,
    username   TEXT NOT NULL UNIQUE COLLATE NOCASE,
    salt       BLOB NOT NULL,
    pwhash     BLOB NOT NULL,
    is_admin   INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS progress (
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    item_id    INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    position   REAL NOT NULL DEFAULT 0,
    duration   REAL,
    finished   INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, item_id)
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path):
        self.path = path
        self._local = threading.local()
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        # The write lock serialises writers; readers go straight through
        # thanks to WAL mode.
        self._write_lock = threading.Lock()
        self._init_schema()

    @property
    def conn(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    def _init_schema(self):
        with self._write_lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def query(self, sql, params=()):
        return self.conn.execute(sql, params).fetchall()

    def query_one(self, sql, params=()):
        return self.conn.execute(sql, params).fetchone()

    def execute(self, sql, params=()):
        with self._write_lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def executemany(self, sql, seq):
        with self._write_lock:
            cur = self.conn.executemany(sql, seq)
            self.conn.commit()
            return cur

    # -- meta key/value -------------------------------------------------

    def get_meta(self, key, default=None):
        row = self.query_one("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_meta(self, key, value):
        self.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # -- housekeeping ---------------------------------------------------

    def purge_expired_sessions(self):
        self.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))

    def close(self):
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
