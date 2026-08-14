from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from librairy.config import Settings

PAGE_SIZE = 50
TEXT_FIELDS = ("name", "clean_name", "tags", "artist", "album", "title", "show", "genre", "event")
CATEGORIES = {"music", "movies", "shows", "photos", "documents", "books", "projects", "misc"}
# Kept in step with web/thumbs.py: only these can answer a thumbnail request,
# and asking for one on anything else renders a broken-image icon.
THUMBNAILABLE = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".heic", ".avif", ".webp",
    ".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v",
}
FTS_OPERATORS = {"AND", "OR", "NOT", "NEAR"}

# Search answers "which files that are here match this?", so a row whose file a
# scan looked for and did not find is not a result. Every other surface already
# knew this — Review, Commit, plan, dedup, duplicates, the catalog probe,
# content extraction, backup, the indexer, companions — and search.py was the
# one that did not mention the column at all. On the author's library that was
# five files deleted during a drill in August coming back beside one real
# result, rendered identically: same thumbnail slot, same size, same category.
#
# The row is kept, not deleted. It still carries the proposals, the approval
# and the rejection made about that file, History can still reach it, and if
# the file comes back the next scan clears the flag and it is searchable again
# with no rebuild. The record was never wrong; only the query was.
LIVE_ONLY = "i.missing_since IS NULL"


@dataclass(frozen=True)
class SearchFilters:
    category: str | None = None
    root: str | None = None
    year: int | None = None
    genre: str | None = None
    group_kind: str | None = None
    content: bool = False
    page: int = 1


# Searching every root at once mixed three unrelated things — files you have
# not filed yet, your actual library, and things you quarantined — and the
# unfiled ones dominate, because that is where the volume is. "Find my stuff"
# means the library; the inbox is Review's job and quarantine has its own page.
DEFAULT_SEARCH_ROOT = "library"
# The value the "Everywhere" option posts. Empty string would be
# indistinguishable from "the field was not submitted", which is what made the
# default ambiguous in the first place.
ALL_ROOTS = "all"
SEARCH_SCOPES = (
    (DEFAULT_SEARCH_ROOT, "My library"),
    ("inbox", "Inbox — not filed yet"),
    ("quarantine", "Quarantine"),
    (ALL_ROOTS, "Everywhere"),
)


def scope_to_root(scope: str | None) -> str | None:
    """Form value to a root filter. None means every root."""
    if scope == ALL_ROOTS:
        return None
    return scope or DEFAULT_SEARCH_ROOT


