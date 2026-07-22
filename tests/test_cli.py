from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def env_for(tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "APPDATA_DIR": str(tmp_path / "appdata"),
            "INBOX_DIR": str(tmp_path / "inbox"),
            "LIBRARY_DIR": str(tmp_path / "library"),
            "QUARANTINE_DIR": str(tmp_path / "quarantine"),
            "FILE_STABILITY_SECONDS": "0",
        }
    )
    return env


def run_cli(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "librairy", "--json", *args],
        env=env_for(tmp_path),
        text=True,
        capture_output=True,
        check=False,
    )


def test_cli_full_lifecycle(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    library = tmp_path / "library"
    quarantine = tmp_path / "quarantine"
    inbox.mkdir()
    library.mkdir()
    quarantine.mkdir()
    (inbox / "a.txt").write_text("a", encoding="utf-8")
    ops = tmp_path / "ops.json"
    ops.write_text(
        json.dumps(
            [
                {
                    "op_type": "move",
                    "src_relpath": "a.txt",
                    "dest_root": "library",
                    "dest_relpath": "Documents/a.txt",
                }
            ]
        ),
        encoding="utf-8",
    )

    assert json.loads(run_cli(tmp_path, "scan").stdout)["hashed"] == 1
    created = json.loads(run_cli(tmp_path, "plan", "create", "--from-file", str(ops)).stdout)
    plan_id = created["plan_id"]
    shown = json.loads(run_cli(tmp_path, "plan", "show", plan_id).stdout)
    assert shown["plan"]["status"] == "draft"
    approved = json.loads(run_cli(tmp_path, "plan", "approve", plan_id).stdout)
    assert approved["status"] == "approved"
    needs_yes = json.loads(run_cli(tmp_path, "commit", plan_id).stdout)
    assert needs_yes["error"] == "commit requires --yes"
    committed = json.loads(run_cli(tmp_path, "commit", plan_id, "--yes").stdout)
    assert committed["done"] == 1
    assert (library / "Documents/a.txt").read_text(encoding="utf-8") == "a"
    history = json.loads(run_cli(tmp_path, "history", "--plan", plan_id).stdout)
    assert len(history["history"]) == 1
    undone = json.loads(run_cli(tmp_path, "undo", "--plan", plan_id, "--yes").stdout)
    assert undone["results"][0]["outcome"] == "ok"
    assert (inbox / "a.txt").read_text(encoding="utf-8") == "a"


def test_cli_rejects_escape_plan_at_approval(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    library = tmp_path / "library"
    quarantine = tmp_path / "quarantine"
    inbox.mkdir()
    library.mkdir()
    quarantine.mkdir()
    (inbox / "a.txt").write_text("a", encoding="utf-8")
    run_cli(tmp_path, "scan")
    ops = tmp_path / "ops.json"
    ops.write_text(
        json.dumps(
            [
                {
                    "op_type": "move",
                    "src_relpath": "a.txt",
                    "dest_root": "library",
                    "dest_relpath": "../../escape.txt",
                }
            ]
        ),
        encoding="utf-8",
    )

    result = run_cli(tmp_path, "plan", "create", "--from-file", str(ops))

    assert result.returncode == 2
    assert "traversal" in result.stderr
