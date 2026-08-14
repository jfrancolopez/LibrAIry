"""What is waiting to be committed, by what it will actually do.

The Commit page could tell you *which files* were waiting. It could not tell
you what kind of change each one was without reading a source path and a
destination path and working it out — which is fine for three and useless for
three hundred, and actively misleading for the one that had just arrived from
Quarantine, because "a file moving from one folder to another" describes a
library correction and a delete-queue move equally well.

So every pending decision now declares its type, and the type comes from what
the plan actually is rather than from where the row happened to be rendered:

    new-file      an approved inbox proposal        inbox     -> library
    correction    an approved audit finding         library   -> library
    restore       a quarantine restore request      quarantine-> wherever it came from
    delete-queue  a quarantine delete request       quarantine-> quarantine/_to-delete

Two numbers matter and they are not the same number. A *decision* is one thing
the owner chose; an *operation* is one file being moved. One correction to a
twelve-track album is one decision and twelve operations. The headline counts
decisions, because that is what was agreed to, and the operation count is
available beside it — reporting twelve where somebody made one choice makes a
tidy afternoon look like a catastrophe.

Everything here is bounded. Counts come from SQL over the whole queue; rows
come from one LIMIT-ed query per page. Neither grows with the size of the
database, which is the entire scalability story for this page.
"""

from __future__ import annotations

import sqlite3
from pathlib import PurePosixPath
from typing import Any

from librairy.config import Settings
from librairy.humanize import human_bytes

NEW_FILE = "new-file"
CORRECTION = "correction"
RESTORE = "restore"
DELETE_QUEUE = "delete-queue"

# The order the page reads in: what arrives, what is being tidied, what is
# going back, what you are finished with.
TYPE_ORDER = (NEW_FILE, CORRECTION, RESTORE, DELETE_QUEUE)

# The heading for a group of them.
TYPE_LABEL = {
    NEW_FILE: "New files",
    CORRECTION: "Library corrections",
    RESTORE: "Restores",
    DELETE_QUEUE: "Delete queue",
}

# The badge on one row. Says what Commit will do to this file, in one word,
# as text — never colour alone, which is invisible to a colourblind reader and
# to anyone printing the page.
TYPE_BADGE = {
    NEW_FILE: "FILE",
    CORRECTION: "MOVE",
    RESTORE: "RESTORE",
    DELETE_QUEUE: "DELETE QUEUE",
}

# What the group means, under its heading. The delete-queue sentence is the
# most important one on the page.
TYPE_NOTE = {
    NEW_FILE: "New files from your inbox, filed into the library.",
    CORRECTION: "Files already in your library, moved or renamed.",
    RESTORE: "Held files going back where they came from.",
    DELETE_QUEUE: "Moved into one folder for you to empty yourself. "
    "LibrAIry never deletes anything.",
}

# One page of rows, whatever is waiting.
PAGE_SIZE = 50


