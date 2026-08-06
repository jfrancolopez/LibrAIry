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
    # st_dev of the filesystem this root sits on. On a laptop all four roots are
    # usually one volume, and reporting it four times reads as four problems.
    device: int = 0


@dataclass(frozen=True)
class Volume:
    """One physical filesystem, and which of our roots live on it."""

    roots: tuple[str, ...]
    free_gb: float
    total_gb: float
    percent_free: int

    @property
    def percent_used(self) -> int:
        return 100 - self.percent_free

    @property
    def label(self) -> str:
        return " + ".join(self.roots)

    @property
    def low(self) -> bool:
        return self.percent_free < 10


# The item states are the database's vocabulary, not a person's. "proposed"
# and "postponed" are both waiting for you and read as unrelated; "discovered"
# sounds like an achievement rather than a queue.
LIFECYCLE_LABELS = {
    "discovered": "found, not yet identified",
    "unstable": "still being written",
    "pending": "needs more information",
    "proposed": "waiting for your review",
    "postponed": "put off for later",
    "approved": "approved, ready to commit",
    "quarantine-proposed": "duplicate, awaiting your call",
    "quarantined": "moved to quarantine",
    "committed": "filed in your library",
}
# Reading order is the order a file travels, so the column reads as a pipeline
# rather than as whatever sequence GROUP BY happened to return.
LIFECYCLE_ORDER = tuple(LIFECYCLE_LABELS)


def dashboard_data(conn: sqlite3.Connection, settings: Settings) -> dict[str, object]:
    worker_state = _worker_state(conn)
    counts = state_counts(conn)
    # The card is about work in progress, which means the inbox. Library items
    # rest in 'discovered' by design and are not a backlog.
    inbox_counts = state_counts(conn, "inbox")
    proposals = conn.execute(
        "SELECT COUNT(*) FROM proposals WHERE status='proposed'"
    ).fetchone()[0]
    backup = {
        row["state"]: row["count"]
        for row in conn.execute("SELECT state, COUNT(*) AS count FROM backup_queue GROUP BY state")
    }
    disks = _disk_stats(settings)
    return {
        "worker_state": worker_state,
        "current_phase": worker_state.get("current_phase", "unknown"),
        "counts": counts,
        "lifecycle": lifecycle_rows(inbox_counts),
        "library_count": _count(conn, "SELECT COUNT(*) FROM items WHERE root='library'"),
        "proposal_count": proposals,
        "approved_count": _count(conn, "SELECT COUNT(*) FROM proposals WHERE status='approved'"),
        "recent_history": _recent_history(conn),
        "providers": _providers(conn),
        "disks": disks,
        "volumes": volumes(disks),
        "host_inbox_dir": settings.host_inbox_dir,
        "backup_counts": backup,
    }


def lifecycle_rows(counts: dict[str, int]) -> list[tuple[str, int]]:
    """Plain-language lifecycle counts, in the order a file travels.

    States with nothing in them are dropped: a column of zeroes is noise you
    have to read past to find the one number that moved.
    """
    rows = [
        (LIFECYCLE_LABELS[state], counts[state])
        for state in LIFECYCLE_ORDER
        if counts.get(state)
    ]
    # Anything the database grew that this map has not caught up with still
    # deserves to be shown, under its own name, rather than vanishing.
    rows.extend(
        (state, count) for state, count in sorted(counts.items())
        if count and state not in LIFECYCLE_LABELS
    )
    return rows


def volumes(disks: list[DiskStat]) -> list[Volume]:
    """One row per filesystem, not per configured root.

    On a single-disk box inbox, library, quarantine and appdata are all the
    same volume, and listing "8GB free" four times reads as four separate
    warnings about four separate disks.
    """
    grouped: dict[int, list[DiskStat]] = {}
    for disk in disks:
        grouped.setdefault(disk.device, []).append(disk)
    merged = [
        Volume(
            roots=tuple(disk.root for disk in group),
            free_gb=group[0].free_gb,
            total_gb=group[0].total_gb,
            percent_free=group[0].percent_free,
        )
        for group in grouped.values()
    ]
    # Fullest first: the one that will stop a commit is the one to show first.
    return sorted(merged, key=lambda volume: volume.percent_free)


def _count(conn: sqlite3.Connection, sql: str) -> int:
    row = conn.execute(sql).fetchone()
    return int(row[0]) if row else 0


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
        resolved = _existing_path(path)
        usage = shutil.disk_usage(resolved)
        free_gb = usage.free / 1024**3
        total_gb = usage.total / 1024**3
        percent_free = round((usage.free / usage.total) * 100) if usage.total else 0
        try:
            device = resolved.stat().st_dev
        except OSError:
            device = 0
        stats.append(
            DiskStat(name, round(free_gb, 1), round(total_gb, 1), percent_free, device)
        )
    return stats


def _existing_path(path: Path) -> Path:
    current = path
    while not current.exists() and current != current.parent:
        current = current.parent
    return current if current.exists() else Path("/")
