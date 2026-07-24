from __future__ import annotations

import sqlite3
import threading
from dataclasses import asdict, dataclass, field
from typing import Any

from librairy.config import Settings
from librairy.db import connect
from librairy.executor import execute_plan
from librairy.locks import LockHeldError
from librairy.planner import OperationSpec, approve_plan, create_plan
from librairy.web.evidence import humanize_evidence


@dataclass
class CommitState:
    active_plan_id: str | None = None
    error: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


def create_commit_plan(conn: sqlite3.Connection, settings: Settings) -> str:
    specs = [
        OperationSpec(row["action"], row["src_relpath"], row["dest_root"], row["dest_relpath"])
        for row in conn.execute(
            """
            SELECT p.*, i.relpath AS src_relpath
            FROM proposals p
            JOIN items i ON i.id = p.item_id
            WHERE p.status='approved' AND p.dest_relpath IS NOT NULL
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


def mark_committed_proposals(conn: sqlite3.Connection, plan_id: str) -> None:
    conn.execute(
        """
        UPDATE proposals
        SET status='committed', updated_at=datetime('now')
        WHERE item_id IN (SELECT item_id FROM plan_ops WHERE plan_id=? AND result IS NOT NULL)
        """,
        (plan_id,),
    )


def _execute_background(
    settings: Settings,
    state: CommitState,
    plan_id: str,
) -> None:
    conn = connect(settings)
    try:
        summary = execute_plan(conn, plan_id, settings)
        if not summary.partial:
            mark_committed_proposals(conn, plan_id)
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
