"""What changed, day by day — the only thing in this program with a memory of size.

Forty-two tables and not one of them records a measurement over time. `history`
says what happened to a file and `audit_runs` says what a pass found, so some
questions can be answered backwards; "was the Review backlog smaller last
Tuesday" cannot be answered at all, because nothing ever wrote it down.

This is the smallest thing that fixes that, and it is deliberately not
monitoring infrastructure. One table, one row per metric per day, a stated
retention, and no daemon.

## Current state is not history

    the Library and its tables      what is true now. Authoritative, always
    metrics_daily                   what was true then. A record, never a source

Nothing operational reads this table. The Dashboard's top band — what needs
you — and its middle band — what LibrAIry is doing — go on being live
aggregates over indexed columns, because a rollup that became the source of
truth for current state would be a cache that can be wrong about the present.
This answers only the bottom band: *how is my library changing*.

## Every metric is a recomputation, never an increment

The one design decision everything else follows from. A counter that is
incremented as things happen can double-count on a retry, drift after a crash,
and can never be checked against anything. Every measure here is instead a
query over data that is already authoritative:

    a gauge     is measured now, and stored with the moment it was taken
    a count     is derived from the day's own rows in `history`,
                `quarantine_entries` or `proposals`

So re-running a day is *the same answer again*, and the primary key makes that
a replacement rather than a second row. Idempotence is a property of the
schema and the arithmetic, not of anybody remembering to check.

It has one honest asymmetry, and it decides the repair story:

    counts      recomputable for any past day whose source rows still exist
    gauges      measurable only *now*. A snapshot nobody took is gone

So `backfill` can give an upgrade months of commit history from `history`, and
cannot give it last month's library size. That is not a limitation to work
around; it is what a snapshot is.

## Days are UTC days

Chosen rather than inherited. Every timestamp in this program is `utc_now()`,
and letting a rollup use the container's local date would put a metric's day
boundary in a different place from the timestamps it is derived from — so a
file filed at 23:30 local would land in one day's chart and its `history` row
would say another. A day here is a UTC day, the Dashboard says so, and nothing
has to be configured.

## What it costs

At a million library items the two expensive measures are the ones that have
to read the `items` table: total files and bytes (**157 ms**) and the
distribution across top-level folders (**580 ms**). Everything else is under a
millisecond on an index. Measured 2026-09-04, `docs/performance.md`.

Which is why this runs at most once an hour and never on a page render. The
Dashboard reads this table — a few dozen rows a day, keyed by day — and never
the tables it was measured from.

See `docs/ROADMAP.md` M3-01.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from librairy.live import LIVE
from librairy.planner import utc_now

#  What a number *means*, and the reason the two are never mixed. A gauge is
#  the state at a moment: averaging two of them is meaningful, adding them is
#  not. A count is what happened during a period: adding them is meaningful,
#  averaging them answers a different question. A chart that got this wrong
#  would be confidently wrong, which is the worst kind.
GAUGE = "gauge"
COUNT = "count"

#  How long a day's rows are kept. Two years of roughly thirty rows a day is
#  about 22,000 rows — well under a megabyte — and two years is long enough to
#  see a season repeat. Stated here rather than left to grow, because "it is
#  only kilobytes" is how every unbounded table starts.
KEEP_DAYS = 730

#  How often today's snapshot is retaken. The gauges cost about seven tenths of
#  a second at a million items, so hourly is 0.02% of a worker's time and gives
#  a chart a point that is never more than an hour stale.
REFRESH_SECONDS = 3600

#  How much history one read may ask for. A Dashboard query is bounded by the
#  number of days requested and never by the size of the library — that is the
#  whole contract this table exists to provide.
MAX_DAYS = 730
DEFAULT_DAYS = 90

_LAST_ROLLUP = "metrics.rolled_up_at"


@dataclass(frozen=True)
class Measure:
    """One number, what it means, and the question it exists to answer."""

    name: str
    kind: str
    #  Required, and not documentation for its own sake: a metric with no
    #  Dashboard question behind it is a metric nobody will ever read and
    #  everybody will have to keep working. See M3-01's "do not".
    question: str
    sql: str


#  Measured now. Each is the same aggregate the live page already runs, which
#  is what keeps the recorded number and the current one from disagreeing about
#  what they mean.
GAUGES = (
    Measure(
        "library.files",
        GAUGE,
        "how fast is my library growing",
        f"SELECT COUNT(*) FROM items WHERE root='library' AND {LIVE}",
    ),
    Measure(
        "library.bytes",
        GAUGE,
        "how fast is my storage filling",
        f"SELECT COALESCE(SUM(size), 0) FROM items WHERE root='library' AND {LIVE}",
    ),
    Measure(
        "inbox.files",
        GAUGE,
        "is what arrives getting dealt with, or piling up",
        f"SELECT COUNT(*) FROM items WHERE root='inbox' AND {LIVE}",
    ),
    Measure(
        "review.waiting",
        GAUGE,
        "is the decision backlog shrinking",
        "SELECT COUNT(*) FROM proposals WHERE status='proposed'",
    ),
    Measure(
        "review.ready",
        GAUGE,
        "am I approving things and then not committing them",
        "SELECT COUNT(*) FROM proposals WHERE status='approved'",
    ),
    Measure(
        "review.holding",
        GAUGE,
        "is the AI backlog growing while a provider is down",
        "SELECT COUNT(*) FROM processing_waits",
    ),
    Measure(
        "findings.open",
        GAUGE,
        "are library findings being worked through or accumulating",
        "SELECT COUNT(*) FROM audit_findings WHERE status='open'",
    ),
    Measure(
        "quarantine.files",
        GAUGE,
        "how much am I holding on to that I have not decided about",
        f"""SELECT COUNT(*) FROM quarantine_entries qe
            JOIN items i ON i.id = qe.item_id AND {LIVE}
            WHERE qe.restored_at IS NULL""",
    ),
    Measure(
        "quarantine.bytes",
        GAUGE,
        "what is that costing me in storage",
        f"""SELECT COALESCE(SUM(i.size), 0) FROM quarantine_entries qe
            JOIN items i ON i.id = qe.item_id AND {LIVE}
            WHERE qe.restored_at IS NULL""",
    ),
)

#  Derived from the day's own rows. `?` twice: the start of the day and the
#  start of the next one, as strings — every timestamp in this program is
#  `utc_now()`, so a prefix comparison is exact and needs no date function
#  standing between the value and an index.
COUNTS = (
    Measure(
        "filed.files",
        COUNT,
        "how much did I actually file this week",
        """SELECT COUNT(*) FROM history
           WHERE action='move' AND dest_root='library' AND outcome='ok'
             AND ts >= ? AND ts < ?""",
    ),
    Measure(
        "setaside.duplicates",
        COUNT,
        "am I making progress on duplicates",
        """SELECT COUNT(*) FROM quarantine_entries
           WHERE reason IN ('exact_duplicate', 'similar_media')
             AND quarantined_at >= ? AND quarantined_at < ?""",
    ),
    Measure(
        "analysed.files",
        COUNT,
        "how much is LibrAIry getting through",
        "SELECT COUNT(*) FROM proposals WHERE created_at >= ? AND created_at < ?",
    ),
)

MEASURES = GAUGES + COUNTS
BY_NAME = {measure.name: measure for measure in MEASURES}

#  The category distribution, which is a small map rather than one number and
#  so is written as one row per folder. The library's *top-level folders* and
#  not `proposals.category`: a folder is what is on the disk and what Browse
#  shows, and an adopted library's files mostly have no proposal at all.
TOP_LEVEL = "library.top"
_DISTRIBUTION = f"""
SELECT substr(relpath, 1, instr(relpath, '/') - 1) AS folder,
       COUNT(*) AS files, COALESCE(SUM(size), 0) AS bytes
