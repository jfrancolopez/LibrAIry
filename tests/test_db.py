from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from librairy.config import Settings
from librairy.db import SCHEMA_VERSION, DatabaseVersionError, connect, user_version


def settings_for(tmp_path: Path) -> Settings:
    return Settings(APPDATA_DIR=tmp_path / "appdata", _env_file=None)


def test_fresh_db_migrates_to_current_schema(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path))

    assert user_version(conn) == SCHEMA_VERSION
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    assert tables == {
        "items",
        "plans",
        "plan_ops",
        "history",
        "settings",
        "sessions",
        "groups",
        "proposals",
        "provider_status",
        "worker_state",
        "similar_media_flags",
    }

    indexes = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
        )
    }
    assert indexes == {
        "idx_items_fingerprint",
        "idx_items_state",
        "idx_plan_ops_plan_id",
        "idx_history_plan_id",
        "idx_proposals_status",
        "idx_proposals_category",
        "idx_proposals_group_id",
        "idx_groups_kind",
        "idx_provider_status_kind",
        "idx_provider_status_enabled",
        "idx_similar_media_flags_status",
        "idx_similar_media_flags_item_id",
    }

    columns = {row[1] for row in conn.execute("PRAGMA table_info(provider_status)")}
    assert "available_models" in columns


def test_reopening_db_is_noop(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    connect(settings).close()
    conn = connect(settings)

    assert user_version(conn) == SCHEMA_VERSION


def test_wal_and_foreign_keys_are_active(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path))

    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_newer_database_version_is_rejected(tmp_path: Path) -> None:
    db_path = tmp_path / "appdata" / "librairy.db"
    db_path.parent.mkdir()
    conn = sqlite3.connect(db_path)
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
    conn.close()

    with pytest.raises(DatabaseVersionError, match="newer than this code supports"):
        connect(settings_for(tmp_path))


def test_two_connections_can_write_without_database_locked(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    connect(settings).close()
    errors: list[BaseException] = []

    def writer(start: int) -> None:
        try:
            conn = connect(settings)
            for index in range(start, start + 20):
                conn.execute(
                    "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
                    (f"key-{index}", f'{{"value": {index}}}'),
                )
            conn.close()
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [
        threading.Thread(target=writer, args=(0,)),
        threading.Thread(target=writer, args=(20,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    conn = connect(settings)
    assert conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 40
