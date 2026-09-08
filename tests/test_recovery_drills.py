"""Kill LibrAIry in the middle of moving somebody's file, then look at what it says.

The suite already kills a commit between operations and re-runs it
(`test_adversarial.test_kill_mid_execution_then_rerun_completes`). That covers
the wide, easy window — the one where an operation is either finished or not
started. The windows that matter are the narrow ones *inside* an operation,
where the bytes have moved and the four rows that record the move have not all
been written:

    os.rename(src, dest)      ← the file is now in the library
    plan_ops.result = done
    history(... 'ok')         ← what Undo reverses
    items.root/relpath        ← what Browse, Search and every count read

A process killed between any two of those lines leaves a real installation, and
the question this file asks is not "did it crash cleanly" but:

    **what does LibrAIry say afterwards, and is it true?**

Before these drills the answers were all wrong in the same direction. A file
LibrAIry had itself moved a millisecond earlier came back as `skipped_missing`
— *the source is gone* — which put no row in the journal, so the file could
never be undone; left the index naming the inbox, so the next scan reported the
file as vanished and then discovered LibrAIry's own copy of it as something new;
and left a half-written `.part-` file in the library, which the scanner indexed
as ordinary media. Undo failed the same way in reverse: *not put back — the file
is no longer where LibrAIry left it*, said about a file lying exactly where the
reversal had just put it.

None of that lost a file. All of it lost the truth, which is the thing this
program is for.

## How the crash is produced

By killing a real process at a chosen seam, from a script the test writes. The
seams are patched in the *child*, so nothing in the shipped code knows a drill
is running — an executor with a `if TESTING: crash_here()` in it is not the
executor that ships. `SIGKILL` is used rather than an exception: an exception
unwinds, and unwinding is exactly what a power cut does not do.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from librairy import commit_state
from librairy.config import Settings
from librairy.db import connect
from librairy.executor import execute_plan
from librairy.history import undo_plan
from librairy.locks import acquire_lock
from librairy.paths import is_in_flight
from librairy.planner import OperationSpec, approve_plan, create_plan
from librairy.scanner import scan_root
from librairy.undo_sequence import FORWARD_ACTIONS, reversed_already

#  One commit or one reversal, killed at a named instant. Written to disk and
#  run as its own process because the point is a process that stops existing.
CHILD = '''
import errno, os, shutil, signal, sys
from librairy import executor, history
from librairy.config import Settings
from librairy.db import connect

where, plan_id = sys.argv[1], sys.argv[2]


def die(*_args, **_kwargs):
    os.kill(os.getpid(), signal.SIGKILL)


if where == "after_bytes":
    #  The file is in the library; not one row says so yet.
    executor._finish_op = die
elif where == "after_journal":
    #  The journal knows. The index does not.
    executor._move_item_row = die
elif where == "mid_copy":
    #  A cross-filesystem move — inbox and library on different mounts, which
    #  is the ordinary NAS arrangement — killed while the bytes are copying.
    def no_rename(src, dst):
        raise OSError(errno.EXDEV, "forced cross-device")

    def half_copy(src, dst):
        data = open(src, "rb").read()
        with open(dst, "wb") as handle:
            handle.write(data[: len(data) // 2])
        die()

    os.rename = no_rename
    shutil.copy2 = half_copy
elif where == "undo_after_bytes":
    #  The file is back in the inbox; the reversal is not journalled.
    history._record_undo = die
elif where == "undo_after_journal":
    #  The reversal is journalled; the index still names the library.
    history._update_item_after_undo = die
else:
    raise SystemExit(f"unknown crash point: {where}")

settings = Settings(_env_file=None)
conn = connect(settings)
if where.startswith("undo"):
    history.undo_plan(conn, plan_id, settings)
else:
    executor.execute_plan(conn, plan_id, settings)
'''

FILES = 2
CONTENT = "x" * 3000


def settings_for(tmp_path: Path) -> Settings:
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        FILE_STABILITY_SECONDS=0,
        AUTH_REQUIRED=False,
        _env_file=None,
    )
    for directory in (
        settings.appdata_dir,
        settings.inbox_dir,
        settings.library_dir,
        settings.quarantine_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    return settings


def prepare(tmp_path: Path, *, committed: bool = False) -> tuple[Settings, str]:
    """An approved plan for two files, optionally already carried out."""
    settings = settings_for(tmp_path)
    for index in range(FILES):
        (settings.inbox_dir / f"file-{index}.txt").write_text(
            f"{index}{CONTENT}", encoding="utf-8"
        )
    conn = connect(settings)
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    plan_id = create_plan(
        conn,
        [
            OperationSpec("move", f"file-{index}.txt", "library", f"Documents/file-{index}.txt")
            for index in range(FILES)
        ],
        settings,
    )
    approve_plan(conn, plan_id, settings)
    if committed:
        execute_plan(conn, plan_id, settings)
    conn.close()
    return settings, plan_id


def crash(tmp_path: Path, settings: Settings, plan_id: str, where: str) -> None:
    """Run one commit or reversal in a real process, and kill it mid-operation."""
    script = tmp_path / "crash_child.py"
    script.write_text(CHILD, encoding="utf-8")
    env = os.environ.copy()
    env.update(
        {
            "APPDATA_DIR": str(settings.appdata_dir),
            "INBOX_DIR": str(settings.inbox_dir),
            "LIBRARY_DIR": str(settings.library_dir),
            "QUARANTINE_DIR": str(settings.quarantine_dir),
            "FILE_STABILITY_SECONDS": "0",
        }
    )
    done = subprocess.run(  # noqa: S603 - our own interpreter, our own script
        [sys.executable, str(script), where, plan_id],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert done.returncode == -9, (
        f"the {where} drill did not reach its crash point: {done.stderr[-800:]}"
    )


def rows(conn, sql: str, *params: object) -> list:
    return list(conn.execute(sql, params))


def journalled(conn, relpath: str) -> list:
    return rows(
        conn,
        "SELECT * FROM history WHERE dest_relpath=? AND outcome='ok'"
        " AND action NOT LIKE 'undo\\_%' ESCAPE '\\'",
        relpath,
    )


def items_at(conn, root: str, relpath: str) -> list:
    return rows(conn, "SELECT * FROM items WHERE root=? AND relpath=?", root, relpath)


# --- what a killed commit says afterwards ------------------------------------


def test_a_move_the_dead_run_made_is_not_reported_as_a_missing_file(tmp_path: Path) -> None:
    """The first file is in the library. The resumed commit has to agree.

    `skipped_missing` was the old answer, and every word of it was wrong: the
    source is gone because LibrAIry moved it.
    """
    settings, plan_id = prepare(tmp_path)
    crash(tmp_path, settings, plan_id, "after_bytes")

    moved = settings.library_dir / "Documents/file-0.txt"
    assert moved.is_file(), "the drill did not reach the interesting window"

    conn = connect(settings)
    summary = execute_plan(conn, plan_id, settings)

    assert summary.skipped_missing == 0
    assert summary.done == FILES
    assert conn.execute("SELECT status FROM plans WHERE id=?", (plan_id,)).fetchone()[0] == "done"
    #  Recorded once, as the move it was.
    assert len(journalled(conn, "Documents/file-0.txt")) == 1
    #  And the index names the library, so no scan will call this file vanished
    #  and then discover LibrAIry's own copy of it as something new.
    assert len(items_at(conn, "library", "Documents/file-0.txt")) == 1
    assert items_at(conn, "inbox", "file-0.txt") == []


def test_the_file_a_killed_commit_filed_can_still_be_put_back(tmp_path: Path) -> None:
    """The consequence that costs somebody something.

    Undo reverses the journal. A move with no journal row is a move that can
    never be taken back — the file is filed for ever, by a decision the person
    can no longer see or reverse.
    """
    settings, plan_id = prepare(tmp_path)
    crash(tmp_path, settings, plan_id, "after_bytes")
    conn = connect(settings)
    execute_plan(conn, plan_id, settings)

    results = undo_plan(conn, plan_id, settings)

    assert [result.outcome for result in results] == ["ok"] * FILES
    assert (settings.inbox_dir / "file-0.txt").is_file()
    assert not (settings.library_dir / "Documents/file-0.txt").exists()


def test_a_run_killed_after_journalling_leaves_the_index_agreeing(tmp_path: Path) -> None:
    """The other window, three statements later, and the opposite symptom.

    History knows the file moved and the index does not, so Browse and Search
    answer from a row naming a path with nothing at it. The resumed commit
    finishes saying what the dead one had already done — without journalling it
    twice.
    """
    settings, plan_id = prepare(tmp_path)
    crash(tmp_path, settings, plan_id, "after_journal")
    conn = connect(settings)

    execute_plan(conn, plan_id, settings)

    assert len(journalled(conn, "Documents/file-0.txt")) == 1
    assert len(items_at(conn, "library", "Documents/file-0.txt")) == 1
    assert items_at(conn, "inbox", "file-0.txt") == []


def test_a_first_run_never_claims_a_move_it_did_not_make(tmp_path: Path) -> None:
    """The recovery may only fire for a run that was interrupted.

    A file deleted from the inbox before the first commit is `skipped_missing`
    and stays that way, even with an identical copy already at the destination:
    nothing was interrupted, so nothing was recovered.
    """
    settings, plan_id = prepare(tmp_path)
    (settings.library_dir / "Documents").mkdir(parents=True)
    (settings.library_dir / "Documents/file-0.txt").write_text(f"0{CONTENT}", encoding="utf-8")
    (settings.inbox_dir / "file-0.txt").unlink()

    conn = connect(settings)
    summary = execute_plan(conn, plan_id, settings)

    assert summary.skipped_missing == 1
    assert journalled(conn, "Documents/file-0.txt") == []


# --- the half-written file ----------------------------------------------------


def test_an_interrupted_copy_is_never_indexed_as_a_library_file(tmp_path: Path) -> None:
    """A crash inside a cross-filesystem copy leaves half a file behind.

    Inbox and library on separate mounts is the ordinary NAS arrangement, so
    this is not an exotic window — and the leftover carries a real name in a
    real library folder. Indexed, it would be browsable, searchable, counted in
    the library total and queued for backup: a truncated file presented as
    somebody's media.
    """
    settings, plan_id = prepare(tmp_path)
    crash(tmp_path, settings, plan_id, "mid_copy")

    partial = list(settings.library_dir.rglob("*.part-*"))
    assert len(partial) == 1, "the drill did not leave a half-written file"
    assert is_in_flight(partial[0].name)

    conn = connect(settings)
    scan_root(conn, "library", settings.library_dir, settings)

    assert rows(conn, "SELECT * FROM items WHERE root='library'") == []


def test_resuming_clears_the_half_written_file_it_left(tmp_path: Path) -> None:
    """And the commit that owns it removes it, whichever way the retry moves.

    The retry usually renames straight across — a path that never looked at the
    temporary name, so the leftover survived every successful recovery until it
    was cleared before the move rather than inside the copy.
    """
    settings, plan_id = prepare(tmp_path)
    crash(tmp_path, settings, plan_id, "mid_copy")
    conn = connect(settings)

    execute_plan(conn, plan_id, settings)

    assert list(settings.library_dir.rglob("*.part-*")) == []
    assert (settings.library_dir / "Documents/file-0.txt").read_text(
        encoding="utf-8"
    ) == f"0{CONTENT}"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("photo.jpg.part-3f2504e0-4f89-41d3-9a0c-0305e82c3301", True),
        ("photo.jpg.part-undo-41", True),
        ("Episode 2.part-2.mkv", False),
        ("counterpart-notes.txt", False),
        ("photo.jpg", False),
    ],
)
def test_only_librairys_own_in_flight_name_is_hidden(name: str, expected: bool) -> None:
    """Narrow on purpose. `.part-` alone is a suffix somebody might own."""
    assert is_in_flight(name) is expected


# --- what a killed reversal says afterwards -----------------------------------


def test_a_reversal_killed_before_it_was_journalled_is_recognised(tmp_path: Path) -> None:
    """*Not put back — the file is no longer where LibrAIry left it.*

    Said about a file lying exactly where the reversal put it, because the run
    that put it there was killed before it could write the row. Pressing Undo
    again has to finish the job rather than repeat the sentence.
    """
    settings, plan_id = prepare(tmp_path, committed=True)
    crash(tmp_path, settings, plan_id, "undo_after_bytes")
    assert (settings.inbox_dir / "file-1.txt").is_file()

    conn = connect(settings)
    results = undo_plan(conn, plan_id, settings)

    assert [result.outcome for result in results] == ["ok"] * FILES
    for index in range(FILES):
        assert (settings.inbox_dir / f"file-{index}.txt").is_file()
        assert len(items_at(conn, "inbox", f"file-{index}.txt")) == 1
    #  One reversal per move, not two.
    assert len(rows(conn, "SELECT * FROM history WHERE action='undo_move' AND outcome='ok'")) == 2


def test_a_reversal_killed_after_journalling_leaves_the_index_agreeing(tmp_path: Path) -> None:
    """The mirror of the commit case, and the same repair."""
    settings, plan_id = prepare(tmp_path, committed=True)
    crash(tmp_path, settings, plan_id, "undo_after_journal")

    conn = connect(settings)
    undo_plan(conn, plan_id, settings)

    for index in range(FILES):
        assert len(items_at(conn, "inbox", f"file-{index}.txt")) == 1
    assert rows(conn, "SELECT * FROM items WHERE root='library'") == []


def test_undoing_an_already_reversed_plan_records_nothing_new(tmp_path: Path) -> None:
    """The gate that keeps the recovery from being a second reversal.

    After a plan is fully undone, every file is back at its old address with
    its old bytes — which is exactly what an interrupted reversal looks like.
    What tells them apart is the journal, and pressing Undo a third time must
    add nothing to it.
    """
    settings, plan_id = prepare(tmp_path, committed=True)
    conn = connect(settings)
    undo_plan(conn, plan_id, settings)
    before = len(rows(conn, "SELECT * FROM history WHERE outcome='ok'"))

    results = undo_plan(conn, plan_id, settings)

    assert {result.outcome for result in results} == {"undo_refused_missing"}
    assert len(rows(conn, "SELECT * FROM history WHERE outcome='ok'")) == before


# --- what the pages say -------------------------------------------------------


def test_a_killed_run_is_not_described_as_running(tmp_path: Path) -> None:
    """`executing` is a row, not a process.

    The Dashboard read it literally and said *Commit · 1 running* about a
    process that had not existed for a week. The lock is what actually knows:
    the kernel releases it when the holder dies, however it dies.
    """
    from librairy.web.dashboard import dashboard_data

    settings, plan_id = prepare(tmp_path)
    crash(tmp_path, settings, plan_id, "after_bytes")
    conn = connect(settings)

    assert conn.execute(
        "SELECT status FROM plans WHERE id=?", (plan_id,)
    ).fetchone()[0] == "executing"

    stopped = commit_state.unfinished(conn, settings)
    assert len(stopped) == 1
    assert stopped[0].plan_id == plan_id
    assert stopped[0].total == FILES

    data = dashboard_data(conn, settings)
    assert not [row for row in data["activity"] if row["what"] == "Commit"]
    assert [row for row in data["needs_attention"] if "interrupted" in row["text"]]


def test_a_running_commit_is_never_called_interrupted(tmp_path: Path) -> None:
    """Under-reporting is the safe direction, and this is the safety.

    Anything holding the lock — a commit, a reversal, an ordinary worker cycle
    — silences the claim entirely. Being told a minute late that a commit
    stopped is a great deal better than being told a running one has.
    """
    settings, plan_id = prepare(tmp_path)
    crash(tmp_path, settings, plan_id, "after_bytes")
    conn = connect(settings)

    with acquire_lock(settings):
        assert commit_state.unfinished(conn, settings) == []
    assert len(commit_state.unfinished(conn, settings)) == 1


def test_health_says_what_stopped_and_where_it_is_answered(tmp_path: Path) -> None:
    settings, plan_id = prepare(tmp_path)
    crash(tmp_path, settings, plan_id, "after_bytes")
    conn = connect(settings)

    from librairy.attention import report

    concerns = [
        concern for concern in report(conn, settings).concerns
        if concern.code == "commit-interrupted"
    ]

    assert len(concerns) == 1
    assert concerns[0].href == "/commit"
    #  Never "failed" and never "succeeded": each file recorded its own result,
    #  and what the run would have done next is not known.
    said = f"{concerns[0].headline} {concerns[0].detail}".lower()
    assert "failed" not in said
    assert "succeeded" not in said


def test_how_far_it_got_is_counted_from_what_was_recorded() -> None:
    """And it may say one fewer than moved, never one more.

    The window this module exists for is the one where the bytes moved and the
    result had not been written yet, so the count is of recorded operations.
    Under-claiming settles itself the moment the plan is committed again.
    """
    assert commit_state.Unfinished("p", total=40, finished=3).sentence == (
        "3 of 40 files were filed before it stopped."
    )
    assert commit_state.Unfinished("p", total=40, finished=1).sentence == (
        "1 of 40 file was filed before it stopped."
    )
    #  Never "none of them moved", which is the one thing that is not known
    #  here: the first file's bytes may be at their destination already.
    assert commit_state.Unfinished("p", total=40, finished=0).sentence == (
        "It stopped before any of its 40 files were recorded."
    )


def test_a_settled_installation_says_nothing_about_commits(tmp_path: Path) -> None:
    """And the file lock is not even opened when no plan says `executing`."""
    settings, plan_id = prepare(tmp_path)
    conn = connect(settings)
    execute_plan(conn, plan_id, settings)

    assert commit_state.unfinished(conn, settings) == []


# --- the invariant, at every crash point --------------------------------------


@pytest.mark.parametrize(
    ("where", "committed"),
    [
        ("after_bytes", False),
        ("after_journal", False),
        ("mid_copy", False),
        ("undo_after_bytes", True),
        ("undo_after_journal", True),
    ],
)
def test_no_crash_loses_a_file_or_duplicates_one(
    tmp_path: Path, where: str, committed: bool
) -> None:
    """The first half of the rule, asked at every seam.

    A crash may lose work. It may not lose a file, and it may not leave two of
    them: every file is somewhere, exactly once, with its own bytes — and a
    half-written copy is not a file, which is why it is excluded here and
    refused an `items` row everywhere else.
    """
    settings, plan_id = prepare(tmp_path, committed=committed)
    crash(tmp_path, settings, plan_id, where)
    conn = connect(settings)

    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    present = [
        path.read_text(encoding="utf-8")
        for root in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir)
        for path in root.rglob("*")
        if path.is_file() and not is_in_flight(path.name)
    ]
    assert sorted(present) == sorted(f"{index}{CONTENT}" for index in range(FILES))


@pytest.mark.parametrize(
    ("where", "committed"),
    [
        ("after_bytes", False),
        ("after_journal", False),
        ("mid_copy", False),
        ("undo_after_bytes", True),
        ("undo_after_journal", True),
    ],
)
def test_after_the_crash_the_journal_and_the_library_agree(
    tmp_path: Path, where: str, committed: bool
) -> None:
    """The second half, and the one the drills were written to find.

    In the moment after a crash the journal can be *behind* the world — a file
    moved and the row not yet written — and that is survivable. What is not is
    the disagreement outliving the recovery. So: carry on the way a person
    would, by pressing the same button again, and then every operation the
    journal still says stands has to describe a file that is really there.
    """
    from librairy.fingerprint import blake2b_file

    settings, plan_id = prepare(tmp_path, committed=committed)
    crash(tmp_path, settings, plan_id, where)
    conn = connect(settings)

    if where.startswith("undo"):
        undo_plan(conn, plan_id, settings)
    else:
        execute_plan(conn, plan_id, settings)

    roots = {
        "inbox": settings.inbox_dir,
        "library": settings.library_dir,
        "quarantine": settings.quarantine_dir,
    }
    standing = 0
    for row in rows(conn, "SELECT * FROM history WHERE outcome='ok'"):
        if row["action"] not in FORWARD_ACTIONS:
            #  A reversal, which is described by the row it reverses.
            continue
        if reversed_already(conn, int(row["id"])):
            #  A move that has since been put back. The row is still true about
            #  what happened; the reversal above it is what is true now.
            continue
        landed = roots[row["dest_root"]] / row["dest_relpath"]
        assert landed.is_file(), f"the journal claims {row['dest_relpath']} is there"
        if row["fingerprint"]:
            assert blake2b_file(landed) == row["fingerprint"]
        standing += 1
    #  And the index says the same thing about the same files.
    for row in rows(conn, "SELECT * FROM items"):
        assert (roots[row["root"]] / row["relpath"]).is_file(), (
            f"the index claims {row['root']}/{row['relpath']} is there"
        )
    assert standing == (0 if where.startswith("undo") else FILES)
