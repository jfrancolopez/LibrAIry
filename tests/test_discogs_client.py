from __future__ import annotations

import json
from pathlib import Path

from librairy.classify.music import classify_music
from librairy.config import Settings
from librairy.tools import discogs

RESULTS = {
    "results": [
        {
            "title": "Radiohead - OK Computer",
            "year": "1997",
            "genre": ["Rock"],
            "style": ["Alternative Rock"],
        }
    ]
}


class _Fake:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _opener(payload, calls: list):
    def opener(request, timeout=None):  # noqa: ANN001, ARG001
        calls.append((request.full_url, dict(request.headers)))
        return _Fake(payload)

    return opener


def settings_for(tmp_path: Path, **overrides) -> Settings:
    return Settings(
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        APPDATA_DIR=tmp_path / "appdata",
        _env_file=None,
        **overrides,
    )


def setup_function() -> None:
    discogs.reset_cache()


def test_verified_match_is_normalised() -> None:
    calls: list = []

    match = discogs.search_release(
        "Radiohead - Karma Police",
        token="tok",
        opener=_opener(RESULTS, calls),
        sleeper=lambda s: None,
    )

    assert match == {
        "artist": "Radiohead",
        "album": "OK Computer",
        "year": 1997,
        # Style is more specific than genre, so it wins.
        "genre": "Alternative Rock",
    }


def test_token_travels_in_a_header_not_the_url() -> None:
    """Secrets in query strings end up in logs and proxies."""
    calls: list = []

    discogs.search_release(
        "Radiohead - Karma Police",
        token="super-secret",
        opener=_opener(RESULTS, calls),
        sleeper=lambda s: None,
    )
    url, headers = calls[0]

    assert "super-secret" not in url
    assert headers["Authorization"] == "Discogs token=super-secret"


def test_hit_whose_artist_is_absent_from_the_query_is_rejected() -> None:
    """A text search that is not verified is a guess, and a guess is not evidence."""
    calls: list = []
    payload = {"results": [{"title": "Coldplay - Parachutes", "year": "2000"}]}

    match = discogs.search_release(
        "Radiohead - Karma Police",
        token="tok",
        opener=_opener(payload, calls),
        sleeper=lambda s: None,
    )

    assert match is None


def test_numeric_disambiguation_suffix_is_stripped() -> None:
    calls: list = []
    payload = {"results": [{"title": "Nirvana (2) - Nevermind", "year": "1991"}]}

    match = discogs.search_release(
        "Nirvana - Smells Like Teen Spirit",
        token="tok",
        opener=_opener(payload, calls),
        sleeper=lambda s: None,
    )

    assert match["artist"] == "Nirvana"


def test_no_token_makes_no_request() -> None:
    calls: list = []

    assert (
        discogs.search_release("anything", token="", opener=_opener(RESULTS, calls)) is None
    )
    assert calls == []


def test_untagged_file_with_artist_in_the_name_resolves(tmp_path: Path) -> None:
    def lookup(query, _settings):
        assert query == "Radiohead - Karma Police"
        return {"artist": "Radiohead", "album": "OK Computer", "year": 1997, "genre": "Rock"}

    result = classify_music(
        "Radiohead - Karma Police.mp3",
        settings=settings_for(tmp_path),
        discogs_lookup=lookup,
    )

    assert result.fields["artist"] == "Radiohead"
    assert result.fields["album"] == "OK Computer"
    assert result.fields["title"] == "Karma Police"
    assert result.confidence == 0.8
    assert result.dest_relpath == "Music/Rock/Radiohead/OK-Computer/Karma-Police.mp3"


def test_opaque_filename_is_never_searched(tmp_path: Path) -> None:
    """"track01.mp3" carries nothing to verify a hit against."""

    def lookup(query, _settings):
        raise AssertionError("must not search on a stem with no artist")

    result = classify_music(
        "track01.mp3", settings=settings_for(tmp_path), discogs_lookup=lookup
    )

    assert result.confidence == 0.45
    assert result.fields["artist"] == "Unknown Artist"


def test_tagged_audio_never_reaches_discogs(tmp_path: Path) -> None:
    def lookup(query, _settings):
        raise AssertionError("tags are stronger evidence; Discogs is the last resort")

    result = classify_music(
        "Radiohead - Karma Police.mp3",
        settings=settings_for(tmp_path),
        tags={"artist": "Radiohead", "album": "OK Computer", "title": "Karma Police"},
        discogs_lookup=lookup,
    )

    assert result.confidence == 0.9
