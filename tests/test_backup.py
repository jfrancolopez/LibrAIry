from __future__ import annotations

from pathlib import Path

from librairy.backup import backup_status, enqueue_backup_item, snapshot_database
from librairy.config import Settings
from librairy.db import SCHEMA_VERSION, connect, user_version
from librairy.tools.rclone import (
    RcloneError,
    check_command,
    copy_command,
    listremotes_command,
    version_command,
)


def settings_for(tmp_path: Path, **overrides) -> Settings:
    values = {
        "APPDATA_DIR": tmp_path / "appdata",
        "INBOX_DIR": tmp_path / "inbox",
        "LIBRARY_DIR": tmp_path / "library",
        "QUARANTINE_DIR": tmp_path / "quarantine",
        "_env_file": None,
    }
    values.update(overrides)
    return Settings(**values)


def test_schema_adds_backup_queue(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path))

    assert user_version(conn) == SCHEMA_VERSION
    assert SCHEMA_VERSION >= 10
    conn.execute("SELECT * FROM backup_queue")


def test_enqueue_backup_item_only_when_enabled(tmp_path: Path) -> None:
    disabled = settings_for(tmp_path)
    conn = connect(disabled)
    conn.execute(
        """
        INSERT INTO items(
          id, root, relpath, size, mtime_ns, fingerprint, first_seen_at, last_seen_at
        )
        VALUES (1, 'library', 'Documents/a.txt', 1, 1, 'fp', 'now', 'now')
        """
    )

    assert enqueue_backup_item(
        conn,
        disabled,
        item_id=1,
        relpath="Documents/a.txt",
        fingerprint="fp",
    ) is False
    assert conn.execute("SELECT COUNT(*) FROM backup_queue").fetchone()[0] == 0

    enabled = settings_for(tmp_path, BACKUP_ENABLED=True)
    assert enqueue_backup_item(
        conn,
        enabled,
        item_id=1,
        relpath="Documents/a.txt",
        fingerprint="fp",
    ) is True
    assert enqueue_backup_item(
        conn,
        enabled,
        item_id=1,
        relpath="Documents/a.txt",
        fingerprint="fp",
    ) is False
    assert conn.execute("SELECT COUNT(*) FROM backup_queue").fetchone()[0] == 1


def test_rclone_builder_allows_only_non_destructive_verbs(tmp_path: Path) -> None:
    config = tmp_path / "rclone.conf"
    source = tmp_path / "library"

    commands = [
        version_command(),
        listremotes_command(config),
        copy_command(config, source, "remote:library", "1M"),
        check_command(config, source, "remote:library"),
    ]

    assert {command[1] for command in commands} == {"version", "listremotes", "copy", "check"}
    forbidden = " ".join(" ".join(command) for command in commands)
    assert " sync " not in forbidden
    assert " delete " not in forbidden
    assert " purge " not in forbidden
    assert " move " not in forbidden

    import pytest

    with pytest.raises(RcloneError):
        copy_command(config, source, "sync")[:-1]


def test_backup_status_reports_missing_binary_or_config_without_crashing(tmp_path: Path) -> None:
    settings = settings_for(tmp_path, BACKUP_ENABLED=True, BACKUP_REMOTE="remote:library")

    status = backup_status(settings)

    assert status.available is False
    assert status.detail


def test_snapshot_database_uses_sqlite_backup_api(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    conn.execute("INSERT INTO settings(key, value) VALUES ('sample', '\"ok\"')")

    snapshot = snapshot_database(settings, tmp_path / "snapshot.db")
    copied = connect(settings, path=snapshot)

    assert copied.execute("SELECT value FROM settings WHERE key='sample'").fetchone()[0] == '"ok"'
