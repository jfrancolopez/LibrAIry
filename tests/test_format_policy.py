"""What the owner prefers, permits and protects, asked in one place.

The tests that matter most here are the ones asserting **absence**. LibrAIry
has one explicit format preference — MP3 for music — and every other category
must stay neutral until somebody says otherwise. A program that invented
"prefer JPEG" or "prefer H.265" from the fact that it *can* transcode would be
making a quality claim wearing a policy's clothes, which is the exact thing
`format_preference` was split out to avoid.
"""

from __future__ import annotations

import ast
import sqlite3
from pathlib import Path

import pytest

from librairy.config import Settings
from librairy.db import connect
from librairy.format_policy import (
    BY_POLICY,
    BY_ROOT,
    PolicyError,
    preferred_for,
    protect_folder,
    protected_folders,
    resolve,
    set_preferred_format,
    set_transforms,
    unprotect_folder,
)


def settings_for(tmp_path: Path) -> Settings:
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        _env_file=None,
    )
    for root in (
        settings.appdata_dir,
        settings.inbox_dir,
        settings.library_dir,
        settings.quarantine_dir,
    ):
        root.mkdir(parents=True, exist_ok=True)
    return settings


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    return connect(settings_for(tmp_path))


# --------------------------------------------------------------------------
# 1-5: what the policy says on a library nobody has configured
# --------------------------------------------------------------------------


def test_the_existing_mp3_preference_resolves_through_the_central_policy(
    conn: sqlite3.Connection,
) -> None:
    found = resolve(conn, "Music/Rock/Queen/A Night at the Opera/01 - Song.flac")

    assert found.category == "music"
    assert found.preferred_format == "mp3"
    assert found.explanation == "MP3 is your preferred format for Music."


def test_there_is_exactly_one_place_the_mp3_preference_lives(
    conn: sqlite3.Connection,
) -> None:
    """Two rows that both claim to be the preference can disagree; one cannot.

    It used to be a `settings` row. Migration 044 moved it — moved, not
    copied — into the one scope table every other domain will use.
    """
    from librairy.format_preference import preferred, set_preferred

    set_preferred(conn, "flac")

    assert preferred(conn) == "flac"
    assert preferred_for(conn, "music") == "flac"
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM settings WHERE key LIKE '%preferred_format%'"
        ).fetchone()[0]
        == 0
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM format_policy_scopes WHERE preferred_format<>''"
        ).fetchone()[0]
        == 1
    )


def test_photos_have_no_preferred_format(conn: sqlite3.Connection) -> None:
    """No photo representation preference has ever been stated.

    Not "prefer JPEG", not "prefer HEIC", not "prefer smallest" and certainly
    not "delete RAW". LibrAIry being *able* to compare photographs is not the
    owner having an opinion about which encoding they want.
    """
    found = resolve(conn, "Photos/2024/Backyard/IMG_5200.CR3")

    assert found.preferred_format == ""
    assert found.explanation == "No format preference is configured for Photos."
    assert found.stated is False


def test_documents_have_no_preferred_format(conn: sqlite3.Connection) -> None:
    """EPUB against PDF stays a question the owner answers each time."""
    found = resolve(conn, "Documents/Manuals/Netgear/R7000.epub")

    assert found.preferred_format == ""
    assert found.explanation == "No format preference is configured for Documents."


def test_video_has_no_preferred_format(conn: sqlite3.Connection) -> None:
    """Storage Optimization can re-encode. That is a capability, not a wish."""
    for relpath in (
        "Movies/Arrival (2016)/Arrival (2016).mkv",
        "Shows/Severance/S01E01.mp4",
        "Music Videos/Queen - Bohemian Rhapsody.mkv",
    ):
        found = resolve(conn, relpath)

        assert found.preferred_format == ""
        assert found.explanation == "No format preference is configured for Video."


# --------------------------------------------------------------------------
# 6-12: scopes and precedence
# --------------------------------------------------------------------------


def test_a_category_scope_resolves_for_every_file_in_it(
    conn: sqlite3.Connection,
) -> None:
    set_preferred_format(conn, "photos", "jpeg")

    assert resolve(conn, "Photos/2024/IMG_0001.CR3").preferred_format == "jpeg"
    assert resolve(conn, "Music/Rock/01 - Song.flac").preferred_format == "mp3"


