"""Somebody moved a file outside LibrAIry. Agree about where it is now.

The scanner sees a filesystem, not a history of it. Move an album with Finder
and the next scan finds one set of paths gone and another set of paths it has
never seen, with no idea they are the same thirty files — so the old rows are
marked missing, the new ones are discovered as strangers, and everything hanging
off the old `items.id` (its measured metadata, its catalog identity, its
companions, every decision ever taken about it) points at a row describing a
path that is empty.

Nothing is lost. The index is simply describing yesterday.

**Exact bytes, or nothing.** A candidate exists when a missing row's fingerprint
is held by exactly one live row in the same root. Not a matching filename, not a
similar size, not a similar title — those would attach one file's history to
another file, which is the single worst thing this could do. Where the bytes
appear in more than one place the answer is *ambiguous* and stays that way until
a person settles it: picking the alphabetically-first copy would be a guess
wearing a decision's clothes.

**Recognition moves nothing.** It says *this file is here now*. The bytes are
not touched, not copied and not put back where LibrAIry would have filed them —
the person moved them there deliberately, and a program that quietly undid that
would be enforcing a taxonomy rather than keeping an index. What changes is one
`items` row's path, and everything referencing that row's identity follows for
free, because identity was never the path.

**Immutable things stay immutable.** History keeps the paths its operations
actually used. Approved plans keep the sources they named, and go stale rather
than being rewritten to point somewhere nobody approved. Undo is answered by the
existing preflight against what is on disk now.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import PurePosixPath

from librairy.live import dormant_optimization_result

#  One page of candidates. The same fifty every other list in LibrAIry uses.
PAGE_SIZE = 50

#  How many members a folder move names before it stops listing them.
NAMED = 6

#  The smallest number of files that reads as a folder having moved rather than
#  two files having been tidied.
GROUP_FLOOR = 2


class ReconcileRefused(RuntimeError):
    """This cannot be recognised, and the reason is worth reading."""


@dataclass(frozen=True)
class Candidate:
    """A missing row and the one live row holding exactly its bytes."""

    item_id: int
    root: str
    from_relpath: str
    to_item_id: int
    to_relpath: str
    fingerprint: str
    size: int = 0

    @property
    def name(self) -> str:
        return PurePosixPath(self.to_relpath).name or self.to_relpath

    @property
    def from_parent(self) -> str:
        return str(PurePosixPath(self.from_relpath).parent)

    @property
    def to_parent(self) -> str:
        return str(PurePosixPath(self.to_relpath).parent)

    @property
    def renamed(self) -> bool:
        """Whether the file's own name changed as well as its folder."""
        return (
            PurePosixPath(self.from_relpath).name
            != PurePosixPath(self.to_relpath).name
        )


#  How many of the places identical bytes turn up one row names.
#
#  Deliberately small. Bytes shared by half a dozen files are not a question
#  about which one moved — they are a library with copies in it, which the
#  duplicate workflow owns — and printing every path makes a row that reads
#  like a crisis instead of a shrug.
PLACES_SHOWN = 3


@dataclass(frozen=True)
class Ambiguity:
    """A missing row whose bytes are in more than one place."""

    item_id: int
    root: str
    from_relpath: str
    fingerprint: str
    places: tuple[str, ...] = ()
    total_places: int = 0

    @property
    def name(self) -> str:
        return PurePosixPath(self.from_relpath).name or self.from_relpath

    @property
    def more(self) -> int:
        return max(0, self.total_places - len(self.places))


@dataclass(frozen=True)
class Subtree:
    """A folder that appears to have moved as a unit."""

    root: str
    from_parent: str
    to_parent: str
    members: tuple[Candidate, ...] = ()

    @property
    def key(self) -> str:
        return f"{self.root}:{self.from_parent}"

    @property
    def total(self) -> int:
        return len(self.members)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(member.name for member in self.members[:NAMED])

    @property
    def more(self) -> int:
        return max(0, self.total - NAMED)


