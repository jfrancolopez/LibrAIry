from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from librairy.ai.orchestrator import provider_for_config
from librairy.ai.registry import provider_chain
from librairy.ai.status import upsert_provider_status
from librairy.config import Settings
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
    disks = disk_statuses(settings)
    worker = worker_status(conn)
    status = "OK" if all(row.status == "OK" for row in [*tools, db, *disks, worker]) else "WARN"
    return {
        "summary_status": status,
        "tools": tools,
        "providers": providers,
        "db_status": db,
        "disk_statuses": disks,
        "worker_status": worker,
    }


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
    db_path = settings.appdata_dir / "librairy.sqlite3"
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
