from __future__ import annotations

import errno
import logging
import os
import shutil
import signal
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from librairy.attributes import normalize_placed_file, parse_mode
from librairy.backup import enqueue_backup_item
from librairy.config import Settings
from librairy.fingerprint import blake2b_file
from librairy.lifecycle import assert_transition
from librairy.locks import acquire_lock
from librairy.optimization_adopt import record_result_item
from librairy.optimization_preflight import target_is_clear
from librairy.optimization_source import (
    SourceRefused,
    is_optimization_source,
    resolve_optimization_source,
)
from librairy.paths import resolve_collision, validate_dest, validate_relpath
from librairy.planner import compute_plan_hash, utc_now
from librairy.quarantine import record_quarantine_entry
from librairy.search import sync_search_item

TERMINAL_RESULTS = {
    "done",
    "skipped_changed",
    "skipped_missing",
    "renamed_collision",
    "failed",
    # An adoption that was refused stays refused. Re-running the same
    # plan cannot help: the plan is immutable and the fact that changed
    # under it has not changed back.
    "refused_source",
    "refused_collision",
}
LOGGER = logging.getLogger(__name__)


class ExecutionError(RuntimeError):
    pass


@dataclass(frozen=True)
class ExecutionSummary:
    plan_id: str
    done: int = 0
    skipped_changed: int = 0
    skipped_missing: int = 0
    renamed_collision: int = 0
    failed: int = 0
    #  Adoption only. An operation that was stopped before it touched anything,
    #  because a fact it depended on had changed since approval.
    refused_source: int = 0
    refused_collision: int = 0

    @property
    def refused(self) -> int:
        return self.refused_source + self.refused_collision

    @property
    def partial(self) -> bool:
        return (
            self.skipped_changed > 0
            or self.skipped_missing > 0
            or self.failed > 0
            or self.refused > 0
        )


def execute_plan(conn: sqlite3.Connection, plan_id: str, settings: Settings) -> ExecutionSummary:
    with acquire_lock(settings):
        summary = _execute_plan_unlocked(conn, plan_id, settings)
    # The one door both the web commit and the CLI go through, which is why the
    # audit finding is settled here rather than at each caller. It is a no-op
    # for an ordinary inbox plan.
    from librairy.corrections import settle_plan
    from librairy.quarantine_requests import settle_quarantine_plan

    settle_plan(conn, plan_id, settings)
    # The same reasoning, for the other kind of decision that waits here: one
    # door, so there is one place to forget rather than two. Both are no-ops
    # for an ordinary inbox plan.
    settle_quarantine_plan(conn, plan_id)
    return summary