FROM items
WHERE root='library' AND {LIVE} AND instr(relpath, '/') > 0
GROUP BY folder
"""


def today() -> str:
    """The current UTC day. The one place a day boundary is decided."""
    return utc_now()[:10]


def bounds(day: str) -> tuple[str, str]:
    """The half-open range a day's counts are taken over: `[day, next)`.

    String bounds rather than `date(ts) = ?`, so the comparison is on the
    stored value itself. A function around a column is a function applied to
    every row, and it is also the shape that quietly prevents an index from
    ever being used if one is added later.
    """
    from datetime import date, timedelta

    parts = [int(part) for part in day.split("-")]
    return day, (date(*parts) + timedelta(days=1)).isoformat()


def rollup(conn: sqlite3.Connection, day: str = "") -> int:
    """Measure one day and write it. Safe to run again, always.

    Gauges are read as they are *now*, so calling this for a past day measures
    the present and stamps it with that day — which is why nothing calls it
    that way except `backfill`, and `backfill` only asks for counts.
    """
    day = day or today()
    start, end = bounds(day)
    taken = utc_now()
    written = 0
    for measure in GAUGES:
        written += _write(conn, day, measure, _value(conn, measure.sql), taken)
    for measure in COUNTS:
        written += _write(
            conn, day, measure, _value(conn, measure.sql, (start, end)), taken
        )
    written += _distribution(conn, day, taken)
    conn.execute(
        "INSERT INTO worker_state(key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (_LAST_ROLLUP, taken),
    )
    prune(conn)
    return written


def backfill(conn: sqlite3.Connection, days: int = DEFAULT_DAYS) -> int:
    """Recover the *counts* for days before anything was recording.

    An upgrade arrives with months of `history` behind it, and every count here
    is derived from rows that are still there — so a chart of what has been
    filed can start full instead of empty. Cheap, and idempotent for the same
    reason a rollup is: it recomputes rather than adds.

    It writes no gauges. Nobody measured the library's size last March and
    inventing a number for it would be the one thing this table must never
    contain.
    """
    from datetime import date, timedelta

    parts = [int(part) for part in today().split("-")]
    now = date(*parts)
    taken = utc_now()
    written = 0
    for back in range(1, max(0, min(days, MAX_DAYS)) + 1):
        day = (now - timedelta(days=back)).isoformat()
        start, end = bounds(day)
        for measure in COUNTS:
            value = _value(conn, measure.sql, (start, end))
            if value:
                #  Only days that had something. A year of explicit noughts is
                #  a year of rows saying nothing happened, which the absence of
                #  a row already says.
                written += _write(conn, day, measure, value, taken)
    return written


def due(conn: sqlite3.Connection, *, seconds: int = REFRESH_SECONDS) -> bool:
    """Has today's snapshot gone stale enough to retake?

    From the recorded moment rather than from a schedule, so a worker that was
    stopped for a week takes one snapshot when it comes back rather than
    catching up on seven it cannot measure.
    """
    row = conn.execute(
        "SELECT value FROM worker_state WHERE key=?", (_LAST_ROLLUP,)
    ).fetchone()
    if row is None or not str(row["value"] or ""):
        return True
    return _seconds_since(str(row["value"])) >= seconds


def series(
    conn: sqlite3.Connection, names: list[str], days: int = DEFAULT_DAYS
) -> dict[str, list[dict[str, object]]]:
    """The last `days` of each named metric, oldest first.

    Bounded by the number of days asked for and by nothing else. This is the
    contract the whole table exists to provide: a Dashboard drawing ninety
    points costs the same on a library of four files and a library of four
    million, because it reads a few hundred rows keyed by day.
    """
    if not names:
        return {}
    days = max(1, min(days, MAX_DAYS))
    since = _days_ago(days)
    placeholders = ",".join("?" for _ in names)
    found: dict[str, list[dict[str, object]]] = {name: [] for name in names}
    for row in conn.execute(
        f"SELECT day, metric, kind, value, taken_at FROM metrics_daily"  # noqa: S608
        f" WHERE day >= ? AND metric IN ({placeholders})"
        " ORDER BY day",
        (since, *names),
    ):
        found[str(row["metric"])].append(
            {
                "day": str(row["day"]),
                "kind": str(row["kind"]),
                "value": int(row["value"]),
                "taken_at": str(row["taken_at"]),
            }
        )
    return found


def distribution(
    conn: sqlite3.Connection, day: str = "", field: str = "files"
) -> list[dict[str, object]]:
    """One day's category distribution, largest first. Reads the table, not the library."""
    day = day or _latest_day(conn)
    if not day:
        return []
    prefix = f"{TOP_LEVEL}."
    suffix = f".{field}"
    return sorted(
        (
            {
                "folder": str(row["metric"])[len(prefix) : -len(suffix)],
                "value": int(row["value"]),
            }
            for row in conn.execute(
                "SELECT metric, value FROM metrics_daily"
                " WHERE day = ? AND metric LIKE ? ESCAPE '\\'",
                (day, f"{prefix}%{suffix}"),
            )
        ),
        key=lambda entry: (-int(entry["value"]), str(entry["folder"])),
    )


