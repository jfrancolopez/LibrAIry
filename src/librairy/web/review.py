from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from librairy.lifecycle import transition_item
from librairy.planner import utc_now
from librairy.proposals import decode_evidence

PAGE_SIZE = 50


@dataclass(frozen=True)
class ReviewFilters:
    category: str | None = None
    state: str = "proposed"
    min_confidence: float | None = None
    max_confidence: float | None = None
    has_destination: bool | None = None
    page: int = 1


def review_data(conn: sqlite3.Connection, filters: ReviewFilters) -> dict[str, object]:
    rows = _proposal_rows(conn, filters)
    total = _proposal_count(conn, filters)
    return {
        "filters": filters,
        "groups": _group_rows(rows),
        "total": total,
        "page_size": PAGE_SIZE,
        "has_next": filters.page * PAGE_SIZE < total,
        "has_prev": filters.page > 1,
    }


def apply_review_action(
    conn: sqlite3.Connection,
    action: str,
    filters: ReviewFilters,
    *,
    proposal_ids: list[int] | None = None,
    all_matching: bool = False,
) -> int:
    if action not in {"approve", "reject", "postpone"}:
        raise ValueError(f"unknown review action: {action}")
    targets = _matching_ids(conn, filters) if all_matching else proposal_ids or []
    if not targets:
        return 0
    status = {"approve": "approved", "reject": "rejected", "postpone": "postponed"}[action]
    item_state = {"approve": "approved", "reject": "pending", "postpone": "postponed"}[action]
    sql = f"""
        SELECT id, item_id
        FROM proposals
        WHERE status='proposed' AND id IN ({_placeholders(targets)})
        """
    rows = conn.execute(
        sql,
        targets,
    ).fetchall()
    for row in rows:
        transition_item(conn, row["item_id"], item_state)
        conn.execute(
            "UPDATE proposals SET status=?, updated_at=? WHERE id=?",
            (status, utc_now(), row["id"]),
        )
    return len(rows)


def evidence_lines(payload: str) -> list[str]:
    lines: list[str] = []
    for entry in decode_evidence(payload):
        source = entry.source.upper()
        if entry.source == "ai":
            source = f"AI:{entry.detail.split(':', 1)[0]}"
            if "/cloud" in entry.detail:
                source = f"CLOUD {source}"
        lines.append(f"[{source}] {entry.field} {entry.detail} {entry.weight:.2f}")
    return lines


def filters_from_query(
    *,
    category: str | None = None,
    state: str = "proposed",
    min_confidence: float | None = None,
    max_confidence: float | None = None,
    has_destination: str | None = None,
    page: int = 1,
) -> ReviewFilters:
    destination_filter = None
    if has_destination == "yes":
        destination_filter = True
    elif has_destination == "no":
        destination_filter = False
    return ReviewFilters(
        category=category or None,
        state=state,
        min_confidence=min_confidence,
        max_confidence=max_confidence,
        has_destination=destination_filter,
        page=max(1, page),
    )


def _proposal_rows(conn: sqlite3.Connection, filters: ReviewFilters) -> list[dict[str, Any]]:
    where, params = _where(filters)
    params = [*params, PAGE_SIZE, (filters.page - 1) * PAGE_SIZE]
    rows = conn.execute(
        f"""
        SELECT p.*, i.relpath AS item_relpath, i.state AS item_state,
               g.kind AS group_kind, g.label AS group_label
        FROM proposals p
        JOIN items i ON i.id = p.item_id
        LEFT JOIN groups g ON g.id = p.group_id
        WHERE {where}
        ORDER BY COALESCE(g.kind, 'ungrouped'), COALESCE(g.label, 'Ungrouped'),
                 p.confidence DESC, p.id DESC
        LIMIT ? OFFSET ?
        """,
        params,
    ).fetchall()
    return [{**dict(row), "evidence_lines": evidence_lines(row["evidence"])} for row in rows]


def _proposal_count(conn: sqlite3.Connection, filters: ReviewFilters) -> int:
    where, params = _where(filters)
    return int(
        conn.execute(
            f"""
            SELECT COUNT(*)
            FROM proposals p
            JOIN items i ON i.id = p.item_id
            WHERE {where}
            """,
            params,
        ).fetchone()[0]
    )


def _matching_ids(conn: sqlite3.Connection, filters: ReviewFilters) -> list[int]:
    where, params = _where(filters)
    return [
        int(row["id"])
        for row in conn.execute(
            f"""
            SELECT p.id
            FROM proposals p
            JOIN items i ON i.id = p.item_id
            WHERE {where}
            """,
            params,
        )
    ]


def _group_rows(rows: list[dict[str, Any]]) -> list[dict[str, object]]:
    groups: list[dict[str, object]] = []
    by_key: dict[tuple[str, str], dict[str, object]] = {}
    for row in rows:
        key = (row["group_kind"] or "ungrouped", row["group_label"] or "Ungrouped")
        group = by_key.get(key)
        if group is None:
            group = {"kind": key[0], "label": key[1], "rows": []}
            by_key[key] = group
            groups.append(group)
        group_rows = group["rows"]
        assert isinstance(group_rows, list)
        group_rows.append(row)
    return groups


def _where(filters: ReviewFilters) -> tuple[str, list[object]]:
    clauses = ["p.status = ?"]
    params: list[object] = [filters.state]
    if filters.category:
        clauses.append("p.category = ?")
        params.append(filters.category)
    if filters.min_confidence is not None:
        clauses.append("p.confidence >= ?")
        params.append(filters.min_confidence)
    if filters.max_confidence is not None:
        clauses.append("p.confidence <= ?")
        params.append(filters.max_confidence)
    if filters.has_destination is True:
        clauses.append("p.dest_relpath IS NOT NULL")
    elif filters.has_destination is False:
        clauses.append("p.dest_relpath IS NULL")
    return " AND ".join(clauses), params


def _placeholders(values: list[int]) -> str:
    return ",".join("?" for _ in values)
