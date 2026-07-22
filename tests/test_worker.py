from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from librairy import worker as worker_module
from librairy.config import Settings
from librairy.db import connect
from librairy.dedup import set_dedup_option
from librairy.worker import next_sleep, run_once


def settings_for(tmp_path: Path, batch_size: int = 50) -> Settings:
    return Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        FILE_STABILITY_SECONDS=0,
        BATCH_SIZE=batch_size,
        _env_file=None,
    )


def test_worker_once_scans_analyzes_and_second_cycle_is_near_noop(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    settings.inbox_dir.mkdir()
    settings.library_dir.mkdir()
    settings.quarantine_dir.mkdir()
    project = settings.inbox_dir / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text("[project]", encoding="utf-8")
    conn = connect(settings)

    first = run_once(conn, settings)
    second = run_once(conn, settings)

    assert first.scanned == 1
    assert first.analyzed == 1
    assert first.proposed == 1
    assert second.hashed == 0
    assert second.analyzed == 0
    assert conn.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 1
    state = conn.execute("SELECT value FROM worker_state WHERE key='current_phase'").fetchone()[0]
    assert state == '"idle"'


def test_worker_cycle_holds_global_lock(tmp_path: Path, monkeypatch) -> None:
    settings = settings_for(tmp_path)
    settings.inbox_dir.mkdir()
    settings.library_dir.mkdir()
    settings.quarantine_dir.mkdir()
    conn = connect(settings)
    events: list[str] = []

    class FakeLock:
        def __enter__(self):
            events.append("enter")

        def __exit__(self, exc_type, exc, traceback):  # noqa: ANN001
            events.append("exit")

    monkeypatch.setattr(worker_module, "acquire_lock", lambda settings: FakeLock())

    run_once(conn, settings)

    assert events == ["enter", "exit"]


def test_worker_honors_batch_size(tmp_path: Path) -> None:
    settings = settings_for(tmp_path, batch_size=1)
    settings.inbox_dir.mkdir()
    settings.library_dir.mkdir()
    settings.quarantine_dir.mkdir()
    (settings.inbox_dir / "a").mkdir()
    (settings.inbox_dir / "b").mkdir()
    (settings.inbox_dir / "a" / "pyproject.toml").write_text("a", encoding="utf-8")
    (settings.inbox_dir / "b" / "pyproject.toml").write_text("b", encoding="utf-8")
    conn = connect(settings)

    summary = run_once(conn, settings)

    assert summary.analyzed == 1
    assert conn.execute("SELECT COUNT(*) FROM items WHERE state='discovered'").fetchone()[0] == 1


def test_worker_stages_exact_duplicate_quarantine_proposal(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    settings.inbox_dir.mkdir()
    settings.library_dir.mkdir()
    settings.quarantine_dir.mkdir()
    (settings.inbox_dir / "copy.txt").write_text("same", encoding="utf-8")
    (settings.library_dir / "original.txt").write_text("same", encoding="utf-8")
    conn = connect(settings)
    from librairy.scanner import scan_root

    set_dedup_option(conn, "use_rmlint", False)
    scan_root(conn, "library", settings.library_dir, settings)

    summary = run_once(conn, settings)

    assert summary.duplicate_candidates == 1
    proposal = conn.execute("SELECT action, dest_root, dest_relpath FROM proposals").fetchone()
    assert proposal["action"] == "quarantine"
    assert proposal["dest_root"] == "quarantine"
    assert proposal["dest_relpath"].endswith("/copy.txt")


def test_worker_never_imports_or_calls_executor() -> None:
    source = Path("src/librairy/worker.py").read_text(encoding="utf-8")

    assert "execute_plan" not in source
    assert "executor" not in source


def test_worker_backoff_bounds() -> None:
    assert next_sleep(5.0, work_found=True) == 0.5
    assert next_sleep(5.0, work_found=False) == 10.0
    assert next_sleep(60.0, work_found=False) == 60.0


def test_worker_cli_once(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    library = tmp_path / "library"
    quarantine = tmp_path / "quarantine"
    inbox.mkdir()
    library.mkdir()
    quarantine.mkdir()
    project = inbox / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text("[project]", encoding="utf-8")
    env = os.environ.copy()
    env.update(
        {
            "APPDATA_DIR": str(tmp_path / "appdata"),
            "INBOX_DIR": str(inbox),
            "LIBRARY_DIR": str(library),
            "QUARANTINE_DIR": str(quarantine),
            "FILE_STABILITY_SECONDS": "0",
        }
    )

    result = subprocess.run(
        [sys.executable, "-m", "librairy", "--json", "worker", "--once"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert json.loads(result.stdout)["analyzed"] == 1
