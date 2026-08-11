from __future__ import annotations

import sqlite3

from librairy.planner import utc_now
from librairy.search import sync_search_item

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
    # A duplicate found after a file was already classified supersedes the
    # guess. 'proposed' is the machine's opinion, not the owner's decision, so
    # revising it costs nobody anything -- and without this a duplicate that
    # arrived a cycle late could never be staged at all. Deciding states
    # ('pending' after a rejection, 'postponed', 'approved') are not here on
    # purpose: re-staging one would undo an answer the owner already gave.
    "proposed": {
        "approved",
        "pending",
        "postponed",
        "discovered",
        "committed",
        "quarantine-proposed",
    },
    "approved": {"committed", "quarantined", "discovered"},
    "pending": {"discovered", "postponed", "proposed", "quarantine-proposed"},
    "postponed": {"discovered", "proposed"},
    # Review offers the same four buttons on a duplicate row as on any other,
    # and two of them had nowhere legal to go: "Not this" (-> pending) and
    # "Later" (-> postponed) both raised LifecycleError, so saying "no, keep
    # it" to a duplicate was a 500 rather than an answer.
    "quarantine-proposed": {
        "approved",
        "quarantined",
        "discovered",
        "proposed",
        "pending",
        "postponed",
    },
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


# Only a proposal still waiting on the owner is worth clearing. A rejected or
# already-superseded one is resolved: its file being gone changes nothing about
# it, and it is not in anybody's queue. So the number of *records* whose file
# is missing and the number of *entries worth clearing* are two different
# numbers, and the UI has to be careful not to imply they are one.
VANISHED_STATUSES = ("proposed", "approved", "postponed")
_VANISHED_WHERE = (
    f"p.status IN ({', '.join('?' * len(VANISHED_STATUSES))}) AND i.missing_since IS NOT NULL"
)


def _vanished_filter(root: str | None) -> tuple[str, list[object]]:
    """The one predicate `vanished_count`, the listing and the clear all share.

    Scoped by root because clearing them is: the inbox and the library go
    missing for different reasons — an unmounted share versus a file you tidied
    away yourself — and a button offered next to a count of one must not also
    resolve entries belonging to the other.
    """
    where, params = _VANISHED_WHERE, list(VANISHED_STATUSES)
    if root is not None:
        where += " AND i.root=?"
        params.append(root)
    return where, params


def vanished_count(conn: sqlite3.Connection, root: str | None = None) -> int:
    """Proposals whose file is no longer where the scanner last saw it.

    Not an error and not necessarily a problem: an unmounted disk looks
    exactly like this, and everything comes back on the next scan. It is only
    worth a number on screen so the counts add up -- these are filtered out of
    Review and Commit, and without saying so the totals would just be wrong.
    """
    where, params = _vanished_filter(root)
    return int(
        conn.execute(
            f"SELECT COUNT(*) FROM proposals p JOIN items i ON i.id = p.item_id "  # noqa: S608
            f"WHERE {where}",
            params,
        ).fetchone()[0]
    )


def resolved_missing_count(conn: sqlite3.Connection, root: str | None = None) -> int:
    """Records whose file is gone but which have nothing left to clear.

    A rejected proposal, a superseded one, or none at all: the file vanishing
    changes nothing about any of them, because none is waiting on the owner.

    This exists so the two numbers can be shown together. Eight records here
    are missing and seven are clearable, and a reader who is only ever told one
    of those has no way to reconcile them with the other.
    """
    where = "i.missing_since IS NOT NULL AND (p.status IS NULL OR p.status NOT IN (?, ?, ?))"
    params: list[object] = list(VANISHED_STATUSES)
    if root is not None:
        where += " AND i.root=?"
        params.append(root)
    return int(
        conn.execute(
            f"""
            SELECT COUNT(*) FROM items i
            LEFT JOIN proposals p ON p.item_id = i.id
            WHERE {where}
            """,  # noqa: S608 - placeholders only
            params,
        ).fetchone()[0]
    )


def vanished_entries(conn: sqlite3.Connection, root: str | None = None) -> list[sqlite3.Row]:
    """The same entries, listed, so the owner can look before deciding.

    Clearing seven rows you cannot see is a worse offer than clearing seven you
    can. Everything here is already in the two tables the count joins; no host
    path is selected, because none of it is anybody's business outside the box.
    """
    where, params = _vanished_filter(root)
    return list(
        conn.execute(
            f"""
            SELECT i.id AS item_id, i.root, i.relpath, i.missing_since, i.state, i.size,
                   p.id AS proposal_id, p.status, p.category, p.confidence,
                   p.dest_root, p.dest_relpath, p.evidence
            FROM proposals p JOIN items i ON i.id = p.item_id
            WHERE {where}
            ORDER BY i.root, i.relpath
            """,  # noqa: S608 - placeholders only
            params,
        )
    )


def forget_vanished(conn: sqlite3.Connection, root: str | None = None) -> int:
    """Resolve the proposals for files that are gone. Never touches a file.

    Deliberately manual. A missing file is usually a disk that is not mounted,
    and clearing these automatically would throw away every decision made
    about a whole volume the moment it dropped offline.

    What it does, exactly, because a button is about to say so: the proposal
    goes to `superseded` and the item back to `discovered`. **Nothing is
    deleted** — not the item, not the proposal row, not its evidence, not the
    search entry, not history, and least of all a file. `missing_since` is left
    alone, so the record stays out of Search and Browse until a scan finds the
    file again, and if it does, the item is simply an unclassified file once
    more. Running it twice is a no-op: the second pass finds nothing in these
    statuses.

    The superseded proposal keeps its evidence in the table but stops being the
    live one, so the item's page no longer shows a category or a "why here?".
    That is the whole cost, and the confirmation copy says it.
    """
    where, params = _vanished_filter(root)
    rows = conn.execute(
        f"SELECT p.id, p.item_id, i.state FROM proposals p "  # noqa: S608 - placeholders only
        f"JOIN items i ON i.id = p.item_id WHERE {where}",
        params,
    ).fetchall()
    for row in rows:
        # Every state that can hold one of these proposals may legally go back
        # to discovered, but the check is free and this used to write the state
        # column directly — the one place in the codebase that skipped it.
        assert_transition(row["state"], "discovered")
        conn.execute(
            "UPDATE proposals SET status='superseded', updated_at=? WHERE id=?",
            (utc_now(), row["id"]),
        )
        conn.execute(
            "UPDATE items SET state='discovered' WHERE id=?",
            (row["item_id"],),
        )
        # The search entry copies its category and clean name off the live
        # proposal. Superseding one without re-syncing left the index asserting
        # a category no proposal claimed any more — invisible while the file is
        # missing, and wrong the moment it came back.
        sync_search_item(conn, row["item_id"])
    return len(rows)
