from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from librairy.backup import (
    backup_due,
    backup_run_pending,
    last_backup_run,
    record_backup_run,
    request_backup_now,
)
from librairy.config import Settings
from librairy.db import connect


def setup(tmp_path: Path, **overrides) -> tuple[object, Settings]:
    options = {"BACKUP_ENABLED": "true", **overrides}
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        _env_file=None,
        **options,
    )
    return connect(settings), settings


def test_the_schedule_is_actually_read(tmp_path: Path) -> None:
    """It was stored and never consulted, so "daily" ran on every cycle."""
    conn, settings = setup(tmp_path, BACKUP_SCHEDULE="manual")

    assert backup_due(conn, settings) is False


def test_after_commit_runs_every_pass(tmp_path: Path) -> None:
    conn, settings = setup(tmp_path, BACKUP_SCHEDULE="after_commit")

    assert backup_due(conn, settings) is True
    record_backup_run(conn)
    assert backup_due(conn, settings) is True


def test_hourly_waits_an_hour(tmp_path: Path) -> None:
    conn, settings = setup(tmp_path, BACKUP_SCHEDULE="hourly")
    noon = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)

    assert backup_due(conn, settings, now=noon) is True, "never run means run now"
    record_backup_run(conn, at=noon)

    assert backup_due(conn, settings, now=noon) is False
    assert backup_due(conn, settings, now=noon.replace(hour=13, minute=1)) is True


def test_daily_waits_for_the_hour_and_then_only_once(tmp_path: Path) -> None:
    conn, settings = setup(tmp_path, BACKUP_SCHEDULE="daily", BACKUP_DAILY_AT="02:00")
    before = datetime(2026, 3, 1, 1, 30, tzinfo=UTC)
    after = datetime(2026, 3, 1, 2, 30, tzinfo=UTC)

    assert backup_due(conn, settings, now=before) is False
    assert backup_due(conn, settings, now=after) is True

    record_backup_run(conn, at=after)
    assert backup_due(conn, settings, now=after.replace(hour=23)) is False
    assert backup_due(conn, settings, now=after.replace(day=2)) is True


def test_back_up_now_beats_every_schedule(tmp_path: Path) -> None:
    """Including manual — that is the whole point of the button."""
    conn, settings = setup(tmp_path, BACKUP_SCHEDULE="manual")
    request_backup_now(conn)

    assert backup_run_pending(conn) is True
    assert backup_due(conn, settings) is True

    record_backup_run(conn)
    assert backup_run_pending(conn) is False
    assert backup_due(conn, settings) is False
    assert last_backup_run(conn)


def test_a_disabled_backup_is_never_due(tmp_path: Path) -> None:
    conn, settings = setup(tmp_path, BACKUP_ENABLED="false")
    request_backup_now(conn)

    assert backup_due(conn, settings) is False


def test_an_unknown_schedule_backs_up_rather_than_going_quiet(tmp_path: Path) -> None:
    """A typo in a config file must not silently stop backing anything up."""
    conn, settings = setup(tmp_path, BACKUP_SCHEDULE="weekly-ish")

    assert backup_due(conn, settings) is True


def test_a_nonsense_time_falls_back_instead_of_raising(tmp_path: Path) -> None:
    conn, settings = setup(tmp_path, BACKUP_SCHEDULE="daily", BACKUP_DAILY_AT="not-a-time")

    assert backup_due(conn, settings, now=datetime(2026, 3, 1, 3, 0, tzinfo=UTC)) is True
