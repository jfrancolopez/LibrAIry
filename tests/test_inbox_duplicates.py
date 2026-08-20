"""A file you already have, arriving again.

The worker has recognised these since the first release. What did not exist was
the workflow around it: the row that says *which* library file it matches, the
Quarantine entry that remembers, and the check that the library copy is still
there when Commit runs.

That last one is why this is a module and not a template change:

    the duplicate is found          the library copy is what makes it redundant
    the library copy is deleted     by hand, by another tool, by a restore
    Commit runs                     and the arrival goes to Quarantine

Now there is no copy anywhere. Nothing was deleted by LibrAIry and nothing was
overwritten, and the person has lost the file, because the evidence that made it
safe to set aside had expired and nobody re-read it.

So the twin is looked up at the moment of execution, by fingerprint, against the
disk. Most of the rest of this file falls out of that one decision.
"""

from __future__ import annotations

from pathlib import Path

from librairy.config import Settings
from librairy.db import connect
from librairy.dedup import detect_exact_duplicates
from librairy.executor import execute_plan
from librairy.inbox_duplicates import (
    describe,
    is_duplicate_proposal,
    still_redundant,
    twins_of,
)
from librairy.planner import approve_plan, create_plan
from librairy.quarantine import quarantine_operation
from librairy.scanner import scan_root

FILED = "Music/Live/Artist/concert.flac"
ARRIVAL = "concert.flac"
BYTES = "the same concert"


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
    for root in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        root.mkdir(parents=True, exist_ok=True)
    return settings


def scene(tmp_path: Path, *, library: dict[str, str], inbox: dict[str, str]):
    settings = settings_for(tmp_path)
    conn = connect(settings)
    for root, files in ((settings.library_dir, library), (settings.inbox_dir, inbox)):
        for relpath, body in files.items():
            path = root / relpath
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
    scan_root(conn, "library", settings.library_dir, settings)
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    return conn, settings


def ordinary(tmp_path: Path):
    """One arrival, one filed copy, identical bytes."""
    return scene(tmp_path, library={FILED: BYTES}, inbox={ARRIVAL: BYTES})


def item_id(conn, root: str, relpath: str) -> int:
    return int(
        conn.execute(
            "SELECT id FROM items WHERE root=? AND relpath=?", (root, relpath)
        ).fetchone()["id"]
    )


def agrees(pairs, settings):  # noqa: ANN001, ARG001
    """Stands in for rmlint, which is not installed on every machine.

    Agreeing with the hash is the ordinary confirmed case and the one this
    workflow is about. `dedup.py` already has its own tests for what happens
    when the two detectors disagree.
    """
    return {tuple(sorted((left.id, right.id))) for left, right in pairs}


def stage(conn, settings):
    """What the worker's duplicate pass does, through the worker's own code."""
    from librairy.worker import _stage_quarantine_proposals

    _stage_quarantine_proposals(
        conn, detect_exact_duplicates(conn, settings, rmlint_check=agrees)
    )
    return item_id(conn, "inbox", ARRIVAL)


def approved_plan(conn, settings, relpath: str = ARRIVAL) -> str:
    plan_id = create_plan(conn, [quarantine_operation(relpath)], settings)
    approve_plan(conn, plan_id, settings)
    return plan_id


# --- what the worker finds ------------------------------------------------------


def test_an_arrival_that_matches_a_filed_file_is_staged(tmp_path: Path) -> None:
    conn, settings = ordinary(tmp_path)

    ident = stage(conn, settings)

    assert is_duplicate_proposal(conn, ident)
    proposal = conn.execute(
        "SELECT action, dest_root, status FROM proposals WHERE item_id=?", (ident,)
    ).fetchone()
    assert proposal["action"] == "quarantine"
    assert proposal["dest_root"] == "quarantine"


def test_the_library_copy_is_the_keeper_and_the_arrival_is_the_duplicate(
    tmp_path: Path,
) -> None:
    conn, settings = ordinary(tmp_path)

    candidates = detect_exact_duplicates(conn, settings, rmlint_check=agrees)

    assert [candidate.duplicate.relpath for candidate in candidates] == [ARRIVAL]
    assert [candidate.keeper.relpath for candidate in candidates] == [FILED]


def test_the_row_names_the_file_it_matches(tmp_path: Path) -> None:
    """"Already in your library" without saying where leaves no way to judge
    whether this arrival is the better copy."""
    conn, settings = ordinary(tmp_path)
    ident = stage(conn, settings)

    described = describe(conn, ident)

    assert described["match"] == FILED
    assert described["extra"] == 0
    assert described["note"] == "The bytes are identical."


