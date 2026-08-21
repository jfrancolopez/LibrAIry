from __future__ import annotations

from pathlib import Path

from librairy.classify import music
from librairy.classify.music import classify_music
from librairy.config import Settings


def settings_for(tmp_path: Path, acoustid_key: str = "key") -> Settings:
    settings = Settings(
        LIBRARY_DIR=tmp_path / "library",
        ACOUSTID_KEY=acoustid_key,
        CONFIDENCE_THRESHOLD=0.8,
        MB_RATE_LIMIT=1.0,
        _env_file=None,
    )
    settings.library_dir.mkdir()
    return settings


def test_tagged_file_classifies_from_tags_without_network(tmp_path: Path) -> None:
    result = classify_music(
        "01-track.mp3",
        settings=settings_for(tmp_path),
        tags={
            "artist": "Queen",
            "album": "A Night at the Opera",
            "title": "Death on Two Legs",
            "track": "1/12",
            "date": "1975-11-21",
            "genre": "Rock",
        },
    )

    assert result.confidence >= 0.85
    assert result.dest_relpath == "Music/Rock/Queen/A Night at the Opera/01 - Death on Two Legs.mp3"
    assert [entry.source for entry in result.evidence] == ["tags"]


def test_untagged_file_uses_acoustid_and_musicbrainz_fixtures(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(music.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(music.time, "monotonic", lambda: 100.0)

    def acoustid_lookup(relpath, settings):
        return {"score": 0.9, "recording_id": "mbid-1"}

    def mb_lookup(mbid, settings):
        return {
            "artist": "Queen",
            "album": "News of the World",
            "title": "We Are the Champions",
            "year": 1977,
            "genre": "Rock",
            "track": 11,
        }

    result = classify_music(
        "unknown.flac",
        settings=settings_for(tmp_path),
        acoustid_lookup=acoustid_lookup,
        musicbrainz_lookup=mb_lookup,
    )

    assert (
        result.dest_relpath
        == "Music/Rock/Queen/News of the World/11 - We Are the Champions.flac"
    )
    assert [entry.source for entry in result.evidence] == ["acoustid", "musicbrainz"]


def test_missing_acoustid_key_skips_fingerprinting_silently(tmp_path: Path) -> None:
    result = classify_music("unknown.mp3", settings=settings_for(tmp_path, acoustid_key=""))

    assert result.confidence < 0.8
    assert result.dest_relpath is None
    assert [entry.source for entry in result.evidence] == ["heuristic"]


def test_music_destinations_render_genre_first_by_default(tmp_path: Path) -> None:
    result = classify_music(
        "track.mp3",
        settings=settings_for(tmp_path),
        tags={"artist": "Queen", "album": "Jazz", "title": "Bicycle Race", "genre": "Rock"},
    )

    assert result.dest_relpath == "Music/Rock/Queen/Jazz/Bicycle Race.mp3"