def _execute_plan_unlocked(
    conn: sqlite3.Connection,
    plan_id: str,
    settings: Settings,
) -> ExecutionSummary:
    plan = conn.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
    if plan is None:
        raise ExecutionError(f"plan not found: {plan_id}")
    if plan["status"] not in {"approved", "executing", "failed", "done"}:
        raise ExecutionError(f"plan must be approved before commit; status is {plan['status']}")
    if plan["status"] == "done":
        return ExecutionSummary(plan_id)
    if not plan["plan_hash"] or compute_plan_hash(conn, plan_id) != plan["plan_hash"]:
        raise ExecutionError("plan hash mismatch; refusing to touch files")

    conn.execute("UPDATE plans SET status='executing' WHERE id=?", (plan_id,))
    counts = {
        "done": 0,
        "skipped_changed": 0,
        "skipped_missing": 0,
        "renamed_collision": 0,
        "failed": 0,
        # Adoption's two refusals. Both mean "a fact changed since this plan
        # was approved", and both are failures rather than skips: an adoption
        # that placed the original in quarantine and then could not file the
        # optimized copy has left a gap, and calling that a skip would be a
        # cheerful word for a library with a hole in it.
        "refused_source": 0,
        "refused_collision": 0,
    }
    rows = conn.execute(
        "SELECT * FROM plan_ops WHERE plan_id=? ORDER BY seq",
        (plan_id,),
    ).fetchall()
    # A library correction is one logical action over several files. If any
    # part of it has moved on since it was approved, none of it runs: half an
    # album in its new home and half in the old one is worse than either, and
    # it is not what the user approved. Ordinary inbox plans are unaffected —
    # their operations are genuinely independent of one another.
    if plan["audit_finding_id"] is not None:
        blocked = _incoherent_ops(rows, settings)
        if blocked:
            for row in rows:
                _finish_op(conn, row["id"], blocked[row["id"]], None)
                _journal(conn, row, row["dest_relpath"], row["src_fingerprint"], blocked[row["id"]])
                counts[blocked[row["id"]]] += 1
            conn.execute(
                "UPDATE plans SET status='failed', finished_at=? WHERE id=?",
                (utc_now(), plan_id),
            )
            return ExecutionSummary(plan_id, **counts)
    if plan["optimization_job_id"] is not None:
        # Asked before the first operation, so the ordinary answer to "somebody
        # dropped a file at the destination since approval" is that nothing
        # moved at all, rather than that the original went to quarantine and
        # came back.
        occupied = target_is_clear(conn, settings, plan_id)
        if occupied is not None:
            for row in rows:
                _finish_op(conn, row["id"], "refused_collision", None)
                _journal(
                    conn, row, row["dest_relpath"], row["src_fingerprint"],
                    f"refused_collision {occupied.code}",
                )
                counts["refused_collision"] += 1
            conn.execute(
                "UPDATE plans SET status='failed', finished_at=? WHERE id=?",
                (utc_now(), plan_id),
            )
            return ExecutionSummary(plan_id, **counts)
    for row in rows:
        if row["result"] in TERMINAL_RESULTS:
            continue
        try:
            result = _execute_op(conn, row, settings)
        except Exception as exc:
            result = "failed"
            _finish_op(conn, row["id"], result, None)
            _journal(conn, row, row["dest_relpath"], row["src_fingerprint"], str(exc))
        LOGGER.info(
            "plan=%s op=%s type=%s src=%s/%s dest=%s/%s result=%s",
            plan_id,
            row["id"],
            row["op_type"],
            row["src_root"],
            row["src_relpath"],
            row["dest_root"],
            row["dest_relpath"],
            result,
        )
        counts[result] += 1
        _test_pause_after_op()
        if result != "done" and plan["optimization_job_id"] is not None:
            # An adoption is one decision, not two independent moves. If the
            # second half cannot happen, the first half must not stand — and
            # nothing after it may run either.
            _compensate_adoption(conn, plan_id, settings)
            break

    final_status = (
        "failed"
        if counts["failed"]
        or counts["skipped_changed"]
        or counts["skipped_missing"]
        or counts["refused_source"]
        or counts["refused_collision"]
        else "done"
    )
    conn.execute(
        "UPDATE plans SET status=?, finished_at=? WHERE id=?",
        (final_status, utc_now(), plan_id),
    )
    return ExecutionSummary(plan_id, **counts)


