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


def test_pipeline_fingerprints_tagless_audio_and_resolves_it(tmp_path: Path, monkeypatch) -> None:
    """AcoustID + MusicBrainz were injection points no production code passed.

    The file has no embedded tags, so this is the only path that can name it.
    """
    from librairy.db import connect
    from librairy.tools import acoustid, musicbrainz

    acoustid.reset_cache()
    musicbrainz.reset_cache()
    fingerprinted: list[Path] = []
    resolved: list[str] = []

    monkeypatch.setattr(
        "librairy.tools.ffprobe.probe",
        lambda path, settings: ToolResult(True, data={"tags": {}}),  # noqa: ARG005
    )

    def fake_fpcalc(path, settings):  # noqa: ANN001, ARG001
        fingerprinted.append(path)
        return ToolResult(True, data={"duration": 355, "fingerprint": "AQADtEmi"})

    def fake_acoustid_lookup(fingerprint, duration, **kwargs):  # noqa: ANN001, ARG001
        return {"score": 0.98, "recording_id": "b1a9c0e9-d987-4042-ae91-78d6a3267d69"}

    def fake_mb(mbid, **kwargs):  # noqa: ANN001, ARG001
        resolved.append(mbid)
        return {
            "artist": "Queen",
            "album": "A Night at the Opera",
            "title": "Bohemian Rhapsody",
            "year": 1975,
            "track": 0,
        }

    monkeypatch.setattr("librairy.tools.fpcalc.fingerprint", fake_fpcalc)
    monkeypatch.setattr("librairy.tools.acoustid.lookup", fake_acoustid_lookup)
    monkeypatch.setattr("librairy.tools.musicbrainz.lookup_recording", fake_mb)

    settings = _settings(tmp_path, ACOUSTID_KEY="secret", INBOX_DIR=tmp_path)
    conn = connect(settings)
    audio = tmp_path / "track01.flac"
    audio.write_bytes(b"audio")

    result = classify_item(audio, "track01.flac", settings, conn=conn)

    assert fingerprinted, "pipeline never fingerprinted tagless audio"
    assert resolved == ["b1a9c0e9-d987-4042-ae91-78d6a3267d69"]
    assert result.fields["artist"] == "Queen"
    assert result.fields["title"] == "Bohemian Rhapsody"
    sources = [entry.source for entry in result.evidence]
    assert "acoustid" in sources
    assert "musicbrainz" in sources
    assert result.confidence >= 0.9


def test_no_acoustid_key_means_no_fingerprinting(tmp_path: Path, monkeypatch) -> None:
    """fpcalc over a whole inbox is expensive and useless without somewhere to send it."""
    from librairy.db import connect

    monkeypatch.setattr(
        "librairy.tools.ffprobe.probe",
        lambda path, settings: ToolResult(True, data={"tags": {}}),  # noqa: ARG005
    )

    def forbidden(path, settings):  # noqa: ANN001, ARG001
        raise AssertionError("fingerprinted audio without an AcoustID key")

    monkeypatch.setattr("librairy.tools.fpcalc.fingerprint", forbidden)

    settings = _settings(tmp_path, INBOX_DIR=tmp_path)
    conn = connect(settings)
    audio = tmp_path / "track02.flac"
    audio.write_bytes(b"audio")

    result = classify_item(audio, "track02.flac", settings, conn=conn)

    assert result.category == "music"
    assert "heuristic" in [entry.source for entry in result.evidence]


def test_disabled_catalog_toggle_skips_the_lookup(tmp_path: Path, monkeypatch) -> None:
    from librairy.db import connect

    monkeypatch.setattr(
        "librairy.tools.ffprobe.probe",
        lambda path, settings: ToolResult(True, data={"tags": {}}),  # noqa: ARG005
    )

    def forbidden(path, settings):  # noqa: ANN001, ARG001
        raise AssertionError("fingerprinted audio with the acoustid catalog disabled")

    monkeypatch.setattr("librairy.tools.fpcalc.fingerprint", forbidden)

    settings = _settings(tmp_path, ACOUSTID_KEY="secret", INBOX_DIR=tmp_path)
    conn = connect(settings)
    conn.execute(
        "INSERT INTO settings(key, value) VALUES ('catalog.acoustid.enabled', 'false')"
    )
    audio = tmp_path / "track03.flac"
    audio.write_bytes(b"audio")

    result = classify_item(audio, "track03.flac", settings, conn=conn)

    assert "heuristic" in [entry.source for entry in result.evidence]