def test_a_folder_scope_outranks_its_category(conn: sqlite3.Connection) -> None:
    protect_folder(conn, "Music/Family Recordings")

    inside = resolve(conn, "Music/Family Recordings/Grandad 1998.wav")
    outside = resolve(conn, "Music/Rock/Queen/01 - Song.flac")

    assert inside.protected_original is True
    assert inside.protected_by == "Music/Family Recordings"
    assert inside.protection_kind == BY_POLICY
    #  And the category preference survives underneath it: protecting one
    #  folder must not silently repeal `Music → MP3` for the rest.
    assert inside.preferred_format == "mp3"
    assert outside.protected_original is False


def test_a_nested_folder_scope_outranks_its_parent(conn: sqlite3.Connection) -> None:
    """Most specific wins, per field, and that has to work in both directions.

    Protecting `Photos/Wedding` and then saying `Photos/Wedding/Exports` is
    *not* protected is a thing somebody may reasonably want — the exports are
    disposable and the originals are not.
    """
    protect_folder(conn, "Photos/Wedding")
    protect_folder(conn, "Photos/Wedding/Exports", preserve=False)

    original = resolve(conn, "Photos/Wedding/IMG_0001.CR3")
    export = resolve(conn, "Photos/Wedding/Exports/IMG_0001.jpg")

    assert original.protected_original is True
    assert export.protected_original is False


def test_preserve_originals_is_reported_with_the_folder_that_says_so(
    conn: sqlite3.Connection,
) -> None:
    protect_folder(conn, "Photos/Wedding")

    found = resolve(conn, "Photos/Wedding/2019/IMG_0001.CR3")

    assert found.explanation == (
        "This file is inside Photos/Wedding, which is set to preserve originals."
    )


def test_a_similarly_named_folder_is_not_protected(conn: sqlite3.Connection) -> None:
    """`Photos/Wedding` must not protect `Photos/WeddingExports`.

    A `startswith` comparison says it does, which is a protection somebody
    believes they have and does not.
    """
    protect_folder(conn, "Photos/Wedding")

    assert resolve(conn, "Photos/WeddingExports/IMG_0001.jpg").protected_original is False
    assert resolve(conn, "Photos/Holiday/IMG_0001.jpg").protected_original is False


