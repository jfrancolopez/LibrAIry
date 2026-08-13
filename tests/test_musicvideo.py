"""Reading a DJ filename, and knowing a version from a duplicate.

The test this file exists for is
`test_clean_and_dirty_are_two_files_not_one`. Every other assumption LibrAIry
makes about music says two files with the same artist and title are the same
file; in a video pool they are usually not, and offering to quarantine half a
working DJ collection is the worst thing this software could do to it.
"""

from __future__ import annotations

import pytest

from librairy.musicvideo import (
    DUPLICATE,
    POSSIBLE,
    RELATED,
    UNRELATED,
    group_versions,
    parse,
    relationship,
    version_label,
)

# --- the shapes a pool actually writes -----------------------------------------


@pytest.mark.parametrize(
    ("filename", "artist", "title"),
    [
        ("Bee Gees - Night Fever.mp4", "Bee Gees", "Night Fever"),
        ("Artist – Title.mp4", "Artist", "Title"),  # en dash
        ("Artist — Title.mp4", "Artist", "Title"),  # em dash
        ("01 - Artist - Title.mp4", "Artist", "Title"),
        ("01. Artist - Title.mp4", "Artist", "Title"),
        ("07 Artist - Title.mp4", "Artist", "Title"),
        ("Jay-Z - Song Title.mp4", "Jay-Z", "Song Title"),
        ("Artist - Song - Part Two.mp4", "Artist", "Song - Part Two"),
    ],
)
def test_artist_and_title(filename: str, artist: str, title: str) -> None:
    parsed = parse(filename)

    assert parsed.primary_artist == artist
    assert parsed.title == title
    assert parsed.confident


def test_a_number_in_an_artist_name_is_not_a_track_number() -> None:
    """`50 Cent` was filed as `Cent` by an earlier version of the pattern.

    A track number is either followed by punctuation or zero-padded. `50 Cent`
    is neither, and the distinction is the whole rule.
    """
    parsed = parse("50 Cent - In Da Club.mp4")

    assert parsed.primary_artist == "50 Cent"
    assert parsed.title == "In Da Club"


def test_a_hyphen_inside_a_word_is_not_a_separator() -> None:
    assert parse("Jay-Z - 99 Problems.mp4").primary_artist == "Jay-Z"


def test_an_unreadable_name_invents_nothing() -> None:
    """A garbage artist folder outlives the guess that made it."""
    parsed = parse("random-video-001.mp4")

    assert parsed.primary_artist == ""
    assert parsed.confident is False
    assert parsed.work_key == ""
    assert "separator" in parsed.notes[0]


def test_an_underscore_split_is_made_but_not_trusted() -> None:
    """`Artist_Title_Extended` and `song_clean_final` are the same shape, and
    only one of them names an artist. Split, marked unsure, sent to Review."""
    good, bad = parse("Artist_Title_Extended.mp4"), parse("song_clean_final.mp4")

    assert good.primary_artist == "Artist"
    assert good.confident is False
    assert bad.confident is False
    assert any("guess" in note for note in bad.notes)


def test_two_unreadable_names_are_not_the_same_song() -> None:
    """An empty work key must never match another empty one."""
    left, right = parse("random-001.mp4"), parse("random-002.mp4")

    assert relationship(left, right) == UNRELATED


# --- versions ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "version"),
    [
        ("Artist - Song (Clean).mp4", "Clean"),
        ("Artist - Song (Dirty).mp4", "Dirty"),
        ("Artist - Song (Extended Mix).mp4", "Extended Mix"),
        ("Artist - Song (DJ Intro Edit).mp4", "DJ Intro Edit"),
        ("Artist - Song (Outro Edit).mp4", "Outro Edit"),
        ("Artist - Song (Club Mix).mp4", "Club Mix"),
        ("Artist - Song (Acapella).mp4", "Acapella"),
        ("Artist - Song (Instrumental).mp4", "Instrumental"),
        ("Artist - Song (Radio Edit).mp4", "Radio Edit"),
        ("Artist - Song [Clean].mp4", "Clean"),
        ("Artist - Song (Tiesto Remix).mp4", "Tiesto Remix"),
    ],
)
def test_version_markers_are_read(filename: str, version: str) -> None:
    assert parse(filename).versions == (version,)


