from __future__ import annotations

from pathlib import Path
from subprocess import CompletedProcess

from librairy.backup import (
    backup_status,
    category_sizes,
    enqueue_backup_item,
    run_backup_once,
    selected_categories,
    snapshot_database,
)
from librairy.config import Settings
from librairy.db import SCHEMA_VERSION, connect, user_version
from librairy.locks import acquire_lock
from librairy.proposals import EvidenceEntry, upsert_proposal
from librairy.search import sync_search_item
from librairy.taxonomy import CATEGORIES
from librairy.tools.rclone import (
    RcloneError,
    check_command,
    copy_command,
    listremotes_command,
    version_command,
)


class AvailableStatus:
    available = True
    detail = "ok"


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


def test_backup_runner_copies_verifies_and_marks_done(tmp_path: Path, monkeypatch) -> None:
    settings = settings_for(tmp_path, BACKUP_ENABLED=True, BACKUP_REMOTE="remote:library")
    settings.library_dir.mkdir(parents=True)
    config = settings.appdata_dir / "rclone" / "rclone.conf"
    config.parent.mkdir(parents=True)
    config.write_text("[remote]\n", encoding="utf-8")
    source = settings.library_dir / "Documents/a.txt"
    source.parent.mkdir(parents=True)
    source.write_text("a", encoding="utf-8")
    conn = connect(settings)
    conn.execute(
        """
        INSERT INTO items(
          id, root, relpath, size, mtime_ns, fingerprint, first_seen_at, last_seen_at
        )
        VALUES (1, 'library', 'Documents/a.txt', 1, 1, 'fp', 'now', 'now')
        """
    )
    enqueue_backup_item(conn, settings, item_id=1, relpath="Documents/a.txt", fingerprint="fp")
    commands: list[list[str]] = []

    monkeypatch.setattr("librairy.backup.rclone_status", lambda path: AvailableStatus())

    def fake_run(command: list[str]):
        commands.append(command)
        return CompletedProcess(command, 0, stdout="ok", stderr="")

    monkeypatch.setattr("librairy.backup.run", fake_run)

    summary = run_backup_once(conn, settings)

    row = conn.execute("SELECT state, attempts, last_error FROM backup_queue").fetchone()
    assert summary.copied == 1
    assert row["state"] == "done"
    assert row["attempts"] == 0
    assert row["last_error"] is None
    assert [command[1] for command in commands] == ["copy", "check"]


def test_backup_runner_retries_then_stops_after_failures(tmp_path: Path, monkeypatch) -> None:
    settings = settings_for(tmp_path, BACKUP_ENABLED=True, BACKUP_REMOTE="remote:library")
    settings.library_dir.mkdir(parents=True)
    source = settings.library_dir / "Documents/a.txt"
    source.parent.mkdir(parents=True)
    source.write_text("a", encoding="utf-8")
    conn = connect(settings)
    conn.execute(
        """
        INSERT INTO items(
          id, root, relpath, size, mtime_ns, fingerprint, first_seen_at, last_seen_at
        )
        VALUES (1, 'library', 'Documents/a.txt', 1, 1, 'fp', 'now', 'now')
        """
    )
    enqueue_backup_item(conn, settings, item_id=1, relpath="Documents/a.txt", fingerprint="fp")
    monkeypatch.setattr("librairy.backup.rclone_status", lambda path: AvailableStatus())
    monkeypatch.setattr(
        "librairy.backup.run",
        lambda command: CompletedProcess(command, 1, stdout="", stderr="network down"),
    )

    for _ in range(5):
        run_backup_once(conn, settings)

    row = conn.execute("SELECT state, attempts, last_error FROM backup_queue").fetchone()
    assert row["state"] == "failed"
    assert row["attempts"] == 3
    assert "network down" in row["last_error"]
    assert source.read_text(encoding="utf-8") == "a"
    assert conn.execute("SELECT state FROM items WHERE id=1").fetchone()[0] == "discovered"


