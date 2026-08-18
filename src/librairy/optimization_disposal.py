"""What happens to the original after its optimized version has been adopted.

Adoption leaves two files on the disk and frees nothing. That is the honest
outcome of a decision about *representation*, but it is not the outcome anybody
wanted from a feature called Storage Optimization: the point was the space, and
the space is still there, sitting in Quarantine under a card that says so.

So there has to be a way through to the end. There is exactly one, and it is
the same one every other file in this application gets:

    preserved original
        -> Delete queue          a request, committed like any other
        -> quarantine/_to-delete a move, still intact, still yours
        -> you delete it         in your own file manager, deliberately

LibrAIry does not delete it at step three, or at any other step. It never has
and this does not start.

## Why this was withheld until now

Undo finds the preserved original at the exact path the adoption plan put it
at. Moving it into `_to-delete` changes that path, so the adoption's Undo would
afterwards find nothing there and refuse — correctly, but permanently, and with
no way back for a person who changed their mind.

The fix is not to teach Undo to go looking. A journal that says where a file
went and an Undo that searches for it by content are two different promises,
and the second one is unfalsifiable: it cannot tell a file you moved back
yourself from one it found by luck. So the path change stays journalled and
explicit, and the reversal simply *reverses both plans, in order*:

    plan A   library/<original>   -> quarantine/<preserved>      (adoption)
             optimization/<out>   -> library/<target>

    plan B   quarantine/<preserved> -> quarantine/_to-delete/... (disposal)

    Restore original  =  undo B, then undo A

## The dependency needed no new column

B is a plan with `quarantine_entry_id` set. That entry *is* the preserved
original, and it already records both halves: `plan_id` is the adoption that
created it, and `optimization_job_id` is the job. So "B moved the file A
depends on" is not a fact that had to be stored — it is the only way these rows
can be arranged.

Which B, when there have been several, is answered by the file rather than by a
timestamp: the disposal plan is the one whose destination is where the original
actually is now. `finished_at` has second resolution and has already picked the
wrong plan once in this feature's history.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from librairy.config import Settings
from librairy.quarantine import (
    QuarantineError,
    is_preserved_original,
    marked_for_deletion,
)

#  Where a preserved original stands. Not stored: derived from the file, the
#  item row and the plans, so it cannot disagree with any of them.
PRESERVED = "preserved"
WAITING = "waiting-for-commit"
IN_DELETE_QUEUE = "in-delete-queue"
REMOVED = "removed"
RESTORED = "restored"

#  The heading on the card, in the words the rest of the application uses.
STATE_LABEL = {
    PRESERVED: "Preserved original",
    WAITING: "Waiting for Commit",
    IN_DELETE_QUEUE: "Original in delete queue",
    REMOVED: "Original removed",
    RESTORED: "Original put back",
}


@dataclass(frozen=True)
class Outcome:
    """What a reversal did, and what the person is now looking at."""

    ok: bool
    code: str
    message: str


def preserved_state(conn: sqlite3.Connection, entry: sqlite3.Row) -> str:
    """Where this preserved original is in its life, read off what is true.

    Order matters. A file that is gone is gone whatever else is recorded about
    it — a pending request on a deleted file is not a decision anybody can
    still carry out, and offering to carry it out would be the dead button this
    whole pass exists to remove.
    """
    if entry["restored_at"] is not None:
        return RESTORED
    item = conn.execute(
        "SELECT relpath, missing_since FROM items WHERE id=?", (entry["item_id"],)
    ).fetchone()
    if item is None or item["missing_since"] is not None:
        return REMOVED
    from librairy.quarantine_requests import pending_request

    if pending_request(conn, int(entry["id"])) is not None:
        return WAITING
    if marked_for_deletion(item["relpath"]):
        return IN_DELETE_QUEUE
    return PRESERVED


def adoption_plan_id(entry: sqlite3.Row) -> str:
    """The plan that preserved this file. Recorded, never guessed."""
    return str(entry["plan_id"] or "")


def disposal_plan_id(conn: sqlite3.Connection, entry: sqlite3.Row) -> str:
    """The executed plan that put this original where it is sitting now.

    Found through the journal, which is the only record that is both specific
    and ordered. After a queue, a Keep original and a second queue there are two
    finished disposal plans for one entry with the *same destination path*, so
    the destination alone does not separate them; `finished_at` has second
    resolution and picking by it has already reversed the wrong plan once in
    this feature's history; and plan ids are UUIDs, so "the newest id" is not a
    thing that exists.

    `history.id` is a monotonic integer and every row in it is a move that
    actually happened. The most recent one that put a file at this exact path
    is the move that is standing right now, by construction.
    """
    item = conn.execute(
        "SELECT relpath FROM items WHERE id=?", (entry["item_id"],)
    ).fetchone()
    if item is None or not marked_for_deletion(item["relpath"]):
        return ""
    row = conn.execute(
        """
        SELECT h.plan_id FROM history h JOIN plans p ON p.id = h.plan_id
        WHERE p.quarantine_entry_id = ? AND h.outcome = 'ok'
          AND h.action IN ('move','quarantine')
          AND h.dest_root = 'quarantine' AND h.dest_relpath = ?
        ORDER BY h.id DESC LIMIT 1
        """,
        (int(entry["id"]), item["relpath"]),
    ).fetchone()
    return str(row["plan_id"]) if row else ""


def _entry(conn: sqlite3.Connection, entry_id: int) -> sqlite3.Row:
    entry = conn.execute(
        "SELECT * FROM quarantine_entries WHERE id=?", (int(entry_id),)
    ).fetchone()
    if entry is None:
        raise QuarantineError("that quarantine record no longer exists")
    if not is_preserved_original(entry):
        raise QuarantineError("that is not a preserved original")
    return entry


def _blocked(blockers, settings_note: str = "") -> str:
    from librairy.history import BLOCKER_TEXT

    reasons = {BLOCKER_TEXT.get(blocker.code, blocker.code) for blocker in blockers}
    return f"{settings_note}{'; '.join(sorted(reasons))}."


def restore_original(
    conn: sqlite3.Connection, settings: Settings, entry_id: int
) -> Outcome:
    """Put the original back as the live file. One decision, up to two reversals.

    From the delete queue that is B then A, and both are checked before either
    runs. The order is not a preference: reversing A first would move the
    optimized file out of the library and only then discover that the original
    cannot come back, which is the one outcome worth any amount of care to
    avoid — a library with neither version in it.
    """
    from librairy.history import undo_plan, undo_preflight

    entry = _entry(conn, entry_id)
    state = preserved_state(conn, entry)
    if state == REMOVED:
        return Outcome(
            False,
            "removed",
            "The original is no longer stored, so LibrAIry cannot put it back.",
        )
    if state == RESTORED:
        return Outcome(False, "restored", "This original has already been put back.")
    if state == WAITING:
        return Outcome(
            False,
            "waiting",
            "A decision on this file is waiting for Commit. Cancel it first.",
        )

    plan_a = adoption_plan_id(entry)
    if not plan_a:
        return Outcome(
            False,
            "no-adoption",
            "LibrAIry cannot find the optimization that preserved this file.",
        )
    plan_b = disposal_plan_id(conn, entry) if state == IN_DELETE_QUEUE else ""
    if state == IN_DELETE_QUEUE and not plan_b:
        return Outcome(
            False,
            "no-disposal",
            "This original is in the delete queue and LibrAIry cannot find the "
            "move that put it there.",
        )

    #  Everything, before anything.
    blockers = list(undo_preflight(conn, settings, plan_b)) if plan_b else []
    if blockers:
        return Outcome(
            False,
            "blocked-disposal",
            _blocked(blockers, "The original cannot come out of the delete queue: "),
        )
    if plan_b and not _same_bytes(conn, plan_a, plan_b):
        return Outcome(
            False,
            "chain",
            "The file in the delete queue is not the original this optimization "
            "preserved.",
        )
    blockers = undo_preflight(
        conn, settings, plan_a, skip=_relocated(conn, plan_a, plan_b)
    )
    if blockers:
        return Outcome(
            False,
            "blocked-adoption",
            _blocked(blockers, "The optimization cannot be undone: "),
        )

    if plan_b:
        results = undo_plan(conn, plan_b, settings)
        if any(result.outcome != "ok" for result in results):
            return Outcome(
                False,
                "disposal-failed",
                "The original is still in the delete queue and nothing else was "
                "touched. See History.",
            )
    results = undo_plan(conn, plan_a, settings)
    if any(result.outcome != "ok" for result in results):
        #  Not a compensation, because there is nothing to compensate: the
        #  original is back where a preserved original belongs, the optimized
        #  version is still the live one, and that is a state this application
        #  already knows how to describe. Putting the original *back* into the
        #  delete queue to tidy up would be moving a file nobody asked to move.
        return Outcome(
            False,
            "adoption-failed",
            "The original is back in Quarantine as a preserved original, but the "
            "optimization could not be undone. See History.",
        )
    return Outcome(
        True,
        "ok",
        "The original is back in the library, and the optimized copy is waiting "
        "for review again.",
    )


def keep_original(
    conn: sqlite3.Connection, settings: Settings, entry_id: int
) -> Outcome:
    """Take the original out of the delete queue and leave everything else alone.

    A different decision from Restore original, and worth its own button: "I
    have changed my mind about deleting this" is not "I want the old file back".
    The optimized version stays live and the original goes back to being
    preserved.
    """
    from librairy.history import undo_plan, undo_preflight

    entry = _entry(conn, entry_id)
    state = preserved_state(conn, entry)
    if state != IN_DELETE_QUEUE:
        return Outcome(False, "not-queued", "This original is not in the delete queue.")
    plan_b = disposal_plan_id(conn, entry)
    if not plan_b:
        return Outcome(
            False,
            "no-disposal",
            "LibrAIry cannot find the move that put this file in the delete queue.",
        )
    blockers = undo_preflight(conn, settings, plan_b)
    if blockers:
        return Outcome(False, "blocked-disposal", _blocked(blockers))
    results = undo_plan(conn, plan_b, settings)
    if any(result.outcome != "ok" for result in results):
        return Outcome(
            False,
            "disposal-failed",
            "The original could not be taken out of the delete queue. See History.",
        )
    return Outcome(
        True,
        "ok",
        "The original is preserved again. The optimized version is still the one "
        "in your library.",
    )


def _same_bytes(conn: sqlite3.Connection, plan_a: str, plan_b: str) -> bool:
    """Is the file B moved the file A preserved, still unchanged?

    Both journals record a fingerprint, so this is one comparison rather than a
    search. It is what makes reversing two plans in sequence a chain instead of
    a coincidence.
    """
    from librairy.history import plan_journal

    preserved = [
        entry for entry in plan_journal(conn, plan_a) if entry["dest_root"] == "quarantine"
    ]
    moved = plan_journal(conn, plan_b)
    if not preserved or not moved:
        return False
    return bool(preserved[0]["fingerprint"]) and preserved[0]["fingerprint"] == (
        moved[0]["fingerprint"]
    )


def _relocated(
    conn: sqlite3.Connection, plan_a: str, plan_b: str
) -> frozenset[int]:
    """A's operations whose file B is holding, so A's preflight can skip them.

    Their journalled destination is legitimately empty right now — the file is
    in the delete queue — and it stops being empty the moment B is reversed,
    which happens before A is touched.
    """
    if not plan_b:
        return frozenset()
    from librairy.history import plan_journal

    sources = {
        (entry["src_root"], entry["src_relpath"]) for entry in plan_journal(conn, plan_b)
    }
    return frozenset(
        entry["id"]
        for entry in plan_journal(conn, plan_a)
        if (entry["dest_root"], entry["dest_relpath"]) in sources
    )


#  Only these contribute to a realized total. An adoption whose original is
#  still on the disk has reduced storage by nothing, however much smaller the
#  new file is, and that is the whole reason this aggregate is written once
#  here rather than assembled in a template.
def outcomes(conn: sqlite3.Connection) -> dict[str, int]:
    """How many optimizations stand in each state, and the one honest total.

    `realized_bytes` sums `original - optimized` over exactly the jobs whose
    preserved original LibrAIry can see is gone. Not what removing them *would*
    free — that is a different number, larger than this one, and describing
    either as the other is the confusion this feature has been avoiding since
    the first storage card.

    One query, no rows in Python: the counts stay true at any size.
    """
    row = conn.execute(
        """
        SELECT
          COUNT(*) AS adopted,
          COALESCE(SUM(CASE WHEN gone THEN 1 ELSE 0 END), 0) AS removed,
          COALESCE(SUM(CASE WHEN NOT gone AND queued THEN 1 ELSE 0 END), 0)
            AS in_delete_queue,
          COALESCE(SUM(CASE WHEN NOT gone AND NOT queued THEN 1 ELSE 0 END), 0)
            AS preserved,
          COALESCE(SUM(CASE WHEN gone THEN source_bytes - actual_bytes ELSE 0 END), 0)
            AS realized_bytes
        FROM (
          SELECT
            (i.id IS NULL OR i.missing_since IS NOT NULL) AS gone,
            (i.relpath LIKE '_to-delete/%' ESCAPE '\\') AS queued,
            j.source_bytes AS source_bytes, j.actual_bytes AS actual_bytes
          FROM quarantine_entries qe
          JOIN optimization_jobs j ON j.id = qe.optimization_job_id
          LEFT JOIN items i ON i.id = qe.item_id
          WHERE qe.restored_at IS NULL
        )
        """
    ).fetchone()
    keys = ("adopted", "removed", "in_delete_queue", "preserved", "realized_bytes")
    return {key: int(row[key] or 0) for key in keys}