def test_a_version_keeps_its_whole_phrase() -> None:
    """`Tiesto Remix` and `Armand Van Helden Remix` are different files.
    Reducing both to `remix` would make them look like one."""
    left = parse("Artist - Song (Tiesto Remix).mp4")
    right = parse("Artist - Song (Armand Van Helden Remix).mp4")

    assert left.versions != right.versions
    assert relationship(left, right) == RELATED


def test_a_remixer_is_named_but_never_becomes_the_artist() -> None:
    parsed = parse("Artist - Song (Tiesto Remix).mp4")

    assert parsed.remixer == "Tiesto"
    assert parsed.primary_artist == "Artist", "the remixer does not own the track"


def test_an_intro_edit_is_not_a_remix_by_someone_called_dj_intro() -> None:
    """Reading `DJ Intro Edit` as a remixer invents a person, and that person
    then becomes an artist folder."""
    parsed = parse("Artist - Song (DJ Intro Edit).mp4")

    assert parsed.versions == ("DJ Intro Edit",)
    assert parsed.remixer == ""


def test_an_unbracketed_trailing_version_is_still_a_version() -> None:
    """Pools write these constantly, and losing it merges two files."""
    parsed = parse("DAVID GUETTA FT SIA - TITANIUM EXTENDED.mp4")

    assert parsed.title == "TITANIUM"
    assert parsed.versions == ("EXTENDED",)


def test_a_version_word_at_the_start_of_a_title_is_part_of_the_title() -> None:
    """*Live and Let Die* is a song, not a live recording."""
    parsed = parse("Artist - Live and Let Die.mp4")

    assert parsed.title == "Live and Let Die"
    assert parsed.versions == ()


def test_two_version_words_are_both_kept() -> None:
    assert parse("50 Cent - In Da Club (Dirty Extended).mp4").versions == (
        "Dirty Extended",
    )
    assert set(parse("Artist - Song Extended Clean.mp4").versions) == {
        "Extended", "Clean",
    }


def test_an_unmarked_file_is_labelled_original() -> None:
    assert version_label(parse("Artist - Song.mp4")) == "Original"
    assert version_label(parse("Artist - Song (Clean).mp4")) == "Clean"


# --- credits -------------------------------------------------------------------


@pytest.mark.parametrize(
    "filename",
    [
        "David Guetta feat. Sia - Titanium.mp4",
        "David Guetta ft. Sia - Titanium.mp4",
        "David Guetta ft Sia - Titanium.mp4",
        "David Guetta featuring Sia - Titanium.mp4",
    ],
)
def test_a_featured_artist_is_a_guest_not_the_act(filename: str) -> None:
    parsed = parse(filename)

    assert parsed.primary_artist == "David Guetta"
    assert parsed.featured_artists == ("Sia",)


def test_joint_billing_takes_the_first_name_and_keeps_the_rest() -> None:
    """Stability beats fairness here: the hierarchy has to sit still, and the
    full credit survives in `credited_artist` and in the filename."""
    parsed = parse("Calvin Harris & Dua Lipa - One Kiss.mp4")

    assert parsed.primary_artist == "Calvin Harris"
    assert parsed.featured_artists == ("Dua Lipa",)
    assert parsed.credited_artist == "Calvin Harris & Dua Lipa"


def test_a_mashup_credit_survives_without_a_catalog() -> None:
    """Catalogs will never have heard of this, and that is fine."""
    parsed = parse("Artist A vs. Artist B - Song A x Song B (DJ Name Mashup).mp4")

    assert parsed.primary_artist == "Artist A"
    assert parsed.featured_artists == ("Artist B",)
    assert parsed.title == "Song A x Song B"
    assert parsed.remixer == "DJ Name"


