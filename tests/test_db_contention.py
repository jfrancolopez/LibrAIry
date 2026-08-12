"""A page load must not fail because the worker is writing.

The bug this pins down was seen in production: a GET returned 500 with
`sqlite3.OperationalError: database is locked`, raised from `create_session`
inside the auth middleware. SQLite allows one writer, the worker held it for
longer than the five-second busy timeout, and an ordinary page render was
competing as a *writer* — for a session row and a sliding-expiry refresh.

Every test here holds a real write lock on a second connection for the whole
request, so there is nothing intermittent about it: before the fix these all
fail, deterministically.

The point is not that the timeout is too short. The point is that reading
Browse should never have needed the writer lock.
"""

from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from librairy.config import Settings
from librairy.db import best_effort_write, connect, database_path, is_locked
from librairy.models import EvidenceEntry
from librairy.proposals import upsert_proposal
from librairy.scanner import scan_root
from librairy.web.app import create_app
from librairy.web.auth import (
    SESSION_MAX_AGE_SECONDS,
    create_session,
    session_from_request,
)

PAGES = ("/dashboard", "/review", "/browse", "/history", "/quarantine", "/settings", "/commit")
WRITE = re.compile(r"^\s*(INSERT|UPDATE|DELETE|REPLACE)\b", re.I)


def settings_for(tmp_path: Path, **overrides) -> Settings:
    values = {
        "APPDATA_DIR": tmp_path / "appdata",
        "INBOX_DIR": tmp_path / "inbox",
        "LIBRARY_DIR": tmp_path / "library",
        "QUARANTINE_DIR": tmp_path / "quarantine",
        "FILE_STABILITY_SECONDS": 0,
        "AUTH_REQUIRED": False,
        "_env_file": None,
    }
    values.update(overrides)
    settings = Settings(**values)
    for root in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        root.mkdir(parents=True, exist_ok=True)
    return settings


def scene(tmp_path: Path, **overrides):
    settings = settings_for(tmp_path, **overrides)
    conn = connect(settings)
    (settings.inbox_dir / "a.mkv").write_text("a", encoding="utf-8")
    library_file = settings.library_dir / "Movies/b.mkv"
    library_file.parent.mkdir(parents=True, exist_ok=True)
    library_file.write_text("b", encoding="utf-8")
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    scan_root(conn, "library", settings.library_dir, settings)
    item = conn.execute("SELECT id FROM items WHERE root='inbox'").fetchone()
    upsert_proposal(
        conn,
        item_id=item["id"],
        category="movies",
        clean_name="a.mkv",
        dest_relpath="Movies/a.mkv",
        confidence=0.9,
        evidence=[EvidenceEntry("heuristic", "title", "A", 0.9)],
    )
    conn.commit()
    return TestClient(create_app(settings, conn)), conn, settings, item["id"]


class HeldWriteLock:
    """A second connection inside an open write transaction, exactly as the
    worker looks to everyone else mid-scan."""

    def __init__(self, settings: Settings) -> None:
        self.path = database_path(settings)

    def __enter__(self) -> sqlite3.Connection:
        self.conn = sqlite3.connect(self.path, timeout=0.1, isolation_level=None)
        self.conn.execute("PRAGMA busy_timeout=100")
        self.conn.execute("BEGIN IMMEDIATE")
        self.conn.execute(
            "INSERT OR REPLACE INTO worker_state(key, value) VALUES ('holding', '1')"
        )
        return self.conn

    def __exit__(self, *exc) -> None:
        self.conn.execute("ROLLBACK")
        self.conn.close()


def writes_during(conn: sqlite3.Connection, call) -> list[str]:
    seen: list[str] = []

    def trace(statement: str) -> None:
        if WRITE.match(statement):
            seen.append(" ".join(statement.split())[:80])

    conn.set_trace_callback(trace)
    try:
        call()
    finally:
        conn.set_trace_callback(None)
    return seen


# --- the reproduction ---------------------------------------------------------


@pytest.mark.parametrize("page", PAGES)
def test_a_page_loads_while_the_worker_holds_the_write_lock(tmp_path: Path, page: str) -> None:
    """The production failure, one page at a time."""
    client, _, settings, _ = scene(tmp_path)

    with HeldWriteLock(settings):
        response = client.get(page, follow_redirects=True)

    assert response.status_code == 200, response.text[:400]


def test_a_first_visit_with_no_cookie_survives_the_lock(tmp_path: Path) -> None:
    """The exact traceback: no cookie, so the middleware tries to mint a
    session, and the insert cannot get the lock."""
    client, _, settings, _ = scene(tmp_path)
    client.cookies.clear()

    with HeldWriteLock(settings):
        response = client.get("/browse")

    assert response.status_code == 200
    assert "Your library" in response.text


