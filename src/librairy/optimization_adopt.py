"""The record that has to exist when an optimized file becomes the live one.

Adoption moves two files and leaves the database describing three things: the
original, now preserved; the optimized file, now active; and the job that links
them. This module owns the second of those, because the executor cannot: op 2's
`item_id` is NULL by construction — the generated file has no `items` row while
it sits in staging, and `items.root` is CHECK-constrained so it could not have
one there anyway.

## Why there is almost nothing to carry forward

`docs/plan/adoption-architecture.md` records the audit. The short version is
that `items` is a **file record**, not an identity record: root, relpath, size,
mtime, fingerprint, state. Every one of those is a property of the actual bytes
at the actual path, so every one is read from the new file.

Logical identity lives elsewhere and mostly does not want copying:

- **Category** is derived from the path by the index, not stored on the item.
  `Music/Live/concert.flac` is music for the same reason `concert.wav` was, so
  it needs no carrying at all.
- **`vision_results` and `content_extractions` are keyed by fingerprint.**
  Attaching a caption computed from the WAV's bytes to the FLAC's bytes would
  be asserting that something looked at bytes nothing has looked at. The
  picture may be identical; the claim would still be false.
- **`audit_findings`, `duplicate_reports`, `similar_media_flags`** are all
  statements about specific bytes or specific pairs of them.
- **`backup_queue`** is made by the executor for whatever lands in the library,
  so the result gets its own row through the normal path.

That leaves nothing, which is the safest possible answer and not a shortcut: a
representation change should inherit the *file's* place in the library and none
of the conclusions drawn about the old bytes.

## Undo

The row is marked missing rather than deleted or moved. `items.root` cannot
hold the staging namespace, and deleting is not available either — fourteen
tables hold foreign keys into `items`, seven of those columns NOT NULL, and a
library file has acquired a `backup_queue` row by the time Undo runs. Measured:

    DELETE FROM items WHERE id=<result>  ->  FOREIGN KEY constraint failed

`missing_since` already means "recorded, not at that path right now" everywhere
else here — an unmounted share produces exactly this — and Search already
filters on it. Re-adoption clears it and reuses the same row, so lineage
survives and no foreign key churns.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from librairy.config import Settings
from librairy.fingerprint import blake2b_file
from librairy.planner import utc_now
from librairy.search import sync_search_item

# The optimized file arrives already decided: it is in the library because a
# person approved a plan that put it there. `discovered` is what an ordinary
# filed library file reads, and it keeps the result out of every queue that
# asks for an opinion.
RESULT_STATE = "discovered"

# What must never be copied from the original's record onto the result's: every
# table whose rows are statements about specific bytes. Asserted by
# `tests/test_optimization_adopt.py` reading this module's own SQL, so adding a
# carry-forward later is a deliberate act rather than an accident.
#
# `scripts/inventory_item_tables.py` derives the full list from the schema, the
# lazily created tables and the FTS shadows, and fails if anything here is
# unclassified.
NEVER_CARRIED = (
    "vision_results",
    "content_extractions",
    # The ffprobe cache: codec, bitrate, duration, channels, sample format.
    # Created lazily by `tools.common.ensure_metadata_cache`, so it is absent
    # from a fresh schema and easy to miss. Every field in it is a property of
    # the encoding that just changed.
    "item_metadata",
    "audit_findings",
    "duplicate_reports",
    "similar_media_flags",
    # An offer to optimize specific bytes. The result is the output of one, not
    # a candidate for another.
    "optimization_opportunities",
    "backup_queue",
    "proposals",
)

# What *is* carried, and it is empty on purpose.
#
# The tempting candidate is logical identity — a trusted TMDB or MusicBrainz
# answer should survive MKV -> MP4, and throwing it away because the container
# changed would be a real loss. Measured rather than assumed:
#
#     catalog_identity(scope_kind, scope_key, provider)  UNIQUE
#     scope_key = the library-relative FOLDER
#
# It has no `item_id` and no foreign key to `items` at all. Identity belongs to
# the album or movie folder, not to each of its forty tracks — and adoption
# keeps the file in its folder by construction (`target_relpath` changes only
# the suffix). So the identity is not carried and not lost: it was never
# attached to the item, and it still describes the same folder afterwards.
# `library_patterns` is keyed by artist or show name and unaffected for the
# same reason.
#
# `item_metadata` is the one that looks like it might hold identity and does
# not: despite the name it is a single tool cache, read only on a fingerprint
# match, holding ffprobe output.
CARRIED: tuple[str, ...] = ()


class AdoptionError(RuntimeError):
    """Something about the adoption's bookkeeping does not add up."""


