"""Music reconciliation, against libraries built to look like real ones.

The detector that matters most here is the split album, because the real
library is one: forty-five tracks of a single compilation filed as twenty-seven
artist folders. The test that matters most is that it produces *one* row. A
reconciliation pass that turns one problem into twenty-seven is not a stronger
audit, it is an audit nobody will run twice.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from librairy import audit_music
from librairy.audit import EXECUTABLE_KINDS, KINDS, LibraryView, detect


def view_for(files: dict[str, dict[str, str] | None], *, indexed: bool = True) -> LibraryView:
    """A library described by path and tags, with nothing on disk.

    The detectors are pure functions of a gathered view, which is what makes
    a twenty-seven-folder compilation something you can write down in a test.
    """
    return LibraryView(
        files=sorted(files),
        indexed={} if not indexed else {relpath: None for relpath in files},  # type: ignore[dict-item]
        fingerprints={},
        tags={relpath: tags for relpath, tags in files.items() if tags is not None},
        junk=[],
    )


def track(artist: str, album: str, number: int, title: str, *, album_artist=None, branch="Pop"):
    """One track, filed the way this library files them."""
    return (
        f"Music/{branch}/{artist}/{album}/{number:02d} - {title}.flac",
        {
            "artist": artist,
            "album_artist": album_artist or artist,
            "album": album,
            "track": str(number),
        },
    )


def kinds(findings) -> list[str]:
    return sorted(finding.kind for finding in findings)


def only(findings, kind):
    matching = [finding for finding in findings if finding.kind == kind]
    assert len(matching) == 1, f"expected one {kind}, got {len(matching)}: {matching}"
    return matching[0]


# --- the compilation ----------------------------------------------------------


COMPILATION = "Best Road Trip Disco Fever Classics"


def real_compilation() -> dict:
    """The shape of the actual library: one album, one folder per artist."""
    artists = [
        "A Taste Of Honey", "Abba", "Barry White", "Bee Gees", "Cameo",
        "Commodores", "DeBarge", "Diana Ross", "Donna Summer", "Gloria Gaynor",
        "JAMES BROWN", "Kool & The Gang", "Lionel Richie", "Lipps Inc.",
        "Marvin Gaye", "Parliament", "Peaches & Herb", "Rose Royce",
        "Rupert Holmes", "Stevie Wonder", "Sylvester", "The Brothers Johnson",
        "The Gap Band", "Thelma Houston", "Walter Murphy", "Yvonne Elliman",
        "Chic",
    ]
    files = {}
    number = 1
    for artist in artists:
        for _ in range(2 if artist in {"Abba", "Bee Gees"} else 1):
            relpath, tags = track(artist, COMPILATION, number, f"Song {number}")
            tags["album_artist"] = "V.A."
            files[relpath] = tags
            number += 1
    return files


def test_one_compilation_in_twenty_seven_folders_is_one_finding() -> None:
    findings = audit_music.detect(view_for(real_compilation()))

    split = only(findings, "split-album")
    assert "27 artist folders" in split.summary
    assert "29 tracks" in split.summary


def test_the_split_finding_names_every_folder_it_speaks_for() -> None:
    """Why has to be able to list them; `detect` has to be able to skip them."""
    split = only(audit_music.detect(view_for(real_compilation())), "split-album")

    folders = {
        entry.detail
        for entry in split.evidence
        if entry.source == "filesystem" and entry.field == "folder"
    }
    assert len(folders) == 27
    assert f"Music/Pop/Abba/{COMPILATION}" in folders


def test_the_split_finding_cites_the_unbroken_track_run() -> None:
    """Twenty-seven albums that happen to share a name would not number 1-29."""
    split = only(audit_music.detect(view_for(real_compilation())), "split-album")

    runs = [entry.detail for entry in split.evidence if entry.field == "track numbers"]
    assert runs == ["1-29, complete"]


def test_a_compilation_does_not_become_twenty_seven_artist_findings() -> None:
    """The trap the brief names: 27 identities are not 27 wrong folders."""
    findings = audit_music.detect(view_for(real_compilation()))

    assert kinds(findings) == ["split-album"]


def test_the_split_finding_suggests_nothing_when_the_library_has_no_convention(
) -> None:
    """Inventing `Various Artists/` for a library that has never used one is
    imposing a convention, not reading it."""
    split = only(audit_music.detect(view_for(real_compilation())), "split-album")

    assert split.dest_relpath is None


def test_a_split_album_by_one_artist_does_suggest_where_it_belongs() -> None:
    files = dict(
        [
            track("Queen", "A Night at the Opera", 1, "Death on Two Legs"),
            track("Queen", "A Night at the Opera", 2, "Lazing"),
        ]
    )
    files.update(
        dict(
            [
                ("Music/Pop/Queen/Opera/11 - Bohemian Rhapsody.flac",
                 {"artist": "Queen", "album_artist": "Queen",
                  "album": "A Night at the Opera", "track": "11"}),
                ("Music/Pop/Queen/Opera/12 - God Save.flac",
                 {"artist": "Queen", "album_artist": "Queen",
                  "album": "A Night at the Opera", "track": "12"}),
            ]
        )
    )

    split = only(audit_music.detect(view_for(files)), "split-album")

    assert split.dest_relpath == "Music/Pop/Queen/A Night at the Opera"


def test_a_split_album_across_two_sections_suggests_nothing() -> None:
    """A suggestion that moves music between genre folders would be choosing
    a taxonomy for the user. It declines."""
    files = dict(
        [
            track("Queen", "A Night at the Opera", 1, "One", branch="Pop"),
            track("Queen", "A Night at the Opera", 2, "Two", branch="Pop"),
            track("Queen", "A Night at the Opera", 3, "Three", branch="Rock"),
            track("Queen", "A Night at the Opera", 4, "Four", branch="Rock"),
        ]
    )

    split = only(audit_music.detect(view_for(files)), "split-album")

    assert split.dest_relpath is None


def test_one_album_in_one_folder_is_silent() -> None:
    files = dict(
        track("Abba", "Arrival", number, f"Song {number}") for number in range(1, 11)
    )

    assert audit_music.detect(view_for(files)) == []


# --- the artist in two places -------------------------------------------------


def test_an_artist_under_two_sections_is_reported_once() -> None:
    files = dict(
        [
            track("Queen", "A Night at the Opera", 1, "One", branch="Rock"),
            track("Queen", "A Night at the Opera", 2, "Two", branch="Rock"),
            track("Queen", "News of the World", 1, "Three", branch="Rock"),
            track("Queen", "News of the World", 2, "Four", branch="Rock"),
            track("Queen", "Hot Space", 1, "Five", branch="Pop"),
            track("Queen", "Hot Space", 2, "Six", branch="Pop"),
        ]
    )

    finding = only(audit_music.detect(view_for(files)), "artist-split")

    assert "2 different sections" in finding.summary
    assert finding.relpath == "Music/Pop/Queen/Hot Space", "it points at the stray"


def test_the_library_majority_decides_which_place_is_home() -> None:
    files = dict(
        [
            track("Queen", "Opera", 1, "One", branch="Rock"),
            track("Queen", "Opera", 2, "Two", branch="Rock"),
            track("Queen", "News", 1, "Three", branch="Rock"),
            track("Queen", "News", 2, "Four", branch="Rock"),
            track("Queen", "Hot Space", 1, "Five", branch="Pop"),
            track("Queen", "Hot Space", 2, "Six", branch="Pop"),
        ]
    )

    finding = only(audit_music.detect(view_for(files)), "artist-split")

    homes = [entry.detail for entry in finding.evidence if entry.field == "mostly under"]
    assert homes == ["Music/Rock"]


def test_a_catalog_genre_is_never_the_reason_to_move_anything() -> None:
    """Abba tagged Disco, filed under Pop, and consistent with itself.

    The brief is explicit: `MusicBrainz genre = Disco` must not mean
    `Music/Pop -> Music/Disco`. Nothing in this module reads the genre tag,
    and this is the test that says so.
    """
    files = dict(
        track("Abba", "Arrival", number, f"Song {number}") for number in range(1, 11)
    )
    for tags in files.values():
        tags["genre"] = "Disco"

    assert audit_music.detect(view_for(files)) == []
    assert "genre" not in Path(audit_music.__file__).read_text(encoding="utf-8").split(
        "COMPILATION_ARTISTS"
    )[0]


# --- folder against tags ------------------------------------------------------


def test_a_folder_named_unlike_its_tags_is_one_finding_not_forty() -> None:
    files = {}
    for number in range(1, 41):
        relpath = f"Music/Pop/Abba/Arival/{number:02d} - Song {number}.flac"
        files[relpath] = {
            "artist": "Abba", "album_artist": "Abba", "album": "Arrival",
            "track": str(number),
        }

    finding = only(audit_music.detect(view_for(files)), "album-name-mismatch")

    assert "'Arival'" in finding.summary
    assert "'Arrival'" in finding.summary
    assert finding.relpath == "Music/Pop/Abba/Arival"


def test_punctuation_alone_is_not_a_disagreement() -> None:
    """`Risque` and `Risqué` differ; `Rock & Roll` and `Rock and Roll` do not
    differ in a way worth a row."""
    files = {}
    for number in range(1, 6):
        relpath = f"Music/Pop/Chic/C'est Chic/{number:02d} - Song {number}.flac"
        files[relpath] = {
            "artist": "Chic", "album_artist": "Chic", "album": "Cest Chic",
            "track": str(number),
        }

    assert not [f for f in audit_music.detect(view_for(files)) if f.kind == "album-name-mismatch"]


# --- numbering and names ------------------------------------------------------


def test_a_gap_in_the_middle_of_an_album_is_reported() -> None:
    files = dict(
        track("Abba", "Arrival", number, f"Song {number}")
        for number in (1, 2, 3, 5, 6, 7, 8)
    )

    finding = only(audit_music.detect(view_for(files)), "track-numbering")

    assert "no track 4" in finding.summary


def test_an_album_that_simply_has_seven_tracks_is_silent() -> None:
    files = dict(
        track("Abba", "Arrival", number, f"Song {number}") for number in range(1, 8)
    )

    assert audit_music.detect(view_for(files)) == []


def test_a_split_album_does_not_also_report_its_numbering_holes() -> None:
    """One problem, one finding. Twenty-seven folders each missing forty of
    forty-five track numbers is the split album seen twenty-seven times."""
    findings = audit_music.detect(view_for(real_compilation()))

    assert "track-numbering" not in kinds(findings)


def test_one_file_named_unlike_its_neighbours_is_reported() -> None:
    files = dict(
        track("Abba", "Arrival", number, f"Song {number}") for number in range(1, 12)
    )
    files["Music/Pop/Abba/Arrival/track_final2.flac"] = {
        "artist": "Abba", "album_artist": "Abba", "album": "Arrival",
    }

    finding = only(audit_music.detect(view_for(files)), "naming-outlier")

    assert finding.relpath == "Music/Pop/Abba/Arrival/track_final2.flac"
    assert "11 tracks" in finding.summary


def test_a_folder_with_its_own_consistent_style_is_left_alone() -> None:
    """The convention is read from the folder, never imposed on it."""
    files = {}
    for number in range(1, 9):
        relpath = f"Music/Pop/Bootlegs/Live 1977/side-a-{number}.flac"
        files[relpath] = {"artist": "Band", "album_artist": "Band", "album": "Live 1977"}

    assert not [f for f in audit_music.detect(view_for(files)) if f.kind == "naming-outlier"]


def test_loose_tracks_beside_album_folders_are_reported_once() -> None:
    files = dict(
        track("Abba", "Arrival", number, f"Song {number}") for number in range(1, 6)
    )
    for number in (1, 2, 3):
        files[f"Music/Pop/Abba/{number:02d} - Stray {number}.flac"] = {
            "artist": "Abba", "album_artist": "Abba", "album": "Singles",
        }

    finding = only(audit_music.detect(view_for(files)), "loose-tracks")

    assert "3 track(s)" in finding.summary
    assert finding.relpath == "Music/Pop/Abba"


def test_an_artist_whose_tracks_are_all_loose_is_consistent() -> None:
    files = {
        f"Music/Pop/Abba/{number:02d} - Song {number}.flac": {
            "artist": "Abba", "album_artist": "Abba", "album": "Singles",
        }
        for number in range(1, 6)
    }

    assert not [f for f in audit_music.detect(view_for(files)) if f.kind == "loose-tracks"]


# --- the safety rail ----------------------------------------------------------


@pytest.mark.parametrize(
    "kind",
    ["split-album", "artist-split", "album-name-mismatch", "track-numbering",
     "naming-outlier", "loose-tracks"],
)
def test_no_music_reconciliation_finding_is_executable(kind: str) -> None:
    """Every one of these is about a folder, or a set of them. The correction
    plan resolves a file plus its companions in one directory — not a subtree —
    so none of them may acquire a button by accident."""
    assert kind in KINDS, "a finding kind with no label renders as a bare slug"
    assert kind not in EXECUTABLE_KINDS


def test_a_suggested_destination_does_not_make_a_finding_executable() -> None:
    """`split-album` carries a destination. That must stay a suggestion."""
    from librairy.corrections import is_executable

    files = dict(
        [
            track("Queen", "A Night at the Opera", 1, "One"),
            track("Queen", "A Night at the Opera", 2, "Two"),
            ("Music/Pop/Queen/Opera/11 - Bohemian.flac",
             {"artist": "Queen", "album_artist": "Queen",
              "album": "A Night at the Opera", "track": "11"}),
            ("Music/Pop/Queen/Opera/12 - God Save.flac",
             {"artist": "Queen", "album_artist": "Queen",
              "album": "A Night at the Opera", "track": "12"}),
        ]
    )
    split = only(audit_music.detect(view_for(files)), "split-album")
    row = {"kind": split.kind, "dest_relpath": split.dest_relpath}

    assert split.dest_relpath, "the suggestion is worth showing"
    assert not is_executable(row, "current")


def test_music_detectors_do_nothing_when_tags_were_not_read() -> None:
    """`--no-tags` is a fast structural pass, not a broken one."""
    view = view_for(real_compilation())
    view.tags = {}

    assert detect(view) == [] or all(
        finding.kind not in {"split-album", "album-name-mismatch"} for finding in detect(view)
    )
