"""Is the search index still able to answer?

FTS5 keeps an inverted index in ordinary SQLite tables. It can be damaged by
the things that damage any SQLite file — a WAL truncated by a crash, a database
copied while it was being written, a filesystem that lied about a flush — and
when it is, the failure is quiet in the worst way: `SELECT ... MATCH` returns
*fewer* rows rather than an error. A search that comes back empty looks like a
search that found nothing.

The live installation is in exactly that state. `PRAGMA integrity_check` on a
consistent snapshot reports `malformed inverted index for FTS5 table
main.search_fts` at a point before anything in this pass touched it.

Two rules shape what this module does and does not do:

* **Report, do not repair.** Rebuilding is a write over the whole index, and a
  page load or a startup path is not where that decision belongs. It is one
  command, run by a person: `librairy index rebuild`.
* **Never block Browse.** Browse walks the filesystem and is unaffected by any
  of this. A damaged index is a reason to warn on Search, not to hide the
  library.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

# What a person needs to know, in the order they need it: that results may be
# short, and what to do about it.
WARNING = "Search index needs rebuild. Results may be incomplete."
REMEDY = "Run `librairy index rebuild`, or use the rebuild button on Health."


@dataclass(frozen=True)
class IndexHealth:
    """Whether the FTS tables can be read, and what to say if not."""

    ok: bool
    detail: str = ""

    @property
    def warning(self) -> str:
        return "" if self.ok else WARNING


RECORDED_KEY = "search_index_state"


def check_search_index(conn: sqlite3.Connection) -> IndexHealth:
    """Ask FTS5 to verify its own index.

    **This is a write.** FTS5 expresses `integrity-check` as an INSERT into the
    table's command column, so SQLite opens a write transaction for it — which
    means it must not be called while drawing a page. LibrAIry holds a hard
    rule that rendering never writes, and calling this from Search broke it
    (`test_web_suite.py::test_drawing_a_page_never_writes_to_the_database`
    caught it immediately). `recorded_health` is what the render path reads.

    Any failure at all is reported rather than classified. A caller wants to
    know "can I trust these results", and every way of answering no has the
    same remedy.
    """
    try:
        conn.execute(
            "INSERT INTO search_fts(search_fts, rank) VALUES('integrity-check', 0)"
        )
    except sqlite3.DatabaseError as exc:
        return IndexHealth(False, str(exc))
    except sqlite3.Error as exc:  # pragma: no cover - defensive
        return IndexHealth(False, str(exc))
    return IndexHealth(True)


def record_health(conn: sqlite3.Connection, health: IndexHealth) -> None:
    """Remember the verdict, so a page can show it without checking again."""
    conn.execute(
        "INSERT INTO worker_state(key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (RECORDED_KEY, "ok" if health.ok else "damaged"),
    )


def recorded_health(conn: sqlite3.Connection) -> IndexHealth:
    """The last verdict, read only.

    Search shows what was last found rather than checking on every keystroke:
    the check is a write, and a search box that writes to the database on each
    request is a search box that fights the worker for the writer lock.

    Unknown reads as healthy on purpose. A warning nobody has evidence for is
    a warning people learn to dismiss, and the check runs on Health, on
    `librairy db check`, and after every rebuild.
    """
    row = conn.execute(
        "SELECT value FROM worker_state WHERE key=?", (RECORDED_KEY,)
    ).fetchone()
    if row is None or row["value"] == "ok":
        return IndexHealth(True)
    return IndexHealth(False, "recorded by the last integrity check")


#  Every count on this page is one of three primitives, and two of the three
#  are the expensive kind: an FTS5 table has no row count to look up, so
#  counting it reads it. Measured at a million — 224 ms for the index, 321 ms
#  for the join, 73 ms for the items — which is why they are asked once and
#  arithmetic does the rest.
#
#  `current` is derived rather than counted. Every index row belongs to an item
#  that is either present or missing, so `total - missing` is the same number
#  as the join that asks for it directly, and the join costs 325 ms. Deriving
#  it is not an approximation; it is the same fact, read once.
@dataclass(frozen=True)
class IndexCounts:
    """What the index holds, counted once."""

    total: int
    missing_retained: int
    live_items: int

    @property
    def current(self) -> int:
        """Index rows belonging to a file that is currently on disk."""
        return max(0, self.total - self.missing_retained)

    @property
    def unindexed(self) -> int:
        """A present file with no index row — the only one that means damage."""
        return max(0, self.live_items - self.current)


def counted(conn: sqlite3.Connection) -> IndexCounts:
    """The three primitives, in three statements.

    Callers that need more than one number ask for this once and read the rest
    off it. Health used to reach the expensive join twice and the item count
    twice on one render, through three modules that could not see each other.
    """
    total = int(conn.execute("SELECT COUNT(*) FROM search_fts").fetchone()[0])
    missing = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM search_fts s JOIN items i ON i.id = s.item_id
            WHERE i.missing_since IS NOT NULL
            """
        ).fetchone()[0]
    )
    return IndexCounts(total=total, missing_retained=missing, live_items=live_items(conn))


