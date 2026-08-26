"""Reversing an old decision must not quietly reverse a newer one.

Undo has always been plan-scoped, and for a long time that was the whole truth.
LibrAIry now produces sequences — file, then correct, then replace; set aside,
then restore, then correct one of them — and each step is an explicit choice
somebody made. Blind reversal of the first invalidates the ones after it, which
is the last place in the program where a decision can be overwritten without
anybody being asked.

The tests that matter most are the ones asserting a plan is *not* blocked. A
sequence check that stopped every old Undo because newer history exists would
be safe and useless, and would be switched off within a week.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from librairy.config import Settings
from librairy.db import connect
from librairy.executor import execute_plan
from librairy.history import undo_plan
from librairy.planner import OperationSpec, approve_plan, create_plan
from librairy.scanner import scan_root
from librairy.undo_sequence import (
    BLOCKED,
    CLEAR,
    DRIFTED,
    UNDONE,
    UNKNOWN,
    sequence,
    sequences,
)


def settings_for(tmp_path: Path) -> Settings:
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        FILE_STABILITY_SECONDS=0,
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
def scene(tmp_path: Path) -> tuple[sqlite3.Connection, Settings]:
    settings = settings_for(tmp_path)
    return connect(settings), settings


def arrive(settings: Settings, relpath: str, body: bytes = b"the bytes") -> None:
    path = settings.inbox_dir / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)


def filed(settings: Settings, relpath: str, body: bytes = b"the bytes") -> None:
    path = settings.library_dir / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)


def commit(
    conn: sqlite3.Connection,
    settings: Settings,
    specs: list[OperationSpec],
    *,
    coherent: bool = False,
) -> str:
    """One decision, taken and carried out."""
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    scan_root(conn, "library", settings.library_dir, settings)
    scan_root(conn, "quarantine", settings.quarantine_dir, settings)
    plan_id = create_plan(conn, specs, settings)
    if coherent:
        conn.execute("UPDATE plans SET coherent=1 WHERE id=?", (plan_id,))
    approve_plan(conn, plan_id, settings)
    summary = execute_plan(conn, plan_id, settings)
    assert summary.done == len(specs), summary
    return plan_id


def file_it(
    conn: sqlite3.Connection, settings: Settings, name: str, dest: str
) -> str:
    arrive(settings, name)
    return commit(
        conn,
        settings,
        [OperationSpec("move", name, "library", dest, src_root="inbox")],
    )


def move_it(
    conn: sqlite3.Connection, settings: Settings, source: str, dest: str
) -> str:
    return commit(
        conn,
        settings,
        [OperationSpec("move", source, "library", dest, src_root="library")],
    )


# --------------------------------------------------------------------------
# 1-5: the chain
# --------------------------------------------------------------------------


def test_an_unrelated_later_decision_does_not_block_anything(
    scene: tuple[sqlite3.Connection, Settings],
) -> None:
    """The test that keeps this feature usable.

    A sequence check that blocked every old Undo because newer history exists
    would be safe, useless, and switched off within a week.
    """
    conn, settings = scene
    first = file_it(conn, settings, "foo.jpg", "Photos/2024/foo.jpg")
    second = file_it(conn, settings, "bar.mp3", "Music/Rock/bar.mp3")

    found = sequences(conn, [first, second])

    assert found[first].state == CLEAR
    assert found[second].state == CLEAR
    assert found[first].explanation == ""


def test_a_later_move_of_the_same_file_blocks_the_earlier_decision(
    scene: tuple[sqlite3.Connection, Settings],
) -> None:
    conn, settings = scene
    filing = file_it(conn, settings, "foo.jpg", "Photos/2024/foo.jpg")
    move_it(conn, settings, "Photos/2024/foo.jpg", "Photos/2024/Trip/foo.jpg")

    found = sequence(conn, filing)

    assert found.state == BLOCKED
    assert found.blockers[0].shared == 1
    assert "Reverse that one first." in found.explanation


def test_a_chain_unwinds_in_reverse_and_only_in_reverse(
    scene: tuple[sqlite3.Connection, Settings],
) -> None:
    """A → B → C. The natural workflow, without a graph editor."""
    conn, settings = scene
    a = file_it(conn, settings, "foo.jpg", "Photos/foo.jpg")
    b = move_it(conn, settings, "Photos/foo.jpg", "Photos/2024/foo.jpg")
    c = move_it(conn, settings, "Photos/2024/foo.jpg", "Photos/2024/Trip/foo.jpg")

    found = sequences(conn, [a, b, c])
    assert [found[a].state, found[b].state, found[c].state] == [
        BLOCKED, BLOCKED, CLEAR
    ]

    undo_plan(conn, c, settings)
    found = sequences(conn, [a, b, c])
    assert [found[a].state, found[b].state, found[c].state] == [
        BLOCKED, CLEAR, UNDONE
    ]

    undo_plan(conn, b, settings)
    found = sequences(conn, [a, b, c])
    assert [found[a].state, found[b].state, found[c].state] == [
        CLEAR, UNDONE, UNDONE
    ]

    undo_plan(conn, a, settings)
    assert (settings.inbox_dir / "foo.jpg").is_file()
    assert sequence(conn, a).state == UNDONE


def test_nothing_cascades_on_its_own(
    scene: tuple[sqlite3.Connection, Settings],
) -> None:
    """Reversing one decision to make room for another crosses two explicit
    choices. This names the later one and stops."""
    conn, settings = scene
    filing = file_it(conn, settings, "foo.jpg", "Photos/foo.jpg")
    later = move_it(conn, settings, "Photos/foo.jpg", "Photos/2024/foo.jpg")

    found = sequence(conn, filing)

    assert found.state == BLOCKED
    #  It reports. It does not reverse.
    assert sequence(conn, later).state == CLEAR
    assert (settings.library_dir / "Photos" / "2024" / "foo.jpg").is_file()


def test_reading_a_file_is_not_a_dependency(
    scene: tuple[sqlite3.Connection, Settings],
) -> None:
    """Audits, metadata measurements, relationship discovery and decision
    memory all read. None of them is a filesystem decision, so none of them can
    stand in the way of reversing one."""
    from librairy.decisions import Cue, record
    from librairy.planner import utc_now
    from librairy.relationships import SUBTITLE
    from librairy.relationships import record as relate
    from librairy.tools.common import IMAGE_TOOL, set_cached_metadata

    conn, settings = scene
    filing = file_it(conn, settings, "foo.jpg", "Photos/foo.jpg")
    item = conn.execute(
        "SELECT id, fingerprint FROM items WHERE relpath='Photos/foo.jpg'"
    ).fetchone()
    other = file_it(conn, settings, "foo.srt", "Photos/foo.srt")
    assert other

    set_cached_metadata(
        conn, int(item["id"]), str(item["fingerprint"]), IMAGE_TOOL,
        {"camera": "x", "taken": "", "content_id": "", "unique_id": ""}, utc_now(),
    )
    relate(
        conn,
        companion_item_id=int(
            conn.execute(
                "SELECT id FROM items WHERE relpath='Photos/foo.srt'"
            ).fetchone()["id"]
        ),
        subject_item_id=int(item["id"]),
        kind=SUBTITLE,
        provenance="names the same file",
    )
    record(conn, cue=Cue("destination", {"category": "photos"}), outcome="Photos")
    from librairy.audit import Finding, record_findings

    record_findings(
        conn,
        [Finding(kind="naming", severity="low", relpath="Photos/foo.jpg",
                 summary="a name to tidy", evidence=[])],
    )

    assert sequence(conn, filing).state == CLEAR


# --------------------------------------------------------------------------
# 6-11: the real shapes LibrAIry produces
# --------------------------------------------------------------------------


def test_a_replacement_chain_is_detected(
    scene: tuple[sqlite3.Connection, Settings],
) -> None:
    """FLAC to Quarantine, MP3 takes the slot, then the MP3 is reorganised.

    Undoing the replacement would have to put the FLAC back where the MP3 no
    longer is.
    """
    conn, settings = scene
    filed(settings, "Music/Rock/01 - Song.flac", b"lossless bytes")
    filed(settings, "Music/Rock/01 - Song.mp3", b"lossy bytes")
    swap = commit(
        conn,
        settings,
        [
            OperationSpec(
                "quarantine", "Music/Rock/01 - Song.flac", "quarantine",
                "2026/01 - Song.flac", src_root="library",
            ),
            OperationSpec(
                "move", "Music/Rock/01 - Song.mp3", "library",
                "Music/Rock/Queen/01 - Song.mp3", src_root="library",
            ),
        ],
        coherent=True,
    )
    assert sequence(conn, swap).state == CLEAR

    move_it(
        conn, settings,
        "Music/Rock/Queen/01 - Song.mp3",
        "Music/Rock/Queen/A Night at the Opera/01 - Song.mp3",
    )

    found = sequence(conn, swap)
    assert found.state == BLOCKED
    assert found.operations == 2
    assert found.affected == 1


def test_a_restore_blocks_the_decision_it_reversed_the_effect_of(
    scene: tuple[sqlite3.Connection, Settings],
) -> None:
    """Set aside, then restore. The files are back, and the set-aside plan's
    journal points at quarantine paths that no longer hold them."""
    conn, settings = scene
    for index in range(3):
        filed(settings, f"Photos/Trip/IMG_{index}.jpg", f"photo {index}".encode())
    aside = commit(
        conn,
        settings,
        [
            OperationSpec(
                "quarantine", f"Photos/Trip/IMG_{index}.jpg", "quarantine",
                f"2026-08-25/IMG_{index}.jpg", src_root="library",
            )
            for index in range(3)
        ],
        coherent=True,
    )
    commit(
        conn,
        settings,
        [
            OperationSpec(
                "move", f"2026-08-25/IMG_{index}.jpg", "library",
                f"Photos/Trip/IMG_{index}.jpg", src_root="quarantine",
            )
            for index in range(3)
        ],
        coherent=True,
    )

    found = sequence(conn, aside)

    assert found.state == BLOCKED
    assert found.affected == 3


def test_one_dependent_member_blocks_a_whole_coherent_decision(
    scene: tuple[sqlite3.Connection, Settings],
) -> None:
    """Reversing the plan reverses all eighteen. Seventeen being untouched does
    not make that safe, and the sentence says so."""
    conn, settings = scene
    for index in range(18):
        arrive(settings, f"card/IMG_{index:02d}.jpg", f"photo {index}".encode())
    filing = commit(
        conn,
        settings,
        [
            OperationSpec(
                "move", f"card/IMG_{index:02d}.jpg", "library",
                f"Photos/2024/IMG_{index:02d}.jpg", src_root="inbox",
            )
            for index in range(18)
        ],
        coherent=True,
    )
    move_it(
        conn, settings, "Photos/2024/IMG_07.jpg", "Photos/2024/Trip/IMG_07.jpg"
    )

    found = sequence(conn, filing)

    assert found.state == BLOCKED
    assert (found.affected, found.operations) == (1, 18)
    assert "1 of 18 files" in found.explanation


def test_the_explanation_names_the_later_decision(
    scene: tuple[sqlite3.Connection, Settings],
) -> None:
    conn, settings = scene
    filing = file_it(conn, settings, "foo.jpg", "Photos/foo.jpg")
    move_it(conn, settings, "Photos/foo.jpg", "Photos/2024/foo.jpg")

    found = sequence(conn, filing)

    assert found.blockers[0].summary
    assert found.blockers[0].when
    assert found.blockers[0].plan_id != filing


def test_drift_is_reported_separately_from_sequence(
    scene: tuple[sqlite3.Connection, Settings], tmp_path: Path
) -> None:
    """Two different questions, both of which must be yes.

    Sequence asks whether reversing this would reverse somebody else's
    decision. Preflight asks whether the files are still what we left. Only the
    second one hashes, which is why it happens behind a button.
    """
    conn, settings = scene
    filing = file_it(conn, settings, "foo.jpg", "Photos/foo.jpg")
    assert sequence(conn, filing, settings=settings).state == CLEAR

    (settings.library_dir / "Photos" / "foo.jpg").write_bytes(b"edited by hand")

    assert sequence(conn, filing).state == CLEAR
    assert sequence(conn, filing, settings=settings).state == DRIFTED
    assert tmp_path


def test_a_journal_that_disagrees_with_its_plan_refuses_rather_than_guessing(
    scene: tuple[sqlite3.Connection, Settings],
) -> None:
    """The one genuinely ambiguous case.

    A plan that has operations recorded and journal rows that cannot be tied to
    any of them leaves no way to know which files those rows were about.
    "Probably independent" is not a thing to say before moving files.
    """
    conn, settings = scene
    filing = file_it(conn, settings, "foo.jpg", "Photos/foo.jpg")
    conn.execute("UPDATE history SET op_id=NULL WHERE plan_id=?", (filing,))

    found = sequence(conn, filing)

    assert found.state == UNKNOWN
    assert "cannot tell" in found.explanation


def test_an_old_journal_without_item_ids_is_still_analysable(
    scene: tuple[sqlite3.Connection, Settings],
) -> None:
    """The other half of the same rule.

    A journal from before operations carried item ids is not ambiguous — a
    later decision that read exactly where an earlier one wrote is a handover,
    whatever identity is available. Refusing every historical Undo would be
    safe and useless.
    """
    conn, settings = scene
    filing = file_it(conn, settings, "foo.jpg", "Photos/foo.jpg")
    other = file_it(conn, settings, "bar.jpg", "Photos/bar.jpg")
    conn.execute("UPDATE plan_ops SET item_id=NULL")

    assert sequence(conn, filing).state == CLEAR
    assert sequence(conn, other).state == CLEAR

    move_it(conn, settings, "Photos/foo.jpg", "Photos/2024/foo.jpg")
    conn.execute("UPDATE plan_ops SET item_id=NULL")

    #  Found by continuation: the later decision took the file from exactly
    #  where this one put it.
    assert sequence(conn, filing).state == BLOCKED
    assert sequence(conn, other).state == CLEAR


def test_a_historical_plan_with_a_linkable_journal_is_not_gratuitously_blocked(
    scene: tuple[sqlite3.Connection, Settings],
) -> None:
    """An old plan whose journal proves safety stays undoable."""
    conn, settings = scene
    filing = file_it(conn, settings, "foo.jpg", "Photos/foo.jpg")
    conn.execute("UPDATE plans SET finished_at='2019-01-01T00:00:00+00:00'")

    assert sequence(conn, filing).state == CLEAR


# --------------------------------------------------------------------------
# Bounded
# --------------------------------------------------------------------------


def test_a_page_of_fifty_plans_costs_a_fixed_number_of_queries(
    scene: tuple[sqlite3.Connection, Settings],
) -> None:
    """One recursive query per card, each scanning every operation ever
    executed, is the shape that works on a fixture and stops on a real
    journal."""
    conn, settings = scene
    plans = [
        file_it(conn, settings, f"f{index}.jpg", f"Photos/f{index}.jpg")
        for index in range(50)
    ]

    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        many = sequences(conn, plans)
        for_many = len(statements)
        statements.clear()
        few = sequences(conn, plans[:1])
        for_few = len(statements)
    finally:
        conn.set_trace_callback(None)

    assert len(many) == 50
    assert len(few) == 1
    assert for_many == for_few