def latest(conn: sqlite3.Connection, name: str) -> dict[str, object] | None:
    """The most recent recorded value of one metric, or None."""
    row = conn.execute(
        "SELECT day, kind, value, taken_at FROM metrics_daily WHERE metric=?"
        " ORDER BY day DESC LIMIT 1",
        (name,),
    ).fetchone()
    if row is None:
        return None
    return {
        "day": str(row["day"]),
        "kind": str(row["kind"]),
        "value": int(row["value"]),
        "taken_at": str(row["taken_at"]),
    }


def prune(conn: sqlite3.Connection, keep: int = KEEP_DAYS) -> int:
    """Drop days older than the stated retention. Returns how many rows went."""
    cursor = conn.execute(
        "DELETE FROM metrics_daily WHERE day < ?", (_days_ago(keep),)
    )
    return int(cursor.rowcount or 0)


def forget(conn: sqlite3.Connection, day: str = "") -> int:
    """Delete a day, or all of it. The whole repair story.

    Losing this table degrades trends and breaks nothing: no decision, no
    proposal, no plan and no file depends on a row in it. The counts come back
    on the next `backfill`; the gauges do not, because nobody can measure last
    Tuesday today.
    """
    if day:
        cursor = conn.execute("DELETE FROM metrics_daily WHERE day=?", (day,))
    else:
        cursor = conn.execute("DELETE FROM metrics_daily")
    return int(cursor.rowcount or 0)


