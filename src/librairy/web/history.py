from __future__ import annotations

import sqlite3
from dataclasses import asdict

from librairy.config import Settings
from librairy.history import list_history, undo_op, undo_plan


def history_data(conn: sqlite3.Connection, limit: int = 50) -> dict[str, object]:
    entries = [_augment(dict(row)) for row in list_history(conn, limit=limit)]
    plans = {row["id"]: row for row in _plans(conn)}
    return {
        "entries": entries,
        "plans": list(plans.values()),
        "timeline": _timeline(entries, plans),
    }


def _augment(entry: dict[str, object]) -> dict[str, object]:
    entry["browse_href"] = _browse_href(entry.get("dest_root"), entry.get("dest_relpath"))
    return entry


def _browse_href(dest_root: object, dest_relpath: object) -> str | None:
    """Deep-link a committed destination to Browse at its containing folder."""
    if dest_root != "library" or not dest_relpath:
        return None
    parts = str(dest_relpath).split("/")
    if len(parts) < 2:
        return None
    category = parts[0].lower()
    folder = "/".join(parts[1:-1])
    return f"/browse/{category}?folder={folder}" if folder else f"/browse/{category}"


def _timeline(entries: list[dict[str, object]], plans: dict) -> list[dict[str, object]]:
    """Group journal entries by plan, newest first, git-log style."""
    groups: list[dict[str, object]] = []
    by_plan: dict[object, dict[str, object]] = {}
    for entry in entries:
        plan_id = entry.get("plan_id")
        group = by_plan.get(plan_id)
        if group is None:
            plan = plans.get(plan_id)
            group = {
                "plan_id": plan_id,
                "status": plan["status"] if plan else None,
                "ts": entry.get("ts"),
                "entries": [],
            }
            by_plan[plan_id] = group
            groups.append(group)
        group["entries"].append(entry)
    return groups


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
