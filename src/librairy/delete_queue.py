"""What is waiting to be deleted, why it is there, and how to get it back.

The delete queue has been a folder. `quarantine/_to-delete/…` is where a file
goes when somebody answers *I do not want this*, and LibrAIry has never emptied
it — that is a thing a person does, deliberately, in their own file manager.
Which is right, and left the queue as the one part of the lifecycle with no
surface of its own: you could put files in and had no way to see what was in
there, how much of the disk it was holding, when any of it arrived, or which
decision sent it.

So this is the missing view. It adds no power. Nothing here deletes anything,
nothing expires, there is no *Empty queue* and no thirty-day rule — the whole
point of a queue LibrAIry will not empty is that emptying it stays a human act.
What it adds is the ability to look before you do.

Three things it is careful about.

**Bytes waiting are not bytes saved.** Everything in here is still on the disk,
in full. `14.8 GB waiting in the delete queue` is a fact; `14.8 GB saved` would
be a claim about storage the disk does not support until somebody has actually
removed it.

**Provenance is read, never reconstructed.** Why a file is here comes from the
quarantine entry that recorded it and the plan that moved it — never from
parsing the filename it happens to have.

**Current context, historical decision.** A file queued last month may now sit
under a folder the owner has since protected, or be half of a Live Photo. Both
are worth knowing before it is removed, and neither retroactively cancels the
decision that put it here. It is shown, and nothing is pulled back out on its
own.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from librairy.humanize import human_ago, human_bytes
from librairy.quarantine import DELETE_PILE

#  How many entries one page draws. The same bound every other list in LibrAIry
#  uses, for the same reason: a queue of ten thousand is a number, not a list.
PAGE_SIZE = 50

#  How many members a grouped decision names before it starts counting.
NAMED = 6

#  A decision that sent more than one file here is a group. One file is just a
#  file, and heading it "a decision that queued 1 file" is furniture.
GROUP_FLOOR = 2

#  Where the file physically is, and the state of the bytes.
PRESENT = "present"
GONE = "gone"
CHANGED = "changed"

STATE_NOTE = {
    PRESENT: "",
    GONE: "Not on disk. Something removed it outside LibrAIry.",
    CHANGED: "Changed since it was queued. These are not the bytes you queued.",
}


@dataclass(frozen=True)
class Entry:
    """One file waiting in the delete queue."""

    entry_id: int
    item_id: int | None
    relpath: str
    original_root: str
    original_relpath: str
    size: int
    queued_at: str
    reason: str
    plan_id: str
    state: str = PRESENT
    protected_by: str = ""
    related: list[dict[str, str]] = field(default_factory=list)
    waiting: bool = False

    @property
    def name(self) -> str:
        return PurePosixPath(self.relpath).name

    @property
    def when(self) -> str:
        return human_ago(self.queued_at)

    @property
    def size_label(self) -> str:
        return human_bytes(self.size)

    @property
    def restorable(self) -> bool:
        """Whether `Restore` can be offered at all.

        A control that can only produce an error is worse than no control. The
        bytes have to be there and have to be the ones that were queued, and
        somewhere to go back to has to be recorded.
        """
        return (
            self.state == PRESENT
            and bool(self.original_root and self.original_relpath)
            and not self.waiting
        )


@dataclass(frozen=True)
class Decision:
    """One originating decision, and what is left of what it queued."""

    plan_id: str
    when: str
    total: int
    bytes: int
    names: tuple[str, ...]

    @property
    def more(self) -> int:
        return max(0, self.total - len(self.names))

    @property
    def size_label(self) -> str:
        return human_bytes(self.bytes)

    @property
    def ago(self) -> str:
        return human_ago(self.when)


#  A file is in the queue when it is physically inside the delete pile. Read
#  from where the file actually is, not from a status column: the queue is a
#  folder, and the folder is the truth about it.
_IN_QUEUE = "i.relpath LIKE '_to-delete/%' ESCAPE '\\'"


def summary(conn: sqlite3.Connection) -> dict[str, object]:
    """How much is waiting, in files and in bytes.

    Both are current storage. Nothing here has been freed, and the wording
    everywhere downstream says *waiting* rather than *saved* for that reason.
    """
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS files, COALESCE(SUM(i.size), 0) AS bytes,
               MIN(qe.quarantined_at) AS oldest
        FROM quarantine_entries qe
        JOIN items i ON i.id = qe.item_id
        WHERE qe.restored_at IS NULL AND i.root='quarantine'
          AND i.missing_since IS NULL AND {_IN_QUEUE}
        """,  # noqa: S608 - `_IN_QUEUE` is a module constant
    ).fetchone()
    files = int(row["files"] or 0) if row else 0
    return {
        "files": files,
        "bytes": int(row["bytes"] or 0) if row else 0,
        "bytes_label": human_bytes(int(row["bytes"] or 0) if row else 0),
        "oldest": human_ago(str(row["oldest"] or "")) if row and row["oldest"] else "",
    }


