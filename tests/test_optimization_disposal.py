"""The other half of Storage Optimization: letting the original actually go.

Adoption leaves two files on the disk and frees nothing, which is honest and is
not what anybody wanted from a feature about space. The way out is the same one
every other file gets — Delete queue, Commit, and then you empty the folder
yourself — and it was withheld until now for one specific reason: the adoption's
Undo finds the preserved original at the exact path the plan put it at, and this
moves it.

So the reversal reverses both plans in order rather than teaching Undo to go
looking. These tests are mostly about that ordering, about what is checked
before anything moves, and about the state after somebody really has deleted the
file — where the honest answer is that the optimization can no longer be undone
and the storage reduction is, for the first time, real.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from librairy import executor
from librairy.config import Settings
from librairy.db import connect
from librairy.fingerprint import blake2b_file
from librairy.optimization_adopt import plan_adoption
from librairy.optimization_disposal import (
    IN_DELETE_QUEUE,
    PRESERVED,
    REMOVED,
    RESTORED,
    WAITING,
    adoption_plan_id,
    disposal_plan_id,
    keep_original,
    outcomes,
    preserved_state,
    restore_original,
)
from librairy.planner import utc_now
from librairy.quarantine_requests import cancel_request, request_delete_queue
from librairy.scanner import scan_root

ORIGINAL = "Music/Live/concert.wav"
TARGET = "Music/Live/concert.flac"
QUEUED = f"_to-delete/{ORIGINAL}"

#  The worked example from the specification, in bytes: O = 100, N = 60.
ORIGINAL_BYTES = b"o" * 100
OPTIMIZED_BYTES = b"n" * 60


@pytest.fixture
def adopted(tmp_path: Path):
    """One committed adoption: optimized file live, original preserved."""
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
    conn = connect(settings)
    original = settings.library_dir / ORIGINAL
    original.parent.mkdir(parents=True)
    original.write_bytes(ORIGINAL_BYTES)
    scan_root(conn, "library", settings.library_dir, settings)
    item = conn.execute(
        "SELECT id, fingerprint FROM items WHERE relpath=?", (ORIGINAL,)
    ).fetchone()
    job_id = int(
        conn.execute(
            """
            INSERT INTO optimization_jobs(
              item_id, root, relpath, fingerprint, kind, quality, from_label,
              to_label, preset, source_bytes, estimated_bytes, actual_bytes,
              state, verified, output_relpath, staging_dir, queued_at, updated_at
            ) VALUES (?, 'library', ?, ?, 'audio-to-flac', 'lossless', 'WAV',
                      'FLAC', 'flac-lossless', 100, 60, 60, 'ready', 'passed',
                      'output.flac', '', ?, ?)
            """,
            (item["id"], ORIGINAL, item["fingerprint"], utc_now(), utc_now()),
        ).lastrowid
    )
    staging = settings.appdata_dir / "optimization" / "jobs" / str(job_id)
    staging.mkdir(parents=True)
    output = staging / "output.flac"
    output.write_bytes(OPTIMIZED_BYTES)
    conn.execute(
        "UPDATE optimization_jobs SET output_fingerprint=? WHERE id=?",
        (blake2b_file(output), job_id),
    )
    plan_id = plan_adoption(conn, settings, job_id)
    executor.execute_plan(conn, plan_id, settings)
    entry = conn.execute(
        "SELECT * FROM quarantine_entries ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return conn, settings, int(entry["id"]), job_id, plan_id


def entry_of(conn, entry_id: int):
    return conn.execute(
        "SELECT * FROM quarantine_entries WHERE id=?", (entry_id,)
    ).fetchone()


def queue_it(conn, settings, entry_id: int) -> str:
    """Ask, and then commit — the two steps a person takes, separately."""
    plan_id = request_delete_queue(conn, settings, entry_id)
    executor.execute_plan(conn, plan_id, settings)
    return plan_id


def files_under(root: Path) -> set[str]:
    return {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}


# --- asking is not doing ------------------------------------------------------------


def test_asking_moves_nothing(adopted) -> None:
    conn, settings, entry_id, _, _ = adopted
    before = files_under(settings.quarantine_dir) | files_under(settings.library_dir)

    request_delete_queue(conn, settings, entry_id)

    assert files_under(settings.quarantine_dir) | files_under(settings.library_dir) == before
    assert preserved_state(conn, entry_of(conn, entry_id)) == WAITING


def test_the_request_is_a_plan_that_knows_which_original_it_moves(adopted) -> None:
    """No new column records the dependency, because the rows already cannot be
    arranged any other way: the plan names the quarantine entry, and the entry
    has named its adoption and its job since the moment adoption wrote it."""
    conn, settings, entry_id, job_id, plan_a = adopted

    plan_b = request_delete_queue(conn, settings, entry_id)

    plan = conn.execute("SELECT * FROM plans WHERE id=?", (plan_b,)).fetchone()
    entry = entry_of(conn, entry_id)
    assert plan["quarantine_entry_id"] == entry_id
    assert entry["optimization_job_id"] == job_id
    assert adoption_plan_id(entry) == plan_a


def test_cancelling_moves_nothing_and_leaves_the_adoption_intact(adopted) -> None:
    conn, settings, entry_id, job_id, _ = adopted
    request_delete_queue(conn, settings, entry_id)

    cancel_request(conn, entry_id)

    assert preserved_state(conn, entry_of(conn, entry_id)) == PRESERVED
    assert (settings.quarantine_dir / ORIGINAL).is_file()
    assert (settings.library_dir / TARGET).is_file()
    assert conn.execute(
        "SELECT result_item_id FROM optimization_jobs WHERE id=?", (job_id,)
    ).fetchone()[0] is not None


# --- committing moves it, and only within quarantine --------------------------------


def test_commit_moves_the_original_into_the_delete_queue_and_nowhere_else(
    adopted,
) -> None:
    conn, settings, entry_id, _, _ = adopted
    original_hash = blake2b_file(settings.quarantine_dir / ORIGINAL)

    queue_it(conn, settings, entry_id)

    assert files_under(settings.quarantine_dir) == {QUEUED}
    assert blake2b_file(settings.quarantine_dir / QUEUED) == original_hash
    assert preserved_state(conn, entry_of(conn, entry_id)) == IN_DELETE_QUEUE


def test_nothing_is_deleted(adopted) -> None:
    """The invariant this whole feature is built around. Every byte of the
    original is still on the disk after Commit; what changed is which folder
    it is in."""
    conn, settings, entry_id, _, _ = adopted

    queue_it(conn, settings, entry_id)

    assert (settings.quarantine_dir / QUEUED).read_bytes() == ORIGINAL_BYTES


def test_the_optimized_file_is_untouched(adopted) -> None:
    conn, settings, entry_id, _, _ = adopted
    before = (settings.library_dir / TARGET).read_bytes()

    queue_it(conn, settings, entry_id)

    assert (settings.library_dir / TARGET).read_bytes() == before
    assert files_under(settings.library_dir) == {TARGET}


def test_the_disposal_plan_is_found_by_where_the_file_is(adopted) -> None:
    """Not by `finished_at`, which has second resolution and has already picked
    the wrong plan once in this feature's history."""
    conn, settings, entry_id, _, _ = adopted

    plan_b = queue_it(conn, settings, entry_id)

    assert disposal_plan_id(conn, entry_of(conn, entry_id)) == plan_b


