from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

DESTRUCTIVE_VERBS = {
    "sync",
    "delete",
    "deletefile",
    "purge",
    "move",
    "moveto",
    "rmdir",
    "rmdirs",
    "cleanup",
}
ALLOWED_VERBS = {"copy", "copyto", "check", "lsjson", "listremotes", "version", "about"}

#  Flags that make a *permitted* verb destructive. `rclone copy --delete-excluded`
#  removes files at the destination, and the verb allowlist above cannot see it:
#  every check here was on `command[1]`, so a destructive option travelling as an
#  argument went straight through. Matched by prefix, because rclone spells
#  several of these with a suffix (`--delete-before`, `--delete-during`) and a
#  new one should be refused before anybody has heard of it.
#
#  Kept as defence in depth. It is not the boundary — `ALLOWED_FLAGS` is.
DESTRUCTIVE_FLAGS = (
    "--delete",
    "--rmdirs",
    "--purge",
    "--backup-dir",
    "--max-delete",
    "--suffix",
)

#  **The boundary.** Every option this program may pass, listed.
#
#  An allowlist rather than a denylist, and the difference is the whole point:
#  a denylist has to keep up with every option rclone will ever add, and it only
#  has to be behind once. This has to keep up with what LibrAIry needs, which is
#  a change somebody makes deliberately, with a test, in this file.
#
#  So a destructive option cannot arrive by being unheard-of. It has to be added
#  here first, by name, by somebody reading this comment.
ALLOWED_FLAGS = frozenset(
    {
        "--config",
        "--bwlimit",
        "--transfers",
        "--checkers",
        "--contimeout",
        "--timeout",
        "--retries",
        "--low-level-retries",
        "--stats",
        "--stats-one-line",
        "--use-json-log",
        "--log-level",
        "--fast-list",
        "--size-only",
        "--checksum",
        "--no-traverse",
        "--ignore-existing",
        "--update",
        "--recursive",
        "-R",
        "--json",
        "--files-from",
        "--no-check-dest",
    }
)


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
    *,
    stats: bool = False,
) -> list[str]:
    command = ["rclone", "copy", str(source), remote, "--config", str(config_path)]
    if bandwidth_limit:
        command.extend(["--bwlimit", bandwidth_limit])
    if stats:
        #  Make rclone report what it moved. A run that fails halfway otherwise
        #  leaves no evidence of how far it got, and the alternative to evidence
        #  is a guess. See `transfer_run._moved`.
        command.extend(["--stats-one-line", "--stats", "1m"])
    return _assert_safe(command)


def check_command(config_path: Path, source: Path, remote: str) -> list[str]:
    return _assert_safe(["rclone", "check", str(source), remote, "--config", str(config_path)])


def run(command: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
    _assert_safe(command)
    return subprocess.run(command, text=True, capture_output=True, timeout=timeout, check=False)


def lsjson_command(
    config_path: Path, target: str, *, recursive: bool = True
) -> list[str]:
    """List what is at a destination. Reads, and cannot do anything else.

    This is how a comparison finds out what is already there — including the
    files the Library no longer has, which every mode reports and none removes.
    """
    command = ["rclone", "lsjson", target, "--config", str(config_path)]
    if recursive:
        command.append("-R")
    return _assert_safe(command)


def _assert_safe(command: list[str]) -> list[str]:
    """The one gate. Every command in this module goes through it, twice.

    Once when it is built and once when it is run — belt and braces on purpose,
    because the failure being guarded against is a command assembled somewhere
    else and handed to `run`.
    """
    if len(command) < 2 or command[0] != "rclone" or command[1] not in ALLOWED_VERBS:  # noqa: PLR2004
        raise RcloneError("unsupported rclone command")
    forbidden = DESTRUCTIVE_VERBS.intersection(command)
    if forbidden:
        raise RcloneError(f"destructive rclone verb refused: {sorted(forbidden)[0]}")
    for argument in command[2:]:
        if not argument.startswith("-"):
            #  A path or a remote. Where those may point is decided by
            #  `librairy/transfer_paths.py`, which is a different question and
            #  a much longer one.
            continue
        if any(argument.startswith(flag) for flag in DESTRUCTIVE_FLAGS):
            raise RcloneError(f"destructive rclone option refused: {argument}")
        #  `--flag=value` and `--flag value` are the same flag. Splitting means
        #  the allowlist holds names rather than having to anticipate spellings.
        name = argument.split("=", 1)[0]
        if name not in ALLOWED_FLAGS:
            raise RcloneError(f"rclone option not on the allowlist: {name}")
    return command
