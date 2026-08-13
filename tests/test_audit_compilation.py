"""When a multi-artist folder should stay whole, and when it should not.

The load-bearing test in this file is
`test_a_dissolved_collection_never_becomes_an_album_under_every_artist`.
Everything else describes how the verdict is reached; that one describes the
structure the whole policy exists to prevent — twenty-seven artist folders
each containing an album folder named after a collection that no catalog has
heard of. That shape is worse than either alternative, because the album is
not together *and* every artist now claims a release that does not exist.
"""

from __future__ import annotations

import pytest

from librairy.audit import LibraryView
from librairy.audit_catalog import Identity
from librairy.audit_compilation import (
    CUSTOM,
    LOOSE,
    RECOGNIZED,
    classify_collection,
    library_convention,
)
from librairy.audit_music import album_groups, albums_in, is_multi_artist

COLLECTION = "Best Road Trip Disco Fever Classics"
ARTISTS = [
    "Abba", "Barry White", "Bee Gees", "Cameo", "Chic", "Commodores",
    "Diana Ross", "Donna Summer", "Gloria Gaynor", "Sylvester",
]


def view_for(files: dict[str, dict[str, str]], *, artwork: bool = False) -> LibraryView:
    return LibraryView(
        files=sorted(files),
        indexed={},
        fingerprints={},
        tags=dict(files),
        junk=[],
        artwork={relpath: artwork for relpath in files},
    )


def collection(
    *,
    artists: list[str] | None = None,
    album_artist: str = "V.A.",
    tracktotal: str | None = None,
    barcode: str = "",
    year: str = "2023",
    compilation_tag: bool = True,
    numbering: str = "run",
) -> dict[str, dict[str, str]]:
    """The real library's shape, with each evidence signal switchable."""
    artists = artists or ARTISTS
    files: dict[str, dict[str, str]] = {}
    for index, artist in enumerate(artists, start=1):
        number = {"run": index, "repeat": 1, "none": 0}[numbering]
        tags = {
            "artist": artist,
            "album": COLLECTION,
            "album_artist": album_artist,
            "date": year,
        }
        if number:
            tags["track"] = str(number)
        if tracktotal is not None:
            tags["tracktotal"] = tracktotal
        if barcode:
            tags["upc"] = barcode
        if compilation_tag:
            tags["mediatype"] = "compilation"
        files[f"Music/Pop/{artist}/{COLLECTION}/{index:02d} - Song {index}.flac"] = tags
    return files


def verdict_for(files, **kwargs):
    view = view_for(files, artwork=kwargs.pop("artwork", False))
    members = next(iter(album_groups(view, albums_in(view)).values()))
    return classify_collection(view, members, **kwargs)


MUSICBRAINZ = Identity("musicbrainz", "release", "mbid-1", COLLECTION, "Various Artists")
DISCOGS = Identity("discogs", "release", "d-1", COLLECTION, "Various Artists")


# --- what makes a collection a release ----------------------------------------


def test_a_catalog_hit_makes_it_a_recognized_compilation() -> None:
    result = verdict_for(collection(), catalogs=(MUSICBRAINZ,))

    assert result.kind == RECOGNIZED
    assert result.keeps_together


def test_a_recognized_compilation_lives_in_one_folder_not_one_per_artist() -> None:
    """The whole point. Ten artists, one destination."""
    result = verdict_for(collection(), catalogs=(MUSICBRAINZ,))

    assert result.home == f"Music/Pop/Various Artists/{COLLECTION}"
    assert result.dissolve_to == ()


def test_musicbrainz_alone_is_enough() -> None:
    assert verdict_for(collection(), catalogs=(MUSICBRAINZ,)).kind == RECOGNIZED


def test_discogs_alone_is_enough() -> None:
    assert verdict_for(collection(), catalogs=(DISCOGS,)).kind == RECOGNIZED


