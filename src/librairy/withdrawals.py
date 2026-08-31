"""Decisions that were taken back before anything moved.

Withdrawing an approval has always been possible and has never been visible.
That was tolerable while it was rare. It is not rare any more: two waiting
decisions can now be found to contradict each other, and the way out is for
somebody to send one of them back — so the program actively asks people to
withdraw things, and then keeps no legible account of what they withdrew or
why.

**This is not History.** History is the journal of operations that moved files.
A withdrawal moved nothing: that is the whole point of withdrawing rather than
undoing. Putting one in the journal would claim a move that never happened, and
would put a row in front of Undo that Undo cannot reverse because there is
nothing there to reverse.

**Reasons are recorded, never inferred.** A withdrawal knows why it happened at
the moment it happens — the route that did it knows whether somebody pressed
*Send back to Review* or *Cancel request*, and whether there was a conflicting
decision at the time. Working it out afterwards from the current state would be
reconstructing a motive, and a page that says "withdrawn to resolve a conflict"
about a withdrawal that had nothing to do with one is worse than a page that
says nothing. Where a caller does not know, the record says so.

**Nothing here teaches anything.** Decision Memory learns from decisions that
*completed*; a withdrawn decision is the opposite of one, and making withdrawals
visible must not quietly turn them into evidence.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import PurePosixPath

from librairy.humanize import human_ago

#  One page. The same fifty every other list uses.
PAGE_SIZE = 50

#  What a person did, in the words the button they pressed used. Three, because
#  those are the three that exist — a fourth would have to be invented, and an
#  invented source is a claim about somebody's intent.
SENT_BACK = "sent-back"
CANCELLED = "cancelled"
SUPERSEDED = "superseded"

SOURCE_LABEL = {
    SENT_BACK: "Sent back to Review",
    CANCELLED: "Request cancelled",
    SUPERSEDED: "Replaced by a later decision",
}
#  What an old row says. Written before withdrawals recorded why, and left
#  alone: filling it in now would be guessing.
UNRECORDED = "Withdrawn"


@dataclass(frozen=True)
class Withdrawal:
    """One decision that was taken back, and what is known about why."""

    plan_id: str
    relpath: str
    dest_relpath: str = ""
    op_count: int = 0
    approved_at: str = ""
    withdrawn_at: str = ""
    source: str = ""
    reason: str = ""
    conflicted_with: str = ""
    conflict_summary: str = ""

    @property
    def name(self) -> str:
        return PurePosixPath(self.relpath).name or self.relpath

    @property
    def when(self) -> str:
        return human_ago(self.withdrawn_at) if self.withdrawn_at else ""

    @property
    def action(self) -> str:
        return SOURCE_LABEL.get(self.source, UNRECORDED)

    @property
    def files(self) -> str:
        count = max(1, self.op_count)
        return f"{count} file{'' if count == 1 else 's'}"


def record(
    conn: sqlite3.Connection,
    plan_id: str,
    *,
    source: str = "",
    reason: str = "",
    audit_finding_id: int | None = None,
    relpath: str = "",
    dest_relpath: str | None = None,
) -> None:
    """Keep the fact that this was approved, before the plan is removed.

    Called *before* the delete, while the hash, the approval time and the
    operations are still readable. One row describing one decision — not a
    journal, not something Undo can reach, and never a claim that files moved.

    The conflict is looked up here rather than remembered by the caller,
    because here is the last moment it is still true. Once the plan is gone the
    collision goes with it, and any later attempt to say what this resolved
    would be reconstruction.
    """
    from librairy.planner import utc_now

    plan = conn.execute(
        "SELECT plan_hash, approved_at FROM plans WHERE id=?", (plan_id,)
    ).fetchone()
    ops = conn.execute(
        "SELECT src_relpath, dest_relpath FROM plan_ops WHERE plan_id=? ORDER BY seq",
        (plan_id,),
    ).fetchall()
    conflicted, summary = _conflict(conn, plan_id)
    _release_decisions(conn, plan_id)
    conn.execute(
        "INSERT INTO plan_withdrawals(plan_id, plan_hash, audit_finding_id, relpath,"
        " dest_relpath, op_count, approved_at, withdrawn_at, source, reason,"
        " conflicted_with) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            plan_id,
            plan["plan_hash"] if plan else None,
            audit_finding_id,
            relpath or (str(ops[0]["src_relpath"]) if ops else ""),
            dest_relpath
            if dest_relpath is not None
            else (str(ops[-1]["dest_relpath"]) if ops else None),
            len(ops),
            plan["approved_at"] if plan else None,
            utc_now(),
            source,
            reason or summary,
            conflicted,
        ),
    )


def _release_decisions(conn: sqlite3.Connection, plan_id: str) -> None:
    """Unhook decision events from the plan that is about to be deleted.

    `decision_events.plan_id` is a foreign key, so deleting a withdrawn plan
    while an event still points at it raises `FOREIGN KEY constraint failed` —
    which is exactly what happened when somebody withdrew a comparison
    decision, because the comparison records its representation choice against
    the plan. The whole withdrawal failed on a table that has nothing to do
    with withdrawing.

    The event is kept and the link is dropped. The person really did make that
    choice, and the record of it is theirs; what is no longer true is that a
    plan is going to carry it out. It teaches nothing either way — Decision
    Memory counts only events with a `settled_at`, and a withdrawn decision
    never gets one.
    """
    conn.execute(
        "UPDATE decision_events SET plan_id=NULL"
        " WHERE plan_id=? AND settled_at IS NULL",
        (plan_id,),
    )


def _conflict(conn: sqlite3.Connection, plan_id: str) -> tuple[str | None, str]:
    """The waiting decision this one collided with, while that is still true.

    The id is kept only when the other party is a plan, because that is the
    only kind of decision this column can name — an approved arrival is a
    `proposals` row and will have become something else by the time anybody
    reads this. The sentence is kept either way, because the sentence is the
    part somebody actually needs.
    """
    from librairy.plan_conflicts import PLAN, check

    for conflict in check(conn, plan_id):
        for party in conflict.without(PLAN, plan_id):
            if not party.summary:
                continue
            reference = party.ref if party.kind == PLAN else None
            return reference, f"it conflicted with {party.summary}"
    return None, ""


def total(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM plan_withdrawals").fetchone()[0])


def listing(
    conn: sqlite3.Connection, *, limit: int = PAGE_SIZE, offset: int = 0
) -> list[Withdrawal]:
    """One bounded page, newest first.

    Bounded like every other list in the program. A decision that was taken
    back is worth being able to find; it is not worth loading a hundred
    thousand of them to draw a page.
    """
    rows = conn.execute(
        "SELECT * FROM plan_withdrawals ORDER BY withdrawn_at DESC, id DESC"
        " LIMIT ? OFFSET ?",
        (limit, max(0, offset)),
    ).fetchall()
    if not rows:
        return []
    #  The conflicting decisions still described, where they still exist. A
    #  plan that has since been committed or withdrawn itself has no summary,
    #  and the record says the id rather than inventing prose about it.
    from librairy.planner import summarise

    named = summarise(
        conn, [str(row["conflicted_with"]) for row in rows if row["conflicted_with"]]
    )
    return [
        Withdrawal(
            plan_id=str(row["plan_id"]),
            relpath=str(row["relpath"] or ""),
            dest_relpath=str(row["dest_relpath"] or ""),
            op_count=int(row["op_count"] or 0),
            approved_at=str(row["approved_at"] or ""),
            withdrawn_at=str(row["withdrawn_at"] or ""),
            source=str(_column(row, "source")),
            reason=str(_column(row, "reason")),
            conflicted_with=str(_column(row, "conflicted_with")),
            conflict_summary=named.get(str(_column(row, "conflicted_with")), ("", ""))[1],
        )
        for row in rows
    ]


def _column(row: sqlite3.Row, name: str) -> str:
    """A column an older database may not have. Absent reads as unrecorded."""
    try:
        return row[name] or ""
    except (IndexError, KeyError):
        return ""