def record_result_item(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    relpath: str,
    job_id: int,
) -> int:
    """The one place a result item is created or brought back.

    Everything technical is read from the file that is actually there — not
    from the job's expectations about it, and not from the original's record.
    If those two disagree, the file on disk is the one telling the truth.

    Re-adoption finds the row from last time and revives it rather than
    inserting a second one, which is what keeps the job's link to its result
    stable across an Undo and a change of mind.
    """
    path = settings.library_dir / relpath
    if not path.is_file():
        raise AdoptionError("the optimized file is not where the plan put it")
    stat = path.stat()
    fingerprint = blake2b_file(path)
    now = utc_now()

    existing = conn.execute(
        "SELECT id FROM items WHERE root='library' AND relpath=?", (relpath,)
    ).fetchone()
    if existing is not None:
        conn.execute(
            """
            UPDATE items
            SET size=?, mtime_ns=?, fingerprint=?, state=?, last_seen_at=?,
                missing_since=NULL
            WHERE id=?
            """,
            (stat.st_size, stat.st_mtime_ns, fingerprint, RESULT_STATE, now,
             existing["id"]),
        )
        item_id = int(existing["id"])
    else:
        cursor = conn.execute(
            """
            INSERT INTO items(
              root, relpath, size, mtime_ns, fingerprint, state,
              first_seen_at, last_seen_at
            ) VALUES ('library', ?, ?, ?, ?, ?, ?, ?)
            """,
            (relpath, stat.st_size, stat.st_mtime_ns, fingerprint, RESULT_STATE,
             now, now),
        )
        item_id = int(cursor.lastrowid)

    conn.execute(
        "UPDATE optimization_jobs SET result_item_id=?, updated_at=? WHERE id=?",
        (item_id, now, job_id),
    )
    # Through the normal path, so the result is searchable the moment it is
    # adopted rather than after the next scan. Category comes from the path,
    # which is why nothing had to be carried to make this correct.
    sync_search_item(conn, item_id)
    return item_id


def retire_result_item(
    conn: sqlite3.Connection, *, relpath: str, job_id: int
) -> int | None:
    """Undo has taken the optimized file back to staging. The row cannot follow.

    Marked missing, never deleted and never moved — see the module docstring
    for why neither is available. Search excludes it immediately, so there is
    no phantom library file left behind claiming to be the live one.
    """
    row = conn.execute(
        "SELECT id FROM items WHERE root='library' AND relpath=?", (relpath,)
    ).fetchone()
    if row is None:
        return None
    item_id = int(row["id"])
    conn.execute(
        "UPDATE items SET missing_since=?, last_seen_at=? WHERE id=?",
        (utc_now(), utc_now(), item_id),
    )
    conn.execute(
        "UPDATE optimization_jobs SET updated_at=? WHERE id=?", (utc_now(), job_id)
    )
    sync_search_item(conn, item_id)
    return item_id


def target_relpath(original_relpath: str, output_name: str) -> str:
    """Where the optimized file goes: same folder, same stem, new suffix.

    Optimization is a representation change, so it must not become an excuse to
    reorganise. The destination classifier is deliberately not consulted — the
    file already lives where the owner decided it lives, and asking again could
    move a file somebody had deliberately put somewhere unusual.
    """
    original = Path(original_relpath)
    suffix = Path(output_name).suffix
    if not suffix:
        raise AdoptionError("the optimized output has no file extension")
    return original.with_suffix(suffix).as_posix()
