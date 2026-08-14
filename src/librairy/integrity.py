"""Where the finding↔plan relationship has come apart, and whether it is safe
to put back.

Two rules define the relationship. A finding may have at most one active plan,
and its status must agree with whether it has one. The database now enforces
the first (`idx_plans_one_active_per_finding`) and the service layer stopped
breaking the second, but neither of those repairs a database that was already
wrong — the live installation is, and it stayed wrong through this whole pass
on purpose, because a page that silently rewrites rows as a side effect of
being read destroys the evidence of the bug that made them.

So: report always, repair only when asked, and refuse when the right answer is
a guess.

That last part is the point of the split between `REPAIRABLE` and the rest. Two
approved plans for one finding is not a case with an obviously correct fix —
one of them is what somebody meant and the other is not, and nothing in the
database records which. A checker that "fixed" it by keeping the newer one
would be inventing a decision on the owner's behalf, over files.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from librairy.config import Settings
from librairy.correction_state import ACTIVE_PLAN_STATUSES, active_plans, plan_drift
from librairy.planner import utc_now

# Named so the name says the state, not the remedy.
OPEN_WITH_ACTIVE_PLAN = "open-finding-with-active-plan"
ACCEPTED_WITHOUT_PLAN = "accepted-finding-without-active-plan"
CORRECTED_WITH_ACTIVE_PLAN = "corrected-finding-with-active-plan"
DISMISSED_WITH_ACTIVE_PLAN = "dismissed-finding-with-active-plan"
DUPLICATE_ACTIVE_PLANS = "duplicate-active-plans"
FINISHED_PLAN_STILL_PENDING = "finished-plan-with-pending-finding"
STALE_ACTIVE_PLAN = "active-plan-with-changed-source"
DANGLING_PLAN_LINK = "finding-points-at-a-missing-plan"

# The cases where the database itself says what was meant, so a repair is
# transcription rather than judgement.
#
# `OPEN_WITH_ACTIVE_PLAN`: an approved, hashed, immutable plan exists. Nothing
# but an approval can produce one. The status is the field that lost.
# `ACCEPTED_WITHOUT_PLAN`: the row claims an approval with nothing to execute,
# so there is no correction — reopening asks the question again, which is the
# state the owner was in before they pressed anything.
# `FINISHED_PLAN_STILL_PENDING`: the executor already recorded the outcome; the
# finding just never heard. This writes what `settle_plan` would have.
REPAIRABLE = frozenset(
    {OPEN_WITH_ACTIVE_PLAN, ACCEPTED_WITHOUT_PLAN, FINISHED_PLAN_STILL_PENDING}
)


@dataclass(frozen=True)
class Issue:
    """One inconsistency, in terms a person can look up.

    `relpath` is library-relative, like every path LibrAIry stores. A
    diagnostic that prints host absolute paths is a diagnostic people paste
    into bug reports along with their directory layout.
    """

    kind: str
    finding_id: int | None
    relpath: str
    detail: str
    plan_ids: tuple[str, ...] = ()

    @property
    def repairable(self) -> bool:
        return self.kind in REPAIRABLE

    def __str__(self) -> str:
        plans = f" [{', '.join(self.plan_ids)}]" if self.plan_ids else ""
        return f"{self.kind}: {self.relpath}{plans} — {self.detail}"


def check(conn: sqlite3.Connection, settings: Settings | None = None) -> list[Issue]:
    """Every disagreement between the findings and the plans that claim them.

    `settings` is optional and only buys one extra check: whether a pending
    approval still matches the bytes on disk. Without it the check stays a pure
    database read, which is what makes it safe to run against a live
    installation from a second process.
    """
    issues: list[Issue] = []
    rows = conn.execute(
        "SELECT id, relpath, status, plan_id FROM audit_findings ORDER BY id"
    ).fetchall()
    for row in rows:
        issues.extend(_check_finding(conn, settings, row))
    issues.extend(_orphan_plans(conn))
    return issues


def _check_finding(
    conn: sqlite3.Connection, settings: Settings | None, row: sqlite3.Row
) -> list[Issue]:
    issues: list[Issue] = []
    plans = active_plans(conn, row["id"])
    ids = tuple(plan.plan_id for plan in plans)
    status = row["status"]

    if len(plans) > 1:
        issues.append(
            Issue(
                DUPLICATE_ACTIVE_PLANS,
                row["id"],
                row["relpath"],
                f"{len(plans)} active plans claim this one finding; "
                "which was intended is not recorded anywhere",
                ids,
            )
        )
        # Everything below reasons about "the" plan. With two of them the
        # answer to every one of those questions is also ambiguous, and
        # stacking four derived complaints on one real problem makes the
        # report harder to act on, not more complete.
        return issues

    if plans:
        plan = plans[0]
        if status == "open":
            issues.append(
                Issue(
                    OPEN_WITH_ACTIVE_PLAN,
                    row["id"],
                    row["relpath"],
                    "an approved plan is waiting for Commit, but the finding "
                    "reads as unanswered",
                    ids,
                )
            )
        elif status == "corrected":
            issues.append(
                Issue(
                    CORRECTED_WITH_ACTIVE_PLAN,
                    row["id"],
                    row["relpath"],
                    "the finding says this was applied while a plan for it has "
                    "still not run",
                    ids,
                )
            )
        elif status == "kept":
            issues.append(
                Issue(
                    DISMISSED_WITH_ACTIVE_PLAN,
                    row["id"],
                    row["relpath"],
                    "the finding was dismissed while an approved plan for it is "
                    "still waiting",
                    ids,
                )
            )
        if settings is not None and not plan.applying:
            drift = plan_drift(conn, settings, plan.plan_id)
            if drift:
                issues.append(
                    Issue(
                        STALE_ACTIVE_PLAN,
                        row["id"],
                        row["relpath"],
                        f"a source file has {drift} since this was approved, so "
                        "the plan can no longer run as approved",
                        ids,
                    )
                )
    elif status == "accepted":
        issues.append(
            Issue(
                ACCEPTED_WITHOUT_PLAN,
                row["id"],
                row["relpath"],
                _accepted_detail(conn, row),
                (row["plan_id"],) if row["plan_id"] else (),
            )
        )

    if row["plan_id"] and not _plan_exists(conn, row["plan_id"]):
        issues.append(
            Issue(
                DANGLING_PLAN_LINK,
                row["id"],
                row["relpath"],
                "the finding points at a plan that is not in the database",
                (row["plan_id"],),
            )
        )
    return issues


def _accepted_detail(conn: sqlite3.Connection, row: sqlite3.Row) -> str:
    if not row["plan_id"]:
        return "the finding is marked approved but names no plan at all"
    plan = conn.execute(
        "SELECT status FROM plans WHERE id=?", (row["plan_id"],)
    ).fetchone()
    if plan is None:
        return "the finding is marked approved and its plan no longer exists"
    return (
        "the finding is still marked approved, but its plan has finished "
        f"({plan['status']})"
    )


def _plan_exists(conn: sqlite3.Connection, plan_id: str) -> bool:
    return (
        conn.execute("SELECT 1 FROM plans WHERE id=?", (plan_id,)).fetchone() is not None
    )


def _orphan_plans(conn: sqlite3.Connection) -> list[Issue]:
    """Finished correction plans whose finding never heard the result."""
    rows = conn.execute(
        """
        SELECT p.id AS plan_id, p.status AS plan_status, f.id AS finding_id,
               f.relpath, f.status
        FROM plans p
        JOIN audit_findings f ON f.id = p.audit_finding_id
        WHERE p.status IN ('done', 'failed') AND f.status = 'accepted'
        ORDER BY p.id
        """
    ).fetchall()
    return [
        Issue(
            FINISHED_PLAN_STILL_PENDING,
            row["finding_id"],
            row["relpath"],
            f"the plan finished ({row['plan_status']}) but the finding still "
            "says it is waiting for Commit",
            (row["plan_id"],),
        )
        for row in rows
    ]


class RepairRefused(RuntimeError):
    """A repair that would have had to guess."""


def repair(conn: sqlite3.Connection, issues: list[Issue]) -> list[str]:
    """Apply only the unambiguous fixes, or refuse the whole run.

    All-or-nothing deliberately. Repairing the three easy rows and leaving the
    ambiguous one produces a database that passes a partial check and still has
    the problem that mattered, and somebody reading "repaired 3" has no reason
    to look further.

    Never called from startup, from a request, or from the worker. The only
    caller is a person typing the command.
    """
    blocked = [issue for issue in issues if not issue.repairable]
    if blocked:
        raise RepairRefused(
            "refusing to repair: "
            + "; ".join(f"{issue.kind} on {issue.relpath}" for issue in blocked)
        )
    done: list[str] = []
    for issue in issues:
        if issue.kind == OPEN_WITH_ACTIVE_PLAN:
            conn.execute(
                "UPDATE audit_findings SET status='accepted', plan_id=?, updated_at=?"
                " WHERE id=?",
                (issue.plan_ids[0], utc_now(), issue.finding_id),
            )
            done.append(f"{issue.relpath}: marked as waiting for Commit")
        elif issue.kind == ACCEPTED_WITHOUT_PLAN:
            conn.execute(
                "UPDATE audit_findings SET status='open', plan_id=NULL, updated_at=?"
                " WHERE id=?",
                (utc_now(), issue.finding_id),
            )
            done.append(f"{issue.relpath}: returned to Review")
        elif issue.kind == FINISHED_PLAN_STILL_PENDING:
            plan = conn.execute(
                "SELECT status FROM plans WHERE id=?", (issue.plan_ids[0],)
            ).fetchone()
            status = "corrected" if plan["status"] == "done" else "open"
            conn.execute(
                "UPDATE audit_findings SET status=?, updated_at=? WHERE id=?",
                (status, utc_now(), issue.finding_id),
            )
            done.append(f"{issue.relpath}: recorded as {status}")
    return done


def summary(issues: list[Issue]) -> str:
    if not issues:
        return "No finding/plan inconsistencies."
    repairable = sum(1 for issue in issues if issue.repairable)
    return (
        f"{len(issues)} inconsistency(ies); {repairable} can be repaired "
        f"automatically, {len(issues) - repairable} need a decision."
    )


__all__ = [
    "ACTIVE_PLAN_STATUSES",
    "Issue",
    "RepairRefused",
    "check",
    "repair",
    "summary",
]