def test_every_classifier_result_survives_destination_rendering() -> None:
    """analyze_items re-renders each result with dataclasses.replace(reason=...).

    AIClassification and UnknownResult had no `reason` field, so the moment an
    AI provider actually returned an answer the whole batch died with
    "unexpected keyword argument 'reason'". It stayed hidden for as long as
    every provider returned None.
    """
    import dataclasses

    from librairy.ai.orchestrator import AIClassification
    from librairy.classify import UnknownResult
    from librairy.classify.documents import ClassificationResult
    from librairy.classify.heuristics import HeuristicResult
    from librairy.classify.music import MusicClassification
    from librairy.classify.video import VideoClassification

    for result_type in (
        AIClassification,
        UnknownResult,
        ClassificationResult,
        HeuristicResult,
        MusicClassification,
        VideoClassification,
    ):
        fields = {field.name for field in dataclasses.fields(result_type)}
        assert "reason" in fields, f"{result_type.__name__} cannot carry a reason"
        assert "dest_relpath" in fields


def test_ai_result_can_be_rerendered_the_way_analyze_does(tmp_path: Path) -> None:
    import dataclasses

    from librairy.ai.orchestrator import AIClassification

    result = AIClassification("music", "song.flac", "Music/song.flac", 0.9, (), {})

    replaced = dataclasses.replace(result, dest_relpath=None, reason="below threshold")

    assert replaced.dest_relpath is None
    assert replaced.reason == "below threshold"


def test_pipeline_calls_tvmaze_for_episodes(tmp_path: Path, monkeypatch) -> None:
    from librairy.db import connect
    from librairy.tools import tvmaze

    tvmaze.reset_cache()
    seen: list[str] = []

    def fake_search(query, **kwargs):  # noqa: ANN001, ARG001
        seen.append(query)
        return {
            "name": "Breaking Bad",
            "genres": ["Drama"],
            "first_air_date": "2008-01-20",
            "episode_name": "Ozymandias",
        }

    monkeypatch.setattr("librairy.tools.tvmaze.search_show", fake_search)
    settings = _settings(tmp_path)
    conn = connect(settings)
    video = tmp_path / "breaking.bad.s05e14.mkv"
    video.write_bytes(b"video")

    result = classify_item(video, "breaking.bad.s05e14.mkv", settings, conn=conn)

    assert seen, "pipeline never called TVmaze — it needs no key, so nothing else gates it"
    assert result.clean_name == "S05E14-Ozymandias.mkv"
    assert "tvmaze" in [entry.source for entry in result.evidence]


def test_pipeline_calls_discogs_for_untagged_audio(tmp_path: Path, monkeypatch) -> None:
    from librairy.db import connect
    from librairy.tools import discogs

    discogs.reset_cache()
    seen: list[str] = []

    monkeypatch.setattr(
        "librairy.tools.ffprobe.probe",
        lambda path, settings: ToolResult(True, data={"tags": {}}),  # noqa: ARG005
    )

    def fake_search(query, **kwargs):  # noqa: ANN001, ARG001
        seen.append(query)
        return {"artist": "Radiohead", "album": "OK Computer", "year": 1997, "genre": "Rock"}

    monkeypatch.setattr("librairy.tools.discogs.search_release", fake_search)
    settings = _settings(tmp_path, DISCOGS_TOKEN="tok", INBOX_DIR=tmp_path)
    conn = connect(settings)
    audio = tmp_path / "Radiohead - Karma Police.mp3"
    audio.write_bytes(b"audio")

    result = classify_item(audio, "Radiohead - Karma Police.mp3", settings, conn=conn)

    assert seen == ["Radiohead - Karma Police"]
    assert result.fields["album"] == "OK Computer"
    assert "discogs" in [entry.source for entry in result.evidence]


def test_no_discogs_token_means_no_search(tmp_path: Path, monkeypatch) -> None:
    from librairy.db import connect

    monkeypatch.setattr(
        "librairy.tools.ffprobe.probe",
        lambda path, settings: ToolResult(True, data={"tags": {}}),  # noqa: ARG005
    )

    def forbidden(query, **kwargs):  # noqa: ANN001, ARG001
        raise AssertionError("searched Discogs without a token")

    monkeypatch.setattr("librairy.tools.discogs.search_release", forbidden)
    settings = _settings(tmp_path, INBOX_DIR=tmp_path)
    conn = connect(settings)
    audio = tmp_path / "Radiohead - Karma Police.mp3"
    audio.write_bytes(b"audio")

    result = classify_item(audio, "Radiohead - Karma Police.mp3", settings, conn=conn)

    assert "heuristic" in [entry.source for entry in result.evidence]


