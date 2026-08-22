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
from librairy.correction_state import ACTIVE_PLAN_STATUSES, active_plan
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

    `files` is the whole list because there are now two shapes of correction and
    only one of them has a first file that means anything. A file correction is
    a primary plus the companions that follow it; a folder correction is a
    subtree, where every file is in the group for the same reason and calling
    one of them the primary would be inventing a distinction. `subject` says
    which, and `primary`/`companions` are kept for the first shape.
    """

    finding_id: int
    files: tuple[Affected, ...]
    subject: str = "file"

    @property
    def primary(self) -> Affected:
        return self.files[0]

    @property
    def companions(self) -> tuple[Affected, ...]:
        return self.files[1:]

    @property
    def count(self) -> int:
        return len(self.files)


def resolve_group(
    conn: sqlite3.Connection,
    settings: Settings,
    row: sqlite3.Row,
    *,
    verify: bool = True,
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

    A folder finding takes the other road entirely: there is no primary and no
    companion rule, only every file beneath the folder, re-rooted. See
    `librairy/subtree.py`. Both shapes come back as one `CorrectionGroup`
    because both are approved, planned, committed and undone the same way — the
    difference is which files are in it, not what happens to them.

    Anything the group cannot move safely refuses the whole group rather than
    moving part of it. A correction the user approved as one action is one
    action.
    """
    from librairy.audit import _in_dvd_structure
    from librairy.destination_choice import is_destination_finding
    from librairy.merge import is_merge_finding
    from librairy.subtree import is_subtree_finding
    from librairy.track_filing import is_filing_finding

    if is_filing_finding(row):
        return resolve_filing_group(conn, settings, row, verify=verify)
    if is_destination_finding(row):
        return resolve_destination_group(conn, settings, row, verify=verify)
    if is_merge_finding(row):
        return resolve_merge_group(conn, settings, row, verify=verify)
    if is_subtree_finding(row):
        return resolve_subtree_group(conn, settings, row, verify=verify)

    src_relpath = row["relpath"]
    dest_relpath = row["dest_relpath"]
    if not dest_relpath:
        raise CorrectionRefused("this row has no destination to move to")
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
    return CorrectionGroup(finding_id=int(row["id"]), files=(primary, *companions))


def resolve_merge_group(
    conn: sqlite3.Connection,
    settings: Settings,
    row: sqlite3.Row,
    *,
    verify: bool = True,
) -> CorrectionGroup:
    """Two folders becoming one, as the operations that answer every collision.

    The group is only resolvable when every conflict has been answered — see
    `librairy/merge.py`. An unanswered merge raises, which is what keeps a
    half-decided merge from becoming a plan however the request arrived.
    """
    from librairy.merge import plan_merge

    return _merge_group(row, plan_merge(conn, settings, row, verify=verify))


def resolve_destination_group(
    conn: sqlite3.Connection,
    settings: Settings,
    row: sqlite3.Row,
    *,
    verify: bool = True,
) -> CorrectionGroup:
    """An artist consolidated into the folder the owner picked.

    There is nothing here but the direction. Once `destination_choice` has
    said which folder is the destination, this *is* a merge, and it is the
    same merge planner that produces the operations — see
    `librairy/destination_choice.py` for why that was the whole design.
    """
    from librairy.destination_choice import plan_for

    view = plan_for(conn, settings, row, verify=verify)
    if view is None:
        raise CorrectionRefused("choose which folder this artist should use first")
    return _merge_group(row, view)


def resolve_filing_group(
    conn: sqlite3.Connection,
    settings: Settings,
    row: sqlite3.Row,
    *,
    verify: bool = True,
) -> CorrectionGroup:
    """Loose tracks filed into the albums somebody chose, one track at a time.

    Only the tracks that move are in the group. A track answered `Leave here`
    is answered — it is just not a file operation, and putting a no-op in the
    plan so the counts looked symmetrical would mean Commit reporting work it
    did not do and Undo offering to reverse it.
    """
    from librairy.track_filing import operations, plan_filing

    view = plan_filing(conn, settings, row, verify=verify)
    if view is None:
        raise CorrectionRefused("there is nothing left to file here")
    specs = operations(view)
    return CorrectionGroup(
        finding_id=int(row["id"]),
        files=tuple(
            Affected(
                relpath=spec.src_relpath,
                dest_relpath=spec.dest_relpath,
                role="displaced" if spec.op_type == "quarantine" else "member",
                reason=(
                    "set aside so the track could be filed"
                    if spec.op_type == "quarantine"
                    else f"filed into {PurePosixPath(spec.dest_relpath).parent.name}"
                ),
            )
            for spec in specs
        ),
        subject="filing",
    )


