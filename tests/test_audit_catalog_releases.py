"""Asking a catalog about a release that has no artist to search by.

A compilation's `album_artist` is `V.A.`, and the old tier skipped every one
of them because searching for an artist called "V.A." returns whatever happens
to be named that. Twenty-seven folders of the real library went unchecked. The
questions that actually identify a release are the barcode and the title, and
the title needs verifying because MusicBrainz's relevance score is not a match
threshold — asked for *Best Road Trip Disco Fever Classics* it returns *Road
Trip Classics* at full score, and accepting that would have LibrAIry declaring
a custom collection an official release.
"""

from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path

import pytest

from librairy.audit import LibraryView
from librairy.audit_catalog import (
    CatalogRun,
    Identity,
    reconcile_collections,
)
from librairy.config import Settings
from librairy.db import connect
from librairy.tools import discogs, musicbrainz

COLLECTION = "Best Road Trip Disco Fever Classics"


@pytest.fixture(autouse=True)
def _clean_caches():
    musicbrainz.reset_cache()
    discogs.reset_cache()
    yield
    musicbrainz.reset_cache()
    discogs.reset_cache()


class Response(BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def answering(payload: dict, *, record: list | None = None):
    def opener(request, timeout=None):  # noqa: ARG001
        if record is not None:
            record.append(request.full_url)
        return Response(json.dumps(payload).encode())

    return opener


def no_sleep(_seconds):
    return None


# --- MusicBrainz ---------------------------------------------------------------


def test_a_barcode_is_asked_first_and_needs_no_verification() -> None:
    """A UPC is the closest thing a release has to a primary key."""
    urls: list[str] = []
    payload = {"releases": [{"id": "mbid-1", "title": "Whatever It Is Called"}]}

    found = musicbrainz.search_compilation(
        COLLECTION,
        barcode="0602455907691",
        opener=answering(payload, record=urls),
        sleeper=no_sleep,
    )

    assert found["id"] == "mbid-1"
    assert len(urls) == 1, "a barcode hit should not also run a title search"
    assert "barcode" in urls[0]


def test_a_title_search_rejects_a_near_miss() -> None:
    """The real trap: MusicBrainz scores `Road Trip Classics` at 100 for this
    query, and taking it would invent an official identity for a collection
    that has none."""
    payload = {"releases": [{"id": "mbid-2", "title": "Road Trip Classics"}]}

    found = musicbrainz.search_compilation(
        COLLECTION, opener=answering(payload), sleeper=no_sleep
    )

    assert found is None


def test_a_title_search_accepts_an_exact_match_case_aside() -> None:
    payload = {"releases": [{"id": "mbid-3", "title": COLLECTION.upper()}]}

    found = musicbrainz.search_compilation(
        COLLECTION, opener=answering(payload), sleeper=no_sleep
    )

    assert found["id"] == "mbid-3"


def test_a_release_with_the_wrong_number_of_tracks_is_not_this_release() -> None:
    payload = {"releases": [{"id": "mbid-4", "title": COLLECTION, "track-count": 12}]}

    found = musicbrainz.search_compilation(
        COLLECTION, track_count=45, opener=answering(payload), sleeper=no_sleep
    )

    assert found is None


def test_no_track_count_on_either_side_is_not_a_disagreement() -> None:
    payload = {"releases": [{"id": "mbid-5", "title": COLLECTION}]}

    found = musicbrainz.search_compilation(
        COLLECTION, track_count=45, opener=answering(payload), sleeper=no_sleep
    )

    assert found["id"] == "mbid-5"


def test_the_artist_is_never_part_of_the_query() -> None:
    """The bug this whole path exists for. `V.A.` is not a performer."""
    urls: list[str] = []

    musicbrainz.search_compilation(
        COLLECTION, opener=answering({"releases": []}, record=urls), sleeper=no_sleep
    )

    assert urls and all("artist" not in url for url in urls)


# --- Discogs -------------------------------------------------------------------


def test_discogs_is_asked_by_barcode_then_title() -> None:
    urls: list[str] = []
    payload = {"results": [{"id": 771, "title": f"Various - {COLLECTION}"}]}

    found = discogs.search_compilation(
        COLLECTION, token="t", opener=answering(payload, record=urls), sleeper=no_sleep
    )

    assert found["id"] == "771"
    assert found["title"] == COLLECTION
    assert "release_title" in urls[0]


def test_discogs_accepts_a_release_with_no_artist_prefix() -> None:
    """A compilation's Discogs title is often the release name alone, and
    `_first_verified`'s "the artist must appear in the query" rule cannot be
    reused for a release whose artist is thirty people."""
    payload = {"results": [{"id": 42, "title": COLLECTION}]}

    found = discogs.search_compilation(
        COLLECTION, token="t", opener=answering(payload), sleeper=no_sleep
    )

    assert found["id"] == "42"


def test_discogs_rejects_a_different_release() -> None:
    payload = {"results": [{"id": 9, "title": "Various - Road Trip Classics"}]}

    assert (
        discogs.search_compilation(
            COLLECTION, token="t", opener=answering(payload), sleeper=no_sleep
        )
        is None
    )


def test_discogs_without_a_token_asks_nothing() -> None:
    urls: list[str] = []

    result = discogs.search_compilation(
        COLLECTION, token="", opener=answering({}, record=urls), sleeper=no_sleep
    )

    assert result is None
    assert urls == []


# --- the tier ------------------------------------------------------------------


def library(tmp_path: Path):
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        AUTH_REQUIRED=False,
        _env_file=None,
    )
    settings.library_dir.mkdir(parents=True, exist_ok=True)
    return connect(settings), settings


