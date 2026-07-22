from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

DESTRUCTIVE_VERBS = {"sync", "delete", "purge", "move"}
ALLOWED_VERBS = {"copy", "check", "listremotes", "version"}


class RcloneError(RuntimeError):
    pass


@dataclass(frozen=True)
class RcloneStatus:
    available: bool
    detail: str


def rclone_status(config_path: Path) -> RcloneStatus:
    if shutil.which("rclone") is None:
        return RcloneStatus(False, "rclone binary not found")
    if not config_path.exists():
        return RcloneStatus(False, f"rclone config not found: {config_path}")
    return RcloneStatus(True, "rclone available")


def version_command() -> list[str]:
    return _assert_safe(["rclone", "version"])


def listremotes_command(config_path: Path) -> list[str]:
    return _assert_safe(["rclone", "listremotes", "--config", str(config_path)])


def copy_command(
    config_path: Path,
    source: Path,
    remote: str,
    bandwidth_limit: str = "",
) -> list[str]:
    command = ["rclone", "copy", str(source), remote, "--config", str(config_path)]
    if bandwidth_limit:
        command.extend(["--bwlimit", bandwidth_limit])
    return _assert_safe(command)


def check_command(config_path: Path, source: Path, remote: str) -> list[str]:
    return _assert_safe(["rclone", "check", str(source), remote, "--config", str(config_path)])


def run(command: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
    _assert_safe(command)
    return subprocess.run(command, text=True, capture_output=True, timeout=timeout, check=False)


def _assert_safe(command: list[str]) -> list[str]:
    if len(command) < 2 or command[0] != "rclone" or command[1] not in ALLOWED_VERBS:
        raise RcloneError("unsupported rclone command")
    forbidden = DESTRUCTIVE_VERBS.intersection(command)
    if forbidden:
        raise RcloneError(f"destructive rclone verb refused: {sorted(forbidden)[0]}")
    return command
