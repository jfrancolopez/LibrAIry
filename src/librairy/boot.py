from __future__ import annotations

import os
import socket
import sqlite3
import sys
from pathlib import Path

from librairy.config import Settings
from librairy.db import DatabaseVersionError, connect
from librairy.roots import observe


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
    """Open the database, finish any upgrade it needs, and identify the storage.

    Three things, and they are one function because they happen in one attempt:
    `connect` migrates, so a failed upgrade and a failed open arrive here as the
    same call and have to be told apart before either is described.

    They used to be told apart by nothing at all. `DatabaseVersionError` is a
    `RuntimeError`, so the one failure with the highest stakes in the whole
    program — an image rolled back under a database a newer one had already
    upgraded — walked straight past `except sqlite3.Error`, out of
    `validate_boot_or_die`, and printed a Python traceback as the container's
    only account of itself.
    """
    try:
        conn = connect(settings)
    except DatabaseVersionError as exc:
        #  The migration runs inside a transaction and rolls back, so this is
        #  provable rather than reassuring: the database is still at the version
        #  it was, and no file in the Library was moved by an upgrade that did
        #  not happen. Nothing here claims a backup — LibrAIry does not take one.
        return [
            f"LibrAIry could not finish upgrading its database. {exc} "
            "The upgrade was rolled back, so the database is unchanged and no "
            "Library files were reorganized. Fix the cause, or start the "
            "version this database was last used with."
        ]
    except sqlite3.Error as exc:
        return [
            f"SQLite database in {settings.appdata_dir} cannot be opened: {exc}. "
            "No Library files were changed. Make the appdata folder available "
            "and writable, then start LibrAIry again."
        ]
    try:
        conn.execute("SELECT 1").fetchone()
        #  Which filesystem each root is on, written down while LibrAIry is
        #  starting so that a later operation can notice it changed. Reads only;
        #  see `librairy/roots.py`.
        observe(conn, settings)
    except sqlite3.Error as exc:
        return [f"SQLite database in {settings.appdata_dir} cannot be read: {exc}"]
    finally:
        conn.close()
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
