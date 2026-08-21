"""What a music file is called after LibrAIry names it.

The policy is one line — the folders carry artist and album, the filename
carries the track number and the title — and everything here is about the two
ways that goes wrong. It can go wrong by being unreadable, which is where it
started: `01-Death-on-Two-Legs.flac` is safe on every filesystem and is not
what anybody wants to read down a track list. And it can go wrong by being
readable and unsafe, which is worse, so most of this file is the safety half.

The third failure is not in the formatter at all: a naming policy that changes
and then reports the whole existing library for not matching it. That one has
its own test at the bottom, and it is the reason this could be done at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from librairy.classify.music import classify_music
from librairy.musicnames import canonical_name, parse

ROOT = Path("/library")


def dest(**tags: str) -> str | None:
    from librairy.config import Settings

    settings = Settings(
        APPDATA_DIR=Path("/tmp/appdata"),
        INBOX_DIR=Path("/tmp/inbox"),
        LIBRARY_DIR=ROOT,
        QUARANTINE_DIR=Path("/tmp/quarantine"),
        _env_file=None,
    )
    return classify_music("inbox/song.flac", settings=settings, tags=tags).dest_relpath


# --- the shape ----------------------------------------------------------------------


def test_the_name_that_started_this_is_readable_now() -> None:
    assert dest(
        artist="Queen", album="A Night at the Opera", title="Death on Two Legs",
        track="1", genre="Rock",
    ) == "Music/Rock/Queen/A Night at the Opera/01 - Death on Two Legs.flac"


@pytest.mark.parametrize(
    ("track", "expected"),
    [("1", "01 - Song.flac"), ("01", "01 - Song.flac"), ("7/12", "07 - Song.flac"),
     ("11", "11 - Song.flac"), ("100", "100 - Song.flac")],
)
def test_a_track_number_is_always_spelled_the_same_way(track: str, expected: str) -> None:
    """One convention, not one per code path. Two digits, zero-padded, and a
    number that genuinely needs three keeps three."""
    assert canonical_name("Song", ".flac", track=int(track.split("/")[0])) == expected


def test_a_track_with_no_number_is_just_its_title() -> None:
    """Not `00 - `. A single or a stray never had a number and inventing a zero
    would sort it in front of every real track."""
    assert canonical_name("Bicycle Race", ".mp3") == "Bicycle Race.mp3"


def test_the_disc_appears_only_when_there_is_more_than_one() -> None:
    assert canonical_name("Song", ".flac", track=1, disc=1, discs=2) == "1-01 - Song.flac"
    assert canonical_name("Song", ".flac", track=1, disc=2, discs=2) == "2-01 - Song.flac"


def test_a_single_disc_album_is_not_prefixed_with_the_disc_it_is_on() -> None:
    """Tags routinely say `1/1`. A `1-` in front of every filename is a
    distinction nobody made."""
    assert canonical_name("Song", ".flac", track=1, disc=1, discs=1) == "01 - Song.flac"
    assert canonical_name("Song", ".flac", track=1, disc=1) == "01 - Song.flac"


def test_the_disc_is_read_from_either_tagging_convention() -> None:
    assert dest(
        artist="Queen", album="Live Killers", title="Song", track="1",
        discnumber="2/2", genre="Rock",
    ) == "Music/Rock/Queen/Live Killers/2-01 - Song.flac"
    assert dest(
        artist="Queen", album="Live Killers", title="Song", track="1",
        disc="2", disctotal="2", genre="Rock",
    ) == "Music/Rock/Queen/Live Killers/2-01 - Song.flac"


# --- punctuation that means something -----------------------------------------------


@pytest.mark.parametrize(
    "title",
    ["Don't Stop Me Now", "You're My Best Friend", "Rock 'n' Roll",
     "Sgt. Pepper's Lonely Hearts Club Band"],
)
def test_an_apostrophe_survives(title: str) -> None:
    """Legal on every filesystem LibrAIry runs on, and in a great many titles.
    Dropping it was house style paying nothing for the damage it did."""
    assert canonical_name(title, ".flac", track=3) == f"03 - {title}.flac"


def test_a_question_mark_goes_even_though_the_title_has_one() -> None:
    """Where readability and safety actually conflict, safety wins and says so.
    `?` is rejected by Windows and by every SMB share, so `What's Going On?`
    keeps its apostrophe and loses its question mark."""
    assert canonical_name("What's Going On?", ".flac", track=3) == (
        "03 - What's Going On.flac"
    )


def test_an_ampersand_survives() -> None:
    """`Rock & Roll` must not become `Rock and Roll` — that is a different
    string, and a person looking for the file will not find it."""
    assert canonical_name("Sisters & Brothers", ".flac", track=2) == (
        "02 - Sisters & Brothers.flac"
    )


@pytest.mark.parametrize(
    "title", ["Ça Plane Pour Moi", "君の名は",
              "Björk Guðmundsdóttir", "Ζορμπάς"]
)
def test_unicode_survives(title: str) -> None:
    """A name in another script is a name, not a weird character."""
    assert canonical_name(title, ".flac", track=1) == f"01 - {title}.flac"


@pytest.mark.parametrize(
    "title",
    ["Song (Remastered 2011)", "Song (Live)", "Song (Radio Edit)",
     "Song (Acoustic)", "Song (Demo)", "Song (Kruder & Dorfmeister Remix)"],
)
def test_version_information_in_a_title_is_kept(title: str) -> None:
    """These are the difference between two files a person deliberately owns.
    Nothing here treats a bracket as noise to be tidied away."""
    assert canonical_name(title, ".flac", track=4) == f"04 - {title}.flac"


# --- safety, which readability never buys back ---------------------------------------


def test_a_slash_in_a_title_becomes_something_that_is_not_a_directory() -> None:
    out = canonical_name("AC/DC Tribute", ".flac", track=1)

    assert "/" not in out
    assert out == "01 - AC-DC Tribute.flac"


def test_traversal_cannot_come_out_of_a_title() -> None:
    out = canonical_name("../../etc/passwd", ".flac", track=1)

    assert "/" not in out
    assert not out.startswith(".")
    assert out == "01 - ..-..-etc-passwd.flac"


def test_a_reserved_device_name_is_still_refused() -> None:
    """Windows rejects `CON` whatever the extension, and an SMB share is how
    most of these libraries are read."""
    assert canonical_name("CON", ".flac") == "CON_.flac"


@pytest.mark.parametrize(
    "title",
    ["Song trailing", "Song\ttabbed", "Song​hidden", "Song   spaced"],
)
def test_control_characters_and_odd_spacing_never_reach_a_filename(title: str) -> None:
    out = canonical_name(title, ".flac", track=1)

    assert " " not in out and "\t" not in out and "​" not in out
    assert "  " not in out


def test_a_trailing_dot_does_not_survive() -> None:
    """Windows drops it silently, which turns two names into one collision."""
    assert canonical_name("Song.", ".flac", track=1) == "01 - Song.flac"


def test_an_over_long_title_is_capped_and_keeps_its_extension() -> None:
    out = canonical_name("A" * 400, ".flac", track=1)

    assert out.endswith(".flac")
    assert len(out) < 200


@pytest.mark.parametrize("ext", [".flac", ".mp3", ".m4a", ".ogg", ".opus"])
def test_the_extension_is_preserved_exactly(ext: str) -> None:
    assert canonical_name("Song", ext, track=1) == f"01 - Song{ext}"


def test_a_title_made_entirely_of_unusable_characters_still_produces_a_file() -> None:
    out = canonical_name("\U0001f525\U0001f525", ".flac", track=1)

    assert out.endswith(".flac")
    assert out.rsplit(".", 1)[0]


# --- reading it back ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "track", "disc", "title"),
    [
        ("01 - Death on Two Legs.flac", 1, 0, "Death on Two Legs"),
        ("11 - Bohemian Rhapsody.flac", 11, 0, "Bohemian Rhapsody"),
        ("2-05 - Song.flac", 5, 2, "Song"),
        ("03 - Don't Stop Me Now.flac", 3, 0, "Don't Stop Me Now"),
        ("Bicycle Race.mp3", 0, 0, "Bicycle Race"),
    ],
)
def test_a_name_librairy_wrote_can_be_read_back(
    name: str, track: int, disc: int, title: str
) -> None:
    read = parse(name)

    assert (read.track, read.disc, read.title) == (track, disc, title)


def test_what_it_writes_is_what_it_reads() -> None:
    """The round trip itself, over the whole grammar."""
    for track, disc, discs, title in [
        (1, 0, 0, "Death on Two Legs"), (11, 0, 0, "Don't Stop Me Now"),
        (5, 2, 2, "Song (Live)"), (0, 0, 0, "Bicycle Race"),
    ]:
        name = canonical_name(title, ".flac", track=track, disc=disc, discs=discs)
        read = parse(name)

        assert read.title == title
        assert read.track == track


def test_a_name_librairy_did_not_write_is_not_guessed_at() -> None:
    """`track_final2` is a track called `track_final2`. The digit on the end of
    a name is not a track number, and reading it as one is how a library gets
    renumbered by a parser."""
    read = parse("track_final2.mp3")

    assert read.track == 0
    assert read.title == "track_final2"


def test_a_file_librairy_named_is_not_numbered_twice_when_it_comes_back() -> None:
    """A track tag and no title tag means the title comes from the filename —
    and for a file LibrAIry named, the filename already begins with the number.
    Reading the stem whole wrote `01 - 01 - Death on Two Legs.flac`."""
    assert dest(
        artist="Queen", album="A Night at the Opera", track="1", genre="Rock",
    ) is not None
    from librairy.classify.music import classify_music
    from librairy.config import Settings

    settings = Settings(
        APPDATA_DIR=Path("/tmp/appdata"), INBOX_DIR=Path("/tmp/inbox"),
        LIBRARY_DIR=ROOT, QUARANTINE_DIR=Path("/tmp/quarantine"), _env_file=None,
    )
    result = classify_music(
        "inbox/01 - Death on Two Legs.flac",
        settings=settings,
        tags={"artist": "Queen", "album": "A Night at the Opera", "track": "1",
              "genre": "Rock"},
    )

    assert result.clean_name == "01 - Death on Two Legs.flac"


def test_a_track_number_in_the_name_is_used_when_the_tags_have_none() -> None:
    from librairy.classify.music import classify_music
    from librairy.config import Settings

    settings = Settings(
        APPDATA_DIR=Path("/tmp/appdata"), INBOX_DIR=Path("/tmp/inbox"),
        LIBRARY_DIR=ROOT, QUARANTINE_DIR=Path("/tmp/quarantine"), _env_file=None,
    )
    result = classify_music(
        "inbox/07 - Sweet Lady.flac",
        settings=settings,
        tags={"artist": "Queen", "album": "A Night at the Opera", "genre": "Rock"},
    )

    assert result.clean_name == "07 - Sweet Lady.flac"


def test_the_filename_never_claims_to_know_the_artist() -> None:
    """Artist and album are the folders. A parser that answered them from the
    name would be answering from something nobody wrote down."""
    read = parse("01 - Death on Two Legs.flac")

    assert not hasattr(read, "artist")
    assert not hasattr(read, "album")


# --- the library that already exists --------------------------------------------------


def test_an_old_style_music_filename_is_not_reported_by_the_audit(tmp_path: Path) -> None:
    """This is the test that made the change possible.

    Every file filed before today is spelled the old way. If a new formatter
    turned all of them into findings, the audit would open with thousands of
    rows saying "this is not how I would have spelled it" — which is house
    style masquerading as a defect, and exactly what `naming.py` refuses to do.
    """
    from librairy.audit import audit_library
    from librairy.config import Settings
    from librairy.db import connect
    from librairy.scanner import scan_root

    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        FILE_STABILITY_SECONDS=0,
        _env_file=None,
    )
    for root in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        root.mkdir(parents=True, exist_ok=True)
    folder = settings.library_dir / "Music/Rock/Queen/A-Night-at-the-Opera"
    folder.mkdir(parents=True, exist_ok=True)
    for name in [
        "01-Death-on-Two-Legs.flac", "02-Lazing-on-a-Sunday-Afternoon.flac",
        "03-Im-in-Love-with-My-Car.flac", "04-Youre-My-Best-Friend.flac",
        "05-39.flac", "06-Sweet-Lady.flac",
    ]:
        (folder / name).write_text(name, encoding="utf-8")
    conn = connect(settings)
    scan_root(conn, "library", settings.library_dir, settings)

    audit_library(conn, settings, read_tags=False, use_catalogs=False)

    findings = conn.execute(
        "SELECT kind, relpath FROM audit_findings"
        " WHERE relpath LIKE 'Music/%' AND kind LIKE 'naming%'"
    ).fetchall()
    assert [dict(row) for row in findings] == []