def test_an_ordinary_held_file_still_works_the_way_it_did(adopted) -> None:
    conn, settings, _entry_id, _, _ = adopted
    stray = settings.quarantine_dir / "2026-01-01/spare.mkv"
    stray.parent.mkdir(parents=True)
    stray.write_bytes(b"an ordinary rejected file")
    scan_root(conn, "quarantine", settings.quarantine_dir, settings)
    item_id = conn.execute(
        "SELECT id FROM items WHERE relpath='2026-01-01/spare.mkv'"
    ).fetchone()[0]
    entry_id = int(
        conn.execute(
            "INSERT INTO quarantine_entries(item_id, reason, original_root,"
            " original_relpath, quarantined_at) VALUES (?, 'user', 'inbox',"
            " 'spare.mkv', ?)",
            (item_id, utc_now()),
        ).lastrowid
    )

    plan_id = request_delete_queue(conn, settings, entry_id)
    executor.execute_plan(conn, plan_id, settings)

    assert (settings.quarantine_dir / "_to-delete/2026-01-01/spare.mkv").is_file()
    #  And it is not dragged into the optimization vocabulary on the way: the
    #  disposal states belong to preserved originals and nothing else.
    from librairy.quarantine import is_preserved_original
    from librairy.web.quarantine import _disposal_state

    assert not is_preserved_original(entry_of(conn, entry_id))
    assert _disposal_state(conn, entry_of(conn, entry_id)) == ""


