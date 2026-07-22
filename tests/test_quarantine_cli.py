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


def test_quarantine_cli_lists_entries(tmp_path: Path) -> None:
    appdata = tmp_path / "appdata"
    appdata.mkdir()
    result = run_cli(tmp_path, "quarantine", "list")

    assert result.returncode == 0
    assert json.loads(result.stdout) == {"entries": []}
