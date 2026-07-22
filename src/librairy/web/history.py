from __future__ import annotations

import sqlite3
from dataclasses import asdict

from librairy.config import Settings
from librairy.history import list_history, undo_op, undo_plan


def history_data(conn: sqlite3.Connection, limit: int = 50) -> dict[str, object]:
    return {"entries": list_history(conn, limit=limit), "plans": _plans(conn)}


def plan_detail_data(conn: sqlite3.Connection, plan_id: str) -> dict[str, object]:
    plan = conn.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
    if plan is None:
        raise ValueError("plan not found")
    ops = conn.execute("SELECT * FROM plan_ops WHERE plan_id=? ORDER BY seq", (plan_id,)).fetchall()
    entries = list_history(conn, plan_id=plan_id, limit=200)
    return {"plan": plan, "ops": ops, "entries": entries}


def undo_history_entry(
    conn: sqlite3.Connection, settings: Settings, history_id: int
) -> dict[str, object]:
    return asdict(undo_op(conn, history_id, settings))


def undo_history_plan(
    conn: sqlite3.Connection, settings: Settings, plan_id: str
) -> list[dict[str, object]]:
    return [asdict(result) for result in undo_plan(conn, plan_id, settings)]


def _plans(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT p.*, COUNT(op.id) AS op_count
            FROM plans p
            LEFT JOIN plan_ops op ON op.plan_id = p.id
            GROUP BY p.id
            ORDER BY p.created_at DESC
            LIMIT 25
            """
        )
    )
