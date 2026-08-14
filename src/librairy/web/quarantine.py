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

#  What the reason column holds, said the way a person would say it. The keys
#  are the three the schema's CHECK allows — `user_discard` was here instead of
#  `user`, which the column cannot hold, so every hand-quarantined file read
#  "no reason recorded".
REASONS = {
    "exact_duplicate": "byte-for-byte copy of a file you already have",
    "similar_media": "close enough to something you already have to be worth a look",
    "user": "you said you did not want it",
}
UNWANTED = "you sent it here from Review"

#  One word for the badge, where the sentence above is the explanation.
REASON_TAGS = {
    "exact_duplicate": "duplicate",
    "similar_media": "similar",
    "user": "you sent it here",
}


# One page of rows, whatever the database holds. A quarantine with ten
# thousand files in it is a real thing — a deduplication run over a photo
# library produces one — and rendering all of them was never a decision
# anybody took, it was just what happened when nobody wrote a LIMIT.
PAGE_SIZE = 50

# What the user can be looking at. Every one maps to a real state; there is no
# tab here that is a filter over nothing.
VIEWS = {
    "held": "Held",
    "waiting": "Waiting for Commit",
    "delete-queue": "Delete queue",
    "restored": "Put back",
}
DEFAULT_VIEW = "held"


def quarantine_data(
    conn: sqlite3.Connection,
    settings: Settings | None = None,
    *,
    view: str = "",
    page: int = 1,
) -> dict[str, object]:
    """One bounded page of quarantine, plus counts for the whole of it.

    The counts come from SQL over the whole table; the rows come from one
    `LIMIT`-ed query. That split is the entire scalability story for this page:
    the summary stays true at a million rows and the DOM stays the same size.
    """
    view = view if view in VIEWS else DEFAULT_VIEW
    page = max(1, page)
    counts = _counts(conn)
    total = counts.get(view, 0)
    rows = _entries(conn, view=view, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE)
    host_dir = str(settings.host_quarantine_dir) if settings else ""
    return {
        "staged": _staged(conn),
        "entries": rows,
        "similar_flags": _similar_flags(conn),
        "view": view,
        "views": VIEWS,
        "counts": counts,
        "page": page,
        "page_size": PAGE_SIZE,
        "total": total,
        "page_count": max(1, -(-total // PAGE_SIZE)),
        "has_next": page * PAGE_SIZE < total,
        "has_prev": page > 1,
        "range_start": 0 if not total else (page - 1) * PAGE_SIZE + 1,
        "range_end": min(page * PAGE_SIZE, total),
        # The one thing the page could not answer: where the files actually
        # are, so you can go and delete them yourself. LibrAIry will not.
        "host_quarantine_dir": host_dir,
        "held": counts.get("held", 0),
        # The pile you asked for: one folder to point a file manager at, so
        # emptying it is one deliberate gesture rather than two hundred.
        "for_deletion": counts.get("delete-queue", 0),
        "delete_pile_dir": f"{host_dir.rstrip('/')}/{DELETE_PILE}" if host_dir else "",
    }


# A held file is in exactly one of these states, and the expressions below are
# the definition. Written once, in SQL, so the count in the tab and the rows
# under it cannot drift apart — two hand-written filters agreeing is luck.
_ACTIVE_PLAN = (
    "EXISTS (SELECT 1 FROM plans p WHERE p.quarantine_entry_id = qe.id"
    " AND p.status IN ('approved','executing'))"
)
_IN_DELETE_QUEUE = "i.relpath LIKE '_to-delete/%' ESCAPE '\\'"
_WHERE = {
    "held": f"qe.restored_at IS NULL AND NOT {_ACTIVE_PLAN} AND NOT ({_IN_DELETE_QUEUE})",
    "waiting": f"qe.restored_at IS NULL AND {_ACTIVE_PLAN}",
    "delete-queue": f"qe.restored_at IS NULL AND ({_IN_DELETE_QUEUE})",
    "restored": "qe.restored_at IS NOT NULL",
}


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    """How many are in each view, counted in SQL over the whole table.

    One query, four counts, no rows loaded into Python. `SELECT COUNT(*)` over
    an indexed table stays fast at sizes where building a list of entry dicts
    to call `len()` on does not.
    """
    parts = ", ".join(
        f"SUM(CASE WHEN {clause} THEN 1 ELSE 0 END) AS \"{name}\""
        for name, clause in _WHERE.items()
    )
    row = conn.execute(
        f"SELECT {parts} FROM quarantine_entries qe"  # noqa: S608 — module constants
        " LEFT JOIN items i ON i.id = qe.item_id"
    ).fetchone()
    return {name: int(row[name] or 0) for name in _WHERE}


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


def _entries(
    conn: sqlite3.Connection,
    *,
    view: str = DEFAULT_VIEW,
    limit: int = PAGE_SIZE,
    offset: int = 0,
) -> list[dict[str, object]]:
    """One page of held files, filtered and ordered in SQL.

    `ORDER BY qe.id DESC` with `LIMIT/OFFSET` over a primary key is a stable,
    total order, which is what makes paging deterministic: no row appears on
    two pages and none is skipped between them.

    Nothing here touches the filesystem. Every fact on the row — where it is,
    where it came from, how big it is, what is waiting on it — is already in
    the database, and a `stat()` per row is what turns a page into a network
    round trip per file on a NAS.
    """
    rows = list(
        conn.execute(
            f"""
            SELECT qe.*, i.relpath AS item_relpath, i.size AS item_size,
                   i.state AS item_state
            FROM quarantine_entries qe
            LEFT JOIN items i ON i.id = qe.item_id
            WHERE {_WHERE.get(view, _WHERE[DEFAULT_VIEW])}
            ORDER BY qe.id DESC
            LIMIT ? OFFSET ?
            """,  # noqa: S608 — clause comes from the module's own dict
            (limit, offset),
        )
    )
    # One query for the whole page rather than one per row: a request lookup
    # inside the loop is the N+1 that makes fifty rows fifty-one queries.
    from librairy.quarantine_requests import pending_requests

    requests = pending_requests(conn)
    return [
        {
            **dict(row),
            "reason_text": reason_text(row["reason"]),
            "reason_tag": REASON_TAGS.get(str(row["reason"] or ""), "set aside"),
            "marked": marked_for_deletion(row["item_relpath"]),
            "size_label": human_size(row["item_size"]),
            # The name is what identifies the row; the path is detail. Both
            # were in one mono blob that wrapped to four lines on a phone.
            "display_name": _basename(row["item_relpath"] or row["original_relpath"]),
            # What the user already decided, and what Commit will do about it.
            "request": requests.get(int(row["id"])),
            # Whether Restore can be offered at all. A control that can only
            # produce an error is worse than no control.
            "restorable": bool(row["original_root"] and row["original_relpath"]),
            "gone": row["item_relpath"] is None or row["item_state"] == "missing",
        }
        for row in rows
    ]


def _basename(relpath: object) -> str:
    return str(relpath or "").rstrip("/").rsplit("/", 1)[-1]


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
