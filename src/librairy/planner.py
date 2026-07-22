from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from librairy.config import Settings
from librairy.paths import PathValidationError, validate_dest


class PlanError(RuntimeError):
    pass


class PlanApprovalError(PlanError):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass(frozen=True)
class OperationSpec:
    op_type: str
    src_relpath: str
    dest_root: str
    dest_relpath: str
    src_root: str = "inbox"


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def create_plan(
    conn: sqlite3.Connection,
    specs: list[OperationSpec],
    settings: Settings,
) -> str:
    plan_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO plans(id, status, created_at) VALUES (?, 'draft', ?)",
        (plan_id, utc_now()),
    )
    for seq, spec in enumerate(specs, start=1):
        add_plan_op(conn, plan_id, seq, spec, settings)
    return plan_id


def add_plan_op(
    conn: sqlite3.Connection,
    plan_id: str,
    seq: int,
    spec: OperationSpec,
    settings: Settings,
) -> int:
    status = _plan_status(conn, plan_id)
    if status != "draft":
        raise PlanError(f"plan {plan_id} is immutable because status is {status}")
    if spec.op_type not in {"move", "quarantine"}:
        raise PlanError(f"unsupported op_type: {spec.op_type}")
    item = conn.execute(
        """
        SELECT id, fingerprint FROM items
        WHERE root=? AND relpath=? AND missing_since IS NULL AND fingerprint IS NOT NULL
        """,
        (spec.src_root, spec.src_relpath),
    ).fetchone()
    if item is None:
        raise PlanError(f"source not ready: {spec.src_root}:{spec.src_relpath}")
    _validate_dest_root(spec.dest_root)
    validate_dest(_root_path(settings, spec.dest_root), spec.dest_relpath)
    cursor = conn.execute(
        """
        INSERT INTO plan_ops(
          plan_id, seq, op_type, item_id, src_root, src_relpath, src_fingerprint,
          dest_root, dest_relpath
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            plan_id,
            seq,
            spec.op_type,
            item["id"],
            spec.src_root,
            spec.src_relpath,
            item["fingerprint"],
            spec.dest_root,
            spec.dest_relpath,
        ),
    )
    return int(cursor.lastrowid)


def approve_plan(conn: sqlite3.Connection, plan_id: str, settings: Settings) -> str:
    status = _plan_status(conn, plan_id)
    if status != "draft":
        raise PlanError(f"only draft plans can be approved; current status is {status}")
    errors = _approval_errors(conn, plan_id, settings)
    if errors:
        raise PlanApprovalError(errors)
    plan_hash = compute_plan_hash(conn, plan_id)
    conn.execute(
        "UPDATE plans SET status='approved', plan_hash=?, approved_at=? WHERE id=?",
        (plan_hash, utc_now(), plan_id),
    )
    return plan_hash


def compute_plan_hash(conn: sqlite3.Connection, plan_id: str) -> str:
    import hashlib

    payload = canonical_plan_ops(conn, plan_id)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def canonical_plan_ops(conn: sqlite3.Connection, plan_id: str) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT seq, op_type, src_root, src_relpath, src_fingerprint, dest_root, dest_relpath
        FROM plan_ops WHERE plan_id=? ORDER BY seq
        """,
        (plan_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def load_operation_specs(path: Path) -> list[OperationSpec]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise PlanError("operation spec file must contain a JSON list")
    return [OperationSpec(**item) for item in data]


def _approval_errors(conn: sqlite3.Connection, plan_id: str, settings: Settings) -> list[str]:
    rows = conn.execute(
        "SELECT * FROM plan_ops WHERE plan_id=? ORDER BY seq",
        (plan_id,),
    ).fetchall()
    errors: list[str] = []
    seen_sources: set[tuple[str, str]] = set()
    seen_dests: set[tuple[str, str]] = set()
    for row in rows:
        source = (row["src_root"], row["src_relpath"])
        dest = (row["dest_root"], row["dest_relpath"])
        prefix = f"op {row['seq']}:"
        item = conn.execute(
            "SELECT id FROM items WHERE root=? AND relpath=? AND missing_since IS NULL",
            source,
        ).fetchone()
        if item is None:
            errors.append(f"{prefix} source is missing: {source[0]}:{source[1]}")
        if source in seen_sources:
            errors.append(f"{prefix} duplicate source: {source[0]}:{source[1]}")
        seen_sources.add(source)
        if dest in seen_dests:
            errors.append(f"{prefix} duplicate destination: {dest[0]}:{dest[1]}")
        seen_dests.add(dest)
        try:
            _validate_dest_root(row["dest_root"])
            validate_dest(_root_path(settings, row["dest_root"]), row["dest_relpath"])
        except (PathValidationError, PlanError) as exc:
            errors.append(f"{prefix} invalid destination: {exc}")
    if not rows:
        errors.append("plan has no operations")
    return errors


def _plan_status(conn: sqlite3.Connection, plan_id: str) -> str:
    row = conn.execute("SELECT status FROM plans WHERE id=?", (plan_id,)).fetchone()
    if row is None:
        raise PlanError(f"plan not found: {plan_id}")
    return str(row["status"])


def _validate_dest_root(root: str) -> None:
    if root not in {"library", "quarantine"}:
        raise PlanError("destination root must be library or quarantine")


def _root_path(settings: Settings, root: str) -> Path:
    if root == "inbox":
        return settings.inbox_dir
    if root == "library":
        return settings.library_dir
    if root == "quarantine":
        return settings.quarantine_dir
    raise PlanError(f"unknown root: {root}")