def test_an_item_page_loads_while_the_worker_holds_the_lock(tmp_path: Path) -> None:
    client, _, settings, item_id = scene(tmp_path)

    with HeldWriteLock(settings):
        response = client.get(f"/items/{item_id}")

    assert response.status_code == 200


def test_the_page_still_renders_its_content_not_an_error_page(tmp_path: Path) -> None:
    client, _, settings, _ = scene(tmp_path)

    with HeldWriteLock(settings):
        html = client.get("/review", follow_redirects=True).text

    assert "Review" in html
    assert "database is locked" not in html
    assert "Internal Server Error" not in html


def test_repeated_loads_under_a_held_lock_all_succeed(tmp_path: Path) -> None:
    """Intermittent failures are the ones that erode trust, so this asks for
    the same thing many times rather than once."""
    client, _, settings, _ = scene(tmp_path)

    with HeldWriteLock(settings):
        codes = {client.get(page, follow_redirects=True).status_code for page in PAGES * 4}

    assert codes == {200}


# --- writes removed from the render path --------------------------------------


def test_a_returning_visitor_writes_nothing_at_all(tmp_path: Path) -> None:
    """The dominant write: the sliding session refresh fired on every request
    to every page, extending a seven-day window by milliseconds."""
    client, conn, _, _ = scene(tmp_path)
    client.get("/dashboard")  # establishes the cookie

    for page in PAGES:
        assert writes_during(conn, lambda page=page: client.get(page, follow_redirects=True)) == []


def test_an_expiring_session_is_still_refreshed(tmp_path: Path) -> None:
    """Skipping the write must not turn a sliding session into a fixed one."""
    client, conn, _, _ = scene(tmp_path)
    client.get("/dashboard")
    stale = int(time.time()) + 60  # nearly expired
    conn.execute("UPDATE sessions SET expires_at=?", (str(stale),))

    client.get("/review", follow_redirects=True)

    row = conn.execute("SELECT expires_at FROM sessions").fetchone()
    assert int(row["expires_at"]) > stale + SESSION_MAX_AGE_SECONDS // 2


def test_rendering_the_header_does_not_write(tmp_path: Path) -> None:
    from librairy.settings_service import provider_header

    _, conn, settings, _ = scene(tmp_path)

    assert writes_during(conn, lambda: provider_header(conn, settings)) == []


def test_a_first_visit_writes_only_the_session_row(tmp_path: Path) -> None:
    """One write is left on a cold render, and it is the one that carries the
    CSRF token. Everything else was removed."""
    client, conn, _, _ = scene(tmp_path)
    client.get("/dashboard")
    client.cookies.clear()

    statements = writes_during(conn, lambda: client.get("/browse"))

    assert len(statements) == 1
    assert statements[0].startswith("INSERT INTO sessions")


# --- narrow handling, not blanket swallowing ----------------------------------


def test_only_lock_errors_are_absorbed(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)

    with pytest.raises(sqlite3.OperationalError):
        best_effort_write(conn, "UPDATE no_such_table SET x=1", (), what="a real mistake")


