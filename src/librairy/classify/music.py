from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from librairy.config import Settings
from librairy.models import EvidenceEntry
from librairy.taxonomy import RenderResult, clean_name_from_title, render_destination

AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".wav"}


@dataclass(frozen=True)
class MusicClassification:
    category: str
    clean_name: str
    dest_relpath: str | None
    confidence: float
    evidence: tuple[EvidenceEntry, ...]
    fields: dict[str, object]
    group_key: str | None = None
    reason: str | None = None


AcoustidLookup = Callable[[str, Settings], dict[str, Any] | None]
MusicBrainzLookup = Callable[[str, Settings], dict[str, Any] | None]
DiscogsLookup = Callable[[str, Settings], dict[str, Any] | None]
GenreLookup = Callable[[str, str, Settings], str | None]

UNKNOWN_ARTIST = "Unknown Artist"
DEFAULT_GENRE = "General"
# A filename only carries a searchable release if it names more than the track:
# "Radiohead - Karma Police.mp3" is worth a query, "track01.mp3" is not.
STEM_SEPARATORS = (" - ", " – ", "_-_")

_last_mb_call = 0.0


def classify_music(
    relpath: str,
    *,
    settings: Settings,
    tags: dict[str, str] | None = None,
    acoustid_lookup: AcoustidLookup | None = None,
    musicbrainz_lookup: MusicBrainzLookup | None = None,
    discogs_lookup: DiscogsLookup | None = None,
    genre_lookup: GenreLookup | None = None,
) -> MusicClassification:
    """Identify an audio file.

    Evidence is tried strongest-first: embedded tags, then an AcoustID
    fingerprint resolved through MusicBrainz, then — with nothing left but the
    filename — a verified Discogs text search. Last.fm is not part of that
    cascade; it fills in a genre once the release is known, because genre is
    the first path component under the genre-first template and is the field
    tags most often leave blank.
    """
    path = PurePosixPath(relpath)
    tags = {key.lower(): value for key, value in (tags or {}).items()}
    evidence: list[EvidenceEntry] = []
    fields = _fields_from_tags(path, tags)
    confidence = 0.0

    if fields.get("artist") and (fields.get("album") or fields.get("title")):
        confidence = 0.9 if fields.get("album") else 0.86
        evidence.append(EvidenceEntry("tags", "metadata", "embedded audio tags", confidence))
    elif settings.acoustid_key.get_secret_value() and acoustid_lookup and musicbrainz_lookup:
        acoustid = acoustid_lookup(relpath, settings)
        if acoustid and float(acoustid.get("score", 0.0)) > 0.65:
            mbid = str(acoustid.get("recording_id"))
            evidence.append(EvidenceEntry("acoustid", "recording_id", mbid, 0.8))
            mb = _rate_limited_musicbrainz_lookup(mbid, settings, musicbrainz_lookup)
            if mb:
                fields.update(_fields_from_musicbrainz(path, mb))
                confidence = 0.9
                evidence.append(EvidenceEntry("musicbrainz", "recording", mbid, 0.9))

    if confidence == 0.0 and discogs_lookup is not None:
        release = _discogs_release(path, discogs_lookup, settings)
        if release:
            fields = _fields_from_discogs(path, release)
            confidence = 0.8
            evidence.append(
                EvidenceEntry("discogs", "release", str(release.get("album")), confidence)
            )

    if confidence == 0.0:
        fields = _fallback_fields(path)
        confidence = 0.45
        evidence.append(
            EvidenceEntry("heuristic", "category", "audio extension fallback", confidence)
        )

    if genre_lookup is not None and _genre_missing(fields):
        genre = genre_lookup(
            str(fields.get("artist") or ""), str(fields.get("album") or ""), settings
        )
        if genre:
            fields["genre"] = genre
            evidence.append(EvidenceEntry("lastfm", "genre", genre, 0.7))

    clean_name = _clean_music_name(fields, path.suffix)
    fields["clean_name"] = clean_name
    fields.setdefault("genre", DEFAULT_GENRE)
    rendered = _render_if_confident(fields, confidence, settings)
    artist = str(fields.get("artist", UNKNOWN_ARTIST))
    album = str(fields.get("album", "Singles"))
    return MusicClassification(
        "music",
        clean_name,
        rendered.relpath,
        confidence,
        tuple(evidence),
        fields,
        f"album:{artist}:{album}",
        rendered.reason,
    )