def test_several_library_copies_are_all_named(tmp_path: Path) -> None:
    """The arrival is redundant whichever of them is kept, so this is still one
    answer. Which library copy is canonical is the library's question, and
    `audit_duplicates` is where it gets asked."""
    conn, settings = scene(
        tmp_path,
        library={FILED: BYTES, "Music/Spare/concert.flac": BYTES},
        inbox={ARRIVAL: BYTES},
    )
    ident = stage(conn, settings)

    described = describe(conn, ident)

    assert len(described["twins"]) == 2
    assert described["extra"] == 1


def test_a_file_nobody_else_has_is_not_a_duplicate(tmp_path: Path) -> None:
    from librairy.worker import _stage_quarantine_proposals

    conn, settings = scene(
        tmp_path, library={FILED: BYTES}, inbox={"something else.flac": "other bytes"}
    )

    _stage_quarantine_proposals(
        conn, detect_exact_duplicates(conn, settings, rmlint_check=agrees)
    )

    assert describe(conn, item_id(conn, "inbox", "something else.flac")) is None


def test_a_near_match_is_not_an_exact_duplicate(tmp_path: Path) -> None:
    """Different bytes are different bytes. Similar media has its own semantics
    and its own flags; it must not borrow this flow."""
    conn, settings = scene(
        tmp_path, library={FILED: BYTES}, inbox={ARRIVAL: BYTES + " (re-encoded)"}
    )

    assert detect_exact_duplicates(conn, settings, rmlint_check=agrees) == []
    assert twins_of(conn, item_id(conn, "inbox", ARRIVAL)) == []


def test_a_staged_duplicate_is_never_sent_for_analysis(tmp_path: Path) -> None:
    """The strongest possible evidence already exists. Asking a model, a
    catalog or a naming policy about it would spend work to arrive at a worse
    answer than the hash already gave."""
    from librairy.scanner import ready_items

    conn, settings = ordinary(tmp_path)
    ident = stage(conn, settings)

    waiting = [int(row["id"]) for row in ready_items(conn, "inbox")]

    assert ident not in waiting
    assert conn.execute(
        "SELECT state FROM items WHERE id=?", (ident,)
    ).fetchone()["state"] == "quarantine-proposed"


# --- deciding, and committing -------------------------------------------------------


def test_setting_it_aside_moves_nothing_until_commit(tmp_path: Path) -> None:
    conn, settings = ordinary(tmp_path)
    stage(conn, settings)

    approved_plan(conn, settings)

    assert (settings.inbox_dir / ARRIVAL).is_file()
    assert not any(settings.quarantine_dir.rglob("*.flac"))


def test_commit_holds_the_arrival_and_leaves_the_filed_copy(tmp_path: Path) -> None:
    conn, settings = ordinary(tmp_path)
    stage(conn, settings)

    summary = execute_plan(conn, approved_plan(conn, settings), settings)

    assert summary.done == 1
    assert not (settings.inbox_dir / ARRIVAL).exists()
    assert (settings.library_dir / FILED).read_text() == BYTES
    held = list(settings.quarantine_dir.rglob("concert.flac"))
    assert len(held) == 1


def test_quarantine_remembers_which_file_it_matched(tmp_path: Path) -> None:
    conn, settings = ordinary(tmp_path)
    stage(conn, settings)

    execute_plan(conn, approved_plan(conn, settings), settings)

    entry = conn.execute(
        "SELECT reason, duplicate_of, original_root FROM quarantine_entries"
    ).fetchone()
    assert entry["reason"] == "exact_duplicate"
    assert entry["original_root"] == "inbox"
    assert entry["duplicate_of"] == item_id(conn, "library", FILED)


def test_restoring_it_returns_it_to_the_inbox(tmp_path: Path) -> None:
    """And never straight into the library. It was never filed, so there is
    nowhere in the library for it to go back to."""
    from librairy.quarantine_requests import request_restore

    conn, settings = ordinary(tmp_path)
    stage(conn, settings)
    execute_plan(conn, approved_plan(conn, settings), settings)
    entry = conn.execute("SELECT id FROM quarantine_entries").fetchone()

    plan_id = request_restore(conn, settings, int(entry["id"]))
    execute_plan(conn, plan_id, settings)

    assert (settings.inbox_dir / ARRIVAL).read_text() == BYTES
    assert (settings.library_dir / FILED).read_text() == BYTES


