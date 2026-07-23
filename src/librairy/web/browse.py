from __future__ import annotations

import sqlite3
from pathlib import PurePosixPath

from librairy.config import Settings
from librairy.proposals import decode_evidence
from librairy.search import host_path
from librairy.web.thumbs import PreviewError, preview_for_item

CATEGORIES = ("music", "movies", "shows", "photos", "documents", "books", "projects", "misc")
PAGE_SIZE = 50


def browse_home(conn: sqlite3.Connection) -> dict[str, object]:
    counts = {
        row["category"] or "misc": row["count"]
        for row in conn.execute(
            "SELECT category, COUNT(*) AS count FROM search_fts GROUP BY category"
        )
    }
    return {"categories": [(category, counts.get(category, 0)) for category in CATEGORIES]}


def browse_category(
    conn: sqlite3.Connection, category: str, folder: str = "", page: int = 1
) -> dict[str, object]:
    if category not in CATEGORIES:
        raise ValueError("unknown category")
    prefix = _category_prefix(category, folder)
    rows = conn.execute(
        """
        SELECT search_fts.item_id, i.relpath, search_fts.category
        FROM search_fts
        JOIN items i ON i.id = search_fts.item_id
        WHERE search_fts.category=? AND i.relpath >= ? AND i.relpath < ?
        ORDER BY i.relpath
        LIMIT ? OFFSET ?
        """,
        (category, prefix, _prefix_end(prefix), PAGE_SIZE, (page - 1) * PAGE_SIZE),
    ).fetchall()
    folders: dict[str, int] = {}
    items = []
    for row in rows:
        remainder = row["relpath"][len(prefix) :].lstrip("/") if prefix else row["relpath"]
        parts = PurePosixPath(remainder).parts
        if len(parts) > 1:
            folders[parts[0]] = folders.get(parts[0], 0) + 1
        else:
            items.append(row)
    return {
        "category": category,
        "folder": folder,
        "folders": sorted(folders.items()),
        "items": items,
        "page": page,
        "has_next": len(rows) == PAGE_SIZE,
        "has_prev": page > 1,
    }


def item_detail(conn: sqlite3.Connection, settings: Settings, item_id: int) -> dict[str, object]:
    row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if row is None:
        raise ValueError("item not found")
    proposal = conn.execute(
        """
        SELECT * FROM proposals
        WHERE item_id=? AND status != 'superseded'
        ORDER BY id DESC LIMIT 1
        """,
        (item_id,),
    ).fetchone()
    history = conn.execute(
        """
        SELECT * FROM history
        WHERE (src_root=? AND src_relpath=?) OR (dest_root=? AND dest_relpath=?)
        ORDER BY id DESC LIMIT 10
        """,
        (row["root"], row["relpath"], row["root"], row["relpath"]),
    ).fetchall()
    siblings = _siblings(conn, row, proposal)
    preview_error = None
    try:
        preview = preview_for_item(conn, settings, item_id)
    except (OSError, PreviewError) as exc:
        preview = None
        preview_error = str(exc) or exc.__class__.__name__
    evidence_error = None
    try:
        evidence = decode_evidence(proposal["evidence"]) if proposal else []
    except (TypeError, ValueError) as exc:
        evidence = []
        evidence_error = str(exc) or exc.__class__.__name__
    return {
        "item": row,
        "proposal": proposal,
        "evidence": evidence,
        "evidence_error": evidence_error,
        "history": history,
        "siblings": siblings,
        "preview": preview,
        "preview_error": preview_error,
        "host_path": host_path(settings, row["root"], row["relpath"]),
    }


def _siblings(conn: sqlite3.Connection, item: sqlite3.Row, proposal: sqlite3.Row | None):
    if proposal and proposal["group_id"] is not None:
        return conn.execute(
            """
            SELECT i.id, i.relpath
            FROM proposals p JOIN items i ON i.id = p.item_id
            WHERE p.group_id=? AND i.id != ?
            ORDER BY i.relpath LIMIT 10
            """,
            (proposal["group_id"], item["id"]),
        ).fetchall()
    parent = PurePosixPath(item["relpath"]).parent.as_posix()
    return conn.execute(
        """
        SELECT id, relpath FROM items
        WHERE root=? AND relpath LIKE ? AND id != ?
        ORDER BY relpath LIMIT 10
        """,
        (item["root"], f"{parent}/%", item["id"]),
    ).fetchall()


def _category_prefix(category: str, folder: str) -> str:
    top = category.capitalize() if category != "misc" else "Misc"
    if category == "movies":
        top = "Movies"
    if category == "shows":
        top = "Shows"
    return f"{top}/{folder}".rstrip("/")


def _prefix_end(prefix: str) -> str:
    return prefix + "\uffff"
