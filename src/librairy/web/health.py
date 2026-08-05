from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from librairy.ai.orchestrator import provider_for_config
from librairy.ai.registry import provider_chain
from librairy.ai.status import upsert_provider_status
from librairy.backup import backup_status
from librairy.config import Settings
from librairy.db import database_path
from librairy.web.dashboard import _disk_stats, _worker_state

PROBE_TTL_SECONDS = 60
TOOL_COMMANDS = {
    "ffprobe": ["ffprobe", "-version"],
    "exiftool": ["exiftool", "-ver"],
    "fpcalc": ["fpcalc", "-version"],
    "rmlint": ["rmlint", "--version"],
    "czkawka": ["czkawka_cli", "--version"],
}
_TOOL_CACHE: dict[tuple[str, str], tuple[float, HealthRow]] = {}


@dataclass(frozen=True)
class HealthRow:
    name: str
    status: str
    detail: str
    hint: str = ""


def health_data(conn: sqlite3.Connection, settings: Settings) -> dict[str, object]:
    providers = list(conn.execute("SELECT * FROM provider_status ORDER BY name"))
    tools = tool_statuses(settings)
    db = db_status(settings)
    disk_stats = _disk_stats(settings)
    disks = disk_statuses(settings)
    worker = worker_status(conn)
    backup = backup_health(settings)
    rows = [*tools, db, *disks, worker, backup]
    status = "OK" if all(row.status == "OK" for row in rows) else "WARN"
    return {
        "summary_status": status,
        "tools": tools,
        "providers": providers,
        "db_status": db,
        "disk_statuses": disks,
        "worker_status": worker,
        "backup_status": backup,
        "recommendations": recommendations(
            tools=tools,
            providers=providers,
            disks=disk_stats,
            worker=worker,
            backup=backup,
        ),
        **health_metrics(conn, settings),
    }


@dataclass(frozen=True)
class Recommendation:
    severity: str  # "warn" | "fail"
    text: str
    action: str


@dataclass(frozen=True)
class Bar:
    """One bar in a chart. `pct` is width, `tone` picks the colour."""

    label: str
    value: int
    pct: int
    tone: str = "accent"  # accent | ok | warn | fail
    caption: str = ""


GROWTH_DAYS = 14
# Anything below the confidence threshold needs a human; the bands split the
# queue into "just approve it", "glance at it", and "this one needs thought".
CONFIDENCE_BANDS = (
    ("high", 0.85, 1.01, "ok"),
    ("medium", 0.70, 0.85, "warn"),
    ("low", 0.0, 0.70, "fail"),
)


def health_metrics(conn: sqlite3.Connection, settings: Settings) -> dict[str, object]:
    """Numbers worth watching, shaped for bar charts.

    Rendered server-side as sized divs rather than a charting library: the
    portal ships no JavaScript build step, and its CSP blocks anything inline.
    """
    return {
        "growth": _library_growth(conn),
        "pipeline": _pipeline(conn),
        "confidence": _confidence(conn),
        "disk_meters": _disk_meters(settings),
        "totals": _totals(conn),
    }


def _library_growth(conn: sqlite3.Connection) -> list[Bar]:
    """Files committed per day. The one number that shows LibrAIry working."""
    today = datetime.now(UTC).date()
    counts = {
        str(row["day"]): row["count"]
        for row in conn.execute(
            """
            SELECT substr(ts, 1, 10) AS day, COUNT(*) AS count
            FROM history
            WHERE action='move' AND outcome='ok'
            GROUP BY day
            """
        )
    }
    days = [today - timedelta(days=offset) for offset in range(GROWTH_DAYS - 1, -1, -1)]
    values = [counts.get(day.isoformat(), 0) for day in days]
    peak = max(values) or 1
    return [
        Bar(
            label=day.strftime("%d %b"),
            value=value,
            pct=round(value / peak * 100),
            tone="accent",
            caption=f"{value} file(s) on {day.isoformat()}",
        )
        for day, value in zip(days, values, strict=True)
    ]


def _pipeline(conn: sqlite3.Connection) -> list[Bar]:
    """Where everything currently sits, inbox through committed."""
    tones = {
        "discovered": "accent",
        "proposed": "ok",
        "pending": "warn",
        "postponed": "warn",
        "approved": "ok",
        "committed": "ok",
        "quarantined": "fail",
        "unstable": "warn",
    }
    rows = list(
        conn.execute(
            "SELECT state, COUNT(*) AS count FROM items GROUP BY state ORDER BY count DESC"
        )
    )
    total = sum(row["count"] for row in rows) or 1
    return [
        Bar(
            label=row["state"],
            value=row["count"],
            pct=round(row["count"] / total * 100),
            tone=tones.get(row["state"], "accent"),
            caption=f"{row['count']} of {total} items",
        )
        for row in rows
    ]


