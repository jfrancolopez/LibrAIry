from __future__ import annotations

import sqlite3
from dataclasses import asdict

from librairy.config import Settings
from librairy.lifecycle import transition_item
from librairy.planner import utc_now
from librairy.quarantine import (
    DELETE_PILE,
    mark_entry_for_deletion,
    marked_for_deletion,
    restore_entry,
)
from librairy.web.evidence import humanize_evidence

#  What the reason column holds, said the way a person would say it.
REASONS = {
    "exact_duplicate": "byte-for-byte copy of a file you already have",
    "user_discard": "you said you did not want it",
}
UNWANTED = "you sent it here from Review"


def quarantine_data(
    conn: sqlite3.Connection, settings: Settings | None = None
) -> dict[str, object]:
    entries = _entries(conn)
    host_dir = str(settings.host_quarantine_dir) if settings else ""
    live = [entry for entry in entries if not entry["restored_at"]]
    return {
        "staged": _staged(conn),
        "entries": entries,
        "similar_flags": _similar_flags(conn),
        # The one thing the page could not answer: where the files actually
        # are, so you can go and delete them yourself. LibrAIry will not.
        "host_quarantine_dir": host_dir,
        "held": len(live),
        # The pile you asked for: one folder to point a file manager at, so
        # emptying it is one deliberate gesture rather than two hundred.
        "for_deletion": sum(1 for entry in live if entry["marked"]),
        "delete_pile_dir": f"{host_dir.rstrip('/')}/{DELETE_PILE}" if host_dir else "",
    }


def reason_text(reason: str | None) -> str:
    return REASONS.get(str(reason or ""), str(reason or "no reason recorded"))


def staged_reason(evidence: str | None) -> str:
    """Why a file is queued for quarantine, from the evidence already on it.

    Only two things put a file here: the duplicate finder, which writes
    "exact duplicate of ..." into its evidence, and Quarantine in Review,
    which leaves the original evidence untouched. Reading it back beats
    another column that could disagree with the evidence beside it.
    """
    if "exact duplicate of" in (evidence or ""):
        return REASONS["exact_duplicate"]
    return UNWANTED


def human_size(size: int | None) -> str:
    if not size or size < 0:
        return ""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return ""


def restore_quarantine(
    conn: sqlite3.Connection, settings: Settings, entry_id: int
) -> dict[str, object]:
    return asdict(restore_entry(conn, entry_id, settings))


def mark_for_deletion(
    conn: sqlite3.Connection, settings: Settings, entry_id: int
) -> dict[str, object]:
    return asdict(mark_entry_for_deletion(conn, entry_id, settings))


def unstage_proposal(conn: sqlite3.Connection, proposal_id: int) -> None:
    row = conn.execute("SELECT item_id FROM proposals WHERE id=?", (proposal_id,)).fetchone()
    if row is None:
        raise ValueError("proposal not found")
    transition_item(conn, row["item_id"], "proposed")
    conn.execute(
        """
        UPDATE proposals
        SET action='move', dest_root='library', status='proposed', updated_at=?
        WHERE id=?
        """,
        (utc_now(), proposal_id),
    )


def stage_for_deletion(conn: sqlite3.Connection, proposal_id: int) -> None:
    """Approve a staged quarantine, aimed at the delete pile instead.

    Without this, being finished with a duplicate that has not moved yet costs
    two commits: one to put it in quarantine and another to move it along.
    """
    from librairy.web.review import discard_proposals

    if not discard_proposals(conn, [proposal_id], to_delete_pile=True):
        raise ValueError("proposal not found")


def approve_stage(conn: sqlite3.Connection, proposal_id: int) -> None:
    row = conn.execute("SELECT item_id FROM proposals WHERE id=?", (proposal_id,)).fetchone()
    if row is None:
        raise ValueError("proposal not found")
    transition_item(conn, row["item_id"], "approved")
    conn.execute(
        "UPDATE proposals SET status='approved', updated_at=? WHERE id=?",
        (utc_now(), proposal_id),
    )


def _staged(conn: sqlite3.Connection) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT p.*, i.relpath AS item_relpath, i.size AS item_size
        FROM proposals p
        JOIN items i ON i.id = p.item_id
        WHERE p.action='quarantine' AND p.status IN ('proposed', 'approved')
        ORDER BY p.id DESC
        """
    ).fetchall()
    return [
        {
            **dict(row),
            "evidence_views": humanize_evidence(row["evidence"] or ""),
            "reason_text": staged_reason(row["evidence"]),
            "size_label": human_size(row["item_size"]),
        }
        for row in rows
    ]


def _entries(conn: sqlite3.Connection) -> list[dict[str, object]]:
    rows = list(
        conn.execute(
            """
            SELECT qe.*, i.relpath AS item_relpath, i.size AS item_size, i.state AS item_state
            FROM quarantine_entries qe
            LEFT JOIN items i ON i.id = qe.item_id
            ORDER BY qe.id DESC
            """
        )
    )
    return [
        {
            **dict(row),
            "reason_text": reason_text(row["reason"]),
            "marked": marked_for_deletion(row["item_relpath"]),
            "size_label": human_size(row["item_size"]),
        }
        for row in rows
    ]


def _similar_flags(conn: sqlite3.Connection) -> list[dict[str, object]]:
    rows = list(
        conn.execute(
            """
            SELECT f.*, a.relpath AS item_relpath, b.relpath AS similar_relpath,
                   a.size AS item_size, b.size AS similar_size
            FROM similar_media_flags f
            JOIN items a ON a.id = f.item_id
            JOIN items b ON b.id = f.similar_item_id
            WHERE f.status='review'
            ORDER BY f.id DESC
            """
        )
    )
    return [
        {
            **dict(row),
            "item_size_label": human_size(row["item_size"]),
            "similar_size_label": human_size(row["similar_size"]),
        }
        for row in rows
    ]
