from __future__ import annotations

import sqlite3
import threading
from dataclasses import asdict, dataclass, field
from typing import Any

from librairy.config import Settings
from librairy.db import connect
from librairy.executor import execute_plan
from librairy.humanize import human_bytes
from librairy.lifecycle import vanished_count
from librairy.locks import LockHeldError
from librairy.planner import OperationSpec, approve_plan, create_plan
from librairy.web.evidence import humanize_evidence


@dataclass
class CommitState:
    active_plan_id: str | None = None
    error: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


def commit_overview(conn: sqlite3.Connection) -> dict[str, Any]:
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
        "corrections": _corrections(conn),
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
    }


def _unfinished_plans(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Plans created but never executed — otherwise they are invisible forever.

    Correction plans are excluded: they are approved on purpose and listed in
    their own section, so calling them "started but never run" would be both
    wrong and alarming.
    """
    return list(
        conn.execute(
            """
            SELECT p.*, (SELECT COUNT(*) FROM plan_ops WHERE plan_id = p.id) AS op_count
            FROM plans p
            WHERE p.status IN ('draft', 'approved') AND p.audit_finding_id IS NULL
            ORDER BY p.created_at DESC LIMIT 5
            """
        )
    )


def _corrections(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Accepted library corrections waiting to be executed, with their files.

    Each is its own plan, which is what makes a correction one logical action:
    it commits as a unit, journals as a unit, and undoes as a unit. They are
    never folded into the inbox plan, and the inbox plan could not reach them
    if it tried — it is built from `proposals`, and a correction has no
    proposal row.
    """
    from librairy.corrections import pending_corrections, plan_files

    found = []
    for row in pending_corrections(conn):
        ops = plan_files(conn, row["plan_id"])
        found.append(
            {
                "finding_id": row["id"],
                "plan_id": row["plan_id"],
                "current": row["relpath"],
                "suggested": row["dest_relpath"],
                "summary": row["summary"],
                "op_count": len(ops),
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
    return {"plan": plan, "counts": counts, "recent_ops": recent}


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