def queue_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    """Decisions, operations and bytes per type — counted in SQL.

    Four aggregate queries over indexed columns, and no row objects built in
    Python. This is what stays honest when the queue is large: `SELECT
    COUNT(*)` does not care how many rows it counted.
    """
    inbox = conn.execute(
        """
        SELECT COUNT(*) AS decisions, COALESCE(SUM(i.size), 0) AS bytes
        FROM proposals p JOIN items i ON i.id = p.item_id
        WHERE p.status='approved' AND p.dest_relpath IS NOT NULL
          AND i.missing_since IS NULL
        """
    ).fetchone()
    plans = conn.execute(
        """
        SELECT
          CASE
            WHEN p.audit_finding_id IS NOT NULL THEN 'correction'
            WHEN o.dest_root='quarantine' AND o.dest_relpath LIKE '_to-delete/%'
              THEN 'delete-queue'
            ELSE 'restore'
          END AS kind,
          COUNT(DISTINCT p.id) AS decisions,
          COUNT(o.id) AS operations,
          COALESCE(SUM(i.size), 0) AS bytes
        FROM plans p
        JOIN plan_ops o ON o.plan_id = p.id
        LEFT JOIN items i ON i.id = o.item_id
        WHERE p.status IN ('approved','executing')
          AND (p.audit_finding_id IS NOT NULL OR p.quarantine_entry_id IS NOT NULL)
        GROUP BY kind
        """
    ).fetchall()

    types: dict[str, dict[str, int]] = {
        NEW_FILE: {
            "decisions": int(inbox["decisions"]),
            # One inbox proposal is one file: decision and operation coincide.
            "operations": int(inbox["decisions"]),
            "bytes": int(inbox["bytes"]),
        }
    }
    for row in plans:
        types[row["kind"]] = {
            "decisions": int(row["decisions"]),
            "operations": int(row["operations"]),
            "bytes": int(row["bytes"]),
        }
    groups = [
        {
            "type": key,
            "label": TYPE_LABEL[key],
            "note": TYPE_NOTE[key],
            "badge": TYPE_BADGE[key],
            **types.get(key, {"decisions": 0, "operations": 0, "bytes": 0}),
        }
        for key in TYPE_ORDER
    ]
    total_decisions = sum(group["decisions"] for group in groups)
    total_bytes = sum(group["bytes"] for group in groups)
    return {
        "groups": [group for group in groups if group["decisions"]],
        "all_groups": groups,
        "decisions": total_decisions,
        "operations": sum(group["operations"] for group in groups),
        "bytes": total_bytes,
        "size": human_bytes(total_bytes),
    }


def queue_rows(
    conn: sqlite3.Connection,
    settings: Settings | None,
    *,
    kind: str,
    page: int = 1,
    page_size: int = PAGE_SIZE,
) -> list[dict[str, Any]]:
    """One bounded page of pending decisions of one type.

    Every row carries the same five things — what it is, what it is called,
    where it is now, where it will be, and how big it is — because one shape
    renders in one component, and four bespoke row layouts are four places for
    the vocabulary to drift.
    """
    offset = max(0, (max(1, page) - 1) * page_size)
    if kind == NEW_FILE:
        return _inbox_rows(conn, page_size, offset)
    return _plan_rows(conn, settings, kind, page_size, offset)


