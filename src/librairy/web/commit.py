from __future__ import annotations

import sqlite3
import threading
from dataclasses import asdict, dataclass, field
from pathlib import PurePosixPath
from typing import Any

from librairy.config import Settings
from librairy.db import connect
from librairy.executor import execute_plan
from librairy.lifecycle import vanished_count
from librairy.locks import LockHeldError
from librairy.planner import OperationSpec, approve_plan, create_plan
from librairy.web.commit_queue import NEW_FILE
from librairy.web.evidence import humanize_evidence


@dataclass
class CommitState:
    active_plan_id: str | None = None
    error: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


def commit_overview(
    conn: sqlite3.Connection,
    settings: Settings | None = None,
    *,
    kind: str = "",
    page: int = 1,
) -> dict[str, Any]:
    """What a commit would actually do, before anyone presses the button.

    The page used to say "N approved proposal(s) ready" and nothing else, which
    is the one screen in LibrAIry that moves files — you could not see what was
    about to move, how much of it there was, or where it was going.
    """
    #  Everything waiting, by what it will actually do. Counted over the whole
    #  queue in SQL; listed one bounded page at a time.
    queue = _queue(conn, settings, kind, page)
    return {
        #  The only thing left of the old aggregate: `/commit/create` refuses to
        #  build an empty plan. It comes out of the same summary the page counts
        #  with rather than a query of its own.
        "approved_count": next(
            group["decisions"]
            for group in queue["summary"]["all_groups"]
            if group["type"] == NEW_FILE
        ),
        "unfinished": _unfinished_plans(conn),
        "waiting_review": conn.execute(
            """
            SELECT COUNT(*) FROM proposals p JOIN items i ON i.id = p.item_id
            WHERE p.status='proposed' AND p.dest_relpath IS NOT NULL
              AND i.missing_since IS NULL
            """
        ).fetchone()[0],
        "vanished": vanished_count(conn),
        "last_plan": conn.execute(
            "SELECT * FROM plans WHERE status='done' ORDER BY finished_at DESC LIMIT 1"
        ).fetchone(),
        # What just happened, kept visible after the pending count reaches
        # zero. Committing the last item used to replace the whole page with
        # "Nothing is approved yet", which is true and reads as though the
        # thing you just did never occurred. Built from History, which already
        # has all of it — this is a view, not a second record.
        "last_result": _last_result(conn),
        **queue,
    }


