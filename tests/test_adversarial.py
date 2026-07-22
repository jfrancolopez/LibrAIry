from __future__ import annotations

import errno
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

from librairy.config import Settings
from librairy.db import connect
from librairy.executor import execute_plan
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


def prepare_plan(tmp_path: Path, count: int = 2):
    settings = settings_for(tmp_path)
    settings.inbox_dir.mkdir()
    settings.library_dir.mkdir()
    settings.quarantine_dir.mkdir()
    for index in range(count):
        (settings.inbox_dir / f"file-{index}.txt").write_text(f"file-{index}", encoding="utf-8")
    conn = connect(settings)
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    specs = [
        OperationSpec("move", f"file-{index}.txt", "library", f"Documents/file-{index}.txt")
        for index in range(count)
    ]
    plan_id = create_plan(conn, specs, settings)
    approve_plan(conn, plan_id, settings)
    conn.close()
    return settings, plan_id


def env_for(settings: Settings, marker: Path | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "APPDATA_DIR": str(settings.appdata_dir),
            "INBOX_DIR": str(settings.inbox_dir),
            "LIBRARY_DIR": str(settings.library_dir),
            "QUARANTINE_DIR": str(settings.quarantine_dir),
            "FILE_STABILITY_SECONDS": "0",
        }
    )
    if marker is not None:
        env["LIBRAIRY_TEST_PAUSE_AFTER_OP_MARKER"] = str(marker)
    return env


def test_kill_mid_execution_then_rerun_completes(tmp_path: Path) -> None:
    settings, plan_id = prepare_plan(tmp_path, count=2)
    marker = tmp_path / "paused"
    process = subprocess.Popen(
        [sys.executable, "-m", "librairy", "--json", "commit", plan_id, "--yes"],
        env=env_for(settings, marker),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.time() + 10
        while not marker.exists():
            if time.time() > deadline:
                raise AssertionError("executor did not pause after first op")
            time.sleep(0.05)
        os.kill(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()

    result = subprocess.run(
        [sys.executable, "-m", "librairy", "--json", "commit", plan_id, "--yes"],
        env=env_for(settings),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert (settings.library_dir / "Documents/file-0.txt").read_text(encoding="utf-8") == "file-0"
    assert (settings.library_dir / "Documents/file-1.txt").read_text(encoding="utf-8") == "file-1"
    assert not list(settings.library_dir.rglob("*.part-*"))
    assert not list(settings.inbox_dir.glob("file-*.txt"))


def test_double_execution_race_only_one_process_proceeds(tmp_path: Path) -> None:
    settings, plan_id = prepare_plan(tmp_path, count=2)
    marker = tmp_path / "paused"
    first = subprocess.Popen(
        [sys.executable, "-m", "librairy", "--json", "commit", plan_id, "--yes"],
        env=env_for(settings, marker),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.time() + 10
        while not marker.exists():
            if time.time() > deadline:
                raise AssertionError("executor did not pause after first op")
            time.sleep(0.05)
        second = subprocess.run(
            [sys.executable, "-m", "librairy", "--json", "commit", plan_id, "--yes"],
            env=env_for(settings),
            text=True,
            capture_output=True,
            check=False,
        )
        assert second.returncode == 2
        assert "another LibrAIry process holds the lock" in second.stderr
    finally:
        if first.poll() is None:
            first.kill()
            first.wait(timeout=5)


def test_forced_cross_device_copy_verifies_before_source_removal(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings, plan_id = prepare_plan(tmp_path, count=1)
    conn = connect(settings)

    def raise_cross_device(src, dest):
        raise OSError(errno.EXDEV, "cross device")

    monkeypatch.setattr("librairy.executor.os.rename", raise_cross_device)
    summary = execute_plan(conn, plan_id, settings)

    assert summary.done == 1
    assert (settings.library_dir / "Documents/file-0.txt").read_text(encoding="utf-8") == "file-0"
    assert not (settings.inbox_dir / "file-0.txt").exists()


def test_cross_device_bad_copy_keeps_source_and_cleans_temp(tmp_path: Path, monkeypatch) -> None:
    settings, plan_id = prepare_plan(tmp_path, count=1)
    conn = connect(settings)

    def raise_cross_device(src, dest):
        raise OSError(errno.EXDEV, "cross device")

    def bad_copy(src, dest):
        Path(dest).write_text("corrupt", encoding="utf-8")

    monkeypatch.setattr("librairy.executor.os.rename", raise_cross_device)
    monkeypatch.setattr("librairy.executor.shutil.copy2", bad_copy)
    summary = execute_plan(conn, plan_id, settings)

    assert summary.failed == 1
    assert (settings.inbox_dir / "file-0.txt").read_text(encoding="utf-8") == "file-0"
    assert not list(settings.library_dir.rglob("*.part-*"))


def test_safety_invariants_forbid_moves_outside_executor() -> None:
    package_root = Path("src/librairy")
    move_pattern = re.compile(
        r"\b(shutil\.move|os\.rename|os\.replace|Path\.rename|Path\.replace)\b"
    )
    delete_pattern = re.compile(r"\b(os\.unlink|shutil\.rmtree|Path\.unlink|send2trash)\b")
    allowed = {Path("src/librairy/executor.py")}

    violations: list[str] = []
    for path in package_root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if path not in allowed and move_pattern.search(text):
            violations.append(f"move primitive outside executor: {path}")
        if path not in allowed and delete_pattern.search(text):
            violations.append(f"delete primitive outside executor: {path}")
    assert violations == []


def test_stale_plan_replaced_source_tree_skips_without_move(tmp_path: Path) -> None:
    settings, plan_id = prepare_plan(tmp_path, count=1)
    original = settings.inbox_dir / "file-0.txt"
    original.write_text("replacement", encoding="utf-8")
    conn = connect(settings)

    summary = execute_plan(conn, plan_id, settings)

    assert summary.skipped_changed == 1
    assert original.read_text(encoding="utf-8") == "replacement"
    assert not (settings.library_dir / "Documents/file-0.txt").exists()


def test_unicode_filename_commit(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    settings.inbox_dir.mkdir()
    settings.library_dir.mkdir()
    settings.quarantine_dir.mkdir()
    name = "unicodé-file.txt"
    (settings.inbox_dir / name).write_text("ok", encoding="utf-8")
    conn = connect(settings)
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    plan_id = create_plan(
        conn,
        [OperationSpec("move", name, "library", f"Documents/{name}")],
        settings,
    )
    approve_plan(conn, plan_id, settings)

    execute_plan(conn, plan_id, settings)

    assert (settings.library_dir / f"Documents/{name}").read_text(encoding="utf-8") == "ok"