def index_counts(
    conn: sqlite3.Connection, counts: IndexCounts | None = None
) -> dict[str, int]:
    """What the index holds, split so the numbers add up on sight.

    This used to return `indexed` beside `items`, where `indexed` counted every
    row in the index and `items` counted only the ones whose file is still on
    disk. On the author's library that read:

        indexed 243 · items 235

    which looks like eight lost records and is nothing of the kind: a file that
    has gone missing keeps its index row on purpose, so a share that comes back
    online is searchable immediately rather than after a rescan. The eight are
    deliberate, and the presentation made them look like damage.

    So the split is explicit now, and `unindexed` is the only one of these that
    is ever a problem worth reporting.
    """
    found = counts or counted(conn)
    return {
        "current": found.current,
        "missing_retained": found.missing_retained,
        "total": found.total,
        # A present file with no index row. Unlike the others, this one means
        # something is actually wrong.
        "unindexed": found.unindexed,
    }


def live_items(conn: sqlite3.Connection) -> int:
    """Files LibrAIry currently believes are on disk."""
    return int(
        conn.execute("SELECT COUNT(*) FROM items WHERE missing_since IS NULL").fetchone()[0]
    )


def indexed_live(conn: sqlite3.Connection) -> int:
    """Index rows belonging to a file that is currently on disk."""
    return int(
        conn.execute(
            """
            SELECT COUNT(*) FROM search_fts s JOIN items i ON i.id = s.item_id
            WHERE i.missing_since IS NULL
            """
        ).fetchone()[0]
    )


def unindexed(conn: sqlite3.Connection, counts: IndexCounts | None = None) -> int:
    """Files on disk with no index entry — the only count here that is a problem.

    Its own function because Health asks for exactly this and nothing else. It
    used to read the whole of `index_counts` to get at one number, which meant
    the page ran the same four counts twice: once for the attention line and
    once for the panel below it.

    **Subtraction, not `NOT EXISTS`.** The obvious spelling of this question —
    every item with no matching `search_fts` row — is quadratic here and looks
    perfectly ordinary:

        SELECT COUNT(*) FROM items i WHERE i.missing_since IS NULL
          AND NOT EXISTS (SELECT 1 FROM search_fts s WHERE s.item_id = i.id)

    `search_fts` is an FTS5 table whose `item_id` is declared `UNINDEXED`, so
    there is nothing for that subquery to seek on and the planner says exactly
    what it does: `SCAN i`, then a `CORRELATED SCALAR SUBQUERY` doing `SCAN s
    VIRTUAL TABLE` — a full scan of the index for every row of `items`. 3.8
    seconds at five thousand files; unusable at a hundred thousand, which is
    where M1-01 found it, on the one page whose job is to say something is
    wrong.

    Two counts and a subtraction answer the same question, because a row is
    inserted into `search_fts` with `rowid = item_id`: there is at most one
    index row per item, so the indexed-and-live count can never exceed the live
    count. `max` guards the arithmetic anyway rather than letting a negative
    number reach a page — if that ever fires the index has duplicate rows, and
    reporting "0 unindexed" is the honest answer to a question about *missing*
    rows.
    """
    return (counts or counted(conn)).unindexed