def _compensate_adoption(
    conn: sqlite3.Connection, plan_id: str, settings: Settings
) -> None:
    """Put back whatever this adoption already moved. Here, not in History.

    Reuses `undo_op` rather than growing a second reversal routine: it is
    hash-verified, it journals what it did, and it already knows how to put an
    adopted file back in its job's staging directory. Reversing in `id DESC`
    order is what makes the same-path case safe — the optimized copy leaves the
    library slot before the original comes back into it.

    "Go to History and undo the half-finished plan" is not a recovery
    mechanism. It is a thing to tell somebody after their library already has a
    gap in it, and by then they have to know a gap exists.

    The lock is already held by `execute_plan`, so the unlocked form is called
    directly; `undo_op` would deadlock.
    """
    from librairy.history import FORWARD_ACTIONS, _undo_op_unlocked

    done = conn.execute(
        f"""
        SELECT * FROM history
        WHERE plan_id=? AND outcome='ok'
          AND action IN ({",".join("?" * len(FORWARD_ACTIONS))})
        ORDER BY id DESC
        """,  # noqa: S608 - placeholders only
        (plan_id, *FORWARD_ACTIONS),
    ).fetchall()
    for entry in done:
        try:
            result = _undo_op_unlocked(conn, entry["id"], settings)
            outcome = result.outcome
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            outcome = f"error {exc}"
        if outcome == "ok":
            continue
        # Rare, and the application must not hide it. Recorded where History
        # already looks, in relative terms, with the hash somebody would need
        # to check the file by hand.
        LOGGER.error(
            "adoption compensation failed plan=%s entry=%s outcome=%s",
            plan_id, entry["id"], outcome,
        )
        conn.execute(
            """
            INSERT INTO history(
              ts, plan_id, op_id, action, src_root, src_relpath, dest_root,
              dest_relpath, fingerprint, outcome
            ) VALUES (?, ?, ?, 'adoption_recovery', ?, ?, ?, ?, ?, ?)
            """,
            (
                utc_now(),
                plan_id,
                entry["op_id"],
                entry["dest_root"],
                entry["dest_relpath"],
                entry["src_root"],
                entry["src_relpath"],
                entry["fingerprint"],
                f"recovery_required {outcome}",
            ),
        )
        # Nothing further is attempted. Another reversal on top of a failed one
        # is how a bad situation becomes an unreadable one.
        return


def _incoherent_ops(rows: list[sqlite3.Row], settings: Settings) -> dict[int, str]:
    """Empty when every source in a correction group is still exactly as
    approved; otherwise the result to record against each operation.

    Checked before anything moves, and for the whole group, because the answer
    "the primary changed" has to stop the companions too. The per-operation
    fingerprint check in `_execute_op` still runs afterwards — this does not
    replace it, it just makes the group's failure mode all-or-nothing.
    """
    stale = False
    for row in rows:
        if row["result"] in TERMINAL_RESULTS:
            continue
        src = validate_relpath(
            _root_path(settings, row["src_root"]), row["src_relpath"], kind="source"
        )
        if not src.exists() or blake2b_file(src) != row["src_fingerprint"]:
            stale = True
            break
    if not stale:
        return {}
    blocked: dict[int, str] = {}
    for row in rows:
        src = validate_relpath(
            _root_path(settings, row["src_root"]), row["src_relpath"], kind="source"
        )
        blocked[row["id"]] = "skipped_missing" if not src.exists() else "skipped_changed"
    return blocked


def _execute_op(conn: sqlite3.Connection, row: sqlite3.Row, settings: Settings) -> str:
    if is_optimization_source(row["src_root"]):
        return _execute_adoption_op(conn, row, settings)
    src = validate_relpath(_root_path(settings, row["src_root"]), row["src_relpath"], kind="source")
    if not src.exists():
        _finish_op(conn, row["id"], "skipped_missing", None)
        _journal(conn, row, row["dest_relpath"], row["src_fingerprint"], "skipped_missing")
        return "skipped_missing"
    current_fingerprint = blake2b_file(src)
    if current_fingerprint != row["src_fingerprint"]:
        _finish_op(conn, row["id"], "skipped_changed", None)
        _journal(conn, row, row["dest_relpath"], current_fingerprint, "skipped_changed")
        return "skipped_changed"

    dest = validate_dest(_root_path(settings, row["dest_root"]), row["dest_relpath"])
    if _is_adoption_plan(conn, row["plan_id"]) and dest.exists():
        # An adoption never renumbers. See `_execute_adoption_op` for why; the
        # preserved original is held to the same rule, because a preserved
        # `concert (2).wav` is not what the person approved either.
        _finish_op(conn, row["id"], "refused_collision", None)
        _journal(conn, row, row["dest_relpath"], row["src_fingerprint"], "refused_collision")
        return "refused_collision"
    final_dest = resolve_collision(dest)
    final_dest.parent.mkdir(parents=True, exist_ok=True)
    _move_verified(src, final_dest, row["src_fingerprint"], row["plan_id"])
    # After the move and after verification: a file that arrived safely and
    # kept an awkward mode beats a commit that reports failure for a file it
    # actually moved.
    if settings.normalize_attributes:
        normalize_placed_file(
            final_dest,
            file_mode=parse_mode(settings.file_mode),
            dir_mode=parse_mode(settings.dir_mode),
        )
    dest_root = _root_path(settings, row["dest_root"]).resolve()
    final_relpath = final_dest.relative_to(dest_root).as_posix()
    result = "renamed_collision" if final_dest != dest else "done"
    _finish_op(conn, row["id"], result, final_relpath)
    _journal(conn, row, final_relpath, row["src_fingerprint"], "ok")
    _move_item_row(conn, row, final_relpath, final_dest)
    _mark_proposal_committed(conn, row["item_id"])
    if row["dest_root"] == "library":
        enqueue_backup_item(
            conn,
            settings,
            item_id=row["item_id"],
            relpath=final_relpath,
            fingerprint=row["src_fingerprint"],
        )
    if row["op_type"] == "quarantine":
        record_quarantine_entry(conn, row)
    return result


