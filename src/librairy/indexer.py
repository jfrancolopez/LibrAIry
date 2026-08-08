from __future__ import annotations

import re
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
        for pattern in _patterns_from_relpath(row["relpath"]):
            if (pattern.kind, pattern.key) in seen:
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


#  What the templates call the thing that owns a folder, per category.
PATTERN_KINDS = {"music": "artist", "shows": "show", "movies": "movie"}


def pattern_key(category: str, fields: dict[str, object]) -> tuple[str, str] | None:
    """The (kind, name) this file would be filed under, or None.

    Movies are keyed on the folder name the template builds — `The Matrix
    (1999)` — because that is the folder an existing library actually has.
    """
    kind = PATTERN_KINDS.get(category)
    if kind is None:
        return None
    if kind == "movie":
        title = str(fields.get("title") or "").strip()
        year = fields.get("year") or 0
        name = f"{title} ({year})" if title and year else title
    else:
        name = str(fields.get(kind) or "").strip()
    return (kind, name) if name else None


def apply_library_pattern(
    conn: sqlite3.Connection,
    *,
    kind: str,
    key: str,
    relpath: str,
) -> tuple[str | None, EvidenceEntry | None]:
    """Re-root `relpath` onto the folder your library already uses, if it has one.

    Only the part of the path *above and including* the artist/show/movie is
    replaced; everything below it is kept. This used to return
    `f"{dest_base}/{clean_name}"`, which threw away the album:
    `Music/Rock/Queen/A-Night-at-the-Opera/01-track.mp3` came back as
    `Music/Queen/01-track.mp3`, flattening an album into an artist folder. It
    was never called by anything, so nobody found out.

    Returns `(None, None)` when there is no matching folder, or when the
    rendered path has no segment for the key — in which case the template's
    own answer stands.
    """
    pattern = find_pattern(conn, kind, key)
    if pattern is None:
        return None, None
    target = _normalize(key)
    parts = relpath.split("/")
    for index, part in enumerate(parts[:-1]):
        if _normalize(part) != target:
            continue
        rebased = "/".join([pattern.dest_base, *parts[index + 1 :]])
        if rebased == relpath:
            return None, None
        return (
            rebased,
            EvidenceEntry("library-pattern", "dest_base", pattern.dest_base, 0.9),
        )
    return None, None


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


TOP_LEVEL_KINDS = {"Music": "artist", "Shows": "show", "Movies": "movie"}
#  A season folder is not the name of a show. Without this, a conventional
#  Shows/Breaking Bad/Season 01/ library registers "season01" as a show.
_SEASON = re.compile(r"(?i)^season\s*\d+$")
#  How deep an owner folder can sit under its top level: directly under it in a
#  conventional library, one lower in a genre-first one. Stopping at two is
#  what keeps an album from being registered as an artist.
MAX_PATTERN_DEPTH = 2


def _patterns_from_relpath(relpath: str) -> list[LibraryPattern]:
    """Every folder in this path that could be an artist, a show or a film.

    Both candidate depths are recorded rather than guessed between, because
    there is no way to tell `Music/Queen/Album/` from `Music/Rock/Queen/` by
    shape alone — and the old code guessed, always picking depth 1, so every
    genre-first library registered its *genres* as artist names. The lookup is
    by the real artist name, so recording both and letting the name decide is
    both simpler and right.
    """
    parts = Path(relpath).parts
    kind = TOP_LEVEL_KINDS.get(parts[0]) if parts else None
    if kind is None:
        return []
    patterns = []
    for depth in range(1, MAX_PATTERN_DEPTH + 1):
        # A folder only owns something if there is a file below it, and the
        # last component is the filename.
        if depth > len(parts) - 2:
            break
        name = parts[depth]
        if _SEASON.match(name):
            continue
        patterns.append(
            LibraryPattern(kind, _normalize(name), "/".join(parts[: depth + 1]))
        )
    return patterns


def _normalize(value: str) -> str:
    return "".join(char.lower() for char in value if char.isalnum())
