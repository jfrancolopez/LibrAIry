from __future__ import annotations

import fnmatch
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from librairy.config import Settings
from librairy.fingerprint import blake2b_file
from librairy.lifecycle import should_reset_for_fingerprint_change
from librairy.proposals import supersede_proposal
from librairy.search import sync_search_item

VALID_ROOTS = {"inbox", "library", "quarantine"}


@dataclass(frozen=True)
class ScanSummary:
    root: str
    discovered: int = 0
    hashed: int = 0
    skipped_unchanged: int = 0
    unstable: int = 0
    missing: int = 0
    symlinks_skipped: int = 0


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def scan_root(
    conn: sqlite3.Connection,
    root: str,
    root_path: Path,
    settings: Settings | None = None,
) -> ScanSummary:
    if root not in VALID_ROOTS:
        raise ValueError(f"unknown root: {root}")
    if settings is None:
        settings = Settings()

    root_path = root_path.resolve()
    now = utc_now()
    now_ns = datetime.now(UTC).timestamp() * 1_000_000_000
    seen: set[str] = set()
    discovered = hashed = skipped_unchanged = unstable = symlinks_skipped = 0

    for dirpath, dirnames, filenames in os.walk(root_path, followlinks=False):
        current_dir = Path(dirpath)
        dirnames[:] = [
            name
            for name in dirnames
            if not _is_hidden(name)
            and not _ignored(_posix_rel(current_dir / name, root_path), settings.ignore_patterns)
            and not (current_dir / name).is_symlink()
        ]
        for name in filenames:
            path = current_dir / name
            relpath = _posix_rel(path, root_path)
            if _is_hidden(name) or _ignored(relpath, settings.ignore_patterns):
                continue
            if path.is_symlink():
                symlinks_skipped += 1
                continue

            stat = path.stat()
            seen.add(relpath)
            discovered += 1
            existing = conn.execute(
                """
                SELECT id, size, mtime_ns, fingerprint, state
                FROM items WHERE root=? AND relpath=?
                """,
                (root, relpath),
            ).fetchone()
            stability_ns = settings.file_stability_seconds * 1_000_000_000
            is_unstable = now_ns - stat.st_mtime_ns < stability_ns
            if is_unstable:
                unstable += 1
                fingerprint = existing["fingerprint"] if existing else None
                state = "unstable"
            elif (
                existing
                and existing["size"] == stat.st_size
                and existing["mtime_ns"] == stat.st_mtime_ns
            ):
                skipped_unchanged += 1
                fingerprint = existing["fingerprint"]
                state = existing["state"]
            else:
                hashed += 1
                fingerprint = blake2b_file(path)
                state = "discovered"
                changed_tracked_item = (
                    existing
                    and existing["fingerprint"]
                    and existing["fingerprint"] != fingerprint
                    and should_reset_for_fingerprint_change(existing["state"])
                )

            conn.execute(
                """
                INSERT INTO items(
                  root, relpath, size, mtime_ns, fingerprint, state,
                  first_seen_at, last_seen_at, missing_since
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(root, relpath) DO UPDATE SET
                  size=excluded.size,
                  mtime_ns=excluded.mtime_ns,
                  fingerprint=excluded.fingerprint,
                  state=excluded.state,
                  last_seen_at=excluded.last_seen_at,
                  missing_since=NULL
                """,
                (root, relpath, stat.st_size, stat.st_mtime_ns, fingerprint, state, now, now),
            )
            item_id = conn.execute(
                "SELECT id FROM items WHERE root=? AND relpath=?", (root, relpath)
            ).fetchone()[0]
            sync_search_item(conn, item_id)
            unchanged = (
                existing
                and existing["size"] == stat.st_size
                and existing["mtime_ns"] == stat.st_mtime_ns
            )
            if not is_unstable and not unchanged and existing and changed_tracked_item:
                supersede_proposal(conn, existing["id"])

    missing = _mark_missing(conn, root, seen, now)
    return ScanSummary(
        root,
        discovered,
        hashed,
        skipped_unchanged,
        unstable,
        missing,
        symlinks_skipped,
    )


def ready_items(conn: sqlite3.Connection, root: str = "inbox") -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT * FROM items
            WHERE root=?
              AND missing_since IS NULL
              AND state = 'discovered'
              AND fingerprint IS NOT NULL
            ORDER BY relpath
            """,
            (root,),
        )
    )


def _mark_missing(conn: sqlite3.Connection, root: str, seen: set[str], now: str) -> int:
    rows = conn.execute(
        "SELECT relpath FROM items WHERE root=? AND missing_since IS NULL",
        (root,),
    ).fetchall()
    missing = [row["relpath"] for row in rows if row["relpath"] not in seen]
    for relpath in missing:
        conn.execute(
            "UPDATE items SET missing_since=?, last_seen_at=? WHERE root=? AND relpath=?",
            (now, now, root, relpath),
        )
    return len(missing)


def _posix_rel(path: Path, root_path: Path) -> str:
    return path.relative_to(root_path).as_posix()


def _is_hidden(name: str) -> bool:
    return name.startswith(".")


def _ignored(relpath: str, patterns: list[str]) -> bool:
    name = Path(relpath).name
    return any(
        fnmatch.fnmatch(relpath, pattern) or fnmatch.fnmatch(name, pattern) for pattern in patterns
    )