def _confidence(conn: sqlite3.Connection) -> list[Bar]:
    """How sure LibrAIry is about what is still waiting for a decision."""
    rows = list(
        conn.execute(
            "SELECT confidence FROM proposals WHERE status IN ('proposed', 'approved')"
        )
    )
    total = len(rows) or 1
    bars = []
    for label, low, high, tone in CONFIDENCE_BANDS:
        count = sum(1 for row in rows if low <= (row["confidence"] or 0) < high)
        bars.append(
            Bar(
                label=label,
                value=count,
                pct=round(count / total * 100),
                tone=tone,
                caption=f"{count} proposal(s) at {int(low * 100)}–{int(min(high, 1) * 100)}%",
            )
        )
    return bars


def _disk_meters(settings: Settings) -> list[Bar]:
    """Space *used*, so a long bar means trouble the way people expect."""
    seen: set[object] = set()
    meters = []
    for stat in _disk_stats(settings):
        # One bar per volume: four roots on one laptop disk is one bar.
        key = stat.device or stat.root
        if key in seen:
            continue
        seen.add(key)
        used = 100 - stat.percent_free
        tone = "ok" if stat.percent_free >= 20 else "warn" if stat.percent_free >= 10 else "fail"
        meters.append(
            Bar(
                label=stat.root,
                value=used,
                pct=used,
                tone=tone,
                caption=f"{stat.free_gb}GB free of {stat.total_gb}GB",
            )
        )
    return meters


def _totals(conn: sqlite3.Connection) -> dict[str, int]:
    def count(sql: str) -> int:
        return int(conn.execute(sql).fetchone()[0])

    return {
        "library_files": count("SELECT COUNT(*) FROM items WHERE root='library'"),
        "inbox_files": count("SELECT COUNT(*) FROM items WHERE root='inbox'"),
        "moves_all_time": count("SELECT COUNT(*) FROM history WHERE action='move'"),
        "quarantined": count("SELECT COUNT(*) FROM quarantine_entries"),
    }


def recommendations(
    *,
    tools: list[HealthRow],
    providers: list,
    disks: list,  # DiskStat — carries percent_free and the volume it lives on
    worker: HealthRow,
    backup: HealthRow,
) -> list[Recommendation]:
    """Plain rules over the health signals — what's wrong and what to do."""
    recs: list[Recommendation] = []

    for tool in tools:
        if tool.status != "OK":
            recs.append(
                Recommendation(
                    "warn" if tool.status == "WARN" else "fail",
                    f"{tool.name} is unavailable — {tool.detail}.",
                    tool.hint or f"Install {tool.name} or rebuild the container image.",
                )
            )

    reachable = any(row["last_ok_at"] and not row["last_error"] for row in providers)
    if providers and not reachable:
        recs.append(
            Recommendation(
                "warn",
                "No AI provider is reachable — organizing runs on heuristics only.",
                "Check OLLAMA_HOST and your provider settings, then Test from Health.",
            )
        )

    recs.extend(_disk_recommendations(disks))

    if worker.status not in {"OK", ""}:
        recs.append(
            Recommendation("warn", f"Worker: {worker.detail}.", worker.hint or "Check the logs."),
        )

    if backup.status not in {"OK", ""}:
        recs.append(
            Recommendation(
                "warn",
                f"Backup: {backup.detail}.",
                backup.hint or "Check the rclone remote and config.",
            )
        )

    return recs


def _disk_recommendations(disks: list) -> list[Recommendation]:
    """One warning per volume, not per root.

    inbox/library/quarantine/appdata usually share a single filesystem — on a
    laptop they always do — and emitting the same "low on space" four times
    reads as four separate problems when there is one disk to clear.
    """
    by_device: dict[object, list] = {}
    for disk in disks:
        percent = getattr(disk, "percent_free", None)
        if percent is None or percent >= 10:
            continue
        by_device.setdefault(getattr(disk, "device", 0) or disk.root, []).append(disk)

    recs: list[Recommendation] = []
    for group in by_device.values():
        worst = min(group, key=lambda d: d.percent_free)
        roots = ", ".join(sorted(d.root for d in group))
        subject = f"{roots} share a volume that is" if len(group) > 1 else f"{worst.root} is"
        recs.append(
            Recommendation(
                "warn" if worst.percent_free >= 5 else "fail",
                f"{subject} low on space ({worst.percent_free}% free).",
                "Free up space on that volume before committing more moves.",
            )
        )
    return recs