def test_a_constraint_violation_still_raises(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    create_session(conn)
    row = conn.execute("SELECT token_hash, created_at, expires_at, csrf_token FROM sessions")
    existing = row.fetchone()

    with pytest.raises(sqlite3.IntegrityError):
        best_effort_write(
            conn,
            "INSERT INTO sessions(token_hash, created_at, expires_at, csrf_token)"
            " VALUES (?, ?, ?, ?)",
            tuple(existing),
            what="a duplicate",
        )


def test_a_lock_error_returns_false_rather_than_raising(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    conn.execute("PRAGMA busy_timeout=100")

    with HeldWriteLock(settings):
        wrote = best_effort_write(
            conn,
            "INSERT OR REPLACE INTO settings(key, value) VALUES ('x', 'y')",
            (),
            what="something optional",
        )

    assert wrote is False
    assert conn.execute("SELECT COUNT(*) c FROM settings WHERE key='x'").fetchone()["c"] == 0


def test_is_locked_recognises_the_real_message_and_nothing_else() -> None:
    assert is_locked(sqlite3.OperationalError("database is locked"))
    assert is_locked(sqlite3.OperationalError("database table is locked: sessions"))
    assert not is_locked(sqlite3.OperationalError("no such table: sessions"))
    assert not is_locked(sqlite3.OperationalError("syntax error"))
    assert not is_locked(sqlite3.IntegrityError("UNIQUE constraint failed"))


# --- authentication still behaves ---------------------------------------------


def test_login_still_persists_a_session(tmp_path: Path) -> None:
    settings = settings_for(tmp_path, AUTH_REQUIRED=True)
    conn = connect(settings)
    client = TestClient(create_app(settings, conn))
    client.post("/setup", data={"password": "correct horse battery"}, follow_redirects=False)

    before = conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"]
    response = client.post(
        "/login", data={"password": "correct horse battery"}, follow_redirects=False
    )

    assert response.status_code in (302, 303)
    after = conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"]
    assert after == before + 1, "a real login writes a real session"


def test_an_authenticated_page_still_requires_a_session(tmp_path: Path) -> None:
    settings = settings_for(tmp_path, AUTH_REQUIRED=True)
    conn = connect(settings)
    client = TestClient(create_app(settings, conn))
    client.post("/setup", data={"password": "correct horse battery"}, follow_redirects=False)
    client.cookies.clear()

    response = client.get("/review", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == "/login"


def test_a_locked_database_does_not_hand_out_access(tmp_path: Path) -> None:
    """The degrade path is for the open portal only. With auth required, a
    session-less request is still sent to the login page."""
    settings = settings_for(tmp_path, AUTH_REQUIRED=True)
    conn = connect(settings)
    client = TestClient(create_app(settings, conn))
    client.post("/setup", data={"password": "correct horse battery"}, follow_redirects=False)
    client.cookies.clear()

    with HeldWriteLock(settings):
        response = client.get("/review", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == "/login"


def test_a_transient_session_sets_no_cookie(tmp_path: Path) -> None:
    """So the next request tries again rather than carrying a token that was
    never written down."""
    client, _, settings, _ = scene(tmp_path)
    client.cookies.clear()

    with HeldWriteLock(settings):
        response = client.get("/browse")

    assert response.status_code == 200
    assert "librairy_session" not in response.cookies


def test_an_expired_session_is_rejected_even_if_cleanup_cannot_run(tmp_path: Path) -> None:
    client, conn, settings, _ = scene(tmp_path)
    client.get("/dashboard")
    conn.execute("UPDATE sessions SET expires_at=?", (str(int(time.time()) - 10),))

    class FakeRequest:
        cookies = {"librairy_session": "whatever"}

    with HeldWriteLock(settings):
        assert session_from_request(conn, FakeRequest()) is None


# --- the worker keeps working -------------------------------------------------


def test_worker_writes_still_persist(tmp_path: Path) -> None:
    """None of this may quietly turn a real write into a no-op."""
    client, conn, settings, _ = scene(tmp_path)
    (settings.inbox_dir / "later.mkv").write_text("later", encoding="utf-8")

    scan_root(conn, "inbox", settings.inbox_dir, settings)
    conn.commit()

    assert conn.execute(
        "SELECT COUNT(*) c FROM items WHERE relpath='later.mkv'"
    ).fetchone()["c"] == 1
    assert client.get("/browse", follow_redirects=True).status_code == 200


def test_the_worker_holds_no_long_write_transaction(tmp_path: Path) -> None:
    """Why the fix is on the reader's side and not the worker's.

    The connection is opened with `isolation_level=None`, so every write is
    its own transaction and commits immediately. There is no long transaction
    to shorten: the contention is a burst of many short writes against a
    reader that also wanted the writer lock, which is writer-vs-writer
    starvation and is why WAL does not help either.
    """
    settings = settings_for(tmp_path)
    conn = connect(settings)
    for index in range(40):
        (settings.inbox_dir / f"file{index}.mkv").write_text(str(index), encoding="utf-8")

    open_after: list[bool] = []

    def trace(statement: str) -> None:
        if WRITE.match(statement):
            # The trace callback fires before the statement runs, so this
            # records whether a transaction was already open from an earlier
            # write -- which is exactly the "one long transaction" shape.
            open_after.append(conn.in_transaction)

    conn.set_trace_callback(trace)
    try:
        scan_root(conn, "inbox", settings.inbox_dir, settings)
    finally:
        conn.set_trace_callback(None)

    assert open_after, "the scan did write something"
    assert not any(open_after), "no write found a transaction already open"


def test_the_database_is_in_wal_where_the_filesystem_allows_it(tmp_path: Path) -> None:
    """Recorded rather than changed. WAL lets readers run during a write, and
    is already on — it does not help two writers, which is what this was."""
    settings = settings_for(tmp_path)
    conn = connect(settings)

    mode = conn.execute("PRAGMA journal_mode").fetchone()[0].upper()
    timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]

    assert mode in {"WAL", "DELETE"}
    assert timeout == 5000, "unchanged: the fix is fewer writes, not more waiting"
