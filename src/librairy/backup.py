from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from librairy.config import Settings
from librairy.db import database_path
from librairy.planner import utc_now
from librairy.tools.rclone import RcloneStatus, rclone_status


@dataclass(frozen=True)
class BackupQueueResult:
    queued: int


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
