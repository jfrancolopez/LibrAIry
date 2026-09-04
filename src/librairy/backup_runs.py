"""What each backup run did — and the flag this module deliberately does not have.

## There is no "this destination is up to date" column

That absence is the design, and it is the answer to the question this feature
is easiest to get wrong: *a run that copied 73 of 100 files and then died.*

A stored "current" flag has to be right about every way a transfer can end —
killed process, dropped connection, full disk, a file that vanished halfway —
and it only has to be wrong once for a backup to sit there saying it is fine.
So nothing here stores one. **Whether a destination is up to date is answered
by comparing, every time it is asked**, and the comparison is cheap, repeatable
and reads authoritative state on both sides.

What is stored is what *happened*:

    this run started at 09:14, planned 100 copies, transferred 73,
    and stopped because the disk filled

which is true regardless of how it ended, and stays true afterwards. A partial
run is recorded as a partial run. The next comparison finds the 27 that are
still missing, because they are still missing, and converges.

That is also why there is no resume protocol. Convergence comes from the
comparison, not from remembering where the last one got to.

## Operational history, not Library history

    History          what happened to your files. Moves, undos, quarantine
    backup runs      what happened to a *copy* of them, somewhere else

A backup run saying "312 copied, 9 updated, 14 only at the destination" is not
a claim that 335 things happened to the library. Nothing in the library
changed; 321 files were copied outward and 14 were left alone. Keeping these in
separate tables with separate words is what stops a failed backup from reading
like a failed Commit.

## Cadence

A policy is compared at most once every `MIN_INTERVAL`. Not a cron: a freshness
guard, in the same spirit as the metrics rollup — the expensive part of a
backup is the comparison, and comparing a category of three hundred thousand
photographs every five-second worker cycle would be a machine that does nothing
else. See `docs/ROADMAP.md` M3-03.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from librairy.planner import utc_now

#  A run's life. `planned` exists so that a process killed between planning and
#  finishing leaves a row saying so rather than leaving nothing at all — an
#  absence would be indistinguishable from a run that never started.
PLANNED = "planned"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"

STATES = (PLANNED, RUNNING, SUCCEEDED, FAILED)

#  How often one policy may be compared. The comparison is the expensive half
#  and it is the half that has to be bounded; the transfer itself is rclone's
#  problem and only moves what differs.
MIN_INTERVAL = 3600

#  How many runs are kept per destination. A backup that has run every hour for
#  a year is 8,760 rows nobody will read past the first twenty of.
KEEP_RUNS = 200


@dataclass(frozen=True)
class Run:
    id: int
    destination_id: int
    category: str
    mode: str
    state: str
    started_at: str
    finished_at: str
    planned_copies: int
    planned_updates: int
    destination_only: int
    transferred: int
    bytes_sent: int
    outcome: str
    detail: str

    @property
    def complete(self) -> bool:
        return self.state == SUCCEEDED

    @property
    def partial(self) -> bool:
        """Did work, and did not finish. A state worth naming rather than
        rounding to either success or failure."""
        return self.state == FAILED and self.transferred > 0

    @property
    def summary(self) -> str:
        parts = []
        if self.transferred:
            parts.append(f"{self.transferred} copied")
        if self.destination_only:
            #  Said in the same breath and in the words that imply nothing.
            parts.append(f"{self.destination_only} only at the destination")
        return ", ".join(parts) or "nothing to do"


def begin(
    conn: sqlite3.Connection,
    *,
    destination_id: int,
    category: str,
    mode: str,
    planned_copies: int = 0,
    planned_updates: int = 0,
    destination_only: int = 0,
) -> int:
    """Open a run before anything is transferred.

    Written first so that a process killed mid-transfer leaves a row in
    `running` rather than leaving nothing — an absence cannot be told from a
    run that never started, and "we do not know what happened" is a thing this
    should be able to say.
    """
    cursor = conn.execute(
        """
        INSERT INTO backup_runs(destination_id, category, mode, state, started_at,
                                planned_copies, planned_updates, destination_only)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            destination_id,
            category,
            mode,
            RUNNING,
            utc_now(),
            planned_copies,
            planned_updates,
            destination_only,
        ),
    )
    return int(cursor.lastrowid)


