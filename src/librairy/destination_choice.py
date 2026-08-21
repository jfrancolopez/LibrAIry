"""The second shape of choice: not which file wins, but where this belongs.

`merge.py` answers a collision. Two files want one name, and the person says
which one keeps it. That is a choice *between files*, and every question it
asks can be answered by looking at two files.

This is the other one, and it has been sitting in the audit as an observation
since the first release:

    Music/Rock/Prince/     3 albums, 12 files
    Music/Pop/Prince/      5 albums, 27 files

Nothing is colliding. Both folders are perfectly good folders. The only thing
wrong is that there are two of them, and **which one is right is a fact about
how somebody wants their library arranged** — it is not in the files, not in a
catalog, and not recoverable from counting. `audit_music` has always said so:
it reports the split and proposes no destination, because proposing one would
mean inventing the answer.

So the finding asks. The row lists the folders that really exist, with what is
in each, and the person presses one. Two rules keep that honest:

* **No folder is invented.** Every candidate is a directory the library
  already has, discovered from the index — never `Music/Other/Prince`, never a
  genre a catalog suggested.
* **No candidate is recommended.** The counts are shown because they are
  facts; they are not scored, ranked or starred. "27 files here and 12 there"
  is a reason a person might choose, not a reason LibrAIry has chosen.

**What happens after the choice is not new.** Picking a destination resolves
the only two things a merge did not know — which side is the source and which
is the destination — and from there it *is* a merge, planned by `merge.py`,
with the same collision questions, the same immutable plan, the same Commit
and the same Undo. There is no second merge implementation and no new executor
operation. Choosing the destination is orchestration; the machinery underneath
it is the machinery that already worked.

That makes this a two-stage choice, and the row stays `CHOICE` through both
stages: first the destination, then whatever collisions the chosen direction
turns out to have. Changing the destination clears the collision answers,
because "keep the existing one" means a different file once the two folders
have swapped roles — an answer that quietly survived a direction change would
be the person's words applied to a question they were not asked.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import PurePosixPath

from librairy.config import Settings
from librairy.planner import utc_now

# Findings whose correction is "these are two folders and one of them is the
# one you meant". Only `artist-split` today, and the set exists so the second
# one is a line rather than a rewrite.
DESTINATION_KINDS = frozenset({"artist-split"})

# More than this and the row stops being a choice and starts being a form. A
# split across three or four sections is the real shape; a split across twelve
# is a library-wide layout question that no single button answers.
MAX_CANDIDATES = 6


@dataclass(frozen=True)
class Candidate:
    """One folder this artist could live in, and what is in it now."""

    relpath: str
    files: int
    albums: int
    bytes: int

    @property
    def section(self) -> str:
        """`Music/Rock/Prince` -> `Music/Rock`. What distinguishes it."""
        return str(PurePosixPath(self.relpath).parent)

    @property
    def name(self) -> str:
        return PurePosixPath(self.relpath).name


def is_destination_finding(row: sqlite3.Row) -> bool:
    try:
        return row["kind"] in DESTINATION_KINDS
    except (KeyError, IndexError):
        return False


def subject(row: sqlite3.Row) -> str:
    """The artist this finding is about, off its own evidence."""
    for entry in _evidence(row):
        if entry.source == "filesystem" and entry.field == "artist":
            return str(entry.detail)
    parts = str(row["relpath"]).split("/")
    return parts[-2] if len(parts) >= 2 else str(row["relpath"])


def candidates(conn: sqlite3.Connection, row: sqlite3.Row) -> tuple[Candidate, ...]:
    """Every folder this artist actually has, read from the index.

    Not from the finding's evidence, which is a statement about the moment the
    audit ran. A folder emptied by hand since then is not somewhere to put
    anything, and one created since is a real alternative the row should offer.
    The evidence supplies the *name*; the index supplies the folders.
    """
    name = subject(row)
    if not name:
        return ()
    folders: dict[str, list[str]] = {}
    for item in conn.execute(
        "SELECT relpath, size FROM items"
        " WHERE root='library' AND missing_since IS NULL AND relpath LIKE 'Music/%'"
    ):
        folder = _artist_folder(str(item["relpath"]), name)
        if folder:
            folders.setdefault(folder, []).append(str(item["relpath"]))
    found = [
        Candidate(
            relpath=folder,
            files=len(members),
            albums=len({_album_of(folder, member) for member in members}),
            bytes=_bytes_under(conn, folder),
        )
        for folder, members in sorted(folders.items())
    ]
    return tuple(found) if len(found) <= MAX_CANDIDATES else ()


def _artist_folder(relpath: str, name: str) -> str:
    """`Music/Pop/Prince/Album/01.flac` -> `Music/Pop/Prince`, if it is theirs."""
    from librairy.audit_music import key

    parts = relpath.split("/")
    for depth in range(2, len(parts)):
        if key(parts[depth - 1]) == key(name):
            return "/".join(parts[:depth])
    return ""


def _album_of(folder: str, relpath: str) -> str:
    rest = relpath[len(folder) + 1 :].split("/")
    return rest[0] if len(rest) > 1 else ""


def _bytes_under(conn: sqlite3.Connection, folder: str) -> int:
    found = conn.execute(
        "SELECT COALESCE(SUM(size), 0) AS total FROM items"
        " WHERE root='library' AND missing_since IS NULL AND relpath LIKE ?",
        (f"{folder}/%",),
    ).fetchone()
    return int(found["total"] or 0)


# --- the answer ---------------------------------------------------------------------


def choose(
    conn: sqlite3.Connection, settings: Settings, finding_id: int, dest_relpath: str
) -> None:
    """Record which folder this artist should use, and forget the rest.

    Clearing the merge answers is not tidiness. `keep existing` names the file
    at the destination, and the destination has just changed sides — the same
    words now describe the opposite outcome. Re-asking is the only honest thing
    to do with an answer whose question moved.
    """
    from librairy.corrections import CorrectionRefused, load_finding
    from librairy.merge import clear_choices

    row = load_finding(conn, finding_id)
    if not is_destination_finding(row):
        raise CorrectionRefused("this finding is not a choice between folders")
    available = candidates(conn, row)
    if not any(candidate.relpath == dest_relpath for candidate in available):
        raise CorrectionRefused("that is not one of the folders this artist is in")
    if len(available) < 2:
        raise CorrectionRefused("this artist is only in one folder now")
    previous = chosen(conn, finding_id)
    if previous and previous != dest_relpath:
        clear_choices(conn, finding_id)
    conn.execute(
        "INSERT INTO destination_choices(audit_finding_id, dest_relpath, decided_at)"
        " VALUES (?, ?, ?)"
        " ON CONFLICT(audit_finding_id)"
        " DO UPDATE SET dest_relpath=excluded.dest_relpath, decided_at=excluded.decided_at",
        (finding_id, dest_relpath, utc_now()),
    )


def chosen(conn: sqlite3.Connection, finding_id: int) -> str:
    found = conn.execute(
        "SELECT dest_relpath FROM destination_choices WHERE audit_finding_id=?",
        (finding_id,),
    ).fetchone()
    return str(found["dest_relpath"]) if found else ""


def clear(conn: sqlite3.Connection, finding_id: int) -> None:
    from librairy.merge import clear_choices

    conn.execute("DELETE FROM destination_choices WHERE audit_finding_id=?", (finding_id,))
    clear_choices(conn, finding_id)


def selected(conn: sqlite3.Connection, row: sqlite3.Row) -> str:
    """The chosen folder, but only while it is still one of the candidates.

    A stored answer is not the same as a current one. Between choosing and
    approving, the folder can be renamed, emptied or merged into by something
    else — and an answer naming a folder that is no longer there has to fall
    back to the question, not to an approval nobody could review.
    """
    answer = chosen(conn, int(row["id"]))
    if not answer:
        return ""
    return answer if any(c.relpath == answer for c in candidates(conn, row)) else ""


# --- what it becomes ----------------------------------------------------------------


def plan_for(conn: sqlite3.Connection, settings: Settings, row: sqlite3.Row, *, verify: bool):  # noqa: ANN201
    """The merge this choice resolves to, or None while it is unanswered.

    Everything past this line is `merge.py`. The only thing this function
    contributes is the pair the merge planner could not work out for itself —
    which folder is the destination and which ones are the sources.
    """
    from librairy.merge import plan_merge

    target = selected(conn, row)
    if not target:
        return None
    sources = [c.relpath for c in candidates(conn, row) if c.relpath != target]
    if not sources:
        return None
    return plan_merge(conn, settings, row, verify=verify, target=target, sources=sources)


def _evidence(row: sqlite3.Row) -> list:
    from librairy.proposals import decode_evidence

    try:
        return decode_evidence(row["evidence"]) if row["evidence"] else []
    except (TypeError, ValueError):
        return []