def view_for(artists=("Abba", "Bee Gees", "Chic")) -> LibraryView:
    files = {
        f"Music/Pop/{artist}/{COLLECTION}/0{index} - Song.flac": {
            "artist": artist,
            "album": COLLECTION,
            "album_artist": "V.A.",
            "track": str(index),
            "tracktotal": str(len(artists)),
            "upc": "0602455907691",
        }
        for index, artist in enumerate(artists, start=1)
    }
    return LibraryView(
        files=sorted(files),
        indexed={},
        fingerprints={},
        tags=dict(files),
        junk=[],
        artwork={},
    )


def groups_of(view):
    from librairy.audit_music import album_groups, albums_in

    return album_groups(view, albums_in(view))


def test_every_configured_catalog_is_asked_not_just_the_first(tmp_path: Path) -> None:
    conn, _ = library(tmp_path)
    view = view_for()
    asked: list[str] = []

    def lookup(provider):
        def inner(title, barcode, tracks):  # noqa: ARG001
            asked.append(provider)
            return Identity(provider, "release", f"{provider}-1", COLLECTION, "Various Artists")

        return inner

    run = CatalogRun()
    reconcile_collections(
        conn,
        view,
        groups_of(view),
        {"musicbrainz": lookup("musicbrainz"), "discogs": lookup("discogs")},
        run=run,
    )

    assert sorted(asked) == ["discogs", "musicbrainz"]
    assert run.asked == 2
    assert run.matched == 2


def test_the_lookup_is_given_the_barcode_and_the_track_count(tmp_path: Path) -> None:
    conn, _ = library(tmp_path)
    view = view_for()
    seen: list[tuple] = []

    def lookup(title, barcode, tracks):
        seen.append((title, barcode, tracks))
        return None

    reconcile_collections(conn, view, groups_of(view), {"musicbrainz": lookup})

    assert seen == [(COLLECTION, "0602455907691", 3)]


def test_a_second_run_reads_the_remembered_answer(tmp_path: Path) -> None:
    conn, _ = library(tmp_path)
    view = view_for()
    calls = []

    def lookup(title, barcode, tracks):  # noqa: ARG001
        calls.append(1)
        return Identity("musicbrainz", "release", "mbid-1", COLLECTION, "Various Artists")

    reconcile_collections(conn, view, groups_of(view), {"musicbrainz": lookup})
    second = CatalogRun()
    reconcile_collections(conn, view, groups_of(view), {"musicbrainz": lookup}, run=second)

    assert len(calls) == 1
    assert second.asked == 0
    assert second.cached == 1


def test_a_remembered_miss_is_not_re_asked(tmp_path: Path) -> None:
    """Re-asking a question that had no answer is the expensive half of a
    rate limit."""
    conn, _ = library(tmp_path)
    view = view_for()
    calls = []

    def lookup(title, barcode, tracks):  # noqa: ARG001
        calls.append(1)
        return None

    reconcile_collections(conn, view, groups_of(view), {"musicbrainz": lookup})
    reconcile_collections(conn, view, groups_of(view), {"musicbrainz": lookup})

    assert len(calls) == 1


def test_a_catalog_that_raises_does_not_abort_the_audit(tmp_path: Path) -> None:
    conn, _ = library(tmp_path)
    view = view_for()

    def broken(title, barcode, tracks):  # noqa: ARG001
        raise RuntimeError("provider down")

    def working(title, barcode, tracks):  # noqa: ARG001
        return Identity("discogs", "release", "d-1", COLLECTION, "Various Artists")

    run = CatalogRun()
    findings = reconcile_collections(
        conn, view, groups_of(view), {"musicbrainz": broken, "discogs": working}, run=run
    )

    assert len(findings) == 1
    assert findings[0].kind == "collection-recognized"
    assert run.failed == 1
    assert run.unavailable == "did not answer"


def test_with_no_catalogs_at_all_the_collection_still_gets_a_verdict(tmp_path: Path) -> None:
    """"No catalog is configured" is not a reason to say nothing about a
    folder of twenty-seven artists."""
    conn, _ = library(tmp_path)
    view = view_for()

    findings = reconcile_collections(conn, view, groups_of(view), {})

    assert [finding.kind for finding in findings] == ["collection-custom"]


def test_a_single_artist_album_in_two_folders_is_not_asked_about(tmp_path: Path) -> None:
    conn, _ = library(tmp_path)
    files = {
        f"Music/Pop/Queen/{name}/0{n} - Song.flac": {
            "artist": "Queen", "album": "A Night at the Opera", "track": str(n),
        }
        for n, name in ((1, "A Night at the Opera"), (2, "Opera"))
    }
    view = LibraryView(
        files=sorted(files), indexed={}, fingerprints={},
        tags=dict(files), junk=[], artwork={},
    )
    asked = []

    findings = reconcile_collections(
        conn, view, groups_of(view), {"musicbrainz": lambda *a: asked.append(a)}
    )

    assert findings == []
    assert asked == []
