"""Files that are only at the destination — recorded, shown, and never removed.

A Mirror is a place that should represent the current Library. When it holds
something the Library no longer has, that is worth saying out loud, and saying
it is the *entire* difference between Mirror and Backup. One cell of the policy
matrix, and no second architecture:

    Backup   only at destination → keep, quietly
    Mirror   only at destination → keep, and say so

**It is not permission.** Nothing in this module removes anything, and nothing
downstream of it can: the vocabulary two layers up has no verb for it.

## Current divergence is not run history

Two questions that a single table would blur:

    what is only at the destination *now*      this module
    what each run did, and when                `backup_runs`

A file sitting at a destination across ten Mirror runs is **one** fact about
today, not ten findings. So this is keyed on the file rather than on the run:
running again updates `last_seen_at` and leaves `first_seen_at` alone, which
gives the two dates worth having — *this has been there since March, and we
checked twenty minutes ago* — without a row per observation.

And when somebody deletes it themselves, it clears on the next comparison,
because rows not seen by that comparison are dropped. Nothing has to notice the
deletion or be told about it; the absence is simply not re-recorded.

**That clearing is never read as a Library change.** A file vanishing from a
destination is news about the destination.

## The smallest truthful representation

A destination holding four hundred thousand files the Library no longer has is
a real thing to be told about. Storing four hundred thousand path strings for
ever, and rewriting them every hour, is not the way to tell somebody.

So the **count is complete** and the **paths are a bounded sample**: `KEEP` of
them, oldest-first so the sample is stable between runs rather than reshuffling.
The count comes from the comparison, which can reproduce the rest cheaply if
anything ever needs them. A page that says *18 only at the destination* is
exact; one that says *1,000 of 412,338 shown* is exact about both numbers.

See `docs/ROADMAP.md` M3-03.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from librairy.planner import utc_now

#  How many paths are kept per destination and category. A thousand is more
#  than anybody scrolls and small enough to rewrite hourly without thinking
#  about it; the count beside them is always the whole truth.
KEEP = 1000

#  One page of them on screen.
PAGE = 50


@dataclass(frozen=True)
class Divergent:
    """One file that is at the destination and not in the library."""

    relpath: str
    size: int
    first_seen_at: str
    last_seen_at: str


@dataclass(frozen=True)
class Summary:
    """What is only at one destination, and when anybody last looked."""

    destination_id: int
    #  The complete number, from the comparison. Never `len(kept)`.
    count: int = 0
    kept: int = 0
    checked_at: str = ""
    since: str = ""

    @property
    def any(self) -> bool:
        return self.count > 0

    @property
    def partial(self) -> bool:
        """Are there more than were kept? The page has to be able to say so."""
        return self.count > self.kept

    @property
    def sentence(self) -> str:
        """The agreed words, and only the agreed words.

        Not *extra*, which invites tidying, and not *stale*, which sounds like
        rot. `docs/ui-vocabulary.md` pins this.
        """
        if not self.count:
            return "nothing here that your library does not have"
        return f"{self.count:,} only at the destination"


def record(
    conn: sqlite3.Connection,
    *,
    destination_id: int,
    category: str,
    entries,  # noqa: ANN001 - transfer_plan.Entry, only `relpath` and sizes read
    count: int,
) -> None:
    """Replace what is known about this destination and category.

    Read what is remembered, clear it, write what was seen. Not an upsert
    followed by "delete anything older than now": `utc_now()` has one-second
    granularity, so two comparisons in the same second are indistinguishable
    and the stale rows survive. Timestamps are for showing people, never for
    deciding what a set contains.

    A file somebody removed by hand clears without this module ever hearing
    about the removal — it is simply not in what was seen. That is the only
    bookkeeping that cannot drift, and it is why `first_seen_at` is carried
    across by hand rather than left to a conflict clause.
    """
    now = utc_now()
    seen = list(entries)[:KEEP]
    since = {
        str(row["relpath"]): str(row["first_seen_at"])
        for row in conn.execute(
            "SELECT relpath, first_seen_at FROM backup_divergence"
            " WHERE destination_id=? AND category=?",
            (destination_id, category),
        )
    }
    conn.execute(
        "DELETE FROM backup_divergence WHERE destination_id=? AND category=?",
        (destination_id, category),
    )
    conn.executemany(
        """
        INSERT INTO backup_divergence(destination_id, category, relpath, size,
                                      first_seen_at, last_seen_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (
                destination_id,
                category,
                entry.relpath,
                entry.destination_size,
                #  Kept from the first time it was noticed. "There since March"
                #  is the fact somebody actually wants, and re-stamping it every
                #  hour would replace it with "there since an hour ago".
                since.get(entry.relpath, now),
                now,
            )
            for entry in seen
        ],
    )
    conn.execute(
        """
        INSERT INTO backup_divergence_totals(destination_id, category, count, checked_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(destination_id, category) DO UPDATE SET
          count=excluded.count, checked_at=excluded.checked_at
        """,
        (destination_id, category, count, now),
    )


def forget(conn: sqlite3.Connection, destination_id: int, category: str = "") -> None:
    """Drop what is known about a destination. Touches nothing at the destination."""
    where = " AND category=?" if category else ""
    args = (destination_id, category) if category else (destination_id,)
    conn.execute(
        f"DELETE FROM backup_divergence WHERE destination_id=?{where}",  # noqa: S608
        args,
    )
    conn.execute(
        f"DELETE FROM backup_divergence_totals WHERE destination_id=?{where}",  # noqa: S608
        args,
    )


def summary(conn: sqlite3.Connection, destination_id: int) -> Summary:
    """How much is only at this destination, across every category it holds."""
    row = conn.execute(
        "SELECT COALESCE(SUM(count), 0) AS total, MAX(checked_at) AS checked"
        " FROM backup_divergence_totals WHERE destination_id=?",
        (destination_id,),
    ).fetchone()
    kept = conn.execute(
        "SELECT COUNT(*) AS kept, MIN(first_seen_at) AS since FROM backup_divergence"
        " WHERE destination_id=?",
        (destination_id,),
    ).fetchone()
    return Summary(
        destination_id=destination_id,
        count=int(row["total"] or 0) if row else 0,
        kept=int(kept["kept"] or 0) if kept else 0,
        checked_at=str(row["checked"] or "") if row else "",
        since=str(kept["since"] or "") if kept else "",
    )


def page(
    conn: sqlite3.Connection, destination_id: int, *, page_number: int = 1
) -> list[Divergent]:
    """One bounded page of the files that are only there.

    Oldest first, which is what makes the sample stable: a destination whose
    divergence is re-read every hour shows the same files in the same order
    rather than reshuffling under somebody who is reading it.
    """
    offset = max(0, (max(1, page_number) - 1) * PAGE)
    return [
        Divergent(
            relpath=str(row["relpath"]),
            size=int(row["size"] or 0),
            first_seen_at=str(row["first_seen_at"] or ""),
            last_seen_at=str(row["last_seen_at"] or ""),
        )
        for row in conn.execute(
            "SELECT relpath, size, first_seen_at, last_seen_at FROM backup_divergence"
            " WHERE destination_id=? ORDER BY first_seen_at, relpath LIMIT ? OFFSET ?",
            (destination_id, PAGE, offset),
        )
    ]


def totals(conn: sqlite3.Connection) -> dict[int, int]:
    """Every destination's divergence count, for a status line. One statement."""
    return {
        int(row["destination_id"]): int(row["total"] or 0)
        for row in conn.execute(
            "SELECT destination_id, SUM(count) AS total FROM backup_divergence_totals"
            " GROUP BY destination_id"
        )
    }