def entries(
    conn: sqlite3.Connection, *, limit: int = PAGE_SIZE, offset: int = 0
) -> list[Entry]:
    """One bounded page of the queue, newest first.

    Every fact on the row is already in the database — where it came from, when
    it was queued, which decision sent it, how big it is. Nothing here stats a
    file: a page that did would be one network round trip per row on a NAS.
    The one thing that *is* compared against the bytes is the fingerprint, and
    that comes from the index too.
    """
    rows = conn.execute(
        f"""
        SELECT qe.id AS entry_id, qe.item_id AS item_id, qe.reason AS reason,
               qe.quarantined_at AS queued_at, qe.plan_id AS plan_id,
               qe.original_root AS original_root,
               qe.original_relpath AS original_relpath,
               i.relpath AS relpath, i.size AS size, i.fingerprint AS fingerprint,
               i.missing_since AS missing_since,
               --  What the file was when the decision that queued it moved it.
               --  Compared against the index rather than by hashing: the
               --  scanner keeps `items.fingerprint` current, so "changed since
               --  queued" is answerable without a page reading every file.
               (SELECT o.src_fingerprint FROM plan_ops o
                 WHERE o.plan_id = qe.plan_id AND o.item_id = qe.item_id
                 ORDER BY o.seq LIMIT 1) AS queued_fingerprint
        FROM quarantine_entries qe
        LEFT JOIN items i ON i.id = qe.item_id
        WHERE qe.restored_at IS NULL AND i.id IS NOT NULL AND i.root='quarantine'
          AND {_IN_QUEUE}
        ORDER BY qe.id DESC LIMIT ? OFFSET ?
        """,  # noqa: S608 - `_IN_QUEUE` is a module constant
        (limit, max(0, offset)),
    ).fetchall()
    if not rows:
        return []
    item_ids = [int(row["item_id"]) for row in rows if row["item_id"] is not None]
    protection = _protection(conn, rows)
    related = _related(conn, item_ids)
    waiting = _waiting(conn, [int(row["entry_id"]) for row in rows])
    return [
        Entry(
            entry_id=int(row["entry_id"]),
            item_id=int(row["item_id"]) if row["item_id"] is not None else None,
            relpath=str(row["relpath"] or ""),
            original_root=str(row["original_root"] or ""),
            original_relpath=str(row["original_relpath"] or ""),
            size=int(row["size"] or 0),
            queued_at=str(row["queued_at"] or ""),
            reason=str(row["reason"] or ""),
            plan_id=str(row["plan_id"] or ""),
            state=_state(row),
            protected_by=protection.get(str(row["original_relpath"] or ""), ""),
            related=related.get(int(row["item_id"] or 0), []),
            waiting=int(row["entry_id"]) in waiting,
        )
        for row in rows
    ]


