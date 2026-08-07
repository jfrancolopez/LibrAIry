from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from librairy.config import Settings
from librairy.tools.common import ToolResult, posix_path

VALID_MODES = {"dup", "image", "video"}


@dataclass(frozen=True)
class SimilarMediaFile:
    path: str
    score: float | None = None


@dataclass(frozen=True)
class SimilarMediaGroup:
    files: tuple[SimilarMediaFile, ...]


def similar_media(roots: list[Path], mode: str, settings: Settings) -> ToolResult:
    if mode not in VALID_MODES:
        raise ValueError(f"unknown czkawka mode: {mode}")
    binary = "czkawka_cli"
    if shutil.which(binary) is None:
        return ToolResult(False, error=f"missing binary: {binary}")
    with tempfile.TemporaryDirectory(prefix="librairy-czkawka-") as temp_dir:
        output_path = Path(temp_dir) / "czkawka.json"
        command = [binary, mode]
        # One directory per -d flag, exactly like -x below. Passing them as a
        # list after a single -d made czkawka read the second root as a stray
        # positional and refuse the whole command, so the scan never ran once:
        # "error: unexpected argument '/data/library' found".
        for root in roots:
            command += ["-d", posix_path(root)]
        command += [
            "-C",
            posix_path(output_path),
            # czkawka exits non-zero when it *finds* something; without this a
            # successful detection would be reported as a tool failure.
            "-W",
        ]
        command += _sensitivity(mode, settings)
        # czkawka takes one extension per -x flag. A comma-joined list is read as a
        # single bogus extension, which excludes everything the tool supports: the
        # scan then aborts, writes `[]`, and still exits 0 — a silent no-op.
        for extension in settings.czkawka_extensions:
            command += ["-x", extension]
        try:
            result = subprocess.run(
                command,
                text=True,
                capture_output=True,
                timeout=settings.ai_timeout,
                check=False,
                env=_environment(settings),
            )
        except subprocess.TimeoutExpired:
            return ToolResult(False, error=f"timeout: {binary}")
        if result.returncode != 0:
            error = result.stderr.strip() or f"{binary} exited {result.returncode}"
            return ToolResult(False, error=error)
        try:
            data = json.loads(output_path.read_text(encoding="utf-8"))
        except OSError as exc:
            return ToolResult(False, error=f"missing JSON output from {binary}: {exc}")
        except json.JSONDecodeError as exc:
            return ToolResult(False, error=f"invalid JSON from {binary}: {exc}")
    return ToolResult(True, data=parse_similar_media(data))


#  czkawka scores images 0-40 and videos 0-20, on scales nobody can calibrate
#  without running the tool twice. These three were measured against a real
#  library: at 5 only visually identical files group, and at 20 eleven
#  unrelated photographs arrive as one "similar" pile.
SENSITIVITY = {
    "strict": {"image": 5, "video": 5},
    "balanced": {"image": 12, "video": 10},
    "loose": {"image": 20, "video": 15},
}
#  The two modes spell the same idea differently, and passing the wrong flag is
#  an "unexpected argument" that kills the whole scan.
SENSITIVITY_FLAG = {"image": "--max-difference", "video": "--tolerance"}


def _sensitivity(mode: str, settings: Settings) -> list[str]:
    level = SENSITIVITY.get(str(settings.czkawka_similarity).strip().lower())
    flag = SENSITIVITY_FLAG.get(mode)
    if level is None or flag is None or mode not in level:
        # An unknown word is a typo in a config file, not a reason to stop
        # looking for duplicates. czkawka's own default stands in.
        return []
    return [flag, str(level[mode])]


def _environment(settings: Settings) -> dict[str, str]:
    """czkawka's cache, on the appdata volume rather than under $HOME.

    Two reasons. It has to be somewhere writable — the container drops to
    PUID:PGID with HOME still pointing at root's home, and czkawka panics on a
    cache it cannot create, exiting 101 with an empty stderr. And it is worth
    keeping: the cache holds a perceptual hash per image, which is the entire
    cost of the scan. Under HOME it was thrown away with every `docker compose
    up`; here it survives, and the second scan of a large library is instant.
    """
    cache = settings.appdata_dir / "cache" / "czkawka"
    with suppress(OSError):
        cache.mkdir(parents=True, exist_ok=True)
    return {
        **os.environ,
        "HOME": str(cache),
        "XDG_CACHE_HOME": str(cache),
        "XDG_CONFIG_HOME": str(cache),
    }


def parse_similar_media(data: Any) -> list[SimilarMediaGroup]:
    raw_groups = data.get("groups", data) if isinstance(data, dict) else data
    groups: list[SimilarMediaGroup] = []
    for raw_group in raw_groups or []:
        files = _files_from_group(raw_group)
        if len(files) > 1:
            groups.append(SimilarMediaGroup(tuple(files)))
    return groups


def _files_from_group(raw_group: Any) -> list[SimilarMediaFile]:
    if isinstance(raw_group, dict):
        raw_files = (
            raw_group.get("files") or raw_group.get("items") or raw_group.get("duplicates") or []
        )
    else:
        raw_files = raw_group
    files: list[SimilarMediaFile] = []
    for raw_file in raw_files or []:
        if isinstance(raw_file, str):
            files.append(SimilarMediaFile(raw_file))
        elif isinstance(raw_file, dict):
            path = raw_file.get("path") or raw_file.get("file") or raw_file.get("name")
            if path:
                files.append(SimilarMediaFile(str(path), _score(raw_file)))
    return files


def _score(raw_file: dict[str, Any]) -> float | None:
    value = raw_file.get("similarity") or raw_file.get("score")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
