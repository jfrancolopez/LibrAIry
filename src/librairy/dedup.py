from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from librairy.config import Settings
from librairy.fingerprint import blake2b_file
from librairy.models import Item
from librairy.tools.rmlint import duplicate_path_pairs, duplicates


class DedupConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class DedupOptions:
    use_fingerprints: bool = True
    use_rmlint: bool = True


@dataclass(frozen=True)
class DuplicateCandidate:
    duplicate: Item
    keeper: Item
    status: str
    reason: str


RmlintCheck = Callable[[list[tuple[Item, Item]], Settings], set[tuple[int, int]]]


def set_dedup_option(conn: sqlite3.Connection, key: str, value: bool) -> None:
    if key not in {"use_fingerprints", "use_rmlint"}:
        raise DedupConfigError(f"unknown dedup option: {key}")
    conn.execute(
        "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
        (f"dedup.{key}", json.dumps(value)),
    )


def dedup_options(conn: sqlite3.Connection) -> DedupOptions:
    options = DedupOptions(
        use_fingerprints=_setting_bool(conn, "dedup.use_fingerprints", True),
        use_rmlint=_setting_bool(conn, "dedup.use_rmlint", True),
    )
    if not options.use_fingerprints and not options.use_rmlint:
        raise DedupConfigError("at least one exact duplicate method must be enabled")
    return options


def detect_exact_duplicates(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    rmlint_check: RmlintCheck | None = None,
) -> list[DuplicateCandidate]:
    options = dedup_options(conn)
    if not options.use_fingerprints:
        return []
    pairs = _fingerprint_pairs(conn)
    if not pairs:
        return []
    agreed = None
    if options.use_rmlint:
        agreed = (rmlint_check or _rmlint_check)(pairs, settings)
    candidates: list[DuplicateCandidate] = []
    for keeper, duplicate in pairs:
        pair_key = _pair_key(keeper, duplicate)
        confirmed = agreed is None or pair_key in agreed
        candidates.append(
            DuplicateCandidate(
                duplicate=duplicate,
                keeper=keeper,
                status="confirmed" if confirmed else "review",
                reason="exact_duplicate" if confirmed else "fingerprint_rmlint_disagreement",
            )
        )
    return candidates


def hash_size_colliding_library_files(
    conn: sqlite3.Connection,
    settings: Settings,
    hash_file: Callable[[Path], str] = blake2b_file,
) -> int:
    sizes = [
        row["size"]
        for row in conn.execute(
            "SELECT DISTINCT size FROM items WHERE root='inbox' AND missing_since IS NULL"
        )
    ]
    if not sizes:
        return 0
    placeholders = ",".join("?" for _ in sizes)
    rows = conn.execute(
        f"""
        SELECT id, relpath FROM items
        WHERE root='library' AND missing_since IS NULL AND fingerprint IS NULL
          AND size IN ({placeholders})
        """,
        sizes,
    ).fetchall()
    hashed = 0
    for row in rows:
        fingerprint = hash_file(settings.library_dir / row["relpath"])
        conn.execute("UPDATE items SET fingerprint=? WHERE id=?", (fingerprint, row["id"]))
        hashed += 1
    return hashed


def _fingerprint_pairs(conn: sqlite3.Connection) -> list[tuple[Item, Item]]:
    rows = [_item_from_row(row) for row in conn.execute(_DUP_QUERY)]
    groups: dict[str, list[Item]] = {}
    for row in rows:
        if row.fingerprint:
            groups.setdefault(row.fingerprint, []).append(row)
    pairs: list[tuple[Item, Item]] = []
    for group in groups.values():
        if len(group) < 2:
            continue
        keeper = _keeper(group)
        pairs.extend(
            (keeper, item) for item in group if item.id != keeper.id and item.root == "inbox"
        )
    return pairs


def _keeper(group: list[Item]) -> Item:
    library_items = [item for item in group if item.root == "library"]
    if library_items:
        return sorted(library_items, key=lambda item: item.relpath)[0]
    return sorted(group, key=lambda item: (item.first_seen_at, item.relpath))[0]


def _rmlint_check(pairs: list[tuple[Item, Item]], settings: Settings) -> set[tuple[int, int]]:
    paths = sorted({_path_for(settings, item) for pair in pairs for item in pair})
    result = duplicates(paths, settings)
    if not result.ok or not isinstance(result.data, list):
        return set()
    path_pairs = duplicate_path_pairs(result.data)
    agreed: set[tuple[int, int]] = set()
    for keeper, duplicate in pairs:
        if (
            frozenset(
                (_path_for(settings, keeper).as_posix(), _path_for(settings, duplicate).as_posix())
            )
            in path_pairs
        ):
            agreed.add(_pair_key(keeper, duplicate))
    return agreed


def _path_for(settings: Settings, item: Item) -> Path:
    root = settings.library_dir if item.root == "library" else settings.inbox_dir
    return root / item.relpath


def _pair_key(left: Item, right: Item) -> tuple[int, int]:
    return tuple(sorted((left.id, right.id)))


def _setting_bool(conn: sqlite3.Connection, key: str, default: bool) -> bool:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row is None:
        return default
    return bool(json.loads(row["value"]))


def _item_from_row(row: sqlite3.Row) -> Item:
    return Item(
        id=row["id"],
        root=row["root"],
        relpath=row["relpath"],
        size=row["size"],
        mtime_ns=row["mtime_ns"],
        fingerprint=row["fingerprint"],
        state=row["state"],
        first_seen_at=row["first_seen_at"],
        last_seen_at=row["last_seen_at"],
        missing_since=row["missing_since"],
    )


_DUP_QUERY = """
SELECT * FROM items
WHERE root IN ('inbox', 'library')
  AND missing_since IS NULL
  AND fingerprint IS NOT NULL
ORDER BY fingerprint, root, relpath
"""