def _state(row: sqlite3.Row) -> str:
    """Where the bytes stand: there, gone, or not the ones that were queued.

    Both answers matter before a permanent removal. A file somebody deleted
    outside LibrAIry is not waiting for anything, and a file that has changed
    since is not the file the decision was about — restoring it would put
    different bytes back where the original used to be.
    """
    if row["missing_since"] is not None:
        return GONE
    queued = str(row["queued_fingerprint"] or "")
    current = str(row["fingerprint"] or "")
    if queued and current and queued != current:
        return CHANGED
    return PRESENT


def total(conn: sqlite3.Connection) -> int:
    return int(summary(conn)["files"])


def decisions(conn: sqlite3.Connection, *, limit: int = 20) -> list[Decision]:
    """The originating decisions that queued more than one file.

    Grouped by the plan that moved them and by nothing else. Files queued on
    the same day, for the same reason, from the same folder are still separate
    answers somebody gave separately — grouping on any of those would merge
    decisions that merely resemble each other.
    """
    rows = conn.execute(
        f"""
        SELECT qe.plan_id AS plan_id, COUNT(*) AS total,
               COALESCE(SUM(i.size), 0) AS bytes,
               MAX(qe.quarantined_at) AS when_
        FROM quarantine_entries qe
        JOIN items i ON i.id = qe.item_id
        WHERE qe.restored_at IS NULL AND qe.plan_id IS NOT NULL
          AND i.root='quarantine' AND i.missing_since IS NULL AND {_IN_QUEUE}
        GROUP BY qe.plan_id
        HAVING total >= ?
        ORDER BY when_ DESC, plan_id LIMIT ?
        """,  # noqa: S608 - `_IN_QUEUE` is a module constant
        (GROUP_FLOOR, limit),
    ).fetchall()
    if not rows:
        return []
    names = _names(conn, [str(row["plan_id"]) for row in rows])
    return [
        Decision(
            plan_id=str(row["plan_id"]),
            when=str(row["when_"] or ""),
            total=int(row["total"]),
            bytes=int(row["bytes"] or 0),
            names=names.get(str(row["plan_id"]), ()),
        )
        for row in rows
    ]


def _names(
    conn: sqlite3.Connection, plan_ids: list[str]
) -> dict[str, tuple[str, ...]]:
    placeholders = ",".join("?" * len(plan_ids))
    rows = conn.execute(
        f"""
        SELECT qe.plan_id AS plan_id, i.relpath AS relpath
        FROM quarantine_entries qe JOIN items i ON i.id = qe.item_id
        WHERE qe.plan_id IN ({placeholders}) AND qe.restored_at IS NULL
          AND i.root='quarantine' AND i.missing_since IS NULL AND {_IN_QUEUE}
        ORDER BY qe.plan_id, qe.id
        """,  # noqa: S608 - placeholders are counted; `_IN_QUEUE` is a constant
        plan_ids,
    ).fetchall()
    found: dict[str, list[str]] = {}
    for row in rows:
        bucket = found.setdefault(str(row["plan_id"]), [])
        if len(bucket) < NAMED:
            bucket.append(PurePosixPath(str(row["relpath"])).name)
    return {plan_id: tuple(names) for plan_id, names in found.items()}


def _protection(
    conn: sqlite3.Connection, rows: list[sqlite3.Row]
) -> dict[str, str]:
    """Whether the place a file came from is now protected — one read.

    Current context about a historical decision. A RAW queued last month whose
    folder has since been set to preserve originals is exactly the thing to
    know before removing it permanently, and it does not retroactively cancel
    the decision that put it here.
    """
    from librairy.format_policy import index, protecting
    from librairy.protected import protected_roots

    originals = [
        str(row["original_relpath"] or "")
        for row in rows
        if str(row["original_root"] or "") == "library" and row["original_relpath"]
    ]
    if not originals:
        return {}
    cached, roots = index(conn), protected_roots(conn)
    return {
        relpath: protecting(conn, relpath, cached=cached, roots=roots)
        for relpath in dict.fromkeys(originals)
    }


