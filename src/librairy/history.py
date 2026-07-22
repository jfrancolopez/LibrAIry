from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from librairy.config import Settings
from librairy.executor import _move_verified, _root_path
from librairy.fingerprint import blake2b_file
from librairy.locks import acquire_lock
from librairy.paths import resolve_collision, validate_dest, validate_relpath
from librairy.planner import utc_now


class UndoError(RuntimeError):
    pass


@dataclass(frozen=True)
class UndoResult:
    history_id: int
    outcome: str
    dest_relpath: str | None = None


def list_history(
    conn: sqlite3.Connection,
    plan_id: str | None = None,
    limit: int = 50,
) -> list[sqlite3.Row]:
    if plan_id:
        return list(
            conn.execute(
                "SELECT * FROM history WHERE plan_id=? ORDER BY id DESC LIMIT ?",
                (plan_id, limit),
            )
        )
    return list(conn.execute("SELECT * FROM history ORDER BY id DESC LIMIT ?", (limit,)))


def undo_plan(conn: sqlite3.Connection, plan_id: str, settings: Settings) -> list[UndoResult]:
    rows = conn.execute(
        "SELECT id FROM history WHERE plan_id=? AND outcome='ok' ORDER BY id DESC",
        (plan_id,),
    ).fetchall()
    return [undo_op(conn, row["id"], settings) for row in rows]


def undo_op(conn: sqlite3.Connection, history_id: int, settings: Settings) -> UndoResult:
    with acquire_lock(settings):
        return _undo_op_unlocked(conn, history_id, settings)


def _undo_op_unlocked(
    conn: sqlite3.Connection,
    history_id: int,
    settings: Settings,
) -> UndoResult:
    entry = conn.execute("SELECT * FROM history WHERE id=?", (history_id,)).fetchone()
    if entry is None:
        raise UndoError(f"history entry not found: {history_id}")
    src = validate_relpath(
        _root_path(settings, entry["dest_root"]),
        entry["dest_relpath"],
        kind="source",
    )
    if not src.exists():
        return _record_refused(conn, entry, "undo_refused_missing")
    current_fingerprint = blake2b_file(src)
    if entry["fingerprint"] and current_fingerprint != entry["fingerprint"]:
        return _record_refused(
            conn,
            entry,
            f"undo_refused_changed expected={entry['fingerprint']} actual={current_fingerprint}",
        )

    dest = validate_dest(_root_path(settings, entry["src_root"]), entry["src_relpath"])
    final_dest = resolve_collision(dest)
    final_dest.parent.mkdir(parents=True, exist_ok=True)
    _move_verified(src, final_dest, current_fingerprint, f"undo-{history_id}")
    src_root = _root_path(settings, entry["src_root"]).resolve()
    final_relpath = final_dest.relative_to(src_root).as_posix()
    _record_undo(conn, entry, final_relpath, current_fingerprint, "ok")
    _update_item_after_undo(conn, entry, final_relpath, final_dest)
    return UndoResult(history_id, "ok", final_relpath)


def _record_refused(conn: sqlite3.Connection, entry: sqlite3.Row, outcome: str) -> UndoResult:
    _record_undo(conn, entry, entry["src_relpath"], entry["fingerprint"], outcome)
    return UndoResult(entry["id"], outcome)


def _record_undo(
    conn: sqlite3.Connection,
    entry: sqlite3.Row,
    final_relpath: str,
    fingerprint: str | None,
    outcome: str,
) -> None:
    action = "undo_quarantine" if entry["action"] == "quarantine" else "undo_move"
    conn.execute(
        """
        INSERT INTO history(
          ts, plan_id, op_id, action, src_root, src_relpath, dest_root, dest_relpath,
          fingerprint, outcome
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            utc_now(),
            entry["plan_id"],
            entry["op_id"],
            action,
            entry["dest_root"],
            entry["dest_relpath"],
            entry["src_root"],
            final_relpath,
            fingerprint,
            outcome,
        ),
    )


def _update_item_after_undo(
    conn: sqlite3.Connection,
    entry: sqlite3.Row,
    final_relpath: str,
    final_dest,
) -> None:
    stat = final_dest.stat()
    conn.execute(
        """
        UPDATE items SET root=?, relpath=?, size=?, mtime_ns=?, last_seen_at=?, missing_since=NULL
        WHERE root=? AND relpath=?
        """,
        (
            entry["src_root"],
            final_relpath,
            stat.st_size,
            stat.st_mtime_ns,
            utc_now(),
            entry["dest_root"],
            entry["dest_relpath"],
        ),
    )