#  Missing rows paired with live rows holding the same bytes in the same root.
#
#  Same root deliberately. A library file whose bytes turn up in the inbox has
#  not moved — that is an arrival that happens to be a copy of something you
#  own, and the duplicate workflow already knows what to do with it.
#
#  `idx_items_fingerprint` carries the join, so this costs an index lookup per
#  missing row rather than a comparison against every other row.
_PAIRS = f"""
  SELECT gone.id AS item_id, gone.root AS root, gone.relpath AS from_relpath,
         gone.fingerprint AS fingerprint,
         found.id AS to_item_id, found.relpath AS to_relpath,
         found.size AS size,
         (SELECT COUNT(*) FROM items other
           WHERE other.fingerprint = gone.fingerprint
             AND other.missing_since IS NULL AND other.root = gone.root
             AND other.id <> gone.id) AS places,
         (SELECT COUNT(*) FROM items rival
           WHERE rival.fingerprint = gone.fingerprint
             AND rival.missing_since IS NOT NULL AND rival.root = gone.root
             AND rival.id <> gone.id) AS rivals
  FROM items gone
  JOIN items found ON found.fingerprint = gone.fingerprint
                  AND found.missing_since IS NULL
                  AND found.root = gone.root
                  AND found.id <> gone.id
  WHERE gone.missing_since IS NOT NULL
    AND gone.fingerprint IS NOT NULL AND gone.fingerprint <> ''
    AND NOT ({dormant_optimization_result("gone")})
"""

#  One-to-one, in both directions. One missing row, one live row holding its
#  bytes, and no *other* missing row that the same live row could equally well
#  belong to. Two copies of one file that both went missing, with one copy now
#  on disk, is not a move anybody can identify — it is two histories and one
#  file, and picking either would be a guess.
_UNAMBIGUOUS = f"SELECT * FROM ({_PAIRS}) WHERE places = 1 AND rivals = 0"


def candidates(
    conn: sqlite3.Connection, *, limit: int = PAGE_SIZE, offset: int = 0
) -> list[Candidate]:
    """Files whose new location the bytes establish beyond doubt."""
    rows = conn.execute(
        f"SELECT * FROM ({_UNAMBIGUOUS})"  # noqa: S608 - a module constant
        f" ORDER BY from_relpath LIMIT ? OFFSET ?",
        (limit, max(0, offset)),
    ).fetchall()
    return [_candidate(row) for row in rows]


def total(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute(
            f"SELECT COUNT(*) FROM ({_UNAMBIGUOUS})"  # noqa: S608 - a module constant
        ).fetchone()[0]
    )


def ambiguous(conn: sqlite3.Connection, *, limit: int = PAGE_SIZE) -> list[Ambiguity]:
    """Missing rows whose bytes are in several places. Never resolved here."""
    rows = conn.execute(
        f"""
        SELECT item_id, root, from_relpath, fingerprint,
               COUNT(*) AS places
          FROM ({_PAIRS})
         WHERE places > 1 OR rivals > 0
         GROUP BY item_id ORDER BY from_relpath LIMIT ?
        """,  # noqa: S608 - `_PAIRS` is a module constant
        (limit,),
    ).fetchall()
    if not rows:
        return []
    found: list[Ambiguity] = []
    for row in rows:
        places = tuple(
            str(place["relpath"])
            for place in conn.execute(
                "SELECT relpath FROM items WHERE fingerprint=? AND root=?"
                " AND missing_since IS NULL ORDER BY relpath LIMIT ?",
                (row["fingerprint"], row["root"], PLACES_SHOWN),
            )
        )
        total_places = int(
            conn.execute(
                "SELECT COUNT(*) FROM items WHERE fingerprint=? AND root=?"
                " AND missing_since IS NULL",
                (row["fingerprint"], row["root"]),
            ).fetchone()[0]
        )
        found.append(
            Ambiguity(
                item_id=int(row["item_id"]),
                root=str(row["root"]),
                from_relpath=str(row["from_relpath"]),
                fingerprint=str(row["fingerprint"]),
                places=places,
                total_places=total_places,
            )
        )
    return found


def subtrees(conn: sqlite3.Connection, *, limit: int = 20) -> list[Subtree]:
    """Folders whose whole contents appear at one new folder.

    A folder move is one decision somebody made, and asking for it thirty times
    is thirty chances to answer differently by accident. It is only offered when
    the correspondence is complete: every missing file in the old folder has an
    unambiguous partner, and every one of those partners landed in the same new
    folder under the same name. One member in doubt and the folder is not
    offered at all — the remaining files are still there to recognise one at a
    time, which is the version that cannot be wrong.
    """
    found = candidates(conn, limit=10_000)
    groups: dict[tuple[str, str, str], list[Candidate]] = {}
    for candidate in found:
        if candidate.renamed:
            continue
        groups.setdefault(
            (candidate.root, candidate.from_parent, candidate.to_parent), []
        ).append(candidate)
    complete: list[Subtree] = []
    for (root, from_parent, to_parent), members in groups.items():
        if len(members) < GROUP_FLOOR:
            continue
        if _stragglers(conn, root, from_parent, len(members)):
            continue
        complete.append(
            Subtree(
                root=root,
                from_parent=from_parent,
                to_parent=to_parent,
                members=tuple(sorted(members, key=lambda item: item.to_relpath)),
            )
        )
    complete.sort(key=lambda tree: (-tree.total, tree.from_parent))
    return complete[:limit]


