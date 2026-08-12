"""Turning an audit finding into a correction the executor can be trusted with.

The Library Audit stops at "this looks wrong". This module is the bridge from
there to the existing immutable plan, and it exists as its own file because it
is a different kind of reasoning: `audit.py` reads a library and forms an
opinion, this decides whether an opinion is still safe to act on.

Nothing here executes anything. It produces `OperationSpec`s for the one
executor LibrAIry has, whose guarantees — containment, fingerprint match at
commit time, collisions never overwriting, an immutable approved plan, an
exact undo — are the reason a library correction is possible at all. Those are
proven in `test_library_to_library.py` and are not restated here.

Two things had to be true before any of this could be offered:

* **A finding is a statement about a file at a moment.** It carries the
  fingerprint it was made against, and it is only executable while the file
  still matches. A file that has been re-tagged, replaced or moved by hand
  since the audit gets *Needs re-analysis* — never a silent re-classification,
  because the finding is the evidence the user is being asked to approve.
* **Companions travel with their media.** A correction that moves
  `05 - Song.flac` and leaves `05 - Song.lrc` behind has broken something the
  user did not ask to have broken. Every companion move is an operation in the
  same plan, visible before Commit, undone by the same undo.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from librairy.config import Settings
from librairy.fingerprint import blake2b_file
from librairy.paths import PathValidationError, validate_relpath

# The three answers worth telling apart. A fourth ("moved to somewhere else in
# the library") was considered and dropped: the audited path is what the
# finding describes, and "not there any more" is one fact however it happened.
CURRENT = "current"
STALE = "stale"
MISSING = "missing"

STATE_LABEL = {
    CURRENT: "Ready",
    STALE: "Needs re-analysis",
    MISSING: "Not on disk",
}


class CorrectionRefused(RuntimeError):
    """A correction that must not be turned into a plan, and why."""


def finding_state(settings: Settings, row: sqlite3.Row) -> str:
    """Is this finding still a true statement about the file it describes?

    Reads the filesystem and the row it was given. No database write, because
    this is called while rendering Review and a page that writes is how the
    portal used to return "System Fault" during a scan.

    The fingerprint is the whole test. `mtime` is not consulted: copying a
    library between disks rewrites every timestamp without changing a byte,
    and a file can be replaced while its timestamps are preserved. Size is used
    only as a fast negative — a different length is a different file, and
    proves staleness without reading the bytes.
    """
    try:
        path = validate_relpath(settings.library_dir, row["relpath"], kind="finding")
    except PathValidationError:
        # A finding whose path no longer resolves inside the library is not a
        # thing to correct; it is a thing to look at.
        return MISSING
    if not path.exists():
        return MISSING
    if path.is_dir():
        # Album- and folder-level findings (missing artwork, naming) describe a
        # directory. There is no fingerprint for one, and none of them are
        # executable, so "the folder is still there" is the whole question.
        return CURRENT
    audited = row["fingerprint"]
    if not audited:
        # Nothing was recorded to compare against — an unindexed file, or a
        # finding made before the file was hashed. Not provably the same file,
        # so not correctable.
        return STALE
    indexed_size = _audited_size(row)
    if indexed_size is not None and path.stat().st_size != indexed_size:
        return STALE
    return CURRENT if blake2b_file(path) == audited else STALE


def _audited_size(row: sqlite3.Row) -> int | None:
    """The size recorded alongside the fingerprint, when the query joined it."""
    try:
        value = row["audited_size"]
    except (IndexError, KeyError):
        return None
    return int(value) if value is not None else None


def is_executable(row: sqlite3.Row, state: str) -> bool:
    """Whether this finding may become a plan.

    Three independent conditions, all required. The kind must be one someone
    has reasoned about — `dest_relpath is not None` is *not* the test, because
    a future detector could set a destination without anyone having thought
    about what executing it means. There has to be a destination. And the file
    has to still be the one that was audited.
    """
    from librairy.audit import EXECUTABLE_KINDS

    if row["kind"] not in EXECUTABLE_KINDS:
        return False
    if not row["dest_relpath"]:
        return False
    return state == CURRENT


@dataclass(frozen=True)
class Affected:
    """One file a correction will move, and why it is in the group."""

    relpath: str
    dest_relpath: str
    role: str  # "primary" or "companion"
    reason: str

    @property
    def name(self) -> str:
        return PurePosixPath(self.relpath).name


def describe_state(row: sqlite3.Row, state: str) -> str:
    if state == MISSING:
        return "This file is no longer at the path that was audited."
    if state == STALE:
        return "The file changed after this audit was created."
    return STATE_LABEL[CURRENT]


def library_path(settings: Settings, relpath: str) -> Path:
    return validate_relpath(settings.library_dir, relpath, kind="finding")
