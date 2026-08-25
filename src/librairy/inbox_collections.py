"""What arrived together.

Review has always treated arrivals as independent files, and for a dropped-in
download that is right. It is wrong for the way most things actually arrive:

    Inbox/CameraCard-Aug24/     200 photographs and 30 clips off one card
    Inbox/Album Rip/            twelve tracks, a cover and a cue sheet
    Inbox/Old Drive/            somebody's entire previous computer

Those are one import, and a page that shows them as eighty-seven unrelated
rows makes the person reconstruct that fact by reading paths.

**A collection is the folder, and nothing else.** Not files that arrived within
a minute of each other, not consecutive item ids, not "these look like they go
together". Arrival time is the tempting one and it is wrong for the same reason
in both directions: copying a card over USB 2 takes twenty minutes and drops
two hundred files into one minute each, while a slow NAS copy of one folder
spans an hour. A directory is something the person made. Timestamps are
something the transfer did.

Files sitting directly at the inbox root are not a collection. They arrived
loose, and inventing a container called "Inbox" for them would be a heading
over an accident.

**It is presentation and orchestration, never classification.** The collection
does not choose a category, a destination or a decision for anything in it. A
camera card holding photographs, phone video and two files LibrAIry cannot name
is one import with three answers in it, and flattening that to one answer is
exactly the confident wrongness this program is built to avoid. What the
collection knows is *how much of this is settled* — and where the rest is.

Nothing here is persisted. The collection's identity is the folder name, which
is already durable, already stable while its files are there, and already gone
when they leave. A table would only be a second copy of that, capable of
disagreeing with it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from librairy.live import live

# Sections, in the order somebody works through them: what is settled, what
# needs them, what could not be answered at all.
READY = "ready"
CHOICE = "choice"
UNRESOLVED = "unresolved"
WAITING = "waiting"

SECTIONS = (READY, CHOICE, UNRESOLVED, WAITING)

SECTION_LABEL = {
    READY: "Ready to file",
    CHOICE: "Needs your choice",
    UNRESOLVED: "Unresolved",
    WAITING: "Approved, waiting for Commit",
}

SECTION_NOTE = {
    READY: "Each of these has a destination of its own. Approving them applies "
    "answers LibrAIry already has — it decides nothing new.",
    CHOICE: "A copy you may already have, or a version to pick between. "
    "Only you can answer these, and nothing bulk reaches them.",
    UNRESOLVED: "LibrAIry could not name a destination for these. They stay "
    "here until it can, or until you say where they go.",
    WAITING: "Already approved. Nothing has moved yet.",
}

#  A folder holding one file is not an import.
#
#  The same rule Review already applies to proposal groups, for the same
#  reason: a heading, a set of counts and a link to a page holding one row is
#  three pieces of furniture around a decision somebody was going to make on
#  the row itself. The file still appears in the list below, where it always
#  did.
FLOOR = 2

#  How many collections the Review summary lists. The page is a summary, and a
#  summary of two hundred folders is a list of two hundred folders. The count
#  above it stays true either way — it is a COUNT over the whole inbox, not the
#  length of what got rendered.
SUMMARY_LIMIT = 12

#  One page of a collection's members. The same fifty every other list in
#  LibrAIry uses; `tests/test_scale.py` pins them together.
PAGE_SIZE = 50

#  A proposal that is a question rather than an answer: it points *out* of the
#  library, or the file has a comparison waiting on it. Either way somebody has
#  to say which, and "Approve the ready ones" must not be able to reach it.
_IS_CHOICE = (
    "(p.dest_root='quarantine'"
    " OR EXISTS (SELECT 1 FROM duplicate_reports d WHERE d.item_id = i.id))"
)

#  What section a member is in, as one expression, so the counts on the summary
#  and the rows on the detail page cannot drift apart. Two hand-written filters
#  agreeing is luck.
_SECTION = f"""
  CASE
    WHEN p.id IS NULL THEN 'unresolved'
    WHEN p.status='approved' THEN 'waiting'
    WHEN {_IS_CHOICE} THEN 'choice'
    WHEN p.status='proposed' AND p.dest_relpath IS NOT NULL THEN 'ready'
    ELSE 'unresolved'
  END
"""

#  The members of a collection: live inbox files under a top-level folder,
#  excluding anything already decided against or already filed. A committed
#  proposal's item has left the inbox by then anyway — this is belt and braces
#  for the moment between the move and the next scan.
_MEMBER_JOIN = f"""
  FROM items i
  LEFT JOIN proposals p ON p.item_id = i.id
       AND p.status NOT IN ('superseded','rejected','committed')
  WHERE i.root='inbox' AND {live()} AND instr(i.relpath, '/') > 0
