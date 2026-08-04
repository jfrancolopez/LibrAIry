from __future__ import annotations

import json

from librairy.tools import musicbrainz


class _Fake:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _opener(payload, calls):
    def opener(request, timeout=None):  # noqa: ANN001, ARG001
        calls.append(request)
        return _Fake(payload)

    return opener


def setup_function() -> None:
    musicbrainz.reset_cache()


RECORDING = {
    "title": "Bohemian Rhapsody",
    "artist-credit": [{"name": "Queen"}],
    "releases": [
        {"title": "Greatest Hits", "date": "1981-10-26"},
        {"title": "A Night at the Opera", "date": "1975-11-21"},
    ],
}


def test_maps_a_recording_onto_classifier_fields_and_caches() -> None:
    calls: list = []

    first = musicbrainz.lookup_recording("mb-uuid", opener=_opener(RECORDING, calls))
    musicbrainz.lookup_recording("mb-uuid", opener=_opener(RECORDING, calls))

    assert first == {
        "artist": "Queen",
        "album": "A Night at the Opera",
        "title": "Bohemian Rhapsody",
        "year": 1975,
        "track": 0,
    }
    assert len(calls) == 1, "identical MBID should hit the process cache"


def test_earliest_release_wins_over_later_compilations() -> None:
    """A track's album is the original record, not whichever hits arrived first."""
    calls: list = []

    result = musicbrainz.lookup_recording("mb-uuid", opener=_opener(RECORDING, calls))

    assert result["album"] == "A Night at the Opera"
    assert result["year"] == 1975


def test_undated_releases_never_beat_a_dated_one() -> None:
    calls: list = []
    payload = {
        "title": "Song",
        "artist-credit": [{"name": "Band"}],
        "releases": [{"title": "Unknown Pressing"}, {"title": "Debut", "date": "1990"}],
    }

    result = musicbrainz.lookup_recording("mb-uuid", opener=_opener(payload, calls))

    assert result["album"] == "Debut"
    assert result["year"] == 1990


def test_joins_collaborating_artists() -> None:
    calls: list = []
    payload = {
        "title": "Under Pressure",
        "artist-credit": [{"name": "Queen"}, {"artist": {"name": "David Bowie"}}],
        "releases": [{"title": "Hot Space", "date": "1982"}],
    }

    result = musicbrainz.lookup_recording("mb-uuid", opener=_opener(payload, calls))

    assert result["artist"] == "Queen & David Bowie"


def test_recording_with_no_releases_falls_back_to_singles() -> None:
    calls: list = []
    payload = {"title": "Demo Take", "artist-credit": [{"name": "Band"}]}

    result = musicbrainz.lookup_recording("mb-uuid", opener=_opener(payload, calls))

    assert result["album"] == "Singles"
    assert result["year"] == 0


def test_titleless_blank_and_broken_responses_return_none() -> None:
    calls: list = []

    assert musicbrainz.lookup_recording("", opener=_opener(RECORDING, calls)) is None
    assert musicbrainz.lookup_recording("x", opener=_opener({"title": ""}, calls)) is None
    musicbrainz.reset_cache()
    assert musicbrainz.lookup_recording("y", opener=_opener([], calls)) is None
    assert not calls or len(calls) <= 3


def test_network_failure_degrades_to_none() -> None:
    def broken(request, timeout=None):  # noqa: ANN001, ARG001
        raise OSError("musicbrainz unreachable")

    assert musicbrainz.lookup_recording("mb-uuid", opener=broken) is None


def test_request_identifies_the_app_as_musicbrainz_requires() -> None:
    calls: list = []

    musicbrainz.lookup_recording("mb-uuid", opener=_opener(RECORDING, calls))

    request = calls[0]
    assert "LibrAIry" in request.get_header("User-agent")
    assert "fmt=json" in request.full_url
    assert "mb-uuid" in request.full_url
