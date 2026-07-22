from __future__ import annotations

from pathlib import Path

from librairy.config import Settings
from librairy.db import connect
from librairy.dedup import set_dedup_option
from librairy.executor import execute_plan
from librairy.history import undo_plan
from librairy.planner import approve_plan, create_plan_from_proposals
from librairy.scanner import scan_root
from librairy.worker import run_once


def settings_for(tmp_path: Path) -> Settings:
    return Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        FILE_STABILITY_SECONDS=0,
        _env_file=None,
    )


def test_headless_worker_plan_commit_and_undo(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    settings.inbox_dir.mkdir()
    settings.library_dir.mkdir()
    settings.quarantine_dir.mkdir()
    project = settings.inbox_dir / "ProjectOne #work"
    project.mkdir()
    (project / "pyproject.toml").write_text("[project]", encoding="utf-8")
    (settings.inbox_dir / "copy.txt").write_text("same", encoding="utf-8")
    (settings.inbox_dir / "mystery.bin").write_text("?", encoding="utf-8")
    (settings.library_dir / "original.txt").write_text("same", encoding="utf-8")
    conn = connect(settings)
    set_dedup_option(conn, "use_rmlint", False)
    scan_root(conn, "library", settings.library_dir, settings)

    summary = run_once(conn, settings)
    plan_id = create_plan_from_proposals(conn, settings, min_confidence=0.8)
    approve_plan(conn, plan_id, settings)
    execution = execute_plan(conn, plan_id, settings)

    assert summary.proposed == 1
    assert summary.pending == 1
    assert summary.duplicate_candidates == 1
    assert execution.done == 2
    assert any(path.is_file() for path in (settings.library_dir / "Projects").rglob("*"))
    assert any(settings.quarantine_dir.rglob("copy.txt"))
    assert conn.execute("SELECT COUNT(*) FROM quarantine_entries").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM similar_media_flags").fetchone()[0] == 0
    assert (settings.inbox_dir / "mystery.bin").exists()

    results = undo_plan(conn, plan_id, settings)

    assert {result.outcome for result in results} == {"ok"}
    assert (settings.inbox_dir / "copy.txt").exists()
    assert (project / "pyproject.toml").exists()
