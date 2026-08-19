from __future__ import annotations

import sqlite3
import threading
from dataclasses import asdict, dataclass, field
from pathlib import PurePosixPath
from typing import Any

from librairy.config import Settings
from librairy.db import connect
from librairy.executor import execute_plan
from librairy.humanize import human_ago, human_bytes
from librairy.lifecycle import vanished_count
from librairy.locks import LockHeldError
from librairy.planner import OperationSpec, approve_plan, create_plan
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
    rows = list(
        conn.execute(
            """
            SELECT p.category, COUNT(*) AS count, COALESCE(SUM(i.size), 0) AS bytes
            FROM proposals p
            JOIN items i ON i.id = p.item_id
            WHERE p.status='approved' AND p.dest_relpath IS NOT NULL
              AND i.missing_since IS NULL
            GROUP BY p.category
            ORDER BY count DESC
            """
        )
    )
    approved = sum(row["count"] for row in rows)
    total_bytes = sum(row["bytes"] for row in rows)
    sample = list(
        conn.execute(
            """
            SELECT i.relpath AS src, p.dest_relpath AS dest
            FROM proposals p
            JOIN items i ON i.id = p.item_id
            WHERE p.status='approved' AND p.dest_relpath IS NOT NULL
              AND i.missing_since IS NULL
            ORDER BY p.id LIMIT 5
            """
        )
    )
    return {
        "approved_count": approved,
        "total_bytes": human_bytes(total_bytes),
        "by_category": [
            {
                "category": row["category"] or "misc",
                "count": row["count"],
                "size": human_bytes(row["bytes"]),
            }
            for row in rows
        ],
        "sample": sample,
        # Corrections to files already in the library are counted and listed
        # apart from new files, all the way through. They are a different
        # promise: one of these moves something the owner already had.
        "corrections": _corrections(conn, settings),
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
        # What is waiting, by what it will actually do. Counted over the whole
        # queue in SQL; listed one bounded page at a time.
        **_queue(conn, settings, kind, page),
    }


def _queue(
    conn: sqlite3.Connection, settings: Settings | None, kind: str, page: int
) -> dict[str, Any]:
    from librairy.web.commit_queue import (
        PAGE_SIZE,
        TYPE_LABEL,
        TYPE_ORDER,
        queue_rows,
        queue_summary,
    )

    summary = queue_summary(conn)
    kind = kind if kind in TYPE_ORDER else ""
    shown = [kind] if kind else [g["type"] for g in summary["groups"]]
    total = 0
    #  A page number that has run past the end of the list is not an error to
    #  report, it is a page to not be on. Sending the last decision of page 2
    #  back left "Page 2" above nothing at all, with a Previous link as the only
    #  way out — and a filter with nothing left in it rendered a heading, a note
    #  and an empty list.
    if kind:
        total = next(
            (g["decisions"] for g in summary["all_groups"] if g["type"] == kind), 0
        )
        page = min(max(1, page), max(1, -(-total // PAGE_SIZE)))
    else:
        #  Across types the page is a set of bounded groups, each showing its
        #  own first page. There is nothing for a page number to mean here.
        page = 1
    groups = []
    for key in shown:
        group = next((g for g in summary["all_groups"] if g["type"] == key), None)
        if group is None or not group["decisions"]:
            continue
        groups.append(
            {**group, "rows": queue_rows(conn, settings, kind=key, page=page)}
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
        "queue_empty_kind": bool(kind) and not groups,
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
            ORDER BY p.created_at DESC LIMIT 5
            """
        )
    )


def _corrections(
    conn: sqlite3.Connection, settings: Settings | None = None
) -> list[dict[str, Any]]:
    """Accepted library corrections waiting to be executed, with their files.

    Each is its own plan, which is what makes a correction one logical action:
    it commits as a unit, journals as a unit, and undoes as a unit. They are
    never folded into the inbox plan, and the inbox plan could not reach them
    if it tried — it is built from `proposals`, and a correction has no
    proposal row.
    """
    from librairy.correction_state import plan_drift
    from librairy.corrections import pending_corrections, plan_files, withdrawals_for

    found = []
    for row in pending_corrections(conn):
        ops = plan_files(conn, row["plan_id"])
        # Asked here rather than at execution time only. The executor already
        # refuses a correction whose sources moved on — it stops the whole
        # group rather than half-applying it — so a Commit button on a plan
        # that is certain to be refused is a button that exists to fail.
        drift = "" if settings is None else plan_drift(conn, settings, row["plan_id"])
        found.append(
            {
                "finding_id": row["id"],
                "plan_id": row["plan_id"],
                # What this correction is about, so the card is headed by the
                # album rather than by a plan id.
                "subject": PurePosixPath(row["relpath"]).name or row["relpath"],
                "current": row["relpath"],
                "suggested": row["dest_relpath"],
                "summary": row["summary"],
                "evidence": humanize_evidence(row["evidence"]) if row["evidence"] else [],
                "op_count": len(ops),
                "size": human_bytes(_correction_bytes(conn, ops)),
                "approved_ago": human_ago(row["approved_at"]),
                "applying": row["plan_status"] == "executing",
                "stale": bool(drift),
                "stale_reason": _DRIFT_TEXT.get(drift, ""),
                # A change of mind before Commit is history worth keeping, and
                # the only place it can be seen is next to the thing it is
                # about. Never in the History page: nothing moved.
                "withdrawals": len(withdrawals_for(conn, row["id"])),
                "files": [
                    {
                        "role": op["role"],
                        "src": op["src_relpath"],
                        "dest": op["dest_relpath"],
                    }
                    for op in ops
                ],
            }
        )
    return found


# Said in terms of the file, not of the check that noticed. "changed" is what a
# person did to it; `skipped_changed` is what the executor will call the result.
_DRIFT_TEXT = {
    "changed": "A file changed after you approved this correction.",
    "missing": "A file is no longer where it was when you approved this correction.",
}


def _correction_bytes(conn: sqlite3.Connection, ops: list[sqlite3.Row]) -> int:
    """How much this correction actually moves.

    From the indexed sizes, and silently zero for anything unindexed rather
    than stat-ing the library from a render path.
    """
    paths = [op["src_relpath"] for op in ops]
    if not paths:
        return 0
    placeholders = ",".join("?" * len(paths))
    row = conn.execute(
        f"SELECT COALESCE(SUM(size), 0) AS bytes FROM items"  # noqa: S608
        f" WHERE root='library' AND relpath IN ({placeholders})",
        paths,
    ).fetchone()
    return int(row["bytes"])




def create_commit_plan(conn: sqlite3.Connection, settings: Settings) -> str:
    specs = [
        OperationSpec(row["action"], row["src_relpath"], row["dest_root"], row["dest_relpath"])
        for row in conn.execute(
            """
            SELECT p.*, i.relpath AS src_relpath
            FROM proposals p
            JOIN items i ON i.id = p.item_id
            WHERE p.status='approved' AND p.dest_relpath IS NOT NULL
              AND i.missing_since IS NULL
            ORDER BY p.id
            """
        )
    ]
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
