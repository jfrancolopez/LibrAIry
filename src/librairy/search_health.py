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


def index_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """How many rows the index holds against how many it should.

    A count mismatch is a weaker signal than the integrity check — an item with
    nothing worth indexing legitimately has no row — so this is reported as
    context beside the check rather than used as a verdict on its own.
    """
    indexed = conn.execute("SELECT COUNT(*) FROM search_fts").fetchone()[0]
    items = conn.execute(
        "SELECT COUNT(*) FROM items WHERE missing_since IS NULL"
    ).fetchone()[0]
    return {"indexed": int(indexed), "items": int(items)}
