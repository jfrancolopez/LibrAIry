"""One reading of "has this correction already been approved?".

LibrAIry stored that answer twice — `audit_findings.status` and the plan the
finding points at — and had no rule for which one wins when they disagree. They
did disagree, on the live installation: a finding sitting at `open` while an
approved, unexecuted plan named it. Review believed the status, drew a checkbox
and offered *Approve change*; pressing it would have built a **second** plan for
the same files and left the first orphaned.

The rule is written here, once:

    An active plan outranks the finding's status.

Not because the status is unimportant but because of what the two things are. A
status is a flag some code path last wrote. A plan is an immutable, hashed,
approved record of exactly which files were to move and where — it cannot exist
unless somebody approved it. When the cheap field and the expensive artefact
disagree, the artefact is the evidence.

That makes this module a *reader*. It renders honestly over inconsistent data
and never repairs it: a page that quietly rewrites rows as a side effect of
being looked at destroys the evidence of the bug that produced them. Repair is
`librairy db repair`, run deliberately, by a person, and refused where the
right answer is ambiguous.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from librairy.config import Settings
from librairy.fingerprint import blake2b_file
from librairy.paths import validate_relpath

# A plan that owns its finding: it has been approved and not yet finished, so
# the files it names are spoken for. `draft` is not here — a draft is not an
# approval — and neither are `done` and `failed`, which have stopped claiming
# anything. This tuple is the definition the database index enforces too
# (`idx_plans_one_active_per_finding`); change one and the other must follow.
ACTIVE_PLAN_STATUSES = ("approved", "executing")

# What the source did after approval. Empty string means "nothing".
DRIFT_CHANGED = "changed"
DRIFT_MISSING = "missing"
#  Not the source: a file this decision was *explained in terms of* and does
#  not touch. "The still stays in Photos" is a claim about a file no operation
#  names, so nothing else in the commit path would ever notice it changing.
DRIFT_RELATED = "related"


@dataclass(frozen=True)
class ActivePlan:
    """An approved correction that has not finished, and what it covers."""

    plan_id: str
    status: str
    approved_at: str | None
    op_count: int
    executed_count: int
    # "" while every source is still byte-for-byte what was approved.
    drift: str = ""

    @property
    def applying(self) -> bool:
        """Has this plan started touching files?

        Either flag alone is enough. `executing` is set before the first move,
        and an executed operation proves it ran even if the process died before
        the plan status could be written back.
        """
        return self.status == "executing" or self.executed_count > 0

    @property
    def stale(self) -> bool:
        return bool(self.drift)


def active_plans(conn: sqlite3.Connection, finding_id: int) -> list[ActivePlan]:
    """Every active plan claiming this finding, by either link.

    Both directions are queried deliberately. `plans.audit_finding_id` and
    `audit_findings.plan_id` are two foreign keys describing one relationship,
    and an inconsistency is free to live in exactly one of them — reading only
    the one the caller happens to hold is how a duplicate stays invisible.

    Returns a list, not one row, because "there should only ever be one" is a
    claim this function exists to check rather than assume.
    """
    placeholders = ",".join("?" * len(ACTIVE_PLAN_STATUSES))
    rows = conn.execute(
        f"""
        SELECT p.id, p.status, p.approved_at,
               (SELECT COUNT(*) FROM plan_ops WHERE plan_id=p.id) AS op_count,
               (SELECT COUNT(*) FROM plan_ops
                 WHERE plan_id=p.id AND executed_at IS NOT NULL) AS executed_count
        FROM plans p
        WHERE p.status IN ({placeholders})
          AND (p.audit_finding_id = ?
               OR p.id = (SELECT plan_id FROM audit_findings WHERE id = ?))
        ORDER BY p.created_at, p.id
        """,  # noqa: S608 — placeholders are the module constant, never input
        (*ACTIVE_PLAN_STATUSES, finding_id, finding_id),
    ).fetchall()
    return [
        ActivePlan(
            plan_id=row["id"],
            status=row["status"],
            approved_at=row["approved_at"],
            op_count=int(row["op_count"]),
            executed_count=int(row["executed_count"]),
        )
        for row in rows
    ]


def active_plan(
    conn: sqlite3.Connection,
    finding_id: int,
    settings: Settings | None = None,
) -> ActivePlan | None:
    """The active plan for this finding, with its drift measured.

    The first when there is somehow more than one — never a silent pick of a
    "best" one. Ambiguity is reported by the integrity checker; here the only
    job is to make sure the row does not offer a third approval on top of two.
    """
    plans = active_plans(conn, finding_id)
    if not plans:
        return None
    plan = plans[0]
    if settings is None:
        return plan
    return ActivePlan(**{**plan.__dict__, "drift": plan_drift(conn, settings, plan.plan_id)})


def plan_drift(conn: sqlite3.Connection, settings: Settings, plan_id: str) -> str:
    """Would this plan still do what it said, if committed right now?

    Approval happens on one day and Commit on another, and in between the
    library is a directory somebody else can write to. The executor already
    refuses a correction whose sources have moved on — `_incoherent_ops` stops
    the whole group rather than half-applying it — so this asks the same
    question at render time. The page then says "your approval is outdated"
    instead of offering a Commit button that is guaranteed to fail.

    Stops at the first drift. The distinction that reaches the reader is only
    gone-versus-changed, and one operation is enough to establish either.
    """
    rows = conn.execute(
        "SELECT src_root, src_relpath, src_fingerprint FROM plan_ops"
        " WHERE plan_id=? AND executed_at IS NULL ORDER BY seq",
        (plan_id,),
    ).fetchall()
    for row in rows:
        #  Every root a plan can read from, by name. Folding "not library" into
        #  "inbox" was fine while only those two could be sources; a decision
        #  that brings a held file back reads from Quarantine, and looking for
        #  it in the inbox reported a perfectly good approval as outdated.
        root = {
            "library": settings.library_dir,
            "inbox": settings.inbox_dir,
            "quarantine": settings.quarantine_dir,
        }.get(str(row["src_root"]), settings.inbox_dir)
        try:
            path = validate_relpath(root, row["src_relpath"], kind="source")
        except Exception:
            return DRIFT_MISSING
        if not path.is_file():
            return DRIFT_MISSING
        if blake2b_file(path) != row["src_fingerprint"]:
            return DRIFT_CHANGED
    #  Asked last because it is the cheapest and the least likely: a source
    #  that has moved on is the ordinary reason an approval goes stale, and a
    #  related file changing underneath one is the rare one. Both make the
    #  card say the same thing — this approval can no longer run.
    from librairy.relationship_impact import drift as relationship_drift

    return DRIFT_RELATED if relationship_drift(conn, plan_id) else ""
