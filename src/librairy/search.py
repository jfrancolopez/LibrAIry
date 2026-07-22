from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

PAGE_SIZE = 50
TEXT_FIELDS = ("name", "clean_name", "tags", "artist", "album", "title", "show", "genre", "event")
CATEGORIES = {"music", "movies", "shows", "photos", "documents", "books", "projects", "misc"}
FTS_OPERATORS = {"AND", "OR", "NOT", "NEAR"}


@dataclass(frozen=True)
class SearchFilters:
    category: str | None = None
    root: str | None = None
    year: int | None = None
    genre: str | None = None
    group_kind: str | None = None
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
) -> list[sqlite3.Row]:
    filters = filters or SearchFilters()
    where, params = _where(filters)
    match = _match_query(query)
    if match:
        where = f"search_fts MATCH ? AND {where}"
        params.insert(0, match)
        order = "bm25(search_fts)"
    else:
        order = "item_id"
    params.extend([PAGE_SIZE, (filters.page - 1) * PAGE_SIZE])
    return list(
        conn.execute(
            f"""
            SELECT item_id, root, category,
                   highlight(search_fts, 0, '<mark>', '</mark>') AS name,
                   highlight(search_fts, 1, '<mark>', '</mark>') AS clean_name,
                   snippet(search_fts, 2, '<mark>', '</mark>', '...', 12) AS snippet
            FROM search_fts
            WHERE {where}
            ORDER BY {order}
            LIMIT ? OFFSET ?
            """,
            params,
        )
    )


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
        clauses.append("category=?")
        params.append(filters.category)
    if filters.root:
        clauses.append("root=?")
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
