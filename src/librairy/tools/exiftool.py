from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from librairy.config import Settings
from librairy.tools.common import ToolResult, posix_path, run_json_tool


@dataclass(frozen=True)
class ImageMetadata:
    tags: dict[str, Any]
    gps_latitude: Any | None = None
    gps_longitude: Any | None = None
    created_at: Any | None = None
    camera: str | None = None


def extract(path: Path, settings: Settings) -> ToolResult:
    result = run_json_tool(["exiftool", "-j", posix_path(path)], settings)
    if not result.ok or not isinstance(result.data, list):
        return result
    if not result.data:
        return ToolResult(False, error="empty exiftool response")
    return ToolResult(True, data=parse_exiftool(result.data[0]).__dict__)


def extract_many(paths: list[Path], settings: Settings) -> list[ImageMetadata | None]:
    """Metadata for several files in **one** invocation, aligned to `paths`.

    `extract` spawns a process per file, which is right for one photograph
    being classified and wrong for a page of two dozen: twenty-four spawns to
    draw a grid is most of a second spent on process creation alone. exiftool
    has always taken a list, so a page of a large photo group costs exactly one
    subprocess however many pictures are on it.

    Results are matched back by `SourceFile`, because exiftool is not obliged
    to answer in the order it was asked and a file it could not read simply
    does not appear. A missing entry is `None` — no facts, which is a normal
    outcome and never an error.
    """
    if not paths:
        return []
    result = run_json_tool(
        ["exiftool", "-j", *[posix_path(path) for path in paths]], settings
    )
    if not result.ok or not isinstance(result.data, list):
        return [None] * len(paths)
    by_source: dict[str, dict[str, Any]] = {}
    for entry in result.data:
        if isinstance(entry, dict) and entry.get("SourceFile"):
            by_source[str(entry["SourceFile"])] = entry
    return [
        parse_exiftool(by_source[posix_path(path)]) if posix_path(path) in by_source else None
        for path in paths
    ]


def parse_exiftool(data: dict[str, Any]) -> ImageMetadata:
    return ImageMetadata(
        tags=data,
        gps_latitude=data.get("GPSLatitude"),
        gps_longitude=data.get("GPSLongitude"),
        created_at=data.get("DateTimeOriginal") or data.get("CreateDate"),
        camera=" ".join(_camera_parts(data))
        or None,
    )


def _camera_parts(data: dict[str, Any]) -> list[str]:
    return [
        part
        for part in [str(data.get("Make", "")).strip(), str(data.get("Model", "")).strip()]
        if part
    ]
