"""Merging two folders into one, and the question that makes it hard.

Renaming a folder is settled: `subtree.py` expands it into the file moves it
always was. Merging is the other half of structural correction and it is a
different problem, because moving the files is the easy part.

    Music/Soul/James Brown/          Music/Soul/JAMES BROWN/
        cover.jpg                        cover.jpg
        01 - Track.flac                  02 - Track.flac

`01` and `02` move without anyone thinking about it. The two `cover.jpg` files
are the whole difficulty, and there is no rule that answers them. Keep the
larger? Keep the newer? Keep the one already at the destination? Each of those
is a preference wearing a rule's clothes, and a merge applies it to every album
in a library at once — which is exactly the reasoning `audit_duplicates.py`
already went through for two identical files.

So this reuses that answer. A merge with no collisions is a correction like any
other. A merge with collisions is a **CHOICE**: the row lists each conflict, the
person picks, and the merge becomes approvable only when every one has been
answered. Three outcomes, and none of them loses bytes:

    keep existing    the incoming copy goes to Quarantine
    use incoming     the existing copy goes to Quarantine, then the incoming
                     one takes its place
    keep both        the incoming copy is moved to a collision-safe name, and
                     that name is shown *before* approval rather than invented
                     by Commit afterwards

Nothing here executes anything, and there is no second executor. Every outcome
is one or two ordinary plan operations — a library-to-library `move` and a
`quarantine` — frozen into one immutable plan, all-or-nothing at Commit, and
reversed by the same Undo. The quarantines are what make "use incoming"
reversible at all: overwriting a destination would lose its bytes, and LibrAIry
does not have an operation that loses bytes.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import PurePosixPath

from librairy.config import Settings
from librairy.fingerprint import blake2b_file
from librairy.paths import (
    PathValidationError,
    resolve_collision,
    validate_dest,
)
from librairy.planner import OperationSpec, utc_now
from librairy.quarantine import quarantine_operation
from librairy.subtree import MAX_SUBTREE_FILES

# Folder findings whose correction is "put these together", not "call this
# something else". Only `split-album` today: it names every member folder in its
# evidence and proposes a single home, which is the whole input a merge needs.
#
# `artist-split` is deliberately absent. It reports that one artist has folders
# under two *sections* and proposes no destination, because which section is
# right is a judgement about how somebody wants their library arranged — not a
# collision anyone can resolve by looking at two files.
MERGE_KINDS = frozenset({"split-album"})

# What is at a member's destination.
FREE = "free"
IDENTICAL = "identical"
CONFLICT = "conflict"

# What the person can decide about an occupied destination.
KEEP_EXISTING = "keep-existing"
USE_INCOMING = "use-incoming"
KEEP_BOTH = "keep-both"

# `use incoming` is missing from the identical case on purpose: the bytes are
# the same, so "use the other one" is not a different outcome, it is the same
# outcome with two extra file operations.
OPTIONS: dict[str, tuple[str, ...]] = {
    IDENTICAL: (KEEP_EXISTING, KEEP_BOTH),
    CONFLICT: (KEEP_EXISTING, USE_INCOMING, KEEP_BOTH),
}

CHOICES = frozenset({KEEP_EXISTING, USE_INCOMING, KEEP_BOTH})

# Said on the row, in the words the button uses.
CHOICE_LABEL = {
    KEEP_EXISTING: "Keep existing",
    USE_INCOMING: "Use incoming",
    KEEP_BOTH: "Keep both",
}

# What each outcome does, so the row can say it before it is pressed rather
# than after.
CHOICE_NOTE = {
    KEEP_EXISTING: "The incoming copy goes to Quarantine. Nothing is deleted.",
    USE_INCOMING: "The copy already there goes to Quarantine, and this one "
    "takes its place. Nothing is deleted.",
    KEEP_BOTH: "Both are kept. The incoming one is renamed.",
}


@dataclass(frozen=True)
class Member:
    """One file in a source folder, and what is waiting for it at the other end."""

    relpath: str
    dest_relpath: str
    state: str
    size: int
    #  Only for an occupied destination: the file already there, and its size,
    #  so the row can show a person the two things they are choosing between.
    occupant_size: int = 0
    choice: str = ""
    #  Where "keep both" would put it. Computed here and shown on the row,
    #  because a name Commit invents afterwards is a name nobody approved.
    keep_both_relpath: str = ""

    @property
    def name(self) -> str:
        return PurePosixPath(self.relpath).name

    @property
    def needs_choice(self) -> bool:
        return self.state != FREE

    @property
    def options(self) -> tuple[str, ...]:
        return OPTIONS.get(self.state, ())


@dataclass(frozen=True)
class MergeView:
    """Everything one merge would do, resolved before anyone approves it."""

    finding_id: int
    target: str
    sources: tuple[str, ...]
    members: tuple[Member, ...]

    @property
    def moving(self) -> tuple[Member, ...]:
        return tuple(member for member in self.members if not member.needs_choice)

    @property
    def conflicts(self) -> tuple[Member, ...]:
        return tuple(member for member in self.members if member.needs_choice)

    @property
    def unresolved(self) -> tuple[Member, ...]:
        return tuple(member for member in self.conflicts if not member.choice)

    @property
    def settled(self) -> bool:
        """Every question answered. Only then may this be approved."""
        return not self.unresolved

    @property
    def total_bytes(self) -> int:
        return sum(member.size for member in self.members)

    @property
    def operations(self) -> int:
        """Concrete file operations, which is not the same as files.

        `use incoming` is two — a quarantine and a move — and `keep existing`
        is one that is not a move at all. The safety limit counts these rather
        than counting files, because operations are what the plan holds and
        what a person reads before approving it.
        """
        return sum(len(_specs_for(member)) for member in self.members)


# --- reading the merge -------------------------------------------------------------


def is_merge_finding(row: sqlite3.Row) -> bool:
    return row["kind"] in MERGE_KINDS and bool(row["dest_relpath"])


def plan_merge(
    conn: sqlite3.Connection,
    settings: Settings,
    row: sqlite3.Row,
    *,
    verify: bool,
    target: str = "",
    sources: Sequence[str] | None = None,
) -> MergeView:
    """What this merge would do, with every collision found and classified.

    `verify` is the same two-speed test the subtree planner makes: the page
    checks what `stat` answers, and approval reads the bytes. The difference
    matters more here, because telling two files apart is the entire point —
    a destination is only `identical` if the hashes say so, and the page says
    so on the strength of size alone until somebody approves.
    """
    from librairy.corrections import CorrectionRefused

    #  `target` and `sources` are supplied by `destination_choice.py`, which is
    #  the only caller that knows them: a `split-album` finding proposes its own
    #  destination, and an `artist-split` finding deliberately proposes none
    #  because the answer is a person's. Everything after this line is the same
    #  merge either way — the direction is the only thing the choice decides.
    target = target or row["dest_relpath"]
    if not target:
        raise CorrectionRefused("there is no single folder to merge these into")
    folders = list(sources) if sources is not None else _sources(row, target)
    if not folders:
        raise CorrectionRefused("these are already in one folder")
    _refuse_topology(folders, target)
    _refuse_protected(conn, [*folders, target])

    answers = choices_for(conn, int(row["id"]))
    members: list[Member] = []
    for source in folders:
        members.extend(_members(conn, settings, source, target, verify=verify))
    members = [
        replace(member, choice=answers.get(member.relpath, ""))
        if member.needs_choice
        else member
        for member in members
    ]
    _refuse_duplicate_destinations(members)
    view = MergeView(
        finding_id=int(row["id"]),
        target=target,
        sources=tuple(folders),
        members=tuple(members),
    )
    if not view.members:
        raise CorrectionRefused("there are no files in these folders to merge")
    if view.operations > MAX_SUBTREE_FILES:
        raise CorrectionRefused(
            f"this merge would be {view.operations} operations, which is more than "
            f"one correction should carry out at once"
        )
    return view


def _sources(row: sqlite3.Row, target: str) -> list[str]:
    """Every folder this finding speaks for, except the one they go into.

    Read off the evidence the detector recorded, exactly as `web/review._spans`
    reads it, so the folders listed on the row and the folders that move are
    the same list rather than two lists that agree today.
    """
    from librairy.proposals import decode_evidence

    folders = [row["relpath"]]
    try:
        entries = decode_evidence(row["evidence"]) if row["evidence"] else []
    except (TypeError, ValueError):
        entries = []
    folders.extend(
        entry.detail
        for entry in entries
        if entry.source == "filesystem" and entry.field == "folder"
    )
    seen: list[str] = []
    for folder in folders:
        if folder not in seen and folder != target:
            seen.append(folder)
    return sorted(seen)


def _members(
    conn: sqlite3.Connection,
    settings: Settings,
    source: str,
    target: str,
    *,
    verify: bool,
) -> list[Member]:
    from librairy.corrections import CorrectionRefused
    from librairy.subtree import plan_moves

    #  The subtree planner already answers "every file under this folder, with
    #  its destination, and refuse if any of them cannot be moved". A merge is
    #  that, per source folder, pointed at a shared destination — so it is
    #  reused rather than written again, and every refusal it makes is a
    #  refusal here: unindexed files, changed files, files that have vanished
    #  since the audit, files already waiting for Commit, disc structures.
    row = _as_finding_row(source, target)
    moves = plan_moves(conn, settings, row, verify=verify, merging=True)
    members: list[Member] = []
    for relpath, dest_relpath in moves:
        size = _indexed_size(conn, relpath)
        try:
            destination = validate_dest(settings.library_dir, dest_relpath)
        except (PathValidationError, ValueError) as exc:
            raise CorrectionRefused(
                f"{PurePosixPath(relpath).name} has no safe destination: {exc}"
            ) from exc
        if not destination.exists():
            members.append(Member(relpath, dest_relpath, FREE, size))
            continue
        occupant_size = destination.stat().st_size
        same = _same_bytes(settings, relpath, destination, occupant_size, verify=verify)
        members.append(
            Member(
                relpath=relpath,
                dest_relpath=dest_relpath,
                state=IDENTICAL if same else CONFLICT,
                size=size,
                occupant_size=occupant_size,
                keep_both_relpath=_relative(settings, resolve_collision(destination)),
            )
        )
    return members


def _same_bytes(
    settings: Settings, relpath: str, destination, occupant_size: int, *, verify: bool
) -> bool:
    """Are these two files the same file?

    Size is a fast negative and never a positive: two covers of the same
    dimensions are constantly the same length and different pictures. So the
    page's answer is provisional — it says `conflict` unless it has read both —
    and approval, which does read them, is what can say `identical`.
    """
    source = settings.library_dir / relpath
    if occupant_size != source.stat().st_size:
        return False
    if not verify:
        return False
    return blake2b_file(source) == blake2b_file(destination)


def _relative(settings: Settings, path) -> str:
    return path.relative_to(settings.library_dir.resolve()).as_posix()


def _indexed_size(conn: sqlite3.Connection, relpath: str) -> int:
    row = conn.execute(
        "SELECT size FROM items WHERE root='library' AND relpath=?", (relpath,)
    ).fetchone()
    return int(row["size"]) if row and row["size"] else 0


class _FindingRow(dict):
    """A `sqlite3.Row`-shaped view over one source folder, for `plan_moves`."""

    def __getitem__(self, key: str):  # noqa: ANN204
        return dict.__getitem__(self, key)


def _as_finding_row(source: str, target: str) -> _FindingRow:
    return _FindingRow(relpath=source, dest_relpath=target, kind="split-album")


# --- the refusals ------------------------------------------------------------------


def _refuse_topology(sources: list[str], target: str) -> None:
    """No folder may be inside another folder taking part in this merge.

    Two shapes, and both produce nonsense rather than a mistake. A source that
    *contains* the target would move its files into one of its own
    subdirectories; a source inside another source would have its files moved
    twice, once by each. Neither is a merge anybody asked for.

    The target being *equal* to a source is normal and allowed — merging A and
    B into A is the commonest case there is.
    """
    from librairy.corrections import CorrectionRefused

    for source in sources:
        if _inside(target, source):
            raise CorrectionRefused(
                f"{PurePosixPath(target).name!r} is inside "
                f"{PurePosixPath(source).name!r}, so this is not a merge"
            )
        for other in sources:
            if other != source and _inside(source, other):
                raise CorrectionRefused(
                    f"{PurePosixPath(source).name!r} is inside "
                    f"{PurePosixPath(other).name!r}, and one file cannot move twice"
                )


def _inside(candidate: str, folder: str) -> bool:
    return candidate.startswith(f"{folder}/")


def _refuse_protected(conn: sqlite3.Connection, folders: list[str]) -> None:
    from librairy.corrections import CorrectionRefused
    from librairy.protected import is_protected, protected_roots

    roots = protected_roots(conn)
    for folder in folders:
        if is_protected(folder, roots):
            raise CorrectionRefused(
                "one of these folders is protected, which LibrAIry does not move"
            )


def _refuse_duplicate_destinations(members: list[Member]) -> None:
    """Two source files that would land on the same name.

    Not a collision with something already at the destination — a collision
    between two files in this merge, which no per-file choice can resolve
    because neither of them is there yet. Three folders each holding a
    `cover.jpg` is the ordinary way to produce it.
    """
    from librairy.corrections import CorrectionRefused

    seen: dict[str, str] = {}
    for member in members:
        first = seen.get(member.dest_relpath)
        if first is not None:
            raise CorrectionRefused(
                f"{PurePosixPath(first).name} appears in more than one of these "
                f"folders, and a merge cannot decide between two files that are "
                f"both arriving"
            )
        seen[member.dest_relpath] = member.relpath


# --- the answers -------------------------------------------------------------------


def record_choice(
    conn: sqlite3.Connection, finding_id: int, relpath: str, choice: str
) -> None:
    """Remember one decision about one conflicting file.

    Stored rather than posted with the approval because a merge with six
    conflicts is six separate decisions, made by a person reading six pairs of
    files, and losing them on a refresh would make the page an exam.
    """
    from librairy.corrections import CorrectionRefused

    if choice not in CHOICES:
        raise CorrectionRefused("that is not one of the choices")
    conn.execute(
        "INSERT INTO merge_choices(audit_finding_id, relpath, choice, decided_at)"
        " VALUES (?, ?, ?, ?)"
        " ON CONFLICT(audit_finding_id, relpath)"
        " DO UPDATE SET choice=excluded.choice, decided_at=excluded.decided_at",
        (finding_id, relpath, choice, utc_now()),
    )


def choices_for(conn: sqlite3.Connection, finding_id: int) -> dict[str, str]:
    return {
        row["relpath"]: row["choice"]
        for row in conn.execute(
            "SELECT relpath, choice FROM merge_choices WHERE audit_finding_id=?",
            (finding_id,),
        )
    }


def clear_choices(conn: sqlite3.Connection, finding_id: int) -> None:
    conn.execute("DELETE FROM merge_choices WHERE audit_finding_id=?", (finding_id,))


# --- what it becomes ---------------------------------------------------------------


def operations(view: MergeView) -> list[OperationSpec]:
    """The whole merge, as operations, in the order they have to happen.

    Quarantines first. `use incoming` is two operations that only make sense
    that way round — the existing file has to be out of the way before the
    incoming one can take its place — and doing every quarantine before every
    move means the destination check before execution can be a single
    statement about the state the moves will find.
    """
    from librairy.corrections import CorrectionRefused

    if not view.settled:
        raise CorrectionRefused(
            f"{len(view.unresolved)} of these files still need your choice"
        )
    quarantines: list[OperationSpec] = []
    moves: list[OperationSpec] = []
    for member in view.members:
        for spec in _specs_for(member):
            (quarantines if spec.op_type == "quarantine" else moves).append(spec)
    return [*quarantines, *moves]


def _specs_for(member: Member) -> list[OperationSpec]:
    if not member.needs_choice:
        return [_move(member.relpath, member.dest_relpath)]
    if member.choice == KEEP_EXISTING:
        #  The incoming copy is the one that goes. It is not deleted and it is
        #  not left behind either — leaving it would mean the source folder
        #  never empties and the merge never finishes.
        return [_quarantine(member.relpath)]
    if member.choice == USE_INCOMING:
        return [
            _quarantine(member.dest_relpath),
            _move(member.relpath, member.dest_relpath),
        ]
    if member.choice == KEEP_BOTH:
        return [_move(member.relpath, member.keep_both_relpath)]
    return []


def _move(src_relpath: str, dest_relpath: str) -> OperationSpec:
    return OperationSpec(
        op_type="move",
        src_root="library",
        src_relpath=src_relpath,
        dest_root="library",
        dest_relpath=dest_relpath,
    )


def _quarantine(relpath: str) -> OperationSpec:
    return replace(quarantine_operation(relpath), src_root="library")
