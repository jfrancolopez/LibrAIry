from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from librairy.config import Settings
from librairy.models import EvidenceEntry
from librairy.taxonomy import RenderResult, clean_name_from_title, render_destination

VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".m4v", ".webm"}
JUNK = re.compile(
    r"\b(480p|720p|1080p|2160p|4k|x264|x265|h264|h265|webrip|bluray|brrip|dvdrip|hdrip|aac|dts)\b",
    re.IGNORECASE,
)
EPISODE_RE = re.compile(r"(?i)\bS(?P<season>\d{1,2})E(?P<episode>\d{1,2})\b")
YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")


@dataclass(frozen=True)
class ParsedVideoName:
    title: str
    year: int | None
    season: int | None
    episode: int | None
    ext: str

    @property
    def is_episode(self) -> bool:
        return self.season is not None and self.episode is not None


@dataclass(frozen=True)
class VideoClassification:
    category: str
    clean_name: str
    dest_relpath: str | None
    confidence: float
    evidence: tuple[EvidenceEntry, ...]
    fields: dict[str, object]
    group_key: str | None = None
    reason: str | None = None


TmdbLookup = Callable[[ParsedVideoName, Settings], dict[str, Any] | None]


def parse_video_name(relpath: str) -> ParsedVideoName:
    path = PurePosixPath(relpath)
    ext = path.suffix.lower()
    stem = path.stem
    episode_match = EPISODE_RE.search(stem)
    year_match = YEAR_RE.search(stem)
    title_part = stem[: episode_match.start()] if episode_match else stem
    if year_match and not episode_match:
        title_part = stem[: year_match.start()]
    title = JUNK.sub("", title_part)
    title = re.sub(r"[._-]+", " ", title)
    title = re.sub(r"\s+", " ", title).strip().title() or path.stem
    return ParsedVideoName(
        title=title,
        year=int(year_match.group(1)) if year_match else None,
        season=int(episode_match.group("season")) if episode_match else None,
        episode=int(episode_match.group("episode")) if episode_match else None,
        ext=ext,
    )


def classify_video(
    relpath: str,
    *,
    settings: Settings,
    tmdb_lookup: TmdbLookup | None = None,
) -> VideoClassification:
    parsed = parse_video_name(relpath)
    evidence: list[EvidenceEntry] = [EvidenceEntry("heuristic", "title", parsed.title, 0.55)]
    tmdb = None
    if settings.tmdb_key.get_secret_value() and tmdb_lookup is not None:
        tmdb = tmdb_lookup(parsed, settings)

    if parsed.is_episode:
        return _classify_episode(parsed, settings, tmdb, evidence)
    return _classify_movie(parsed, settings, tmdb, evidence)


def _classify_movie(
    parsed: ParsedVideoName,
    settings: Settings,
    tmdb: dict[str, Any] | None,
    evidence: list[EvidenceEntry],
) -> VideoClassification:
    title = str(tmdb.get("title") if tmdb else parsed.title)
    year = _year_from_tmdb(tmdb) or parsed.year or 0
    genre = _genre_from_tmdb(tmdb) or "General"
    confidence = 0.86 if tmdb else 0.65
    if tmdb:
        evidence.append(EvidenceEntry("tmdb", "title", title, 0.86))
    clean_name = clean_name_from_title(f"{title} ({year})", parsed.ext)
    fields: dict[str, object] = {
        "title": title,
        "year": year,
        "genre": genre,
        "clean_name": clean_name,
    }
    rendered = _render_if_confident("movies", fields, confidence, settings)
    return VideoClassification(
        "movies",
        clean_name,
        rendered.relpath,
        confidence,
        tuple(evidence),
        fields,
        reason=rendered.reason,
    )


def _classify_episode(
    parsed: ParsedVideoName,
    settings: Settings,
    tmdb: dict[str, Any] | None,
    evidence: list[EvidenceEntry],
) -> VideoClassification:
    show = str(tmdb.get("name") if tmdb else parsed.title)
    genre = _genre_from_tmdb(tmdb) or "General"
    confidence = 0.84 if tmdb else 0.62
    if tmdb:
        evidence.append(EvidenceEntry("tmdb", "show", show, 0.84))
    clean_name = clean_name_from_title(f"S{parsed.season:02d}E{parsed.episode:02d}", parsed.ext)
    fields: dict[str, object] = {
        "show": show,
        "season": parsed.season,
        "episode": parsed.episode,
        "genre": genre,
        "clean_name": clean_name,
    }
    rendered = _render_if_confident("shows", fields, confidence, settings)
    group_key = f"show:{show}:s{parsed.season:02d}"
    return VideoClassification(
        "shows",
        clean_name,
        rendered.relpath,
        confidence,
        tuple(evidence),
        fields,
        group_key,
        rendered.reason,
    )


def _render_if_confident(
    category: str,
    fields: dict[str, object],
    confidence: float,
    settings: Settings,
) -> RenderResult:
    if confidence < settings.confidence_threshold:
        return RenderResult(None, "below confidence threshold")
    return render_destination(category, fields, library_root=settings.library_dir)


def _genre_from_tmdb(tmdb: dict[str, Any] | None) -> str | None:
    if not tmdb:
        return None
    genres = tmdb.get("genres") or tmdb.get("genre_names") or []
    if genres and isinstance(genres[0], dict):
        return str(genres[0].get("name"))
    if genres:
        return str(genres[0])
    return None


def _year_from_tmdb(tmdb: dict[str, Any] | None) -> int | None:
    if not tmdb:
        return None
    date = str(tmdb.get("release_date") or tmdb.get("first_air_date") or "")
    match = YEAR_RE.search(date)
    return int(match.group(1)) if match else None
