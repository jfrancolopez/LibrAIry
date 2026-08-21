from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from librairy.config import Settings
from librairy.fingerprint import blake2b_file
from librairy.lifecycle import assert_transition
from librairy.locks import acquire_lock
from librairy.paths import resolve_collision, validate_dest, validate_relpath
from librairy.planner import OperationSpec, utc_now
from librairy.search import sync_search_item

#  Quarantine is the out-tray: set aside indefinitely, still yours. This is the
#  shelf inside it for the files you have finished with -- one folder, so you
#  can point a file manager at it and empty it in one gesture. LibrAIry still
#  never deletes anything; it only ever gathers.
DELETE_PILE = "_to-delete"


class QuarantineError(RuntimeError):
    pass


@dataclass(frozen=True)
class RestoreResult:
    entry_id: int
    outcome: str
    dest_relpath: str | None = None


def quarantine_operation(src_relpath: str, date: str | None = None) -> OperationSpec:
    day = date or datetime.now(UTC).date().isoformat()
    return OperationSpec("quarantine", src_relpath, "quarantine", f"{day}/{src_relpath}")


def deletion_operation(src_relpath: str, date: str | None = None) -> OperationSpec:
    """Straight to the delete pile, skipping the shelf you might change your
    mind on. Still a quarantine move: same planner, same hash check, same
    journal, and "Put it back" still works."""
    day = date or datetime.now(UTC).date().isoformat()
    return OperationSpec(
        "quarantine", src_relpath, "quarantine", f"{DELETE_PILE}/{day}/{src_relpath}"
    )


def marked_for_deletion(relpath: str | None) -> bool:
    return bool(relpath) and str(relpath).replace("\\", "/").split("/", 1)[0] == DELETE_PILE


def _duplicate_of(conn: sqlite3.Connection, op: sqlite3.Row) -> int | None:
    """The library file this one is a copy of, when that is why it is here.

    Two ways to be here for a reason involving another file, and they resolve
    differently because they mean different things. An exact duplicate is
    matched by fingerprint — the same bytes are somewhere else. A similar
    representation is not: the bytes differ, and the file it points at is one
    of the representations the person kept.
    """
    from librairy.inbox_duplicates import twins_of
    from librairy.similar_media import companion_of

    item_id = op["item_id"]
    if item_id is None:
        return None
    reason = _plan_reason(conn, op) or quarantine_reason(conn, int(item_id))
    if reason == "similar_media":
        return companion_of(conn, int(item_id))
    if reason != "exact_duplicate":
        return None
    twins = twins_of(conn, int(item_id))
    return twins[0].item_id if twins else None


def _plan_reason(conn: sqlite3.Connection, op: sqlite3.Row) -> str:
    """`similar_media` when this quarantine came from a comparison.

    Read off the plan's finding rather than stored a second time. A library
    file set aside from Review has no proposal to read evidence from, so
    `quarantine_reason` would fall through to `user` — "you said you did not
    want it" — over a file that was set aside after comparing two encodes.
    """
    from librairy.similar_media import KIND

    plan_id = op["plan_id"]
    if not plan_id:
        return ""
    found = conn.execute(
        "SELECT f.kind FROM plans p JOIN audit_findings f ON f.id = p.audit_finding_id"
        " WHERE p.id=?",
        (plan_id,),
    ).fetchone()
    return "similar_media" if found is not None and found["kind"] == KIND else ""


def quarantine_reason(conn: sqlite3.Connection, item_id: int) -> str:
    """Why this file is in quarantine: the duplicate finder, or you.

    Two very different answers, and the row recorded `exact_duplicate` for both
    — so a file you set aside by hand in Review was described on the Quarantine
    page as a byte-for-byte copy of something you already have. Read back off
    the evidence rather than stored twice, which is what the staged list above
    it already does; two columns saying why can disagree, one cannot.
    """
    row = conn.execute(
        "SELECT evidence FROM proposals WHERE item_id=? AND status != 'superseded' "
        "ORDER BY id DESC LIMIT 1",
        (item_id,),
    ).fetchone()
    evidence = (row["evidence"] if row else "") or ""
    return "exact_duplicate" if "exact duplicate of" in evidence else "user"


#  What a preserved original is, said as a value rather than as a `reason`.
#  `quarantine_entries.reason` is CHECK-constrained to three strings and SQLite
#  cannot widen a CHECK, so the fourth kind is carried by the job link instead.
PRESERVED_ORIGINAL = "preserved_original"