def finish(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    succeeded: bool,
    transferred: int = 0,
    bytes_sent: int = 0,
    outcome: str = "",
    detail: str = "",
) -> None:
    """Close a run with what it actually did.

    `transferred` is recorded whether or not the run succeeded, because it is
    true either way: 73 files did reach the destination. What it is *not* is
    permission to call the destination current — nothing here can say that,
    because there is nothing here that stores it.
    """
    conn.execute(
        """
        UPDATE backup_runs
        SET state=?, finished_at=?, transferred=?, bytes_sent=?, outcome=?, detail=?
        WHERE id=?
        """,
        (
            SUCCEEDED if succeeded else FAILED,
            utc_now(),
            transferred,
            bytes_sent,
            outcome,
            detail[:500],
            run_id,
        ),
    )
    prune(conn)


def last_run(
    conn: sqlite3.Connection, destination_id: int, category: str = ""
) -> Run | None:
    """The most recent run, whatever became of it."""
    sql = "SELECT * FROM backup_runs WHERE destination_id=?"
    args: list[object] = [destination_id]
    if category:
        sql += " AND category=?"
        args.append(category)
    row = conn.execute(f"{sql} ORDER BY id DESC LIMIT 1", args).fetchone()  # noqa: S608
    return _run(row) if row is not None else None


def last_success(
    conn: sqlite3.Connection, destination_id: int, category: str = ""
) -> Run | None:
    """The most recent run that finished. Not the same question as the last one.

    "Last attempted" and "last succeeded" are both worth showing and are
    different facts — a destination attempted hourly and last successful in
    March is the exact state somebody needs to see, and one number cannot say
    it.
    """
    sql = "SELECT * FROM backup_runs WHERE destination_id=? AND state=?"
    args: list[object] = [destination_id, SUCCEEDED]
    if category:
        sql += " AND category=?"
        args.append(category)
    row = conn.execute(f"{sql} ORDER BY id DESC LIMIT 1", args).fetchone()  # noqa: S608
    return _run(row) if row is not None else None


def recent(conn: sqlite3.Connection, destination_id: int, limit: int = 20) -> list[Run]:
    return [
        _run(row)
        for row in conn.execute(
            "SELECT * FROM backup_runs WHERE destination_id=? ORDER BY id DESC LIMIT ?",
            (destination_id, max(1, min(limit, KEEP_RUNS))),
        )
    ]


def due(
    conn: sqlite3.Connection,
    destination_id: int,
    category: str,
    *,
    seconds: int = MIN_INTERVAL,
) -> bool:
    """Has it been long enough since this policy was last compared?

    From the last *attempt* rather than the last success. A destination that
    has been failing every hour should be retried every hour, not hammered — and
    a guard keyed on success would do exactly that, retrying continuously for as
    long as it kept failing.
    """
    row = conn.execute(
        "SELECT started_at FROM backup_runs WHERE destination_id=? AND category=?"
        " ORDER BY id DESC LIMIT 1",
        (destination_id, category),
    ).fetchone()
    if row is None:
        return True
    return _seconds_since(str(row["started_at"] or "")) >= seconds


def prune(conn: sqlite3.Connection, keep: int = KEEP_RUNS) -> int:
    """Keep the last `keep` runs per destination, and no more."""
    cursor = conn.execute(
        """
        DELETE FROM backup_runs WHERE id IN (
          SELECT id FROM (
            SELECT id, ROW_NUMBER() OVER (
              PARTITION BY destination_id ORDER BY id DESC
            ) AS position
            FROM backup_runs
          ) WHERE position > ?
        )
        """,
        (keep,),
    )
    return int(cursor.rowcount or 0)


def _run(row: sqlite3.Row) -> Run:
    return Run(
        id=int(row["id"]),
        destination_id=int(row["destination_id"]),
        category=str(row["category"]),
        mode=str(row["mode"]),
        state=str(row["state"]),
        started_at=str(row["started_at"] or ""),
        finished_at=str(row["finished_at"] or ""),
        planned_copies=int(row["planned_copies"] or 0),
        planned_updates=int(row["planned_updates"] or 0),
        destination_only=int(row["destination_only"] or 0),
        transferred=int(row["transferred"] or 0),
        bytes_sent=int(row["bytes_sent"] or 0),
        outcome=str(row["outcome"] or ""),
        detail=str(row["detail"] or ""),
    )


def _seconds_since(stamp: str) -> float:
    from datetime import UTC, datetime

    try:
        then = datetime.fromisoformat(stamp)
    except ValueError:
        return float("inf")
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    return (datetime.now(UTC) - then).total_seconds()
