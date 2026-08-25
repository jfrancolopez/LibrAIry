from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from librairy.config import Settings
from librairy.optimization_source import (
    SourceRefused,
    is_optimization_source,
    resolve_optimization_source,
)
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
    # Only for a source in the `optimization` namespace, which has no `items`
    # row to read a fingerprint from and could not have one — `items.root` is
    # CHECK-constrained to the three user roots. The caller supplies the hash
    # recorded when the output was verified, and `optimization_source` refuses
    # the operation at approval and again at execution unless it is exactly
    # that hash, on exactly that job's output.
    src_fingerprint: str = ""


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
    if is_optimization_source(spec.src_root):
        # A generated file has no item row, so there is nothing to look up and
        # nothing to be missing. Its authorization is the job, checked in full
        # by `_approval_errors` before this plan can be approved.
        if not spec.src_fingerprint:
            raise PlanError("an optimization source must carry its own fingerprint")
        item_id, fingerprint = None, spec.src_fingerprint
    else:
        item = conn.execute(
            """
            SELECT id, fingerprint FROM items
            WHERE root=? AND relpath=? AND missing_since IS NULL
              AND fingerprint IS NOT NULL
            """,
            (spec.src_root, spec.src_relpath),
        ).fetchone()
        if item is None:
            raise PlanError(f"source not ready: {spec.src_root}:{spec.src_relpath}")
        item_id, fingerprint = item["id"], item["fingerprint"]
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
            item_id,
            spec.src_root,
            spec.src_relpath,
            fingerprint,
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
    #  What was true about the relationships this decision touches, at the
    #  moment it was approved. Imported here rather than at module scope
    #  because relationships are recorded with this module's own clock.
    #
    #  It records; it never refuses. A decision that separates a RAW from its
    #  JPEG is a decision somebody is allowed to make, and approval is not
    #  where LibrAIry gets an opinion about it.
    from librairy.relationship_impact import snapshot

    snapshot(conn, plan_id)
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


def create_plan_from_proposals(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    min_confidence: float,
    proposal_ids: list[int] | None = None,
) -> str:
    sql = """
        SELECT p.*, i.relpath AS src_relpath
        FROM proposals p JOIN items i ON i.id=p.item_id
        WHERE p.status='proposed' AND p.dest_relpath IS NOT NULL AND p.confidence>=?
    """
    params: list[object] = [min_confidence]
    if proposal_ids:
        placeholders = ",".join("?" for _ in proposal_ids)
        sql += f" AND p.id IN ({placeholders})"
        params.extend(proposal_ids)
    rows = conn.execute(sql, params).fetchall()
    specs = [
        OperationSpec(row["action"], row["src_relpath"], row["dest_root"], row["dest_relpath"])
        for row in rows
    ]
    return create_plan(conn, specs, settings)


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
        if is_optimization_source(row["src_root"]):
            # No item row, by construction. The whole authorization chain runs
            # instead — plan -> job -> canonical output -> recorded hash ->
            # this operation -> the bytes on disk — and it runs here, at
            # approval, so a plan that could never execute is never approved.
            try:
                resolve_optimization_source(
                    conn,
                    settings,
                    plan_id=plan_id,
                    src_relpath=row["src_relpath"],
                    src_fingerprint=row["src_fingerprint"],
                    dest_root=row["dest_root"],
                )
            except SourceRefused as exc:
                errors.append(f"{prefix} {exc}")
        else:
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


# Where a plan is allowed to put a file. `inbox` is here for exactly one
# reason: putting a quarantined file back where it came from, and a file
# quarantined by the duplicate finder came from the inbox — it was never filed.
# Restoring it to the library instead would file something the owner has not
# reviewed, which is the one thing the inbox exists to prevent.
#
# Nothing else may aim at the inbox. Analysis and Commit both move files *out*
# of it, and a plan that put one back would loop.
DEST_ROOTS = frozenset({"library", "quarantine", "inbox"})


def _validate_dest_root(root: str) -> None:
    if root not in DEST_ROOTS:
        raise PlanError("destination root must be library, quarantine or inbox")


def _root_path(settings: Settings, root: str) -> Path:
    if root == "inbox":
        return settings.inbox_dir
    if root == "library":
        return settings.library_dir
    if root == "quarantine":
        return settings.quarantine_dir
    raise PlanError(f"unknown root: {root}")
