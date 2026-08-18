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
from librairy.optimization_source import OPTIMIZATION_ROOT
from librairy.planner import utc_now
from librairy.reserved import dormant_optimization_relpath
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

    # Found through the job, not through the path. The job is what knows which
    # row its output became; a path lookup would only agree with it by
    # coincidence, and in the same-path case the path is briefly the original's.
    existing = conn.execute(
        "SELECT i.id FROM optimization_jobs j JOIN items i ON i.id = j.result_item_id"
        " WHERE j.id=?",
        (int(job_id),),
    ).fetchone()
    if existing is not None:
        conn.execute(
            """
            UPDATE items
            SET relpath=?, size=?, mtime_ns=?, fingerprint=?, state=?,
                last_seen_at=?, missing_since=NULL
            WHERE id=?
            """,
            (relpath, stat.st_size, stat.st_mtime_ns, fingerprint, RESULT_STATE,
             now, existing["id"]),
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


def parked_relpath(job_id: int, item_id: int) -> str:
    """Where a dormant result row's `relpath` points while its file is away.

    The architecture record said the row would keep the library path it used to
    hold. Writing the same-path case proved that cannot be true for all three
    shapes, and the reason is a constraint, not a preference:

        CREATE TABLE items (... UNIQUE (root, relpath));

    An HEVC re-encode of an MP4 produces an MP4, so the optimized copy lands on
    the original's own path. On Undo the original comes back to that path while
    the dormant row is still claiming it, and SQLite says so:

        UNIQUE constraint failed: items.root, items.relpath

    It is a table constraint, which SQLite cannot alter, and rebuilding `items`
    means dropping a table fifteen foreign keys point into — the same wall that
    blocked a fourth root and a `withdrawn` plan status. So the row yields the
    path, which is the honest answer anyway: there is no file at
    `Music/Live/concert.flac` while the copy is in staging, and a row saying
    there is was the thing this whole audit set out to prevent.

    The address is **reserved** rather than conventional. An earlier version
    parked at `_optimization/<job>/<former path>`, which is a plausible folder
    for somebody to make in their own library — and the moment they did, this
    bookkeeping address would collide with real media through the same UNIQUE
    constraint. `librairy.reserved` owns the namespace and the rules that keep
    real files out of it.

    The former path is deliberately *not* encoded. `optimization_jobs`,
    `plan_ops` and `history` all record where the file was and where it went;
    spelling it a fourth time would be a fourth thing to keep in agreement, and
    this string is never read as a path by anything.
    """
    return dormant_optimization_relpath(job_id, item_id)


def retire_result_item(
    conn: sqlite3.Connection, *, relpath: str, job_id: int
) -> int | None:
    """Undo has taken the optimized file back to staging. The row cannot follow.

    Marked missing, never deleted and never moved to another root — see the
    module docstring for why neither is available — and parked off the library
    path it was holding, for the reason `parked_relpath` records. Search
    excludes it immediately, so there is no phantom library file claiming to be
    the live one.
    """
    row = conn.execute(
        "SELECT i.id FROM optimization_jobs j JOIN items i ON i.id = j.result_item_id"
        " WHERE j.id=?",
        (int(job_id),),
    ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT id FROM items WHERE root='library' AND relpath=?", (relpath,)
        ).fetchone()
    if row is None:
        return None
    item_id = int(row["id"])
    conn.execute(
        "UPDATE items SET relpath=?, missing_since=?, last_seen_at=? WHERE id=?",
        (parked_relpath(job_id, item_id), utc_now(), utc_now(), item_id),
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


def plan_adoption(
    conn: sqlite3.Connection, settings: Settings, job_id: int
) -> str | object:
    """One decision, two operations, no filesystem move.

    Returns the approved plan's id, or the preflight `Refusal` that stopped it.
    Nothing here computes a path: every value comes from `adoption_preflight`,
    so what was checked and what the plan does cannot drift apart.

        op 1  library/<original>      -> quarantine/<same relpath>   preserve
        op 2  optimization/<job>/<out> -> library/<target>            adopt

    The order is the whole reason the same-path HEVC case works. Operation 1
    takes `Movies/film.mkv` out of the library before operation 2 puts the new
    `Movies/film.mkv` in, and `undo_plan` reverses in `id DESC`, which is
    exactly the inverse — so neither direction ever has two files wanting one
    path.

    The plan is approved here rather than left as a draft. Approval is what
    runs the closed resolver over operation 2, and a draft adoption nobody
    approved would be a second waiting state beside "waiting for Commit".
    """
    from librairy.db import transaction
    from librairy.optimization_preflight import Refusal, adoption_preflight
    from librairy.planner import OperationSpec, approve_plan, create_plan

    checked = adoption_preflight(conn, settings, job_id)
    if isinstance(checked, Refusal):
        return checked

    with transaction(conn):
        plan_id = create_plan(
            conn,
            [
                OperationSpec(
                    "quarantine",
                    checked.original_relpath,
                    "quarantine",
                    checked.preserved_relpath,
                    src_root="library",
                ),
                OperationSpec(
                    "move",
                    checked.generated_relpath,
                    "library",
                    checked.target_relpath,
                    src_root=OPTIMIZATION_ROOT,
                    src_fingerprint=checked.generated_fingerprint,
                ),
            ],
            settings,
        )
        conn.execute(
            "UPDATE plans SET optimization_job_id=? WHERE id=?", (int(job_id), plan_id)
        )
        conn.execute(
            "UPDATE plan_ops SET role='preserve' WHERE plan_id=? AND seq=1", (plan_id,)
        )
        conn.execute(
            "UPDATE plan_ops SET role='adopt' WHERE plan_id=? AND seq=2", (plan_id,)
        )
        approve_plan(conn, plan_id, settings)
    return plan_id


def cancel_adoption(conn: sqlite3.Connection, plan_id: str) -> bool:
    """Withdraw an approved adoption before Commit. Moves nothing.

    Deliberately the same shape as `corrections.withdraw_approval`, and
    deliberately not called Undo: Undo reverses files that moved, this reverses
    a decision about files that did not. An approved plan is immutable, so it
    is not mutated — it is withdrawn whole, which is safe precisely because
    nothing executed. A plan with any executed operation is refused, so a
    half-run commit can never be cancelled out of existence.

    Removing the plan is what releases the job: the partial unique index, the
    resolver and preflight all key off `status IN ('approved','executing')`, so
    the moment the row is gone the optimization is offerable again.
    """
    from librairy.db import transaction

    plan = conn.execute(
        "SELECT * FROM plans WHERE id=?", (plan_id,)
    ).fetchone()
    if plan is None or plan["optimization_job_id"] is None:
        return False
    if plan["status"] != "approved":
        return False
    ops = conn.execute(
        "SELECT * FROM plan_ops WHERE plan_id=? ORDER BY seq", (plan_id,)
    ).fetchall()
    if any(op["result"] is not None for op in ops):
        return False

    with transaction(conn):
        # Written before the delete, while the hash and the approval time are
        # still readable. One row describing one decision — never a claim that
        # files moved.
        conn.execute(
            "INSERT INTO plan_withdrawals(plan_id, plan_hash, audit_finding_id,"
            " relpath, dest_relpath, op_count, approved_at, withdrawn_at)"
            " VALUES (?, ?, NULL, ?, ?, ?, ?, ?)",
            (
                plan_id,
                plan["plan_hash"],
                ops[0]["src_relpath"] if ops else "",
                ops[-1]["dest_relpath"] if ops else None,
                len(ops),
                plan["approved_at"],
                utc_now(),
            ),
        )
        conn.execute("DELETE FROM plan_ops WHERE plan_id=?", (plan_id,))
        conn.execute("DELETE FROM plans WHERE id=?", (plan_id,))
    return True


# --- effective state -------------------------------------------------------------
#
# `optimization_jobs.state` says `ready` for a verified result whether or not a
# person has decided anything about it. Once an adoption plan exists, the job is
# *waiting for Commit* — and the plan is the immutable record, so it outranks
# the cheaper column, exactly as an active correction plan outranks a finding's
# status.

WAITING_FOR_COMMIT = "waiting-for-commit"
APPLYING = "applying"
ADOPTED = "adopted"


def active_adoption(conn: sqlite3.Connection, job_id: int):
    """The approved-or-executing adoption plan for this job, if there is one.

    One at most: `idx_plans_one_active_per_optimization` is a partial unique
    index over exactly this predicate, so this cannot quietly return the first
    of several.
    """
    return conn.execute(
        "SELECT * FROM plans WHERE optimization_job_id=?"
        " AND status IN ('approved','executing')",
        (int(job_id),),
    ).fetchone()


def adoption_state(conn: sqlite3.Connection, job_id: int) -> str:
    """What this job is effectively doing, plan first and column second."""
    plan = active_adoption(conn, job_id)
    if plan is not None:
        return APPLYING if plan["status"] == "executing" else WAITING_FOR_COMMIT
    adopted = conn.execute(
        "SELECT i.id FROM optimization_jobs j JOIN items i ON i.id = j.result_item_id"
        " WHERE j.id=? AND i.missing_since IS NULL",
        (int(job_id),),
    ).fetchone()
    return ADOPTED if adopted is not None else ""