# --- adoption: the one source that is not a root -----------------------------------


def _is_adoption_plan(conn: sqlite3.Connection, plan_id: str) -> bool:
    row = conn.execute(
        "SELECT optimization_job_id FROM plans WHERE id=?", (plan_id,)
    ).fetchone()
    return row is not None and row["optimization_job_id"] is not None


def _execute_adoption_op(
    conn: sqlite3.Connection, row: sqlite3.Row, settings: Settings
) -> str:
    """Move a verified encoder output into the library.

    Deliberately not the generic path. `_root_path` does not know the
    `optimization` namespace and must not learn it — resolving it there would
    make it a destination too, and then any plan could move any file into the
    encoder's workspace. Instead the source is resolved by the job that
    produced it, through every check in `optimization_source`.

    Two other differences from an ordinary move, both of which are the point:

    - **no collision resolution.** `resolve_collision` renumbering an import to
      `photo (2).jpg` is right; `concert (2).flac` sitting beside the
      `concert.wav` it was supposed to replace is not. An occupied destination
      means a fact changed since the plan was approved, and the honest answer
      is to stop.
    - **the item row is created here**, because there is not one to move. The
      generated file has no `items` row while it is in staging, and could not
      have one: `items.root` is CHECK-constrained to the three user roots.
    """
    try:
        resolved = resolve_optimization_source(
            conn,
            settings,
            plan_id=row["plan_id"],
            src_relpath=row["src_relpath"],
            src_fingerprint=row["src_fingerprint"],
            dest_root=row["dest_root"],
        )
    except SourceRefused as exc:
        _finish_op(conn, row["id"], "refused_source", None)
        _journal(
            conn, row, row["dest_relpath"], row["src_fingerprint"],
            f"refused_source {exc.code}",
        )
        return "refused_source"

    dest = validate_dest(_root_path(settings, row["dest_root"]), row["dest_relpath"])
    if dest.exists():
        _finish_op(conn, row["id"], "refused_collision", None)
        _journal(
            conn, row, row["dest_relpath"], row["src_fingerprint"], "refused_collision"
        )
        return "refused_collision"

    dest.parent.mkdir(parents=True, exist_ok=True)
    _move_verified(resolved.path, dest, resolved.fingerprint, row["plan_id"])
    if settings.normalize_attributes:
        normalize_placed_file(
            dest,
            file_mode=parse_mode(settings.file_mode),
            dir_mode=parse_mode(settings.dir_mode),
        )
    dest_root = _root_path(settings, row["dest_root"]).resolve()
    final_relpath = dest.relative_to(dest_root).as_posix()
    _finish_op(conn, row["id"], "done", final_relpath)
    _journal(conn, row, final_relpath, resolved.fingerprint, "ok")

    # Settlement, through the one helper that owns result items. Everything it
    # records is read from the file that is actually there.
    item_id = record_result_item(
        conn, settings, relpath=final_relpath, job_id=resolved.job_id
    )
    conn.execute(
        "UPDATE plan_ops SET item_id=? WHERE id=?", (item_id, row["id"])
    )
    enqueue_backup_item(
        conn,
        settings,
        item_id=item_id,
        relpath=final_relpath,
        fingerprint=resolved.fingerprint,
    )
    return "done"