# --- the evidence expiring ------------------------------------------------------------


def test_a_deleted_library_copy_stops_the_commit(tmp_path: Path) -> None:
    """The safety property. Without it, "nothing was deleted" would be true and
    the person would still have lost the file."""
    conn, settings = ordinary(tmp_path)
    stage(conn, settings)
    plan_id = approved_plan(conn, settings)
    (settings.library_dir / FILED).unlink()

    summary = execute_plan(conn, plan_id, settings)

    assert summary.done == 0
    assert summary.skipped_changed == 1
    assert (settings.inbox_dir / ARRIVAL).read_text() == BYTES
    assert conn.execute("SELECT COUNT(*) FROM quarantine_entries").fetchone()[0] == 0


def test_an_edited_library_copy_stops_the_commit(tmp_path: Path) -> None:
    """Still there is not the test — still *identical* is."""
    conn, settings = ordinary(tmp_path)
    stage(conn, settings)
    plan_id = approved_plan(conn, settings)
    (settings.library_dir / FILED).write_text("re-tagged since", encoding="utf-8")

    summary = execute_plan(conn, plan_id, settings)

    assert summary.skipped_changed == 1
    assert (settings.inbox_dir / ARRIVAL).is_file()


def test_a_library_copy_that_moved_is_still_the_twin(tmp_path: Path) -> None:
    """Read by fingerprint, not by a stored pair. Filing the keeper somewhere
    else does not make the arrival worth keeping."""
    conn, settings = ordinary(tmp_path)
    stage(conn, settings)
    plan_id = approved_plan(conn, settings)
    moved = settings.library_dir / "Music/Live/Artist 2/concert.flac"
    moved.parent.mkdir(parents=True, exist_ok=True)
    (settings.library_dir / FILED).rename(moved)
    scan_root(conn, "library", settings.library_dir, settings)

    summary = execute_plan(conn, plan_id, settings)

    assert summary.done == 1


def test_one_file_losing_its_twin_does_not_stop_the_others(tmp_path: Path) -> None:
    """An inbox commit's files are independent of one another, deliberately.
    This is a fact about one file and is checked per operation."""
    conn, settings = scene(
        tmp_path,
        library={FILED: BYTES},
        inbox={ARRIVAL: BYTES, "keep me.flac": "quite different"},
    )
    stage(conn, settings)
    plan_id = create_plan(
        conn,
        [
            quarantine_operation(ARRIVAL),
            quarantine_operation("keep me.flac"),
        ],
        settings,
    )
    approve_plan(conn, plan_id, settings)
    (settings.library_dir / FILED).unlink()

    summary = execute_plan(conn, plan_id, settings)

    assert summary.done == 1
    assert summary.skipped_changed == 1
    assert (settings.inbox_dir / ARRIVAL).is_file()
    assert not (settings.inbox_dir / "keep me.flac").exists()


def test_the_same_file_arriving_again_is_recognised_again(tmp_path: Path) -> None:
    """A fingerprint that was quarantined once is not suppressed for ever."""
    conn, settings = ordinary(tmp_path)
    stage(conn, settings)
    execute_plan(conn, approved_plan(conn, settings), settings)

    (settings.inbox_dir / ARRIVAL).write_text(BYTES, encoding="utf-8")
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    again = stage(conn, settings)

    assert is_duplicate_proposal(conn, again)
    assert describe(conn, again)["match"] == FILED


def test_one_arrival_gets_one_active_finding(tmp_path: Path) -> None:
    """Running the duplicate pass twice re-proposes its own row rather than
    stacking a second one on the same physical file."""
    conn, settings = ordinary(tmp_path)
    ident = stage(conn, settings)

    stage(conn, settings)

    assert conn.execute(
        "SELECT COUNT(*) FROM proposals WHERE item_id=? AND status != 'superseded'",
        (ident,),
    ).fetchone()[0] == 1


def test_still_redundant_reads_the_disk_and_not_only_the_index(
    tmp_path: Path,
) -> None:
    conn, settings = ordinary(tmp_path)
    ident = stage(conn, settings)
    assert still_redundant(conn, settings, ident) is not None

    #  The index still says the file is there; the disk disagrees, and the disk
    #  is what a commit acts on.
    (settings.library_dir / FILED).unlink()

    assert still_redundant(conn, settings, ident) is None
