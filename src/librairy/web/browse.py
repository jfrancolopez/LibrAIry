from __future__ import annotations

import sqlite3
from pathlib import Path, PurePosixPath
from urllib.parse import urlencode

from librairy.config import Settings
from librairy.mediakind import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS
from librairy.paths import PathValidationError, validate_relpath
from librairy.proposals import decode_evidence
from librairy.scanner import is_visible_entry
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
    conn: sqlite3.Connection,
    settings: Settings,
    category: str,
    folder: str = "",
    page: int = 1,
) -> dict[str, object]:
    """What is actually in this library folder, enriched with what we know.

    **The filesystem decides what exists; the database only describes it.**
    This used to be the other way round, and the result was a Browse you could
    not trust. Directories were inferred from a page of *indexed files*:

        SELECT ... FROM search_fts JOIN items ... ORDER BY relpath LIMIT 50

    and then the second path component of each of those fifty rows became the
    folder list. On the author's library `Photos/` holds 89 indexed files, the
    first 83 of them inside `2022/`, so rows 0–49 were all in one folder and
    `Photos/Unknown/` — starting at row 83 — did not exist as far as the UI was
    concerned. You could still navigate straight into it, because that is a
    different prefix and therefore a different fifty rows, which is exactly why
    the bug looked arbitrary.

    Two consequences beyond the truncation: a file with no `search_fts` row was
    invisible however few files there were, and an empty directory could never
    appear at all.

    Directories now come from `iterdir()` and are never paginated — there is no
    number of files that can hide a folder. Only the file list pages.
    """
    if category not in CATEGORIES:
        raise ValueError("unknown category")
    prefix = _category_prefix(category, folder)
    base = _library_dir(settings, prefix)
    folders, files = _direct_children(settings, base, prefix)
    total_files = len(files)
    window = files[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]
    items = _enriched(conn, prefix, window)
    return {
        "category": category,
        "folder": folder,
        # Built here rather than concatenated in the template: a folder called
        # "R&B" turned "?folder=R&B" into folder=R plus a stray B parameter,
        # and the pane came up empty with nothing to explain why.
        "folders": [
            {"name": name, "count": count, "href": folder_href(category, folder, name)}
            for name, count in folders
        ],
        "items": items,
        # Load the first file into the details pane straight away, so arriving
        # at a folder shows something instead of "select a file". Only an
        # indexed file has a panel to show.
        "first_item_id": next((i["item_id"] for i in items if i["item_id"]), None),
        "page": page,
        "has_next": total_files > page * PAGE_SIZE,
        "has_prev": page > 1,
        "crumbs": _crumbs(category, folder),
        "parent_href": _parent_href(category, folder),
        # Pane 1 of the explorer: every category, so you can switch without
        # bouncing through /browse.
        **browse_home(conn),
    }


def _library_dir(settings: Settings, relpath: str) -> Path | None:
    """The physical directory for a Browse path, or None if there is not one.

    Containment is the existing `validate_relpath`, unchanged and not relaxed:
    `..`, encoded traversal, absolute paths and anything resolving outside the
    library root raise before a single directory is read.
    """
    if not relpath:
        return settings.library_dir
    try:
        candidate = validate_relpath(settings.library_dir, relpath, kind="folder")
    except PathValidationError:
        return None
    return candidate if candidate.is_dir() and not candidate.is_symlink() else None


def _direct_children(
    settings: Settings, base: Path | None, prefix: str
) -> tuple[list[tuple[str, int]], list[Path]]:
    """`(folders, files)` directly inside this directory, ignore rules applied.

    Direct children only. A folder's visibility does not depend on how many
    files are inside it, or on whether any of them are indexed.
    """
    if base is None:
        return [], []
    try:
        entries = sorted(base.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        # An unreadable directory is a fact worth not crashing over.
        return [], []
    visible = [
        entry
        for entry in entries
        if is_visible_entry(entry, f"{prefix}/{entry.name}" if prefix else entry.name,
                            settings.ignore_patterns)
    ]
    folders = [(entry.name, _child_count(settings, entry)) for entry in visible if entry.is_dir()]
    files = [entry for entry in visible if entry.is_file()]
    return folders, files


def _child_count(settings: Settings, directory: Path) -> int:
    """How many visible entries are directly inside — not a recursive count.

    Cheap and honest. A recursive total would mean walking the whole tree of
    every sibling on every page load, which on a NAS is not a thing to do to
    render a folder list.
    """
    try:
        return sum(
            1
            for entry in directory.iterdir()
            if is_visible_entry(entry, entry.name, settings.ignore_patterns)
        )
    except OSError:
        return 0


def _enriched(
    conn: sqlite3.Connection, prefix: str, files: list[Path]
) -> list[dict[str, object]]:
    """Disk entries, with whatever the index knows about them attached.

    A missing database row means *metadata unavailable*, never *file does not
    exist* — so an unindexed file is listed, with `indexed` false, and Browse
    stays a view of the library rather than a report on the index.
    """
    if not files:
        return []
    names = [file.name for file in files]
    placeholders = ",".join("?" for _ in names)
    relpaths = [f"{prefix}/{name}" if prefix else name for name in names]
    rows = {
        row["relpath"]: row
        for row in conn.execute(
            f"SELECT id, relpath, size FROM items "  # noqa: S608 - placeholders only
            f"WHERE root='library' AND relpath IN ({placeholders})",
            relpaths,
        )
    }
    items = []
    for file, relpath in zip(files, relpaths, strict=True):
        row = rows.get(relpath)
        items.append(
            {
                "item_id": row["id"] if row else None,
                "relpath": relpath,
                "name": file.name,
                "size": human_size(_size_of(file, row)),
                "thumb": has_thumbnail(file.name) and row is not None,
                # Drives one small badge. Correctness first: the file is listed
                # either way, this only says whether we know anything about it.
                "indexed": row is not None,
            }
        )
    return items


def _size_of(file: Path, row: sqlite3.Row | None) -> int:
    if row is not None:
        return int(row["size"] or 0)
    try:
        return file.stat().st_size
    except OSError:
        return 0


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