"""

#  The folder an inbox file belongs to: everything before the first slash.
#  `Old Drive/Documents/tax.pdf` belongs to `Old Drive`, which is the import,
#  not to `Old Drive/Documents`, which is how the person had it arranged.
_FOLDER = "substr(i.relpath, 1, instr(i.relpath, '/') - 1)"


@dataclass(frozen=True)
class Collection:
    """One import, as the summary prints it."""

    folder: str
    total: int
    ready: int
    choice: int
    unresolved: int
    waiting: int
    companions: int
    categories: list[tuple[str, int]] = field(default_factory=list)
    companion_kinds: list[tuple[str, int]] = field(default_factory=list)

    @property
    def settled(self) -> bool:
        """Nothing left that anybody has to look at."""
        return not (self.choice or self.unresolved)

    @property
    def approvable(self) -> int:
        """How many `Approve the ready ones` would apply.

        Never the whole collection unless the whole collection is
        independently approvable — which is the same number, said once.
        """
        return self.ready


def folder_of(relpath: str) -> str:
    """The collection a given inbox path belongs to, or "" for a loose file."""
    head, slash, _rest = str(relpath).replace("\\", "/").partition("/")
    return head if slash else ""


def summaries(
    conn: sqlite3.Connection, *, limit: int = SUMMARY_LIMIT
) -> tuple[list[Collection], int]:
    """Every import currently in the inbox, biggest first, bounded.

    Three aggregate queries and no row objects built per member: the counts are
    `COUNT(*)` over indexed columns, which does not care whether it counted ten
    files or ten thousand. The second half of the return is how many
    collections there are in total, so a bounded list can say what it left out.
    """
    rows = conn.execute(
        f"""
        SELECT {_FOLDER} AS folder, COUNT(*) AS total,
               SUM(CASE WHEN {_SECTION} = 'ready' THEN 1 ELSE 0 END) AS ready,
               SUM(CASE WHEN {_SECTION} = 'choice' THEN 1 ELSE 0 END) AS choice,
               SUM(CASE WHEN {_SECTION} = 'unresolved' THEN 1 ELSE 0 END) AS unresolved,
               SUM(CASE WHEN {_SECTION} = 'waiting' THEN 1 ELSE 0 END) AS waiting
        {_MEMBER_JOIN}
        GROUP BY folder
        HAVING total >= ?
        ORDER BY total DESC, folder COLLATE NOCASE
        LIMIT ?
        """,  # noqa: S608 - every fragment is a module constant
        (FLOOR, max(1, limit)),
    ).fetchall()
    total = int(
        conn.execute(
            f"""
            SELECT COUNT(*) AS n FROM (
              SELECT {_FOLDER} AS folder, COUNT(*) AS members
              {_MEMBER_JOIN} GROUP BY folder HAVING members >= ?
            )
            """,  # noqa: S608 - every fragment is a module constant
            (FLOOR,),
        ).fetchone()["n"]
    )
    folders = [str(row["folder"]) for row in rows]
    companions = _companion_counts(conn, folders)
    kinds = _companion_kinds(conn, folders)
    categories = _category_counts(conn, folders)
    return (
        [
            Collection(
                folder=str(row["folder"]),
                total=int(row["total"]),
                ready=int(row["ready"]),
                choice=int(row["choice"]),
                unresolved=int(row["unresolved"]),
                waiting=int(row["waiting"]),
                companions=companions.get(str(row["folder"]), 0),
                companion_kinds=kinds.get(str(row["folder"]), []),
                categories=categories.get(str(row["folder"]), []),
            )
            for row in rows
        ],
        total,
    )


def summary(conn: sqlite3.Connection, folder: str) -> Collection | None:
    """One collection by name, or None when nothing of it is left.

    None is the honest answer to a folder whose files have all been committed:
    the import is over. Keeping a heading alive because a row once existed is
    how a queue comes to list work that finished last week.
    """
    if not folder or "/" in folder:
        return None
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN {_SECTION} = 'ready' THEN 1 ELSE 0 END) AS ready,
               SUM(CASE WHEN {_SECTION} = 'choice' THEN 1 ELSE 0 END) AS choice,
               SUM(CASE WHEN {_SECTION} = 'unresolved' THEN 1 ELSE 0 END) AS unresolved,
               SUM(CASE WHEN {_SECTION} = 'waiting' THEN 1 ELSE 0 END) AS waiting
        {_MEMBER_JOIN} AND {_FOLDER} = ?
        """,  # noqa: S608 - every fragment is a module constant
        (folder,),
    ).fetchone()
    if row is None or int(row["total"] or 0) < FLOOR:
        return None
    return Collection(
        folder=folder,
        total=int(row["total"]),
        ready=int(row["ready"]),
        choice=int(row["choice"]),
        unresolved=int(row["unresolved"]),
        waiting=int(row["waiting"]),
        companions=_companion_counts(conn, [folder]).get(folder, 0),
        companion_kinds=_companion_kinds(conn, [folder]).get(folder, []),
        categories=_category_counts(conn, [folder]).get(folder, []),
    )