def tool_statuses(settings: Settings) -> list[HealthRow]:  # noqa: ARG001
    return [_tool_status(name, command) for name, command in TOOL_COMMANDS.items()]


def test_provider(conn: sqlite3.Connection, settings: Settings, name: str) -> sqlite3.Row | None:
    configs = provider_chain(conn, settings)
    config = next((provider for provider in configs if provider.name == name), None)
    if config is None:
        return conn.execute("SELECT * FROM provider_status WHERE name=?", (name,)).fetchone()
    provider = provider_for_config(config, settings)
    health = provider.health(settings.ai_timeout)
    upsert_provider_status(conn, config, health)
    return conn.execute("SELECT * FROM provider_status WHERE name=?", (name,)).fetchone()


def db_status(settings: Settings) -> HealthRow:
    db_path = database_path(settings)
    if not db_path.exists():
        return HealthRow("SQLite", "WARN", "database has not been created", "start LibrAIry once")
    try:
        with sqlite3.connect(db_path) as conn:
            result = conn.execute("PRAGMA quick_check").fetchone()[0]
        size_mb = db_path.stat().st_size / 1024**2
        wal_mb = _file_mb(db_path.with_name(f"{db_path.name}-wal"))
    except sqlite3.Error as exc:
        return HealthRow("SQLite", "FAIL", str(exc), "restore appdata or rebuild the index")
    status = "OK" if result == "ok" else "FAIL"
    return HealthRow(
        "SQLite",
        status,
        f"quick_check={result}; db={size_mb:.1f}MB; wal={wal_mb:.1f}MB",
    )


def disk_statuses(settings: Settings) -> list[HealthRow]:
    rows: list[HealthRow] = []
    for stat in _disk_stats(settings):
        status = "OK" if stat.percent_free >= 10 else "WARN"
        rows.append(
            HealthRow(
                stat.root,
                status,
                f"{stat.free_gb}GB free of {stat.total_gb}GB ({stat.percent_free}%)",
                "free disk space" if status == "WARN" else "",
            )
        )
    return rows


def worker_status(conn: sqlite3.Connection) -> HealthRow:
    state = _worker_state(conn)
    phase = str(state.get("current_phase", "unknown"))
    last_cycle = state.get("last_cycle_at")
    if not isinstance(last_cycle, str):
        return HealthRow("Worker", "WARN", f"phase={phase}; no heartbeat", "wait for worker loop")
    try:
        heartbeat = datetime.fromisoformat(last_cycle)
    except ValueError:
        return HealthRow("Worker", "WARN", f"phase={phase}; invalid heartbeat", "restart worker")
    age = max(0, round((datetime.now(UTC) - heartbeat).total_seconds()))
    status = "OK" if age <= 300 else "WARN"
    return HealthRow(
        "Worker",
        status,
        f"phase={phase}; heartbeat {age}s ago",
        "worker may be stopped" if status == "WARN" else "",
    )


def backup_health(settings: Settings) -> HealthRow:
    status = backup_status(settings)
    if not settings.backup_enabled:
        return HealthRow("Backup", "OK", "disabled")
    return HealthRow("Backup", "OK" if status.available else "WARN", status.detail)


def _tool_status(name: str, command: list[str]) -> HealthRow:
    cache_key = (name, os.environ.get("PATH", ""))
    cached = _TOOL_CACHE.get(cache_key)
    now = time.monotonic()
    if cached and now - cached[0] < PROBE_TTL_SECONDS:
        return cached[1]
    row = _probe_tool(name, command)
    _TOOL_CACHE[cache_key] = (now, row)
    return row


def _probe_tool(name: str, command: list[str]) -> HealthRow:
    binary = command[0]
    if shutil.which(binary) is None:
        return HealthRow(name, "WARN", "missing", f"install {binary} in the container image")
    try:
        result = subprocess.run(command, text=True, capture_output=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return HealthRow(name, "WARN", exc.__class__.__name__, f"check {binary} installation")
    output = (result.stdout or result.stderr).strip().splitlines()
    detail = output[0][:120] if output else f"exit {result.returncode}"
    status = "OK" if result.returncode == 0 else "WARN"
    hint = f"check {binary} installation" if status == "WARN" else ""
    return HealthRow(name, status, detail, hint)


def _file_mb(path: Path) -> float:
    return path.stat().st_size / 1024**2 if path.exists() else 0.0
