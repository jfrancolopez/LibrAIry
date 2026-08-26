"""The delete queue, as a place rather than a folder path.

Everything on this page already existed in the database; none of it had
anywhere to be looked at. The view adds no power — there is no *Empty queue*,
nothing expires, and LibrAIry still never deletes — it adds the ability to see
what you have decided before you act on it yourself.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from librairy.delete_queue import (
    PAGE_SIZE,
    STATE_NOTE,
    decisions,
    entries,
    summary,
)


def page_data(
    conn: sqlite3.Connection, *, page: int = 1, error: str = ""
) -> dict[str, Any]:
    """One bounded page of the queue, with its totals and its groups."""
    page = max(1, page)
    counted = summary(conn)
    files = int(counted["files"])
    pages = max(1, -(-files // PAGE_SIZE))
    page = min(page, pages)
    offset = (page - 1) * PAGE_SIZE
    rows = entries(conn, limit=PAGE_SIZE, offset=offset)
    return {
        "error": error,
        "summary": counted,
        "decisions": [
            {
                "plan_id": found.plan_id,
                "total": found.total,
                "size_label": found.size_label,
                "when": found.ago,
                "names": found.names,
                "more": found.more,
            }
            for found in decisions(conn)
        ],
        "entries": [_row(entry) for entry in rows],
        "page": page,
        "page_count": pages,
        "has_prev": page > 1,
        "has_next": page < pages,
        "range_start": 0 if not files else offset + 1,
        "range_end": min(offset + PAGE_SIZE, files),
        "total": files,
    }


def _row(entry) -> dict[str, Any]:  # noqa: ANN001
    return {
        "entry_id": entry.entry_id,
        "item_id": entry.item_id,
        "name": entry.name,
        "relpath": entry.relpath,
        "came_from": f"{entry.original_root}/{entry.original_relpath}"
        if entry.original_relpath
        else "",
        "size_label": entry.size_label,
        "when": entry.when,
        "queued_at": entry.queued_at,
        "plan_id": entry.plan_id,
        "state": entry.state,
        #  Said in words rather than shown as a colour. A file somebody removed
        #  outside LibrAIry is not waiting for anything, and a file whose bytes
        #  have changed is not the file the decision was about.
        "state_note": STATE_NOTE.get(entry.state, ""),
        #  Current context about a historical decision. Worth knowing before a
        #  permanent removal; it does not cancel the decision that queued it.
        "protected_by": entry.protected_by,
        "related": entry.related,
        "restorable": entry.restorable,
        "waiting": entry.waiting,
    }