def _merge_group(row: sqlite3.Row, view) -> CorrectionGroup:  # noqa: ANN001
    from librairy.merge import operations

    specs = operations(view)
    return CorrectionGroup(
        finding_id=int(row["id"]),
        files=tuple(
            Affected(
                relpath=spec.src_relpath,
                dest_relpath=spec.dest_relpath,
                role="displaced" if spec.op_type == "quarantine" else "member",
                reason=(
                    "set aside so the merge could go ahead"
                    if spec.op_type == "quarantine"
                    else f"merged into {PurePosixPath(view.target).name}"
                ),
            )
            for spec in specs
        ),
        subject="merge",
    )


def resolve_subtree_group(
    conn: sqlite3.Connection,
    settings: Settings,
    row: sqlite3.Row,
    *,
    verify: bool = True,
) -> CorrectionGroup:
    """A folder rename, as the list of file moves it will actually perform."""
    from librairy.subtree import plan_moves

    moves = plan_moves(conn, settings, row, verify=verify)
    folder = PurePosixPath(row["relpath"]).name
    return CorrectionGroup(
        finding_id=int(row["id"]),
        files=tuple(
            Affected(
                relpath=relpath,
                dest_relpath=dest_relpath,
                role="member",
                reason=f"inside {folder}",
            )
            for relpath, dest_relpath in moves
        ),
        subject="subtree",
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
        raise CorrectionRefused("that row no longer exists")
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
    # The plan first, and the status second. Asking the status alone is exactly
    # how a finding that had already been approved — sitting at `open` because
    # a later audit rewrote it — was allowed through to build a second plan over
    # the same files. An approval already exists whenever a plan says so,
    # whatever the row that points at it currently reads.
    existing = active_plan(conn, finding_id)
    if existing is not None:
        raise CorrectionRefused(
            "this correction is already waiting for Commit"
            if not existing.applying
            else "this correction is already being applied"
        )
    if row["status"] == "accepted":
        # No active plan, yet the row claims one. Refusing is the safe half of
        # a real inconsistency: `librairy db check` reports it and `db repair`
        # can reopen it, deliberately, rather than this path silently deciding.
        raise CorrectionRefused(
            "this row is marked as approved but has no active plan; "
            "run `librairy db check`"
        )
    if row["status"] == "corrected":
        raise CorrectionRefused("this correction has already been applied")
    state = finding_state(settings, row)
    if _is_per_item_choice(row):
        _assert_filing_settled(conn, settings, row, state)
    elif _is_destination_choice(row):
        # A destination choice is never `executable` in the allowlist sense:
        # its kind proposes no destination, deliberately, because the
        # destination is the question. What makes it approvable is that the
        # question has been answered — and that is asked here, on the way in,
        # rather than trusted from whichever page drew the button.
        _assert_destination_settled(conn, settings, row, state)
    elif not is_executable(row, state):
        raise CorrectionRefused(_refusal(row, state))

    group = resolve_group(conn, settings, row)
    specs = _specs_for(conn, settings, row, group)
    plan_id = create_plan(conn, specs, settings)
    for seq, affected in enumerate(group.files, start=1):
        conn.execute(
            "UPDATE plan_ops SET role=? WHERE plan_id=? AND seq=?",
            (affected.role, plan_id, seq),
        )
    conn.execute(
        "UPDATE plans SET audit_finding_id=? WHERE id=?", (finding_id, plan_id)
    )
    try:
        # The check above and this line are not the same guarantee. Between them
        # another request can approve the same finding, and only the database
        # sees both. `idx_plans_one_active_per_finding` fires here, on the
        # transition to `approved`, because that is the moment a second plan
        # would start claiming files the first one already claims.
        approve_plan(conn, plan_id, settings)
    except sqlite3.IntegrityError as exc:
        # The plan is still a draft — never approved, so nothing may execute it
        # — but leaving it would litter the table with dead drafts that look
        # like corrections somebody abandoned.
        conn.execute("DELETE FROM plan_ops WHERE plan_id=?", (plan_id,))
        conn.execute("DELETE FROM plans WHERE id=?", (plan_id,))
        raise CorrectionRefused(
            "this correction was approved by something else a moment ago"
        ) from exc
    conn.execute(
        "UPDATE audit_findings SET status='accepted', plan_id=?, updated_at=? WHERE id=?",
        (plan_id, utc_now(), finding_id),
    )
    return plan_id


def _is_per_item_choice(row: sqlite3.Row) -> bool:
    from librairy.track_filing import is_filing_finding

    return is_filing_finding(row)


def _assert_filing_settled(
    conn: sqlite3.Connection, settings: Settings, row: sqlite3.Row, state: str
) -> None:
    """Every track answered, and every answer still true of the library now.

    `plan_filing` re-reads the album folders rather than trusting the stored
    answers, so a destination renamed or emptied since it was chosen comes back
    as the question again instead of as an approval nobody could review.
    """
    from librairy.track_filing import plan_filing

    if state == MISSING:
        raise CorrectionRefused(_refusal(row, state))
    view = plan_filing(conn, settings, row, verify=True)
    if view is None:
        raise CorrectionRefused("there is nothing left to file here")
    if not view.settled:
        raise CorrectionRefused(
            f"{len(view.unresolved)} of these tracks still need an answer"
        )
    if not view.moving:
        raise CorrectionRefused("every one of these tracks is staying where it is")


def _is_destination_choice(row: sqlite3.Row) -> bool:
    from librairy.destination_choice import is_destination_finding

    return is_destination_finding(row)


def _assert_destination_settled(
    conn: sqlite3.Connection, settings: Settings, row: sqlite3.Row, state: str
) -> None:
    """Both stages answered, and both still true of the library as it is now.

    `plan_for` re-reads the candidate folders rather than trusting the stored
    answer, so a destination that has been renamed or emptied since it was
    chosen comes back unanswered instead of coming back approvable. The merge
    it returns then refuses on its own account if a collision appeared after
    the last question was answered.
    """
    from librairy.destination_choice import plan_for

    if state == MISSING:
        raise CorrectionRefused(_refusal(row, state))
    view = plan_for(conn, settings, row, verify=True)
    if view is None:
        raise CorrectionRefused("choose which folder this artist should use first")
    if not view.settled:
        raise CorrectionRefused(
            f"{len(view.unresolved)} of these files still need your choice"
        )


def _specs_for(
    conn: sqlite3.Connection, settings: Settings, row: sqlite3.Row, group: CorrectionGroup
) -> list[OperationSpec]:
    """The operations this correction becomes.

    Every correction but one is a list of moves and can be read straight off the
    group. A merge is not: `keep existing` is a quarantine and no move at all,
    and `use incoming` is a quarantine *followed by* a move, in that order and
    only that order. So the merge planner produces its own operations and this
    asks it rather than guessing from the group it produced.
    """
    from librairy.destination_choice import is_destination_finding
    from librairy.destination_choice import plan_for as plan_destination
    from librairy.merge import is_merge_finding, operations, plan_merge
    from librairy.track_filing import is_filing_finding, plan_filing
    from librairy.track_filing import operations as filing_operations

    if is_filing_finding(row):
        filing = plan_filing(conn, settings, row, verify=True)
        if filing is None:
            raise CorrectionRefused("there is nothing left to file here")
        return filing_operations(filing)
    if is_destination_finding(row):
        view = plan_destination(conn, settings, row, verify=True)
        if view is None:
            raise CorrectionRefused("choose which folder this artist should use first")
        return operations(view)
    if is_merge_finding(row):
        return operations(plan_merge(conn, settings, row, verify=True))
    return [
        OperationSpec(
            op_type="move",
            src_root="library",
            src_relpath=affected.relpath,
            dest_root="library",
            dest_relpath=affected.dest_relpath,
        )
        for affected in group.files
    ]


def withdraw_approval(conn: sqlite3.Connection, finding_id: int) -> None:
    """Take back an approval that has not executed, and return the row to Review.

    Deliberately not called Undo. Undo reverses files that moved; this reverses
    a decision about files that did not. Calling both by the same word is how
    someone comes to believe Undo will put their library back after a commit
    they never made — or, worse, hesitates to press the one that would.

    An approved plan is immutable, and this does not mutate one: it withdraws
    it whole. The plan and its operations are removed, which is safe precisely
    because nothing executed — there is no journal entry, no moved file and no
    partial state to reconcile. A plan with any executed operation is refused,
    so a half-run commit can never be "unapproved" out of existence.
    """
    row = load_finding(conn, finding_id)
    # Found by plan, not by status, for the same reason approval is: the row
    # this most needs to work on is one whose status disagrees with its plan.
    # Requiring `status='accepted'` first meant the one correction in the live
    # database that was genuinely stuck could not be sent back at all.
    plan = active_plan(conn, finding_id)
    if plan is None:
        raise CorrectionRefused("this is not waiting for Commit")
    if plan.applying:
        raise CorrectionRefused("this correction has already started and cannot be recalled")
    plan_id = plan.plan_id
    _record_withdrawal(conn, row, plan)
    # The row lets go of the plan before the plan is removed. Both directions
    # are foreign keys — `audit_findings.plan_id` and `plans.audit_finding_id`
    # — so the order is not stylistic; the other way round fails outright.
    conn.execute(
        "UPDATE audit_findings SET status='open', plan_id=NULL, updated_at=? WHERE id=?",
        (utc_now(), finding_id),
    )
    conn.execute("DELETE FROM plan_ops WHERE plan_id=?", (plan_id,))
    conn.execute("DELETE FROM plans WHERE id=?", (plan_id,))


def _record_withdrawal(conn: sqlite3.Connection, row: sqlite3.Row, plan) -> None:
    """Keep the fact that this was approved, after the plan is gone.

    Written before the delete, so the hash and the approval time are still
    readable. It is one row describing one decision — not a journal, not
    something Undo can reach, and never a claim that files moved.
    """
    plan_hash = conn.execute(
        "SELECT plan_hash FROM plans WHERE id=?", (plan.plan_id,)
    ).fetchone()
    conn.execute(
        "INSERT INTO plan_withdrawals(plan_id, plan_hash, audit_finding_id, relpath,"
        " dest_relpath, op_count, approved_at, withdrawn_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            plan.plan_id,
            plan_hash["plan_hash"] if plan_hash else None,
            row["id"],
            row["relpath"],
            row["dest_relpath"],
            plan.op_count,
            plan.approved_at,
            utc_now(),
        ),
    )


