from __future__ import annotations

from pathlib import Path

import pytest

from librairy.config import Settings
from librairy.db import connect
from librairy.planner import (
    OperationSpec,
    PlanApprovalError,
    PlanError,
    add_plan_op,
    approve_plan,
    canonical_plan_ops,
    compute_plan_hash,
    create_plan,
)
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


def setup_files(tmp_path: Path) -> tuple[Settings, object]:
    settings = settings_for(tmp_path)
    settings.inbox_dir.mkdir()
    settings.library_dir.mkdir()
    settings.quarantine_dir.mkdir()
    (settings.inbox_dir / "a.txt").write_text("a", encoding="utf-8")
    (settings.inbox_dir / "b.txt").write_text("b", encoding="utf-8")
    conn = connect(settings)
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    return settings, conn


def test_draft_to_approved_sets_reproducible_hash(tmp_path: Path) -> None:
    settings, conn = setup_files(tmp_path)
    plan_id = create_plan(
        conn,
        [OperationSpec("move", "a.txt", "library", "Documents/a.txt")],
        settings,
    )

    plan_hash = approve_plan(conn, plan_id, settings)

    assert plan_hash == compute_plan_hash(conn, plan_id)
    row = conn.execute("SELECT status, plan_hash FROM plans WHERE id=?", (plan_id,)).fetchone()
    assert row["status"] == "approved"
    assert row["plan_hash"] == plan_hash
    assert canonical_plan_ops(conn, plan_id)[0]["seq"] == 1


def test_approval_rejects_invalid_plan_ops(tmp_path: Path) -> None:
    settings, conn = setup_files(tmp_path)
    plan_id = create_plan(
        conn,
        [OperationSpec("move", "a.txt", "library", "Documents/a.txt")],
        settings,
    )
    add_plan_op(
        conn,
        plan_id,
        2,
        OperationSpec("move", "b.txt", "library", "Documents/a.txt"),
        settings,
    )
    conn.execute(
        """
        INSERT INTO plan_ops(
          plan_id, seq, op_type, src_root, src_relpath, src_fingerprint, dest_root, dest_relpath
        ) VALUES (?, 3, 'move', 'inbox', 'missing.txt', 'abc', 'library', '../escape.txt')
        """,
        (plan_id,),
    )

    with pytest.raises(PlanApprovalError) as exc_info:
        approve_plan(conn, plan_id, settings)

    message = str(exc_info.value)
    assert "duplicate destination" in message
    assert "source is missing" in message
    assert "invalid destination" in message


def test_approved_plan_cannot_be_modified_via_planner(tmp_path: Path) -> None:
    settings, conn = setup_files(tmp_path)
    plan_id = create_plan(
        conn,
        [OperationSpec("move", "a.txt", "library", "Documents/a.txt")],
        settings,
    )
    approve_plan(conn, plan_id, settings)

    with pytest.raises(PlanError, match="immutable"):
        add_plan_op(
            conn,
            plan_id,
            2,
            OperationSpec("move", "b.txt", "library", "Documents/b.txt"),
            settings,
        )