def sync_search_item(conn: sqlite3.Connection, item_id: int) -> None:
    row = conn.execute(
        """
        SELECT i.*, p.clean_name, p.category, p.evidence, g.kind AS group_kind,
               v.caption AS vision_caption, v.subjects AS vision_subjects,
               v.tags AS vision_tags, v.visible_text AS vision_text
        FROM items i
        LEFT JOIN proposals p ON p.item_id = i.id AND p.status != 'superseded'
        LEFT JOIN groups g ON g.id = p.group_id
        LEFT JOIN vision_results v ON v.item_id = i.id
        WHERE i.id=?
        """,
        (item_id,),
    ).fetchone()
    conn.execute("DELETE FROM search_fts WHERE rowid=?", (item_id,))
    if row is None:
        return
    fields = _fields_from_row(row)
    conn.execute(
        f"""
        INSERT INTO search_fts(rowid, {", ".join(TEXT_FIELDS)}, category, root, item_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            item_id,
            fields["name"],
            fields["clean_name"],
            fields["tags"],
            fields["artist"],
            fields["album"],
            fields["title"],
            fields["show"],
            fields["genre"],
            fields["event"],
            fields["category"],
            row["root"],
            item_id,
        ),
    )


# The index's own definition, in one place, so a rebuild recreates exactly
# what migration 008 created. Kept byte-identical to it on purpose: a rebuild
# that produced a subtly different tokenizer would change what Search matches.
SEARCH_FTS_DDL = """
CREATE VIRTUAL TABLE search_fts USING fts5(
  name,
  clean_name,
  tags,
  artist,
  album,
  title,
  show,
  genre,
  event,
  category UNINDEXED,
  root UNINDEXED,
  item_id UNINDEXED,
  tokenize='unicode61 remove_diacritics 2'
);
"""


def rebuild_search_index(conn: sqlite3.Connection) -> int:
    """Throw the index away and build it again from the item rows.

    It drops the table rather than emptying it, and that is the whole point.
    `DELETE FROM search_fts` has to *read* the inverted index in order to
    remove its rows — so on a damaged index the repair raised "database disk
    image is malformed" and stopped, which meant the one documented remedy did
    not work in exactly the situation it existed for. Measured on a copy of the
    live database, which is in that state.

    Dropping loses nothing: every column here is derived from `items` and
    `proposals`, which is what makes the index rebuildable at all.
    """
    try:
        conn.execute("DROP TABLE IF EXISTS search_fts")
    except sqlite3.DatabaseError:
        # Even the drop can fail on a badly damaged index. FTS5 keeps its
        # storage in ordinary shadow tables, and removing those by hand leaves
        # a plain table nobody reads, which the CREATE below replaces.
        for suffix in ("data", "idx", "content", "docsize", "config"):
            conn.execute(f"DROP TABLE IF EXISTS search_fts_{suffix}")
        conn.execute("DELETE FROM sqlite_master WHERE name='search_fts'")
    conn.executescript(SEARCH_FTS_DDL)
    item_ids = [row["id"] for row in conn.execute("SELECT id FROM items ORDER BY id")]
    for item_id in item_ids:
        sync_search_item(conn, item_id)
    # The index is known-good now, and the pages that warn about it read a
    # recorded verdict rather than checking for themselves.
    from librairy.search_health import check_search_index, record_health

    record_health(conn, check_search_index(conn))
    return len(item_ids)


def search_items(
    conn: sqlite3.Connection,
    query: str,
    filters: SearchFilters | None = None,
) -> list[dict[str, object]]:
    filters = filters or SearchFilters()
    rows = _name_search_items(conn, query, filters)
    if filters.content and query:
        seen = {int(row["item_id"]) for row in rows}
        for row in _content_search_items(conn, query, filters):
            if int(row["item_id"]) in seen:
                continue
            rows.append(row)
            seen.add(int(row["item_id"]))
    return rows[:PAGE_SIZE]


def _name_search_items(
    conn: sqlite3.Connection,
    query: str,
    filters: SearchFilters,
) -> list[dict[str, object]]:
    where, params = _where(filters)
    match = _match_query(query)
    if match:
        where = f"search_fts MATCH ? AND {where}"
        params.insert(0, match)
        order = "bm25(search_fts)"
    else:
        order = "item_id"
    params.extend([PAGE_SIZE, (filters.page - 1) * PAGE_SIZE])
    return [
        dict(row) | {"source": "name"}
        for row in conn.execute(
            f"""
                SELECT search_fts.item_id, search_fts.root, search_fts.category,
                   i.relpath AS relpath,
                   highlight(search_fts, 0, '<mark>', '</mark>') AS name,
                   highlight(search_fts, 1, '<mark>', '</mark>') AS clean_name,
                   snippet(search_fts, 2, '<mark>', '</mark>', '...', 12) AS snippet
            FROM search_fts
            JOIN items i ON i.id = search_fts.item_id
            WHERE {where}
            ORDER BY {order}
            LIMIT ? OFFSET ?
            """,
            params,
        )
    ]


def _content_search_items(
    conn: sqlite3.Connection,
    query: str,
    filters: SearchFilters,
) -> list[dict[str, object]]:
    match = _match_query(query)
    if not match:
        return []
    clauses = ["content_fts MATCH ?", LIVE_ONLY]
    params: list[object] = [match]
    if filters.category:
        clauses.append("COALESCE(search_fts.category, '')=?")
        params.append(filters.category)
    if filters.root:
        clauses.append("i.root=?")
        params.append(filters.root)
    params.extend([PAGE_SIZE, (filters.page - 1) * PAGE_SIZE])
    return [
        dict(row) | {"source": "content", "name": row["relpath"], "clean_name": row["relpath"]}
        for row in conn.execute(
            f"""
            SELECT content_fts.item_id, i.root, COALESCE(search_fts.category, 'misc') AS category,
                   i.relpath AS relpath,
                   snippet(content_fts, 0, '<mark>', '</mark>', '...', 16) AS snippet
            FROM content_fts
            JOIN items i ON i.id = content_fts.item_id
            LEFT JOIN search_fts ON search_fts.item_id = i.id
            WHERE {" AND ".join(clauses)}
            ORDER BY bm25(content_fts)
            LIMIT ? OFFSET ?
            """,
            params,
        )
    ]


def _result_details(conn: sqlite3.Connection, row: dict[str, object]) -> dict[str, object]:
    """The facts that decide "is this the file I meant?" without opening it.

    A result used to be a name, a path and a category. Everything here is one
    row from tables the search already joined against, so enriching costs a
    lookup per result rather than a second pass over the index.
    """
    item = conn.execute(
        "SELECT size, state FROM items WHERE id=?", (row["item_id"],)
    ).fetchone()
    proposal = conn.execute(
        """
        SELECT confidence, dest_root, dest_relpath, status
        FROM proposals WHERE item_id=? AND status != 'superseded'
        ORDER BY id DESC LIMIT 1
        """,
        (row["item_id"],),
    ).fetchone()
    name = PurePosixPath(str(row["relpath"])).name
    suffix = PurePosixPath(name).suffix.lower()
    return {
        "file_name": name,
        "size": human_size(item["size"] if item else None),
        "extension": suffix.lstrip(".") or "no extension",
        "state": item["state"] if item else "",
        "has_thumbnail": suffix in THUMBNAILABLE,
        "confidence": proposal["confidence"] if proposal else None,
        "proposal_status": proposal["status"] if proposal else None,
        # Suppressed once the file is already there — a committed item would
        # otherwise show "goes to" pointing at the path printed just above it.
        "destination": (
            f"{proposal['dest_root']}/{proposal['dest_relpath']}"
            if proposal
            and proposal["dest_relpath"]
            and (proposal["dest_root"], proposal["dest_relpath"]) != (row["root"], row["relpath"])
            else ""
        ),
    }


def human_size(size: int | None) -> str:
    if not size or size < 0:
        return ""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return ""


def search_data(
    conn: sqlite3.Connection,
    settings: Settings,
    query: str,
    filters: SearchFilters | None = None,
) -> dict[str, object]:
    filters = filters or SearchFilters()
    rows = search_items(conn, query, filters)
    for row in rows:
        row["host_path"] = host_path(settings, row["root"], row["relpath"])
        row["history_count"] = conn.execute(
            "SELECT COUNT(*) FROM history WHERE dest_root=? AND dest_relpath=?",
            (row["root"], row["relpath"]),
        ).fetchone()[0]
        row.update(_result_details(conn, row))
    from librairy.search_health import recorded_health

    # Shown here because this is where incomplete results appear. Read, never
    # checked: FTS5's integrity-check is an INSERT, and drawing a page must not
    # write. Health and `librairy db check` run the real check and record it.
    health = recorded_health(conn)
    return {
        "query": query,
        "filters": filters,
        "results": rows,
        "page_size": PAGE_SIZE,
        "has_prev": filters.page > 1,
        "has_next": len(rows) == PAGE_SIZE,
        "index_ok": health.ok,
        "index_warning": health.warning,
    }


def host_path(settings: Settings, root: str, relpath: str) -> str:
    base = {
        "inbox": settings.host_inbox_dir,
        "library": settings.host_library_dir,
        "quarantine": settings.host_quarantine_dir,
    }.get(root, settings.host_library_dir)
    return (base / relpath).as_posix()


def search_checksum(conn: sqlite3.Connection, query: str = "") -> tuple[int, tuple[int, ...]]:
    rows = search_items(conn, query, SearchFilters(page=1))
    return len(rows), tuple(int(row["item_id"]) for row in rows)


def perf_search(conn: sqlite3.Connection, query: str) -> float:
    started = time.perf_counter()
    search_items(conn, query)
    return time.perf_counter() - started


def _fields_from_row(row: sqlite3.Row) -> dict[str, str]:
    evidence = _evidence(row["evidence"])
    name = row["relpath"].replace("/", " ")
    tags = " ".join(entry["detail"] for entry in evidence if entry.get("source") == "hashtag")
    # A caption, the things in the picture, and the text read out of it, in the
    # column that already holds "other words about this file". Searching "wifi"
    # and getting the screenshot of the Wi-Fi settings is the whole reason to
    # have looked at it. It costs one LEFT JOIN and needs no separate index.
    tags = " ".join(part for part in (tags, _vision_words(row)) if part)
    by_field = {str(entry.get("field")): str(entry.get("detail")) for entry in evidence}
    return {
        "name": name,
        "clean_name": row["clean_name"] or name,
        "tags": tags,
        "artist": by_field.get("artist", ""),
        "album": by_field.get("album", ""),
        "title": by_field.get("title", ""),
        "show": by_field.get("show", ""),
        "genre": by_field.get("genre", ""),
        "event": by_field.get("event", ""),
        "category": row["category"] or _category_from_path(row["relpath"]),
    }


def _vision_words(row: sqlite3.Row) -> str:
    """Everything a model said about an image, flattened for the index.

    Tolerant of a row that has no vision columns at all: `sync_search_item` is
    not the only shape of row this helper has ever been handed.
    """
    keys = row.keys() if hasattr(row, "keys") else ()
    if "vision_caption" not in keys:
        return ""
    parts = [row["vision_caption"] or "", row["vision_text"] or ""]
    for column in ("vision_subjects", "vision_tags"):
        try:
            value = json.loads(row[column] or "[]")
        except (TypeError, ValueError):
            continue
        if isinstance(value, list):
            parts.append(" ".join(str(item) for item in value))
    return " ".join(part for part in parts if part.strip())


def _evidence(payload: str | None) -> list[dict[str, Any]]:
    if not payload:
        return []
    try:
        values = json.loads(payload)
    except json.JSONDecodeError:
        return []
    return values if isinstance(values, list) else []


def _category_from_path(relpath: str) -> str:
    parts = PurePosixPath(relpath).parts
    top = parts[0].lower() if parts else "misc"
    return top if top in CATEGORIES else "misc"


def _where(filters: SearchFilters) -> tuple[str, list[object]]:
    # First clause, not last, and in the WHERE rather than a filter over the
    # results: everything below narrows what LIMIT/OFFSET then pages through,
    # so a stale row excluded here can never occupy a slot on a page or push a
    # real result onto the next one. Removing it afterwards would have hidden
    # the ghosts and quietly shortened every page.
    clauses = [LIVE_ONLY]
    params: list[object] = []
    if filters.category:
        clauses.append("search_fts.category=?")
        params.append(filters.category)
    if filters.root:
        clauses.append("search_fts.root=?")
        params.append(filters.root)
    if filters.year:
        clauses.append("search_fts MATCH ?")
        params.append(str(filters.year))
    if filters.genre:
        clauses.append("genre=?")
        params.append(filters.genre)
    if filters.group_kind:
        clauses.append("tags MATCH ?")
        params.append(filters.group_kind)
    return " AND ".join(clauses), params


def _match_query(query: str) -> str:
    terms = re.findall(r"[\w]+", query, flags=re.UNICODE)
    safe = [term for term in terms if term.upper() not in FTS_OPERATORS]
    return " ".join(f"{term}*" for term in safe)
