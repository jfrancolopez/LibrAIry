"""Does the index still describe the library?

Browse shows what is on disk. Search answers from the index. Those are two
different questions on purpose, and most of the time they give the same answer
— but nothing rescans the library on a schedule (the worker only watches the
inbox), so a file copied straight in over SMB is browsable and unfindable, and
until now there was no way to know that except by noticing a search come back
empty for something you were looking straight at.

This reports the difference. It does not repair it: no row is deleted, no scan
is triggered, nothing is indexed because someone opened a page. Observation
only, calculated per request off the same walk Browse already does.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import PurePosixPath

from librairy.config import Settings
from librairy.scanner import visible_files

# Enough to recognise which files are meant without turning a status line into
# a directory listing. The counts are always exact; only the examples are cut.
SAMPLE = 5


@dataclass(frozen=True)
class LibraryConsistency:
    """What the filesystem holds, what the index holds, and the gap."""

    physical_files: int
    indexed_files: int
    unindexed_files: int
    missing_files: int
    unindexed_sample: tuple[str, ...] = ()
    missing_sample: tuple[str, ...] = ()

    @property
    def matches(self) -> bool:
        return not self.unindexed_files and not self.missing_files


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
    present = set(on_disk)
    indexed = {
        row["relpath"]
        for row in conn.execute("SELECT relpath FROM items WHERE root='library'")
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
    )


def consistency_view(state: LibraryConsistency) -> dict[str, object]:
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
    return {
        "matches": state.matches,
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
                )
                if part
            )
        ),
        "notes": notes,
        "examples": [
            *({"label": "Not indexed", "path": path} for path in state.unindexed_sample),
            *({"label": "Missing on disk", "path": path} for path in state.missing_sample),
        ],
        "more": max(state.unindexed_files - len(state.unindexed_sample), 0)
        + max(state.missing_files - len(state.missing_sample), 0),
    }


def _files(count: int) -> str:
    return "file" if count == 1 else "files"


def top_level(relpath: str) -> str:
    """The library folder a relative path belongs to, or "" for a loose file."""
    parts = PurePosixPath(relpath).parts
    return parts[0] if len(parts) > 1 else ""