# --- taking it back out -------------------------------------------------------------


def test_keep_original_reverses_only_the_disposal(adopted) -> None:
    """A different decision from Restore original: the optimized version stays
    live, and only the move into the delete queue is taken back."""
    conn, settings, entry_id, _, _ = adopted
    queue_it(conn, settings, entry_id)

    result = keep_original(conn, settings, entry_id)

    assert result.ok
    assert preserved_state(conn, entry_of(conn, entry_id)) == PRESERVED
    assert (settings.quarantine_dir / ORIGINAL).read_bytes() == ORIGINAL_BYTES
    assert (settings.library_dir / TARGET).read_bytes() == OPTIMIZED_BYTES


def test_keep_original_refuses_when_it_is_not_in_the_delete_queue(adopted) -> None:
    conn, settings, entry_id, _, _ = adopted

    result = keep_original(conn, settings, entry_id)

    assert not result.ok
    assert result.code == "not-queued"


def test_it_can_be_queued_again_afterwards(adopted) -> None:
    """And the second disposal is found by the file, not by which plan is
    newest — the reason `disposal_plan_id` looks at the destination."""
    conn, settings, entry_id, _, _ = adopted
    first = queue_it(conn, settings, entry_id)
    keep_original(conn, settings, entry_id)

    second = queue_it(conn, settings, entry_id)

    assert second != first
    assert disposal_plan_id(conn, entry_of(conn, entry_id)) == second


# --- restore original, from either state --------------------------------------------


def test_restore_from_preserved_undoes_the_adoption(adopted) -> None:
    conn, settings, entry_id, job_id, _ = adopted

    result = restore_original(conn, settings, entry_id)

    assert result.ok
    assert (settings.library_dir / ORIGINAL).read_bytes() == ORIGINAL_BYTES
    assert not (settings.library_dir / TARGET).exists()
    staged = settings.appdata_dir / "optimization" / "jobs" / str(job_id) / "output.flac"
    assert staged.read_bytes() == OPTIMIZED_BYTES


def test_restore_from_the_delete_queue_reverses_both_plans(adopted) -> None:
    conn, settings, entry_id, job_id, _ = adopted
    queue_it(conn, settings, entry_id)

    result = restore_original(conn, settings, entry_id)

    assert result.ok
    #  The original is the live file again, byte for byte.
    assert (settings.library_dir / ORIGINAL).read_bytes() == ORIGINAL_BYTES
    #  Nothing is left in quarantine at all — not at the preserved path and not
    #  in the delete queue.
    assert files_under(settings.quarantine_dir) == set()
    #  And the optimized copy is back where its job can offer it again.
    staged = settings.appdata_dir / "optimization" / "jobs" / str(job_id) / "output.flac"
    assert staged.read_bytes() == OPTIMIZED_BYTES


def test_the_result_item_goes_dormant_and_leaves_search(adopted) -> None:
    from librairy.reserved import is_dormant_optimization
    from librairy.search import search_items

    conn, settings, entry_id, job_id, _ = adopted
    queue_it(conn, settings, entry_id)

    restore_original(conn, settings, entry_id)

    result = conn.execute(
        "SELECT i.relpath, i.missing_since FROM optimization_jobs j"
        " JOIN items i ON i.id = j.result_item_id WHERE j.id=?",
        (job_id,),
    ).fetchone()
    assert result["missing_since"] is not None
    assert is_dormant_optimization(result["relpath"])
    assert "concert.flac" not in " ".join(
        row["relpath"] for row in search_items(conn, "concert")
    )


def test_the_entry_is_settled_so_the_row_stops_offering_actions(adopted) -> None:
    conn, settings, entry_id, _, _ = adopted
    queue_it(conn, settings, entry_id)

    restore_original(conn, settings, entry_id)

    assert preserved_state(conn, entry_of(conn, entry_id)) == RESTORED


# --- everything is checked before anything moves ------------------------------------