def withdrawals_for(conn: sqlite3.Connection, finding_id: int) -> list[sqlite3.Row]:
    """Approvals taken back on this finding, newest first."""
    return list(
        conn.execute(
            "SELECT * FROM plan_withdrawals WHERE audit_finding_id=?"
            " ORDER BY withdrawn_at DESC, id DESC",
            (finding_id,),
        )
    )


def _refusal(row: sqlite3.Row, state: str) -> str:
    """Why this cannot be accepted, in words that reach the page.

    These sentences are read by a person on Review, so they avoid the words
    this module thinks in. "Finding" is a row in a table; what the reader has
    is a file and an observation about it.
    """
    from librairy.audit import EXECUTABLE_KINDS

    if row["kind"] not in EXECUTABLE_KINDS or not row["dest_relpath"]:
        return "this is an observation, and no move answers it"
    if state == MISSING:
        return "this file is not on disk, so there is nothing to correct"
    return "this file changed after it was audited and needs re-analysis"


def settle_plan(
    conn: sqlite3.Connection, plan_id: str, settings: Settings | None = None
) -> None:
    """Close the loop between a finished plan and the finding that asked for it.

    Called from `execute_plan`, which is the one door both the web commit and
    the CLI go through — a second call site is a second place to forget. It
    does nothing at all for an ordinary inbox plan.

    A plan that only partly applied puts its finding back to `open`. The
    correction did not happen as approved, and the honest thing is to let the
    next audit look at whatever state the files are actually in now.

    `settings` is optional only because `integrity.py` calls this to write what
    a crashed commit never got to write, and has no filesystem work to do. With
    it, a finished folder rename also loses the directories it emptied.
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
    if plan["status"] == "done":
        _remember_comparison(conn, plan_id, int(plan["audit_finding_id"]))
    if settings is not None and plan["status"] == "done":
        _remove_emptied_directories(conn, settings, plan_id)


def _remember_comparison(
    conn: sqlite3.Connection, plan_id: str, finding_id: int
) -> None:
    """A comparison answered by a replacement is not asked again.

    Only once the plan has actually run. Dismissing at approval would leave the
    pair suppressed if the approval were sent back — and a finding that came
    back to Review with its evidence suppressed is a row that disappears at the
    next audit instead of being decided.

    The pair is recorded against the two fingerprints, so a re-encode of either
    side is a live question again. Same rule as every other comparison memory
    in the program — see `similar_media.dismiss_between`.
    """
    from librairy.similar_media import KIND, dismiss_between

    found = conn.execute(
        "SELECT kind FROM audit_findings WHERE id=?", (finding_id,)
    ).fetchone()
    if found is None or found["kind"] != KIND:
        return
    items = [
        int(row["item_id"])
        for row in conn.execute(
            "SELECT item_id FROM plan_ops WHERE plan_id=? AND item_id IS NOT NULL",
            (plan_id,),
        )
    ]
    if len(items) >= 2:
        dismiss_between(conn, items)


def _remove_emptied_directories(
    conn: sqlite3.Connection, settings: Settings, plan_id: str
) -> None:
    """Take away the folders the correction emptied, and nothing else.

    Only directories this plan moved files out of. The reasoning is in
    `subtree.remove_emptied_directories`; the reverse direction is in
    `history.undo_plan`, which has exactly the same job with the two paths
    swapped.
    """
    from librairy.subtree import remove_emptied_directories

    moved = [
        row["src_relpath"]
        for row in conn.execute(
            "SELECT src_relpath FROM plan_ops"
            " WHERE plan_id=? AND src_root='library' AND result IN ('done','renamed_collision')",
            (plan_id,),
        )
    ]
    if moved:
        remove_emptied_directories(settings, moved)


def pending_corrections(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Corrections whose plan is approved and has not finished.

    Driven from the plans, not from the findings. The old query started at
    `audit_findings.status='accepted'`, which meant a finding whose status had
    been rewritten by a later audit vanished from Commit while its approved
    plan sat in the database — Review saying "waiting for Commit" and Commit
    showing nothing was the same disagreement seen from the other side.

    `plans.audit_finding_id` is the link written when the correction was
    approved, and an approved plan is the thing that has to be committed, so it
    is the right place to start.
    """
    placeholders = ",".join("?" * len(ACTIVE_PLAN_STATUSES))
    return list(
        conn.execute(
            f"""
            SELECT f.*, p.id AS plan_id, p.status AS plan_status,
                   p.approved_at AS approved_at,
                   (SELECT COUNT(*) FROM plan_ops WHERE plan_id=p.id) AS op_count
            FROM plans p
            JOIN audit_findings f ON f.id = p.audit_finding_id
            WHERE p.status IN ({placeholders})
            ORDER BY f.relpath
            """,  # noqa: S608 — placeholders are a module constant, never input
            ACTIVE_PLAN_STATUSES,
        )
    )


