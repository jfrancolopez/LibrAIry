"""What the search index and the library look like, before and after a rebuild.

Read-only, and deliberately made of query invariants rather than a file hash:
the database is live, the service writes to it continuously, and a whole-file
hash of a SQLite database under a running process answers a question nobody
asked. What matters is whether the same rows are still there afterwards.

Run it twice — once before `librairy index rebuild`, once after — and diff.

    docker exec librairy python3 /app/scripts/search_index_preflight.py
"""

from __future__ import annotations

import json
import sys

from librairy.config import Settings
from librairy.db import connect
from librairy.search_health import check_search_index, index_counts, recorded_health

# Queries that have to keep answering the same thing across the rebuild. A
# corrupt FTS index fails quietly by returning *fewer* rows, so counts that
# survive unchanged are the evidence that nothing was lost.
SAMPLES = ("matrix", "the", "a*", "mp3", "jpg")


def snapshot() -> dict[str, object]:
    settings = Settings()
    conn = connect(settings)
    counts = index_counts(conn)
    row = conn.execute("PRAGMA user_version").fetchone()
    # The FTS integrity check is an INSERT, so this is the one write here and
    # it is the same one Health performs. Everything else is a read.
    integrity = check_search_index(conn)
    quick = conn.execute("PRAGMA integrity_check").fetchone()[0]
    searchable = conn.execute(
        "SELECT COUNT(*) FROM items WHERE missing_since IS NULL"
    ).fetchone()[0]
    missing = conn.execute(
        "SELECT COUNT(*) FROM items WHERE missing_since IS NOT NULL"
    ).fetchone()[0]
    by_root = {
        r["root"]: r["n"]
        for r in conn.execute("SELECT root, COUNT(*) AS n FROM items GROUP BY root")
    }
    by_category = {
        str(r["category"]): r["n"]
        for r in conn.execute(
            "SELECT category, COUNT(*) AS n FROM search_fts GROUP BY category"
        )
    }
    queries = {}
    for term in SAMPLES:
        try:
            queries[term] = conn.execute(
                "SELECT COUNT(*) FROM search_fts WHERE search_fts MATCH ?", (term,)
            ).fetchone()[0]
        except Exception as exc:  # noqa: BLE001 - a corrupt index is the point
            queries[term] = f"error: {exc}"
    return {
        "schema_version": row[0],
        "fts_integrity": "ok" if integrity.ok else f"FAILED: {integrity.detail}",
        "recorded_health": "ok" if recorded_health(conn).ok else "damaged",
        "sqlite_integrity_check": quick,
        "index_counts": counts,  # current + missing_retained = total
        "items_searchable": searchable,
        "items_missing_since": missing,
        "items_by_root": by_root,
        "indexed_by_category": by_category,
        "queries": queries,
    }


if __name__ == "__main__":
    json.dump(snapshot(), sys.stdout, indent=2, sort_keys=True)
    print()
