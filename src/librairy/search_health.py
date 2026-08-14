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


def check_search_index(conn: sqlite3.Connection) -> IndexHealth:
    """Ask FTS5 to verify its own index.

    `integrity-check` is FTS5's own command and reads only the index's tables,
    so it does not walk the library and does not depend on the item rows being
    correct. Cheap enough to run on a page render; the whole point is that the
    warning appears where the incomplete results do.

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
