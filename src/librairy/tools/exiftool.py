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
