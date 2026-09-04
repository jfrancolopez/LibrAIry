"""Files that are only at the destination — recorded whole, shown a page at a
time, and never removed.

A Mirror is a place that should represent the current Library. When it holds
something the Library no longer has, that is worth saying out loud, and saying
it is the *entire* difference between Mirror and Backup. One cell of the policy
matrix, and no second architecture:

    Backup   only at destination → keep, quietly
    Mirror   only at destination → keep, and say so

**It is not permission.** Nothing in this module removes anything at a
destination, and nothing downstream of it can: the vocabulary two layers up has
no verb for it.

## Current divergence is not run history

Two questions that a single table would blur:

    what is only at the destination *now*      this module
    what each run did, and when                `backup_runs`

A file sitting at a destination across ten Mirror runs is **one** fact about
today, not ten findings. So this is keyed on the file rather than on the run:
running again updates `last_seen_at` and leaves `first_seen_at` alone, which
gives the two dates worth having — *this has been there since March, and we
checked twenty minutes ago* — without a row per observation.

## The set is complete; the page is bounded

These are different requirements and the first version of this module confused
them. It kept a thousand paths beside a complete count, which reads honestly —
*1,000 of 412,338 shown* is true about both numbers — and is the wrong answer,
because the other 411,338 were not on a later page. They were never written
down. Somebody asking *which files are only on my backup?* is usually asking
because they intend to go and look at them, and a sample cannot be paged,
searched, or worked through.

So the dataset is one row per divergent file, and boundedness is kept where it
belongs:

    the comparison        streams; nothing accumulates but the write batch
    reconciliation        one DELETE, not a set read into Python
    the page              a LIMIT and a cursor
    the count             SQL

A destination holding four hundred thousand of them is four hundred thousand
rows, which SQLite is untroubled by — `scripts/measure_divergence.py` is the
measurement that says so rather than the assumption that it would be fine.

## How a set is reconciled

Every comparison of one scope gets a **generation** number. Files it saw are
upserted with that number; when the comparison saw the *whole* destination,
everything in the scope carrying an older number is deleted in one statement.

Two things fall out of that, and both are the point:

**A file somebody removed by hand clears without this module hearing about the
removal.** It is simply not in what was seen. That is the only bookkeeping that
cannot drift, and it is why `first_seen_at` is left alone by the upsert rather
than recomputed.

**A comparison that did not finish deletes nothing.** Half a listing is not
evidence that the other half is gone — and for a drive that was unplugged
mid-scan it is evidence of nothing at all. An incomplete comparison refreshes
what it saw, records itself as incomplete, and leaves the rest of the last
known set exactly where it was. `complete` has no default here; every caller
has to say which kind of comparison it made.

**And clearing is never read as a Library change.** A file vanishing from a
destination is news about the destination.

See `docs/ROADMAP.md` M3-03.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from librairy.db import transaction
from librairy.planner import utc_now

#  One page of them on screen. The same page size as everywhere else in the
#  program; see `docs/performance.md` on the bounded-page rule.
PAGE = 50

#  How many rows are written per statement. Large enough that a hundred
#  thousand files is fifty round trips rather than a hundred thousand, small
#  enough that the batch is the only thing held in memory.
CHUNK = 2_000


@dataclass(frozen=True)
class Divergent:
    """One file that is at the destination and not in the library."""

    relpath: str
    size: int
    first_seen_at: str
    last_seen_at: str
    category: str = ""


@dataclass(frozen=True)
class Page:
    """One bounded page, and the cursor that reaches the next one."""

    rows: tuple[Divergent, ...] = ()
    #  Where this page started, and where the next one starts. Keyset rather
    #  than OFFSET: the cursor is the primary key, so page eight thousand costs
    #  what page one costs instead of walking four hundred thousand rows to
    #  reach it. `scripts/measure_divergence.py` measures both.
    after: str = ""
    next: str = ""

    @property
    def more(self) -> bool:
        return bool(self.next)

    def __len__(self) -> int:
        return len(self.rows)

    def __iter__(self) -> Iterator[Divergent]:
        return iter(self.rows)


@dataclass(frozen=True)
class Summary:
    """What is only at one destination, and how well anybody knows it."""

    destination_id: int
    count: int = 0
    #  When anybody last looked, and when the set was last known whole. A drive
    #  that was unplugged during a comparison has the first without the second,
    #  and a page that showed only one of them would be lying by omission.
    checked_at: str = ""
    verified_at: str = ""
    since: str = ""
    complete: bool = True

    @property
    def any(self) -> bool:
        return self.count > 0

    @property
    def unverified(self) -> bool:
        """Did the last comparison stop before it had seen everything?"""
        return not self.complete

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
    entries: Iterable,  # transfer_plan.Entry; only `relpath` and sizes are read
    complete: bool,
) -> int:
    """Write down everything that is only at this destination, in one scope.

    `entries` is consumed one at a time and never turned into a list — a
    destination holding four hundred thousand of these is a stream, not an
    argument.

    `complete` is the caller saying whether the listing behind `entries` was
    the whole destination. It is required, and getting it wrong is the one way
    to lose information here, so it is a question every call site has to
    answer out loud rather than inherit from a default.

    Returns how many were seen.
    """
    now = utc_now()
    seen = 0
    #  One transaction for the whole scan, for two reasons and the second is
    #  the one that matters. Every connection in this program is opened in
    #  autocommit, so without this a million files is five hundred separate
    #  commits — measured at 57 seconds, against 4 inside a transaction.
    #
    #  And a comparison is one fact: the rows it saw and the rows it removed
    #  are the same statement about the destination. A process killed between
    #  them would leave a set that was never true — half of it reconciled
    #  against this comparison and half against the last one.
    with transaction(conn):
        generation = _next_generation(conn, destination_id, category)
        seen = _observe(conn, destination_id, category, entries, generation, now)
        if complete:
            #  Set arithmetic in one statement: whatever this comparison did
            #  not see is not there any more. Only ever reached when the
            #  listing was whole — an incomplete scan removing rows would
            #  report a pulled drive as somebody having tidied up.
            conn.execute(
                "DELETE FROM backup_divergence"
                " WHERE destination_id=? AND category=? AND generation<>?",
                (destination_id, category, generation),
            )
        _stamp(conn, destination_id, category, generation, complete=complete, now=now)
    return seen


def _observe(
    conn: sqlite3.Connection,
    destination_id: int,
    category: str,
    entries: Iterable,
    generation: int,
    now: str,
) -> int:
    seen = 0
    for batch in _batched(entries, CHUNK):
        conn.executemany(
            """
            INSERT INTO backup_divergence(destination_id, category, relpath, size,
                                          first_seen_at, last_seen_at, generation)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(destination_id, relpath) DO UPDATE SET
              category=excluded.category,
              size=excluded.size,
              last_seen_at=excluded.last_seen_at,
              generation=excluded.generation
            """,
            #  `first_seen_at` is absent from the update clause on purpose.
            #  "There since March" is the fact somebody actually wants, and
            #  re-stamping it hourly would replace it with "there since an
            #  hour ago".
            [
                (
                    destination_id,
                    category,
                    entry.relpath,
                    entry.destination_size,
                    now,
                    now,
                    generation,
                )
                for entry in batch
            ],
        )
        seen += len(batch)
    return seen


def _stamp(
    conn: sqlite3.Connection,
    destination_id: int,
    category: str,
    generation: int,
    *,
    complete: bool,
    now: str,
) -> None:
    conn.execute(
        """
        INSERT INTO backup_divergence_scans(destination_id, category, generation,
                                            complete, checked_at, verified_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(destination_id, category) DO UPDATE SET
          generation=excluded.generation,
          complete=excluded.complete,
          checked_at=excluded.checked_at,
          -- "When anybody last looked" always moves. "When the set was last
          -- known whole" only moves when it was.
          verified_at=CASE WHEN excluded.complete
                           THEN excluded.verified_at
                           ELSE backup_divergence_scans.verified_at END
        """,
        (destination_id, category, generation, int(complete), now, now if complete else ""),
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
        f"DELETE FROM backup_divergence_scans WHERE destination_id=?{where}",  # noqa: S608
        args,
    )


def summary(conn: sqlite3.Connection, destination_id: int) -> Summary:
    """How much is only at this destination, across every scope it holds."""
    row = conn.execute(
        "SELECT COUNT(*) AS total, MIN(first_seen_at) AS since"
        " FROM backup_divergence WHERE destination_id=?",
        (destination_id,),
    ).fetchone()
    scan = conn.execute(
        "SELECT MAX(checked_at) AS checked, MIN(verified_at) AS verified,"
        " MIN(complete) AS complete FROM backup_divergence_scans WHERE destination_id=?",
        (destination_id,),
    ).fetchone()
    return Summary(
        destination_id=destination_id,
        count=int(row["total"] or 0) if row else 0,
        since=str(row["since"] or "") if row else "",
        checked_at=str(scan["checked"] or "") if scan else "",
        verified_at=str(scan["verified"] or "") if scan else "",
        #  One scope that could not be finished makes the whole destination's
        #  answer unverified, because the number shown adds them all together.
        complete=bool(scan["complete"]) if scan and scan["complete"] is not None else True,
    )


def page(
    conn: sqlite3.Connection,
    destination_id: int,
    *,
    after: str = "",
    limit: int = PAGE,
) -> Page:
    """One bounded page of the files that are only there, in path order.

    Path order rather than by date, because the person reading this is usually
    working through them a folder at a time — and because it is the primary
    key, which is what makes the cursor free.

    `after` is the last path of the previous page. Passing the cursor rather
    than a page number keeps the deep pages as cheap as the first: an OFFSET of
    four hundred thousand has to walk four hundred thousand rows to discard
    them.
    """
    size = max(1, min(int(limit), 500))
    rows = [
        Divergent(
            relpath=str(row["relpath"]),
            size=int(row["size"] or 0),
            first_seen_at=str(row["first_seen_at"] or ""),
            last_seen_at=str(row["last_seen_at"] or ""),
            category=str(row["category"] or ""),
        )
        #  One row more than the page, to find out whether there is a next one
        #  without a second COUNT over the remainder.
        for row in conn.execute(
            "SELECT relpath, size, first_seen_at, last_seen_at, category"
            " FROM backup_divergence WHERE destination_id=? AND relpath > ?"
            " ORDER BY relpath LIMIT ?",
            (destination_id, after, size + 1),
        )
    ]
    return Page(
        rows=tuple(rows[:size]),
        after=after,
        next=rows[size - 1].relpath if len(rows) > size else "",
    )


def totals(conn: sqlite3.Connection) -> dict[int, int]:
    """Every destination's divergence count, for a status line. One statement."""
    return {
        int(row["destination_id"]): int(row["total"] or 0)
        for row in conn.execute(
            "SELECT destination_id, COUNT(*) AS total FROM backup_divergence"
            " GROUP BY destination_id"
        )
    }


def _next_generation(conn: sqlite3.Connection, destination_id: int, category: str) -> int:
    row = conn.execute(
        "SELECT generation FROM backup_divergence_scans"
        " WHERE destination_id=? AND category=?",
        (destination_id, category),
    ).fetchone()
    return (int(row["generation"] or 0) if row else 0) + 1


def _batched(entries: Iterable, size: int) -> Iterator[list]:
    batch: list = []
    for entry in entries:
        batch.append(entry)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch
