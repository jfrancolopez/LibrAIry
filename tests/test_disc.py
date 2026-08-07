"""A ripped disc is one thing, not forty unanswerable questions.

Nine files of one DVD sat in Review at 0.3 confidence with no destination,
because nothing about "VTS_01_3.VOB" says what film it is. The title is on the
folder two levels up, and the names inside must not be rewritten on the way.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from librairy.classify.disc import classify_disc, disc_part, disc_title
from librairy.config import Settings
from librairy.naming import tidy_relpath


def settings_for(tmp_path: Path) -> Settings:
    return Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        _env_file=None,
    )


def test_a_dvd_files_itself_under_the_name_on_its_folder(tmp_path: Path) -> None:
    result = classify_disc(
        "Queen - 1979-12-26 - The Queen Special on TV - DVD5/VIDEO_TS/VTS_01_1.VOB",
        settings=settings_for(tmp_path),
    )

    assert result is not None
    assert result.category == "movies"
    assert result.dest_relpath == (
        "Movies/General/Queen-The-Queen-Special-on-TV-(1979)/VIDEO_TS/VTS_01_1.VOB"
    )
    # High enough to clear a default threshold: a file inside a VIDEO_TS has
    # exactly one sensible home, whatever the classifier makes of its name.
    assert result.confidence >= 0.8


@pytest.mark.parametrize(
    "name",
    ["VTS_01_1.VOB", "VIDEO_TS.IFO", "VIDEO_TS.BUP", "VTS_01_0.IFO"],
)
def test_the_names_inside_a_disc_survive_the_move_exactly(tmp_path: Path, name: str) -> None:
    """A player looks for VTS_01_1.VOB by that name, and VIDEO_TS.IFO points at
    its siblings by theirs. A tidied disc folder is one that no longer plays."""
    result = classify_disc(f"The Matrix DVD9/VIDEO_TS/{name}", settings=settings_for(tmp_path))

    assert result is not None
    assert result.dest_relpath.endswith(f"/VIDEO_TS/{name}")


def test_tidy_relpath_leaves_a_disc_structure_alone_but_still_tidies_above_it() -> None:
    tidied = tidy_relpath("Movies/General/The Matrix (1999)/VIDEO_TS/VTS_01_1.VOB")

    assert tidied == "Movies/General/The-Matrix-(1999)/VIDEO_TS/VTS_01_1.VOB"


def test_tidy_relpath_still_rewrites_an_ordinary_path() -> None:
    """The exception is for disc structures, not a hole in the guarantee."""
    assert tidy_relpath("Movies/General/The Matrix (1999)/The Matrix.MKV") == (
        "Movies/General/The-Matrix-(1999)/The-Matrix.mkv"
    )


def test_a_stray_video_ts_file_is_just_a_file(tmp_path: Path) -> None:
    """Only a disc *directory* counts. Treating a lone VIDEO_TS.IFO as a disc
    would invent a movie out of whatever folder happened to contain it."""
    assert classify_disc("downloads/VIDEO_TS.IFO", settings=settings_for(tmp_path)) is None


def test_a_disc_directory_with_nothing_above_it_to_name_it_is_left_alone(
    tmp_path: Path,
) -> None:
    assert classify_disc("VIDEO_TS/VTS_01_1.VOB", settings=settings_for(tmp_path)) is None


@pytest.mark.parametrize(
    ("folder", "expected"),
    [
        ("The Matrix (1999) DVD9", ("The Matrix", 1999)),
        ("The Matrix 1999 DVD-9", ("The Matrix", 1999)),
        (
            "Queen - 1979-12-26 - The Queen Special on TV - DVD5",
            ("Queen The Queen Special on TV", 1979),
        ),
        ("Some Concert", ("Some Concert", 0)),
        ("Holiday Video Disc 2", ("Holiday Video", 0)),
    ],
)
def test_the_medium_is_not_part_of_the_title(folder: str, expected: tuple[str, int]) -> None:
    """"DVD5" and "Disc 2" say how it was written down, not what it is."""
    assert disc_title(folder) == expected


def test_every_file_of_one_disc_shares_a_group_key(tmp_path: Path) -> None:
    """One disc is one decision; nine rows that group together is the whole
    difference between answerable and not."""
    settings = settings_for(tmp_path)
    keys = {
        classify_disc(f"A Concert DVD5/VIDEO_TS/{name}", settings=settings).group_key
        for name in ("VIDEO_TS.IFO", "VTS_01_1.VOB", "VTS_01_2.VOB")
    }

    assert len(keys) == 1


def test_disc_part_finds_the_folder_that_names_the_disc() -> None:
    part = disc_part("films/The Matrix DVD9/VIDEO_TS/VTS_01_1.VOB")

    assert part is not None
    assert part.title_folder == "The Matrix DVD9"
    assert part.inner_relpath == "VIDEO_TS/VTS_01_1.VOB"
    assert part.disc_directory == "VIDEO_TS"
