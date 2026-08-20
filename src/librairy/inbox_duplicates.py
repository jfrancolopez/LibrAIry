"""A file you already have, arriving again.

The worker has recognised these since the first release: `dedup.py` finds an
inbox file whose bytes match something in the library, and stages a quarantine
proposal for the inbox copy. What was never built is the *user-facing* half —
the row that says which library file it matches, the Quarantine entry that
remembers, and the check that the library copy is still there when Commit runs.

That last one is the reason this module exists rather than a template change.

    the duplicate is found          the library copy is what makes it redundant
    the library copy is deleted     by hand, by another tool, by a restore
    the person presses Commit       and the inbox copy goes to Quarantine

Now there is no copy anywhere. Nothing was overwritten and nothing was deleted
by LibrAIry, and the person has still lost the file — because the evidence that
made it safe to set aside had expired and nobody re-read it. So the twin is
looked up **at the moment of execution**, from the index, by fingerprint; if it
is not there, the operation is skipped and the inbox copy stays exactly where
it is.

Reading it by fingerprint rather than by a stored pair id is what makes the rest
fall out for free. A library copy that moved is still the twin. A second inbox
copy of the same bytes finds the same twin. And an inbox file whose twin has
gone is not a duplicate any more, which is the same sentence as the safety
check — one rule, read in two places, rather than two rules that agree today.

Nothing here decides *which* library copy is canonical. When several exist that
is a genuine ambiguity and it belongs to the library, not to the inbox: the
inbox copy is redundant whichever one is kept, and `audit_duplicates.py` is
where the library-side question gets asked.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from librairy.config import Settings

#  Written into a duplicate proposal's evidence by the worker, and read back by
#  `quarantine.quarantine_reason` to tell "the duplicate finder decided this"
#  from "you did". One spelling, in one place, so the two ends cannot drift.
EVIDENCE_PREFIX = "exact duplicate of"


@dataclass(frozen=True)
class Twin:
    """The library file an inbox file is a copy of."""

    item_id: int
    relpath: str
    size: int


def twins_of(conn: sqlite3.Connection, item_id: int) -> list[Twin]:
    """Every library file with this item's bytes, as the index has them now.

    Plural because a library may hold more than one copy, and that is a real
    situation with a real answer — just not this file's answer. The arrival is
    redundant whichever of them is kept.

    Deliberately not restricted to items in the inbox. The same question is
    asked twice about the same file at two different moments: before Commit,
    while it is still an arrival, and again as it is being recorded in
    Quarantine, by which point its row has already followed it there. A guard
    on `root` made the second call return nothing, which is how the column that
    says *what this is a copy of* came to be written as NULL.
    """
    row = conn.execute(
        "SELECT fingerprint FROM items WHERE id=?", (item_id,)
    ).fetchone()
    if row is None or not row["fingerprint"]:
        return []
    return [
        Twin(int(found["id"]), found["relpath"], int(found["size"] or 0))
        for found in conn.execute(
            "SELECT id, relpath, size FROM items"
            " WHERE root='library' AND fingerprint=? AND missing_since IS NULL"
            " AND id != ? ORDER BY relpath",
            (row["fingerprint"], item_id),
        )
    ]


def is_duplicate_proposal(conn: sqlite3.Connection, item_id: int) -> bool:
    """Did the duplicate finder stage this, or did a person?

    Read off the evidence, like `quarantine.quarantine_reason` does, rather
    than stored a second time. Two columns saying why can disagree; one cannot.
    """
    row = conn.execute(
        "SELECT evidence FROM proposals WHERE item_id=? AND status != 'superseded'"
        " ORDER BY id DESC LIMIT 1",
        (item_id,),
    ).fetchone()
    return bool(row) and EVIDENCE_PREFIX in (row["evidence"] or "")


def still_redundant(
    conn: sqlite3.Connection, settings: Settings, item_id: int
) -> Twin | None:
    """The library copy that still makes this inbox file redundant, or None.

    Checked against the disk and not only the index. A row saying a file is in
    the library is a statement about the last scan; setting a copy aside on the
    strength of it is a decision about right now.
    """
    from librairy.fingerprint import blake2b_file
    from librairy.paths import PathValidationError, validate_relpath

    row = conn.execute(
        "SELECT fingerprint FROM items WHERE id=?", (item_id,)
    ).fetchone()
    if row is None or not row["fingerprint"]:
        return None
    for twin in twins_of(conn, item_id):
        try:
            path = validate_relpath(settings.library_dir, twin.relpath, kind="library")
        except PathValidationError:
            continue
        if path.is_file() and blake2b_file(path) == row["fingerprint"]:
            return twin
    return None


def describe(conn: sqlite3.Connection, item_id: int) -> dict[str, object] | None:
    """What Review and Quarantine both say about this file.

    One description, two pages, so "already in your library" cannot be phrased
    two ways. Returns None for anything that is not a staged exact duplicate.
    """
    if not is_duplicate_proposal(conn, item_id):
        return None
    twins = twins_of(conn, item_id)
    return {
        "twins": [
            {"item_id": twin.item_id, "relpath": twin.relpath} for twin in twins
        ],
        "match": twins[0].relpath if twins else "",
        "extra": max(0, len(twins) - 1),
        #  The whole reason this is actionable without asking which copy to
        #  keep: the library copy is the filed one and this is the arrival.
        "note": (
            "The bytes are identical."
            if twins
            else "The copy that made this a duplicate is no longer in your library."
        ),
        "still_redundant": bool(twins),
    }
