"""The analyze pipeline must actually CALL the metadata lookups.

These guard a bug class that mocked unit tests cannot catch: the classifiers
accept injected lookups, and for a long time the pipeline simply never passed
them, so TMDB/tags were dead in production while every test stayed green.
"""

from __future__ import annotations

from pathlib import Path

from librairy.classify import classify_item
from librairy.config import Settings
from librairy.tools.common import ToolResult


def _settings(tmp_path: Path, **kw) -> Settings:
    return Settings(
        APPDATA_DIR=tmp_path / "appdata",
        LIBRARY_DIR=tmp_path / "lib",
        _env_file=None,
        **kw,
    )


def test_pipeline_reads_embedded_audio_tags(tmp_path: Path, monkeypatch) -> None:
    calls: list[Path] = []

    def fake_probe(path, settings):  # noqa: ANN001, ARG001
        calls.append(path)
        return ToolResult(
            True,
            data={
                "tags": {
                    "artist": "Queen",
                    "album": "A Night at the Opera",
                    "title": "Bohemian Rhapsody",
                    "genre": "Rock",
                    "date": "1975",
                }
            },
        )

    monkeypatch.setattr("librairy.tools.ffprobe.probe", fake_probe)
    audio = tmp_path / "unknown-track-01.mp3"
    audio.write_bytes(b"audio")

    result = classify_item(audio, "unknown-track-01.mp3", _settings(tmp_path))

    assert calls, "pipeline never probed the file for embedded tags"
    assert result.category == "music"
    assert result.fields["artist"] == "Queen"
    assert result.fields["title"] == "Bohemian Rhapsody"
    assert result.confidence >= 0.9
    assert "tags" in [entry.source for entry in result.evidence]


def test_unreadable_tags_degrade_to_heuristics(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "librairy.tools.ffprobe.probe",
        lambda path, settings: ToolResult(False, error="no ffprobe"),  # noqa: ARG005
    )
    audio = tmp_path / "track.mp3"
    audio.write_bytes(b"audio")

    result = classify_item(audio, "track.mp3", _settings(tmp_path))

    assert result.category == "music"
    assert "heuristic" in [entry.source for entry in result.evidence]


def test_pipeline_calls_tmdb_when_a_key_is_configured(tmp_path: Path, monkeypatch) -> None:
    from librairy.db import connect

    seen: list[str] = []

    def fake_search(query, **kwargs):  # noqa: ANN001, ARG001
        seen.append(query)
        return {"title": "Blade Runner", "release_date": "1982-06-25"}

    monkeypatch.setattr("librairy.tools.tmdb.search", fake_search)
    settings = _settings(tmp_path, TMDB_KEY="secret")
    conn = connect(settings)
    video = tmp_path / "Blade.Runner.1982.1080p.mkv"
    video.write_bytes(b"video")

    result = classify_item(video, "Blade.Runner.1982.1080p.mkv", settings, conn=conn)

    assert seen, "pipeline never called TMDB despite a configured key"
    assert result.category == "movies"
    assert "tmdb" in [entry.source for entry in result.evidence]
