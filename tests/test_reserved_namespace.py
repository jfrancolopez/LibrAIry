"""One relpath in the database is not an address on disk, so it is reserved.

The dormant result of an un-adopted optimization keeps an `items` row in the
`library` root — `items.root` allows no other value — and cannot keep the
library path it used to hold, because `UNIQUE (root, relpath)` is a table
constraint and an HEVC re-encode of an MP4 lands on the original's own path.

The first version of that parking address was `_optimization/<job>/<path>`,
which is a folder somebody could plausibly create. These tests are about the
replacement being genuinely reserved rather than merely unlikely.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from librairy.config import Settings
from librairy.consistency import consistency_view, library_consistency
from librairy.db import connect
from librairy.optimization_adopt import parked_relpath
from librairy.paths import validate_dest, validate_relpath
from librairy.reserved import (
    DORMANT_OPTIMIZATION,
    RESERVED_TOP,
    ReservedPathError,
    is_dormant_optimization,
    is_reserved,
    refuse_reserved,
)
from librairy.scanner import scan_root


@pytest.fixture
def library(tmp_path: Path):
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        FILE_STABILITY_SECONDS=0,
        _env_file=None,
    )
    for directory in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        directory.mkdir(parents=True)
    return connect(settings), settings


# --- the address ------------------------------------------------------------------


def test_the_parked_address_is_deterministic_and_carries_no_former_path() -> None:
    assert parked_relpath(7, 42) == f"{DORMANT_OPTIMIZATION}/7/42"
    assert parked_relpath(7, 42) == parked_relpath(7, 42)
    # Keyed by both ids, so it is stable across any number of cycles and unique
    # per result.
    assert parked_relpath(7, 42) != parked_relpath(8, 42) != parked_relpath(8, 43)


def test_the_address_is_recognisable_as_reserved_and_as_dormant() -> None:
    parked = parked_relpath(1, 1)

    assert is_reserved(parked)
    assert is_dormant_optimization(parked)
    assert parked.startswith(f"{RESERVED_TOP}/")


@pytest.mark.parametrize(
    "relpath",
    [
        "Music/Live/concert.flac",
        "_optimization/1/x.flac",
        "__librairy_internal_/x",
        "not__librairy_internal__/x",
        "",
        None,
    ],
)
def test_ordinary_paths_are_not_reserved(relpath) -> None:
    assert not is_reserved(relpath)


def test_the_whole_subtree_is_reserved_not_just_the_exact_address() -> None:
    assert is_reserved(RESERVED_TOP)
    assert is_reserved(f"{RESERVED_TOP}/anything/at/all.mkv")
    assert is_reserved(f"{RESERVED_TOP}\\windows\\style")


# --- nothing real may be filed there -----------------------------------------------


def test_validate_dest_refuses_the_namespace(library) -> None:
    """One refusal, at the function every destination in the application goes
    through — a plan operation, a correction, an import, a quarantine restore
    and a typed destination all arrive here."""
    _conn, settings = library

    with pytest.raises(ReservedPathError):
        validate_dest(settings.library_dir, f"{RESERVED_TOP}/optimization-dormant/1/1")
    with pytest.raises(ReservedPathError):
        validate_dest(settings.library_dir, f"{RESERVED_TOP}/anything.mkv")
    # And in the other roots too, because reserving one name everywhere removes
    # the question "which roots does this apply to".
    with pytest.raises(ReservedPathError):
        validate_dest(settings.quarantine_dir, f"{RESERVED_TOP}/x")


def test_a_source_may_still_be_read_from_it(library) -> None:
    """Only destinations are refused. A read is how a mistake gets cleaned up,
    and refusing that would make a stuck file unrecoverable."""
    _conn, settings = library

    assert validate_relpath(
        settings.library_dir, f"{RESERVED_TOP}/x", kind="source"
    ).name == "x"


def test_the_planner_refuses_a_reserved_destination(library) -> None:
    from librairy.planner import OperationSpec, create_plan

    conn, settings = library
    (settings.inbox_dir / "song.flac").write_bytes(b"song")
    scan_root(conn, "inbox", settings.inbox_dir, settings)

    with pytest.raises(ReservedPathError):
        create_plan(
            conn,
            [OperationSpec("move", "song.flac", "library",
                           f"{RESERVED_TOP}/optimization-dormant/1/1")],
            settings,
        )

    assert conn.execute("SELECT COUNT(*) FROM plan_ops").fetchone()[0] == 0


def test_a_correction_cannot_target_the_namespace(library) -> None:
    from librairy.planner import OperationSpec, create_plan

    conn, settings = library
    (settings.library_dir / "film.mkv").write_bytes(b"film")
    scan_root(conn, "library", settings.library_dir, settings)

    with pytest.raises(ReservedPathError):
        create_plan(
            conn,
            [OperationSpec("move", "film.mkv", "library",
                           f"{RESERVED_TOP}/Movies/film.mkv", src_root="library")],
            settings,
        )


def test_refuse_reserved_names_the_kind_it_was_asked_about() -> None:
    with pytest.raises(ReservedPathError, match="destination"):
        refuse_reserved(f"{RESERVED_TOP}/x")
    with pytest.raises(ReservedPathError, match="proposal"):
        refuse_reserved(f"{RESERVED_TOP}/x", kind="proposal")
    refuse_reserved("Music/ok.flac")


# --- and a physical file there is reported, not hidden -------------------------------


def test_the_scanner_does_not_index_a_file_found_in_the_namespace(library) -> None:
    """Indexing it would either collide with a parked row through
    UNIQUE(root, relpath) or quietly take its address."""
    conn, settings = library
    reserved = settings.library_dir / RESERVED_TOP / "optimization-dormant" / "1"
    reserved.mkdir(parents=True)
    (reserved / "1").write_bytes(b"a file somebody put here")
    (settings.library_dir / "ordinary.flac").write_bytes(b"ordinary")

    summary = scan_root(conn, "library", settings.library_dir, settings)

    assert summary.reserved_skipped == 1
    assert [row["relpath"] for row in conn.execute("SELECT relpath FROM items")] == [
        "ordinary.flac"
    ]


def test_the_file_is_left_exactly_where_it_is(library) -> None:
    conn, settings = library
    reserved = settings.library_dir / RESERVED_TOP
    reserved.mkdir()
    intruder = reserved / "somebodys-file.mkv"
    intruder.write_bytes(b"do not touch this")

    scan_root(conn, "library", settings.library_dir, settings)

    assert intruder.read_bytes() == b"do not touch this"


def test_the_conflict_is_reported_with_its_own_remedy(library) -> None:
    """Not as drift. "Scan the library" would not index it, and a remedy that
    does nothing is worse than an explanation."""
    conn, settings = library
    reserved = settings.library_dir / RESERVED_TOP
    reserved.mkdir()
    (reserved / "somebodys-file.mkv").write_bytes(b"x")
    (settings.library_dir / "ordinary.flac").write_bytes(b"ordinary")
    scan_root(conn, "library", settings.library_dir, settings)

    state = library_consistency(conn, settings)

    assert state.reserved_files == 1
    assert state.unindexed_files == 0
    assert state.missing_files == 0
    assert not state.matches

    view = consistency_view(state)
    note = next(n for n in view["notes"] if RESERVED_TOP in n["text"])
    assert note["remedy"] is None
    assert "will not be indexed, moved or deleted" in note["text"]
    assert "reserved folder" in view["summary"]
    assert {"label": "Reserved name", "path": f"{RESERVED_TOP}/somebodys-file.mkv"} in (
        view["examples"]
    )


def test_a_clean_library_still_reports_a_match(library) -> None:
    conn, settings = library
    (settings.library_dir / "ordinary.flac").write_bytes(b"ordinary")
    scan_root(conn, "library", settings.library_dir, settings)

    state = library_consistency(conn, settings)

    assert state.matches
    assert state.reserved_files == 0


def test_a_parked_row_alone_is_not_reported_as_anything(library) -> None:
    """The row exists, its file does not, and neither bucket should mention it —
    that is the whole point of the dormant exclusion."""
    conn, settings = library
    (settings.library_dir / "ordinary.flac").write_bytes(b"ordinary")
    scan_root(conn, "library", settings.library_dir, settings)
    from librairy.planner import utc_now

    item_id = int(
        conn.execute(
            "INSERT INTO items(root, relpath, size, mtime_ns, fingerprint, state,"
            " first_seen_at, last_seen_at, missing_since)"
            " VALUES ('library', ?, 1, 1, 'x', 'discovered', ?, ?, ?)",
            (parked_relpath(1, 999), utc_now(), utc_now(), utc_now()),
        ).lastrowid
    )
    conn.execute(
        "INSERT INTO optimization_jobs(item_id, root, relpath, fingerprint, kind,"
        " quality, from_label, to_label, preset, source_bytes, estimated_bytes,"
        " actual_bytes, state, result_item_id, queued_at, updated_at)"
        " VALUES (NULL, 'library', 'x', 'x', 'audio-to-flac', 'lossless', 'W', 'F',"
        " 'flac-lossless', 1, 1, 1, 'ready', ?, ?, ?)",
        (item_id, utc_now(), utc_now()),
    )

    state = library_consistency(conn, settings)

    assert state.matches
    assert (state.reserved_files, state.missing_files, state.unindexed_files) == (0, 0, 0)


# --- and it never reaches a reader --------------------------------------------------


def test_the_namespace_appears_in_no_template(library) -> None:
    """It is bookkeeping. If a template ever renders it, somebody sees
    `__librairy_internal__` where a filename should be."""
    templates = Path("src/librairy/web/templates")

    for page in templates.rglob("*.html"):
        assert RESERVED_TOP not in page.read_text(encoding="utf-8"), page.name


def test_search_never_returns_a_parked_row(library) -> None:
    from librairy.planner import utc_now
    from librairy.search import search_items, sync_search_item

    conn, settings = library
    item_id = int(
        conn.execute(
            "INSERT INTO items(root, relpath, size, mtime_ns, fingerprint, state,"
            " first_seen_at, last_seen_at, missing_since)"
            " VALUES ('library', ?, 1, 1, 'x', 'discovered', ?, ?, ?)",
            (parked_relpath(3, 4), utc_now(), utc_now(), utc_now()),
        ).lastrowid
    )
    sync_search_item(conn, item_id)

    assert search_items(conn, "librairy") == []
    assert search_items(conn, "optimization") == []