def _move_verified(src: Path, dest: Path, fingerprint: str, plan_id: str) -> None:
    try:
        os.rename(src, dest)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        temp = dest.with_name(f"{dest.name}.part-{plan_id}")
        if temp.exists():
            temp.unlink()
        shutil.copy2(src, temp)
        if blake2b_file(temp) != fingerprint:
            temp.unlink(missing_ok=True)
            raise ExecutionError("destination fingerprint mismatch after copy") from None
        os.replace(temp, dest)
        os.remove(src)


def _finish_op(
    conn: sqlite3.Connection,
    op_id: int,
    result: str,
    final_relpath: str | None,
) -> None:
    conn.execute(
        "UPDATE plan_ops SET result=?, final_relpath=?, executed_at=? WHERE id=?",
        (result, final_relpath, utc_now(), op_id),
    )


def _journal(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    final_relpath: str | None,
    fingerprint: str | None,
    outcome: str,
) -> None:
    action = "quarantine" if row["op_type"] == "quarantine" else "move"
    conn.execute(
        """
        INSERT INTO history(
          ts, plan_id, op_id, action, src_root, src_relpath, dest_root, dest_relpath,
          fingerprint, outcome
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            utc_now(),
            row["plan_id"],
            row["id"],
            action,
            row["src_root"],
            row["src_relpath"],
            row["dest_root"],
            final_relpath or row["dest_relpath"],
            fingerprint,
            outcome,
        ),
    )


def _mark_proposal_committed(conn: sqlite3.Connection, item_id: int | None) -> None:
    """The file has moved, so its proposal is spent. Per op, not per plan.

    This used to live in the web commit route, behind `if not summary.partial`,
    which got it wrong twice over. A plan where one file had been edited since
    it was planned is "partial", so *every* proposal in it stayed 'proposed' —
    including the ones whose files had already been moved. And the CLI commit
    path never called it at all.

    The result was a review queue full of files that were already filed, each
    one proposing to move itself to where it already was: on this author's
    machine, 140 of 239 rows. Doing it here means it happens exactly when the
    move happens, for every caller, however the rest of the plan goes.
    """
    if item_id is None:
        return
    conn.execute(
        "UPDATE proposals SET status='committed', updated_at=? "
        "WHERE item_id=? AND status<>'committed'",
        (utc_now(), item_id),
    )


def _move_item_row(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    final_relpath: str,
    final_dest: Path,
) -> None:
    stat = final_dest.stat()
    state = "quarantined" if row["dest_root"] == "quarantine" else "discovered"
    current = conn.execute("SELECT state FROM items WHERE id=?", (row["item_id"],)).fetchone()
    if current is not None:
        assert_transition(current["state"], state)
    conn.execute(
        """
        UPDATE items SET root=?, relpath=?, size=?, mtime_ns=?, state=?,
          last_seen_at=?, missing_since=NULL
        WHERE id=?
        """,
        (
            row["dest_root"],
            final_relpath,
            stat.st_size,
            stat.st_mtime_ns,
            state,
            utc_now(),
            row["item_id"],
        ),
    )
    sync_search_item(conn, row["item_id"])


def _root_path(settings: Settings, root: str) -> Path:
    if root == "inbox":
        return settings.inbox_dir
    if root == "library":
        return settings.library_dir
    if root == "quarantine":
        return settings.quarantine_dir
    raise ExecutionError(f"unknown root: {root}")


def _test_pause_after_op() -> None:
    marker = os.environ.get("LIBRAIRY_TEST_PAUSE_AFTER_OP_MARKER")
    if not marker:
        return
    Path(marker).write_text(str(os.getpid()), encoding="utf-8")
    if hasattr(signal, "pause"):
        signal.pause()
    else:  # pragma: no cover - non-POSIX fallback
        time.sleep(60)
