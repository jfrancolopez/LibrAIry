"""Parts of the library nothing may be queued to change.

A phone's camera roll backed up once is the only copy there will ever be, and
the whole value of keeping it is that it is untouched. The load-bearing tests
are the containment ones: a protection that stops at the first directory, or
that lets `Photos/Memories` accidentally extend to `Photos/MemoriesOfLastYear`,
is a rule people believe they have and do not.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from librairy.config import Settings
from librairy.db import connect
from librairy.paths import PathValidationError
from librairy.protected import (
    DEFAULT_ROOTS,
    is_protected,
    protected_roots,
    protecting_root,
    set_protected_roots,
)

ROOTS = ("Photos/Memories", "Masters")


def scene(tmp_path: Path):
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


# --- containment ----------------------------------------------------------------


def test_a_protected_root_protects_itself() -> None:
    assert is_protected("Photos/Memories", ROOTS)


@pytest.mark.parametrize(
    "relpath",
    [
        "Photos/Memories/IMG_0001.HEIC",
        "Photos/Memories/2024/IMG_0001.HEIC",
        "Photos/Memories/2024/January/Trip/IMG_0001.HEIC",
        "Masters/Session 1/take.wav",
    ],
)
def test_protection_reaches_every_descendant(relpath: str) -> None:
    """Recursive, or it is a rule people believe they have and do not."""
    assert is_protected(relpath, ROOTS)


@pytest.mark.parametrize(
    "relpath",
    [
        "Photos/2024/IMG_0001.HEIC",
        "Photos/MemoriesOfLastYear/IMG_0001.HEIC",
        "Music/Pop/Abba/01 - Song.flac",
        "MastersOfTheUniverse/clip.mkv",
        "Somewhere/Photos/Memories/IMG_0001.HEIC",
    ],
)
def test_a_sibling_or_a_near_name_is_not_protected(relpath: str) -> None:
    """`startswith` would call `Photos/MemoriesOfLastYear` protected, and a
    rule that protects more than it says is as wrong as one that protects
    less — it silently disables the feature on folders nobody chose."""
    assert not is_protected(relpath, ROOTS)


def test_nothing_is_protected_when_nothing_is_configured() -> None:
    assert not is_protected("Photos/Memories/IMG_0001.HEIC", ())
    assert DEFAULT_ROOTS == ()


def test_case_does_not_defeat_protection() -> None:
    """APFS and SMB are as common as ext4 here, and a protection that stops
    working when somebody types `photos/memories` is not a protection."""
    assert is_protected("photos/MEMORIES/img.heic", ROOTS)


def test_the_protecting_root_is_named_so_the_ui_can_say_it() -> None:
    assert protecting_root("Photos/Memories/2024/x.heic", ROOTS) == "photos/memories"
    assert protecting_root("Music/x.flac", ROOTS) == ""


# --- configuration ---------------------------------------------------------------


def test_roots_survive_a_round_trip(tmp_path: Path) -> None:
    conn, settings = scene(tmp_path)

    set_protected_roots(conn, ["Photos/Memories", "Masters"], library_dir=settings.library_dir)

    assert protected_roots(conn) == ("Photos/Memories", "Masters")


def test_several_roots_are_supported_without_a_code_change(tmp_path: Path) -> None:
    conn, settings = scene(tmp_path)

    stored = set_protected_roots(
        conn,
        ["Photos/Memories", "Masters", "Archives", "RAW Photos"],
        library_dir=settings.library_dir,
    )

    assert len(stored) == 4
    assert is_protected("RAW Photos/2024/DSC_0001.NEF", stored)


def test_blanks_and_duplicates_are_dropped(tmp_path: Path) -> None:
    conn, settings = scene(tmp_path)

    stored = set_protected_roots(
        conn, ["Masters", "", "  ", "/Masters/", "Masters"], library_dir=settings.library_dir
    )

    assert stored == ("Masters",)


@pytest.mark.parametrize(
    "bad",
    [
        "../etc",
        "Photos/../../etc",
        "~/secrets",
        "..\\Windows",
    ],
)
def test_a_root_that_escapes_the_library_is_refused(tmp_path: Path, bad: str) -> None:
    """The same containment check as every other path in LibrAIry. A
    protected-root check with its own string comparison is a protected-root
    check with its own bugs."""
    conn, settings = scene(tmp_path)

    with pytest.raises(PathValidationError):
        set_protected_roots(conn, [bad], library_dir=settings.library_dir)


def test_encoded_traversal_does_not_evade_protection() -> None:
    """`%2e%2e` is not traversal until something decodes it, and nothing here
    does — so it is an ordinary folder name and is simply not protected."""
    assert not is_protected("Photos/%2e%2e/Memories/x.heic", ROOTS)
    assert is_protected("Photos/Memories/%2e%2e/x.heic", ROOTS)


def test_unreadable_configuration_protects_nothing_rather_than_crashing(
    tmp_path: Path,
) -> None:
    conn, _ = scene(tmp_path)
    conn.execute(
        "INSERT OR REPLACE INTO settings(key, value) VALUES "
        "('optimization.protected_roots', 'not json')"
    )

    assert protected_roots(conn) == ()


# --- what protection does and does not stop ---------------------------------------


def test_a_protected_file_can_still_be_analyzed(tmp_path: Path) -> None:
    """Refusing to describe it would make the library's biggest folder
    invisible in its own storage report, which helps nobody."""
    from librairy.optimization import MediaFacts, advise

    facts = MediaFacts(container=".wav", size=200 * 1024 * 1024, duration=600)

    opportunity = advise("Photos/Memories/audio.wav", facts, protected_by="Photos/Memories")

    assert opportunity is not None, "still described"
    assert opportunity.current_bytes == 200 * 1024 * 1024
    assert opportunity.estimated_saving > 0


def test_a_protected_file_can_never_be_queued(tmp_path: Path) -> None:
    from librairy.optimization import MediaFacts, advise

    facts = MediaFacts(container=".wav", size=200 * 1024 * 1024, duration=600)

    opportunity = advise("Photos/Memories/audio.wav", facts, protected_by="Photos/Memories")

    assert opportunity.protected is True
    assert opportunity.eligible is False
    assert opportunity.protected_by == "Photos/Memories"


def test_an_unprotected_file_is_eligible() -> None:
    from librairy.optimization import MediaFacts, advise

    facts = MediaFacts(container=".wav", size=200 * 1024 * 1024, duration=600)

    opportunity = advise("Music/audio.wav", facts)

    assert opportunity.protected is False
    assert opportunity.eligible is True


def test_a_leading_slash_is_read_as_library_relative(tmp_path: Path) -> None:
    """Typing `/Photos/Memories` means the folder in the library, not the one
    at the filesystem root. Normalised rather than refused — and harmless
    either way, because containment still decides what the name can reach."""
    conn, settings = scene(tmp_path)

    stored = set_protected_roots(
        conn, ["/Photos/Memories"], library_dir=settings.library_dir
    )

    assert stored == ("Photos/Memories",)
