from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from dataclasses import dataclass

from fastapi import HTTPException, Request

from librairy.db import best_effort_write

SESSION_COOKIE = "librairy_session"
ADMIN_PASSWORD_KEY = "auth.admin_password"
WELCOME_DISMISSED_KEY = "ux.welcome_dismissed"
SESSION_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
# Refresh a sliding session only once it is past halfway. Sooner buys nothing
# and costs a database write on every page view.
SESSION_REFRESH_AFTER_SECONDS = SESSION_MAX_AGE_SECONDS // 2
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


def transient_session(csrf_token: str | None = None) -> dict[str, str]:
    """A session-shaped object that was never written down.

    An open portal mints a session on a page load purely so forms have a CSRF
    token; the row carries no authorisation. When the worker holds the writer
    lock that insert cannot happen, and the choice is between failing the page
    and rendering it with a token that will not be honoured later. Rendering
    wins: reading Browse should not depend on a background scan finishing.

    No cookie is set for one of these, so the next request tries again and
    gets a real session the moment the lock frees.
    """
    return {"csrf_token": csrf_token or secrets.token_urlsafe(32), "transient": "1"}


def session_row(conn: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM sessions WHERE token_hash=?", (_token_hash(token),)
    ).fetchone()


def session_from_request(conn: sqlite3.Connection, request: Request) -> sqlite3.Row | None:
    """Validate the session cookie. Reads; writes only when it has to.

    This used to extend the expiry on *every* request — a SQLite write on the
    render path of every page in the portal, competing with the worker for the
    single writer lock. Pushing a seven-day window out by a few milliseconds
    is not worth a write, so the refresh now happens only once the session is
    past halfway, which is the same sliding behaviour with three orders of
    magnitude fewer writes.
    """
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    row = conn.execute(
        "SELECT * FROM sessions WHERE token_hash=?", (_token_hash(token),)
    ).fetchone()
    now = int(time.time())
    if row is None or int(row["expires_at"]) < now:
        if row is not None:
            # Tidying up, not a precondition: the caller gets None regardless.
            best_effort_write(
                conn,
                "DELETE FROM sessions WHERE token_hash=?",
                (row["token_hash"],),
                what="expired session cleanup",
            )
        return None
    if int(row["expires_at"]) - now < SESSION_REFRESH_AFTER_SECONDS:
        best_effort_write(
            conn,
            "UPDATE sessions SET expires_at=? WHERE token_hash=?",
            (str(now + SESSION_MAX_AGE_SECONDS), row["token_hash"]),
            what="session expiry refresh",
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