def test_a_missing_original_refuses_before_the_optimized_file_moves(adopted) -> None:
    """The race the specification names: the reversal starts, and the original
    has been deleted in between. The one outcome worth any amount of care to
    avoid is a library holding neither version."""
    conn, settings, entry_id, _, _ = adopted
    queue_it(conn, settings, entry_id)
    (settings.quarantine_dir / QUEUED).unlink()

    result = restore_original(conn, settings, entry_id)

    assert not result.ok
    assert result.code == "blocked-disposal"
    assert (settings.library_dir / TARGET).read_bytes() == OPTIMIZED_BYTES


def test_an_edited_original_refuses_the_same_way(adopted) -> None:
    conn, settings, entry_id, _, _ = adopted
    queue_it(conn, settings, entry_id)
    (settings.quarantine_dir / QUEUED).write_bytes(b"something else entirely")

    result = restore_original(conn, settings, entry_id)

    assert not result.ok
    assert "edited" in result.message
    assert (settings.library_dir / TARGET).read_bytes() == OPTIMIZED_BYTES


def test_a_missing_optimized_file_refuses_before_the_original_moves(adopted) -> None:
    """The other direction, and the reason A is preflighted too rather than
    just run after B: the original stays in the delete queue rather than being
    taken out for a reversal that could not have finished."""
    conn, settings, entry_id, _, _ = adopted
    queue_it(conn, settings, entry_id)
    (settings.library_dir / TARGET).unlink()

    result = restore_original(conn, settings, entry_id)

    assert not result.ok
    assert result.code == "blocked-adoption"
    assert (settings.quarantine_dir / QUEUED).read_bytes() == ORIGINAL_BYTES


def test_a_waiting_request_blocks_the_reversal_rather_than_racing_it(adopted) -> None:
    conn, settings, entry_id, _, _ = adopted
    request_delete_queue(conn, settings, entry_id)

    result = restore_original(conn, settings, entry_id)

    assert not result.ok
    assert result.code == "waiting"
    assert (settings.quarantine_dir / ORIGINAL).is_file()


# --- after somebody really deletes it -----------------------------------------------


def remove_it(conn, settings) -> None:
    """What the owner does in their own file manager, and what LibrAIry does
    about it: notices, on the next cycle, that the file is not there."""
    (settings.quarantine_dir / QUEUED).unlink()
    scan_root(conn, "quarantine", settings.quarantine_dir, settings)


def test_an_external_deletion_is_observed(adopted) -> None:
    conn, settings, entry_id, _, _ = adopted
    queue_it(conn, settings, entry_id)

    remove_it(conn, settings)

    assert preserved_state(conn, entry_of(conn, entry_id)) == REMOVED


def test_the_provenance_survives_it(adopted) -> None:
    """The item row, its fingerprint, the adoption plan, the job, the result
    item and the journal all stay. This is how the optimization explains where
    its original went, and nothing sweeps it up."""
    conn, settings, entry_id, job_id, plan_a = adopted
    queue_it(conn, settings, entry_id)
    entry = entry_of(conn, entry_id)

    remove_it(conn, settings)

    item = conn.execute(
        "SELECT * FROM items WHERE id=?", (entry["item_id"],)
    ).fetchone()
    assert item is not None
    assert item["fingerprint"]
    assert conn.execute("SELECT 1 FROM plans WHERE id=?", (plan_a,)).fetchone()
    assert conn.execute(
        "SELECT 1 FROM optimization_jobs WHERE id=?", (job_id,)
    ).fetchone()
    assert conn.execute(
        "SELECT COUNT(*) FROM history WHERE plan_id=?", (plan_a,)
    ).fetchone()[0] == 2


def test_the_optimized_version_is_still_the_live_one(adopted) -> None:
    conn, settings, entry_id, job_id, _ = adopted
    queue_it(conn, settings, entry_id)

    remove_it(conn, settings)

    assert (settings.library_dir / TARGET).read_bytes() == OPTIMIZED_BYTES
    result = conn.execute(
        "SELECT i.missing_since FROM optimization_jobs j JOIN items i"
        " ON i.id = j.result_item_id WHERE j.id=?",
        (job_id,),
    ).fetchone()
    assert result["missing_since"] is None


def test_undo_is_honestly_unavailable(adopted) -> None:
    conn, settings, entry_id, _, _ = adopted
    queue_it(conn, settings, entry_id)
    remove_it(conn, settings)

    result = restore_original(conn, settings, entry_id)

    assert not result.ok
    assert result.code == "removed"
    assert "no longer stored" in result.message


