from __future__ import annotations

import json
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from librairy.config import Settings
from librairy.humanize import human_bytes
from librairy.lifecycle import state_counts, vanished_count


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
    "quarantine-proposed": "duplicate, waiting for a decision",
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
        # Filtered out of the inbox card's totals, so the number is short by
        # this much and nothing said why. One line, and only when there are
        # any — a nought here would be a card about nothing.
        "vanished_count": vanished_count(conn),
        # What is happening with your files, which is the question this page
        # answers. Health answers a different one — is LibrAIry itself well —
        # and the two were converging.
        **operations_overview(conn, settings),
    }


def operations_overview(
    conn: sqlite3.Connection, settings: Settings
) -> dict[str, object]:
    """Where the work is, what needs a person, and what just happened.

    Every number here is a SQL aggregate over an indexed column. Nothing in
    this function probes a file, calls a provider, asks a catalog or walks the
    library — a dashboard that costs a filesystem traversal is a dashboard
    people stop opening, and this one polls every five seconds.
    """
    from librairy.web.commit_queue import queue_summary

    queue = queue_summary(conn)
    findings = {
        row["status"]: row["count"]
        for row in conn.execute(
            "SELECT status, COUNT(*) AS count FROM audit_findings GROUP BY status"
        )
    }
    quarantine = conn.execute(
        """
        SELECT
          SUM(CASE WHEN qe.restored_at IS NULL THEN 1 ELSE 0 END) AS held,
          SUM(CASE WHEN qe.restored_at IS NULL
                    AND i.relpath LIKE '_to-delete/%' ESCAPE '\\' THEN 1 ELSE 0 END)
            AS delete_queue,
          COALESCE(SUM(CASE WHEN qe.restored_at IS NULL THEN i.size ELSE 0 END), 0)
            AS bytes
        FROM quarantine_entries qe LEFT JOIN items i ON i.id = qe.item_id
        """
    ).fetchone()
    library = conn.execute(
        "SELECT COUNT(*) AS files, COALESCE(SUM(size), 0) AS bytes FROM items"
        " WHERE root='library' AND missing_since IS NULL"
    ).fetchone()
    inbox_waiting = _count(
        conn, "SELECT COUNT(*) FROM proposals WHERE status='proposed'"
    )

    surfaces = [
        {"label": "Inbox", "count": inbox_waiting, "note": "waiting for review",
         "href": "/review"},
        {"label": "Library Review", "count": findings.get("open", 0),
         "note": f"{findings.get('kept', 0)} dismissed", "href": "/review#audit"},
        {"label": "Commit", "count": queue["decisions"], "note": queue["size"],
         "href": "/commit"},
        {"label": "Quarantine", "count": int(quarantine["held"] or 0),
         "note": human_bytes(int(quarantine["bytes"] or 0)), "href": "/quarantine"},
        {"label": "Library", "count": int(library["files"]),
         "note": human_bytes(int(library["bytes"])), "href": "/browse"},
    ]
    return {
        "surfaces": surfaces,
        "needs_attention": _needs_attention(conn, queue, findings, quarantine),
        "activity": _activity(conn),
        "recent": _recent(conn),
        "delete_queue_count": int(quarantine["delete_queue"] or 0),
    }


def _needs_attention(
    conn: sqlite3.Connection, queue, findings, quarantine
) -> list[dict[str, str]]:
    """Only things a person has to do something about.

    Deliberately not a status board. "Everything is fine" repeated in five
    cards teaches people to stop reading the one card that is not, so a healthy
    system produces an empty list here and the section does not render at all.
    """
    items: list[dict[str, str]] = []
    if queue["decisions"]:
        items.append({
            "text": f"{queue['decisions']} change"
                    f"{'' if queue['decisions'] == 1 else 's'} waiting for Commit",
            "href": "/commit",
        })
    held = int(quarantine["held"] or 0) - int(quarantine["delete_queue"] or 0)
    if held:
        items.append({
            "text": f"{held} quarantined file{'' if held == 1 else 's'} "
                    "with no decision yet",
            "href": "/quarantine",
        })
    if findings.get("open"):
        items.append({
            "text": f"{findings['open']} library finding"
                    f"{'' if findings['open'] == 1 else 's'} to look at",
            "href": "/review#audit",
        })
    from librairy.search_health import recorded_health

    # Read, not checked: the dashboard polls every five seconds and rendering
    # must not write.
    if not recorded_health(conn).ok:
        items.append({
            "text": "Search index needs rebuild — results may be incomplete",
            "href": "/health",
        })
    return items


def _activity(conn: sqlite3.Connection) -> list[dict[str, str]]:
    """What LibrAIry is doing now, from state it already keeps."""
    from librairy.audit_job import progress as audit_progress

    rows: list[dict[str, str]] = []
    audit = audit_progress(conn)
    if audit and audit.get("state") in {"running", "queued"}:
        done, total = audit.get("done", 0), audit.get("total", 0)
        rows.append({
            "what": "Library audit",
            "detail": f"{audit.get('phase') or 'working'} · {done}/{total}",
        })
    running = conn.execute(
        "SELECT COUNT(*) AS n FROM plans WHERE status='executing'"
    ).fetchone()["n"]
    if running:
        rows.append({"what": "Commit", "detail": f"{running} running"})
    return rows


def _recent(conn: sqlite3.Connection) -> list[dict[str, str]]:
    """A few lines from the journal. Not a second History page.

    Grouped by what happened rather than listed file by file: "12 files filed"
    is the sentence, and History is one click away for the other 11 rows.
    """
    rows = conn.execute(
        """
        SELECT action, outcome, COUNT(*) AS count, MAX(ts) AS ts
        FROM history
        GROUP BY action, outcome
        ORDER BY ts DESC
        LIMIT 4
        """
    ).fetchall()
    said = {
        ("move", "ok"): "filed",
        ("move", "skipped_changed"): "not moved — changed since",
        ("move", "skipped_missing"): "not moved — missing",
        ("quarantine", "ok"): "moved to quarantine",
        ("undo", "ok"): "put back",
    }
    return [
        {
            "text": f"{row['count']} file{'' if row['count'] == 1 else 's'} "
                    f"{said.get((row['action'], row['outcome']), row['action'])}",
            "when": row["ts"],
        }
        for row in rows
    ]


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
