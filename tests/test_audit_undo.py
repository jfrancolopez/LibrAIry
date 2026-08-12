"""Undo matters more for a correction than for an inbox filing.

An inbox undo puts a file back in a staging folder. A correction undo puts it
back somewhere the owner chose, in a library they were already happy with —
so "the exact original path, with the exact original bytes" is not a nicety,
it is the reason accepting a correction is a safe thing to do at all.
"""

from __future__ import annotations

from pathlib import Path

from test_audit_corrections import TRACK, TRACK_DEST, finding_for, library

from librairy.corrections import accept_correction, undo_correction
from librairy.executor import execute_plan
from librairy.fingerprint import blake2b_file

LYRICS = "Music/Pop/Queen/05 - Song.lrc"
LYRICS_DEST = "Music/Rock/Queen/Album/05 - Song.lrc"


def committed(tmp_path: Path, *relpaths: str):
    conn, settings = library(tmp_path, *relpaths)
    before = {
        relpath: blake2b_file(settings.library_dir / relpath) for relpath in relpaths
    }
    finding = finding_for(conn, TRACK, TRACK_DEST)
    plan_id = accept_correction(conn, settings, finding["id"])
    summary = execute_plan(conn, plan_id, settings)
    return conn, settings, plan_id, before, summary


def test_a_single_file_correction_undoes_exactly(tmp_path: Path) -> None:
    conn, settings, plan_id, before, _ = committed(tmp_path, TRACK)

    results = undo_correction(conn, settings, plan_id)

    assert [result.outcome for result in results] == ["ok"]
    assert (settings.library_dir / TRACK).is_file()
    assert not (settings.library_dir / TRACK_DEST).exists()
    assert blake2b_file(settings.library_dir / TRACK) == before[TRACK]


def test_a_correction_with_companions_undoes_every_file(tmp_path: Path) -> None:
    conn, settings, plan_id, before, summary = committed(tmp_path, TRACK, LYRICS)
    assert summary.done == 2

    results = undo_correction(conn, settings, plan_id)

    assert [result.outcome for result in results] == ["ok", "ok"]
    for relpath, fingerprint in before.items():
        restored = settings.library_dir / relpath
        assert restored.is_file(), relpath
        assert blake2b_file(restored) == fingerprint, relpath


def test_the_corrected_folder_is_left_empty_of_the_moved_files(tmp_path: Path) -> None:
    conn, settings, plan_id, _, _ = committed(tmp_path, TRACK, LYRICS)

    undo_correction(conn, settings, plan_id)

    assert not (settings.library_dir / TRACK_DEST).exists()
    assert not (settings.library_dir / LYRICS_DEST).exists()


def test_the_index_follows_the_files_home(tmp_path: Path) -> None:
    conn, settings, plan_id, _, _ = committed(tmp_path, TRACK, LYRICS)

    undo_correction(conn, settings, plan_id)

    relpaths = {
        row["relpath"]
        for row in conn.execute("SELECT relpath FROM items WHERE root='library'")
    }
    assert {TRACK, LYRICS} <= relpaths


def test_undo_refuses_a_file_edited_since_the_correction(tmp_path: Path) -> None:
    """The same guarantee as the forward direction, in reverse: undo will not
    overwrite work done after the move."""
    conn, settings, plan_id, _, _ = committed(tmp_path, TRACK)
    (settings.library_dir / TRACK_DEST).write_text("edited in its new home", encoding="utf-8")

    results = undo_correction(conn, settings, plan_id)

    assert results[0].outcome.startswith("undo_refused_changed")
    assert (settings.library_dir / TRACK_DEST).is_file()


def test_undo_never_overwrites_something_at_the_original_path(tmp_path: Path) -> None:
    conn, settings, plan_id, before, _ = committed(tmp_path, TRACK)
    replacement = settings.library_dir / TRACK
    replacement.parent.mkdir(parents=True, exist_ok=True)
    replacement.write_text("something else now lives here", encoding="utf-8")
    occupied = blake2b_file(replacement)

    undo_correction(conn, settings, plan_id)

    assert blake2b_file(replacement) == occupied
    siblings = sorted(path.name for path in replacement.parent.iterdir())
    assert len(siblings) == 2


def test_the_journal_still_reads_as_a_library_correction_afterwards(
    tmp_path: Path,
) -> None:
    """Undo adds entries; it does not rewrite what happened."""
    conn, settings, plan_id, _, _ = committed(tmp_path, TRACK, LYRICS)

    undo_correction(conn, settings, plan_id)

    moves = conn.execute(
        "SELECT * FROM history WHERE plan_id=? AND action='move' ORDER BY id", (plan_id,)
    ).fetchall()
    undos = conn.execute(
        "SELECT * FROM history WHERE plan_id=? AND action='undo_move'", (plan_id,)
    ).fetchall()
    assert len(moves) == 2
    assert all((row["src_root"], row["dest_root"]) == ("library", "library") for row in moves)
    assert len(undos) == 2
    plan = conn.execute("SELECT audit_finding_id FROM plans WHERE id=?", (plan_id,)).fetchone()
    assert plan["audit_finding_id"] is not None
