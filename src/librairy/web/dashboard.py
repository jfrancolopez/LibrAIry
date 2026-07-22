from __future__ import annotations

import json
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from librairy.config import Settings
from librairy.lifecycle import state_counts


@dataclass(frozen=True)
class DiskStat:
    root: str
    free_gb: float
    total_gb: float
    percent_free: int


def dashboard_data(conn: sqlite3.Connection, settings: Settings) -> dict[str, object]:
    worker_state = _worker_state(conn)
    counts = state_counts(conn)
    proposals = conn.execute(
        "SELECT COUNT(*) FROM proposals WHERE status='proposed'"
    ).fetchone()[0]
    backup = {
        row["state"]: row["count"]
        for row in conn.execute("SELECT state, COUNT(*) AS count FROM backup_queue GROUP BY state")
    }
    return {
        "worker_state": worker_state,
        "current_phase": worker_state.get("current_phase", "unknown"),
        "counts": counts,
        "proposal_count": proposals,
        "recent_history": _recent_history(conn),
        "providers": _providers(conn),
        "disks": _disk_stats(settings),
        "host_inbox_dir": settings.host_inbox_dir,
        "backup_counts": backup,
    }


def _worker_state(conn: sqlite3.Connection) -> dict[str, object]:
    state: dict[str, object] = {}
    for row in conn.execute("SELECT key, value FROM worker_state"):
        try:
            state[row["key"]] = json.loads(row["value"])
        except json.JSONDecodeError:
            state[row["key"]] = row["value"]
    return state


def _recent_history(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM history ORDER BY id DESC LIMIT 5"))


def _providers(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM provider_status ORDER BY name LIMIT 8"))


def _disk_stats(settings: Settings) -> list[DiskStat]:
    roots = {
        "inbox": settings.inbox_dir,
        "library": settings.library_dir,
        "quarantine": settings.quarantine_dir,
        "appdata": settings.appdata_dir,
    }
    stats: list[DiskStat] = []
    for name, path in roots.items():
        usage = shutil.disk_usage(_existing_path(path))
        free_gb = usage.free / 1024**3
        total_gb = usage.total / 1024**3
        percent_free = round((usage.free / usage.total) * 100) if usage.total else 0
        stats.append(DiskStat(name, round(free_gb, 1), round(total_gb, 1), percent_free))
    return stats


def _existing_path(path: Path) -> Path:
    current = path
    while not current.exists() and current != current.parent:
        current = current.parent
    return current if current.exists() else Path("/")
