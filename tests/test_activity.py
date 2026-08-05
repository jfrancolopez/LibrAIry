from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from librairy.config import Settings
from librairy.db import connect
from librairy.web.activity import activity
from librairy.web.app import create_app


def settings_for(tmp_path: Path) -> Settings:
    settings = Settings(
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        APPDATA_DIR=tmp_path / "appdata",
        _env_file=None,
    )
    for path in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        path.mkdir(parents=True, exist_ok=True)
    return settings


def _state(conn, key: str, value) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO worker_state(key, value) VALUES (?, ?)",
        (key, json.dumps(value)),
    )


def _discovered(conn, count: int) -> None:
    for index in range(count):
        conn.execute(
            """
            INSERT INTO items(
              root, relpath, size, mtime_ns, fingerprint, state, first_seen_at, last_seen_at
            )
            VALUES ('inbox', ?, 1, 1, ?, 'discovered', 'now', 'now')
            """,
            (f"new-{index}.mkv", f"fp{index}"),
        )


def test_idle_and_empty_says_nothing(tmp_path: Path) -> None:
    """A pill that is always shouting is a pill nobody reads."""
    conn = connect(settings_for(tmp_path))
    _state(conn, "current_phase", "idle")
    _state(conn, "last_cycle_at", datetime.now(UTC).isoformat())

    result = activity(conn)

    assert result.busy is False
    assert result.visible is False


def test_working_reports_the_phase_and_the_backlog(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path))
    _state(conn, "current_phase", "analyze")
    _state(conn, "last_cycle_at", datetime.now(UTC).isoformat())
    _discovered(conn, 12)

    result = activity(conn)

    assert result.busy is True
    assert result.label == "identifying files"
    assert result.queued == 12
    assert result.visible is True


def test_a_backlog_with_no_heartbeat_is_reported_as_stalled(tmp_path: Path) -> None:
    """Files waiting and nothing running is the failure worth interrupting for.

    Without this the pill would sit on "3 new files found" forever and look
    like progress.
    """
    conn = connect(settings_for(tmp_path))
    _state(conn, "current_phase", "idle")
    _state(conn, "last_cycle_at", (datetime.now(UTC) - timedelta(hours=1)).isoformat())
    _discovered(conn, 3)

    result = activity(conn)

    assert result.stalled is True
    assert result.visible is True


def test_a_never_started_worker_is_not_called_stalled(tmp_path: Path) -> None:
    """No heartbeat at all means a fresh install, not a crash."""
    conn = connect(settings_for(tmp_path))

    assert activity(conn).stalled is False


def test_pill_renders_on_every_page_and_refreshes_itself(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    _state(conn, "current_phase", "analyze")
    _state(conn, "last_cycle_at", datetime.now(UTC).isoformat())
    _discovered(conn, 7)
    client = TestClient(create_app(settings, conn))
    client.post("/setup", data={"password": "correct horse battery"})

    for page in ("/dashboard", "/review", "/search", "/health"):
        body = client.get(page).text
        assert 'id="activity-pill"' in body, page
        assert "identifying files" in body, page
        assert "7 to go" in body, page

    fragment = client.get("/activity")

    assert fragment.status_code == 200
    assert 'hx-trigger="every 3s"' in fragment.text


def test_pill_is_empty_when_there_is_nothing_to_report(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    _state(conn, "current_phase", "idle")
    _state(conn, "last_cycle_at", datetime.now(UTC).isoformat())
    client = TestClient(create_app(settings, conn))
    client.post("/setup", data={"password": "correct horse battery"})

    body = client.get("/dashboard").text

    # Still polling, but taking up no room.
    assert 'id="activity-pill"' in body
    assert "spinner" not in body
