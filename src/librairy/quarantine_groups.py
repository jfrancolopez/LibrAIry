"""Putting back a whole decision, not a file at a time.

One answer to a photo comparison can send ninety files to Quarantine. Changing
your mind about that answer used to mean ninety separate restores — ninety
plans, ninety Commit cards, ninety History lines — and the boundary that made
them one thing was lost at the first click. The decision was never
reconstructed from filenames or from the day they arrived: it was recorded at
the time, in `quarantine_entries.plan_id`, and it says exactly *these files
moved together because of one thing*.

So this reads that column and nothing else. Not the date folder they landed
in, not a shared reason string, not a common prefix. Those group files that
resemble each other; this groups files that actually left together.

**The restore is one deferred decision, like every other decision here.**
Pressing it moves nothing. It writes one approved, unexecuted, coherent plan
with an operation per member, and Commit runs it — the same executor, the same
fingerprint checks, the same journal, the same Undo. Quarantine does not become
a second place where files move.

**Coherent, deliberately.** `plans.coherent` makes the executor revalidate
every source before the first one moves and refuse the whole group if any of
them has changed. Somebody who pressed `Restore all 18` asked for that
decision back, not for whichever eleven of it still happen to work.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from librairy.config import Settings
from librairy.correction_state import ACTIVE_PLAN_STATUSES
from librairy.fingerprint import blake2b_file
from librairy.live import live
from librairy.paths import validate_relpath
from librairy.planner import OperationSpec, approve_plan, create_plan
from librairy.quarantine import QuarantineError

#  Below this many members a decision is not a group: one file set aside is a
#  row with its own Restore, and wrapping it in a card that says "1 file set
#  aside together" is furniture around a button that already existed.
GROUP_FLOOR = 2

#  How many members' names the group card is willing to print. The card is a
#  summary of a decision and not a listing of it — five hundred filenames
#  rendered eagerly is the shape this pass exists to remove, and the count
#  above them is the honest version of the same fact.
NAMED = 6

#  How many decision groups the page lists at once. Same bound as every other
#  list here.
PAGE_SIZE = 20

# What the card calls the decision that produced these. Read off the finding
# the plan came from, so it is the same words Review used when the answer was
# given.
ORIGIN_LABEL = {
    "similar-media": "Similar files",
    "document-formats": "One work in two formats",
    "exact-duplicate": "Exact duplicates",
}
FALLBACK_LABEL = "Set aside together"


class RestoreGroupError(QuarantineError):
    """The group cannot be put back as one decision, and why."""


@dataclass(frozen=True)
class Member:
    """One file that left with the others."""

    entry_id: int
    item_id: int
    relpath: str
    original_root: str
    original_relpath: str
    fingerprint: str
    restored: bool
    gone: bool

    @property
    def name(self) -> str:
        return self.relpath.rsplit("/", 1)[-1]

    @property
    def restorable(self) -> bool:
        """Held, still on disk, and with somewhere recorded to go back to."""
        return (
            not self.restored
            and not self.gone
            and bool(self.original_root)
            and bool(self.original_relpath)
        )


@dataclass(frozen=True)
class Decision:
    """One committed decision, and what is left of what it set aside."""

    plan_id: str
    kind: str
    when: str
    total: int
    held: int
    restored: int
    gone: int
    names: tuple[str, ...]
    pending_plan_id: str = ""
    #  What this decision is made of, when LibrAIry knows some of its files
    #  belong together: "3 Live Photos", "2 RAW/JPEG pairs", "7 unrelated
    #  files". Description only — the restore boundary is still the originating
    #  plan, and nothing here regroups it.
    pairs: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        return ORIGIN_LABEL.get(self.kind, FALLBACK_LABEL)

    @property
    def waiting(self) -> bool:
        return bool(self.pending_plan_id)

    @property
    def restorable(self) -> bool:
        """Whether `Restore` can be offered at all.

        A group whose every member has already gone back is finished, and a
        control that can only produce an error is worse than no control.
        """
        return self.held > 0 and not self.waiting

    @property
    def partly_restored(self) -> bool:
        return self.restored > 0 and self.held > 0


def decisions(
    conn: sqlite3.Connection, *, limit: int = PAGE_SIZE, offset: int = 0
) -> list[Decision]:
    """Committed decisions that set aside more than one file, newest first.

    Counted in SQL over `plan_id`, so a decision that moved five hundred files
    is one row here and stays one row. The names are a bounded sample for the
    card, never the membership.
    """
    rows = conn.execute(
        f"""
        SELECT qe.plan_id AS plan_id, COUNT(*) AS total,
               MAX(qe.quarantined_at) AS when_,
               SUM(CASE WHEN qe.restored_at IS NOT NULL THEN 1 ELSE 0 END) AS restored,
               SUM(CASE WHEN qe.restored_at IS NULL
                         AND (i.id IS NULL OR i.missing_since IS NOT NULL)
                        THEN 1 ELSE 0 END) AS gone,
               SUM(CASE WHEN qe.restored_at IS NULL AND i.id IS NOT NULL
                         AND {live()} THEN 1 ELSE 0 END) AS held
        FROM quarantine_entries qe
        LEFT JOIN items i ON i.id = qe.item_id
        WHERE qe.plan_id IS NOT NULL AND qe.optimization_job_id IS NULL
        GROUP BY qe.plan_id
        HAVING total >= ?
        ORDER BY when_ DESC, plan_id
        LIMIT ? OFFSET ?
        """,  # noqa: S608 - `live()` is a module constant, never input
        (GROUP_FLOOR, limit, max(0, offset)),
    ).fetchall()
    if not rows:
        return []
    plan_ids = [str(row["plan_id"]) for row in rows]
    kinds = _kinds(conn, plan_ids)
    names = _names(conn, plan_ids)
    pending = pending_restores(conn, plan_ids)
    composition = _composition(conn, plan_ids)
    return [
        Decision(
            plan_id=str(row["plan_id"]),
            kind=kinds.get(str(row["plan_id"]), ""),
            when=str(row["when_"] or ""),
            total=int(row["total"]),
            held=int(row["held"] or 0),
            restored=int(row["restored"] or 0),
            gone=int(row["gone"] or 0),
            names=names.get(str(row["plan_id"]), ()),
            pending_plan_id=pending.get(str(row["plan_id"]), ""),
            pairs=composition.get(str(row["plan_id"]), ()),
        )
        for row in rows
    ]


def decision(conn: sqlite3.Connection, plan_id: str) -> Decision | None:
    """One decision by the plan that made it, or None if it never set aside
    more than one file."""
    rows = conn.execute(
        f"""
        SELECT COUNT(*) AS total, MAX(qe.quarantined_at) AS when_,
               SUM(CASE WHEN qe.restored_at IS NOT NULL THEN 1 ELSE 0 END) AS restored,
               SUM(CASE WHEN qe.restored_at IS NULL
                         AND (i.id IS NULL OR i.missing_since IS NOT NULL)
                        THEN 1 ELSE 0 END) AS gone,
               SUM(CASE WHEN qe.restored_at IS NULL AND i.id IS NOT NULL
                         AND {live()} THEN 1 ELSE 0 END) AS held
        FROM quarantine_entries qe
        LEFT JOIN items i ON i.id = qe.item_id
        WHERE qe.plan_id = ? AND qe.optimization_job_id IS NULL
        """,  # noqa: S608 - `live()` is a module constant, never input
        (plan_id,),
    ).fetchone()
    if rows is None or int(rows["total"] or 0) < GROUP_FLOOR:
        return None
    return Decision(
        plan_id=plan_id,
        kind=_kinds(conn, [plan_id]).get(plan_id, ""),
        when=str(rows["when_"] or ""),
        total=int(rows["total"]),
        held=int(rows["held"] or 0),
        restored=int(rows["restored"] or 0),
        gone=int(rows["gone"] or 0),
        names=_names(conn, [plan_id]).get(plan_id, ()),
        pending_plan_id=pending_restores(conn, [plan_id]).get(plan_id, ""),
        pairs=_composition(conn, [plan_id]).get(plan_id, ()),
    )


def members(
    conn: sqlite3.Connection, plan_id: str, *, limit: int = 0, offset: int = 0
) -> list[Member]:
    """The files one decision set aside.

    `limit=0` means all of them, and only the request path uses that: building
    a plan needs every member, and a page never does.
    """
    clause = " LIMIT ? OFFSET ?" if limit else ""
    params: list[object] = [plan_id]
    if limit:
        params.extend([limit, max(0, offset)])
    rows = conn.execute(
        f"""
        SELECT qe.id AS entry_id, qe.item_id, qe.restored_at, qe.original_root,
               qe.original_relpath, i.relpath, i.fingerprint, i.missing_since
        FROM quarantine_entries qe
        LEFT JOIN items i ON i.id = qe.item_id
        WHERE qe.plan_id = ? AND qe.optimization_job_id IS NULL
        ORDER BY qe.id{clause}
        """,  # noqa: S608 - `clause` is one of two module constants
        params,
    ).fetchall()
    return [
        Member(
            entry_id=int(row["entry_id"]),
            item_id=int(row["item_id"]),
            relpath=str(row["relpath"] or ""),
            original_root=str(row["original_root"] or ""),
            original_relpath=str(row["original_relpath"] or ""),
            fingerprint=str(row["fingerprint"] or ""),
            restored=row["restored_at"] is not None,
            gone=row["relpath"] is None or row["missing_since"] is not None,
        )
        for row in rows
    ]


@dataclass(frozen=True)
class Preflight:
    """What a group restore would find if it ran now."""

    restorable: tuple[Member, ...]
    already_restored: tuple[Member, ...]
    gone: tuple[Member, ...]
    changed: tuple[Member, ...]
    colliding: tuple[Member, ...]
    unknown_origin: tuple[Member, ...]

    @property
    def blocked(self) -> str:
        """Why this cannot be offered as one coherent restore, or "".

        A member that has *already gone back* is not a blocker — it is a
        member that is done, and excluding it is the whole point of `Restore
        the remaining 15`. A member whose bytes changed, or whose old place is
        now taken, is a different matter: acting on it would be acting on a
        decision nobody made about the files as they now are.
        """
        if self.changed:
            return "one of these files is not what it was when it was set aside"
        if self.colliding:
            return "something else is already at one of the original paths"
        if not self.restorable:
            return "none of these files can be put back"
        return ""


def preflight(
    conn: sqlite3.Connection, settings: Settings, plan_id: str
) -> Preflight:
    """Check every member before anything is written down.

    Checked here *and* again by the executor, and the two are not redundant.
    This one is so the page can refuse honestly and say which member; the
    executor's is so a fact that changed between approval and Commit still
    stops the group. Neither on its own is enough.
    """
    restorable: list[Member] = []
    already: list[Member] = []
    gone: list[Member] = []
    changed: list[Member] = []
    colliding: list[Member] = []
    unknown: list[Member] = []
    for member in members(conn, plan_id):
        if member.restored:
            already.append(member)
            continue
        if member.gone:
            gone.append(member)
            continue
        if not member.original_root or not member.original_relpath:
            unknown.append(member)
            continue
        try:
            source = validate_relpath(
                settings.quarantine_dir, member.relpath, kind="source"
            )
        except Exception:  # noqa: BLE001 - an unusable path is a missing one
            gone.append(member)
            continue
        if not source.is_file():
            gone.append(member)
            continue
        if member.fingerprint and blake2b_file(source) != member.fingerprint:
            changed.append(member)
            continue
        if _occupied(conn, settings, member):
            colliding.append(member)
            continue
        restorable.append(member)
    return Preflight(
        tuple(restorable),
        tuple(already),
        tuple(gone),
        tuple(changed),
        tuple(colliding),
        tuple(unknown),
    )


def request_restore(
    conn: sqlite3.Connection, settings: Settings, plan_id: str
) -> str:
    """Write the whole decision down as one approved, unexecuted plan.

    Refuses rather than partially succeeds. Every refusal lives here and not in
    the template, for the same reason approving a correction does: a button
    that is not drawn is not a safety guarantee, and this request can arrive
    from a page left open since yesterday or from curl.
    """
    found = decision(conn, plan_id)
    if found is None:
        raise RestoreGroupError("that decision did not set aside more than one file")
    if found.waiting:
        raise RestoreGroupError("a restore of this decision is already waiting for Commit")
    checked = preflight(conn, settings, plan_id)
    if checked.blocked:
        raise RestoreGroupError(checked.blocked)
    busy = _entries_with_own_request(conn, [m.entry_id for m in checked.restorable])
    if busy:
        raise RestoreGroupError(
            "a decision on one of these files is already waiting for Commit"
        )
    specs = [
        OperationSpec(
            op_type="move",
            src_root="quarantine",
            src_relpath=member.relpath,
            dest_root=member.original_root,
            dest_relpath=member.original_relpath,
        )
        for member in checked.restorable
    ]
    restore_plan = create_plan(conn, specs, settings)
    #  Coherent, so the executor revalidates all of them and moves none if any
    #  one has changed — and `restore_of_plan_id`, so Commit, History and the
    #  Quarantine page can all say which decision this reverses without any of
    #  them inferring it.
    conn.execute(
        "UPDATE plans SET coherent=1, restore_of_plan_id=? WHERE id=?",
        (plan_id, restore_plan),
    )
    approve_plan(conn, restore_plan, settings)
    return restore_plan


def cancel_restore(conn: sqlite3.Connection, plan_id: str) -> None:
    """Take the request back before anything has moved.

    Not Undo — nothing has happened yet. Safe for the same reason withdrawing
    an approval is: an unexecuted plan has no journal entry, no moved file and
    no partial state to reconcile.
    """
    pending = pending_restores(conn, [plan_id]).get(plan_id, "")
    if not pending:
        raise RestoreGroupError("there is no restore waiting on this decision")
    executed = conn.execute(
        "SELECT COUNT(*) AS n FROM plan_ops WHERE plan_id=? AND executed_at IS NOT NULL",
        (pending,),
    ).fetchone()["n"]
    if executed:
        raise RestoreGroupError("part of this has already run")
    conn.execute("DELETE FROM plan_ops WHERE plan_id=?", (pending,))
    conn.execute("DELETE FROM plans WHERE id=?", (pending,))


def pending_restores(
    conn: sqlite3.Connection, plan_ids: list[str]
) -> dict[str, str]:
    """Which of these decisions already have a restore waiting for Commit."""
    if not plan_ids:
        return {}
    statuses = ",".join("?" * len(ACTIVE_PLAN_STATUSES))
    placeholders = ",".join("?" * len(plan_ids))
    rows = conn.execute(
        f"""
        SELECT restore_of_plan_id AS origin, id FROM plans
        WHERE restore_of_plan_id IN ({placeholders}) AND status IN ({statuses})
        """,  # noqa: S608 - both placeholder lists are counted, never input
        [*plan_ids, *ACTIVE_PLAN_STATUSES],
    ).fetchall()
    return {str(row["origin"]): str(row["id"]) for row in rows}


def restored_by(conn: sqlite3.Connection, restore_plan_id: str) -> str:
    """The decision a given restore plan is reversing, or ""."""
    row = conn.execute(
        "SELECT restore_of_plan_id FROM plans WHERE id=?", (restore_plan_id,)
    ).fetchone()
    return str(row["restore_of_plan_id"] or "") if row else ""


def settle(conn: sqlite3.Connection, restore_plan_id: str) -> None:
    """Mark the entries a completed group restore put back.

    Reads the plan's own operations rather than a stored list of entry ids: the
    operations are what actually ran, and an operation that was skipped must
    not settle an entry whose file never moved.
    """
    from librairy.planner import utc_now
    from librairy.quarantine import remember_restored_comparison

    plan = conn.execute(
        "SELECT status, restore_of_plan_id FROM plans WHERE id=?", (restore_plan_id,)
    ).fetchone()
    if plan is None or not plan["restore_of_plan_id"] or plan["status"] != "done":
        return
    ops = conn.execute(
        "SELECT item_id, result FROM plan_ops WHERE plan_id=? AND src_root='quarantine'",
        (restore_plan_id,),
    ).fetchall()
    for op in ops:
        if op["result"] != "done" or op["item_id"] is None:
            continue
        entry = conn.execute(
            "SELECT * FROM quarantine_entries WHERE item_id=? AND restored_at IS NULL"
            " ORDER BY id DESC LIMIT 1",
            (op["item_id"],),
        ).fetchone()
        if entry is None:
            continue
        conn.execute(
            "UPDATE quarantine_entries SET restored_at=? WHERE id=?",
            (utc_now(), entry["id"]),
        )
        remember_restored_comparison(conn, entry, int(entry["item_id"]))


def _entries_with_own_request(
    conn: sqlite3.Connection, entry_ids: list[int]
) -> list[int]:
    """Members that already have a single-file decision waiting on them.

    Two plans pointing at one file is how a restore and a delete-queue move
    come to disagree at Commit. The single-file decision was made first, so it
    is the group that refuses.
    """
    if not entry_ids:
        return []
    statuses = ",".join("?" * len(ACTIVE_PLAN_STATUSES))
    placeholders = ",".join("?" * len(entry_ids))
    rows = conn.execute(
        f"""
        SELECT quarantine_entry_id AS entry_id FROM plans
        WHERE quarantine_entry_id IN ({placeholders}) AND status IN ({statuses})
        """,  # noqa: S608 - both placeholder lists are counted, never input
        [*entry_ids, *ACTIVE_PLAN_STATUSES],
    ).fetchall()
    return [int(row["entry_id"]) for row in rows]


def _occupied(conn: sqlite3.Connection, settings: Settings, member: Member) -> bool:
    """Something is already where this file wants to go back to."""
    root = {
        "library": settings.library_dir,
        "inbox": settings.inbox_dir,
        "quarantine": settings.quarantine_dir,
    }.get(member.original_root)
    if root is None:
        return True
    try:
        target = validate_relpath(root, member.original_relpath, kind="destination")
    except Exception:  # noqa: BLE001 - an unusable path is not a safe one
        return True
    if target.exists():
        return True
    #  Another restore in the same batch, or another plan, already pointing
    #  there. The disk cannot answer this one — nothing has moved yet.
    statuses = ",".join("?" * len(ACTIVE_PLAN_STATUSES))
    claimed = conn.execute(
        f"""
        SELECT 1 FROM plan_ops o JOIN plans p ON p.id = o.plan_id
        WHERE p.status IN ({statuses}) AND o.dest_root=? AND o.dest_relpath=?
        LIMIT 1
        """,  # noqa: S608 - the placeholder list is counted, never input
        [*ACTIVE_PLAN_STATUSES, member.original_root, member.original_relpath],
    ).fetchone()
    return claimed is not None


def _composition(
    conn: sqlite3.Connection, plan_ids: list[str]
) -> dict[str, tuple[str, ...]]:
    """What each decision is made of, where files in it belong together.

    Two queries for the whole page. The pairs counted are only those with
    **both** halves inside the same decision — a Live Photo whose still is
    still in the library is not a pair this decision is putting back, and
    counting it would promise something the restore does not do.

    Description, not structure. The restore boundary is the originating plan
    and stays the originating plan; this only lets its Details say what is in
    it instead of a bare number.
    """
    from librairy.relationship_impact import PAIR_NAME

    if not plan_ids:
        return {}
    placeholders = ",".join("?" * len(plan_ids))
    kinds = conn.execute(
        f"""
        SELECT qe.plan_id AS plan_id, r.kind AS kind, COUNT(*) AS n
        FROM item_relationships r
        JOIN quarantine_entries qe  ON qe.item_id  = r.low_item_id
        JOIN quarantine_entries qe2 ON qe2.item_id = r.high_item_id
                                   AND qe2.plan_id = qe.plan_id
        WHERE qe.plan_id IN ({placeholders})
        GROUP BY qe.plan_id, r.kind
        ORDER BY qe.plan_id, r.kind
        """,  # noqa: S608 - placeholders are counted from the id list
        plan_ids,
    ).fetchall()
    if not kinds:
        return {}
    paired = conn.execute(
        f"""
        SELECT qe.plan_id AS plan_id, COUNT(DISTINCT qe.item_id) AS n
        FROM quarantine_entries qe
        JOIN item_relationships r
          ON r.low_item_id = qe.item_id OR r.high_item_id = qe.item_id
        JOIN quarantine_entries qe2
          ON qe2.plan_id = qe.plan_id
         AND qe2.item_id = CASE WHEN r.low_item_id = qe.item_id
                                THEN r.high_item_id ELSE r.low_item_id END
        WHERE qe.plan_id IN ({placeholders})
        GROUP BY qe.plan_id
        """,  # noqa: S608 - placeholders are counted from the id list
        plan_ids,
    ).fetchall()
    within = {str(row["plan_id"]): int(row["n"]) for row in paired}
    totals = conn.execute(
        f"SELECT plan_id, COUNT(*) AS n FROM quarantine_entries"  # noqa: S608
        f" WHERE plan_id IN ({placeholders}) AND optimization_job_id IS NULL"
        f" GROUP BY plan_id",
        plan_ids,
    ).fetchall()
    total = {str(row["plan_id"]): int(row["n"]) for row in totals}
    lines: dict[str, list[str]] = {}
    for row in kinds:
        plan_id, kind, count = str(row["plan_id"]), str(row["kind"]), int(row["n"])
        singular, plural = PAIR_NAME.get(kind, ("related pair", "related pairs"))
        lines.setdefault(plan_id, []).append(
            f"{count} {singular if count == 1 else plural}"
        )
    for plan_id, rendered in lines.items():
        rest = total.get(plan_id, 0) - within.get(plan_id, 0)
        if rest > 0:
            rendered.append(f"{rest} unrelated file{'' if rest == 1 else 's'}")
    return {plan_id: tuple(rendered) for plan_id, rendered in lines.items()}


def _kinds(conn: sqlite3.Connection, plan_ids: list[str]) -> dict[str, str]:
    """What kind of finding each decision came from, in one query."""
    if not plan_ids:
        return {}
    placeholders = ",".join("?" * len(plan_ids))
    rows = conn.execute(
        f"""
        SELECT p.id, f.kind FROM plans p
        JOIN audit_findings f ON f.id = p.audit_finding_id
        WHERE p.id IN ({placeholders})
        """,  # noqa: S608 - the placeholder list is counted, never input
        plan_ids,
    ).fetchall()
    return {str(row["id"]): str(row["kind"]) for row in rows}


def _names(conn: sqlite3.Connection, plan_ids: list[str]) -> dict[str, tuple[str, ...]]:
    """A bounded sample of each decision's filenames.

    `NAMED` per decision, taken in the database rather than by slicing a full
    list in Python — the point is not to load five hundred rows and show six.
    """
    found: dict[str, tuple[str, ...]] = {}
    for plan_id in plan_ids:
        rows = conn.execute(
            """
            SELECT i.relpath FROM quarantine_entries qe
            JOIN items i ON i.id = qe.item_id
            WHERE qe.plan_id = ? ORDER BY qe.id LIMIT ?
            """,
            (plan_id, NAMED),
        ).fetchall()
        found[plan_id] = tuple(
            str(row["relpath"]).rsplit("/", 1)[-1] for row in rows
        )
    return found