def test_pipeline_asks_lastfm_for_a_missing_genre(tmp_path: Path, monkeypatch) -> None:
    from librairy.db import connect
    from librairy.tools import lastfm

    lastfm.reset_cache()
    seen: list[tuple[str, str]] = []

    monkeypatch.setattr(
        "librairy.tools.ffprobe.probe",
        lambda path, settings: ToolResult(  # noqa: ARG005
            True, data={"tags": {"artist": "Slowdive", "album": "Souvlaki", "title": "Alison"}}
        ),
    )

    def fake_genre(artist, *, album="", **kwargs):  # noqa: ANN001, ARG001
        seen.append((artist, album))
        return "Shoegaze"

    monkeypatch.setattr("librairy.tools.lastfm.top_genre", fake_genre)
    settings = _settings(tmp_path, LASTFM_KEY="k", INBOX_DIR=tmp_path)
    conn = connect(settings)
    audio = tmp_path / "01 Alison.flac"
    audio.write_bytes(b"audio")

    result = classify_item(audio, "01 Alison.flac", settings, conn=conn)

    assert seen == [("Slowdive", "Souvlaki")]
    assert result.fields["genre"] == "Shoegaze"
    assert result.dest_relpath == "Music/Shoegaze/Slowdive/Souvlaki/Alison.flac"


def test_disabled_lastfm_toggle_skips_the_genre_lookup(tmp_path: Path, monkeypatch) -> None:
    from librairy.db import connect

    monkeypatch.setattr(
        "librairy.tools.ffprobe.probe",
        lambda path, settings: ToolResult(  # noqa: ARG005
            True, data={"tags": {"artist": "Slowdive", "album": "Souvlaki"}}
        ),
    )

    def forbidden(artist, **kwargs):  # noqa: ANN001, ARG001
        raise AssertionError("asked Last.fm with the catalog disabled")

    monkeypatch.setattr("librairy.tools.lastfm.top_genre", forbidden)
    settings = _settings(tmp_path, LASTFM_KEY="k", INBOX_DIR=tmp_path)
    conn = connect(settings)
    conn.execute("INSERT INTO settings(key, value) VALUES ('catalog.lastfm.enabled', 'false')")
    audio = tmp_path / "02 Machine Gun.flac"
    audio.write_bytes(b"audio")

    result = classify_item(audio, "02 Machine Gun.flac", settings, conn=conn)

    assert result.fields["genre"] == "General"


def test_every_catalog_is_an_accepted_evidence_source() -> None:
    """A catalog whose evidence `upsert_proposal` rejects aborts the batch.

    Classifier tests never persist, so a missing source stays invisible until a
    real file matches that catalog in production and the analyze run dies.
    """
    from librairy.catalogs import CATALOGS
    from librairy.proposals import VALID_EVIDENCE_SOURCES

    missing = {c.slug for c in CATALOGS} - VALID_EVIDENCE_SOURCES
    assert not missing, f"catalogs cannot record evidence: {sorted(missing)}"


def test_catalog_evidence_survives_being_written_to_a_proposal(tmp_path: Path) -> None:
    """The end of the pipeline, which the lookup tests above stop short of."""
    from librairy.db import connect
    from librairy.models import EvidenceEntry
    from librairy.proposals import upsert_proposal

    settings = _settings(tmp_path, INBOX_DIR=tmp_path)
    conn = connect(settings)
    conn.execute(
        """
        INSERT INTO items(
          id, root, relpath, size, mtime_ns, fingerprint, first_seen_at, last_seen_at
        )
        VALUES (1, 'inbox', 'song.mp3', 1, 1, 'fp', 'now', 'now')
        """
    )

    proposal_id = upsert_proposal(
        conn,
        item_id=1,
        category="music",
        clean_name="song.mp3",
        dest_relpath="Music/Rock/A/B/song.mp3",
        confidence=0.8,
        evidence=[
            EvidenceEntry("tvmaze", "show", "Breaking Bad", 0.82),
            EvidenceEntry("discogs", "release", "OK Computer", 0.8),
            EvidenceEntry("lastfm", "genre", "Shoegaze", 0.7),
        ],
    )

    assert proposal_id
