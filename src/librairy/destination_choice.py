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

* **No folder is invented.** The sections come from the detector's own
  evidence and the folders inside them come from the index — never
  `Music/Other/Prince`, never a genre a catalog suggested.
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
    """Every folder this artist has, among the sections the finding named.

    Two halves, and the split is the point. The **sections** come from the
    finding's evidence, which is where `audit_music` recorded the branches it
    actually found the artist under — so nothing is invented and no third home
    appears out of a rule. The **folders and their contents** come from the
    index, read now, so a section whose folder was emptied or renamed since the
    audit stops being offered rather than being offered and then refusing.

    Deliberately not a scan of everything under `Music/`. That answers the same
    question and reads the whole music half of the index once per row on a page
    that can hold fifty of them; these are prefix queries against the
    `(root, relpath)` index, bounded by the number of sections in the evidence.
    """
    name = subject(row)
    if not name:
        return ()
    found: list[Candidate] = []
    for section in _sections(row):
        folder = _artist_folder_under(conn, section, name)
        if not folder or any(candidate.relpath == folder for candidate in found):
            continue
        counted = _contents(conn, folder)
        if counted is not None:
            found.append(counted)
    found.sort(key=lambda candidate: candidate.relpath)
    #  Too many is not "show the first six": six of twelve is a choice with the
    #  other half hidden. A split that wide is a question about the layout of
    #  the whole library, and the row goes back to reporting it.
    return () if len(found) > MAX_CANDIDATES else tuple(found)


def _sections(row: sqlite3.Row) -> list[str]:
    """`Music/Pop`, `Music/Rock` — the branches the detector reported.

    Plus the branch of the finding's own path, which is the one it is anchored
    at and which the evidence does not always repeat.
    """
    sections = [
        str(entry.detail)
        for entry in _evidence(row)
        if entry.field in {"mostly under", "also under"} and entry.detail
    ]
    parts = str(row["relpath"]).split("/")
    if len(parts) >= 3:
        sections.append("/".join(parts[:-2]))
    return list(dict.fromkeys(sections))


def _artist_folder_under(conn: sqlite3.Connection, section: str, name: str) -> str:
    """The artist's folder inside one section, spelled as the disk spells it."""
    from librairy.audit_music import key

    for item in conn.execute(
        "SELECT relpath FROM items"
        " WHERE root='library' AND missing_since IS NULL AND relpath LIKE ?"
        " ORDER BY relpath",
        (f"{section}/%",),
    ):
        parts = str(item["relpath"]).split("/")
        depth = len(section.split("/"))
        if len(parts) > depth and key(parts[depth]) == key(name):
            return "/".join(parts[: depth + 1])
    return ""


def _contents(conn: sqlite3.Connection, folder: str) -> Candidate | None:
    files = 0
    albums: set[str] = set()
    total = 0
    for item in conn.execute(
        "SELECT relpath, size FROM items"
        " WHERE root='library' AND missing_since IS NULL AND relpath LIKE ?",
        (f"{folder}/%",),
    ):
        files += 1
        total += int(item["size"] or 0)
        rest = str(item["relpath"])[len(folder) + 1 :].split("/")
        if len(rest) > 1:
            albums.add(rest[0])
    if not files:
        return None
    return Candidate(relpath=folder, files=files, albums=len(albums), bytes=total)


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
    record(conn, finding_id, str(row["relpath"]), dest_relpath)


# --- the storage, shared with per-item choice ---------------------------------------
#
#  `destination_choices` holds one answer per *thing being placed*: the
#  finding's own folder for a whole-finding choice, one file for a per-item one.
#  A NULL destination means "leave this where it is", which `loose-tracks` needs
#  and `artist-split` never writes.


def record(
    conn: sqlite3.Connection, finding_id: int, relpath: str, dest_relpath: str | None
) -> None:
    conn.execute(
        "INSERT INTO destination_choices(audit_finding_id, relpath, dest_relpath, decided_at)"
        " VALUES (?, ?, ?, ?)"
        " ON CONFLICT(audit_finding_id, relpath)"
        " DO UPDATE SET dest_relpath=excluded.dest_relpath, decided_at=excluded.decided_at",
        (finding_id, relpath, dest_relpath, utc_now()),
    )


def answers(conn: sqlite3.Connection, finding_id: int) -> dict[str, str | None]:
    """Every answer given for this finding, keyed by what it is about."""
    return {
        str(row["relpath"]): (
            str(row["dest_relpath"]) if row["dest_relpath"] is not None else None
        )
        for row in conn.execute(
            "SELECT relpath, dest_relpath FROM destination_choices WHERE audit_finding_id=?",
            (finding_id,),
        )
    }


def forget(conn: sqlite3.Connection, finding_id: int, relpath: str = "") -> None:
    """Drop one answer, or all of this finding's answers when unnamed."""
    if relpath:
        conn.execute(
            "DELETE FROM destination_choices WHERE audit_finding_id=? AND relpath=?",
            (finding_id, relpath),
        )
        return
    conn.execute("DELETE FROM destination_choices WHERE audit_finding_id=?", (finding_id,))


def chosen(conn: sqlite3.Connection, finding_id: int) -> str:
    """The one answer a whole-finding choice has, whatever it is keyed by.

    Keyed by the finding's own relpath, but read without naming it: the caller
    that asks this has a plan row and not a finding row, and a `LIMIT 1` over a
    table that holds exactly one row for this kind is the honest query.
    """
    found = conn.execute(
        "SELECT dest_relpath FROM destination_choices WHERE audit_finding_id=? LIMIT 1",
        (finding_id,),
    ).fetchone()
    return str(found["dest_relpath"]) if found and found["dest_relpath"] else ""


def clear(conn: sqlite3.Connection, finding_id: int) -> None:
    from librairy.merge import clear_choices

    forget(conn, finding_id)
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
