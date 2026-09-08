"""A commit that started and never came back.

`plans.status` is set to `executing` before the first file moves and rewritten
when the run ends, so the two are only ever apart while a commit is actually
running. Unless the process dies in between — a container restart, an OOM kill,
a power cut — and then the row says `executing` for ever. The Dashboard read
that row literally and went on reporting *Commit · 1 running* about a process
that had not existed for a week.

**The lock is the evidence.** LibrAIry already holds a `flock` for the whole of
any operation that touches files, and the kernel releases it when the process
holding it dies, however it dies. So the question "is a commit running" has an
answer that cannot go stale and needs nothing stored: try to take the lock. A
plan that says `executing` while the lock is free is a plan whose run is gone.

That answer is deliberately one-sided. The worker takes the same lock for a
whole cycle, so during a scan this finds the lock held and reports nothing —
an interrupted commit stays quiet until the machine is idle, which is a few
seconds later. Under-reporting is the safe direction: telling somebody a
running commit is dead would be much worse than telling them a minute late.

Nothing here repairs anything. Committing the plan again is what finishes it,
and the executor knows how to resume — including the operations whose bytes
moved in the run that died. This module only makes the state sayable.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from librairy.config import Settings
from librairy.locks import LockHeldError, acquire_lock

#  How many unfinished commits one answer describes. More than one at a time
#  means the machine has been killed repeatedly, and the page that lists them
#  is Commit.
SHOWN = 20


@dataclass(frozen=True)
class Unfinished:
    """A plan whose run stopped, and how far it had got."""

    plan_id: str
    total: int
    finished: int
    approved_at: str = ""

    @property
    def remaining(self) -> int:
        return max(0, self.total - self.finished)

    @property
    def sentence(self) -> str:
        """How far it got, in the only terms that are actually known.

        Not "failed" and not "succeeded": every operation recorded its own
        result as it went, so how far the run got is a fact, and what would
        have happened next is not.

        It counts recorded operations, and can therefore be one short. The
        window this whole module exists for is the one where a file's bytes
        moved and its result had not been written yet — so *fewer* files are
        claimed than may have moved, never more, and committing again settles
        it in the time it takes to read the sentence.
        """
        if not self.total:
            return "It stopped before it had anything to do."
        if not self.finished:
            return f"It stopped before any of its {self.total} files were recorded."
        files = "file was" if self.finished == 1 else "files were"
        return f"{self.finished} of {self.total} {files} filed before it stopped."


def busy(settings: Settings) -> bool:
    """Is a LibrAIry process holding the lock right now?

    A commit, an undo, or a worker cycle — this cannot tell them apart and does
    not need to. It is asked only to keep a running commit from being described
    as an interrupted one.
    """
    try:
        with acquire_lock(settings):
            return False
    except LockHeldError:
        return True


def unfinished(conn: sqlite3.Connection, settings: Settings) -> list[Unfinished]:
    """Plans left `executing` by a run that is no longer there.

    One grouped query, bounded, and the lock asked once — the whole thing costs
    a query and an `open` even on a library where the answer is always empty.
    """
    rows = conn.execute(
        """
        SELECT p.id AS plan_id,
               COALESCE(p.approved_at, p.created_at, '') AS approved_at,
               COUNT(o.id) AS total,
               SUM(CASE WHEN o.result IN ('done','renamed_collision')
                        THEN 1 ELSE 0 END) AS finished
          FROM plans p
          LEFT JOIN plan_ops o ON o.plan_id = p.id
         WHERE p.status='executing'
         GROUP BY p.id
         ORDER BY approved_at
         LIMIT ?
        """,
        (SHOWN,),
    ).fetchall()
    if not rows or busy(settings):
        return []
    return [
        Unfinished(
            plan_id=str(row["plan_id"]),
            total=int(row["total"] or 0),
            finished=int(row["finished"] or 0),
            approved_at=str(row["approved_at"] or ""),
        )
        for row in rows
    ]