def _stragglers(
    conn: sqlite3.Connection, root: str, from_parent: str, matched: int
) -> int:
    """Missing files in that folder this group does not account for.

    The check that makes a folder move safe to offer as one decision. Twenty-nine
    files that clearly moved together plus one whose bytes are in two places is
    not a folder that moved; it is a folder that moved and one question nobody
    has answered.
    """
    prefix = "" if from_parent in (".", "") else f"{from_parent}/"
    missing = int(
        conn.execute(
            f"""
            SELECT COUNT(*) FROM items gone
             WHERE gone.root = ? AND gone.missing_since IS NOT NULL
               AND gone.relpath LIKE ? ESCAPE '\\'
               AND instr(substr(gone.relpath, ?), '/') = 0
               AND NOT ({dormant_optimization_result("gone")})
            """,  # noqa: S608 - the predicate is a module constant
            (root, f"{_like(prefix)}%", len(prefix) + 1),
        ).fetchone()[0]
    )
    return max(0, missing - matched)


def _like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _candidate(row: sqlite3.Row) -> Candidate:
    return Candidate(
        item_id=int(row["item_id"]),
        root=str(row["root"]),
        from_relpath=str(row["from_relpath"]),
        to_item_id=int(row["to_item_id"]),
        to_relpath=str(row["to_relpath"]),
        fingerprint=str(row["fingerprint"] or ""),
        size=int(row["size"] or 0),
    )


def candidate_for(conn: sqlite3.Connection, item_id: int) -> Candidate | None:
    """The one unambiguous candidate for this missing row, or None."""
    row = conn.execute(
        f"SELECT * FROM ({_UNAMBIGUOUS}) WHERE item_id = ?",  # noqa: S608
        (item_id,),
    ).fetchone()
    return _candidate(row) if row is not None else None


# --- recognising one -----------------------------------------------------------

#  Rows that make the discovered file something other than a discovery.
#
#  A file the scanner found seconds ago carries nothing but derived facts. If
#  it carries an operation, a quarantine record, a remembered decision or an
#  optimization job, then it is not a stranger and merging two identities would
#  destroy whichever of them lost. Refused rather than resolved: this is exactly
#  the situation where the program has no basis for choosing.
_CLAIMS = (
    ("plan_ops", "item_id", "an approved or executed operation"),
    ("quarantine_entries", "item_id", "a quarantine record"),
    ("decision_events", "item_id", "a remembered decision"),
    ("optimization_jobs", "item_id", "an optimization job"),
    ("optimization_opportunities", "item_id", "a storage opportunity"),
    ("reconciliations", "item_id", "an earlier reconciliation"),
)

#  Derived rows belonging to the discovered file. Every one of them can be
#  measured again from the bytes, and every one of them is *about* the same
#  bytes the surviving row already describes — so removing them loses nothing
#  and keeping them would leave two rows claiming one file.
_DERIVED = (
    ("proposals", "item_id"),
    ("item_metadata", "item_id"),
    ("track_identity", "item_id"),
    ("content_extractions", "item_id"),
    ("vision_results", "item_id"),
    ("similar_media_choices", "item_id"),
    ("similar_media_flags", "item_id"),
    ("similar_media_flags", "similar_item_id"),
    ("duplicate_reports", "item_id"),
    ("duplicate_reports", "other_id"),
    ("item_relationships", "low_item_id"),
    ("item_relationships", "high_item_id"),
    ("audit_findings", "item_id"),
    ("backup_queue", "item_id"),
)


