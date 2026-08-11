"""Can the immutable plan and undo carry a library -> library correction?

This existed as a throwaway probe to answer one question before building
Library Audit on top of an assumption. The answer was yes, and the checks are
worth keeping: they are the contract any future "accept this correction"
button depends on, and the day one of them stops holding is the day that
button has to stop existing.

The executor turns out to be root-agnostic already. `OperationSpec.src_root`
merely defaults to "inbox"; every guarantee below comes from code that never
asked which root it was working in.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from librairy.config import Settings
from librairy.db import connect
from librairy.executor import execute_plan
from librairy.fingerprint import blake2b_file
from librairy.history import undo_op
from librairy.paths import PathValidationError
from librairy.planner import OperationSpec, PlanError, add_plan_op, approve_plan, create_plan
from librairy.scanner import scan_root

DEST = "Music/Rock/Queen/A Night at the Opera/05 - You're My Best Friend.mp3"


def settings_for(tmp_path: Path) -> Settings:
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
    return settings


def library(tmp_path: Path, *relpaths: str):
    settings = settings_for(tmp_path)
    conn = connect(settings)
    for relpath in relpaths:
        path = settings.library_dir / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relpath, encoding="utf-8")
    (settings.library_dir / "Music/Rock/Queen/A Night at the Opera").mkdir(
        parents=True, exist_ok=True
    )
    scan_root(conn, "library", settings.library_dir, settings)
    return conn, settings


def correction(src: str = "Music/Pop/random-song.mp3", dest: str = DEST) -> OperationSpec:
    return OperationSpec(
        op_type="move",
        src_root="library",
        src_relpath=src,
        dest_root="library",
        dest_relpath=dest,
    )


def staged(tmp_path: Path, *relpaths: str):
    conn, settings = library(tmp_path, *relpaths)
    plan_id = create_plan(conn, [correction()], settings)
    approve_plan(conn, plan_id, settings)
    return conn, settings, plan_id


SRC = "Music/Pop/random-song.mp3"


def test_a_plan_accepts_a_library_source(tmp_path: Path) -> None:
    conn, settings = library(tmp_path, SRC)

    plan_id = create_plan(conn, [correction()], settings)

    op = conn.execute("SELECT * FROM plan_ops WHERE plan_id=?", (plan_id,)).fetchone()
    assert op["src_root"] == "library"
    assert op["src_fingerprint"] == blake2b_file(settings.library_dir / SRC)


def test_approval_validates_a_library_source(tmp_path: Path) -> None:
    conn, settings = library(tmp_path, SRC)
    plan_id = create_plan(conn, [correction()], settings)

    assert approve_plan(conn, plan_id, settings)


def test_containment_still_refuses_an_escaping_destination(tmp_path: Path) -> None:
    conn, settings = library(tmp_path, SRC)

    with pytest.raises((PathValidationError, PlanError)):
        create_plan(conn, [correction(dest="../escape.mp3")], settings)


def test_the_correction_moves_the_bytes_intact(tmp_path: Path) -> None:
    conn, settings, plan_id = staged(tmp_path, SRC)
    original = blake2b_file(settings.library_dir / SRC)

    summary = execute_plan(conn, plan_id, settings)

    assert summary.done == 1
    assert not (settings.library_dir / SRC).exists()
    assert blake2b_file(settings.library_dir / DEST) == original


def test_the_index_follows_the_correction(tmp_path: Path) -> None:
    conn, settings, plan_id = staged(tmp_path, SRC)
    item_id = conn.execute("SELECT id FROM items").fetchone()["id"]

    execute_plan(conn, plan_id, settings)

    row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    assert row["root"] == "library"
    assert row["relpath"] == DEST
    fts = conn.execute("SELECT name FROM search_fts WHERE item_id=?", (item_id,)).fetchone()
    assert "Best Friend" in fts["name"]


def test_history_records_it_as_library_to_library(tmp_path: Path) -> None:
    """What lets History tell a correction from a newly filed inbox file."""
    conn, settings, plan_id = staged(tmp_path, SRC)

    execute_plan(conn, plan_id, settings)

    entry = conn.execute("SELECT * FROM history WHERE plan_id=?", (plan_id,)).fetchone()
    assert (entry["src_root"], entry["dest_root"]) == ("library", "library")
    assert entry["outcome"] == "ok"


def test_undo_returns_the_file_to_its_exact_original_path(tmp_path: Path) -> None:
    """This matters more here than for an inbox commit: the original location
    is somewhere the owner chose, not a staging folder."""
    conn, settings, plan_id = staged(tmp_path, SRC)
    original = blake2b_file(settings.library_dir / SRC)
    execute_plan(conn, plan_id, settings)
    entry = conn.execute("SELECT id FROM history WHERE plan_id=?", (plan_id,)).fetchone()

    result = undo_op(conn, entry["id"], settings)

    assert result.outcome == "ok"
    assert (settings.library_dir / SRC).exists()
    assert blake2b_file(settings.library_dir / SRC) == original
    assert not (settings.library_dir / DEST).exists()
    row = conn.execute("SELECT relpath FROM items").fetchone()
    assert row["relpath"] == SRC


def test_a_collision_never_overwrites(tmp_path: Path) -> None:
    conn, settings = library(tmp_path, SRC, DEST)
    occupied = blake2b_file(settings.library_dir / DEST)
    plan_id = create_plan(conn, [correction()], settings)
    approve_plan(conn, plan_id, settings)

    summary = execute_plan(conn, plan_id, settings)

    assert summary.renamed_collision == 1
    assert blake2b_file(settings.library_dir / DEST) == occupied


def test_a_source_edited_after_approval_is_skipped(tmp_path: Path) -> None:
    conn, settings, plan_id = staged(tmp_path, SRC)
    (settings.library_dir / SRC).write_text("edited since approval", encoding="utf-8")

    summary = execute_plan(conn, plan_id, settings)

    assert summary.skipped_changed == 1
    assert (settings.library_dir / SRC).exists()
    assert not (settings.library_dir / DEST).exists()


def test_a_source_that_vanished_after_approval_is_skipped(tmp_path: Path) -> None:
    conn, settings, plan_id = staged(tmp_path, SRC)
    (settings.library_dir / SRC).unlink()

    summary = execute_plan(conn, plan_id, settings)

    assert summary.skipped_missing == 1


def test_an_approved_correction_cannot_be_edited(tmp_path: Path) -> None:
    conn, settings, plan_id = staged(tmp_path, SRC)

    with pytest.raises(PlanError):
        add_plan_op(conn, plan_id, 99, correction(dest="Music/Rock/other.mp3"), settings)


def test_a_companion_only_travels_when_the_plan_names_it(tmp_path: Path) -> None:
    """The executor moves exactly what it was told to. Keeping an album
    together is the caller's job, which is why an audit correction has to emit
    an op per companion rather than trusting the move to bring them along.
    """
    conn, settings = library(tmp_path, SRC, "Music/Pop/random-song.lrc")
    plan_id = create_plan(conn, [correction()], settings)
    approve_plan(conn, plan_id, settings)

    execute_plan(conn, plan_id, settings)

    assert (settings.library_dir / "Music/Pop/random-song.lrc").exists()
