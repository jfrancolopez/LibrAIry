"""Renaming a folder, as every move it actually is.

`Music/Pop/Lipps Inc./` is not a thing on disk that can be corrected. The
directory entry is one name; what a person means by "fix that folder" is the
fourteen files underneath it, and the folder disappearing afterwards is a
*consequence* of moving them, not an operation anyone approved.

That distinction is the whole reason folder findings were observations for so
long. LibrAIry has exactly one executor, and its guarantees — containment, a
fingerprint match at commit time, collisions that never overwrite, an immutable
approved plan, an exact undo — are all stated per file. A `mv folderA folderB`
would be none of those things: no fingerprint to check, no per-file journal, and
an undo that could only hope the folder was still shaped the way it left it.

So this module expands a folder finding into concrete moves, and refuses when
it cannot. Nothing here writes; `corrections.accept_correction` builds the plan
from what this returns, using the same `OperationSpec` a single-file correction
uses, and Commit and Undo do not learn anything new.

Two refusals are worth reading before the code, because both look like bugs and
neither is:

* **A rename that only changes capitalisation, on a filesystem that does not
  distinguish capitalisation.** `JAMES BROWN` -> `James Brown` is the *typical*
  output of the naming detector, and on APFS or NTFS the destination directory
  already exists — it is the source. Moving each file would be moving it onto
  itself.

  The obvious fix is to route every file out to a reserved path and back, as
  two ordinary journalled operations with no directory rename anywhere. It was
  tried and measured, and it does not work: the destination directory keeps its
  old spelling, because `mkdir` on a name that already exists in another case
  does nothing to the directory that is there. It only works if the emptied
  source directory is removed *between* the two halves — a mid-plan filesystem
  step the executor does not model, whose failure would leave somebody's music
  inside the reserved namespace `paths.validate_dest` exists to keep everything
  out of. `test_subtree_corrections.py` holds the measurement so the idea does
  not have to be re-argued from first principles.

  So it is refused, with the reason said out loud, and the same finding on the
  Linux volume LibrAIry actually deploys to is executable.
* **A subtree bigger than `MAX_SUBTREE_FILES`.** The plan is a list a person
  reads before approving it. Six hundred rows is not a decision, it is a
  formality, and "reorganising a large subtree" is one of the classes that
  stays an observation on purpose.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path, PurePosixPath

from librairy.config import Settings
from librairy.fingerprint import blake2b_file
from librairy.paths import PathValidationError, validate_dest, validate_relpath

# Folder findings whose correction is a rename of the folder itself, and whose
# destination therefore means "every path beneath this, re-rooted".
#
# Deliberately not "every kind in FOLDER_KINDS that has a dest_relpath". The
# other folder kinds propose things that are not re-rootings — `split-album`
# means merging two folders into one, `artist-split` means choosing between two
# spellings — and re-rooting their destination would produce a confident plan
# for something nobody reasoned about.
SUBTREE_KINDS = frozenset({"naming-inconsistency"})

# A plan is a list somebody reads. Past a couple of hundred files it is not
# read, it is scrolled past, and approving it stops being a decision.
MAX_SUBTREE_FILES = 200


def is_subtree_finding(row: sqlite3.Row) -> bool:
    """Does correcting this finding mean moving everything beneath a folder?"""
    return row["kind"] in SUBTREE_KINDS and bool(row["dest_relpath"])


def plan_moves(
    conn: sqlite3.Connection,
    settings: Settings,
    row: sqlite3.Row,
    *,
    verify: bool,
    merging: bool = False,
) -> list[tuple[str, str]]:
    """Every `(source, destination)` an approved folder rename would carry out.

    Raises `CorrectionRefused` — the same exception a single-file correction
    raises — with a sentence saying which file, so the answer is "run a scan"
    or "that folder already exists" rather than a disabled button.

    `verify` is the difference between drawing the row and approving it. Review
    renders up to fifty findings at a time and cannot read two hundred files per
    row to do it, so the page checks what `stat` can answer: the file is there,
    it is indexed, and it is the length the index recorded. Approval reads the
    bytes. That is the same two-speed test `corrections.finding_state` already
    makes for one file — size is a fast negative, the hash is the proof — and it
    is honest about what the page can promise: a file rewritten to exactly its
    old length between the render and the click is refused at the door, not
    half-moved.
    """
    from librairy.corrections import CorrectionRefused

    source_root = row["relpath"]
    dest_root = row["dest_relpath"]
    if not dest_root:
        raise CorrectionRefused("this row has no destination to move to")

    source = _library_dir(settings, source_root, "this folder")
    _refuse_protected(conn, source_root, dest_root)
    _refuse_destination(settings, source, dest_root, merging=merging)

    members = _members(settings, source_root)
    #  Before the emptiness check, and that ordering is the whole message. A
    #  folder whose files were all deleted by hand since the audit is not an
    #  empty folder with nothing to correct; it is a folder that is no longer
    #  the one that was checked, and saying which file went is the useful half.
    _refuse_vanished(conn, source_root, members)
    if not members:
        raise CorrectionRefused(
            "there are no files in this folder to move, so there is nothing to correct"
        )
    if len(members) > MAX_SUBTREE_FILES:
        raise CorrectionRefused(
            f"this folder holds {len(members)} files, which is more than one "
            f"correction should move at once"
        )
    _refuse_disc_structure(members)
    _refuse_claimed(conn, members)

    moves: list[tuple[str, str]] = []
    for relpath in members:
        dest_relpath = f"{dest_root}{relpath[len(source_root):]}"
        try:
            validate_dest(settings.library_dir, dest_relpath)
        except (PathValidationError, ValueError) as exc:
            raise CorrectionRefused(
                f"{PurePosixPath(relpath).name} has no safe destination: {exc}"
            ) from exc
        _assert_member_ready(conn, settings, relpath, verify=verify)
        moves.append((relpath, dest_relpath))
    return moves


def remove_emptied_directories(settings: Settings, relpaths: list[str]) -> list[str]:
    """Take away library folders that these files leaving has emptied, and nothing else.

    Called in both directions, which is the point. Committing a folder rename
    empties `Lipps Inc./`; undoing it empties the `Lipps Inc/` that the commit
    created. Leaving either behind is the visible half of the bug this whole
    feature exists to fix — an empty folder sitting beside the corrected one
    reads as a correction that half worked.

    This is not an operation and is deliberately not journalled. There is
    nothing in an empty directory for Undo to restore, and every restore does
    `mkdir(parents=True)` on its way back, so the folder returns with the files.

    `rmdir` and never `rmtree`: `rmdir` refuses a directory holding anything at
    all, and that refusal *is* the safety property. A `.DS_Store` nobody asked
    about is enough to keep a folder, which is right — the scanner does not
    index it, so LibrAIry never claimed to have moved it.

    Returns what it removed, deepest first, so callers can say so.
    """
    removed: list[str] = []
    for relpath in _ancestors(relpaths):
        try:
            directory = validate_relpath(settings.library_dir, relpath, kind="finding")
        except PathValidationError:
            continue
        if not directory.is_dir():
            continue
        try:
            directory.rmdir()
        except OSError:
            # Something is still in it, or the filesystem said no. Either way
            # the folder stays, which is the outcome that loses nothing.
            continue
        removed.append(relpath)
    return removed


def _ancestors(relpaths: list[str]) -> list[str]:
    """Every library folder above these files, deepest first.

    Deepest first so a folder is only ever tried after the folders inside it,
    and the library root is never in the list — the walk stops at the top.
    """
    directories: set[str] = set()
    for relpath in relpaths:
        parent = PurePosixPath(relpath).parent
        while str(parent) not in (".", "/", ""):
            directories.add(str(parent))
            parent = parent.parent
    return sorted(directories, key=lambda path: (-path.count("/"), path))


# --- the refusals ----------------------------------------------------------------


def _library_dir(settings: Settings, relpath: str, subject: str) -> Path:
    from librairy.corrections import CorrectionRefused

    try:
        path = validate_relpath(settings.library_dir, relpath, kind="finding")
    except PathValidationError as exc:
        raise CorrectionRefused(f"{subject} is not a path inside the library") from exc
    if not path.is_dir():
        raise CorrectionRefused(f"{subject} is not on disk any more")
    return path


def _refuse_destination(
    settings: Settings, source: Path, dest_root: str, *, merging: bool
) -> None:
    """Is the destination somewhere these files can go?

    For a rename it has to be somewhere nothing is yet: a merge is a different
    decision with different questions in it — which of the two `cover.jpg` files
    wins — and this correction was approved as a rename. `librairy/merge.py`
    answers those questions and calls this with `merging=True`, which is the one
    thing that branch turns off.

    The case-only refusal is never turned off, and is explained at the top of
    the module. It is not about what the correction is; it is about what the
    filesystem can express, and a merge into a folder the filesystem thinks is
    the same folder is no more representable than a rename into one.
    """
    from librairy.corrections import CorrectionRefused

    try:
        destination = validate_dest(settings.library_dir, dest_root)
    except (PathValidationError, ValueError) as exc:
        raise CorrectionRefused(f"the suggested folder name is not usable: {exc}") from exc
    if not destination.exists():
        return
    if _same_directory(source, destination):
        raise CorrectionRefused(
            "this rename only changes capitalisation, and this filesystem treats "
            "both spellings as the same folder, so there is nothing to move it to"
        )
    if merging:
        return
    raise CorrectionRefused(
        f"{PurePosixPath(dest_root).name!r} already exists, and merging two folders "
        f"is a different decision from renaming one"
    )


def _same_directory(left: Path, right: Path) -> bool:
    try:
        return left.samefile(right)
    except OSError:
        return False


def _refuse_protected(conn: sqlite3.Connection, source_root: str, dest_root: str) -> None:
    from librairy.corrections import CorrectionRefused
    from librairy.protected import is_protected, protected_roots

    roots = protected_roots(conn)
    for relpath, side in ((source_root, "is"), (dest_root, "would be")):
        if is_protected(relpath, roots):
            raise CorrectionRefused(
                f"this {side} inside a protected folder, which LibrAIry does not move"
            )


def _refuse_disc_structure(members: list[str]) -> None:
    from librairy.audit import _in_dvd_structure
    from librairy.corrections import CorrectionRefused

    if any(_in_dvd_structure(relpath) for relpath in members):
        raise CorrectionRefused(
            "this folder holds a disc structure, which moves as a whole or not at all"
        )


def _refuse_vanished(
    conn: sqlite3.Connection, source_root: str, members: list[str]
) -> None:
    """A file the index says is in this folder, that is not in this folder.

    Walking the disk alone cannot see this. A track deleted by hand since the
    last scan simply is not in `visible_files`, so the correction would move the
    three files that are left, report success, and leave an `items` row pointing
    at a path that no longer exists inside a folder that no longer exists
    either. The finding describes the folder as it was audited; if that is not
    what is there, the recommendation is out of date, not smaller.
    """
    from librairy.corrections import CorrectionRefused

    on_disk = set(members)
    indexed = conn.execute(
        "SELECT relpath FROM items"
        " WHERE root='library' AND missing_since IS NULL AND relpath LIKE ? ESCAPE '\\'",
        (_like_prefix(source_root) + "%",),
    ).fetchall()
    for row in indexed:
        if row["relpath"] not in on_disk:
            raise CorrectionRefused(
                f"{PurePosixPath(row['relpath']).name} is no longer on disk, so this "
                f"folder is not the one that was checked"
            )


def _like_prefix(source_root: str) -> str:
    """`Music/Pop/Lipps Inc.` -> a LIKE pattern that matches only inside it.

    The trailing slash is the whole point: without it `Music/Pop/Queen` also
    matches `Music/Pop/Queens of the Stone Age`, and a correction would refuse
    because of a file in a folder it has nothing to do with.
    """
    escaped = source_root.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"{escaped}/"


def _refuse_claimed(conn: sqlite3.Connection, members: list[str]) -> None:
    """No file may be in two approved plans at once.

    There is no database constraint saying so — `plan_ops` is unique on nothing
    — and without this check, approving a folder rename over a file that already
    has its own approved correction produces two plans that each believe they
    know where that file is. The second one to commit finds it gone.
    """
    from librairy.correction_state import ACTIVE_PLAN_STATUSES
    from librairy.corrections import CorrectionRefused

    placeholders = ",".join("?" * len(ACTIVE_PLAN_STATUSES))
    for relpath in members:
        claimed = conn.execute(
            f"SELECT 1 FROM plan_ops o JOIN plans p ON p.id = o.plan_id"  # noqa: S608
            f" WHERE o.src_root='library' AND o.src_relpath=?"
            f" AND p.status IN ({placeholders}) LIMIT 1",
            (relpath, *ACTIVE_PLAN_STATUSES),
        ).fetchone()
        if claimed is not None:
            raise CorrectionRefused(
                f"{PurePosixPath(relpath).name} is already waiting for Commit as part "
                f"of another correction"
            )


def _assert_member_ready(
    conn: sqlite3.Connection,
    settings: Settings,
    relpath: str,
    *,
    verify: bool,
) -> None:
    from librairy.corrections import CorrectionRefused

    row = conn.execute(
        "SELECT fingerprint, size FROM items"
        " WHERE root='library' AND relpath=? AND missing_since IS NULL",
        (relpath,),
    ).fetchone()
    name = PurePosixPath(relpath).name
    if row is None or not row["fingerprint"]:
        raise CorrectionRefused(
            f"{name} has not been indexed, so it cannot be part of a correction"
        )
    path = validate_relpath(settings.library_dir, relpath, kind="finding")
    if not path.is_file():
        raise CorrectionRefused(f"{name} is no longer on disk")
    if row["size"] is not None and path.stat().st_size != row["size"]:
        raise CorrectionRefused(f"{name} changed since it was last scanned")
    if verify and blake2b_file(path) != row["fingerprint"]:
        raise CorrectionRefused(f"{name} changed since it was last scanned")


def _members(settings: Settings, source_root: str) -> list[str]:
    """Every file the scanner would index beneath the folder, at any depth.

    `visible_files` and not `os.walk`: a correction has to move exactly the set
    of files LibrAIry claims to know about, or the count on the row is a
    different number from the one that moves.
    """
    from librairy.scanner import visible_files

    base = settings.library_dir / source_root
    return sorted(visible_files(base, settings.ignore_patterns, prefix=source_root))