def test_backup_runner_does_not_hold_executor_lock(tmp_path: Path, monkeypatch) -> None:
    settings = settings_for(tmp_path, BACKUP_ENABLED=True, BACKUP_REMOTE="remote:library")
    settings.library_dir.mkdir(parents=True)
    source = settings.library_dir / "Documents/a.txt"
    source.parent.mkdir(parents=True)
    source.write_text("a", encoding="utf-8")
    conn = connect(settings)
    conn.execute(
        """
        INSERT INTO items(
          id, root, relpath, size, mtime_ns, fingerprint, first_seen_at, last_seen_at
        )
        VALUES (1, 'library', 'Documents/a.txt', 1, 1, 'fp', 'now', 'now')
        """
    )
    enqueue_backup_item(conn, settings, item_id=1, relpath="Documents/a.txt", fingerprint="fp")
    monkeypatch.setattr("librairy.backup.rclone_status", lambda path: AvailableStatus())
    monkeypatch.setattr(
        "librairy.backup.run",
        lambda command: CompletedProcess(command, 0, stdout="ok", stderr=""),
    )

    with acquire_lock(settings):
        summary = run_backup_once(conn, settings)

    assert summary.copied == 1


def test_backup_runner_pauses_when_unavailable(tmp_path: Path) -> None:
    settings = settings_for(tmp_path, BACKUP_ENABLED=True, BACKUP_REMOTE="remote:library")
    conn = connect(settings)

    summary = run_backup_once(conn, settings)

    assert summary.paused is True
    assert summary.warning


def test_choosing_categories_leaves_the_rest_out_of_the_queue(tmp_path: Path) -> None:
    """Off-site storage is metered, and a photo library and a film collection
    are not the same proposition — one is irreplaceable and small, the other is
    large and can be obtained again."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    chosen = settings.model_copy(update={"backup_enabled": True, "backup_categories": "photos"})
    for item_id, category in ((1, "photos"), (2, "movies")):
        conn.execute(
            """
            INSERT INTO items(id, root, relpath, size, mtime_ns, fingerprint,
                              first_seen_at, last_seen_at)
            VALUES (?, 'library', ?, 10, 1, ?, 'now', 'now')
            """,
            (item_id, f"{category}/a-{item_id}.bin", f"fp{item_id}"),
        )
        upsert_proposal(
            conn,
            item_id=item_id,
            category=category,
            clean_name=f"a-{item_id}.bin",
            dest_relpath=f"{category}/a-{item_id}.bin",
            confidence=0.9,
            evidence=[EvidenceEntry("heuristic", "category", "ext", 0.9)],
        )

    queued = [
        enqueue_backup_item(
            conn, chosen, item_id=item_id, relpath=f"x-{item_id}", fingerprint=f"fp{item_id}"
        )
        for item_id in (1, 2)
    ]

    assert queued == [True, False]
    rows = [row["item_id"] for row in conn.execute("SELECT item_id FROM backup_queue")]
    assert rows == [1]


def test_selecting_nothing_means_everything_not_nothing(tmp_path: Path) -> None:
    """The default must never quietly leave files out of a backup you believe
    is complete — including files with no category at all."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    enabled = settings.model_copy(update={"backup_enabled": True, "backup_categories": ""})
    conn.execute(
        """
        INSERT INTO items(id, root, relpath, size, mtime_ns, fingerprint,
                          first_seen_at, last_seen_at)
        VALUES (1, 'library', 'odd/thing.bin', 10, 1, 'fp1', 'now', 'now')
        """
    )

    assert selected_categories(enabled) == set(CATEGORIES)
    assert enqueue_backup_item(conn, enabled, item_id=1, relpath="x", fingerprint="fp1") is True


def test_category_sizes_report_what_each_choice_costs(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    conn.execute(
        """
        INSERT INTO items(id, root, relpath, size, mtime_ns, fingerprint,
                          first_seen_at, last_seen_at)
        VALUES (1, 'library', 'Photos/a.jpg', 1572864, 1, 'fp1', 'now', 'now')
        """
    )
    upsert_proposal(
        conn,
        item_id=1,
        category="photos",
        clean_name="a.jpg",
        dest_relpath="Photos/a.jpg",
        confidence=0.9,
        evidence=[EvidenceEntry("heuristic", "category", "ext", 0.9)],
    )
    sync_search_item(conn, 1)

    sizes = {entry.category: entry for entry in category_sizes(conn, settings)}

    assert sizes["photos"].files == 1
    assert sizes["photos"].size_label == "1.5 MB"
    # Every category is listed, including the empty ones you might file into later.
    assert set(sizes) == set(CATEGORIES)
    assert sizes["movies"].files == 0
