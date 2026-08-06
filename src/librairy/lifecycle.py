from __future__ import annotations

import sqlite3

from librairy.planner import utc_now

ITEM_STATES = {
    "discovered",
    "unstable",
    "proposed",
    "approved",
    "committed",
    "quarantine-proposed",
    "quarantined",
    "postponed",
    "pending",
}

LEGAL_TRANSITIONS = {
    "discovered": {
        "unstable",
        "proposed",
        "pending",
        "quarantine-proposed",
        "committed",
        "quarantined",
    },
    "unstable": {"discovered"},
    "proposed": {"approved", "pending", "postponed", "discovered", "committed"},
    "approved": {"committed", "quarantined", "discovered"},
    "pending": {"discovered", "postponed", "proposed"},
    "postponed": {"discovered", "proposed"},
    "quarantine-proposed": {"approved", "quarantined", "discovered", "proposed"},
    "quarantined": {"discovered"},
    "committed": {"discovered"},
}

RESET_ON_FINGERPRINT_CHANGE = {
    "proposed",
    "approved",
    "quarantine-proposed",
    "postponed",
    "pending",
}


class LifecycleError(RuntimeError):
    pass


def assert_transition(current: str, target: str) -> None:
    if current == target:
        return
    if current not in ITEM_STATES:
        raise LifecycleError(f"unknown item state: {current}")
    if target not in ITEM_STATES:
        raise LifecycleError(f"unknown item state: {target}")
    if target not in LEGAL_TRANSITIONS[current]:
        raise LifecycleError(f"illegal item transition: {current} -> {target}")


def transition_item(conn: sqlite3.Connection, item_id: int, target: str) -> None:
    row = conn.execute("SELECT state FROM items WHERE id=?", (item_id,)).fetchone()
    if row is None:
        raise LifecycleError(f"item not found: {item_id}")
    assert_transition(row["state"], target)
    conn.execute(
        "UPDATE items SET state=?, last_seen_at=? WHERE id=?",
        (target, utc_now(), item_id),
    )


def state_counts(conn: sqlite3.Connection, root: str | None = None) -> dict[str, int]:
    """Item counts per state, optionally for one root only.

    Worth asking for a root: a committed library file sits in 'discovered'
    forever, because that is what an ordinary indexed library file is. Counted
    together with the inbox it reads as an enormous unidentified backlog — 140
    already-filed files on the author's machine, reported as work outstanding.
    """
    if root is None:
        rows = conn.execute("SELECT state, COUNT(*) AS count FROM items GROUP BY state")
    else:
        rows = conn.execute(
            "SELECT state, COUNT(*) AS count FROM items WHERE root=? GROUP BY state",
            (root,),
        )
    return {row["state"]: row["count"] for row in rows}


def should_reset_for_fingerprint_change(state: str) -> bool:
    return state in RESET_ON_FINGERPRINT_CHANGE