# --- duplicate versus version --------------------------------------------------


def test_clean_and_dirty_are_two_files_not_one() -> None:
    """The rule this module exists for."""
    clean = parse("50 Cent - In Da Club (Clean).mp4")
    dirty = parse("50 Cent - In Da Club (Dirty).mp4")

    assert clean.work_key == dirty.work_key, "the same song"
    assert clean.version_key != dirty.version_key, "not the same file"
    assert relationship(clean, dirty) == RELATED


@pytest.mark.parametrize(
    "other",
    [
        "Artist - Song (Clean).mp4",
        "Artist - Song (Dirty).mp4",
        "Artist - Song (Extended Mix).mp4",
        "Artist - Song (DJ Intro Edit).mp4",
        "Artist - Song (Outro Edit).mp4",
        "Artist - Song (Club Mix).mp4",
        "Artist - Song (Tiesto Remix).mp4",
        "Artist - Song (Acapella).mp4",
        "Artist - Song (Instrumental).mp4",
    ],
)
def test_every_legitimate_version_stays(other: str) -> None:
    assert relationship(parse("Artist - Song.mp4"), parse(other)) == RELATED


def test_identical_bytes_are_a_duplicate_whatever_the_names_say() -> None:
    """The one claim that needs no interpretation, so it outranks everything."""
    assert (
        relationship(
            parse("Artist - Song (Clean).mp4"),
            parse("Artist - Song (Extended Mix).mp4"),
            same_bytes=True,
        )
        == DUPLICATE
    )


def test_the_same_version_twice_is_a_possible_duplicate() -> None:
    """A second download of the same edit. Worth mentioning, never automatic."""
    assert (
        relationship(
            parse("Artist - Song (Clean).mp4"),
            parse("Artist - Song (Clean).mp4"),
            similar=True,
        )
        == POSSIBLE
    )


def test_similar_bytes_across_different_songs_is_still_only_possible() -> None:
    assert (
        relationship(parse("A - One.mp4"), parse("B - Two.mp4"), similar=True) == POSSIBLE
    )


def test_matching_artist_and_title_alone_never_produces_a_duplicate() -> None:
    """Stated as the negative, because this is the regression that would
    quietly eat a collection."""
    names = [
        "Artist - Song.mp4", "Artist - Song (Clean).mp4", "Artist - Song (Dirty).mp4",
        "Artist - Song (Extended Mix).mp4", "Artist - Song (Acapella).mp4",
    ]
    parsed = [parse(name) for name in names]

    for left in parsed:
        for right in parsed:
            if left.version_key == right.version_key:
                continue
            assert relationship(left, right) != DUPLICATE


# --- the shape a future Browse needs -------------------------------------------


def test_versions_group_under_one_song_without_a_folder() -> None:
    files = {
        "Music Videos/House/David Guetta/Titanium.mp4": None,
        "Music Videos/House/David Guetta/Titanium (Clean).mp4": None,
        "Music Videos/House/David Guetta/Titanium (Extended Mix).mp4": None,
        "Music Videos/Disco/Bee Gees/Night Fever.mp4": None,
    }
    parsed = {
        relpath: parse(relpath.rsplit("/", 1)[1].replace("Titanium", "David Guetta - Titanium")
                       .replace("Night Fever", "Bee Gees - Night Fever"))
        for relpath in files
    }

    groups = group_versions(parsed)

    assert len(groups) == 2
    titanium = next(paths for key, paths in groups.items() if "titanium" in key)
    assert len(titanium) == 3


def test_unreadable_files_are_not_piled_into_one_group() -> None:
    """Unknown is not a song."""
    parsed = {
        "a.mp4": parse("random-001.mp4"),
        "b.mp4": parse("random-002.mp4"),
    }

    assert group_versions(parsed) == {}
