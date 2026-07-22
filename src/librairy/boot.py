from __future__ import annotations

import os
import socket
import sqlite3
import sys
from pathlib import Path

from librairy.config import Settings
from librairy.db import connect


class BootValidationError(RuntimeError):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


def validate_boot(settings: Settings, *, check_port: bool = True) -> list[str]:
    errors: list[str] = []
    roots = {
        "inbox": settings.inbox_dir,
        "library": settings.library_dir,
        "quarantine": settings.quarantine_dir,
        "appdata": settings.appdata_dir,
    }
    for name, path in roots.items():
        errors.extend(_path_errors(name, path))
    if not errors:
        protected_roots = {key: roots[key] for key in ("inbox", "library", "quarantine")}
        errors.extend(_nesting_errors(protected_roots))
    if not errors:
        errors.extend(_database_errors(settings))
    if check_port:
        errors.extend(_port_errors(settings.dashboard_port))
    return errors


def validate_boot_or_die(settings: Settings, *, check_port: bool = True) -> None:
    errors = validate_boot(settings, check_port=check_port)
    if not errors:
        return
    print("LibrAIry startup validation failed:", file=sys.stderr)
    for index, error in enumerate(errors, start=1):
        print(f"{index}. {error}", file=sys.stderr)
    raise SystemExit(2)


def _path_errors(name: str, path: Path) -> list[str]:
    if not path.exists():
        return [f"/{name} path {path} does not exist; create the host directory or fix the mount"]
    if not path.is_dir():
        return [f"/{name} path {path} is not a directory; point it at a directory"]
    if not os.access(path, os.W_OK):
        return [f"/{name} path {path} is not writable by UID {os.geteuid()}; check PUID/PGID"]
    return []


def _nesting_errors(roots: dict[str, Path]) -> list[str]:
    resolved = {name: path.resolve() for name, path in roots.items()}
    errors: list[str] = []
    for left_name, left_path in resolved.items():
        for right_name, right_path in resolved.items():
            if left_name == right_name:
                continue
            if left_path == right_path:
                errors.append(
                    f"{left_name} and {right_name} point to the same directory: {left_path}"
                )
            elif _is_relative_to(left_path, right_path):
                errors.append(
                    f"{left_name} path {left_path} is inside {right_name} path {right_path}; "
                    "use separate top-level folders"
                )
    return sorted(set(errors))


def _database_errors(settings: Settings) -> list[str]:
    try:
        conn = connect(settings)
        conn.execute("SELECT 1").fetchone()
        conn.close()
    except sqlite3.Error as exc:
        return [f"SQLite database in {settings.appdata_dir} cannot be opened: {exc}"]
    return []


def _port_errors(port: int) -> list[str]:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("0.0.0.0", port))
        except OSError:
            return [f"dashboard port {port} is already in use; set DASHBOARD_PORT to a free port"]
    return []


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return path != parent