def test_folder_scopes_are_stored_library_relative(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """This database gets restored onto other machines.

    A policy that stops applying because a mount point moved is a protection
    nobody has and everybody believes in.
    """
    settings = settings_for(tmp_path)
    (settings.library_dir / "Photos" / "Wedding").mkdir(parents=True)

    protect_folder(conn, "/Photos/Wedding/", library_dir=settings.library_dir)

    assert protected_folders(conn) == ["Photos/Wedding"]
    assert resolve(conn, "photos/wedding/IMG_1.CR3").protected_original is True


def test_a_folder_outside_the_library_is_refused(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    settings = settings_for(tmp_path)

    for bad in ("../etc", "/etc/passwd", "Photos/../../etc", "~/secrets"):
        with pytest.raises(PolicyError):
            protect_folder(conn, bad, library_dir=settings.library_dir)

    assert protected_folders(conn) == []


def test_protecting_a_folder_that_is_not_there_is_refused(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """A typo must fail while somebody can still see it fail.

    `Photos/Weding` saved quietly is a protection that reads correctly in
    Settings and covers nothing at all.
    """
    settings = settings_for(tmp_path)
    (settings.library_dir / "Photos" / "Wedding").mkdir(parents=True)

    with pytest.raises(PolicyError, match="no folder called"):
        protect_folder(conn, "Photos/Weding", library_dir=settings.library_dir)

    assert protected_folders(conn) == []


def test_an_unknown_format_for_a_category_is_refused(
    conn: sqlite3.Connection,
) -> None:
    with pytest.raises(PolicyError):
        set_preferred_format(conn, "music", "mkv")
    with pytest.raises(PolicyError):
        set_preferred_format(conn, "photos", "flac")
    with pytest.raises(PolicyError):
        set_preferred_format(conn, "projects", "zip")


def test_clearing_a_preference_is_a_real_answer(conn: sqlite3.Connection) -> None:
    set_preferred_format(conn, "music", "")

    assert preferred_for(conn, "music") == ""
    assert resolve(conn, "Music/Rock/01 - Song.flac").preferred_format == ""


def test_removing_a_protected_folder_leaves_no_scope_behind(
    conn: sqlite3.Connection,
) -> None:
    protect_folder(conn, "Photos/Wedding")
    unprotect_folder(conn, "Photos/Wedding")

    assert protected_folders(conn) == []
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM format_policy_scopes WHERE scope_kind='folder'"
        ).fetchone()[0]
        == 0
    )


def test_transform_permission_is_separate_from_preferred_representation(
    conn: sqlite3.Connection,
) -> None:
    """Two questions that look like one and are not.

    "MP3 is the copy I want to keep" says nothing about whether LibrAIry may
    go and *make* MP3s. Collapsing them would turn a preference among files
    somebody already has into a licence to re-encode a library.
    """
    found = resolve(conn, "Music/Rock/01 - Song.flac")
    assert found.preferred_format == "mp3"
    #  Unstated is the default everywhere, and it is not "no". It is what keeps
    #  this table from changing any behaviour the day it appears.
    assert found.allow_lossy is None
    assert found.allow_lossless is None

    set_transforms(conn, "music", lossy=False)

    after = resolve(conn, "Music/Rock/01 - Song.flac")
    assert after.allow_lossy is False
    assert after.preferred_format == "mp3"


def test_an_existing_protected_root_protects_here_too_and_says_which(
    conn: sqlite3.Connection,
) -> None:
    """Two protections of different strength, named apart.

    `optimization.protected_roots` stops a folder being queued for change at
    all. A Format Policy scope stops its originals being traded away by a
    representation preference. The stronger one certainly implies the weaker,
    but a page that said only "protected" would leave somebody unable to tell
    which power they had configured.
    """
    from librairy.protected import set_protected_roots

    set_protected_roots(conn, ["Photos/Memories"])

    found = resolve(conn, "Photos/Memories/2024/IMG_0001.HEIC")

    assert found.protected_original is True
    assert found.protection_kind == BY_ROOT
    assert "protected root" in found.explanation


def test_policy_never_decides_whether_two_files_are_the_same_thing(
    conn: sqlite3.Connection,
) -> None:
    """Identity comes first, and from somewhere else.

    A live take and a studio take are two recordings whatever their formats.
    The resolver has no opinion about that, and must not grow one — so it is
    asserted structurally: nothing here imports the identity machinery.
    """
    import librairy.format_policy as module

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    imported = {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    imported |= {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }

    for forbidden in (
        "librairy.catalogs",
        "librairy.track_identity",
        "librairy.document_works",
        "librairy.similar_media",
        "librairy.decisions",
    ):
        assert forbidden not in imported


def test_resolving_a_policy_touches_no_network(conn: sqlite3.Connection) -> None:
    """`tests/conftest.py` fails any socket. This is the assertion that the
    resolver is a database read and nothing else."""
    protect_folder(conn, "Photos/Wedding")
    set_preferred_format(conn, "music", "flac")

    assert resolve(conn, "Photos/Wedding/IMG_1.CR3").protected_original is True
    assert resolve(conn, "Music/Rock/01 - Song.mp3").preferred_format == "flac"


# --------------------------------------------------------------------------
# Bounded whatever the size of the page or the number of scopes
# --------------------------------------------------------------------------


def test_resolving_a_page_of_files_reads_the_scopes_once(
    conn: sqlite3.Connection,
) -> None:
    """A policy lookup per row is the N+1 that makes a comparison page slower
    the more folders somebody has protected — which would be the worst possible
    reason for a page to get worse."""
    from librairy.format_policy import protected_among

    for index in range(200):
        conn.execute(
            "INSERT INTO format_policy_scopes(scope_kind, scope_value,"
            " preserve_originals, created_at, updated_at)"
            " VALUES ('folder', ?, 1, 'now', 'now')",
            (f"Photos/Trip {index:03d}",),
        )
    paths = [f"Photos/Trip {index:03d}/IMG_1.jpg" for index in range(50)]

    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        many = protected_among(conn, paths)
        for_many = len(statements)
        statements.clear()
        few = protected_among(conn, paths[:1])
        for_few = len(statements)
    finally:
        conn.set_trace_callback(None)

    assert all(policy.protected_original for policy in many.values())
    assert len(few) == 1
    #  Same number of queries for fifty paths as for one.
    assert for_many == for_few


def test_ten_thousand_scopes_still_resolve_from_one_read(
    conn: sqlite3.Connection,
) -> None:
    from librairy.format_policy import protected_among

    conn.executemany(
        "INSERT INTO format_policy_scopes(scope_kind, scope_value,"
        " preserve_originals, created_at, updated_at)"
        " VALUES ('folder', ?, 1, 'now', 'now')",
        [(f"Photos/Trip {index:05d}",) for index in range(10_000)],
    )

    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        found = protected_among(
            conn, [f"Photos/Trip {index:05d}/IMG_1.jpg" for index in range(500)]
        )
    finally:
        conn.set_trace_callback(None)

    assert len(found) == 500
    assert all(policy.protected_original for policy in found.values())
    assert len(statements) < 10
