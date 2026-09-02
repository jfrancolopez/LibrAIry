"""Does the index still describe the library?

Browse shows what is on disk. Search answers from the index. Those are two
different questions on purpose, and most of the time they give the same answer
— but nothing rescans the library on a schedule (the worker only watches the
inbox), so a file copied straight in over SMB is browsable and unfindable, and
until now there was no way to know that except by noticing a search come back
empty for something you were looking straight at.

This reports the difference. It does not repair it: no row is deleted, no scan
is triggered, nothing is indexed because someone opened a page. Observation
only.

**Measured away from the page, and reported with its age.** Comparing the whole
library against the whole index is not a render's work: it reads every library
row into memory and every visible file off the disk, so at a million files it
was a second of database time on Browse before the filesystem walk that the
scale harness — which has no files on disk — did not even simulate. It is the
same argument that moved `PRAGMA quick_check` and the FTS integrity check off
their pages, and it takes the same shape: the worker measures it on an idle
cycle, the verdict is recorded with the moment it was taken, and a page shows
that verdict and how old it is. Before the first measurement it says nobody has
looked yet, which is a fact about the check rather than about the library.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import PurePosixPath

from librairy.config import Settings
from librairy.live import dormant_optimization_result
from librairy.reserved import RESERVED_TOP, is_reserved
from librairy.scanner import visible_files

# Enough to recognise which files are meant without turning a status line into
# a directory listing. The counts are always exact; only the examples are cut.
SAMPLE = 5


#  Recorded beside the other maintenance verdicts, in the table the worker
#  already owns. See `web/health.py` for the same arrangement over
#  `PRAGMA quick_check`.
CONSISTENCY_KEY = "library_consistency"


@dataclass(frozen=True)
class LibraryConsistency:
    """What the filesystem holds, what the index holds, and the gap."""

    physical_files: int
    indexed_files: int
    unindexed_files: int
    missing_files: int
    unindexed_sample: tuple[str, ...] = ()
    missing_sample: tuple[str, ...] = ()
    #  Physical files inside the namespace LibrAIry reserves for its own
    #  bookkeeping. Their own bucket, because they are neither drift nor
    #  ordinary media: scanning would not index them and telling somebody to
    #  scan would waste their time.
    reserved_files: int = 0
    reserved_sample: tuple[str, ...] = ()

    @property
    def matches(self) -> bool:
        return (
            not self.unindexed_files
            and not self.missing_files
            and not self.reserved_files
        )


def library_consistency(
    conn: sqlite3.Connection,
    settings: Settings,
    on_disk: list[str] | None = None,
    sample: int = SAMPLE,
) -> LibraryConsistency:
    """Compare the visible library against the item rows that describe it.

    `on_disk` lets a caller that has already walked the library pass the result
    in rather than paying for it twice — Browse renders the root tiles from the
    same list.

    Both sides are library-relative POSIX paths: `items.relpath` is written by
    `scan_root` as `path.relative_to(root).as_posix()`, and `visible_files`
    builds its paths the same way from the same directory entries. Comparing a
    `Path` against a stored string, or normalising one side and not the other,
    is how a checker like this invents drift that is not there.
    """
    if on_disk is None:
        on_disk = visible_files(settings.library_dir, settings.ignore_patterns)
    #  Pulled out before either comparison. A file here is a real conflict with
    #  a name LibrAIry needs, and counting it as unindexed would attach the
    #  remedy "scan the library", which would not index it either.
    reserved = sorted(path for path in on_disk if is_reserved(path))
    present = {path for path in on_disk if not is_reserved(path)}
    # Missing rows are included on purpose: a row describing a file that is not
    # there is exactly the drift this panel exists to report, and the note below
    # explains it rather than offering a command that would not help.
    #
    # The one exception is a dormant optimization result. Its file is in the
    # job's staging directory by design, one click from coming back, and the
    # job's own record says where — so counting it here would be inventing a
    # problem that is already accounted for somewhere the user can see.
    indexed = {
        row["relpath"]
        for row in conn.execute(
            "SELECT relpath FROM items i WHERE root='library'"
            f" AND NOT ({dormant_optimization_result()})"
        )
    }
    unindexed = sorted(present - indexed)
    missing = sorted(indexed - present)
    return LibraryConsistency(
        physical_files=len(present),
        indexed_files=len(present & indexed),
        unindexed_files=len(unindexed),
        missing_files=len(missing),
        unindexed_sample=tuple(unindexed[:sample]),
        missing_sample=tuple(missing[:sample]),
        reserved_files=len(reserved),
        reserved_sample=tuple(reserved[:sample]),
    )


def record_consistency(conn: sqlite3.Connection, state: LibraryConsistency) -> None:
    """Remember what the last comparison found, so a page need not repeat it."""
    from librairy.planner import utc_now

    conn.execute(
        "INSERT INTO worker_state(key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (
            CONSISTENCY_KEY,
            json.dumps(
                {
                    "at": utc_now(),
                    "physical_files": state.physical_files,
                    "indexed_files": state.indexed_files,
                    "unindexed_files": state.unindexed_files,
                    "missing_files": state.missing_files,
                    "unindexed_sample": list(state.unindexed_sample),
                    "missing_sample": list(state.missing_sample),
                    "reserved_files": state.reserved_files,
                    "reserved_sample": list(state.reserved_sample),
                }
            ),
        ),
    )


def recorded_consistency(
    conn: sqlite3.Connection,
) -> tuple[LibraryConsistency, str] | None:
    """The last comparison and when it was taken, or None if nobody has looked."""
    row = conn.execute(
        "SELECT value FROM worker_state WHERE key=?", (CONSISTENCY_KEY,)
    ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(str(row["value"]))
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return None
    return (
        LibraryConsistency(
            physical_files=int(payload.get("physical_files", 0)),
            indexed_files=int(payload.get("indexed_files", 0)),
            unindexed_files=int(payload.get("unindexed_files", 0)),
            missing_files=int(payload.get("missing_files", 0)),
            unindexed_sample=tuple(payload.get("unindexed_sample", ())),
            missing_sample=tuple(payload.get("missing_sample", ())),
            reserved_files=int(payload.get("reserved_files", 0)),
            reserved_sample=tuple(payload.get("reserved_sample", ())),
        ),
        str(payload.get("at", "")),
    )


def consistency_panel(conn: sqlite3.Connection) -> dict[str, object]:
    """What Browse draws: the last comparison, or a note that there is none.

    One statement, and it does not grow with the library. What it replaced read
    every library row into a set on every render.
    """
    from librairy.web.health import _ago

    found = recorded_consistency(conn)
    if found is None:
        return unmeasured_view()
    state, at = found
    return consistency_view(state, _ago(at) if at else "")


def unmeasured_view() -> dict[str, object]:
    """What Browse says before anything has compared the two.

    Not a warning. Nobody has looked, which is a fact about the check and not
    about the library, and a warning without evidence is one people learn to
    dismiss.
    """
    return {
        "matches": True,
        "measured": False,
        "taken": "",
        "physical_files": 0,
        "indexed_files": 0,
        "notes": [],
        "summary": "The library and the index have not been compared yet.",
    }


def consistency_view(state: LibraryConsistency, taken: str = "") -> dict[str, object]:
    """The same numbers, phrased for a person.

    Deliberately factual. "Everything is synchronized" would be a claim about
    the future — this was true when the page was rendered and nothing watches
    it afterwards — so the wording stays in the past tense of a measurement.

    The remedies have to be exact, and they are not the same remedy — which is
    why each note carries its own instead of one line at the bottom.

    `librairy index rebuild` rebuilds the search index from item rows that
    already exist. It discovers nothing, so naming it for an unscanned file
    would waste the owner's time; scanning is what finds a file, and scanning
    the library is also what teaches LibrAIry the layout it already keeps.

    A stale row gets no command at all, because there is no honest one to give.
    A scan sets `missing_since` and stops there; the row survives on purpose,
    carrying the decisions made about that file, and inventing a delete here
    would be exactly the kind of unasked-for repair this page exists to avoid.
    So the note explains the state rather than pointing at a command that would
    not change it.
    """
    notes: list[dict[str, str | None]] = []
    if state.unindexed_files:
        count = state.unindexed_files
        notes.append(
            {
                "text": (
                    f"{count} {_files(count)} {'is' if count == 1 else 'are'} visible in Browse "
                    "but not searchable yet, because nothing has scanned them. Everything "
                    "LibrAIry can see it can index — there is no file type it refuses — so this "
                    "only means not scanned yet."
                ),
                "remedy": "librairy scan --root library",
            }
        )
    if state.missing_files:
        count = state.missing_files
        notes.append(
            {
                "text": (
                    f"{count} indexed {'entry has' if count == 1 else 'entries have'} no file on "
                    "disk — moved or removed outside LibrAIry. Search no longer returns them and "
                    "Browse never did; the record is kept for its history and its evidence, and "
                    "a scan will pick the file up again if it comes back. Nothing here will "
                    "delete a record for you."
                ),
                "remedy": None,
            }
        )
    if state.reserved_files:
        count = state.reserved_files
        notes.append(
            {
                "text": (
                    f"{count} {_files(count)} {'sits' if count == 1 else 'sit'} inside "
                    f"{RESERVED_TOP}, a name LibrAIry keeps for its own bookkeeping. "
                    f"{'It has' if count == 1 else 'They have'} been left exactly "
                    "where they are and will not be indexed, moved or deleted — "
                    "rename that folder and they will be picked up on the next scan."
                ),
                # No command. Scanning will not index them, and offering one that
                # does nothing is worse than explaining the state.
                "remedy": None,
            }
        )
    return {
        "matches": state.matches,
        #  A measurement, and when it was taken. Browse used to compute this on
        #  every render, which made it true *now* and made Browse pay for the
        #  whole library to say so.
        "measured": True,
        "taken": taken,
        # The headline, short enough to sit under the page title.
        "summary": (
            f"{state.physical_files:,} {_files(state.physical_files)} · index up to date"
            if state.matches
            else f"{state.physical_files:,} {_files(state.physical_files)} · "
            + " · ".join(
                part
                for part in (
                    f"{state.unindexed_files} not indexed" if state.unindexed_files else "",
                    f"{state.missing_files} missing on disk" if state.missing_files else "",
                    f"{state.reserved_files} in a reserved folder"
                    if state.reserved_files
                    else "",
                )
                if part
            )
        ),
        "notes": notes,
        "examples": [
            *({"label": "Not indexed", "path": path} for path in state.unindexed_sample),
            *({"label": "Missing on disk", "path": path} for path in state.missing_sample),
            *({"label": "Reserved name", "path": path} for path in state.reserved_sample),
        ],
        "more": max(state.unindexed_files - len(state.unindexed_sample), 0)
        + max(state.missing_files - len(state.missing_sample), 0)
        + max(state.reserved_files - len(state.reserved_sample), 0),
    }


def _files(count: int) -> str:
    return "file" if count == 1 else "files"


def top_level(relpath: str) -> str:
    """The library folder a relative path belongs to, or "" for a loose file."""
    parts = PurePosixPath(relpath).parts
    return parts[0] if len(parts) > 1 else ""
