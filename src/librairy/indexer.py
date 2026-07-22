from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from librairy.config import Settings
from librairy.models import EvidenceEntry
from librairy.planner import utc_now
from librairy.scanner import ScanSummary, scan_root


@dataclass(frozen=True)
class LibraryPattern:
    kind: str
    key: str
    dest_base: str


def index_library(conn: sqlite3.Connection, settings: Settings) -> ScanSummary:
    summary = scan_root(conn, "library", settings.library_dir, settings)
    rebuild_pattern_map(conn)
    return summary


def rebuild_pattern_map(conn: sqlite3.Connection) -> None:
    _ensure_pattern_table(conn)
    conn.execute("DELETE FROM library_patterns")
    rows = conn.execute(
        "SELECT relpath FROM items WHERE root='library' AND missing_since IS NULL ORDER BY relpath"
    ).fetchall()
    seen: set[tuple[str, str]] = set()
    for row in rows:
        pattern = _pattern_from_relpath(row["relpath"])
        if pattern is None or (pattern.kind, pattern.key) in seen:
            continue
        seen.add((pattern.kind, pattern.key))
        conn.execute(
            """
            INSERT INTO library_patterns(kind, key, dest_base, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (pattern.kind, pattern.key, pattern.dest_base, utc_now()),
        )


def find_pattern(conn: sqlite3.Connection, kind: str, key: str) -> LibraryPattern | None:
    _ensure_pattern_table(conn)
    row = conn.execute(
        "SELECT kind, key, dest_base FROM library_patterns WHERE kind=? AND key=?",
        (kind, _normalize(key)),
    ).fetchone()
    if row is None:
        return None
    return LibraryPattern(row["kind"], row["key"], row["dest_base"])


def apply_library_pattern(
    conn: sqlite3.Connection,
    *,
    kind: str,
    key: str,
    clean_name: str,
) -> tuple[str | None, EvidenceEntry | None]:
    pattern = find_pattern(conn, kind, key)
    if pattern is None:
        return None, None
    return (
        f"{pattern.dest_base}/{clean_name}",
        EvidenceEntry("library-pattern", "dest_base", pattern.dest_base, 0.9),
    )


def _ensure_pattern_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS library_patterns (
          kind TEXT NOT NULL,
          key TEXT NOT NULL,
          dest_base TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          PRIMARY KEY(kind, key)
        )
        """
    )


def _pattern_from_relpath(relpath: str) -> LibraryPattern | None:
    parts = Path(relpath).parts
    if len(parts) >= 3 and parts[0] == "Music":
        artist = parts[1]
        return LibraryPattern("artist", _normalize(artist), f"Music/{artist}")
    if len(parts) >= 3 and parts[0] == "Shows":
        show = parts[1]
        return LibraryPattern("show", _normalize(show), f"Shows/{show}")
    if len(parts) >= 2 and parts[0] == "Movies":
        movie = parts[1]
        return LibraryPattern("movie", _normalize(movie), f"Movies/{movie}")
    return None


def _normalize(value: str) -> str:
    return "".join(char.lower() for char in value if char.isalnum())
