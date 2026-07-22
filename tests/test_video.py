from __future__ import annotations

from pathlib import Path

from librairy.classify.video import classify_video, parse_video_name
from librairy.config import Settings


def settings_for(tmp_path: Path, key: str = "tmdb") -> Settings:
    settings = Settings(
        LIBRARY_DIR=tmp_path / "library",
        TMDB_KEY=key,
        CONFIDENCE_THRESHOLD=0.8,
        _env_file=None,
    )
    settings.library_dir.mkdir()
    return settings


def test_parser_handles_movie_scene_names() -> None:
    parsed = parse_video_name("The.Matrix.1999.1080p.x264-GRP.mkv")

    assert parsed.title == "The Matrix"
    assert parsed.year == 1999
    assert parsed.ext == ".mkv"


def test_parser_handles_episode_names() -> None:
    parsed = parse_video_name("show.name.s02e05.720p.mkv")

    assert parsed.title == "Show Name"
    assert parsed.season == 2
    assert parsed.episode == 5


def test_recorded_tmdb_movie_fixture_produces_destination(tmp_path: Path) -> None:
    def lookup(parsed, settings):
        return {"title": "The Matrix", "release_date": "1999-03-31", "genres": [{"name": "Action"}]}

    result = classify_video(
        "The.Matrix.1999.1080p.mkv",
        settings=settings_for(tmp_path),
        tmdb_lookup=lookup,
    )

    assert result.category == "movies"
    assert result.dest_relpath == "Movies/The Matrix (1999)/The Matrix (1999).mkv"
    assert [entry.source for entry in result.evidence] == ["heuristic", "tmdb"]


def test_recorded_tmdb_episode_fixture_groups_season(tmp_path: Path) -> None:
    def lookup(parsed, settings):
        return {"name": "Example Show", "first_air_date": "2020-01-01", "genres": ["Sci-Fi"]}

    result = classify_video(
        "example.show.s02e05.mkv",
        settings=settings_for(tmp_path),
        tmdb_lookup=lookup,
    )

    assert result.category == "shows"
    assert result.dest_relpath == "Shows/Example Show/Season 02/S02E05.mkv"
    assert result.group_key == "show:Example Show:s02"


def test_missing_tmdb_key_returns_lower_confidence_heuristic(tmp_path: Path) -> None:
    result = classify_video("The.Matrix.1999.mkv", settings=settings_for(tmp_path, key=""))

    assert result.category == "movies"
    assert result.confidence < 0.8
    assert result.dest_relpath is None
    assert [entry.source for entry in result.evidence] == ["heuristic"]
