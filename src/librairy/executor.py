from __future__ import annotations

import errno
import logging
import os
import shutil
import signal
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from librairy.config import Settings
from librairy.fingerprint import blake2b_file
from librairy.lifecycle import assert_transition
from librairy.locks import acquire_lock
from librairy.paths import resolve_collision, validate_dest, validate_relpath
from librairy.planner import compute_plan_hash, utc_now
from librairy.quarantine import record_quarantine_entry
from librairy.search import sync_search_item

TERMINAL_RESULTS = {"done", "skipped_changed", "skipped_missing", "renamed_collision", "failed"}
LOGGER = logging.getLogger(__name__)


class ExecutionError(RuntimeError):
    pass


@dataclass(frozen=True)
class ExecutionSummary:
    plan_id: str
    done: int = 0
    skipped_changed: int = 0
    skipped_missing: int = 0
    renamed_collision: int = 0
    failed: int = 0

    @property
    def partial(self) -> bool:
        return self.skipped_changed > 0 or self.skipped_missing > 0 or self.failed > 0


def execute_plan(conn: sqlite3.Connection, plan_id: str, settings: Settings) -> ExecutionSummary:
    with acquire_lock(settings):
        return _execute_plan_unlocked(conn, plan_id, settings)


def _execute_plan_unlocked(
    conn: sqlite3.Connection,
    plan_id: str,
    settings: Settings,
) -> ExecutionSummary:
    plan = conn.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
    if plan is None:
        raise ExecutionError(f"plan not found: {plan_id}")
    if plan["status"] not in {"approved", "executing", "failed", "done"}:
        raise ExecutionError(f"plan must be approved before commit; status is {plan['status']}")
    if plan["status"] == "done":
        return ExecutionSummary(plan_id)
    if not plan["plan_hash"] or compute_plan_hash(conn, plan_id) != plan["plan_hash"]:
        raise ExecutionError("plan hash mismatch; refusing to touch files")

    conn.execute("UPDATE plans SET status='executing' WHERE id=?", (plan_id,))
    counts = {
        "done": 0,
        "skipped_changed": 0,
        "skipped_missing": 0,
        "renamed_collision": 0,
        "failed": 0,
    }
    rows = conn.execute(
        "SELECT * FROM plan_ops WHERE plan_id=? ORDER BY seq",
        (plan_id,),
    ).fetchall()
    for row in rows:
        if row["result"] in TERMINAL_RESULTS:
            continue
        try:
            result = _execute_op(conn, row, settings)
        except Exception as exc:
            result = "failed"
            _finish_op(conn, row["id"], result, None)
            _journal(conn, row, row["dest_relpath"], row["src_fingerprint"], str(exc))
        LOGGER.info(
            "plan=%s op=%s type=%s src=%s/%s dest=%s/%s result=%s",
            plan_id,
            row["id"],
            row["op_type"],
            row["src_root"],
            row["src_relpath"],
            row["dest_root"],
            row["dest_relpath"],
            result,
        )
        counts[result] += 1
        _test_pause_after_op()

    final_status = (
        "failed"
        if counts["failed"] or counts["skipped_changed"] or counts["skipped_missing"]
        else "done"
    )
    conn.execute(
        "UPDATE plans SET status=?, finished_at=? WHERE id=?",
        (final_status, utc_now(), plan_id),
    )
    return ExecutionSummary(plan_id, **counts)