def record_quarantine_entry(conn: sqlite3.Connection, op: sqlite3.Row) -> None:
    #  An adoption's first operation preserves an original; it is not a
    #  rejection. The link is what says so — see `quarantine_effective_reason`.
    plan = conn.execute(
        "SELECT optimization_job_id FROM plans WHERE id=?", (op["plan_id"],)
    ).fetchone()
    job_id = plan["optimization_job_id"] if plan is not None else None
    conn.execute(
        """
        INSERT INTO quarantine_entries(
          item_id, reason, duplicate_of, original_root, original_relpath,
          quarantined_at, plan_id, optimization_job_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            op["item_id"],
            _plan_reason(conn, op) or quarantine_reason(conn, op["item_id"]),
            #  Which file this is a copy of. The column has existed since the
            #  first release and was written as NULL, so the Quarantine page
            #  could say "exact duplicate" and not say of *what* — which is the
            #  only part a person needs in order to decide whether to restore
            #  it. Resolved by fingerprint at the moment of quarantine, so a
            #  library copy that has since moved is still the right answer.
            _duplicate_of(conn, op),
            op["src_root"],
            op["src_relpath"],
            utc_now(),
            op["plan_id"],
            job_id,
        ),
    )


def quarantine_effective_reason(entry) -> str:
    """Why this file is really here, which is not always what `reason` says.

    A file preserved by an adoption has no proposal behind it, so
    `quarantine_reason` falls through to `user` — and `user` is rendered
    everywhere as "you said you did not want it", which is the exact opposite
    of what happened. The person asked for a smaller copy and LibrAIry kept the
    original for them.

    The stored column cannot say so: its CHECK allows three values and SQLite
    cannot widen a CHECK. So the truth lives in the job link, and this is the
    one place that reads it — for the badge, for the sentence, for restore
    eligibility and for the delete queue alike, so none of them can disagree.
    """
    try:
        job_id = entry["optimization_job_id"]
    except (KeyError, IndexError):
        job_id = None
    if job_id is not None:
        return PRESERVED_ORIGINAL
    return str(entry["reason"] or "")


def is_preserved_original(entry) -> bool:
    return quarantine_effective_reason(entry) == PRESERVED_ORIGINAL


def list_quarantine_entries(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM quarantine_entries ORDER BY id"))


def restore_entry(conn: sqlite3.Connection, entry_id: int, settings: Settings) -> RestoreResult:
    with acquire_lock(settings):
        return _restore_entry_unlocked(conn, entry_id, settings)


def _restore_entry_unlocked(
    conn: sqlite3.Connection,
    entry_id: int,
    settings: Settings,
) -> RestoreResult:
    from librairy.executor import _move_verified

    entry = conn.execute("SELECT * FROM quarantine_entries WHERE id=?", (entry_id,)).fetchone()
    if entry is None:
        raise QuarantineError(f"quarantine entry not found: {entry_id}")
    if is_preserved_original(entry):
        # Generic Restore would put the original back beside the optimized copy
        # and leave the job believing it was adopted — two files where the
        # person asked for one, and a result item claiming to be the live
        # version of a recording that is no longer being replaced. Restoring a
        # preserved original means undoing the adoption, and Undo already knows
        # how to do that in the right order, with hashes checked.
        raise QuarantineError(
            "this is a preserved original; restore it by undoing the "
            "optimization that replaced it"
        )
    if entry["restored_at"] is not None:
        return RestoreResult(entry_id, "already_restored")
    item = conn.execute("SELECT * FROM items WHERE id=?", (entry["item_id"],)).fetchone()
    if item is None or item["root"] != "quarantine":
        return RestoreResult(entry_id, "missing")
    src = validate_relpath(settings.quarantine_dir, item["relpath"], kind="source")
    if not src.exists():
        return RestoreResult(entry_id, "missing")
    fingerprint = blake2b_file(src)
    dest = validate_dest(_root_path(settings, entry["original_root"]), entry["original_relpath"])
    final_dest = resolve_collision(dest)
    final_dest.parent.mkdir(parents=True, exist_ok=True)
    _move_verified(src, final_dest, fingerprint, f"restore-{entry_id}")
    root_path = _root_path(settings, entry["original_root"]).resolve()
    final_relpath = final_dest.relative_to(root_path).as_posix()
    stat = final_dest.stat()
    assert_transition(item["state"], "discovered")
    conn.execute(
        """
        UPDATE items SET root=?, relpath=?, size=?, mtime_ns=?, state='discovered',
          last_seen_at=?, missing_since=NULL
        WHERE id=?
        """,
        (
            entry["original_root"],
            final_relpath,
            stat.st_size,
            stat.st_mtime_ns,
            utc_now(),
            item["id"],
        ),
    )
    sync_search_item(conn, item["id"])
    conn.execute("UPDATE quarantine_entries SET restored_at=? WHERE id=?", (utc_now(), entry_id))
    conn.execute(
        """
        INSERT INTO history(
          ts, plan_id, op_id, action, src_root, src_relpath, dest_root, dest_relpath,
          fingerprint, outcome
        ) VALUES (?, ?, NULL, 'restore_quarantine', 'quarantine', ?, ?, ?, ?, 'ok')
        """,
        (
            utc_now(),
            entry["plan_id"],
            item["relpath"],
            entry["original_root"],
            final_relpath,
            fingerprint,
        ),
    )
    return RestoreResult(entry_id, "ok", final_relpath)


def mark_entry_for_deletion(
    conn: sqlite3.Connection, entry_id: int, settings: Settings
) -> RestoreResult:
    """Move a held file into the delete pile. Still does not delete it.

    The out-tray answers "I do not want this in the library". It does not
    answer "I am done with this file", and without somewhere to say so the
    only way to act on a quarantine of two hundred files is to go through it
    by hand in a file manager. This gathers the ones you have finished with
    into a single folder you can empty in one gesture, deliberately, yourself.

    "Put it back" still works from here: the entry remembers where the file
    came from, not where it is sitting now.
    """
    with acquire_lock(settings):
        return _mark_entry_unlocked(conn, entry_id, settings)


def _mark_entry_unlocked(
    conn: sqlite3.Connection, entry_id: int, settings: Settings
) -> RestoreResult:
    from librairy.executor import _move_verified

    entry = conn.execute("SELECT * FROM quarantine_entries WHERE id=?", (entry_id,)).fetchone()
    if entry is None:
        raise QuarantineError(f"quarantine entry not found: {entry_id}")
    if is_preserved_original(entry):
        # Not withheld any more — but not available *here*. This helper moves
        # the file inside the request handler, with no plan and nothing in
        # Commit, and a preserved original's Undo depends on exactly where its
        # file is. That move has to be journalled and reversible, which is what
        # `quarantine_requests.request_delete_queue` and Commit provide, and
        # what `optimization_disposal` reverses in order afterwards.
        raise QuarantineError(
            "a preserved original goes through Commit; use Delete queue on the "
            "Quarantine page"
        )
    if entry["restored_at"] is not None:
        return RestoreResult(entry_id, "already_restored")
    item = conn.execute("SELECT * FROM items WHERE id=?", (entry["item_id"],)).fetchone()
    if item is None or item["root"] != "quarantine":
        return RestoreResult(entry_id, "missing")
    if marked_for_deletion(item["relpath"]):
        return RestoreResult(entry_id, "already_marked", item["relpath"])
    src = validate_relpath(settings.quarantine_dir, item["relpath"], kind="source")
    if not src.exists():
        return RestoreResult(entry_id, "missing")
    fingerprint = blake2b_file(src)
    dest = validate_dest(settings.quarantine_dir, f"{DELETE_PILE}/{item['relpath']}")
    final_dest = resolve_collision(dest)
    final_dest.parent.mkdir(parents=True, exist_ok=True)
    _move_verified(src, final_dest, fingerprint, f"mark-delete-{entry_id}")
    final_relpath = final_dest.relative_to(settings.quarantine_dir.resolve()).as_posix()
    stat = final_dest.stat()
    conn.execute(
        "UPDATE items SET relpath=?, size=?, mtime_ns=?, last_seen_at=? WHERE id=?",
        (final_relpath, stat.st_size, stat.st_mtime_ns, utc_now(), item["id"]),
    )
    sync_search_item(conn, item["id"])
    conn.execute(
        """
        INSERT INTO history(
          ts, plan_id, op_id, action, src_root, src_relpath, dest_root, dest_relpath,
          fingerprint, outcome
        ) VALUES (?, ?, NULL, 'mark_for_deletion', 'quarantine', ?, 'quarantine', ?, ?, 'ok')
        """,
        (utc_now(), entry["plan_id"], item["relpath"], final_relpath, fingerprint),
    )
    return RestoreResult(entry_id, "ok", final_relpath)


def restore_all(conn: sqlite3.Connection, settings: Settings) -> list[RestoreResult]:
    rows = conn.execute(
        "SELECT id FROM quarantine_entries WHERE restored_at IS NULL ORDER BY id"
    ).fetchall()
    with acquire_lock(settings):
        return [_restore_entry_unlocked(conn, row["id"], settings) for row in rows]


def _root_path(settings: Settings, root: str):
    if root == "inbox":
        return settings.inbox_dir
    if root == "library":
        return settings.library_dir
    if root == "quarantine":
        return settings.quarantine_dir
    raise QuarantineError(f"unknown root: {root}")


def destination_intent(dest_root: str | None, dest_relpath: str | None) -> str:
    """What a destination *means*, not just where it points.

    Three proposals can carry a quarantine path and mean different things, and
    the path alone does not say which. One vanished entry here reads

        quarantine/2026-08-06/_drop/Test.Show.S01E05.1080p.mkv

    under a heading that used to say nothing at all, and next to six entries
    genuinely bound for the library it looks like one more filing decision. It
    is the opposite: that file had been set aside, not filed.
    """
    if not dest_relpath:
        return ""
    if dest_root == "quarantine":
        #  Never the banned phrase, which a person saw here: nothing is marked
        #  for deletion anywhere in LibrAIry — the delete queue is a folder.
        if marked_for_deletion(dest_relpath):
            return "Headed for the delete queue"
        return "Set aside"
    return "Would have been filed as"
