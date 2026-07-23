from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
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
        command = [
            binary,
            mode,
            "-d",
            *[posix_path(root) for root in roots],
            "-C",
            posix_path(output_path),
            "-x",
            ",".join(settings.czkawka_extensions),
        ]
        try:
            result = subprocess.run(
                command,
                text=True,
                capture_output=True,
                timeout=settings.ai_timeout,
                check=False,
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
