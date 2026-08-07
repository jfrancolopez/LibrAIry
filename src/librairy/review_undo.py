"""Take back a review decision.

Every button in Review used to be a one-way door. "Not this" was the worst of
them: it sets the proposal to rejected and the file to 'pending', which drops
it out of the queue entirely, and the only way back was `librairy analyze
--reanalyze` on the command line. Nothing in the portal said so, and nothing in
the portal could undo it.

This is deliberately *not* the same thing as History's undo. That one reverses
a commit — files have moved on disk and moving them back is a filesystem
operation with a journal behind it. This one reverses a decision made before
anything moved: it restores the rows the decision changed and nothing else.

A decision that has since been committed is never undone here. The files are
on disk by then, and pretending otherwise by flipping a status back would
describe a library that does not exist.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass

from librairy.lifecycle import transition_item
from librairy.planner import utc_now

#  Enough to fix a mistake you have just noticed, not an audit log. History is
#  the audit log, and it starts where this stops.
KEEP_BATCHES = 20

ACTION_LABELS = {
    "approve": "Approved",
    "reject": "Set aside",
    "postpone": "Put off",
    "discard": "Sent to quarantine",
    "mark_delete": "Marked for deletion",
    "reanalyze": "Sent back for another look",
}


@dataclass(frozen=True)
class ProposalSnapshot:
    """The proposal columns a review decision can change, before it changed."""

    proposal_id: int
    item_id: int
    status: str
    action: str
    dest_root: str
    dest_relpath: str | None
    item_state: str


@dataclass(frozen=True)
class UndoEntry:
    id: int
    action: str
    summary: str
    created_at: str


def snapshot_proposals(conn: sqlite3.Connection, proposal_ids: list[int]) -> list[ProposalSnapshot]:
    if not proposal_ids:
        return []
    placeholders = ",".join("?" for _ in proposal_ids)
    rows = conn.execute(
        f"""
        SELECT p.id, p.item_id, p.status, p.action, p.dest_root, p.dest_relpath,
               i.state AS item_state
        FROM proposals p JOIN items i ON i.id = p.item_id
        WHERE p.id IN ({placeholders})
        """,  # noqa: S608 - placeholders are generated from the id count
        proposal_ids,
    ).fetchall()
    return [
        ProposalSnapshot(
            proposal_id=int(row["id"]),
            item_id=int(row["item_id"]),
            status=row["status"],
            action=row["action"],
            dest_root=row["dest_root"],
            dest_relpath=row["dest_relpath"],
            item_state=row["item_state"],
        )
        for row in rows
    ]


def record(
    conn: sqlite3.Connection, action: str, snapshots: list[ProposalSnapshot]
) -> int | None:
    """Remember what these rows looked like before the decision landed."""
    if not snapshots:
        return None
    cursor = conn.execute(
        "INSERT INTO review_undo(action, summary, snapshot, created_at) VALUES (?, ?, ?, ?)",
        (
            action,
            _summary(action, snapshots),
            json.dumps([asdict(snapshot) for snapshot in snapshots]),
            utc_now(),
        ),
    )
    _trim(conn)
    return int(cursor.lastrowid)


def latest(conn: sqlite3.Connection) -> UndoEntry | None:
    row = conn.execute(
        "SELECT id, action, summary, created_at FROM review_undo ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    return UndoEntry(int(row["id"]), row["action"], row["summary"], row["created_at"])


@dataclass(frozen=True)
class UndoResult:
    restored: int
    skipped_committed: int

    @property
    def message(self) -> str:
        if self.restored and self.skipped_committed:
            return (
                f"Put {self.restored} back in the queue. "
                f"{self.skipped_committed} had already been committed and were left alone — "
                f"undo those from History, which moves the files back."
            )
        if self.restored:
            return f"Put {self.restored} back in the queue."
        if self.skipped_committed:
            return (
                "Already committed, so there was nothing to take back here. "
                "History can undo the move itself."
            )
        return "Nothing to undo."


def undo_last(conn: sqlite3.Connection) -> UndoResult:
    row = conn.execute(
        "SELECT id, snapshot FROM review_undo ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return UndoResult(0, 0)
    restored = 0
    skipped = 0
    for raw in json.loads(row["snapshot"]):
        snapshot = ProposalSnapshot(**raw)
        current = conn.execute(
            "SELECT status FROM proposals WHERE id=?", (snapshot.proposal_id,)
        ).fetchone()
        if current is None:
            continue
        # The files have moved. Flipping a status back here would describe a
        # library that does not exist; History undoes the move itself.
        if current["status"] == "committed":
            skipped += 1
            continue
        conn.execute(
            """
            UPDATE proposals
            SET status=?, action=?, dest_root=?, dest_relpath=?, updated_at=?
            WHERE id=?
            """,
            (
                snapshot.status,
                snapshot.action,
                snapshot.dest_root,
                snapshot.dest_relpath,
                utc_now(),
                snapshot.proposal_id,
            ),
        )
        _restore_item_state(conn, snapshot)
        restored += 1
    conn.execute("DELETE FROM review_undo WHERE id=?", (row["id"],))
    return UndoResult(restored, skipped)


def _restore_item_state(conn: sqlite3.Connection, snapshot: ProposalSnapshot) -> None:
    """Back to the state the item was in, going the long way if it has to.

    The lifecycle only allows the transitions that mean something, and the
    reverse of a decision is not always one of them — 'approved' does not go
    directly back to 'proposed'. Every state can reach 'discovered', so that is
    the way round.
    """
    current = conn.execute(
        "SELECT state FROM items WHERE id=?", (snapshot.item_id,)
    ).fetchone()
    if current is None or current["state"] == snapshot.item_state:
        return
    from librairy.lifecycle import LEGAL_TRANSITIONS

    if snapshot.item_state in LEGAL_TRANSITIONS.get(current["state"], set()):
        transition_item(conn, snapshot.item_id, snapshot.item_state)
        return
    transition_item(conn, snapshot.item_id, "discovered")
    if snapshot.item_state != "discovered":
        transition_item(conn, snapshot.item_id, snapshot.item_state)


def _summary(action: str, snapshots: list[ProposalSnapshot]) -> str:
    label = ACTION_LABELS.get(action, action)
    count = len(snapshots)
    return f"{label} {count} file{'s' if count != 1 else ''}"


def _trim(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        DELETE FROM review_undo
        WHERE id NOT IN (SELECT id FROM review_undo ORDER BY id DESC LIMIT ?)
        """,
        (KEEP_BATCHES,),
    )


__all__ = [
    "ACTION_LABELS",
    "ProposalSnapshot",
    "UndoEntry",
    "UndoResult",
    "latest",
    "record",
    "snapshot_proposals",
    "undo_last",
]
