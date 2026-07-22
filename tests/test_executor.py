from __future__ import annotations

from pathlib import Path

import pytest

from librairy.config import Settings
from librairy.db import connect
from librairy.executor import ExecutionError, execute_plan
from librairy.planner import OperationSpec, approve_plan, create_plan
from librairy.quarantine import quarantine_operation, restore_entry
from librairy.scanner import scan_root


def settings_for(tmp_path: Path) -> Settings:
    return Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        FILE_STABILITY_SECONDS=0,
        _env_file=None,
    )


def setup_plan(tmp_path: Path, specs: list[OperationSpec]):
    settings = settings_for(tmp_path)
    settings.inbox_dir.mkdir()
    settings.library_dir.mkdir()
    settings.quarantine_dir.mkdir()
    (settings.inbox_dir / "a.txt").write_text("a", encoding="utf-8")
    (settings.inbox_dir / "b.txt").write_text("b", encoding="utf-8")
    conn = connect(settings)
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    plan_id = create_plan(conn, specs, settings)
    approve_plan(conn, plan_id, settings)
    return settings, conn, plan_id


def test_execute_multi_op_plan_and_journal(tmp_path: Path) -> None:
    settings, conn, plan_id = setup_plan(
        tmp_path,
        [
            OperationSpec("move", "a.txt", "library", "Documents/a.txt"),
            OperationSpec("quarantine", "b.txt", "quarantine", "dupes/b.txt"),
        ],
    )

    summary = execute_plan(conn, plan_id, settings)

    assert summary.done == 2
    assert (settings.library_dir / "Documents/a.txt").read_text(encoding="utf-8") == "a"
    assert (settings.quarantine_dir / "dupes/b.txt").read_text(encoding="utf-8") == "b"
    assert not (settings.inbox_dir / "a.txt").exists()
    history_count = conn.execute(
        "SELECT COUNT(*) FROM history WHERE plan_id=?",
        (plan_id,),
    ).fetchone()[0]
    assert history_count == 2


def test_quarantine_op_records_entry_and_uses_quarantined_state(tmp_path: Path) -> None:
    settings, conn, plan_id = setup_plan(
        tmp_path,
        [quarantine_operation("b.txt", date="2026-07-21")],
    )

    summary = execute_plan(conn, plan_id, settings)

    assert summary.done == 1
    assert (settings.quarantine_dir / "2026-07-21/b.txt").read_text(encoding="utf-8") == "b"
    entry = conn.execute("SELECT * FROM quarantine_entries").fetchone()
    assert entry["original_root"] == "inbox"
    assert entry["original_relpath"] == "b.txt"
    assert entry["plan_id"] == plan_id
    item = conn.execute(
        "SELECT root, relpath, state FROM items WHERE id=?", (entry["item_id"],)
    ).fetchone()
    assert dict(item) == {
        "root": "quarantine",
        "relpath": "2026-07-21/b.txt",
        "state": "quarantined",
    }


def test_restore_quarantine_entry_is_journaled_and_collision_safe(tmp_path: Path) -> None:
    settings, conn, plan_id = setup_plan(
        tmp_path,
        [quarantine_operation("b.txt", date="2026-07-21")],
    )
    execute_plan(conn, plan_id, settings)
    entry_id = conn.execute("SELECT id FROM quarantine_entries").fetchone()[0]
    (settings.inbox_dir / "b.txt").write_text("collision", encoding="utf-8")

    result = restore_entry(conn, entry_id, settings)

    assert result.outcome == "ok"
    assert result.dest_relpath == "b (2).txt"
    assert (settings.inbox_dir / "b (2).txt").read_text(encoding="utf-8") == "b"
    assert conn.execute(
        "SELECT restored_at FROM quarantine_entries WHERE id=?", (entry_id,)
    ).fetchone()[0]
    assert (
        conn.execute("SELECT action FROM history ORDER BY id DESC LIMIT 1").fetchone()[0]
        == "restore_quarantine"
    )


def test_quarantine_module_does_not_delete_files() -> None:
    source = Path("src/librairy/quarantine.py").read_text(encoding="utf-8")

    assert ".unlink" not in source
    assert "os.remove" not in source


def test_changed_source_is_skipped(tmp_path: Path) -> None:
    settings, conn, plan_id = setup_plan(
        tmp_path,
        [OperationSpec("move", "a.txt", "library", "Documents/a.txt")],
    )
    (settings.inbox_dir / "a.txt").write_text("changed", encoding="utf-8")

    summary = execute_plan(conn, plan_id, settings)

    assert summary.skipped_changed == 1
    assert (settings.inbox_dir / "a.txt").read_text(encoding="utf-8") == "changed"
    assert not (settings.library_dir / "Documents/a.txt").exists()


def test_destination_collision_is_renamed(tmp_path: Path) -> None:
    settings, conn, plan_id = setup_plan(
        tmp_path,
        [OperationSpec("move", "a.txt", "library", "Documents/a.txt")],
    )
    (settings.library_dir / "Documents").mkdir()
    (settings.library_dir / "Documents/a.txt").write_text("existing", encoding="utf-8")

    summary = execute_plan(conn, plan_id, settings)

    assert summary.renamed_collision == 1
    assert (settings.library_dir / "Documents/a (2).txt").read_text(encoding="utf-8") == "a"
    row = conn.execute(
        "SELECT result, final_relpath FROM plan_ops WHERE plan_id=?",
        (plan_id,),
    ).fetchone()
    assert row["result"] == "renamed_collision"
    assert row["final_relpath"] == "Documents/a (2).txt"


def test_hash_mismatch_aborts_before_touching_files(tmp_path: Path) -> None:
    settings, conn, plan_id = setup_plan(
        tmp_path,
        [OperationSpec("move", "a.txt", "library", "Documents/a.txt")],
    )
    conn.execute(
        "UPDATE plan_ops SET dest_relpath='Documents/tampered.txt' WHERE plan_id=?",
        (plan_id,),
    )

    with pytest.raises(ExecutionError, match="plan hash mismatch"):
        execute_plan(conn, plan_id, settings)

    assert (settings.inbox_dir / "a.txt").exists()
    assert not (settings.library_dir / "Documents/tampered.txt").exists()


def test_completed_plan_rerun_is_noop(tmp_path: Path) -> None:
    settings, conn, plan_id = setup_plan(
        tmp_path,
        [OperationSpec("move", "a.txt", "library", "Documents/a.txt")],
    )
    execute_plan(conn, plan_id, settings)
    summary = execute_plan(conn, plan_id, settings)

    assert summary.done == 0
    history_count = conn.execute(
        "SELECT COUNT(*) FROM history WHERE plan_id=?",
        (plan_id,),
    ).fetchone()[0]
    assert history_count == 1
