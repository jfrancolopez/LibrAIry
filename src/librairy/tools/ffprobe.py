from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from librairy.config import Settings
from librairy.tools.common import ToolResult, posix_path, run_json_tool


@dataclass(frozen=True)
class MediaMetadata:
    format_name: str | None
    duration: float | None
    tags: dict[str, str]
    streams: tuple[dict[str, Any], ...]


def probe(path: Path, settings: Settings) -> ToolResult:
    result = run_json_tool(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            posix_path(path),
        ],
        settings,
    )
    if not result.ok or not isinstance(result.data, dict):
        return result
    return ToolResult(True, data=parse_ffprobe(result.data).__dict__)


def parse_ffprobe(data: dict[str, Any]) -> MediaMetadata:
    fmt = data.get("format") or {}
    tags = {str(key).lower(): str(value) for key, value in (fmt.get("tags") or {}).items()}
    duration_raw = fmt.get("duration")
    duration = float(duration_raw) if duration_raw not in {None, "N/A"} else None
    return MediaMetadata(
        format_name=fmt.get("format_name"),
        duration=duration,
        tags=tags,
        streams=tuple(data.get("streams") or ()),
    )
