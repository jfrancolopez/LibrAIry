from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from librairy.config import Settings
from librairy.db import database_path
from librairy.paths import validate_relpath
from librairy.planner import utc_now
from librairy.tools.rclone import RcloneStatus, check_command, copy_command, rclone_status, run

MAX_BACKUP_ATTEMPTS = 3


@dataclass(frozen=True)
class BackupQueueResult:
    queued: int


@dataclass(frozen=True)
class BackupRunSummary:
    copied: int = 0
    failed: int = 0
    paused: bool = False
    warning: str = ""


def rclone_config_path(settings: Settings) -> Path:
    return settings.appdata_dir / "rclone" / "rclone.conf"


def backup_status(settings: Settings) -> RcloneStatus:
    if not settings.backup_enabled:
        return RcloneStatus(False, "backup disabled")
    if not settings.backup_remote:
        return RcloneStatus(False, "backup remote not configured")
    return rclone_status(rclone_config_path(settings))


def enqueue_backup_item(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    item_id: int,
    relpath: str,
    fingerprint: str,
) -> bool:
    if not settings.backup_enabled:
        return False
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO backup_queue(
          item_id, relpath, fingerprint, state, attempts, created_at, updated_at
        ) VALUES (?, ?, ?, 'queued', 0, ?, ?)
        """,
        (item_id, relpath, fingerprint, utc_now(), utc_now()),
    )
    return cursor.rowcount == 1


def enqueue_plan_outputs(
    conn: sqlite3.Connection,
    settings: Settings,
    plan_id: str,
) -> BackupQueueResult:
    queued = 0
    rows = conn.execute(
        """
        SELECT op.item_id, op.final_relpath, op.src_fingerprint
        FROM plan_ops op
        WHERE op.plan_id=?
          AND op.dest_root='library'
          AND op.result IN ('done','renamed_collision')
          AND op.final_relpath IS NOT NULL
        """,
        (plan_id,),
    ).fetchall()
    for row in rows:
        if enqueue_backup_item(
            conn,
            settings,
            item_id=row["item_id"],
            relpath=row["final_relpath"],
            fingerprint=row["src_fingerprint"],
        ):
            queued += 1
    return BackupQueueResult(queued)


def run_backup_once(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    batch_size: int = 50,
) -> BackupRunSummary:
    status = backup_status(settings)
    if not status.available:
        return BackupRunSummary(paused=True, warning=status.detail)
    rows = conn.execute(
        """
        SELECT * FROM backup_queue
        WHERE state IN ('queued','failed') AND attempts < ?
        ORDER BY id
        LIMIT ?
        """,
        (MAX_BACKUP_ATTEMPTS, batch_size),
    ).fetchall()
    copied = failed = 0
    for row in rows:
        if _copy_and_verify(conn, settings, row):
            copied += 1
        else:
            failed += 1
    return BackupRunSummary(copied=copied, failed=failed)


def _copy_and_verify(conn: sqlite3.Connection, settings: Settings, row: sqlite3.Row) -> bool:
    source = validate_relpath(settings.library_dir, row["relpath"], kind="source")
    remote = _remote_path(settings.backup_remote, row["relpath"])
    conn.execute(
        "UPDATE backup_queue SET state='copying', updated_at=? WHERE id=?",
        (utc_now(), row["id"]),
    )
    copy = run(
        copy_command(
            rclone_config_path(settings),
            source,
            remote,
            settings.backup_bandwidth_limit,
        )
    )
    check = None
    if copy.returncode == 0:
        check = run(check_command(rclone_config_path(settings), source, remote))
    if copy.returncode == 0 and check is not None and check.returncode == 0:
        conn.execute(
            """
            UPDATE backup_queue
            SET state='done', last_error=NULL, updated_at=?
            WHERE id=?
            """,
            (utc_now(), row["id"]),
        )
        return True
    error = _process_error(copy, check)
    attempts = int(row["attempts"]) + 1
    state = "failed"
    conn.execute(
        """
        UPDATE backup_queue
        SET state=?, attempts=?, last_error=?, updated_at=?
        WHERE id=?
        """,
        (state, attempts, error[:500], utc_now(), row["id"]),
    )
    return False


def snapshot_database(settings: Settings, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(database_path(settings))
    target = sqlite3.connect(destination)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    return destination


def _remote_path(remote: str, relpath: str) -> str:
    return f"{remote.rstrip('/')}/{relpath}"


def _process_error(copy, check) -> str:  # noqa: ANN001
    failed = check if check is not None and check.returncode != 0 else copy
    return failed.stderr.strip() or failed.stdout.strip() or f"rclone exited {failed.returncode}"
