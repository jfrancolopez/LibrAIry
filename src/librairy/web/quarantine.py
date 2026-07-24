from __future__ import annotations

import sqlite3
from dataclasses import asdict

from librairy.config import Settings
from librairy.lifecycle import transition_item
from librairy.planner import utc_now
from librairy.quarantine import restore_entry
from librairy.web.evidence import humanize_evidence


def quarantine_data(conn: sqlite3.Connection) -> dict[str, object]:
    return {
        "staged": _staged(conn),
        "entries": _entries(conn),
        "similar_flags": _similar_flags(conn),
    }


def restore_quarantine(
    conn: sqlite3.Connection, settings: Settings, entry_id: int
) -> dict[str, object]:
    return asdict(restore_entry(conn, entry_id, settings))


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
        {**dict(row), "evidence_views": humanize_evidence(row["evidence"] or "")} for row in rows
    ]


def _entries(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT qe.*, i.relpath AS item_relpath, i.size AS item_size, i.state AS item_state
            FROM quarantine_entries qe
            LEFT JOIN items i ON i.id = qe.item_id
            ORDER BY qe.id DESC
            """
        )
    )


def _similar_flags(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
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