def recognize(conn: sqlite3.Connection, item_id: int, *, batch: str = "") -> Candidate:
    """Agree that this file is at its current path. Moves zero bytes.

    One `items` row changes its path and stops being missing. Everything that
    referenced that row — its measurements, its companions, the operations that
    ever named it, the decisions it taught — follows without being touched,
    because all of it was keyed on identity and never on the path.
    """
    from librairy.db import transaction
    from librairy.planner import utc_now
    from librairy.search import sync_search_item

    candidate = candidate_for(conn, item_id)
    if candidate is None:
        raise ReconcileRefused(
            "the bytes for that file are not at exactly one other path any more"
        )
    claimed = _claimed(conn, candidate.to_item_id)
    if claimed:
        raise ReconcileRefused(
            f"{candidate.name} at its new path already carries {claimed}, so "
            f"LibrAIry will not merge the two records"
        )
    found = conn.execute(
        "SELECT size, mtime_ns, last_seen_at FROM items WHERE id=?",
        (candidate.to_item_id,),
    ).fetchone()
    if found is None:  # pragma: no cover - the candidate query just read it
        raise ReconcileRefused("that file is no longer indexed at its new path")
    with transaction(conn):
        for table, column in _DERIVED:
            conn.execute(
                f"DELETE FROM {table} WHERE {column} = ?",  # noqa: S608 - constants
                (candidate.to_item_id,),
            )
        conn.execute("DELETE FROM search_fts WHERE rowid=?", (candidate.to_item_id,))
        conn.execute("DELETE FROM items WHERE id=?", (candidate.to_item_id,))
        conn.execute(
            "UPDATE items SET relpath=?, size=?, mtime_ns=?, last_seen_at=?,"
            " missing_since=NULL WHERE id=?",
            (
                candidate.to_relpath,
                int(found["size"] or 0),
                int(found["mtime_ns"] or 0),
                str(found["last_seen_at"] or utc_now()),
                candidate.item_id,
            ),
        )
        sync_search_item(conn, candidate.item_id)
        conn.execute(
            "INSERT INTO reconciliations(item_id, kind, from_root, from_relpath,"
            " to_root, to_relpath, fingerprint, batch, decided_at)"
            " VALUES (?, 'moved', ?, ?, ?, ?, ?, ?, ?)",
            (
                candidate.item_id,
                candidate.root,
                candidate.from_relpath,
                candidate.root,
                candidate.to_relpath,
                candidate.fingerprint,
                batch,
                utc_now(),
            ),
        )
    return candidate


def recognize_subtree(conn: sqlite3.Connection, key: str) -> list[Candidate]:
    """Agree about a whole folder at once. Still zero bytes moved.

    Re-derived from the bytes at the moment it is pressed rather than trusting
    the key that was rendered: the page may have been open for a while, and a
    folder that has stopped being a clean one-to-one correspondence must stop
    being offered as one decision.
    """
    from librairy.planner import utc_now

    tree = next((item for item in subtrees(conn, limit=10_000) if item.key == key), None)
    if tree is None:
        raise ReconcileRefused(
            "that folder no longer looks like a single move — recognise the "
            "files individually instead"
        )
    batch = f"{tree.key}@{utc_now()}"
    return [recognize(conn, member.item_id, batch=batch) for member in tree.members]


def _claimed(conn: sqlite3.Connection, item_id: int) -> str:
    """What the discovered row already carries, in words, or ""."""
    for table, column, label in _CLAIMS:
        row = conn.execute(
            f"SELECT 1 FROM {table} WHERE {column} = ? LIMIT 1",  # noqa: S608 - constants
            (item_id,),
        ).fetchone()
        if row is not None:
            return label
    row = conn.execute(
        "SELECT status FROM proposals WHERE item_id=? AND status IN"
        " ('approved','committed') LIMIT 1",
        (item_id,),
    ).fetchone()
    return "a decision you already approved" if row is not None else ""


# --- what has been recognised already -----------------------------------------


@dataclass(frozen=True)
class Recognised:
    """One past reconciliation, for a page that lists them."""

    kind: str
    root: str
    from_relpath: str
    to_relpath: str
    when: str
    batch: str = ""
    total: int = 1

    @property
    def name(self) -> str:
        return PurePosixPath(self.to_relpath).name or self.to_relpath


def recognised(
    conn: sqlite3.Connection, *, limit: int = PAGE_SIZE
) -> list[Recognised]:
    """What has been agreed, newest first, with folder moves counted as one."""
    rows = conn.execute(
        """
        SELECT kind, from_root AS root, MIN(from_relpath) AS from_relpath,
               MIN(to_relpath) AS to_relpath, MAX(decided_at) AS when_at,
               batch, COUNT(*) AS total
          FROM reconciliations
         GROUP BY CASE WHEN batch = '' THEN 'row:' || id ELSE 'batch:' || batch END
         ORDER BY when_at DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [
        Recognised(
            kind=str(row["kind"]),
            root=str(row["root"]),
            from_relpath=str(row["from_relpath"]),
            to_relpath=str(row["to_relpath"]),
            when=str(row["when_at"] or ""),
            batch=str(row["batch"] or ""),
            total=int(row["total"]),
        )
        for row in rows
    ]