def test_the_missing_original_leaves_the_active_workflows(adopted) -> None:
    """It is missing, in exactly the sense `live.py` owns — so every consumer
    that asks "is this item current and physically available" gets the same
    answer without knowing anything about optimization."""
    from librairy.backup import backup_queue_issues
    from librairy.search import search_items

    conn, settings, entry_id, _, _ = adopted
    queue_it(conn, settings, entry_id)
    remove_it(conn, settings)
    entry = entry_of(conn, entry_id)

    assert conn.execute(
        "SELECT missing_since FROM items WHERE id=?", (entry["item_id"],)
    ).fetchone()[0] is not None
    assert not [row for row in search_items(conn, "concert") if "concert.wav" in row["relpath"]]
    assert not [issue for issue in backup_queue_issues(conn) if issue.code == "shadowed-request"]


# --- and only now is there a saving -------------------------------------------------


def test_the_arithmetic_at_each_step(adopted) -> None:
    """O = 100, N = 60. The numbers from the specification, in bytes."""
    from librairy.optimization_storage import (
        ADOPTED,
        ORIGINAL_IN_DELETE_QUEUE,
        ORIGINAL_REMOVED,
        storage_effect,
    )

    for state in (ADOPTED, ORIGINAL_IN_DELETE_QUEUE):
        effect = storage_effect(100, 60, state)
        assert effect.physical_bytes_now == 160
        assert effect.current_extra_storage_bytes == 60
        assert effect.representation_reduction_bytes == 40
        assert effect.reclaimed_now_bytes == 0

    removed = storage_effect(100, 60, ORIGINAL_REMOVED)
    assert removed.physical_bytes_now == 60
    assert removed.current_extra_storage_bytes == 0
    assert removed.reclaimed_now_bytes == 40
    assert removed.final_net_reduction_bytes == 40


def test_the_delete_queue_frees_nothing_by_itself(adopted) -> None:
    conn, settings, entry_id, _, _ = adopted
    queue_it(conn, settings, entry_id)

    counts = outcomes(conn)

    assert counts["in_delete_queue"] == 1
    assert counts["removed"] == 0
    assert counts["realized_bytes"] == 0


def test_the_reduction_becomes_real_only_when_the_file_does_not_exist(
    adopted,
) -> None:
    conn, settings, entry_id, _, _ = adopted
    queue_it(conn, settings, entry_id)
    remove_it(conn, settings)

    counts = outcomes(conn)

    assert counts["removed"] == 1
    assert counts["in_delete_queue"] == 0
    #  40, not 100. The deletion freed 100 bytes at that moment; the library
    #  ended up 40 smaller than it started, and those are different questions.
    assert counts["realized_bytes"] == 40


def test_an_undone_adoption_contributes_nothing(adopted) -> None:
    conn, settings, entry_id, _, _ = adopted

    restore_original(conn, settings, entry_id)

    assert outcomes(conn) == {
        "adopted": 0,
        "removed": 0,
        "in_delete_queue": 0,
        "preserved": 0,
        "realized_bytes": 0,
    }


# --- and the numbers on every other page -------------------------------------------


def test_the_dashboard_stops_counting_a_file_you_deleted(adopted) -> None:
    """Emptying the delete queue is something LibrAIry asks people to do
    themselves. Until this, the tile went on counting those files and adding
    their sizes into a total describing disk that was no longer in use."""
    from librairy.web.dashboard import dashboard_data

    conn, settings, entry_id, _, _ = adopted
    queue_it(conn, settings, entry_id)
    before = {
        surface["label"]: surface for surface in dashboard_data(conn, settings)["surfaces"]
    }
    assert before["Quarantine"]["count"] == 1

    remove_it(conn, settings)

    after = {
        surface["label"]: surface for surface in dashboard_data(conn, settings)["surfaces"]
    }
    assert after["Quarantine"]["count"] == 0
    assert dashboard_data(conn, settings)["delete_queue_count"] == 0


def test_one_original_is_never_in_two_buckets(adopted) -> None:
    from librairy.web.quarantine import quarantine_data

    conn, settings, entry_id, _, _ = adopted

    for step in (lambda: None, lambda: queue_it(conn, settings, entry_id),
                 lambda: remove_it(conn, settings)):
        step()
        counts = quarantine_data(conn, settings)["counts"]
        buckets = [counts[view] for view in
                   ("held", "waiting", "delete-queue", "removed", "restored")]
        assert sum(buckets) == 1, counts