# --- internals ---------------------------------------------------------------------


def _write(
    conn: sqlite3.Connection, day: str, measure: Measure, value: int, taken: str
) -> int:
    """One row, replacing the day's previous answer for this metric.

    `ON CONFLICT` on the primary key rather than a check-then-insert: two
    rollups for one day cannot produce two rows, and that is a property of the
    schema rather than of this function being careful.
    """
    conn.execute(
        """
        INSERT INTO metrics_daily(day, metric, kind, value, taken_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(day, metric) DO UPDATE SET
          kind=excluded.kind, value=excluded.value, taken_at=excluded.taken_at
        """,
        (day, measure.name, measure.kind, int(value), taken),
    )
    return 1


def _distribution(conn: sqlite3.Connection, day: str, taken: str) -> int:
    written = 0
    for row in conn.execute(_DISTRIBUTION):
        folder = str(row["folder"])
        if not folder:
            continue
        for field, value in (("files", row["files"]), ("bytes", row["bytes"])):
            written += _write(
                conn,
                day,
                Measure(f"{TOP_LEVEL}.{folder}.{field}", GAUGE, "", ""),
                int(value or 0),
                taken,
            )
    return written


def _value(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> int:
    row = conn.execute(sql, args).fetchone()
    return int(row[0] or 0) if row is not None else 0


def _latest_day(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT MAX(day) FROM metrics_daily").fetchone()
    return str(row[0] or "") if row is not None else ""


def _days_ago(days: int) -> str:
    from datetime import date, timedelta

    parts = [int(part) for part in today().split("-")]
    return (date(*parts) - timedelta(days=days - 1)).isoformat()


def _seconds_since(stamp: str) -> float:
    from datetime import UTC, datetime

    try:
        then = datetime.fromisoformat(stamp)
    except ValueError:
        return float("inf")
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    return (datetime.now(UTC) - then).total_seconds()
