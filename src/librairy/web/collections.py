"""One import, as Review prints it.

Two shapes, and the split is the whole scalability story. The Review page gets
a *summary* — counts from SQL, a handful of numbers per import, no member rows
at all — and the collection's own page gets one bounded section of one bounded
page of members. Eighty-seven cards inlined into Review is the thing this
replaces, not a smaller version of it.
"""

from __future__ import annotations

import sqlite3
from typing import Any
from urllib.parse import quote, urlencode

from librairy.config import Settings
from librairy.inbox_collections import (
    PAGE_SIZE,
    SECTION_LABEL,
    SECTION_NOTE,
    SECTIONS,
    Collection,
    members,
    summaries,
    summary,
)
from librairy.relationships import subjects

# What the row calls each category, in the words the rest of the program uses.
# `misc` is the one worth spelling differently: "Unsorted" is what it means to
# a person, and "misc" is what it is called in a column.
CATEGORY_LABEL = {
    "music": "Music",
    "music_videos": "Music videos",
    "movies": "Movies",
    "shows": "Shows",
    "photos": "Photos",
    "documents": "Documents",
    "books": "Books",
    "projects": "Projects",
    "misc": "Unsorted",
    "": "Not analysed yet",
}


def collections_view(conn: sqlite3.Connection) -> dict[str, Any]:
    """The summary block Review renders above its list of files."""
    found, total = summaries(conn)
    return {
        "collections": [_card(item) for item in found],
        "collection_total": total,
        "collections_more": max(0, total - len(found)),
    }


def collection_page(
    conn: sqlite3.Connection,
    settings: Settings | None,
    folder: str,
    *,
    section: str = "",
    page: int = 1,
) -> dict[str, Any] | None:
    """One collection in detail, or None when the import is over.

    None rather than an empty page: a collection whose files have all been
    committed is not an empty collection, it is a finished one, and the route
    turns that into a 404 rather than a heading over nothing.
    """
    found = summary(conn, folder)
    if found is None:
        return None
    section = section if section in SECTIONS else _first_populated(found)
    total = int(getattr(found, section))
    page = max(1, min(page, max(1, -(-total // PAGE_SIZE))))
    rows = members(conn, folder, section=section, page=page)
    companions = subjects(conn, [int(row["item_id"]) for row in rows])
    return {
        **_card(found),
        "section": section,
        "section_label": SECTION_LABEL[section],
        "section_note": SECTION_NOTE[section],
        "sections": [
            {
                "key": key,
                "label": SECTION_LABEL[key],
                "count": int(getattr(found, key)),
                "current": key == section,
                "href": f"{collection_href(folder)}?{urlencode({'section': key})}",
            }
            for key in SECTIONS
        ],
        "rows": [_row(conn, settings, row, companions) for row in rows],
        "page": page,
        "page_size": PAGE_SIZE,
        "section_total": total,
        "page_count": max(1, -(-total // PAGE_SIZE)),
        "has_next": page * PAGE_SIZE < total,
        "has_prev": page > 1,
        "range_start": 0 if not total else (page - 1) * PAGE_SIZE + 1,
        "range_end": min(page * PAGE_SIZE, total),
        "prev_href": _href(folder, section, page - 1),
        "next_href": _href(folder, section, page + 1),
    }


def _first_populated(found: Collection) -> str:
    """Open on the section that has something in it.

    A collection whose every file needs a choice should not open on an empty
    "Ready to file" tab — the first thing the page says would be that there is
    nothing to do.
    """
    for key in SECTIONS:
        if int(getattr(found, key)):
            return key
    return SECTIONS[0]


def _card(found: Collection) -> dict[str, Any]:
    return {
        "folder": found.folder,
        "href": collection_href(found.folder),
        "total": found.total,
        "ready": found.ready,
        "choice": found.choice,
        "unresolved": found.unresolved,
        "waiting": found.waiting,
        "companions": found.companions,
        "settled": found.settled,
        "categories": [
            {"label": CATEGORY_LABEL.get(name, name), "count": count}
            for name, count in found.categories
        ],
    }


def _row(
    conn: sqlite3.Connection,  # noqa: ARG001
    settings: Settings | None,  # noqa: ARG001
    row: sqlite3.Row,
    companions: dict[int, list],
) -> dict[str, Any]:
    from librairy.web.review import human_size

    item_id = int(row["item_id"])
    return {
        **dict(row),
        #  Everything after the collection's own folder. The heading already
        #  says `CameraCard`, and repeating it at the start of every row is
        #  fifty copies of a fact the page has stated once.
        "display_name": str(row["relpath"]).split("/", 1)[-1],
        "category_label": CATEGORY_LABEL.get(row["category"] or "", row["category"] or ""),
        "size_label": human_size(row["size"]),
        "percent": int(round((row["confidence"] or 0) * 100)),
        #  The companions this file explains, shown under it rather than as
        #  unrelated rows further down. They are still real files with their
        #  own operations — this is where they are *read*, not a claim that
        #  they move by magic.
        "related": companions.get(item_id, []),
    }


def collection_href(folder: str) -> str:
    """The collection's own address. One spelling, so a link and a redirect
    after a POST cannot disagree about how a folder with a space in it is
    written."""
    return f"/review/collection/{quote(folder, safe='')}"


def _href(folder: str, section: str, page: int) -> str:
    return (
        f"{collection_href(folder)}?"
        f"{urlencode({'section': section, 'page': max(1, page)})}"
    )