def _fields_from_tags(path: PurePosixPath, tags: dict[str, str]) -> dict[str, object]:
    title = tags.get("title") or path.stem
    return {
        "artist": tags.get("artist") or tags.get("album_artist"),
        "album": tags.get("album") or "Singles",
        "title": title,
        "year": _year(tags.get("date") or tags.get("year")) or 0,
        "genre": tags.get("genre") or "General",
        "track": _track(tags.get("track") or tags.get("tracknumber")),
    }


def _fields_from_musicbrainz(path: PurePosixPath, data: dict[str, Any]) -> dict[str, object]:
    return {
        "artist": data.get("artist") or "Unknown Artist",
        "album": data.get("album") or "Singles",
        "title": data.get("title") or path.stem,
        "year": data.get("year") or 0,
        "genre": data.get("genre") or "General",
        "track": data.get("track") or 0,
    }


def _fields_from_discogs(path: PurePosixPath, data: dict[str, Any]) -> dict[str, object]:
    return {
        "artist": data.get("artist") or UNKNOWN_ARTIST,
        "album": data.get("album") or "Singles",
        "title": _title_from_stem(path.stem),
        "year": data.get("year") or 0,
        "genre": data.get("genre") or DEFAULT_GENRE,
        "track": 0,
    }


def _discogs_release(
    path: PurePosixPath, lookup: DiscogsLookup, settings: Settings
) -> dict[str, Any] | None:
    query = _searchable_stem(path.stem)
    return lookup(query, settings) if query else None


def _searchable_stem(stem: str) -> str:
    """A stem worth searching for names something beyond the track itself."""
    cleaned = stem.replace("_", " ").strip()
    if not any(separator in stem or separator in cleaned for separator in STEM_SEPARATORS):
        return ""
    return cleaned


def _title_from_stem(stem: str) -> str:
    """The part after the artist — "Radiohead - Karma Police" → "Karma Police"."""
    cleaned = stem.replace("_", " ").strip()
    for separator in (" - ", " – "):
        _, found, tail = cleaned.partition(separator)
        if found and tail.strip():
            return tail.strip()
    return cleaned


def _genre_missing(fields: dict[str, object]) -> bool:
    """Last.fm is asked only when there is a real artist and no real genre."""
    artist = str(fields.get("artist") or "")
    genre = str(fields.get("genre") or "")
    return bool(artist) and artist != UNKNOWN_ARTIST and genre in ("", DEFAULT_GENRE)


def _fallback_fields(path: PurePosixPath) -> dict[str, object]:
    return {
        "artist": "Unknown Artist",
        "album": "Singles",
        "title": path.stem,
        "year": 0,
        "genre": "General",
        "track": 0,
    }


def _clean_music_name(fields: dict[str, object], ext: str) -> str:
    track = int(fields.get("track") or 0)
    title = str(fields.get("title") or "Untitled")
    if track > 0:
        return clean_name_from_title(f"{track:02d} - {title}", ext)
    return clean_name_from_title(title, ext)


def _render_if_confident(
    fields: dict[str, object],
    confidence: float,
    settings: Settings,
) -> RenderResult:
    if confidence < settings.confidence_threshold:
        return RenderResult(None, "below confidence threshold")
    return render_destination("music", fields, library_root=settings.library_dir)


def _rate_limited_musicbrainz_lookup(
    mbid: str,
    settings: Settings,
    lookup: MusicBrainzLookup,
) -> dict[str, Any] | None:
    global _last_mb_call
    now = time.monotonic()
    wait = settings.mb_rate_limit - (now - _last_mb_call)
    if wait > 0:
        time.sleep(wait)
    _last_mb_call = time.monotonic()
    return lookup(mbid, settings)


def _year(value: str | None) -> int | None:
    if not value:
        return None
    for token in value.replace("-", " ").split():
        if token.isdigit() and len(token) == 4:
            return int(token)
    return None


def _track(value: str | None) -> int:
    if not value:
        return 0
    first = value.split("/", 1)[0]
    return int(first) if first.isdigit() else 0
