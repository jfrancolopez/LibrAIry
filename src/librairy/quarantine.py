from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from librairy.config import Settings
from librairy.fingerprint import blake2b_file
from librairy.lifecycle import assert_transition
from librairy.locks import acquire_lock
from librairy.paths import resolve_collision, validate_dest, validate_relpath
from librairy.planner import OperationSpec, utc_now
from librairy.search import sync_search_item


class QuarantineError(RuntimeError):
    pass


@dataclass(frozen=True)
class RestoreResult:
    entry_id: int
    outcome: str
    dest_relpath: str | None = None


def quarantine_operation(src_relpath: str, date: str | None = None) -> OperationSpec:
    day = date or datetime.now(UTC).date().isoformat()
    return OperationSpec("quarantine", src_relpath, "quarantine", f"{day}/{src_relpath}")


def record_quarantine_entry(conn: sqlite3.Connection, op: sqlite3.Row) -> None:
    conn.execute(
        """
        INSERT INTO quarantine_entries(
          item_id, reason, duplicate_of, original_root, original_relpath, quarantined_at, plan_id
        ) VALUES (?, 'exact_duplicate', NULL, ?, ?, ?, ?)
        """,
        (op["item_id"], op["src_root"], op["src_relpath"], utc_now(), op["plan_id"]),
    )


def list_quarantine_entries(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM quarantine_entries ORDER BY id"))


def restore_entry(conn: sqlite3.Connection, entry_id: int, settings: Settings) -> RestoreResult:
    with acquire_lock(settings):
        return _restore_entry_unlocked(conn, entry_id, settings)


def _restore_entry_unlocked(
    conn: sqlite3.Connection,
    entry_id: int,
    settings: Settings,
) -> RestoreResult:
    from librairy.executor import _move_verified

    entry = conn.execute("SELECT * FROM quarantine_entries WHERE id=?", (entry_id,)).fetchone()
    if entry is None:
        raise QuarantineError(f"quarantine entry not found: {entry_id}")
    if entry["restored_at"] is not None:
        return RestoreResult(entry_id, "already_restored")
    item = conn.execute("SELECT * FROM items WHERE id=?", (entry["item_id"],)).fetchone()
    if item is None or item["root"] != "quarantine":
        return RestoreResult(entry_id, "missing")
    src = validate_relpath(settings.quarantine_dir, item["relpath"], kind="source")
    if not src.exists():
        return RestoreResult(entry_id, "missing")
    fingerprint = blake2b_file(src)
    dest = validate_dest(_root_path(settings, entry["original_root"]), entry["original_relpath"])
    final_dest = resolve_collision(dest)
    final_dest.parent.mkdir(parents=True, exist_ok=True)
    _move_verified(src, final_dest, fingerprint, f"restore-{entry_id}")
    root_path = _root_path(settings, entry["original_root"]).resolve()
    final_relpath = final_dest.relative_to(root_path).as_posix()
    stat = final_dest.stat()
    assert_transition(item["state"], "discovered")
    conn.execute(
        """
        UPDATE items SET root=?, relpath=?, size=?, mtime_ns=?, state='discovered',
          last_seen_at=?, missing_since=NULL
        WHERE id=?
        """,
        (
            entry["original_root"],
            final_relpath,
            stat.st_size,
            stat.st_mtime_ns,
            utc_now(),
            item["id"],
        ),
    )
    sync_search_item(conn, item["id"])
    conn.execute("UPDATE quarantine_entries SET restored_at=? WHERE id=?", (utc_now(), entry_id))
    conn.execute(
        """
        INSERT INTO history(
          ts, plan_id, op_id, action, src_root, src_relpath, dest_root, dest_relpath,
          fingerprint, outcome
        ) VALUES (?, ?, NULL, 'restore_quarantine', 'quarantine', ?, ?, ?, ?, 'ok')
        """,
        (
            utc_now(),
            entry["plan_id"],
            item["relpath"],
            entry["original_root"],
            final_relpath,
            fingerprint,
        ),
    )
    return RestoreResult(entry_id, "ok", final_relpath)


def restore_all(conn: sqlite3.Connection, settings: Settings) -> list[RestoreResult]:
    rows = conn.execute(
        "SELECT id FROM quarantine_entries WHERE restored_at IS NULL ORDER BY id"
    ).fetchall()
    with acquire_lock(settings):
        return [_restore_entry_unlocked(conn, row["id"], settings) for row in rows]


def _root_path(settings: Settings, root: str):
    if root == "inbox":
        return settings.inbox_dir
    if root == "library":
        return settings.library_dir
    if root == "quarantine":
        return settings.quarantine_dir
    raise QuarantineError(f"unknown root: {root}")
