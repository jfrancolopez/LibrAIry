"""A decision about a quarantined file, waiting for Commit like everything else.

It did not work this way. `Mark for deletion` on a held file called the
executor's move helper *inside the request handler*: the file moved to
`_to-delete` before the response was written. No plan, nothing in Commit,
nothing to send back — and the only sign it had happened was a one-line
confirmation swapped into a `<div>` at the very bottom of a long page, under a
row that did not change. Pressing it looked exactly like pressing nothing.

Worse, the button beside it on the same page, with the *same label*, did the
opposite: on a staged proposal it set an intent and waited for Commit. One
label, two semantics, one immediate and one deferred. That is the whole of "I
marked a file for deletion, nothing happened, and later I found it in Commit".

So a quarantine decision is now a plan. Reusing `plans` rather than inventing a
pending-quarantine table is the point — the fingerprint check before execution,
the journal entry, and the existing Undo all come with it, and Commit can list
a quarantine move beside a library correction because they are now the same
kind of thing.

Two decisions exist, and both are moves:

    delete queue   quarantine/X            -> quarantine/_to-delete/X
    restore        quarantine/X            -> <original root>/<original path>

Neither deletes anything. LibrAIry has never deleted a file and does not start
here: the delete queue is a folder you empty yourself, deliberately, in your
own file manager.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from librairy.config import Settings
from librairy.correction_state import ACTIVE_PLAN_STATUSES
from librairy.fingerprint import blake2b_file
from librairy.paths import validate_relpath
from librairy.planner import OperationSpec, approve_plan, create_plan, utc_now
from librairy.quarantine import (
    DELETE_PILE,
    QuarantineError,
    is_preserved_original,
    marked_for_deletion,
    remember_restored_comparison,
)

# What the user chose, derived from where the plan points rather than stored a
# second time. Two columns that both claim to say what a decision was can
# disagree; one cannot.
DELETE_QUEUE = "delete-queue"
RESTORE = "restore"
#  A held representation taking the place of the filed one it was compared
#  with. Two operations and one decision, which is exactly what `coherent`
#  means — so the intent is read off that rather than off a destination. A
#  replacement's first operation quarantines a *library* file, and reading only
#  that would call the whole thing a restore, which is the one word it must not
#  be called at the last moment before bytes move.
REPLACE = "replace"

# Said to a person, once, so the row, the Commit card and the confirmation all
# use the same words.
LABEL = {
    DELETE_QUEUE: "Move to delete queue",
    RESTORE: "Restore",
    REPLACE: "Use this instead",
}
OUTCOME = {
    DELETE_QUEUE: "Will move to the delete queue. Nothing is deleted.",
    RESTORE: "Will go back where it came from.",
    REPLACE: "Will take the place of the filed version, which is preserved first.",
}


@dataclass(frozen=True)
class Request:
    """A quarantine decision that has been made and not yet carried out."""

    plan_id: str
    entry_id: int
    intent: str
    src_relpath: str
    dest_root: str
    dest_relpath: str
    status: str

    @property
    def label(self) -> str:
        return LABEL[self.intent]

    @property
    def applying(self) -> bool:
        return self.status == "executing"


def intent_of(dest_root: str, dest_relpath: str) -> str:
    """Which decision this plan represents, read off its destination."""
    if dest_root == "quarantine" and marked_for_deletion(dest_relpath):
        return DELETE_QUEUE
    return RESTORE


def _intent(row: sqlite3.Row) -> str:
    """Which decision this plan is, from the plan rather than from one row.

    Coherent means the plan's operations are one decision that must happen
    together, and the only such decision about a held file is a replacement.
    Everything else is a single move and says what it is by where it points.
    """
    if row["coherent"]:
        return REPLACE
    return intent_of(row["dest_root"], row["dest_relpath"])


def pending_request(conn: sqlite3.Connection, entry_id: int) -> Request | None:
    """The decision waiting on this held file, if there is one."""
    placeholders = ",".join("?" * len(ACTIVE_PLAN_STATUSES))
    row = conn.execute(
        f"""
        SELECT p.id, p.status, p.coherent, o.src_relpath, o.dest_root, o.dest_relpath
        FROM plans p JOIN plan_ops o ON o.plan_id = p.id
        WHERE p.quarantine_entry_id = ? AND p.status IN ({placeholders})
          AND o.src_root = 'quarantine'
        ORDER BY o.seq LIMIT 1
        """,  # noqa: S608 — placeholders are a module constant, never input
        (entry_id, *ACTIVE_PLAN_STATUSES),
    ).fetchone()
    if row is None:
        return None
    return Request(
        plan_id=row["id"],
        entry_id=entry_id,
        intent=_intent(row),
        src_relpath=row["src_relpath"],
        dest_root=row["dest_root"],
        dest_relpath=row["dest_relpath"],
        status=row["status"],
    )


def pending_requests(conn: sqlite3.Connection) -> dict[int, Request]:
    """Every pending decision, by entry id — one query for a whole page.

    The page renders a bounded number of rows and asks for their requests in
    one go. A `pending_request` per row is the same query N times, which is
    exactly the shape that stops working at ten thousand.
    """
    placeholders = ",".join("?" * len(ACTIVE_PLAN_STATUSES))
    rows = conn.execute(
        f"""
        SELECT p.id, p.status, p.coherent, p.quarantine_entry_id AS entry_id,
               o.src_relpath, o.dest_root, o.dest_relpath
        FROM plans p JOIN plan_ops o ON o.plan_id = p.id
        WHERE p.quarantine_entry_id IS NOT NULL AND p.status IN ({placeholders})
          AND o.src_root = 'quarantine'
        ORDER BY o.seq
        """,  # noqa: S608 — placeholders are a module constant, never input
        ACTIVE_PLAN_STATUSES,
    ).fetchall()
    return {
        int(row["entry_id"]): Request(
            plan_id=row["id"],
            entry_id=int(row["entry_id"]),
            intent=_intent(row),
            src_relpath=row["src_relpath"],
            dest_root=row["dest_root"],
            dest_relpath=row["dest_relpath"],
            status=row["status"],
        )
        for row in rows
    }


def _entry_and_item(conn: sqlite3.Connection, entry_id: int):
    entry = conn.execute(
        "SELECT * FROM quarantine_entries WHERE id=?", (entry_id,)
    ).fetchone()
    if entry is None:
        raise QuarantineError("that quarantine record no longer exists")
    if entry["restored_at"] is not None:
        raise QuarantineError("this file has already been put back")
    item = conn.execute("SELECT * FROM items WHERE id=?", (entry["item_id"],)).fetchone()
    if item is None or item["root"] != "quarantine":
        raise QuarantineError("this file is no longer in quarantine")
    return entry, item


def _request(
    conn: sqlite3.Connection,
    settings: Settings,
    entry_id: int,
    intent: str,
    dest_root: str,
    dest_relpath: str,
) -> str:
    """Write down one decision as an approved, unexecuted plan.

    Every refusal lives here rather than in the template, for the same reason
    approving a correction does: a button that is not drawn is not a safety
    guarantee, and the same request can arrive from a stale page or from curl.
    """
    entry, item = _entry_and_item(conn, entry_id)
    if intent == RESTORE and is_preserved_original(entry):
        # One door, checked here rather than only where the button is drawn: a
        # button that is not drawn is not a safety guarantee, and this request
        # can arrive from a stale page or from curl. Generic Restore would put
        # the original back beside the optimized copy and leave the job
        # believing it had been adopted; un-adopting moves two files in an
        # order only Undo knows.
        raise QuarantineError(
            "this is a preserved original; use Restore original, which undoes "
            "the optimization that replaced it"
        )
    if pending_request(conn, entry_id) is not None:
        raise QuarantineError("a decision on this file is already waiting for Commit")
    source = validate_relpath(settings.quarantine_dir, item["relpath"], kind="source")
    if not source.is_file():
        raise QuarantineError("this file is not where LibrAIry last saw it")
    plan_id = create_plan(
        conn,
        [
            OperationSpec(
                op_type="move",
                src_root="quarantine",
                src_relpath=item["relpath"],
                dest_root=dest_root,
                dest_relpath=dest_relpath,
            )
        ],
        settings,
    )
    conn.execute(
        "UPDATE plans SET quarantine_entry_id=? WHERE id=?", (entry_id, plan_id)
    )
    try:
        approve_plan(conn, plan_id, settings)
    except sqlite3.IntegrityError as exc:
        conn.execute("DELETE FROM plan_ops WHERE plan_id=?", (plan_id,))
        conn.execute("DELETE FROM plans WHERE id=?", (plan_id,))
        raise QuarantineError(
            "a decision on this file was recorded a moment ago"
        ) from exc
    return plan_id


def request_delete_queue(
    conn: sqlite3.Connection, settings: Settings, entry_id: int
) -> str:
    """"I am finished with this file."

    It gathers in one folder so you can empty that folder yourself, in one
    gesture, when you choose to. Nothing is deleted by this, by Commit, or by
    anything else LibrAIry does.

    Preserved optimization originals may come this way too, which is the whole
    point of Storage Optimization finally reaching an end. Their Undo depends on
    the exact path this move changes, so the reversal reverses this plan first
    and the adoption second — see `optimization_disposal`. Nothing about that
    is guessed: this plan records the quarantine entry, and the entry records
    the adoption.
    """
    _entry, item = _entry_and_item(conn, entry_id)
    if marked_for_deletion(item["relpath"]):
        raise QuarantineError("this file is already in the delete queue")
    return _request(
        conn,
        settings,
        entry_id,
        DELETE_QUEUE,
        "quarantine",
        f"{DELETE_PILE}/{item['relpath']}",
    )


def request_restore(conn: sqlite3.Connection, settings: Settings, entry_id: int) -> str:
    """Put it back where it came from — after Commit, not now."""
    entry, _item = _entry_and_item(conn, entry_id)
    if not restorable(entry):
        raise QuarantineError(
            "LibrAIry does not have a safe original location for this file"
        )
    return _request(
        conn,
        settings,
        entry_id,
        RESTORE,
        entry["original_root"],
        entry["original_relpath"],
    )


def restorable(entry: sqlite3.Row) -> bool:
    """Can this file be put back somewhere LibrAIry can name?

    Asked before the button is drawn *and* again before the plan is written. A
    Restore control on a row whose origin was never recorded is a control that
    can only produce an error, which is the thing this pass exists to remove.
    """
    return bool(entry["original_root"]) and bool(entry["original_relpath"])


def cancel_request(conn: sqlite3.Connection, entry_id: int) -> None:
    """Take a decision back before anything has moved.

    Deliberately not Undo — nothing has happened yet. Same shape as withdrawing
    a correction's approval, and safe for the same reason: an unexecuted plan
    has no journal entry, no moved file and no partial state to reconcile.
    """
    request = pending_request(conn, entry_id)
    if request is None:
        raise QuarantineError("there is no decision waiting on this file")
    if request.applying:
        raise QuarantineError("this has already started and cannot be recalled")
    executed = conn.execute(
        "SELECT COUNT(*) AS n FROM plan_ops WHERE plan_id=? AND executed_at IS NOT NULL",
        (request.plan_id,),
    ).fetchone()["n"]
    if executed:
        raise QuarantineError("part of this has already run")
    conn.execute("DELETE FROM plan_ops WHERE plan_id=?", (request.plan_id,))
    conn.execute("DELETE FROM plans WHERE id=?", (request.plan_id,))


def settle_quarantine_plan(conn: sqlite3.Connection, plan_id: str) -> None:
    """Close the loop after a quarantine decision has run.

    Called from `execute_plan`, the one door both the web commit and the CLI go
    through. A restore that completed marks the entry as restored, which is
    what takes the row off the page; a delete-queue move leaves the entry
    exactly where it is, because the file is still held and still yours.
    """
    plan = conn.execute(
        "SELECT status, quarantine_entry_id FROM plans WHERE id=?", (plan_id,)
    ).fetchone()
    if plan is None or plan["quarantine_entry_id"] is None:
        return
    if plan["status"] != "done":
        return
    #  The operation that moves the held file *out* of Quarantine, which is
    #  what settles the entry. A replacement's first operation moves a library
    #  file in the other direction, and reading that one would settle the wrong
    #  half of the decision.
    op = conn.execute(
        "SELECT dest_root, dest_relpath FROM plan_ops"
        " WHERE plan_id=? AND src_root='quarantine' ORDER BY seq LIMIT 1",
        (plan_id,),
    ).fetchone()
    if op is None:
        return
    #  A replacement's quarantine-side operation is also a move back into the
    #  library, so it settles the entry by the same rule: the file is no longer
    #  held, whether it came back beside the other one or in its place.
    if intent_of(op["dest_root"], op["dest_relpath"]) == RESTORE:
        entry = conn.execute(
            "SELECT * FROM quarantine_entries WHERE id=?",
            (plan["quarantine_entry_id"],),
        ).fetchone()
        conn.execute(
            "UPDATE quarantine_entries SET restored_at=? WHERE id=?",
            (utc_now(), plan["quarantine_entry_id"]),
        )
        if entry is not None:
            #  The same memory the direct restore records, and this is the path
            #  the product actually takes: pressing Restore makes a *request*,
            #  and the file moves when the person commits it. Hooking only the
            #  direct call left the suppression working in tests and never once
            #  in the application.
            remember_restored_comparison(conn, entry, int(entry["item_id"]))


def verify_unchanged(settings: Settings, relpath: str, fingerprint: str) -> bool:
    """Whether the held file still matches what the decision was made against."""
    try:
        path = validate_relpath(settings.quarantine_dir, relpath, kind="source")
    except Exception:
        return False
    return path.is_file() and blake2b_file(path) == fingerprint
