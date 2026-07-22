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
            "CONFIDENCE_THRESHOLD": "0.8",
            "TMDB_KEY": "",
            "ACOUSTID_KEY": "",
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


def test_analyze_propose_plan_commit_flow_keeps_pending_in_inbox(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    library = tmp_path / "library"
    quarantine = tmp_path / "quarantine"
    inbox.mkdir()
    library.mkdir()
    quarantine.mkdir()
    (inbox / "Dune.epub").write_text("book", encoding="utf-8")
    (inbox / "scan001.pdf").write_text("scan", encoding="utf-8")

    assert json.loads(run_cli(tmp_path, "scan").stdout)["hashed"] == 2
    summary = json.loads(run_cli(tmp_path, "analyze").stdout)
    assert summary == {"analyzed": 2, "pending": 1, "proposed": 1}
    proposals = json.loads(run_cli(tmp_path, "proposals", "list").stdout)["proposals"]
    assert len(proposals) == 2
    confident = [proposal for proposal in proposals if proposal["dest_relpath"]]
    assert len(confident) == 1

    plan_id = json.loads(run_cli(tmp_path, "propose-plan", "--min-confidence", "0.8").stdout)[
        "plan_id"
    ]
    assert json.loads(run_cli(tmp_path, "plan", "approve", plan_id).stdout)["status"] == "approved"
    assert json.loads(run_cli(tmp_path, "commit", plan_id, "--yes").stdout)["done"] == 1

    assert (library / "Books/Unknown Author/Dune/Dune.epub").exists()
    assert (inbox / "scan001.pdf").exists()


def test_analyze_does_not_mutate_inbox_tree(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    library = tmp_path / "library"
    quarantine = tmp_path / "quarantine"
    inbox.mkdir()
    library.mkdir()
    quarantine.mkdir()
    file_path = inbox / "scan001.pdf"
    file_path.write_text("scan", encoding="utf-8")
    before = sorted(path.relative_to(inbox).as_posix() for path in inbox.rglob("*"))
    run_cli(tmp_path, "scan")
    run_cli(tmp_path, "analyze")
    after = sorted(path.relative_to(inbox).as_posix() for path in inbox.rglob("*"))

    assert before == after == ["scan001.pdf"]
