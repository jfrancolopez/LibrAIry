from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from librairy.config import Settings
from librairy.tools.common import ToolResult, posix_path, run_json_tool

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
    command = [
        "czkawka_cli",
        mode,
        "-d",
        *[posix_path(root) for root in roots],
        "-f",
        "json",
        "-e",
        ",".join(settings.czkawka_extensions),
    ]
    result = run_json_tool(command, settings)
    if not result.ok:
        return result
    return ToolResult(True, data=parse_similar_media(result.data))


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
