from __future__ import annotations

import json
from pathlib import Path

from librairy.classify.music import classify_music
from librairy.config import Settings
from librairy.tools import lastfm


class _Fake:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _opener(by_method: dict[str, object], calls: list[str]):
    def opener(request, timeout=None):  # noqa: ANN001, ARG001
        calls.append(request.full_url)
        for method, payload in by_method.items():
            if f"method={method}" in request.full_url:
                if payload is None:
                    raise OSError("boom")
                return _Fake(payload)
        raise AssertionError(f"unexpected request: {request.full_url}")

    return opener


def _album_tags(*tags: tuple[str, int]) -> dict:
    return {"album": {"tags": {"tag": [{"name": n, "count": c} for n, c in tags]}}}


def _artist_tags(*tags: tuple[str, int]) -> dict:
    return {"toptags": {"tag": [{"name": n, "count": c} for n, c in tags]}}


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
    lastfm.reset_cache()


def test_highest_counted_tag_wins() -> None:
    calls: list[str] = []
    payload = _album_tags(("shoegaze", 90), ("rock", 40))

    genre = lastfm.top_genre(
        "Slowdive",
        album="Souvlaki",
        api_key="k",
        opener=_opener({"album.getinfo": payload}, calls),
        sleeper=lambda s: None,
    )

    assert genre == "Shoegaze"
    assert "method=album.getinfo" in calls[0]


def test_listener_tags_and_decades_are_not_genres() -> None:
    calls: list[str] = []
    payload = _album_tags(
        ("seen live", 500),
        ("albums i own", 400),
        ("90s", 300),
        ("Slowdive", 200),
        ("dream pop", 100),
    )

    genre = lastfm.top_genre(
        "Slowdive",
        album="Souvlaki",
        api_key="k",
        opener=_opener({"album.getinfo": payload}, calls),
        sleeper=lambda s: None,
    )

    assert genre == "Dream Pop"


def test_thinly_tagged_album_falls_back_to_the_artist() -> None:
    calls: list[str] = []
    openers = {
        # Below MIN_TAG_COUNT: one person's opinion is not a genre.
        "album.getinfo": _album_tags(("bandcamp find", 2)),
        "artist.gettoptags": _artist_tags(("post-punk", 800)),
    }

    genre = lastfm.top_genre(
        "Some Band",
        album="Some Album",
        api_key="k",
        opener=_opener(openers, calls),
        sleeper=lambda s: None,
    )

    assert genre == "Post-Punk"
    assert len(calls) == 2


def test_network_failure_returns_none() -> None:
    calls: list[str] = []
    openers = {"album.getinfo": None, "artist.gettoptags": None}

    genre = lastfm.top_genre(
        "Anyone",
        album="Anything",
        api_key="k",
        opener=_opener(openers, calls),
        sleeper=lambda s: None,
    )

    assert genre is None


def test_no_key_makes_no_request() -> None:
    calls: list[str] = []

    assert lastfm.top_genre("Slowdive", api_key="", opener=_opener({}, calls)) is None
    assert calls == []


def test_repeated_lookups_hit_the_cache_once() -> None:
    calls: list[str] = []
    opener = _opener({"album.getinfo": _album_tags(("jazz", 50))}, calls)

    for artist, album in (("Miles Davis", "Kind of Blue"), ("miles davis", "kind of blue")):
        lastfm.top_genre(artist, album=album, api_key="k", opener=opener, sleeper=lambda s: None)

    assert len(calls) == 1


def test_genre_fills_the_first_path_component(tmp_path: Path) -> None:
    """Without this, a fully identified album still lands in Music/General/."""

    def genre_lookup(artist, album, _settings):
        assert (artist, album) == ("Slowdive", "Souvlaki")
        return "Shoegaze"

    result = classify_music(
        "01 - Alison.flac",
        settings=settings_for(tmp_path),
        tags={"artist": "Slowdive", "album": "Souvlaki", "title": "Alison", "track": "1"},
        genre_lookup=genre_lookup,
    )

    assert result.fields["genre"] == "Shoegaze"
    assert result.dest_relpath == "Music/Shoegaze/Slowdive/Souvlaki/01 - Alison.flac"
    assert ("lastfm", "genre") in [(e.source, e.field) for e in result.evidence]


def test_a_genre_from_the_file_is_not_second_guessed(tmp_path: Path) -> None:
    def genre_lookup(artist, album, _settings):
        raise AssertionError("the file already says what it is")

    result = classify_music(
        "01 - Alison.flac",
        settings=settings_for(tmp_path),
        tags={"artist": "Slowdive", "album": "Souvlaki", "genre": "Dream Pop"},
        genre_lookup=genre_lookup,
    )

    assert result.fields["genre"] == "Dream Pop"


def test_unidentified_audio_is_not_looked_up(tmp_path: Path) -> None:
    """There is no artist to ask about, so the request would be pure cost."""

    def genre_lookup(artist, album, _settings):
        raise AssertionError("nothing to look up")

    result = classify_music(
        "track01.mp3", settings=settings_for(tmp_path), genre_lookup=genre_lookup
    )

    assert result.fields["genre"] == "General"


def test_album_tags_without_counts_are_taken_in_order() -> None:
    """album.getinfo returns relevance-ordered tags and no counts at all.

    A popularity floor applied to those would reject every one of them and
    silently fall through to the vaguer artist-level answer.
    """
    calls: list[str] = []
    payload = {
        "album": {"tags": {"tag": [{"name": "seen live"}, {"name": "slowcore"}]}}
    }

    genre = lastfm.top_genre(
        "Codeine",
        album="Frigid Stars",
        api_key="k",
        opener=_opener({"album.getinfo": payload}, calls),
        sleeper=lambda s: None,
    )

    assert genre == "Slowcore"
    assert len(calls) == 1, "should not have needed the artist fallback"
