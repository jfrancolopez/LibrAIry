from __future__ import annotations

from pathlib import Path

from librairy.config import Settings
from librairy.db import connect
from librairy.executor import execute_plan
from librairy.history import undo_op, undo_plan
from librairy.planner import OperationSpec, approve_plan, create_plan
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


def setup_committed_plan(tmp_path: Path, op_type: str = "move"):
    settings = settings_for(tmp_path)
    settings.inbox_dir.mkdir()
    settings.library_dir.mkdir()
    settings.quarantine_dir.mkdir()
    (settings.inbox_dir / "a.txt").write_text("a", encoding="utf-8")
    conn = connect(settings)
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    dest_root = "quarantine" if op_type == "quarantine" else "library"
    dest_relpath = "dupes/a.txt" if op_type == "quarantine" else "Documents/a.txt"
    plan_id = create_plan(
        conn,
        [OperationSpec(op_type, "a.txt", dest_root, dest_relpath)],
        settings,
    )
    approve_plan(conn, plan_id, settings)
    execute_plan(conn, plan_id, settings)
    return settings, conn, plan_id


def test_undo_plan_restores_original_tree_and_journals_undo(tmp_path: Path) -> None:
    settings, conn, plan_id = setup_committed_plan(tmp_path)

    results = undo_plan(conn, plan_id, settings)

    assert results[0].outcome == "ok"
    assert (settings.inbox_dir / "a.txt").read_text(encoding="utf-8") == "a"
    assert not (settings.library_dir / "Documents/a.txt").exists()
    actions = [
        row["action"]
        for row in conn.execute(
            "SELECT action FROM history WHERE plan_id=? ORDER BY id",
            (plan_id,),
        )
    ]
    assert actions == ["move", "undo_move"]


def test_undo_refuses_modified_destination(tmp_path: Path) -> None:
    settings, conn, plan_id = setup_committed_plan(tmp_path)
    moved = settings.library_dir / "Documents/a.txt"
    moved.write_text("changed", encoding="utf-8")

    result = undo_plan(conn, plan_id, settings)[0]

    assert result.outcome.startswith("undo_refused_changed")
    assert moved.read_text(encoding="utf-8") == "changed"
    assert not (settings.inbox_dir / "a.txt").exists()


def test_undo_of_undo_is_possible(tmp_path: Path) -> None:
    settings, conn, plan_id = setup_committed_plan(tmp_path)
    undo_plan(conn, plan_id, settings)
    undo_history_id = conn.execute(
        "SELECT id FROM history WHERE action='undo_move'"
    ).fetchone()[0]

    result = undo_op(conn, undo_history_id, settings)

    assert result.outcome == "ok"
    assert (settings.library_dir / "Documents/a.txt").read_text(encoding="utf-8") == "a"
    assert not (settings.inbox_dir / "a.txt").exists()


def test_undo_quarantine_restores_original_path(tmp_path: Path) -> None:
    settings, conn, plan_id = setup_committed_plan(tmp_path, op_type="quarantine")

    result = undo_plan(conn, plan_id, settings)[0]

    assert result.outcome == "ok"
    assert (settings.inbox_dir / "a.txt").read_text(encoding="utf-8") == "a"
    assert not (settings.quarantine_dir / "dupes/a.txt").exists()