def _related(
    conn: sqlite3.Connection, item_ids: list[int]
) -> dict[int, list[dict[str, str]]]:
    """The live files each queued file still belongs with — one query.

    A RAW whose JPEG render is still in the library, the still half of a Live
    Photo, the film a subtitle explains. No queue expansion and no restore
    expansion: this says what the file is part of, and nothing acts on it.
    """
    from librairy.relationships import for_items

    if not item_ids:
        return {}
    found = for_items(conn, item_ids)
    return {
        item_id: [
            {"label": entry.label, "path": entry.relpath, "name": entry.name}
            for entry in entries[:3]
        ]
        for item_id, entries in found.items()
    }


def _waiting(conn: sqlite3.Connection, entry_ids: list[int]) -> set[int]:
    """Entries that already have a decision waiting for Commit.

    A second Restore offered for a file that already has one is two plans for
    one intention, and the executor would refuse the second at the worst
    possible moment.
    """
    if not entry_ids:
        return set()
    placeholders = ",".join("?" * len(entry_ids))
    rows = conn.execute(
        f"""
        SELECT quarantine_entry_id AS entry_id FROM plans
        WHERE quarantine_entry_id IN ({placeholders})
          AND status IN ('approved','executing')
        """,  # noqa: S608 - placeholders are counted from the id list
        entry_ids,
    ).fetchall()
    return {int(row["entry_id"]) for row in rows}


def is_queued(relpath: str) -> bool:
    """Whether this quarantine-relative path is inside the delete queue."""
    text = str(relpath or "").replace("\\", "/").strip("/")
    return bool(text) and text.split("/", 1)[0] == DELETE_PILE


def health(conn: sqlite3.Connection) -> dict[str, object]:
    """The queue in four numbers, for a page that is only summarising it.

    One query rather than `summary` plus a page of `entries`: Health wants
    counts, and building fifty `Entry` objects to count two of them is the
    shape that makes a summary page slower than the page it summarises.

    `files` and `bytes` describe what is *waiting* — present, unchanged bytes
    still occupying storage. `changed` and `gone` are counted separately
    because they are the two states where Restore is not offered, and where a
    person needs to know before they empty anything.
    """
    row = conn.execute(
        f"""
        SELECT
          SUM(CASE WHEN i.missing_since IS NULL THEN 1 ELSE 0 END) AS files,
          SUM(CASE WHEN i.missing_since IS NULL THEN i.size ELSE 0 END) AS bytes,
          SUM(CASE WHEN i.missing_since IS NOT NULL THEN 1 ELSE 0 END) AS gone,
          SUM(CASE WHEN i.missing_since IS NULL AND queued.src_fingerprint IS NOT NULL
                    AND i.fingerprint IS NOT NULL
                    AND queued.src_fingerprint <> i.fingerprint
                   THEN 1 ELSE 0 END) AS changed,
          MIN(CASE WHEN i.missing_since IS NULL THEN qe.quarantined_at END) AS oldest
        FROM quarantine_entries qe
        JOIN items i ON i.id = qe.item_id
        LEFT JOIN plan_ops queued ON queued.id = (
          SELECT o.id FROM plan_ops o
           WHERE o.plan_id = qe.plan_id AND o.item_id = qe.item_id
           ORDER BY o.seq LIMIT 1
        )
        WHERE qe.restored_at IS NULL AND i.root='quarantine' AND {_IN_QUEUE}
        """,  # noqa: S608 - `_IN_QUEUE` is a module constant
    ).fetchone()
    return {
        "files": int(row["files"] or 0) if row else 0,
        "bytes": int(row["bytes"] or 0) if row else 0,
        "changed": int(row["changed"] or 0) if row else 0,
        "gone": int(row["gone"] or 0) if row else 0,
        "oldest": human_ago(str(row["oldest"] or "")) if row and row["oldest"] else "",
    }
