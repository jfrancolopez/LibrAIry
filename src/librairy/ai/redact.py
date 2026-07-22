from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from librairy.classify.hashtags import extract_hashtags
from librairy.models import EvidenceEntry, Item

SAFE_TAGS = {
    "title",
    "artist",
    "album",
    "album_artist",
    "albumartist",
    "genre",
    "track",
    "tracknumber",
}
PATH_MARKERS = ("/data/", "/Users/", "C:\\", "\\\\")
COORD_RE = re.compile(r"-?\d{1,3}\.\d{4,}")
YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2}|2100)\b")


class RedactedItemView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_path: str
    file_name: str
    extension: str
    size_bucket: str
    media_kind: str
    duration_seconds: int | None = None
    resolution: str | None = None
    codec: str | None = None
    embedded_title: str | None = None
    embedded_artist: str | None = None
    embedded_album: str | None = None
    embedded_genre: str | None = None
    track_number: int | None = None
    year: int | None = None
    sibling_file_names: tuple[str, ...] = Field(default_factory=tuple, max_length=20)
    folder_chain: tuple[str, ...] = Field(default_factory=tuple)
    hashtag_hints: tuple[str, ...] = Field(default_factory=tuple)
    evidence_summaries: tuple[str, ...] = Field(default_factory=tuple, max_length=20)


def build_view(
    item: Item,
    metadata: dict[str, Any] | None = None,
    evidence: tuple[EvidenceEntry, ...] | list[EvidenceEntry] = (),
    sibling_names: tuple[str, ...] | list[str] = (),
) -> RedactedItemView:
    metadata = metadata or {}
    relpath = _relative_display_path(item.relpath)
    path = PurePosixPath(relpath)
    tags = _safe_tags(metadata)
    return RedactedItemView(
        display_path=relpath,
        file_name=path.name,
        extension=path.suffix.lower(),
        size_bucket=_size_bucket(item.size),
        media_kind=_media_kind(path.suffix.lower()),
        duration_seconds=_duration(metadata),
        resolution=_resolution(metadata),
        codec=_codec(metadata),
        embedded_title=tags.get("title"),
        embedded_artist=tags.get("artist") or tags.get("album_artist") or tags.get("albumartist"),
        embedded_album=tags.get("album"),
        embedded_genre=tags.get("genre"),
        track_number=_track(tags.get("track") or tags.get("tracknumber")),
        year=_year(metadata, tags),
        sibling_file_names=tuple(_safe_component(name) for name in sibling_names[:20]),
        folder_chain=tuple(
            _safe_component(part) for part in path.parent.parts if part not in {"", "."}
        ),
        hashtag_hints=extract_hashtags(relpath).tags,
        evidence_summaries=tuple(_evidence_summary(entry) for entry in evidence[:20]),
    )


def _relative_display_path(relpath: str) -> str:
    path = PurePosixPath(relpath)
    parts = [part for part in path.parts if part not in {"/", "", ".", ".."}]
    return PurePosixPath(*(_safe_component(part) for part in parts)).as_posix()


def _safe_tags(metadata: dict[str, Any]) -> dict[str, str]:
    raw = metadata.get("tags") if isinstance(metadata.get("tags"), dict) else metadata
    tags: dict[str, str] = {}
    for key, value in raw.items():
        normalized = str(key).lower()
        if normalized in SAFE_TAGS:
            safe = _safe_text(value)
            if safe:
                tags[normalized] = safe
    return tags


def _safe_component(value: object) -> str:
    text = str(value).replace("\\", " ").replace("/", " ").strip()
    return " ".join(text.split())[:120]


def _safe_text(value: object) -> str | None:
    text = " ".join(str(value).split()).strip()
    if not text or any(marker in text for marker in PATH_MARKERS) or COORD_RE.search(text):
        return None
    if "/" in text or "\\" in text:
        return None
    return text[:120]


def _size_bucket(size: int) -> str:
    if size < 1024:
        return "<1KB"
    if size < 1024 * 1024:
        return "<1MB"
    if size < 1024 * 1024 * 1024:
        return "<1GB"
    return ">=1GB"


def _media_kind(ext: str) -> str:
    if ext in {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".wav"}:
        return "audio"
    if ext in {".mp4", ".mkv", ".mov", ".avi", ".m4v"}:
        return "video"
    if ext in {".jpg", ".jpeg", ".png", ".gif", ".heic", ".webp", ".avif"}:
        return "image"
    if ext in {".pdf", ".doc", ".docx", ".txt", ".rtf", ".md"}:
        return "document"
    return "other"


def _duration(metadata: dict[str, Any]) -> int | None:
    value = metadata.get("duration")
    if value is None:
        return None
    try:
        return max(0, round(float(value)))
    except (TypeError, ValueError):
        return None


def _resolution(metadata: dict[str, Any]) -> str | None:
    stream = _first_video_stream(metadata)
    if not stream:
        return None
    width = stream.get("width")
    height = stream.get("height")
    return f"{width}x{height}" if width and height else None


def _codec(metadata: dict[str, Any]) -> str | None:
    stream = _first_video_stream(metadata) or _first_audio_stream(metadata)
    return _safe_text(stream.get("codec_name")) if stream else None


def _first_video_stream(metadata: dict[str, Any]) -> dict[str, Any] | None:
    return _first_stream(metadata, "video")


def _first_audio_stream(metadata: dict[str, Any]) -> dict[str, Any] | None:
    return _first_stream(metadata, "audio")


def _first_stream(metadata: dict[str, Any], kind: str) -> dict[str, Any] | None:
    streams = metadata.get("streams") or ()
    return next((stream for stream in streams if stream.get("codec_type") == kind), None)


def _track(value: str | None) -> int | None:
    if not value:
        return None
    match = re.match(r"\d+", value)
    return int(match.group(0)) if match else None


def _year(metadata: dict[str, Any], tags: dict[str, str]) -> int | None:
    candidates = [tags.get("date"), tags.get("year"), metadata.get("created_at")]
    candidates.extend(metadata.get(key) for key in ("DateTimeOriginal", "CreateDate", "year"))
    for candidate in candidates:
        match = YEAR_RE.search(str(candidate)) if candidate is not None else None
        if match:
            return int(match.group(1))
    return None


def _evidence_summary(entry: EvidenceEntry) -> str:
    detail = _safe_text(entry.detail) or "redacted"
    return f"{entry.source}:{entry.field}:{detail}:{entry.weight:.2f}"
