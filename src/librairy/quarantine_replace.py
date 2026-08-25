"""Quarantine as the other half of a comparison, rather than a dead end.

    Library     Music/Rock/Queen/A Night at the Opera/01 - Song.flac
    Quarantine  01 - Song.mp3   — set aside after comparing with that file

Until now a held file had one answer: `Restore`, meaning *bring this back as
well*. That is a real answer and it stays. But the row already knows something
it was not using — **what this was compared with, and what is standing in its
place** — and with that, the reciprocal answer is available:

    Use this instead    the file now in the library goes to Quarantine, and
                        this one takes its place in the same logical slot

Which is a replacement, not a restore, and the difference is the whole module.
A restore moves one file and leaves the other where it is; both end up active.
A replacement moves two, in an order, and exactly one ends up active. Calling
it `Restore` on the Commit card would describe the wrong outcome at the last
moment before bytes move.

Nothing here is new machinery. It is the same coherent plan the cross-root
comparison already builds, in the same order, for the same reason: preserve
first, admit second, and if the second cannot happen the first must not either.
`plans.coherent` is what says so, and it is checked before execution rather
than trusted from when the plan was written.

Three refusals, and each one is a way of not acting on a stale belief:

* **the slot must still be what it was.** The active representation may have
  been moved, renamed or replaced again since this file was set aside. The
  destination comes from where that file is *now*, never from the path this
  file remembers coming from — restoring to a remembered path is what `Restore`
  does, and it is the wrong answer to "use this instead".
* **both files must still be the files.** Fingerprints are checked against the
  index before the plan is written. A re-encode on either side means the
  comparison somebody is answering is not the comparison in front of them.
* **exact duplicates are not offered this at all.** Two files with the same
  bytes have no representation to prefer, and a swap between them would move
  two files to achieve nothing. That workflow keeps its own semantics — see
  `quarantine.remember_restored_comparison`, which draws the same line.

The flip back is not a special case either. Once this runs, the file that left
the library is a held representation with a comparison behind it, so it offers
`Use this instead` in its turn — and pressing it swaps them again, as one more
recorded decision rather than as an undo of the first.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import PurePosixPath

from librairy.config import Settings
from librairy.fingerprint import blake2b_file
from librairy.paths import PathValidationError, validate_dest, validate_relpath
from librairy.quarantine import QuarantineError
from librairy.replacement import approve_coherent, swap_specs

#  What the Commit card and the Quarantine row both call it. One decision, one
#  word, and deliberately not `Restore` — see the module docstring.
LABEL = "Use this instead"


@dataclass(frozen=True)
class Replacement:
    """A held representation, the filed one it would displace, and where."""

    entry_id: int
    held_relpath: str
    held_size: int
    active_item_id: int
    active_relpath: str
    active_size: int
    dest_relpath: str

    @property
    def held_name(self) -> str:
        return PurePosixPath(self.held_relpath).name

    @property
    def active_name(self) -> str:
        return PurePosixPath(self.active_relpath).name

    @property
    def same_path(self) -> bool:
        """Would the arriving file land exactly where the filed one is now?

        True whenever the two share an extension, which is the common case:
        two rips of one track, two exports of one photo. It is not an
        overwrite — the filed copy is preserved by the operation before it —
        but it is the case where the order of the two operations is the only
        thing keeping the bytes.
        """
        return self.dest_relpath == self.active_relpath


def replacement_for(conn: sqlite3.Connection, entry_id: int) -> Replacement | None:
    """What replacing with this held file would mean, by entry id."""
    entry = conn.execute(
        "SELECT * FROM quarantine_entries WHERE id=?", (entry_id,)
    ).fetchone()
    return None if entry is None else replacement_of(conn, entry)


def replacement_of(conn: sqlite3.Connection, entry: sqlite3.Row) -> Replacement | None:
    """The same, for a row the caller already has.

    None rather than an error, because this is asked to decide whether to draw
    a button. Every reason to say no is a reason the button should not be
    there: the wrong kind of quarantine, nothing left to compare with, a
    counterpart that has itself been quarantined or has gone missing.

    Two indexed lookups, and only for a held file that came from a comparison —
    the Quarantine page draws a bounded number of rows and most of them leave
    on the first line.
    """
    if entry["restored_at"] is not None:
        return None
    if not _is_comparison(conn, entry):
        return None
    held = conn.execute(
        "SELECT * FROM items WHERE id=?", (entry["item_id"],)
    ).fetchone()
    if held is None or held["root"] != "quarantine" or held["missing_since"]:
        return None
    partner = entry["duplicate_of"]
    if partner is None:
        return None
    active = conn.execute(
        "SELECT * FROM items WHERE id=? AND root='library' AND missing_since IS NULL",
        (int(partner),),
    ).fetchone()
    if active is None:
        return None
    #  The filed version would go to Quarantine to make room, which is the one
    #  thing a protected folder exists to refuse. No control at all rather than
    #  one that can only produce an error — the same rule every other button on
    #  this page follows.
    from librairy.format_policy import resolve

    if resolve(conn, str(active["relpath"])).protected_original:
        return None
    return Replacement(
        entry_id=int(entry["id"]),
        held_relpath=str(held["relpath"]),
        held_size=int(held["size"] or 0),
        active_item_id=int(active["id"]),
        active_relpath=str(active["relpath"]),
        active_size=int(active["size"] or 0),
        dest_relpath=_destination(str(active["relpath"]), str(held["relpath"])),
    )


def _is_comparison(conn: sqlite3.Connection, entry: sqlite3.Row) -> bool:
    """Only files held because two representations were compared.

    An exact duplicate is deliberately excluded. The bytes are the same, so
    there is no version to prefer, and a swap would move two files to leave the
    library exactly as it was.
    """
    from librairy.quarantine import is_preserved_original

    if is_preserved_original(entry):
        #  A preserved original belongs to an optimization job, whose own
        #  reversal moves both files in an order only that job knows.
        return False
    return str(entry["reason"] or "") == "similar_media"


def _destination(active_relpath: str, held_relpath: str) -> str:
    """The slot the active representation occupies, with this file's extension.

    Taken from where the filed copy is *now*, never from the path the held file
    remembers. The comparison established that these are the same thing; the
    library has since decided where that thing lives, and the remembered path
    may be somewhere it was moved out of a month ago.
    """
    from librairy.arrival_comparison import _destination as slot

    return slot(active_relpath, held_relpath)


def describe(conn: sqlite3.Connection, entry: sqlite3.Row) -> dict | None:
    """The replacement in the words the page uses, or None if it is not on."""
    found = replacement_of(conn, entry)
    if found is None:
        return None
    from librairy.relationship_impact import not_carried
    from librairy.web.quarantine import human_size

    return {
        "label": LABEL,
        "held_name": found.held_name,
        "held_size": human_size(found.held_size),
        "active_name": found.active_name,
        "active_relpath": found.active_relpath,
        "active_size": human_size(found.active_size),
        "dest_relpath": found.dest_relpath,
        "same_path": found.same_path,
        #  These two are already established as versions of one filed thing —
        #  that is what put this control on the row — so the only remaining
        #  question is which representation, and the owner has answered it.
        #  Preselects nothing here (there is one button), but says so.
        "preferred": _preferred(conn, found),
        #  What the outgoing version is paired with and the incoming one is
        #  not. The pairing is deliberately not transferred — it was
        #  established from the bytes being replaced — and an invariant nobody
        #  can see is one somebody will eventually "fix".
        "not_carried": not_carried(
            conn,
            replaced_item_id=found.active_item_id,
            replacing_item_id=int(entry["item_id"]),
        ),
    }


def _preferred(conn: sqlite3.Connection, found) -> str:  # noqa: ANN001
    """The sentence to print when the held copy is the format the owner wants."""
    from librairy.format_preference import prefer_among, sentence

    wanted = prefer_among(conn, [found.held_relpath, found.active_relpath])
    return sentence(conn) if wanted == found.held_relpath else ""


def request_replacement(
    conn: sqlite3.Connection, settings: Settings, entry_id: int
) -> str:
    """Write the swap down as an approved, unexecuted, coherent plan.

    Two operations and one decision. The filed copy is preserved first and the
    held one admitted second, so there is no moment at which the library has
    neither — and the plan is coherent, so the executor revalidates both
    together and runs both or runs neither.
    """
    from librairy.quarantine_requests import pending_request

    found = replacement_for(conn, entry_id)
    if found is None:
        raise QuarantineError(
            "there is no filed version of this to replace any more"
        )
    if pending_request(conn, entry_id) is not None:
        raise QuarantineError("a decision on this file is already waiting for Commit")
    if _claimed(conn, found):
        raise QuarantineError("one of these files is already waiting for Commit")
    _assert_current(conn, settings, "quarantine", found.held_relpath)
    _assert_current(conn, settings, "library", found.active_relpath)
    try:
        destination = validate_dest(settings.library_dir, found.dest_relpath)
    except (PathValidationError, ValueError) as exc:
        raise QuarantineError(
            f"{found.held_name} has no safe destination: {exc}"
        ) from exc
    if destination.exists() and not found.same_path:
        #  Something that is not the copy being replaced is standing where this
        #  would land. Renumbering it would invent a name nobody approved.
        raise QuarantineError(
            f"{PurePosixPath(found.dest_relpath).name} already exists and is not "
            f"the copy you are replacing"
        )
    specs = swap_specs(
        preserve=found.active_relpath,
        source_root="quarantine",
        source_relpath=found.held_relpath,
        dest_relpath=found.dest_relpath,
    )
    return approve_coherent(
        conn,
        settings,
        specs,
        error=QuarantineError,
        clash="a decision on one of these files was recorded a moment ago",
        entry_id=entry_id,
    )


def _claimed(conn: sqlite3.Connection, found: Replacement) -> bool:
    from librairy.correction_state import ACTIVE_PLAN_STATUSES

    statuses = ",".join("?" * len(ACTIVE_PLAN_STATUSES))
    return (
        conn.execute(
            f"SELECT 1 FROM plan_ops o JOIN plans p ON p.id = o.plan_id"  # noqa: S608
            f" WHERE o.src_relpath IN (?, ?) AND p.status IN ({statuses}) LIMIT 1",
            (found.held_relpath, found.active_relpath, *ACTIVE_PLAN_STATUSES),
        ).fetchone()
        is not None
    )


def _assert_current(
    conn: sqlite3.Connection, settings: Settings, root: str, relpath: str
) -> None:
    """Still the bytes the comparison was about, on both sides.

    A stale belief on either side is the same failure: somebody answers a
    question about two files and a third file is what moves.
    """
    row = conn.execute(
        "SELECT fingerprint FROM items WHERE root=? AND relpath=?", (root, relpath)
    ).fetchone()
    if row is None or not row["fingerprint"]:
        raise QuarantineError(f"{PurePosixPath(relpath).name} has not been indexed")
    base = settings.quarantine_dir if root == "quarantine" else settings.library_dir
    try:
        path = validate_relpath(base, relpath, kind=root)
    except PathValidationError as exc:
        raise QuarantineError(
            f"{PurePosixPath(relpath).name} is not a {root} path"
        ) from exc
    if not path.is_file() or blake2b_file(path) != row["fingerprint"]:
        raise QuarantineError(
            f"{PurePosixPath(relpath).name} changed since these were compared"
        )