def members(
    conn: sqlite3.Connection,
    folder: str,
    *,
    section: str = READY,
    page: int = 1,
    page_size: int = PAGE_SIZE,
) -> list[sqlite3.Row]:
    """One bounded page of one section of one collection.

    Sectioned *and* paged, because the two bound different things: a section
    is how a person reads the import, and the page is what keeps the DOM the
    same size whether the card held ten files or ten thousand.
    """
    if section not in SECTIONS:
        section = READY
    return list(
        conn.execute(
            f"""
            SELECT * FROM (
              SELECT i.id AS item_id, i.relpath AS relpath, i.size AS size,
                     i.state AS state, p.id AS proposal_id, p.category AS category,
                     p.confidence AS confidence, p.dest_relpath AS dest_relpath,
                     p.dest_root AS dest_root, p.status AS status,
                     p.evidence AS evidence, p.clean_name AS clean_name,
                     {_SECTION} AS section
              {_MEMBER_JOIN} AND {_FOLDER} = ?
            )
            WHERE section = ?
            ORDER BY relpath COLLATE NOCASE
            LIMIT ? OFFSET ?
            """,  # noqa: S608 - every fragment is a module constant
            (folder, section, page_size, max(0, (max(1, page) - 1) * page_size)),
        )
    )


def ready_proposal_ids(conn: sqlite3.Connection, folder: str) -> list[int]:
    """The proposals `Approve the ready ones` would approve.

    Resolved from the database at the moment the button is pressed, never
    passed in from the page. A list of ids in a form is a list of ids as they
    were when the page was drawn, and between then and now a file can have
    grown a duplicate report — which would make it a choice, and choices are
    exactly what bulk must not reach.
    """
    rows = conn.execute(
        f"""
        SELECT proposal_id FROM (
          SELECT p.id AS proposal_id, {_SECTION} AS section
          {_MEMBER_JOIN} AND {_FOLDER} = ?
        )
        WHERE section = 'ready'
        """,  # noqa: S608 - every fragment is a module constant
        (folder,),
    ).fetchall()
    return [int(row["proposal_id"]) for row in rows]


def _companion_counts(conn: sqlite3.Connection, folders: list[str]) -> dict[str, int]:
    """How many members of each folder are the companion half of a pair.

    A subtitle its video explains is not an unexplained file. Counting it as
    one is what made "5 unresolved" mean "three subtitles and two things you
    actually have to look at".
    """
    if not folders:
        return {}
    placeholders = ",".join("?" * len(folders))
    rows = conn.execute(
        f"""
        SELECT {_FOLDER} AS folder, COUNT(DISTINCT i.id) AS n
        FROM items i
        JOIN item_relationships r ON r.companion_item_id = i.id
        WHERE i.root='inbox' AND {live()} AND instr(i.relpath, '/') > 0
          AND {_FOLDER} IN ({placeholders})
        GROUP BY folder
        """,  # noqa: S608 - placeholders are counted; the rest are constants
        folders,
    ).fetchall()
    return {str(row["folder"]): int(row["n"]) for row in rows}


def _companion_kinds(
    conn: sqlite3.Connection, folders: list[str]
) -> dict[str, list[tuple[str, int]]]:
    """What kinds of pair are in each import, counted in SQL.

    "3 companions" is true and says nothing about what they are. Four RAW/JPEG
    pairs and three Live Photos is the same number told usefully — and it is
    read from persisted relationships, never worked out while the page draws.
    """
    if not folders:
        return {}
    placeholders = ",".join("?" * len(folders))
    rows = conn.execute(
        f"""
        SELECT {_FOLDER} AS folder, r.kind AS kind, COUNT(DISTINCT i.id) AS n
        FROM items i
        JOIN item_relationships r ON r.companion_item_id = i.id
        WHERE i.root='inbox' AND {live()} AND instr(i.relpath, '/') > 0
          AND {_FOLDER} IN ({placeholders})
        GROUP BY folder, kind
        ORDER BY n DESC, kind
        """,  # noqa: S608 - placeholders are counted; the rest are constants
        folders,
    ).fetchall()
    found: dict[str, list[tuple[str, int]]] = {}
    for row in rows:
        found.setdefault(str(row["folder"]), []).append(
            (str(row["kind"]), int(row["n"]))
        )
    return found


def _category_counts(
    conn: sqlite3.Connection, folders: list[str]
) -> dict[str, list[tuple[str, int]]]:
    """What kinds of thing are in each of these imports.

    A camera card is photographs *and* video, and an old drive is everything.
    Saying so is the point: one import does not get one category, and a summary
    that picked the commonest and called it the collection's kind would be
    classifying, which is not this module's job.
    """
    if not folders:
        return {}
    placeholders = ",".join("?" * len(folders))
    rows = conn.execute(
        f"""
        SELECT {_FOLDER} AS folder, COALESCE(p.category, '') AS category,
               COUNT(*) AS n
        {_MEMBER_JOIN} AND {_FOLDER} IN ({placeholders})
        GROUP BY folder, category
        ORDER BY n DESC, category
        """,  # noqa: S608 - placeholders are counted; the rest are constants
        folders,
    ).fetchall()
    found: dict[str, list[tuple[str, int]]] = {}
    for row in rows:
        found.setdefault(str(row["folder"]), []).append(
            (str(row["category"]), int(row["n"]))
        )
    return found
