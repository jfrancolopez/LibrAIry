"""LibrAIry must be able to read a name LibrAIry wrote.

For every category but one that does not matter: nobody parses
`Photos/2024/August/IMG_5150.jpeg` back into anything. For music videos it is
the whole scheme. `musicvideo.parse` reads the artist and the title either side
of ` - ` and the version out of the brackets, and that is what keeps `(Clean)`
and `(Dirty)` apart, what makes two pools' spellings of one file converge, and
what lets Library Audit recognise a file as being under the right artist.

House style turned every space into a dash, so the separator the whole scheme
rests on was the first thing destroyed — and the workaround was to treat the
*artist folder* as the identity instead. This file is the fix and its
boundaries: safety is unchanged, one category is exempt from restyling, and
nothing that was already filed is reported for being spelled the old way.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from librairy.classify.musicvideos import canonical_name
from librairy.musicvideo import parse
from librairy.naming import media_filename, slugify_filename
from librairy.taxonomy import PARSED_FILENAME_CATEGORIES, render_destination

ROOT = Path("/library")

# Names a real pool produces, and every one of them awkward in its own way.
CREDITS = [
    ("50 Cent", "In Da Club", ()),
    ("a-ha", "Take On Me", ("Remastered HD",)),
    ("Beyoncé", "Halo", ("Official Music Video",)),
    ("deadmau5", "Strobe", ()),
    ("Guns N' Roses", "Sweet Child O' Mine", ()),
    ("Earth, Wind & Fire", "September", ()),
    ("The Weeknd feat. Daft Punk", "Starboy", ()),
    ("Daft Punk", "Around the World", ("Official Video",)),
    ("Coldplay", "Yellow", ("Lyric Video",)),
    ("Madonna", "Frozen", ("Live",)),
]


def formatted(credit: str, title: str, versions: tuple[str, ...], ext=".mp4") -> str:
    versions_part = "".join(f" ({version})" for version in versions)
    return media_filename(f"{credit} - {title}{versions_part}{ext}")


# --- the round trip ------------------------------------------------------------


@pytest.mark.parametrize(("credit", "title", "versions"), CREDITS)
def test_a_name_librairy_wrote_is_a_name_librairy_can_read(
    credit: str, title: str, versions: tuple[str, ...]
) -> None:
    read = parse(formatted(credit, title, versions))

    assert read.confident, f"{formatted(credit, title, versions)!r} came back unreadable"
    assert read.credited_artist == credit
    assert read.title == title
    assert read.versions == versions


def test_the_round_trip_survives_a_second_pass(tmp_path: Path) -> None:  # noqa: ARG001
    """Formatting an already-formatted name changes nothing. A policy that
    drifted on each pass would rewrite a library a little at a time."""
    once = formatted("Daft Punk", "Around the World", ("Official Video",))
    read = parse(once)

    twice = canonical_name(read, ".mkv", fallback="unused")

    assert twice == "Daft Punk - Around the World (Official Video).mkv"
    assert parse(twice).credited_artist == "Daft Punk"


def test_a_featured_credit_reads_back_whole(tmp_path: Path) -> None:  # noqa: ARG001
    read = parse(formatted("The Weeknd feat. Daft Punk", "Starboy", ()))

    assert read.primary_artist == "The Weeknd"
    assert read.featured_artists == ("Daft Punk",)


def test_two_versions_of_one_song_stay_two_files() -> None:
    clean = parse(formatted("50 Cent", "In Da Club", ("Clean",)))
    dirty = parse(formatted("50 Cent", "In Da Club", ("Dirty",)))

    assert clean.work_key == dirty.work_key
    assert clean.version_key != dirty.version_key


def test_a_slash_in_a_band_name_becomes_safe_and_stays_readable() -> None:
    """`AC/DC` cannot be a filename. What matters is that what is left is still
    one name and not two path components."""
    out = media_filename("AC/DC - Back In Black.mkv")

    assert "/" not in out
    assert parse(out).credited_artist == "AC-DC"
    assert parse(out).title == "Back In Black"


# --- safety is unchanged ---------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "banned"),
    [
        ("../../etc/passwd.mp4", "/"),
        ("a<b>c:d|e?f*g.mp4", "<"),
        ("trailing space .mp4", None),
        ("trailing dot..mp4", None),
    ],
)
def test_a_presented_name_is_still_a_safe_name(raw: str, banned: str | None) -> None:
    out = media_filename(raw)

    assert "/" not in out
    assert "\\" not in out
    assert not out.startswith(" ") and not out.endswith(" ")
    assert not out.rsplit(".", 1)[0].endswith(".")
    if banned:
        assert banned not in out


def test_a_reserved_device_name_is_still_escaped() -> None:
    assert media_filename("CON.mp4") == "CON_.mp4"


def test_a_control_character_never_reaches_a_filename() -> None:
    assert "\x00" not in media_filename("bad\x00name.mp4")
    assert "\n" not in media_filename("two\nlines.mp4")


def test_a_name_with_nothing_safe_left_falls_back_rather_than_vanishing() -> None:
    out = media_filename("🔥🔥🔥.mp4")

    assert out.endswith(".mp4")
    assert out.rsplit(".", 1)[0]


def test_the_destination_is_still_contained() -> None:
    result = render_destination(
        "music_videos",
        {"genre": "House", "artist": "../../escape", "clean_name": "../../x.mp4"},
        library_root=ROOT,
    )

    assert result.relpath is None or ".." not in Path(result.relpath).parts


# --- the blast radius ---------------------------------------------------------------


def test_exactly_one_category_is_exempt_from_house_style() -> None:
    """Widening this is a decision about how a whole library reads, and would
    change the name of every file filed under whatever is added."""
    assert set(PARSED_FILENAME_CATEGORIES) == {"music_videos"}


@pytest.mark.parametrize(
    ("category", "fields", "expected"),
    [
        (
            "movies",
            {"genre": "Sci-Fi", "title": "The Matrix", "year": 1999,
             "clean_name": "The Matrix (1999).mkv"},
            "Movies/Sci-Fi/The-Matrix-(1999)/The-Matrix-(1999).mkv",
        ),
        (
            "shows",
            {"genre": "Drama", "show": "The Wire", "season": 1,
             "clean_name": "S01E01 - The Target.mkv"},
            "Shows/Drama/The-Wire/Season-01/S01E01-The-Target.mkv",
        ),
        (
            "music",
            {"genre": "Rock", "artist": "Queen", "album": "A Night at the Opera",
             "clean_name": "01 - Death on Two Legs.flac"},
            "Music/Rock/Queen/A-Night-at-the-Opera/01-Death-on-Two-Legs.flac",
        ),
        (
            "photos",
            {"year": 2024, "event": "August", "clean_name": "IMG_5150.jpeg"},
            "Photos/2024/August/IMG_5150.jpeg",
        ),
    ],
)
def test_every_other_category_is_named_exactly_as_before(
    category: str, fields: dict, expected: str
) -> None:
    """The blast radius, pinned. A music video's filename is an identity; a
    film's is a description, and nothing reads it back."""
    assert render_destination(category, fields, library_root=ROOT).relpath == expected


def test_house_style_itself_is_unchanged() -> None:
    """The fix is a category exemption, not a new policy. `slugify` still does
    exactly what it did, for everything that still goes through it."""
    assert slugify_filename("David Guetta feat. Sia - Titanium (Extended Mix).mp4") == (
        "David-Guetta-feat.-Sia-Titanium-(Extended-Mix).mp4"
    )


def test_no_audit_finding_is_raised_for_a_name_spelled_the_old_way(
    tmp_path: Path,
) -> None:
    """A naming policy that changed and then reported the whole library for not
    matching it would be the worst possible version of this change.

    Library Audit enforces hygiene — damaged names — and never house style. A
    file filed before this change is spelled differently and is not damaged.
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
    old = settings.library_dir / (
        "Music Videos/House/Fatboy-Slim/Fatboy-Slim-Praise-You.mp4"
    )
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_text("video", encoding="utf-8")
    conn = connect(settings)
    scan_root(conn, "library", settings.library_dir, settings)

    audit_library(conn, settings, read_tags=False, use_catalogs=False)

    findings = conn.execute(
        "SELECT kind, relpath FROM audit_findings WHERE relpath LIKE 'Music Videos/%'"
    ).fetchall()
    assert [dict(row) for row in findings] == []
