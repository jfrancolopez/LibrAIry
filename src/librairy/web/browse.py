from __future__ import annotations

import sqlite3
from pathlib import PurePosixPath
from urllib.parse import urlencode

from librairy.config import Settings
from librairy.mediakind import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS
from librairy.proposals import decode_evidence
from librairy.search import host_path
from librairy.web.thumbs import PreviewError, preview_for_item

CATEGORIES = ("music", "movies", "shows", "photos", "documents", "books", "projects", "misc")
PAGE_SIZE = 50


def browse_home(conn: sqlite3.Connection) -> dict[str, object]:
    # Count only what Browse can actually show: committed library files. Items
    # still sitting in the inbox are indexed and searchable, but they are not
    # browsable yet, and counting them made the panes look broken.
    counts = {
        row["category"] or "misc": row["count"]
        for row in conn.execute(
            """
            SELECT s.category, COUNT(*) AS count
            FROM search_fts s JOIN items i ON i.id = s.item_id
            WHERE i.root = 'library'
            GROUP BY s.category
            """
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
        SELECT search_fts.item_id, i.relpath, i.size, search_fts.category
        FROM search_fts
        JOIN items i ON i.id = search_fts.item_id
        WHERE search_fts.category=? AND i.root='library' AND i.relpath >= ? AND i.relpath < ?
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
            name = parts[0] if parts else row["relpath"]
            items.append(
                {
                    "item_id": row["item_id"],
                    "relpath": row["relpath"],
                    "name": name,
                    "size": human_size(row["size"]),
                    # Only images and video have a thumbnail; asking for one on
                    # anything else just renders a broken-image icon.
                    "thumb": has_thumbnail(name),
                }
            )
    return {
        "category": category,
        "folder": folder,
        # Built here rather than concatenated in the template: a folder called
        # "R&B" turned "?folder=R&B" into folder=R plus a stray B parameter,
        # and the pane came up empty with nothing to explain why.
        "folders": [
            {"name": name, "count": count, "href": folder_href(category, folder, name)}
            for name, count in sorted(folders.items())
        ],
        "items": items,
        # Load the first file into the details pane straight away, so arriving
        # at a folder shows something instead of "select a file".
        "first_item_id": items[0]["item_id"] if items else None,
        "page": page,
        "has_next": len(rows) == PAGE_SIZE,
        "has_prev": page > 1,
        "crumbs": _crumbs(category, folder),
        "parent_href": _parent_href(category, folder),
        # Pane 1 of the explorer: every category, so you can switch without
        # bouncing through /browse.
        **browse_home(conn),
    }


def has_thumbnail(name: str) -> bool:
    return PurePosixPath(name).suffix.lower() in IMAGE_EXTENSIONS | VIDEO_EXTENSIONS


def human_size(size: int | None) -> str:
    """Bytes as something a person can read at a glance in a file list."""
    if not size or size < 0:
        return ""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return ""


def folder_href(category: str, folder: str, name: str) -> str:
    """Link into a subfolder, with the folder name properly escaped.

    Every folder link in Browse goes through here. Folder names come from the
    filesystem, so they carry &, ?, # and every other character that means
    something in a URL.
    """
    child = f"{folder}/{name}" if folder else name
    return _folder_url(category, child)


def _folder_url(category: str, folder: str) -> str:
    if not folder:
        return f"/browse/{category}"
    return f"/browse/{category}?{urlencode({'folder': folder})}"


def _crumbs(category: str, folder: str) -> list[dict[str, str]]:
    """Breadcrumb trail: All → Category → each folder segment."""
    trail = [
        {"label": "All", "href": "/browse"},
        {"label": category.capitalize(), "href": f"/browse/{category}"},
    ]
    walked = ""
    for part in [segment for segment in folder.split("/") if segment]:
        walked = f"{walked}/{part}" if walked else part
        trail.append({"label": part, "href": _folder_url(category, walked)})
    return trail


def _parent_href(category: str, folder: str) -> str:
    parts = [segment for segment in folder.split("/") if segment]
    if not parts:
        return "/browse"
    return _folder_url(category, "/".join(parts[:-1]))


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
