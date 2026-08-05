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


@dataclass(frozen=True)
class SearchFilters:
    category: str | None = None
    root: str | None = None
    year: int | None = None
    genre: str | None = None
    group_kind: str | None = None
    content: bool = False
    page: int = 1


def sync_search_item(conn: sqlite3.Connection, item_id: int) -> None:
    row = conn.execute(
        """
        SELECT i.*, p.clean_name, p.category, p.evidence, g.kind AS group_kind
        FROM items i
        LEFT JOIN proposals p ON p.item_id = i.id AND p.status != 'superseded'
        LEFT JOIN groups g ON g.id = p.group_id
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


def rebuild_search_index(conn: sqlite3.Connection) -> int:
    conn.execute("DELETE FROM search_fts")
    item_ids = [row["id"] for row in conn.execute("SELECT id FROM items ORDER BY id")]
    for item_id in item_ids:
        sync_search_item(conn, item_id)
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
    clauses = ["content_fts MATCH ?"]
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
    return {
        "query": query,
        "filters": filters,
        "results": rows,
        "page_size": PAGE_SIZE,
        "has_prev": filters.page > 1,
        "has_next": len(rows) == PAGE_SIZE,
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
    clauses = ["1=1"]
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