def test_two_catalogs_disagreeing_is_reported_rather_than_resolved() -> None:
    """Consensus is worth more than either witness, so a split has to show."""
    other = Identity("discogs", "release", "d-2", "Road Trip Classics", "Various")

    result = verdict_for(collection(), catalogs=(MUSICBRAINZ, other))

    assert result.kind == RECOGNIZED
    assert "do not agree" in result.disagreement
    assert "musicbrainz" in result.disagreement and "discogs" in result.disagreement


def test_agreeing_catalogs_raise_no_conflict() -> None:
    assert verdict_for(collection(), catalogs=(MUSICBRAINZ, DISCOGS)).disagreement == ""


def test_no_catalog_but_coherent_tags_is_a_custom_compilation() -> None:
    """The real case. The files describe one release; nobody else has heard
    of it. That is a judgment call, not a licence to take it apart."""
    result = verdict_for(collection(tracktotal="10", barcode="0602455907691"))

    assert result.kind == CUSTOM
    assert result.keeps_together
    assert result.home == f"Music/Pop/Various Artists/{COLLECTION}"


def test_tags_alone_never_claim_an_official_release() -> None:
    result = verdict_for(collection(tracktotal="10", barcode="0602455907691"))

    assert "no configured catalog recognises the release" in _summary(result)
    assert result.catalogs == ()


def test_a_complete_track_sequence_counts_as_evidence() -> None:
    result = verdict_for(collection(tracktotal="10"))

    assert any("tracks 1-10 complete" in signal for signal in result.signals)


def test_shared_embedded_artwork_counts_as_evidence_but_does_not_prove_a_release() -> None:
    result = verdict_for(collection(tracktotal="10"), artwork=True)

    assert any("same cover is embedded" in signal for signal in result.signals)
    assert result.kind == CUSTOM, "artwork is corroboration, never a catalog"


def test_repeated_track_numbers_veto_the_release_identity() -> None:
    """Mass-written tags look coherent until you count. Ten tracks all
    numbered 1 is a folder somebody filled, not a running order."""
    result = verdict_for(collection(tracktotal="10", barcode="x", numbering="repeat"))

    assert result.kind == LOOSE
    assert any("used more than once" in problem for problem in result.contradictions)


def test_a_track_total_that_disagrees_with_the_files_veto_it_too() -> None:
    result = verdict_for(collection(tracktotal="45", barcode="x"))

    assert result.kind == LOOSE
    assert any("45 tracks but 10 are here" in problem for problem in result.contradictions)


def test_a_folder_of_strangers_is_a_loose_collection() -> None:
    result = verdict_for(
        collection(album_artist="", compilation_tag=False, numbering="none", year="")
    )

    assert result.kind == LOOSE
    assert not result.keeps_together
    assert result.home is None


def test_both_catalogs_unavailable_leaves_the_evidence_honest() -> None:
    """"We could not ask" and "we asked and there was nothing" are different
    claims, and neither of them is "this is a real release"."""
    result = verdict_for(collection(tracktotal="10", barcode="x"), catalogs=())

    assert result.catalogs == ()
    assert result.kind == CUSTOM
    assert not any("musicbrainz" in signal.lower() for signal in result.signals)


# --- the rule this module exists for -------------------------------------------


def test_a_dissolved_collection_never_becomes_an_album_under_every_artist() -> None:
    """The bad structure, named and forbidden.

    Every destination is checked, not a sample: one leaked path is the whole
    problem reappearing.
    """
    result = verdict_for(
        collection(album_artist="", compilation_tag=False, numbering="none", year="")
    )

    assert result.dissolve_to, "a loose collection has to say where its tracks go"
    for _source, destination in result.dissolve_to:
        assert COLLECTION not in destination, destination


def test_a_dissolved_track_goes_to_its_own_artist() -> None:
    result = verdict_for(
        collection(album_artist="", compilation_tag=False, numbering="none", year="")
    )

    by_source = dict(result.dissolve_to)
    source = f"Music/Pop/Abba/{COLLECTION}/01 - Song 1.flac"
    assert by_source[source] == "Music/Pop/Abba/01 - Song 1.flac"


