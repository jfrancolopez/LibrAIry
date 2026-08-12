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
from librairy.planner import OperationSpec, approve_plan, create_plan, utc_now

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


@dataclass(frozen=True)
class CorrectionGroup:
    """Every file one approved correction will move, resolved up front.

    The group is the unit the user approves and the unit that executes. It is
    resolved before the plan is built so that Review can show all of it, and so
    that a companion nobody expected to move is a thing you see rather than a
    thing you discover afterwards.
    """

    finding_id: int
    primary: Affected
    companions: tuple[Affected, ...]

    @property
    def files(self) -> tuple[Affected, ...]:
        return (self.primary, *self.companions)

    @property
    def count(self) -> int:
        return 1 + len(self.companions)


def resolve_group(
    conn: sqlite3.Connection, settings: Settings, row: sqlite3.Row
) -> CorrectionGroup:
    """The primary file and every companion that must travel with it.

    Two kinds of companion, because there are two kinds of belonging:

    * **Named after the file.** `05 - Song.lrc`, `Movie.en.forced.srt`,
      `Movie.nfo`. A player finds these by filename, so they follow the
      primary's final stem and keep whatever extra the name carries — the
      `.en.forced` is the only thing telling two subtitles apart.
    * **Named after the folder.** `cover.jpg`, `album.nfo`, `playlist.m3u`,
      `Album.cue`. These describe the release, not the track, and they travel
      *only when the folder is emptying* — when no media file would be left
      behind. Moving one track out of a ten-track album must not take the
      album's cover with it, and proximity alone is never the evidence: the
      classifier learned that from seven phone-camera folders where an
      unrelated `IMG_9323.jpeg` sits beside an `IMG_9323.MOV`.

    Anything the group cannot move safely refuses the whole group rather than
    moving part of it. A correction the user approved as one action is one
    action.
    """
    from librairy.audit import _in_dvd_structure

    src_relpath = row["relpath"]
    dest_relpath = row["dest_relpath"]
    if not dest_relpath:
        raise CorrectionRefused("this finding has no destination to move to")
    if _in_dvd_structure(src_relpath) or _in_dvd_structure(dest_relpath):
        # A DVD rip is a structure, not a collection of files. VIDEO_TS.IFO
        # points at its siblings by name and position; lifting one .VOB out of
        # it produces two broken things instead of one tidy one.
        raise CorrectionRefused("this file is part of a disc structure and moves as a whole")

    primary = Affected(
        relpath=src_relpath,
        dest_relpath=dest_relpath,
        role="primary",
        reason="the file this finding is about",
    )
    companions = _companions_for(conn, settings, primary)
    for affected in (primary, *companions):
        _assert_movable(conn, settings, affected)
    return CorrectionGroup(
        finding_id=int(row["id"]), primary=primary, companions=tuple(companions)
    )


def _companions_for(
    conn: sqlite3.Connection, settings: Settings, primary: Affected
) -> list[Affected]:
    from librairy.classify.companions import artwork_stem, sidecar_kind

    source_dir = PurePosixPath(primary.relpath).parent
    dest_dir = PurePosixPath(primary.dest_relpath).parent
    src_stem = PurePosixPath(primary.relpath).stem
    dest_stem = PurePosixPath(primary.dest_relpath).stem
    siblings = _siblings(settings, source_dir, exclude=primary.relpath)
    emptying = _folder_is_emptying(siblings)

    found: list[Affected] = []
    for relpath in siblings:
        name = PurePosixPath(relpath).name
        kind = sidecar_kind(name)
        artwork = artwork_stem(name)
        if kind is None and artwork is None:
            continue
        extra = _extra_suffix(PurePosixPath(relpath).stem, src_stem)
        if extra is not None:
            suffix = PurePosixPath(name).suffix
            found.append(
                Affected(
                    relpath=relpath,
                    dest_relpath=f"{dest_dir}/{dest_stem}{extra}{suffix}",
                    role="companion",
                    reason=f"named after {PurePosixPath(primary.relpath).name}",
                )
            )
        elif emptying:
            found.append(
                Affected(
                    relpath=relpath,
                    dest_relpath=f"{dest_dir}/{name}",
                    role="companion",
                    reason="belongs to the folder, and nothing else is staying in it",
                )
            )
    return found


