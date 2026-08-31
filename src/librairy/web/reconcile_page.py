"""The page where LibrAIry and the disk are made to agree again.

One surface for two related questions, because they are answered from the same
evidence and a person meeting them separately would have to hold the connection
in their head:

* **does what I have recorded still describe what is on disk?** — after a
  restore, after a share came back, after somebody spent an afternoon in
  Finder. Read-only, always.
* **this file is somewhere else now: shall I agree?** — the only action here,
  and it moves no bytes.
"""

from __future__ import annotations

import sqlite3

from librairy.config import Settings


def page_data(
    conn: sqlite3.Connection, settings: Settings, *, error: str = "", done: str = ""
) -> dict[str, object]:
    """Everything the page draws. Reads only — opening it changes nothing."""
    from librairy import reconcile
    from librairy.restore_check import preserved, validate

    report = validate(conn, settings)
    trees = reconcile.subtrees(conn)
    #  Files already accounted for by a folder move are not offered twice. The
    #  folder is one decision; listing its members again underneath would be
    #  the same question asked thirty-one times.
    grouped = {member.item_id for tree in trees for member in tree.members}
    singles = [
        candidate
        for candidate in reconcile.candidates(conn)
        if candidate.item_id not in grouped
    ]
    return {
        "error": error,
        "done": done,
        "report": report,
        #  Said out loud on this page of all pages. Somebody who has just put a
        #  database back wants to know that what they decided came with it, and
        #  a rebuild of derived state must never be mistaken for losing that.
        "preserved": [
            {"label": label, "count": count}
            for label, count in preserved(conn)
            if count
        ],
        "subtrees": trees,
        "candidates": singles,
        "candidate_total": reconcile.total(conn),
        "ambiguous": reconcile.ambiguous(conn),
        "recognised": reconcile.recognised(conn, limit=10),
    }