def _queue(
    conn: sqlite3.Connection, settings: Settings | None, kind: str, page: int
) -> dict[str, Any]:
    from librairy.plan_conflicts import arrivals_in_conflict
    from librairy.web.commit_queue import (
        PAGE_SIZE,
        PREVIEW_SIZE,
        TYPE_LABEL,
        TYPE_ORDER,
        queue_rows,
        queue_summary,
    )

    summary = queue_summary(conn)
    #  Counted over the whole queue, not over the page being drawn: the group's
    #  button files every approved arrival at once, so a collision on page two
    #  is still a collision the button would run into.
    conflicting = arrivals_in_conflict(conn)
    kind = kind if kind in TYPE_ORDER else ""
    shown = [kind] if kind else [g["type"] for g in summary["groups"]]
    total = 0
    #  A page number that has run past the end of the list is not an error to
    #  report, it is a page to not be on. Sending the last decision of page 2
    #  back left "Page 2" above nothing at all, with a Previous link as the only
    #  way out — and a filter with nothing left in it rendered a heading, a note
    #  and an empty list.
    emptied = False
    if kind:
        total = next(
            (g["decisions"] for g in summary["all_groups"] if g["type"] == kind), 0
        )
        #  Cancelling the last optimization lands here from
        #  `/maintenance/optimization/{job}/send-back`, which renders this page
        #  filtered to optimizations — a filter that, by then, has nothing in
        #  it. Saying so and showing the rest beats a headline above a blank.
        if not total:
            emptied, kind, shown = True, "", [g["type"] for g in summary["groups"]]
        page = min(max(1, page), max(1, -(-total // PAGE_SIZE)))
    if not kind:
        #  Across types the page is a set of bounded groups, each showing its
        #  own first page. There is nothing for a page number to mean here.
        page = 1
    #  Filtered: the working list, fifty at a time. Unfiltered: a preview of
    #  each kind, so the page stays one screenful of each rather than fifty of
    #  every kind that happens to be populated.
    size = PAGE_SIZE if kind else PREVIEW_SIZE
    groups = []
    for key in shown:
        group = next((g for g in summary["all_groups"] if g["type"] == key), None)
        if group is None or not group["decisions"]:
            continue
        rows = queue_rows(conn, settings, kind=key, page=page, page_size=size)
        groups.append(
            {
                **group,
                "rows": rows,
                "more": max(0, int(group["decisions"]) - len(rows)),
                "conflicts": conflicting.get(key, 0),
            }
        )
    return {
        "summary": summary,
        "queue_groups": groups,
        "queue_type": kind,
        "queue_types": TYPE_ORDER,
        "queue_labels": TYPE_LABEL,
        "queue_page": page,
        #  A filter whose category has emptied since the link was made. The
        #  page says so rather than rendering a summary above a blank.
        "queue_empty_kind": emptied,
        "queue_page_size": PAGE_SIZE,
        # Paging only means anything inside one type; across types the page is
        # a set of bounded groups, which is what keeps the DOM small either way.
        "queue_has_next": bool(kind) and page * PAGE_SIZE < total,
        "queue_has_prev": bool(kind) and page > 1,
    }


def _last_result(conn: sqlite3.Connection) -> dict[str, Any] | None:
    plan = conn.execute(
        "SELECT * FROM plans WHERE status IN ('done','failed')"
        " ORDER BY finished_at DESC, id DESC LIMIT 1"
    ).fetchone()
    if plan is None:
        return None
    counts = {
        row["result"] or "pending": row["count"]
        for row in conn.execute(
            "SELECT result, COUNT(*) AS count FROM plan_ops WHERE plan_id=? GROUP BY result",
            (plan["id"],),
        )
    }
    finding = conn.execute(
        "SELECT relpath, summary FROM audit_findings WHERE plan_id=?", (plan["id"],)
    ).fetchone()
    moved = counts.get("done", 0) + counts.get("renamed_collision", 0)
    failed = counts.get("failed", 0) + counts.get("skipped_changed", 0) + counts.get(
        "skipped_missing", 0
    )
    return {
        "plan_id": plan["id"],
        "finished_at": plan["finished_at"],
        "moved": moved,
        "failed": failed,
        # A correction says what it was about; an inbox commit is a count.
        "label": (
            finding["relpath"].rpartition("/")[2] or finding["relpath"]
            if finding is not None
            else f"{moved} file{'' if moved == 1 else 's'} from your inbox"
        ),
        # Only offered while History still has something to reverse.
        "can_undo": bool(
            conn.execute(
                "SELECT COUNT(*) AS n FROM history"
                " WHERE plan_id=? AND action='move' AND outcome='ok'",
                (plan["id"],),
            ).fetchone()["n"]
        ),
    }


def _unfinished_plans(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Plans created but never executed — otherwise they are invisible forever.

    Every plan that has a section of its own is excluded, because calling a
    deliberate approval "started but never run" is both wrong and alarming. That
    was true of corrections from the start; adoptions joined them, and a browser
    found it immediately — the same plan appeared once as an OPTIMIZE card and
    again as an orphan two screens down, with a shortened UUID for a name.

    Quarantine requests turned out to be in it too — they have had their own
    RESTORE and DELETE QUEUE sections since the taxonomy landed, and nothing
    took them out of here. Excluded now, for the same reason.

    And the fourth, which is the one nobody excluded because it *is* the plan
    behind the New files group: pressing `Review moves` builds and approves an
    inbox plan, and walking away from the confirm screen left that plan here —
    a shortened UUID under "Started but never run", beside the very cards whose
    decisions it carries. The live installation has one. So a plan is only an
    orphan if nothing else on the page is already speaking for it, and the test
    for that is whether any of its files still has an approved proposal.

    A plan with no operations is excluded as well. It cannot be finished, it
    cannot do anything, and "started but never run" describes it only in the
    sense that nothing ever started.
    """
    return list(
        conn.execute(
            """
            SELECT p.*, (SELECT COUNT(*) FROM plan_ops WHERE plan_id = p.id) AS op_count
            FROM plans p
            WHERE p.status IN ('draft', 'approved')
              AND p.audit_finding_id IS NULL
              AND p.optimization_job_id IS NULL
              AND p.quarantine_entry_id IS NULL
              AND EXISTS (SELECT 1 FROM plan_ops o WHERE o.plan_id = p.id)
              AND NOT EXISTS (
                SELECT 1 FROM plan_ops o
                JOIN proposals pr ON pr.item_id = o.item_id
                WHERE o.plan_id = p.id AND pr.status = 'approved'
              )
            ORDER BY p.created_at DESC LIMIT 5
            """
        )
    )


def create_commit_plan(conn: sqlite3.Connection, settings: Settings) -> str:
    """One plan for every approved arrival — except the ones in conflict.

    Every arrival is filed by a single plan, so two of them wanting one
    destination used to stop all of them: approval refuses a plan that names
    two files into one place, and eighteen perfectly good decisions went down
    with the two that collided.

    Left out rather than cancelled. Both conflicting decisions stay approved,
    both cards say what they collide with, and both come back into the batch
    the moment one of them is sent back. Which of the two to keep is not a
    question this function is allowed to answer.
    """
    from librairy.plan_conflicts import PROPOSAL, for_decisions

    rows = list(
        conn.execute(
            """
            SELECT p.*, i.relpath AS src_relpath
            FROM proposals p
            JOIN items i ON i.id = p.item_id
            WHERE p.status='approved' AND p.dest_relpath IS NOT NULL
              AND i.missing_since IS NULL
            ORDER BY p.id
            """
        )
    )
    conflicted = for_decisions(
        conn, [(PROPOSAL, str(row["id"])) for row in rows]
    )
    specs = [
        OperationSpec(row["action"], row["src_relpath"], row["dest_root"], row["dest_relpath"])
        for row in rows
        if (PROPOSAL, str(row["id"])) not in conflicted
    ]
    if rows and not specs:
        from librairy.planner import PlanConflict

        raise PlanConflict(
            "every arrival waiting here is in conflict with another decision. "
            "Send one of each pair back and the rest can be filed."
        )
    plan_id = create_plan(conn, specs, settings)
    approve_plan(conn, plan_id, settings)
    return plan_id


def commit_confirm_data(conn: sqlite3.Connection, plan_id: str) -> dict[str, object]:
    plan = _plan(conn, plan_id)
    ops = _ops(conn, plan_id)
    categories = conn.execute(
        """
        SELECT p.category, COUNT(*) AS count
        FROM plan_ops op
        JOIN proposals p ON p.item_id = op.item_id
        WHERE op.plan_id=?
        GROUP BY p.category
        ORDER BY p.category
        """,
        (plan_id,),
    ).fetchall()
    quarantine_count = sum(1 for op in ops if op["op_type"] == "quarantine")
    return {
        "plan": plan,
        "ops": ops,
        "categories": categories,
        "quarantine_count": quarantine_count,
        **progress_data(conn, plan_id),
    }


def start_execution(
    conn: sqlite3.Connection,
    settings: Settings,
    state: CommitState,
    plan_id: str,
) -> bool:
    with state.lock:
        if state.active_plan_id is not None:
            return False
        state.active_plan_id = plan_id
        state.error = None
    thread = threading.Thread(
        target=_execute_background,
        args=(settings, state, plan_id),
        daemon=True,
    )
    thread.start()
    return True


def progress_data(conn: sqlite3.Connection, plan_id: str) -> dict[str, object]:
    plan = _plan(conn, plan_id)
    counts = {
        row["result"] or "pending": row["count"]
        for row in conn.execute(
            """
            SELECT result, COUNT(*) AS count
            FROM plan_ops
            WHERE plan_id=?
            GROUP BY result
            """,
            (plan_id,),
        )
    }
    recent = conn.execute(
        """
        SELECT * FROM plan_ops
        WHERE plan_id=? AND result IS NOT NULL
        ORDER BY executed_at DESC, id DESC
        LIMIT 8
        """,
        (plan_id,),
    ).fetchall()
    return {
        "plan": plan,
        "counts": counts,
        "recent_ops": recent,
        # What is being corrected, so the page can be headed by the album
        # rather than by "1 of 1". Empty for an ordinary inbox commit, which
        # genuinely is a count of unrelated files.
        "subject": _plan_subject(conn, plan),
    }


def _plan_subject(conn: sqlite3.Connection, plan: sqlite3.Row | None) -> str:
    if plan is None or plan["audit_finding_id"] is None:
        return ""
    row = conn.execute(
        "SELECT relpath FROM audit_findings WHERE id=?", (plan["audit_finding_id"],)
    ).fetchone()
    if row is None:
        return ""
    return PurePosixPath(row["relpath"]).name or row["relpath"]


def _execute_background(
    settings: Settings,
    state: CommitState,
    plan_id: str,
) -> None:
    conn = connect(settings)
    try:
        # Proposals are marked committed by the executor, per op, as each file
        # actually moves — not here, and not all-or-nothing on the plan.
        execute_plan(conn, plan_id, settings)
    except LockHeldError:
        with state.lock:
            state.error = "LibrAIry is busy; retry when the worker releases the lock"
    except Exception as exc:  # pragma: no cover - defensive result surfaced in UI
        with state.lock:
            state.error = str(exc)
    finally:
        conn.close()
        with state.lock:
            state.active_plan_id = None


def _plan(conn: sqlite3.Connection, plan_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
    if row is None:
        raise ValueError("plan not found")
    return dict(row)


def _ops(conn: sqlite3.Connection, plan_id: str) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT op.*, p.evidence AS evidence, p.confidence AS confidence
        FROM plan_ops op
        LEFT JOIN proposals p
          ON p.item_id = op.item_id AND p.status != 'superseded'
        WHERE op.plan_id=? ORDER BY op.seq
        """,
        (plan_id,),
    ).fetchall()
    return [
        {**dict(row), "evidence_views": humanize_evidence(row["evidence"] or "")} for row in rows
    ]


def summary_dict(obj) -> dict[str, object]:
    return asdict(obj)