def _execute_op(conn: sqlite3.Connection, row: sqlite3.Row, settings: Settings) -> str:
    src = validate_relpath(_root_path(settings, row["src_root"]), row["src_relpath"], kind="source")
    if not src.exists():
        _finish_op(conn, row["id"], "skipped_missing", None)
        _journal(conn, row, row["dest_relpath"], row["src_fingerprint"], "skipped_missing")
        return "skipped_missing"
    current_fingerprint = blake2b_file(src)
    if current_fingerprint != row["src_fingerprint"]:
        _finish_op(conn, row["id"], "skipped_changed", None)
        _journal(conn, row, row["dest_relpath"], current_fingerprint, "skipped_changed")
        return "skipped_changed"

    dest = validate_dest(_root_path(settings, row["dest_root"]), row["dest_relpath"])
    final_dest = resolve_collision(dest)
    final_dest.parent.mkdir(parents=True, exist_ok=True)
    _move_verified(src, final_dest, row["src_fingerprint"], row["plan_id"])
    dest_root = _root_path(settings, row["dest_root"]).resolve()
    final_relpath = final_dest.relative_to(dest_root).as_posix()
    result = "renamed_collision" if final_dest != dest else "done"
    _finish_op(conn, row["id"], result, final_relpath)
    _journal(conn, row, final_relpath, row["src_fingerprint"], "ok")
    _move_item_row(conn, row, final_relpath, final_dest)
    if row["op_type"] == "quarantine":
        record_quarantine_entry(conn, row)
    return result


def _move_verified(src: Path, dest: Path, fingerprint: str, plan_id: str) -> None:
    try:
        os.rename(src, dest)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        temp = dest.with_name(f"{dest.name}.part-{plan_id}")
        if temp.exists():
            temp.unlink()
        shutil.copy2(src, temp)
        if blake2b_file(temp) != fingerprint:
            temp.unlink(missing_ok=True)
            raise ExecutionError("destination fingerprint mismatch after copy") from None
        os.replace(temp, dest)
        os.remove(src)


def _finish_op(
    conn: sqlite3.Connection,
    op_id: int,
    result: str,
    final_relpath: str | None,
) -> None:
    conn.execute(
        "UPDATE plan_ops SET result=?, final_relpath=?, executed_at=? WHERE id=?",
        (result, final_relpath, utc_now(), op_id),
    )


def _journal(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    final_relpath: str | None,
    fingerprint: str | None,
    outcome: str,
) -> None:
    action = "quarantine" if row["op_type"] == "quarantine" else "move"
    conn.execute(
        """
        INSERT INTO history(
          ts, plan_id, op_id, action, src_root, src_relpath, dest_root, dest_relpath,
          fingerprint, outcome
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            utc_now(),
            row["plan_id"],
            row["id"],
            action,
            row["src_root"],
            row["src_relpath"],
            row["dest_root"],
            final_relpath or row["dest_relpath"],
            fingerprint,
            outcome,
        ),
    )


def _move_item_row(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    final_relpath: str,
    final_dest: Path,
) -> None:
    stat = final_dest.stat()
    state = "quarantined" if row["dest_root"] == "quarantine" else "discovered"
    current = conn.execute("SELECT state FROM items WHERE id=?", (row["item_id"],)).fetchone()
    if current is not None:
        assert_transition(current["state"], state)
    conn.execute(
        """
        UPDATE items SET root=?, relpath=?, size=?, mtime_ns=?, state=?,
          last_seen_at=?, missing_since=NULL
        WHERE id=?
        """,
        (
            row["dest_root"],
            final_relpath,
            stat.st_size,
            stat.st_mtime_ns,
            state,
            utc_now(),
            row["item_id"],
        ),
    )
    sync_search_item(conn, row["item_id"])


def _root_path(settings: Settings, root: str) -> Path:
    if root == "inbox":
        return settings.inbox_dir
    if root == "library":
        return settings.library_dir
    if root == "quarantine":
        return settings.quarantine_dir
    raise ExecutionError(f"unknown root: {root}")


def _test_pause_after_op() -> None:
    marker = os.environ.get("LIBRAIRY_TEST_PAUSE_AFTER_OP_MARKER")
    if not marker:
        return
    Path(marker).write_text(str(os.getpid()), encoding="utf-8")
    if hasattr(signal, "pause"):
        signal.pause()
    else:  # pragma: no cover - non-POSIX fallback
        time.sleep(60)
