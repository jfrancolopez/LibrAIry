from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from dataclasses import dataclass

from fastapi import HTTPException, Request

SESSION_COOKIE = "librairy_session"
ADMIN_PASSWORD_KEY = "auth.admin_password"
WELCOME_DISMISSED_KEY = "ux.welcome_dismissed"
SESSION_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1


@dataclass(frozen=True)
class Session:
    token: str
    csrf_token: str


class LoginRateLimiter:
    def __init__(self) -> None:
        self.failures: dict[str, list[float]] = {}

    def check(self, key: str) -> None:
        now = time.time()
        recent = [stamp for stamp in self.failures.get(key, []) if now - stamp < 300]
        self.failures[key] = recent
        if len(recent) >= 5:
            raise HTTPException(429, "too many login attempts; retry in a few minutes")

    def record_failure(self, key: str) -> None:
        self.failures.setdefault(key, []).append(time.time())

    def reset(self, key: str) -> None:
        self.failures.pop(key, None)


def has_admin_password(conn: sqlite3.Connection) -> bool:
    return (
        conn.execute("SELECT 1 FROM settings WHERE key=?", (ADMIN_PASSWORD_KEY,)).fetchone()
        is not None
    )


def set_admin_password(conn: sqlite3.Connection, password: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
        (ADMIN_PASSWORD_KEY, json.dumps(hash_password(password))),
    )


def clear_admin_password(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM settings WHERE key=?", (ADMIN_PASSWORD_KEY,))


def portal_is_open(conn: sqlite3.Connection, auth_required: bool) -> bool:
    """True when the portal serves pages without a login: no password, none demanded."""
    return not auth_required and not has_admin_password(conn)


def verify_admin_password(conn: sqlite3.Connection, password: str) -> bool:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (ADMIN_PASSWORD_KEY,)).fetchone()
    if row is None:
        return False
    return verify_password(password, json.loads(row["value"]))


def hash_password(
    password: str, *, n: int = SCRYPT_N, r: int = SCRYPT_R, p: int = SCRYPT_P
) -> dict:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p)
    return {"algorithm": "scrypt", "n": n, "r": r, "p": p, "salt": salt.hex(), "hash": digest.hex()}


def verify_password(password: str, stored: dict) -> bool:
    salt = bytes.fromhex(stored["salt"])
    digest = hashlib.scrypt(
        password.encode(),
        salt=salt,
        n=int(stored["n"]),
        r=int(stored["r"]),
        p=int(stored["p"]),
    ).hex()
    return hmac.compare_digest(digest, stored["hash"])


def create_session(conn: sqlite3.Connection) -> Session:
    token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    token_hash = _token_hash(token)
    now = int(time.time())
    expires = now + SESSION_MAX_AGE_SECONDS
    conn.execute(
        "INSERT INTO sessions(token_hash, created_at, expires_at, csrf_token) VALUES (?, ?, ?, ?)",
        (token_hash, str(now), str(expires), csrf_token),
    )
    return Session(token, csrf_token)


def session_row(conn: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM sessions WHERE token_hash=?", (_token_hash(token),)
    ).fetchone()


def session_from_request(conn: sqlite3.Connection, request: Request) -> sqlite3.Row | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    row = conn.execute(
        "SELECT * FROM sessions WHERE token_hash=?", (_token_hash(token),)
    ).fetchone()
    if row is None or int(row["expires_at"]) < int(time.time()):
        if row is not None:
            conn.execute("DELETE FROM sessions WHERE token_hash=?", (row["token_hash"],))
        return None
    expires = int(time.time()) + SESSION_MAX_AGE_SECONDS
    conn.execute(
        "UPDATE sessions SET expires_at=? WHERE token_hash=?", (str(expires), row["token_hash"])
    )
    return row


def delete_session(conn: sqlite3.Connection, token: str | None) -> None:
    if token:
        conn.execute("DELETE FROM sessions WHERE token_hash=?", (_token_hash(token),))


def welcome_banner_visible(conn: sqlite3.Connection, session: sqlite3.Row | None) -> bool:
    if session is None:
        return False
    return (
        conn.execute("SELECT 1 FROM settings WHERE key=?", (WELCOME_DISMISSED_KEY,)).fetchone()
        is None
    )


def dismiss_welcome_banner(conn: sqlite3.Connection, session: sqlite3.Row | None) -> None:
    if session is None:
        return
    conn.execute(
        "INSERT OR REPLACE INTO settings(key, value) VALUES (?, 'true')",
        (WELCOME_DISMISSED_KEY,),
    )


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
