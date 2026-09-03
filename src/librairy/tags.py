"""Tags that outlive the name they were written on, and the Projects made of them.

A hashtag was already read, already evidenced and already stripped out of the
final name. What it was not was **durable**. It lived in the proposal's frozen
evidence, and a proposal is a thing about one moment: file `roof #ProjectHouse.pdf`
and the tag goes into the evidence, the clean name loses the tag on the way to
the library, and re-analysing the filed file reads a library path where the tag
is not. The hint survived exactly as long as the guess that consumed it.

So a tag is stored against the **item**. `items.relpath` changes when a file
moves; `items.id` does not, and that is the whole of why `#ProjectHouse` is
still true a year later.

## What a tag is worth

Explicit user evidence. Nobody types `#Taxes2026` by accident, so it sits above
a learned habit and above a model's guess — and below anything that identifies
the file, because it is a statement about *context* rather than about content:

    1  safety invariants        never overwrite, never delete, revalidate
    2  explicit user policy     a Format Policy, a promoted rule
    3  strong current evidence  a catalog identity, an ISBN, a DOI
       explicit user tag        `#ProjectHouse` — context, from a person
    4  learned habit, AI cue    weaker than either

**A tag never picks a category on its own.** `#ProjectHouse` on an installer
does not make the installer a house document, and nothing here can move a file,
approve one, or reach Commit.

Within that limit it is evidence **now**, in the proposal being made, and not
only once something has been learned about it. Two different facts, and the
program needs both:

    #ProjectHouse                     what you are telling LibrAIry, now
    "you file #ProjectHouse docs      what LibrAIry has learned you tend to
     under Documents/House"            do with that kind of hint

The first does not wait for the second. A tag that names a promoted Project
puts the file in that Project the moment it is read; every tag is explicit
evidence on the proposal, is asked *before* an inferred cue when both could
answer (`decision_cues.cues_for`), and is what a rule about tagged files
matches on. What it does not do is name a destination — that still comes from
evidence about the file, a learned pattern, or a rule somebody promoted, and a
tag is not a shortcut past any of them.

## Projects

A Project is a **promoted tag**, and nothing else. Its members are the items
carrying its tag, read back out of this table; there is no membership list,
because a second copy of that would be free to disagree with the first and
would need writing by everything that ever tags anything.

It is a *view*, and deliberately not a place:

    Project          files that belong together, wherever they live
    Project folder   `Projects/{project}/` — a filing destination on disk

Those are two concepts and the vocabulary keeps them apart. Promoting
`#ProjectHouse` moves no file into `Projects/`, and filing something into
`Projects/` creates no Project. See `docs/ui-vocabulary.md`.

**Promotion is explicit.** A tag on four hundred files is a tag on four hundred
files; it becomes a Project when somebody says so, for the same reason a habit
becomes a rule only when somebody says so (`librairy/rules.py`).

See `docs/ROADMAP.md` M2-05.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from librairy.classify.hashtags import FILENAME, FOLDER, MANUAL, extract_hashtags
from librairy.live import live
from librairy.planner import utc_now

SOURCE_LABEL = {
    FILENAME: "you tagged this file",
    FOLDER: "in a tagged folder",
    MANUAL: "you added this",
}

#  How many files one page of a Project lists. The counts above it are
#  aggregates over the whole thing; a Project of forty thousand files renders
#  the same amount of HTML as one of four.
PAGE_SIZE = 50

#  How many tags the browse filter offers. A library with nine thousand tags
#  has a tag nobody will ever pick out of a list, and the list is a summary.
SHOWN = 40


@dataclass(frozen=True)
class Project:
    id: int
    tag: str
    name: str
    created_at: str


def record(conn: sqlite3.Connection, item_id: int, relpath: str) -> int:
    """Store every hashtag on this path against the item. Idempotent.

    Called at analysis time, from the path the file arrived under — which is
    the only moment the tag is legible, because filing strips it out of the
    name. Adding the same tag twice is one row: the primary key is
    (item, tag), so a re-analysis records provenance again and nothing else.

    Nothing is ever removed here. A tag disappearing from a filename is not
    somebody saying they did not mean it — most often it is LibrAIry's own
    clean name, which is exactly the round trip this table exists to survive.
    Untagging is `remove`, and only a person reaches it.
    """
    hints = extract_hashtags(relpath)
    now = utc_now()
    for found in hints.found:
        conn.execute(
            """
            INSERT INTO item_tags(item_id, tag, label, source, detail, added_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(item_id, tag) DO UPDATE SET
              label=excluded.label, source=excluded.source, detail=excluded.detail
            """,
            (item_id, found.tag, found.label, found.source, found.detail, now),
        )
    return len(hints.found)


def add(
    conn: sqlite3.Connection, item_id: int, label: str, source: str = MANUAL
) -> str:
    """Tag one file by hand. Returns the normalised tag, or "" if it was not one."""
    from librairy.classify.hashtags import _sanitize_tag  # noqa: PLC2701

    tag = _sanitize_tag(label.lstrip("#"))
    if not tag:
        return ""
    conn.execute(
        """
        INSERT INTO item_tags(item_id, tag, label, source, detail, added_at)
        VALUES (?, ?, ?, ?, '', ?)
        ON CONFLICT(item_id, tag) DO UPDATE SET label=excluded.label
        """,
        (item_id, tag, label.lstrip("#").strip(), source, utc_now()),
    )
    return tag


def remove(conn: sqlite3.Connection, item_id: int, tag: str) -> None:
    conn.execute(
        "DELETE FROM item_tags WHERE item_id=? AND tag=?", (item_id, tag)
    )


def for_item(conn: sqlite3.Connection, item_id: int) -> list[dict[str, object]]:
    """This file's tags, with where each came from and the Project it joined.

    The Project is part of the answer rather than a second lookup: a file
    carrying a promoted tag is in that Project from the moment the tag is read,
    and a page that shows the tag without saying so is hiding the useful half.
    """
    return [
        {
            "tag": str(row["tag"]),
            "label": str(row["label"]),
            "source": str(row["source"]),
            "detail": str(row["detail"] or ""),
            "why": SOURCE_LABEL.get(str(row["source"]), str(row["source"])),
            "project": str(row["project"] or ""),
            "project_id": int(row["project_id"] or 0),
        }
        for row in conn.execute(
            """
            SELECT t.tag, t.label, t.source, t.detail,
                   p.name AS project, p.id AS project_id
            FROM item_tags t
            LEFT JOIN projects p ON p.tag = t.tag
            WHERE t.item_id = ? ORDER BY t.tag
            """,
            (item_id,),
        )
    ]


def for_items(conn: sqlite3.Connection, item_ids: list[int]) -> dict[int, list[str]]:
    """The tags on a page of files, in one statement rather than one per row."""
    if not item_ids:
        return {}
    placeholders = ",".join("?" for _ in item_ids)
    found: dict[int, list[str]] = {}
    for row in conn.execute(
        f"SELECT item_id, label FROM item_tags WHERE item_id IN ({placeholders})"  # noqa: S608
        " ORDER BY tag",
        item_ids,
    ):
        found.setdefault(int(row["item_id"]), []).append(str(row["label"]))
    return found


def words_for(conn: sqlite3.Connection, item_id: int) -> str:
    """This item's tags as one string, for the search index's `tags` column.

    Read from here rather than from the proposal's evidence, which is the
    change that makes a tag findable after filing: the evidence belongs to a
    guess that was superseded the moment the file moved.
    """
    try:
        rows = conn.execute(
            "SELECT label FROM item_tags WHERE item_id=? ORDER BY tag", (item_id,)
        ).fetchall()
    except sqlite3.OperationalError:
        #  A database from before this table existed. `sync_search_item` runs
        #  during migrations and against historical fixtures, and a search
        #  index that cannot be written is a worse outcome than one with no
        #  tags in it — the migration backfills them a moment later anyway.
        return ""
    return " ".join(str(row["label"]) for row in rows)


def counts(conn: sqlite3.Connection, limit: int = SHOWN) -> list[dict[str, object]]:
    """Every tag in use, with how many live files carry it. One aggregate."""
    return [
        {"tag": str(row["tag"]), "label": str(row["label"]), "files": int(row["files"])}
        for row in conn.execute(
            f"""
            SELECT t.tag, MAX(t.label) AS label, COUNT(*) AS files
            FROM item_tags t JOIN items i ON i.id = t.item_id AND {live()}
            GROUP BY t.tag
            ORDER BY files DESC, t.tag
            LIMIT ?
            """,  # noqa: S608 - `live()` is a module constant
            (limit,),
        )
    ]


# --- Projects ---------------------------------------------------------------------


def promote(conn: sqlite3.Connection, tag: str, name: str = "") -> int:
    """Make a Project out of a tag. Only ever called by somebody pressing a button.

    A tag on four hundred files is a tag on four hundred files. It becomes a
    Project when its owner says so — the same rule, and for the same reason, as
    a habit becoming a rule in `librairy/rules.py`. Nothing counts its way here.

    Moves no file. A Project is a view across the library, not a folder in it.
    """
    if not tag:
        raise ValueError("a project needs a tag")
    now = utc_now()
    conn.execute(
        """
        INSERT INTO projects(tag, name, created_at) VALUES (?, ?, ?)
        ON CONFLICT(tag) DO UPDATE SET name=excluded.name
        """,
        (tag, name.strip() or _titled(tag), now),
    )
    row = conn.execute("SELECT id FROM projects WHERE tag=?", (tag,)).fetchone()
    return int(row["id"])


def rename(conn: sqlite3.Connection, project_id: int, name: str) -> None:
    """A display name, which changes nothing about the tag or any file.

    The tag is the identity and stays as it was written; this is what the page
    calls it. Renaming a Project must never rewrite a library path.
    """
    if name.strip():
        conn.execute(
            "UPDATE projects SET name=? WHERE id=?", (name.strip(), project_id)
        )


def demote(conn: sqlite3.Connection, project_id: int) -> None:
    """Stop treating this tag as a Project. The tag and the files are untouched."""
    conn.execute("DELETE FROM projects WHERE id=?", (project_id,))


def projects(conn: sqlite3.Connection) -> list[dict[str, object]]:
    """Every Project with its file count. One statement, never a row per file."""
    return [
        {
            "id": int(row["id"]),
            "tag": str(row["tag"]),
            "name": str(row["name"]),
            "files": int(row["files"]),
        }
        for row in conn.execute(
            f"""
            SELECT p.id, p.tag, p.name,
                   (SELECT COUNT(*) FROM item_tags t JOIN items i ON i.id = t.item_id
                     WHERE t.tag = p.tag AND {live()}) AS files
            FROM projects p ORDER BY p.name COLLATE NOCASE
            """  # noqa: S608 - `live()` is a module constant
        )
    ]


def project_for(conn: sqlite3.Connection, project_id: int) -> Project | None:
    row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if row is None:
        return None
    return Project(
        int(row["id"]), str(row["tag"]), str(row["name"]), str(row["created_at"] or "")
    )


def project_by_tag(conn: sqlite3.Connection, tag: str) -> Project | None:
    row = conn.execute("SELECT id FROM projects WHERE tag=?", (tag,)).fetchone()
    return project_for(conn, int(row["id"])) if row else None


def summary(conn: sqlite3.Connection, tag: str) -> dict[str, object]:
    """What a Project is made of, as counts. Never a row per member.

    Four aggregates over the tag: how many files and how much they occupy, what
    kinds they are, how many are still waiting on a decision, and when anything
    last happened. A Project of forty thousand files costs the same as one of
    four, which is the rule every page in this program is held to.
    """
    totals = conn.execute(
        f"""
        SELECT COUNT(*) AS files, COALESCE(SUM(i.size), 0) AS bytes,
               MAX(i.last_seen_at) AS last
        FROM item_tags t JOIN items i ON i.id = t.item_id AND {live()}
        WHERE t.tag = ?
        """,  # noqa: S608 - `live()` is a module constant
        (tag,),
    ).fetchone()
    kinds = [
        {"category": str(row["category"] or "unsorted"), "files": int(row["files"])}
        for row in conn.execute(
            f"""
            SELECT COALESCE(p.category, '') AS category, COUNT(*) AS files
            FROM item_tags t
            JOIN items i ON i.id = t.item_id AND {live()}
            LEFT JOIN proposals p ON p.item_id = i.id AND p.status != 'superseded'
            WHERE t.tag = ?
            GROUP BY category ORDER BY files DESC, category
            """,  # noqa: S608 - `live()` is a module constant
            (tag,),
        )
    ]
    waiting = int(
        conn.execute(
            f"""
            SELECT COUNT(*) FROM item_tags t
            JOIN items i ON i.id = t.item_id AND {live()}
            JOIN proposals p ON p.item_id = i.id AND p.status = 'proposed'
            WHERE t.tag = ?
            """,  # noqa: S608 - `live()` is a module constant
            (tag,),
        ).fetchone()[0]
    )
    filed = int(
        conn.execute(
            f"""
            SELECT COUNT(*) FROM item_tags t
            JOIN items i ON i.id = t.item_id AND {live()}
            WHERE t.tag = ? AND i.root = 'library'
            """,  # noqa: S608 - `live()` is a module constant
            (tag,),
        ).fetchone()[0]
    )
    return {
        "files": int(totals["files"] or 0),
        "bytes": int(totals["bytes"] or 0),
        "last": str(totals["last"] or ""),
        "kinds": kinds,
        "waiting": waiting,
        "filed": filed,
        #  A Project that spans categories is the normal case and the point:
        #  a house project is quotes, photographs and a video walkthrough.
        "categories": len([kind for kind in kinds if kind["files"]]),
    }


def members(
    conn: sqlite3.Connection, tag: str, page: int = 1
) -> list[dict[str, object]]:
    """One bounded page of a Project's files, newest first."""
    offset = max(0, (max(1, page) - 1) * PAGE_SIZE)
    return [
        {
            "item_id": int(row["id"]),
            "root": str(row["root"]),
            "relpath": str(row["relpath"]),
            "name": str(row["relpath"]).rsplit("/", 1)[-1],
            "size": int(row["size"] or 0),
            "category": str(row["category"] or ""),
            "waiting": str(row["status"] or "") == "proposed",
        }
        for row in conn.execute(
            f"""
            SELECT i.id, i.root, i.relpath, i.size, p.category, p.status
            FROM item_tags t
            JOIN items i ON i.id = t.item_id AND {live()}
            LEFT JOIN proposals p ON p.item_id = i.id AND p.status != 'superseded'
            WHERE t.tag = ?
            ORDER BY i.last_seen_at DESC, i.id DESC
            LIMIT ? OFFSET ?
            """,  # noqa: S608 - `live()` is a module constant
            (tag, PAGE_SIZE, offset),
        )
    ]


def promoted(conn: sqlite3.Connection) -> dict[str, str]:
    """Every tag that is a Project, as tag → name. One statement for a batch.

    Read once per analysis pass rather than once per file: a Project is a thing
    somebody made deliberately, so there are never many, and asking per item
    would put a statement per file into the one loop that has to stay cheap at
    a million.
    """
    return {
        str(row["tag"]): str(row["name"])
        for row in conn.execute("SELECT tag, name FROM projects")
    }


def _titled(tag: str) -> str:
    return " ".join(part.capitalize() for part in tag.split("-")) or tag
