"""Storage for the Anvil cloud server: users, encrypted settings, tokens and device codes.

Everything lives in one SQLite file. Settings (API key included) are encrypted with Fernet
(AES-128-CBC + HMAC-SHA256) using a master key kept in a separate 0600 file. Tokens are random
and only their SHA-256 hash is stored, so a copy of the database can't be used to sign in.
"""

import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path

ACCESS_TTL = 3600                 # 1 hour
REFRESH_TTL = 30 * 24 * 3600      # 30 days, extended each time it is used
WEB_SESSION_TTL = 12 * 3600
DEVICE_TTL = 600                  # a login code is valid for 10 minutes
DEVICE_INTERVAL = 3               # seconds between polls
STATE_TTL = 600
USER_CODE_ALPHABET = "BCDFGHJKLMNPQRSTVWXZ"  # no vowels (no accidental words), no 0/O 1/I confusion

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    email TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', settings BLOB, updated REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tokens (
    hash TEXT PRIMARY KEY, kind TEXT NOT NULL, email TEXT NOT NULL, label TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL, expires REAL NOT NULL, last_used REAL NOT NULL DEFAULT 0, parent TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS tokens_email ON tokens(email);
CREATE TABLE IF NOT EXISTS devices (
    device_hash TEXT PRIMARY KEY, user_code TEXT UNIQUE NOT NULL, label TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL, expires REAL NOT NULL, email TEXT, denied INTEGER NOT NULL DEFAULT 0,
    last_poll REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS oauth_states (
    state TEXT PRIMARY KEY, verifier TEXT NOT NULL, next TEXT NOT NULL, expires REAL NOT NULL
);
"""


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def load_master_key(path: Path) -> bytes:
    """Read the Fernet key, creating it (0600) on first use."""
    from cryptography.fernet import Fernet
    path = Path(path)
    try:
        return path.read_bytes().strip()
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    return key


def new_user_code() -> str:
    raw = "".join(secrets.choice(USER_CODE_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def normalize_user_code(code: str) -> str:
    raw = "".join(ch for ch in (code or "").upper() if ch in USER_CODE_ALPHABET)
    return f"{raw[:4]}-{raw[4:]}" if len(raw) == 8 else ""


class Store:
    def __init__(self, db_path: Path, master_key: bytes, clock=time.time):
        from cryptography.fernet import Fernet
        self.fernet = Fernet(master_key)
        self.clock = clock
        self.lock = threading.Lock()
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        if not db_path.exists():
            os.close(os.open(db_path, os.O_WRONLY | os.O_CREAT, 0o600))
        self.db = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def _q(self, sql: str, args=()):
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def cleanup(self) -> None:
        now = self.clock()
        with self.lock:
            self.db.execute("DELETE FROM tokens WHERE expires < ?", (now,))
            self.db.execute("DELETE FROM devices WHERE expires < ?", (now,))
            self.db.execute("DELETE FROM oauth_states WHERE expires < ?", (now,))

    # users and settings -------------------------------------------------------
    def upsert_user(self, email: str, name: str = "") -> None:
        self._q("INSERT INTO users(email, name) VALUES(?, ?) ON CONFLICT(email) DO UPDATE SET name=excluded.name",
                (email, name))

    def user_name(self, email: str) -> str:
        rows = self._q("SELECT name FROM users WHERE email=?", (email,))
        return rows[0][0] if rows else ""

    def get_settings(self, email: str) -> dict:
        rows = self._q("SELECT settings FROM users WHERE email=?", (email,))
        if not rows or not rows[0][0]:
            return {}
        return json.loads(self.fernet.decrypt(rows[0][0]))

    def put_settings(self, email: str, settings: dict) -> None:
        blob = self.fernet.encrypt(json.dumps(settings).encode())
        self._q("INSERT INTO users(email, settings, updated) VALUES(?, ?, ?) "
                "ON CONFLICT(email) DO UPDATE SET settings=excluded.settings, updated=excluded.updated",
                (email, blob, self.clock()))

    # tokens -----------------------------------------------------------------
    def _issue(self, kind: str, email: str, ttl: int, label: str = "", parent: str = "") -> str:
        tok = f"anv_{kind[0]}_" + secrets.token_urlsafe(32)
        now = self.clock()
        self._q("INSERT INTO tokens(hash, kind, email, label, created, expires, parent) VALUES(?,?,?,?,?,?,?)",
                (_hash(tok), kind, email, label, now, now + ttl, parent))
        return tok

    def issue_pair(self, email: str, label: str) -> dict:
        refresh = self._issue("refresh", email, REFRESH_TTL, label)
        access = self._issue("access", email, ACCESS_TTL, label, parent=_hash(refresh))
        return {"access_token": access, "refresh_token": refresh, "expires_in": ACCESS_TTL, "email": email}

    def check(self, token: str, kind: str) -> str | None:
        """The email a live token belongs to, or None."""
        if not token:
            return None
        rows = self._q("SELECT email, expires FROM tokens WHERE hash=? AND kind=?", (_hash(token), kind))
        if not rows or rows[0][1] < self.clock():
            return None
        return rows[0][0]

    def refresh(self, refresh_token: str) -> dict | None:
        email = self.check(refresh_token, "refresh")
        if not email:
            return None
        h, now = _hash(refresh_token), self.clock()
        rows = self._q("SELECT label FROM tokens WHERE hash=?", (h,))
        self._q("UPDATE tokens SET last_used=?, expires=? WHERE hash=?", (now, now + REFRESH_TTL, h))
        access = self._issue("access", email, ACCESS_TTL, rows[0][0] if rows else "", parent=h)
        return {"access_token": access, "expires_in": ACCESS_TTL, "email": email}

    def revoke(self, token: str) -> None:
        h = _hash(token)
        self._q("DELETE FROM tokens WHERE hash=? OR parent=?", (h, h))

    def revoke_refresh(self, refresh_hash: str, email: str) -> None:
        self._q("DELETE FROM tokens WHERE (hash=? OR parent=?) AND email=?", (refresh_hash, refresh_hash, email))

    def revoke_all(self, email: str) -> None:
        self._q("DELETE FROM tokens WHERE email=? AND kind IN ('refresh', 'access')", (email,))

    def list_logins(self, email: str) -> list[dict]:
        rows = self._q("SELECT hash, label, created, last_used FROM tokens WHERE email=? AND kind='refresh' "
                       "AND expires>=? ORDER BY created DESC", (email, self.clock()))
        return [{"id": r[0], "label": r[1], "created": r[2], "last_used": r[3]} for r in rows]

    def new_web_session(self, email: str) -> tuple[str, str]:
        """(cookie value, CSRF token). The CSRF token is derived from the cookie, so it needs no storage."""
        tok = self._issue("web", email, WEB_SESSION_TTL)
        return tok, self.csrf_for(tok)

    @staticmethod
    def csrf_for(web_token: str) -> str:
        return hashlib.sha256(b"csrf:" + web_token.encode()).hexdigest()[:32]

    # device login -------------------------------------------------------------
    def new_device(self, label: str) -> dict:
        device_code = secrets.token_urlsafe(32)
        now = self.clock()
        for _ in range(5):
            user_code = new_user_code()
            try:
                self._q("INSERT INTO devices(device_hash, user_code, label, created, expires) VALUES(?,?,?,?,?)",
                        (_hash(device_code), user_code, label[:80], now, now + DEVICE_TTL))
                break
            except sqlite3.IntegrityError:
                continue
        else:
            raise RuntimeError("could not allocate a login code")
        return {"device_code": device_code, "user_code": user_code, "expires_in": DEVICE_TTL,
                "interval": DEVICE_INTERVAL}

    def device_by_user_code(self, user_code: str) -> dict | None:
        code = normalize_user_code(user_code)
        if not code:
            return None
        rows = self._q("SELECT label, expires, email, denied FROM devices WHERE user_code=?", (code,))
        if not rows or rows[0][1] < self.clock() or rows[0][2] or rows[0][3]:
            return None
        return {"user_code": code, "label": rows[0][0]}

    def decide_device(self, user_code: str, email: str | None, approve: bool) -> bool:
        if not self.device_by_user_code(user_code):
            return False
        code = normalize_user_code(user_code)
        if approve:
            self._q("UPDATE devices SET email=? WHERE user_code=?", (email, code))
        else:
            self._q("UPDATE devices SET denied=1 WHERE user_code=?", (code,))
        return True

    def poll_device(self, device_code: str) -> tuple[str, dict | None]:
        """("authorization_pending" | "slow_down" | "access_denied" | "expired_token" | "ok", tokens)."""
        h, now = _hash(device_code or ""), self.clock()
        rows = self._q("SELECT label, expires, email, denied, last_poll FROM devices WHERE device_hash=?", (h,))
        if not rows or rows[0][1] < now:
            return "expired_token", None
        label, _, email, denied, last_poll = rows[0]
        if denied:
            self._q("DELETE FROM devices WHERE device_hash=?", (h,))
            return "access_denied", None
        if email:
            self._q("DELETE FROM devices WHERE device_hash=?", (h,))  # one use only
            return "ok", self.issue_pair(email, label)
        self._q("UPDATE devices SET last_poll=? WHERE device_hash=?", (now, h))
        if now - last_poll < DEVICE_INTERVAL - 0.5:
            return "slow_down", None
        return "authorization_pending", None

    # OAuth state --------------------------------------------------------------
    def new_oauth_state(self, verifier: str, next_path: str) -> str:
        state = secrets.token_urlsafe(24)
        self._q("INSERT INTO oauth_states(state, verifier, next, expires) VALUES(?,?,?,?)",
                (state, verifier, next_path, self.clock() + STATE_TTL))
        return state

    def take_oauth_state(self, state: str) -> tuple[str, str] | None:
        rows = self._q("SELECT verifier, next, expires FROM oauth_states WHERE state=?", (state or "",))
        self._q("DELETE FROM oauth_states WHERE state=?", (state or "",))
        if not rows or rows[0][2] < self.clock():
            return None
        return rows[0][0], rows[0][1]
