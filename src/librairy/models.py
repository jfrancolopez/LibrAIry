from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Item:
    id: int
    root: str
    relpath: str
    size: int
    mtime_ns: int
    fingerprint: str | None
    state: str
    first_seen_at: str
    last_seen_at: str
    missing_since: str | None


@dataclass(frozen=True)
class Plan:
    id: str
    status: str
    plan_hash: str | None
    created_at: str
    approved_at: str | None
    finished_at: str | None


@dataclass(frozen=True)
class PlanOp:
    id: int
    plan_id: str
    seq: int
    op_type: str
    item_id: int | None
    src_root: str
    src_relpath: str
    src_fingerprint: str
    dest_root: str
    dest_relpath: str
    result: str | None
    final_relpath: str | None
    executed_at: str | None


@dataclass(frozen=True)
class HistoryEntry:
    id: int
    ts: str
    plan_id: str | None
    op_id: int | None
    action: str
    src_root: str
    src_relpath: str
    dest_root: str
    dest_relpath: str
    fingerprint: str | None
    outcome: str
