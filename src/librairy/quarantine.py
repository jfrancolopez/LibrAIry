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

#  Quarantine is the out-tray: set aside indefinitely, still yours. This is the
#  shelf inside it for the files you have finished with -- one folder, so you
#  can point a file manager at it and empty it in one gesture. LibrAIry still
#  never deletes anything; it only ever gathers.
DELETE_PILE = "_to-delete"


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


def deletion_operation(src_relpath: str, date: str | None = None) -> OperationSpec:
    """Straight to the delete pile, skipping the shelf you might change your
    mind on. Still a quarantine move: same planner, same hash check, same
    journal, and "Put it back" still works."""
    day = date or datetime.now(UTC).date().isoformat()
    return OperationSpec(
        "quarantine", src_relpath, "quarantine", f"{DELETE_PILE}/{day}/{src_relpath}"
    )


def marked_for_deletion(relpath: str | None) -> bool:
    return bool(relpath) and str(relpath).replace("\\", "/").split("/", 1)[0] == DELETE_PILE


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


def mark_entry_for_deletion(
    conn: sqlite3.Connection, entry_id: int, settings: Settings
) -> RestoreResult:
    """Move a held file into the delete pile. Still does not delete it.

    The out-tray answers "I do not want this in the library". It does not
    answer "I am done with this file", and without somewhere to say so the
    only way to act on a quarantine of two hundred files is to go through it
    by hand in a file manager. This gathers the ones you have finished with
    into a single folder you can empty in one gesture, deliberately, yourself.

    "Put it back" still works from here: the entry remembers where the file
    came from, not where it is sitting now.
    """
    with acquire_lock(settings):
        return _mark_entry_unlocked(conn, entry_id, settings)


def _mark_entry_unlocked(
    conn: sqlite3.Connection, entry_id: int, settings: Settings
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
    if marked_for_deletion(item["relpath"]):
        return RestoreResult(entry_id, "already_marked", item["relpath"])
    src = validate_relpath(settings.quarantine_dir, item["relpath"], kind="source")
    if not src.exists():
        return RestoreResult(entry_id, "missing")
    fingerprint = blake2b_file(src)
    dest = validate_dest(settings.quarantine_dir, f"{DELETE_PILE}/{item['relpath']}")
    final_dest = resolve_collision(dest)
    final_dest.parent.mkdir(parents=True, exist_ok=True)
    _move_verified(src, final_dest, fingerprint, f"mark-delete-{entry_id}")
    final_relpath = final_dest.relative_to(settings.quarantine_dir.resolve()).as_posix()
    stat = final_dest.stat()
    conn.execute(
        "UPDATE items SET relpath=?, size=?, mtime_ns=?, last_seen_at=? WHERE id=?",
        (final_relpath, stat.st_size, stat.st_mtime_ns, utc_now(), item["id"]),
    )
    sync_search_item(conn, item["id"])
    conn.execute(
        """
        INSERT INTO history(
          ts, plan_id, op_id, action, src_root, src_relpath, dest_root, dest_relpath,
          fingerprint, outcome
        ) VALUES (?, ?, NULL, 'mark_for_deletion', 'quarantine', ?, 'quarantine', ?, ?, 'ok')
        """,
        (utc_now(), entry["plan_id"], item["relpath"], final_relpath, fingerprint),
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
