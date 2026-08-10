from __future__ import annotations

import sqlite3
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta

from librairy.config import Settings
from librairy.history import HISTORY_KINDS, kind_counts, list_history, undo_op, undo_plan


def history_data(
    conn: sqlite3.Connection,
    limit: int = 50,
    query: str = "",
    kind: str = "all",
    page: int = 1,
) -> dict[str, object]:
    if kind not in HISTORY_KINDS:
        kind = "all"
    page = max(1, page)
    offset = (page - 1) * limit
    # One extra row, only to answer "is there another page" without a second
    # COUNT over a table that only grows.
    fetched = [
        _augment(dict(row))
        for row in list_history(
            conn, limit=limit + 1, query=query, kind=kind, offset=offset
        )
    ]
    has_next = len(fetched) > limit
    rows = fetched[:limit]
    # A settings change is journalled for audit, but it is not a move: it has
    # no plan, nothing to undo here, and no source and destination worth the
    # arrow between them. Eighteen of them under a heading reading "Plan None ·
    # 18 file(s) · Undo plan" pushed every real move off the page.
    entries = [row for row in rows if row.get("action") != "settings_change"]
    changes = [row for row in rows if row.get("action") == "settings_change"]
    plans = {row["id"]: row for row in _plans(conn)}
    counts = kind_counts(conn, query)
    return {
        "entries": entries,
        "settings_changes": changes,
        "plans": list(plans.values()),
        "timeline": _timeline(entries, plans),
        "days": _days(entries, plans),
        "query": query,
        "kind": kind,
        "kinds": [
            {"key": key, "label": label, "count": counts.get(key, 0)}
            for key, (label, _) in HISTORY_KINDS.items()
        ],
        "page": page,
        "has_next": has_next,
        "has_prev": page > 1,
        "showing": len(rows),
        "matching": counts.get(kind, 0),
        "total": _journal_size(conn),
    }


def _days(entries: list[dict[str, object]], plans: dict) -> list[dict[str, object]]:
    """Plans, bucketed by the day they ran.

    Presentation only — no journal row is merged, dropped or rewritten. A day
    heading and a one-line plan summary are what turn 145 identical rows into
    "yesterday you filed 12 files", and both are computed here at render time
    from the rows themselves.
    """
    days: list[dict[str, object]] = []
    by_day: dict[str, dict[str, object]] = {}
    for group in _timeline(entries, plans):
        day = str(group.get("ts") or "")[:10]
        bucket = by_day.get(day)
        if bucket is None:
            bucket = {"day": day, "label": _day_label(day), "plans": [], "files": 0}
            by_day[day] = bucket
            days.append(bucket)
        group["time"] = str(group.get("ts") or "")[11:16]
        group["summary"] = _plan_summary(group["entries"])
        bucket["plans"].append(group)
        bucket["files"] = int(bucket["files"]) + len(group["entries"])
    return days


def _day_label(day: str) -> str:
    try:
        parsed = date.fromisoformat(day)
    except ValueError:
        return day or "Undated"
    today = datetime.now(UTC).date()
    if parsed == today:
        return "Today"
    if parsed == today - timedelta(days=1):
        return "Yesterday"
    return parsed.strftime("%-d %B %Y")


def _plan_summary(entries: list[dict[str, object]]) -> str:
    """What this plan did, in the words the buttons that caused it used."""
    count = len(entries)
    files = "file" if count == 1 else "files"
    if all(entry.get("action") == "undo_move" for entry in entries):
        return f"Put {count} {files} back"
    quarantined = sum(1 for entry in entries if entry.get("dest_root") == "quarantine")
    if quarantined == count:
        return f"Quarantined {count} {files}"
    filed = count - quarantined
    if quarantined:
        return f"Filed {filed} {files}, quarantined {quarantined}"
    return f"Filed {count} {files}"


def _journal_size(conn: sqlite3.Connection) -> int:
    """So "12 of 4,318" tells you the search worked, not that history is small."""
    return int(conn.execute("SELECT COUNT(*) FROM history").fetchone()[0])


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