def _extra_suffix(companion_stem: str, primary_stem: str) -> str | None:
    """What a companion's name adds to the primary's, or None if unrelated.

    `Movie` -> ``, `Movie.en.forced` -> `.en.forced`, `Other` -> None. Exact
    stem or stem-plus-suffix only, the same rule the classifier uses to pair a
    subtitle with its video, so `Movie 2.srt` never attaches itself to
    `Movie.mkv`.
    """
    if companion_stem.lower() == primary_stem.lower():
        return ""
    if companion_stem.lower().startswith(primary_stem.lower() + "."):
        return companion_stem[len(primary_stem) :]
    return None


def _folder_is_emptying(siblings: list[str]) -> bool:
    """True when nothing but companions would be left in the source folder."""
    from librairy.classify.companions import artwork_stem, sidecar_kind

    for relpath in siblings:
        name = PurePosixPath(relpath).name
        if sidecar_kind(name) is None and artwork_stem(name) is None:
            return False
    return True


def _siblings(settings: Settings, source_dir: PurePosixPath, *, exclude: str) -> list[str]:
    """Files directly beside the primary, by the same rule Browse uses."""
    from librairy.scanner import visible_files

    directory = str(source_dir)
    base = settings.library_dir / directory if directory else settings.library_dir
    if not base.is_dir():
        return []
    return [
        relpath
        for relpath in visible_files(base, settings.ignore_patterns, prefix=directory)
        if relpath != exclude and str(PurePosixPath(relpath).parent) == directory
    ]


def _assert_movable(conn: sqlite3.Connection, settings: Settings, affected: Affected) -> None:
    """Every file in the group has to be one the plan can actually name.

    `add_plan_op` needs an indexed source with a fingerprint, so a companion
    that was never scanned cannot be planned — and quietly dropping it from the
    group is exactly the stranding this whole change exists to prevent. Refuse
    the group and say which file, so the answer is "run a scan", not "half your
    album moved".
    """
    row = conn.execute(
        "SELECT fingerprint FROM items"
        " WHERE root='library' AND relpath=? AND missing_since IS NULL",
        (affected.relpath,),
    ).fetchone()
    if row is None or not row["fingerprint"]:
        raise CorrectionRefused(
            f"{affected.name} has not been indexed, so it cannot be part of a correction"
        )
    path = library_path(settings, affected.relpath)
    if not path.is_file():
        raise CorrectionRefused(f"{affected.name} is no longer on disk")
    if blake2b_file(path) != row["fingerprint"]:
        raise CorrectionRefused(f"{affected.name} changed since it was last scanned")


# --- accepting a correction ---------------------------------------------------