def _inbox_rows(conn: sqlite3.Connection, limit: int, offset: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT p.id, p.dest_root, p.dest_relpath, i.relpath AS src_relpath, i.size
        FROM proposals p JOIN items i ON i.id = p.item_id
        WHERE p.status='approved' AND p.dest_relpath IS NOT NULL
          AND i.missing_since IS NULL
        ORDER BY p.id
        LIMIT ? OFFSET ?
        """,
        (limit, offset),
    ).fetchall()
    return [
        {
            "type": NEW_FILE,
            "badge": TYPE_BADGE[NEW_FILE],
            "subject": PurePosixPath(row["src_relpath"]).name,
            "current": f"inbox/{row['src_relpath']}",
            "after": f"{row['dest_root']}/{row['dest_relpath']}",
            "size": human_bytes(row["size"]),
            "reason": "You approved this in Review.",
            "op_count": 1,
            "plan_id": "",
            "back_url": "/commit/unapprove",
        }
        for row in rows
    ]


def _plan_rows(
    conn: sqlite3.Connection,
    settings: Settings | None,
    kind: str,
    limit: int,
    offset: int,
) -> list[dict[str, Any]]:
    where = {
        CORRECTION: "p.audit_finding_id IS NOT NULL",
        DELETE_QUEUE: (
            "p.quarantine_entry_id IS NOT NULL AND o.dest_root='quarantine'"
            " AND o.dest_relpath LIKE '_to-delete/%'"
        ),
        RESTORE: (
            "p.quarantine_entry_id IS NOT NULL AND NOT (o.dest_root='quarantine'"
            " AND o.dest_relpath LIKE '_to-delete/%')"
        ),
    }[kind]
    rows = conn.execute(
        f"""
        SELECT p.id AS plan_id, p.status, p.approved_at,
               p.audit_finding_id, p.quarantine_entry_id,
               o.src_root, o.src_relpath, o.dest_root, o.dest_relpath,
               (SELECT COUNT(*) FROM plan_ops WHERE plan_id=p.id) AS op_count,
               (SELECT COALESCE(SUM(i2.size), 0) FROM plan_ops o2
                  LEFT JOIN items i2 ON i2.id = o2.item_id
                 WHERE o2.plan_id = p.id) AS bytes
        FROM plans p
        JOIN plan_ops o ON o.id = (
          SELECT id FROM plan_ops WHERE plan_id = p.id ORDER BY seq LIMIT 1
        )
        WHERE p.status IN ('approved','executing') AND {where}
        ORDER BY p.approved_at, p.id
        LIMIT ? OFFSET ?
        """,  # noqa: S608 — `where` comes from this function's own dict
        (limit, offset),
    ).fetchall()
    return [_plan_row(conn, settings, row, kind) for row in rows]


def _plan_row(
    conn: sqlite3.Connection,
    settings: Settings | None,
    row: sqlite3.Row,
    kind: str,
) -> dict[str, Any]:
    subject = PurePosixPath(row["src_relpath"]).name or row["src_relpath"]
    reason = _reason(conn, row, kind)
    stale = ""
    if settings is not None and kind == CORRECTION:
        from librairy.correction_state import plan_drift

        stale = plan_drift(conn, settings, row["plan_id"])
    return {
        "type": kind,
        "badge": TYPE_BADGE[kind],
        "subject": subject,
        "current": f"{row['src_root']}/{row['src_relpath']}",
        "after": f"{row['dest_root']}/{row['dest_relpath']}",
        "size": human_bytes(row["bytes"]),
        "reason": reason,
        "op_count": int(row["op_count"]),
        "plan_id": row["plan_id"],
        "applying": row["status"] == "executing",
        "stale": bool(stale),
        "finding_id": row["audit_finding_id"],
        "entry_id": row["quarantine_entry_id"],
        # Where "send this back" goes for this kind of decision. One row
        # component, one attribute, rather than a chain of ifs in the template.
        "back_url": (
            f"/review/audit/{row['audit_finding_id']}/unapprove"
            if kind == CORRECTION
            else f"/quarantine/cancel/{row['quarantine_entry_id']}"
        ),
        # A stale approval is not sent back to be reconsidered — it can no
        # longer run at all, so the honest offer is to remove it.
        "back_label": (
            "Remove old approval"
            if stale
            else ("Send back to Review" if kind == CORRECTION else "Cancel request")
        ),
        # Every file this decision touches, on demand. A correction to an album
        # is one decision and twelve moves, and "twelve files" is a number
        # until you can see which twelve.
        "files": _files(conn, row["plan_id"]) if int(row["op_count"]) > 1 else [],
    }


def _files(conn: sqlite3.Connection, plan_id: str) -> list[dict[str, str]]:
    return [
        {
            "role": op["role"],
            "src": op["src_relpath"],
            "dest": op["dest_relpath"],
        }
        for op in conn.execute(
            "SELECT role, src_relpath, dest_relpath FROM plan_ops"
            " WHERE plan_id=? ORDER BY seq",
            (plan_id,),
        )
    ]


def _reason(conn: sqlite3.Connection, row: sqlite3.Row, kind: str) -> str:
    """Why this is waiting, in the words of the decision that made it."""
    if kind == CORRECTION and row["audit_finding_id"]:
        found = conn.execute(
            "SELECT summary FROM audit_findings WHERE id=?", (row["audit_finding_id"],)
        ).fetchone()
        return found["summary"] if found else "Approved in Library Review."
    if kind == DELETE_QUEUE:
        return "You chose Delete queue. Nothing is deleted."
    return "You asked for this to go back."