def undo_correction(conn: sqlite3.Connection, settings: Settings, plan_id: str):
    """Put every file a correction moved back where it came from.

    A correction is one logical action, so undoing it one journal row at a time
    through the History page would be a chore and easy to leave half done.

    This is `history.undo_plan` under a name that says what it is for. It used
    to be its own loop over the same journal rows, and the two drifted the
    moment one of them learned to take away the folders a correction had
    emptied and the other did not — leaving "put it back" with an empty
    `Lipps Inc/` still standing when reached from Review, and not when reached
    from History. Nothing here reverses a move itself.
    """
    from librairy.history import undo_plan

    return undo_plan(conn, plan_id, settings)


def group_size(conn: sqlite3.Connection, group: CorrectionGroup) -> int:
    """How much disk one correction moves, read from the index and not the disk.

    A folder correction that says "14 files" and not how much of the library
    that is has told you the cheaper half of the fact. Taken from `items`
    because the audit already measured it — walking the subtree to add up sizes
    on every page render is exactly the thing `_group_facts` refuses to do.
    """
    relpaths = [affected.relpath for affected in group.files]
    if not relpaths:
        return 0
    placeholders = ",".join("?" * len(relpaths))
    row = conn.execute(
        f"SELECT COALESCE(SUM(size), 0) AS total FROM items"  # noqa: S608 - placeholders only
        f" WHERE root='library' AND relpath IN ({placeholders})",
        relpaths,
    ).fetchone()
    return int(row["total"])


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