def load_finding(conn: sqlite3.Connection, finding_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM audit_findings WHERE id=?", (finding_id,)).fetchone()
    if row is None:
        raise CorrectionRefused("that finding no longer exists")
    return row


def accept_correction(conn: sqlite3.Connection, settings: Settings, finding_id: int) -> str:
    """Turn one approved finding into one immutable, approved plan.

    This is the only door between the audit and the executor, and every refusal
    lives here rather than in the template. A button that is not drawn is not a
    safety guarantee — the same request can arrive from a stale page, from a
    second tab, or from curl.

    The plan is created *and approved* in one step because accepting the
    correction is the approval: the user has read the exact source, the exact
    destination and the full list of files. Commit then executes what was
    approved, and cannot recompute any of it — the plan hash sees to that.
    """
    row = load_finding(conn, finding_id)
    if row["status"] == "accepted":
        raise CorrectionRefused("this correction is already waiting for Commit")
    if row["status"] == "corrected":
        raise CorrectionRefused("this correction has already been applied")
    state = finding_state(settings, row)
    if not is_executable(row, state):
        raise CorrectionRefused(_refusal(row, state))

    group = resolve_group(conn, settings, row)
    specs = [
        OperationSpec(
            op_type="move",
            src_root="library",
            src_relpath=affected.relpath,
            dest_root="library",
            dest_relpath=affected.dest_relpath,
        )
        for affected in group.files
    ]
    plan_id = create_plan(conn, specs, settings)
    for seq, affected in enumerate(group.files, start=1):
        conn.execute(
            "UPDATE plan_ops SET role=? WHERE plan_id=? AND seq=?",
            (affected.role, plan_id, seq),
        )
    conn.execute(
        "UPDATE plans SET audit_finding_id=? WHERE id=?", (finding_id, plan_id)
    )
    approve_plan(conn, plan_id, settings)
    conn.execute(
        "UPDATE audit_findings SET status='accepted', plan_id=?, updated_at=? WHERE id=?",
        (plan_id, utc_now(), finding_id),
    )
    return plan_id


def _refusal(row: sqlite3.Row, state: str) -> str:
    from librairy.audit import EXECUTABLE_KINDS

    if row["kind"] not in EXECUTABLE_KINDS or not row["dest_relpath"]:
        return "this finding is an observation and has no correction to apply"
    if state == MISSING:
        return "this file is not on disk, so there is nothing to correct"
    return "this finding needs re-analysis: the file changed after it was made"


def settle_plan(conn: sqlite3.Connection, plan_id: str) -> None:
    """Close the loop between a finished plan and the finding that asked for it.

    Called from `execute_plan`, which is the one door both the web commit and
    the CLI go through — a second call site is a second place to forget. It
    does nothing at all for an ordinary inbox plan.

    A plan that only partly applied puts its finding back to `open`. The
    correction did not happen as approved, and the honest thing is to let the
    next audit look at whatever state the files are actually in now.
    """
    plan = conn.execute(
        "SELECT status, audit_finding_id FROM plans WHERE id=?", (plan_id,)
    ).fetchone()
    if plan is None or plan["audit_finding_id"] is None:
        return
    status = "corrected" if plan["status"] == "done" else "open"
    conn.execute(
        "UPDATE audit_findings SET status=?, updated_at=? WHERE id=? AND status='accepted'",
        (status, utc_now(), plan["audit_finding_id"]),
    )


def pending_corrections(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Accepted corrections whose plan is approved and not yet executed."""
    return list(
        conn.execute(
            """
            SELECT f.*, p.id AS plan_id, p.status AS plan_status,
                   (SELECT COUNT(*) FROM plan_ops WHERE plan_id=p.id) AS op_count
            FROM audit_findings f
            JOIN plans p ON p.id = f.plan_id
            WHERE f.status='accepted' AND p.status='approved'
            ORDER BY f.relpath
            """
        )
    )


def undo_correction(conn: sqlite3.Connection, settings: Settings, plan_id: str):
    """Put every file a correction moved back where it came from.

    A correction is one logical action, so undoing it one journal row at a time
    through the History page would be a chore and easy to leave half done. This
    walks the plan's own journal entries in reverse and calls the same
    `undo_op` the History page calls — same fingerprint check, same collision
    handling, same refusal when a file has changed since. Nothing here reverses
    a move itself.
    """
    from librairy.history import undo_op

    entries = conn.execute(
        "SELECT id FROM history WHERE plan_id=? AND action='move' AND outcome='ok'"
        " ORDER BY id DESC",
        (plan_id,),
    ).fetchall()
    return [undo_op(conn, entry["id"], settings) for entry in entries]


def plan_files(conn: sqlite3.Connection, plan_id: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT seq, role, src_relpath, dest_relpath, result, final_relpath"
            " FROM plan_ops WHERE plan_id=? ORDER BY seq",
            (plan_id,),
        )
    )


def describe_state(row: sqlite3.Row, state: str) -> str:
    if state == MISSING:
        return "This file is no longer at the path that was audited."
    if state == STALE:
        return "The file changed after this audit was created."
    return STATE_LABEL[CURRENT]


def library_path(settings: Settings, relpath: str) -> Path:
    return validate_relpath(settings.library_dir, relpath, kind="finding")
