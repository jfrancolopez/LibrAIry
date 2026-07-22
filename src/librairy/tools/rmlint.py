from __future__ import annotations

from pathlib import Path
from typing import Any

from librairy.config import Settings
from librairy.tools.common import ToolResult, posix_path, run_json_tool


def duplicates(paths: list[Path], settings: Settings) -> ToolResult:
    if not paths:
        return ToolResult(True, data=[])
    command = [
        "rmlint",
        "--types=duplicates",
        "-o",
        "json:-",
        *[posix_path(path) for path in paths],
    ]
    return run_json_tool(command, settings)


def duplicate_path_pairs(data: list[Any]) -> set[frozenset[str]]:
    groups: dict[str, list[str]] = {}
    for entry in data:
        if not isinstance(entry, dict) or entry.get("type") != "duplicate_file":
            continue
        checksum = str(entry.get("checksum") or entry.get("digest") or "")
        path = entry.get("path")
        if checksum and path:
            groups.setdefault(checksum, []).append(str(path))
    pairs: set[frozenset[str]] = set()
    for paths in groups.values():
        for index, left in enumerate(paths):
            for right in paths[index + 1 :]:
                pairs.add(frozenset((left, right)))
    return pairs
