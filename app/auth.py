"""Signing in from other devices (see remote.py). Requests from this Mac never need it.

The passcode is stored only as a salted scrypt hash. Signing in gives the
device a long random token in a cookie it can't read from JavaScript; only the
token's SHA-256 is stored, so the database alone can't be used to sign in.
Wrong guesses are limited per device and overall.
"""
import hashlib
import hmac
import secrets
import threading
import time
from collections import deque

from . import db

COOKIE = "penny_session"
SESSION_DAYS = 180
MIN_PASSCODE = 8
_SCRYPT = {"n": 2 ** 15, "r": 8, "p": 1}

# Wrong passcodes: 5 per device per 15 minutes, 20 overall per hour.
_PER_DEVICE, _DEVICE_WINDOW = 5, 15 * 60
_OVERALL, _OVERALL_WINDOW = 20, 60 * 60
_failures: dict[str, deque] = {}
_all_failures: deque = deque()
_lock = threading.Lock()


def _hash(passcode: str, salt: bytes) -> bytes:
    return hashlib.scrypt(passcode.encode(), salt=salt, maxmem=64 * 1024 * 1024, dklen=32, **_SCRYPT)


def set_passcode(conn, passcode: str):
    if len(passcode) < MIN_PASSCODE:
        raise ValueError(f"Use at least {MIN_PASSCODE} characters.")
    salt = secrets.token_bytes(16)
    db.set_setting(conn, "remote_passcode", f"scrypt${salt.hex()}${_hash(passcode, salt).hex()}")
    sign_out_all(conn)  # a new passcode signs every device out


def check_passcode(conn, passcode: str) -> bool:
    stored = db.get_setting(conn, "remote_passcode")
    if not stored:
        return False
    _, salt, digest = stored.split("$")
    return hmac.compare_digest(_hash(passcode, bytes.fromhex(salt)).hex(), digest)


def locked_out(client: str) -> bool:
    now = time.time()
    with _lock:
        for q, window in ((_failures.setdefault(client, deque()), _DEVICE_WINDOW), (_all_failures, _OVERALL_WINDOW)):
            while q and q[0] < now - window:
                q.popleft()
        return len(_failures[client]) >= _PER_DEVICE or len(_all_failures) >= _OVERALL


def note_failure(client: str):
    with _lock:
        now = time.time()
        _failures.setdefault(client, deque()).append(now)
        _all_failures.append(now)


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_session(conn, device: str) -> str:
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    conn.execute("INSERT INTO remote_sessions (token_hash, created, last_seen, device) VALUES (?, ?, ?, ?)",
                 (_digest(token), now, now, device[:200]))
    return token


def valid_session(conn, token: str | None) -> bool:
    if not token:
        return False
    row = conn.execute("SELECT created, last_seen FROM remote_sessions WHERE token_hash = ?",
                       (_digest(token),)).fetchone()
    if not row or row["created"] < time.time() - SESSION_DAYS * 86400:
        return False
    if row["last_seen"] < time.time() - 3600:  # note use about once an hour, not every request
        with conn:
            conn.execute("UPDATE remote_sessions SET last_seen = ? WHERE token_hash = ?",
                         (int(time.time()), _digest(token)))
    return True


def end_session(conn, token: str | None):
    if token:
        conn.execute("DELETE FROM remote_sessions WHERE token_hash = ?", (_digest(token),))


def sign_out_all(conn):
    conn.execute("DELETE FROM remote_sessions")
