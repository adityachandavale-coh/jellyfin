"""Users, sessions and signed stream tokens.

Passwords are hashed with ``hashlib.scrypt`` (stdlib, no bcrypt dependency).
Browser sessions are opaque random tokens kept in SQLite and sent as an
HttpOnly cookie. Stream tokens are stateless HMACs so a plain URL can be
pasted into VLC or Kodi without those clients handling cookies.
"""

import base64
import hashlib
import hmac
import logging
import secrets
import time

log = logging.getLogger("lumen.auth")

COOKIE_NAME = "lumen_session"

# scrypt parameters: ~16 MB of memory per hash, which is a sensible desktop
# cost and still trivial for a login endpoint that is rate limited.
SCRYPT_N = 1 << 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32


class AuthError(Exception):
    pass


class Auth:
    def __init__(self, db, config):
        self.db = db
        self.config = config
        self.secret = self._load_secret()

    def _load_secret(self):
        """The HMAC key used for stream tokens, generated once per install."""
        stored = self.db.get_meta("hmac_secret")
        if stored:
            return base64.urlsafe_b64decode(stored.encode("ascii"))
        secret = secrets.token_bytes(32)
        self.db.set_meta("hmac_secret", base64.urlsafe_b64encode(secret).decode("ascii"))
        return secret

    # -- users ----------------------------------------------------------

    @staticmethod
    def hash_password(password, salt=None):
        salt = salt or secrets.token_bytes(16)
        digest = hashlib.scrypt(
            password.encode("utf-8"), salt=salt,
            n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_DKLEN,
            maxmem=64 * 1024 * 1024,
        )
        return salt, digest

    def user_count(self):
        row = self.db.query_one("SELECT COUNT(*) AS n FROM users")
        return row["n"] if row else 0

    def create_user(self, username, password, is_admin=False):
        username = (username or "").strip()
        if not username:
            raise AuthError("username cannot be empty")
        if len(password or "") < 8:
            raise AuthError("password must be at least 8 characters")
        if self.db.query_one("SELECT id FROM users WHERE username = ?", (username,)):
            raise AuthError("user %r already exists" % username)
        salt, digest = self.hash_password(password)
        cursor = self.db.execute(
            "INSERT INTO users (username, salt, pwhash, is_admin, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (username, salt, digest, 1 if is_admin else 0, time.time()),
        )
        log.info("created user %r (admin=%s)", username, bool(is_admin))
        return cursor.lastrowid

    def set_password(self, username, password):
        if len(password or "") < 8:
            raise AuthError("password must be at least 8 characters")
        row = self.db.query_one("SELECT id FROM users WHERE username = ?", (username,))
        if not row:
            raise AuthError("no such user: %s" % username)
        salt, digest = self.hash_password(password)
        self.db.execute(
            "UPDATE users SET salt = ?, pwhash = ? WHERE id = ?",
            (salt, digest, row["id"]),
        )
        # Force a re-login everywhere.
        self.db.execute("DELETE FROM sessions WHERE user_id = ?", (row["id"],))

    def delete_user(self, username):
        row = self.db.query_one("SELECT id FROM users WHERE username = ?", (username,))
        if not row:
            raise AuthError("no such user: %s" % username)
        self.db.execute("DELETE FROM users WHERE id = ?", (row["id"],))

    def list_users(self):
        return self.db.query(
            "SELECT id, username, is_admin, created_at FROM users ORDER BY username"
        )

    def verify_password(self, username, password):
        row = self.db.query_one(
            "SELECT id, username, salt, pwhash, is_admin FROM users WHERE username = ?",
            (username or "",),
        )
        if not row:
            # Spend roughly the same time as a real check so the response time
            # does not reveal whether the account exists.
            self.hash_password(password or "")
            return None
        _, digest = self.hash_password(password or "", row["salt"])
        if not hmac.compare_digest(digest, row["pwhash"]):
            return None
        return row

    # -- browser sessions -------------------------------------------------

    def start_session(self, user_id):
        token = secrets.token_urlsafe(32)
        now = time.time()
        self.db.execute(
            "INSERT INTO sessions (token, user_id, created_at, expires_at) "
            "VALUES (?, ?, ?, ?)",
            (token, user_id, now, now + self.config.session_days * 86400),
        )
        return token

    def session_user(self, token):
        if not token:
            return None
        row = self.db.query_one(
            "SELECT u.id, u.username, u.is_admin, s.expires_at "
            "FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token = ?",
            (token,),
        )
        if not row:
            return None
        if row["expires_at"] < time.time():
            self.db.execute("DELETE FROM sessions WHERE token = ?", (token,))
            return None
        return row

    def end_session(self, token):
        if token:
            self.db.execute("DELETE FROM sessions WHERE token = ?", (token,))

    # -- stream tokens ----------------------------------------------------

    def issue_stream_token(self, item_id, user_id, seconds=None):
        """A signed, expiring token that grants read access to one item."""
        seconds = seconds or self.config.stream_token_seconds
        expires = int(time.time() + seconds)
        payload = "%d.%d.%d" % (int(item_id), int(user_id), expires)
        signature = self._sign(payload)
        return "%s.%s" % (payload, signature)

    def check_stream_token(self, token, item_id):
        """Return the user id the token was issued to, or ``None``."""
        if not token:
            return None
        parts = token.split(".")
        if len(parts) != 4:
            return None
        payload = ".".join(parts[:3])
        try:
            token_item, user_id, expires = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            return None
        if not hmac.compare_digest(self._sign(payload), parts[3]):
            return None
        if token_item != int(item_id) or expires < time.time():
            return None
        return user_id

    def _sign(self, payload):
        digest = hmac.new(self.secret, payload.encode("utf-8"), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