def test_a_dissolved_track_with_a_real_album_of_its_own_uses_it() -> None:
    """`originalalbum` is the track's own release; `album` is the collection
    it was filed into, which is exactly the value that must not be reused."""
    files = collection(
        album_artist="", compilation_tag=False, numbering="none", year=""
    )
    for tags in files.values():
        if tags["artist"] == "Chic":
            tags["originalalbum"] = "C'est Chic"

    result = verdict_for(files)

    destinations = {source: dest for source, dest in result.dissolve_to if "Chic" in source}
    assert all("Music/Pop/Chic/C'est Chic/" in dest for dest in destinations.values())


def test_a_collection_spread_over_two_genres_suggests_no_home() -> None:
    """Choosing between `Music/Pop` and `Music/Soul` is the owner's call."""
    files = collection()
    moved = {}
    for relpath, tags in files.items():
        target = relpath.replace("Music/Pop/Abba", "Music/Soul/Abba")
        moved[target] = tags

    result = verdict_for(moved, catalogs=(MUSICBRAINZ,))

    assert result.home is None


# --- reading the library's own convention --------------------------------------


@pytest.mark.parametrize("folder", ["Various Artists", "Compilations", "Various"])
def test_an_existing_compilation_folder_is_reused_rather_than_replaced(folder: str) -> None:
    files = collection()
    files[f"Music/Pop/{folder}/Some Other Set/01 - Track.flac"] = {"album": "Some Other Set"}
    view = view_for(files)

    assert library_convention(view) == folder


def test_a_library_with_no_convention_is_offered_the_default() -> None:
    assert library_convention(view_for(collection())) == ""
    assert verdict_for(collection(), catalogs=(MUSICBRAINZ,)).home.startswith(
        "Music/Pop/Various Artists/"
    )


def test_the_libraries_own_word_wins_over_the_default() -> None:
    files = collection()
    files["Music/Pop/Compilations/Some Other Set/01 - Track.flac"] = {"album": "Some Other Set"}
    view = view_for(files)
    members = album_groups(view, albums_in(view))[COLLECTION.casefold().replace(" ", "")]

    result = classify_collection(
        view, members, catalogs=(), convention=library_convention(view)
    )

    assert result.home == f"Music/Pop/Compilations/{COLLECTION}"


# --- telling a collection from an album ----------------------------------------


def test_one_artist_in_two_folders_is_not_a_collection() -> None:
    files = {
        f"Music/Pop/Queen/A Night at the Opera/0{n} - Song.flac": {
            "artist": "Queen", "album_artist": "Queen",
            "album": "A Night at the Opera", "track": str(n),
        }
        for n in (1, 2)
    }
    files["Music/Pop/Queen/Opera/11 - Bohemian.flac"] = {
        "artist": "Queen", "album_artist": "Queen",
        "album": "A Night at the Opera", "track": "11",
    }
    view = view_for(files)
    members = next(iter(album_groups(view, albums_in(view)).values()))

    assert is_multi_artist(view, members) is False


def test_one_artist_mistagged_as_various_is_still_one_artist() -> None:
    """A Queen album whose `album_artist` says `V.A.` must not be answered
    with `Various Artists/`. The performer tags outrank the flag."""
    files = {
        f"Music/Pop/Queen/{name}/0{n} - Song.flac": {
            "artist": "Queen", "album_artist": "V.A.",
            "album": "A Night at the Opera", "track": str(n),
        }
        for n, name in ((1, "A Night at the Opera"), (2, "Opera"))
    }
    view = view_for(files)
    members = next(iter(album_groups(view, albums_in(view)).values()))

    assert is_multi_artist(view, members) is False


def test_a_compilation_tag_decides_it_when_no_performer_is_named() -> None:
    files = {
        f"Music/Pop/Unknown/{name}/0{n} - Song.flac": {
            "album_artist": "V.A.", "album": "Party Tape", "track": str(n),
        }
        for n, name in ((1, "Party Tape"), (2, "Party Tape 2"))
    }
    view = view_for(files)
    members = next(iter(album_groups(view, albums_in(view)).values()))

    assert is_multi_artist(view, members) is True


def _summary(result) -> str:
    from librairy.audit_compilation import summarize

    return summarize(result)
