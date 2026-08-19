from __future__ import annotations

import sqlite3
from dataclasses import asdict

from librairy.config import Settings
from librairy.db import transaction
from librairy.humanize import human_ago, human_bytes
from librairy.lifecycle import LifecycleError, assert_transition, transition_item
from librairy.optimization_disposal import (
    IN_DELETE_QUEUE,
    PRESERVED,
    REMOVED,
    RESTORED,
    STATE_LABEL,
    WAITING,
    preserved_state,
)
from librairy.optimization_storage import (
    ADOPTED,
    ORIGINAL_IN_DELETE_QUEUE,
    ORIGINAL_REMOVED,
    UNDONE,
)
from librairy.planner import utc_now
from librairy.quarantine import (
    DELETE_PILE,
    PRESERVED_ORIGINAL,
    QuarantineError,
    is_preserved_original,
    mark_entry_for_deletion,
    marked_for_deletion,
    quarantine_effective_reason,
    restore_entry,
)
from librairy.web.evidence import humanize_evidence

#  Two vocabularies for one situation, mapped in one place. Where the file is
#  decides what the arithmetic is, and the delete queue changes the first
#  without changing the second — which is exactly the confusion this mapping
#  exists to make impossible to introduce by hand in a template.
STORAGE_STATE = {
    PRESERVED: ADOPTED,
    WAITING: ADOPTED,
    IN_DELETE_QUEUE: ORIGINAL_IN_DELETE_QUEUE,
    REMOVED: ORIGINAL_REMOVED,
    RESTORED: UNDONE,
    "": ADOPTED,
}

#  What the reason column holds, said the way a person would say it. The keys
#  are the three the schema's CHECK allows — `user_discard` was here instead of
#  `user`, which the column cannot hold, so every hand-quarantined file read
#  "no reason recorded".
REASONS = {
    "exact_duplicate": "byte-for-byte copy of a file you already have",
    "similar_media": "close enough to something you already have to be worth a look",
    "user": "you said you did not want it",
    #  Not a value the column can hold — its CHECK allows the three above and
    #  SQLite cannot widen a CHECK. `quarantine_effective_reason` derives it
    #  from the optimization job the entry is linked to, so the page and every
    #  non-UI consumer read the same answer from the same function.
    PRESERVED_ORIGINAL: "preserved when an optimized version was adopted",
}
UNWANTED = "you sent it here from Review"

#  One word for the badge, where the sentence above is the explanation.
REASON_TAGS = {
    "exact_duplicate": "duplicate",
    "similar_media": "similar",
    "user": "you sent it here",
    PRESERVED_ORIGINAL: "preserved original",
}


# One page of rows, whatever the database holds. A quarantine with ten
# thousand files in it is a real thing — a deduplication run over a photo
# library produces one — and rendering all of them was never a decision
# anybody took, it was just what happened when nobody wrote a LIMIT.
PAGE_SIZE = 50

# What the user can be looking at. Every one maps to a real state; there is no
# tab here that is a filter over nothing.
VIEWS = {
    "held": "Held",
    "waiting": "Waiting for Commit",
    "delete-queue": "Delete queue",
    #  Files LibrAIry can no longer see. Emptying the delete queue is the
    #  intended way to arrive here, and until this view existed those rows
    #  stayed under Delete queue for ever — so the count of "files waiting for
    #  you to delete" only ever went up, including the ones you had deleted.
    "removed": "Removed",
    "restored": "Put back",
}
DEFAULT_VIEW = "held"


def quarantine_data(
    conn: sqlite3.Connection,
    settings: Settings | None = None,
    *,
    view: str = "",
    page: int = 1,
) -> dict[str, object]:
    """One bounded page of quarantine, plus counts for the whole of it.

    The counts come from SQL over the whole table; the rows come from one
    `LIMIT`-ed query. That split is the entire scalability story for this page:
    the summary stays true at a million rows and the DOM stays the same size.
    """
    view = view if view in VIEWS else DEFAULT_VIEW
    page = max(1, page)
    counts = _counts(conn)
    total = counts.get(view, 0)
    rows = _entries(conn, view=view, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE)
    host_dir = str(settings.host_quarantine_dir) if settings else ""
    return {
        "staged": _staged(conn),
        "entries": rows,
        "similar_flags": _similar_flags(conn),
        "view": view,
        "views": VIEWS,
        "counts": counts,
        "page": page,
        "page_size": PAGE_SIZE,
        "total": total,
        "page_count": max(1, -(-total // PAGE_SIZE)),
        "has_next": page * PAGE_SIZE < total,
        "has_prev": page > 1,
        "range_start": 0 if not total else (page - 1) * PAGE_SIZE + 1,
        "range_end": min(page * PAGE_SIZE, total),
        # The one thing the page could not answer: where the files actually
        # are, so you can go and delete them yourself. LibrAIry will not.
        "host_quarantine_dir": host_dir,
        "held": counts.get("held", 0),
        #  Which of the files in this view are optimization originals. A subset
        #  of the count above, said as a subset — they are not a sixth bucket,
        #  and one physical file is never in two.
        "preserved_here": counts.get(f"preserved:{view}", 0),
        "removed": counts.get("removed", 0),
        # The pile you asked for: one folder to point a file manager at, so
        # emptying it is one deliberate gesture rather than two hundred.
        "for_deletion": counts.get("delete-queue", 0),
        "delete_pile_dir": f"{host_dir.rstrip('/')}/{DELETE_PILE}" if host_dir else "",
    }


# A held file is in exactly one of these states, and the expressions below are
# the definition. Written once, in SQL, so the count in the tab and the rows
# under it cannot drift apart — two hand-written filters agreeing is luck.
_ACTIVE_PLAN = (
    "EXISTS (SELECT 1 FROM plans p WHERE p.quarantine_entry_id = qe.id"
    " AND p.status IN ('approved','executing'))"
)
_IN_DELETE_QUEUE = "i.relpath LIKE '_to-delete/%' ESCAPE '\\'"
#  The same predicate `live.py` owns, phrased for this join: no row at all, or a
#  row whose file is not at that path right now. An unmounted share and an
#  emptied delete queue look identical from here, and both mean the same thing
#  for what may be offered.
_GONE = "(i.id IS NULL OR i.missing_since IS NOT NULL)"
_WHERE = {
    "held": (
        f"qe.restored_at IS NULL AND NOT {_GONE} AND NOT {_ACTIVE_PLAN}"
        f" AND NOT ({_IN_DELETE_QUEUE})"
    ),
    "waiting": f"qe.restored_at IS NULL AND NOT {_GONE} AND {_ACTIVE_PLAN}",
    "delete-queue": f"qe.restored_at IS NULL AND NOT {_GONE} AND ({_IN_DELETE_QUEUE})",
    "removed": f"qe.restored_at IS NULL AND {_GONE}",
    "restored": "qe.restored_at IS NOT NULL",
}

#  A subset of the buckets above, never a bucket of its own: one physical file
#  belongs to exactly one view, and a sixth tab that overlapped the others would
#  count the same original twice. Reported as a caption instead.
_PRESERVED = "qe.optimization_job_id IS NOT NULL"


def held_count(conn: sqlite3.Connection) -> int:
    """How many quarantined files have had no decision taken about them.

    Public because the Dashboard needs exactly this number and had grown its
    own arithmetic for it — present minus the delete queue, which counted the
    files whose decision was approved and waiting for Commit. Two answers to
    one question is how a dashboard comes to contradict the page it links to.
    """
    return int(_counts(conn).get("held", 0))


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    """How many are in each view, counted in SQL over the whole table.

    One query, four counts, no rows loaded into Python. `SELECT COUNT(*)` over
    an indexed table stays fast at sizes where building a list of entry dicts
    to call `len()` on does not.
    """
    parts = ", ".join(
        [
            f"SUM(CASE WHEN {clause} THEN 1 ELSE 0 END) AS \"{name}\""
            for name, clause in _WHERE.items()
        ]
        + [
            f"SUM(CASE WHEN {_PRESERVED} AND ({clause}) THEN 1 ELSE 0 END)"
            f" AS \"preserved:{name}\""
            for name, clause in _WHERE.items()
        ]
    )
    row = conn.execute(
        f"SELECT {parts} FROM quarantine_entries qe"  # noqa: S608 — module constants
        " LEFT JOIN items i ON i.id = qe.item_id"
    ).fetchone()
    counts = {name: int(row[name] or 0) for name in _WHERE}
    counts.update(
        {f"preserved:{name}": int(row[f"preserved:{name}"] or 0) for name in _WHERE}
    )
    return counts


def reason_text(reason: str | None) -> str:
    return REASONS.get(str(reason or ""), str(reason or "no reason recorded"))


def staged_reason(evidence: str | None) -> str:
    """Why a file is queued for quarantine, from the evidence already on it.

    Only two things put a file here: the duplicate finder, which writes
    "exact duplicate of ..." into its evidence, and Quarantine in Review,
    which leaves the original evidence untouched. Reading it back beats
    another column that could disagree with the evidence beside it.
    """
    if "exact duplicate of" in (evidence or ""):
        return REASONS["exact_duplicate"]
    return UNWANTED


def human_size(size: int | None) -> str:
    if not size or size < 0:
        return ""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return ""


def restore_quarantine(
    conn: sqlite3.Connection, settings: Settings, entry_id: int
) -> dict[str, object]:
    return asdict(restore_entry(conn, entry_id, settings))


def mark_for_deletion(
    conn: sqlite3.Connection, settings: Settings, entry_id: int
) -> dict[str, object]:
    return asdict(mark_entry_for_deletion(conn, entry_id, settings))


# The three buttons on a staged row only make sense while the row is still
# waiting on an answer. The page can be a minute old, the same POST can arrive
# from curl, and pressing two buttons in a row on one stale page is not exotic:
# `Move it out` followed by `Keep it` was the first thing that produced a 500.
STAGED_STATUSES = ("proposed", "postponed", "approved")
_NOT_STAGED = "that suggestion is no longer waiting for an answer"
_NOT_ELIGIBLE = "this item is no longer eligible for that action"


def _staged_proposal(conn: sqlite3.Connection, proposal_id: int) -> sqlite3.Row:
    """The row, or a refusal. Never an exception a route has to guess at.

    Refusals are decided here rather than by drawing or not drawing a button.
    A control that is absent from the page is not a guarantee about what the
    server will accept.
    """
    row = conn.execute(
        """
        SELECT p.id, p.item_id, p.status, p.action, i.state, i.relpath
        FROM proposals p JOIN items i ON i.id = p.item_id
        WHERE p.id=?
        """,
        (proposal_id,),
    ).fetchone()
    if row is None:
        raise QuarantineError("that suggestion no longer exists")
    if row["action"] != "quarantine" or row["status"] not in STAGED_STATUSES:
        raise QuarantineError(_NOT_STAGED)
    return row


def _may_transition(state: str, target: str) -> bool:
    try:
        assert_transition(state, target)
    except LifecycleError:
        return False
    return True


def unstage_proposal(conn: sqlite3.Connection, proposal_id: int) -> None:
    """"Dismiss suggestion" — the file is filed normally after all.

    Withdrawing an approval is two events, and the lifecycle is right to want
    both spelled out: the answer is taken back (the item is undecided again),
    and only then does the machine's suggestion stand once more. Going straight
    from `approved` to `proposed` is what the lifecycle forbids on purpose, so
    that a duplicate found late cannot quietly overwrite an answer already
    given. This is the owner giving a different one.
    """
    row = _staged_proposal(conn, proposal_id)
    if row["state"] == "approved" and _may_transition(row["state"], "discovered"):
        transition_item(conn, row["item_id"], "discovered")
        row = _staged_proposal(conn, proposal_id)
    if not _may_transition(row["state"], "proposed"):
        raise QuarantineError(_NOT_ELIGIBLE)
    with transaction(conn):
        transition_item(conn, row["item_id"], "proposed")
        conn.execute(
            """
            UPDATE proposals
            SET action='move', dest_root='library', status='proposed', updated_at=?
            WHERE id=?
            """,
            (utc_now(), proposal_id),
        )


def stage_for_deletion(conn: sqlite3.Connection, proposal_id: int) -> None:
    """Approve a staged quarantine, aimed at the delete pile instead.

    Without this, being finished with a duplicate that has not moved yet costs
    two commits: one to put it in quarantine and another to move it along.

    The eligibility question is asked before anything is written. It used to be
    asked in the middle: `discard_proposals` rewrote the proposal's status and
    destination, *then* moved the item, so an item that could not legally reach
    `approved` left the row pointing at `_to-delete/` with no approval behind
    it. A refusal that half-applies is worse than the 500 it came with.
    """
    from librairy.web.review import discard_proposals

    row = _staged_proposal(conn, proposal_id)
    if row["status"] not in ("proposed", "postponed"):
        raise QuarantineError(_NOT_STAGED)
    if not _may_transition(row["state"], "approved"):
        raise QuarantineError(_NOT_ELIGIBLE)
    with transaction(conn):
        if not discard_proposals(conn, [proposal_id], to_delete_pile=True):
            raise QuarantineError(_NOT_STAGED)


def approve_stage(conn: sqlite3.Connection, proposal_id: int) -> None:
    row = _staged_proposal(conn, proposal_id)
    if not _may_transition(row["state"], "approved"):
        raise QuarantineError(_NOT_ELIGIBLE)
    with transaction(conn):
        transition_item(conn, row["item_id"], "approved")
        conn.execute(
            "UPDATE proposals SET status='approved', updated_at=? WHERE id=?",
            (utc_now(), proposal_id),
        )


def _staged(conn: sqlite3.Connection) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT p.*, i.relpath AS item_relpath, i.size AS item_size
        FROM proposals p
        JOIN items i ON i.id = p.item_id
        WHERE p.action='quarantine' AND p.status IN ('proposed', 'approved')
        ORDER BY p.id DESC
        """
    ).fetchall()
    return [
        {
            **dict(row),
            "evidence_views": humanize_evidence(row["evidence"] or ""),
            "reason_text": staged_reason(row["evidence"]),
            "size_label": human_size(row["item_size"]),
        }
        for row in rows
    ]


def _entries(
    conn: sqlite3.Connection,
    *,
    view: str = DEFAULT_VIEW,
    limit: int = PAGE_SIZE,
    offset: int = 0,
) -> list[dict[str, object]]:
    """One page of held files, filtered and ordered in SQL.

    `ORDER BY qe.id DESC` with `LIMIT/OFFSET` over a primary key is a stable,
    total order, which is what makes paging deterministic: no row appears on
    two pages and none is skipped between them.

    Nothing here touches the filesystem. Every fact on the row — where it is,
    where it came from, how big it is, what is waiting on it — is already in
    the database, and a `stat()` per row is what turns a page into a network
    round trip per file on a NAS.
    """
    rows = list(
        conn.execute(
            f"""
            SELECT qe.*, i.relpath AS item_relpath, i.size AS item_size,
                   i.state AS item_state, i.missing_since AS item_missing_since
            FROM quarantine_entries qe
            LEFT JOIN items i ON i.id = qe.item_id
            WHERE {_WHERE.get(view, _WHERE[DEFAULT_VIEW])}
            ORDER BY qe.id DESC
            LIMIT ? OFFSET ?
            """,  # noqa: S608 — clause comes from the module's own dict
            (limit, offset),
        )
    )
    # One query for the whole page rather than one per row: a request lookup
    # inside the loop is the N+1 that makes fifty rows fifty-one queries.
    from librairy.quarantine_requests import pending_requests

    requests = pending_requests(conn)
    return [
        {
            **dict(row),
            "reason_text": reason_text(quarantine_effective_reason(row)),
            "reason_tag": REASON_TAGS.get(
                quarantine_effective_reason(row), "set aside"
            ),
            #  A preserved original is not a rejection, and its controls are not
            #  the generic ones. Restore means undo the adoption, and the delete
            #  queue is a two-plan dependency rather than a move — which is why
            #  the state comes from `optimization_disposal` and not from a
            #  column here.
            "preserved_original": is_preserved_original(row),
            "disposal_state": _disposal_state(conn, row),
            "state_label": STATE_LABEL.get(_disposal_state(conn, row), ""),
            "active_version": _active_version(conn, row),
            "preserved_storage": _preserved_storage(conn, row),
            "marked": marked_for_deletion(row["item_relpath"]),
            "size_label": human_size(row["item_size"]),
            #  "3 days ago", not "2026-08-19T12:57:35+00:00". The stored stamp
            #  was printed raw in the row header, which is the one place on the
            #  page a person is scanning rather than reading — and every other
            #  surface in LibrAIry has said it in words for a while.
            "when": human_ago(row["quarantined_at"]),
            # The name is what identifies the row; the path is detail. Both
            # were in one mono blob that wrapped to four lines on a phone.
            "display_name": _basename(row["item_relpath"] or row["original_relpath"]),
            # What the user already decided, and what Commit will do about it.
            "request": requests.get(int(row["id"])),
            # Whether Restore can be offered at all. A control that can only
            # produce an error is worse than no control.
            "restorable": bool(row["original_root"] and row["original_relpath"])
            and not is_preserved_original(row),
            #  `missing_since` is the one definition of "not at that path
            #  right now" — the same predicate `live.py` owns and Search,
            #  Browse and the backup queue all use. Asking `state == 'missing'`
            #  instead was a second answer to the same question, and the
            #  scanner writes the first one.
            "gone": row["item_relpath"] is None
            or row["item_missing_since"] is not None
            or row["item_state"] == "missing",
        }
        for row in rows
    ]


def _disposal_state(conn: sqlite3.Connection, row) -> str:
    """Where a preserved original stands. Empty for every other held file."""
    if not is_preserved_original(row):
        return ""
    return preserved_state(conn, row)


def _active_version(conn: sqlite3.Connection, row) -> str:
    """Which file replaced this one, for a preserved original. Empty otherwise.

    The question a person actually has in front of a preserved original is
    "then what am I listening to now", and the answer is one join away.
    """
    if not is_preserved_original(row):
        return ""
    result = conn.execute(
        "SELECT i.relpath FROM optimization_jobs j JOIN items i ON i.id = j.result_item_id"
        " WHERE j.id=? AND i.missing_since IS NULL",
        (int(row["optimization_job_id"]),),
    ).fetchone()
    return str(result["relpath"]) if result else ""


def _preserved_storage(conn: sqlite3.Connection, row) -> dict[str, str]:
    """What keeping this original costs, and what removing it would do.

    From `optimization_storage` and nowhere else. This is the one card where a
    reader is most likely to be weighing exactly that trade, so the number that
    matters here is `bytes_freed_if_original_removed` — what deleting *this
    file* frees at the moment they delete it — beside the net position
    afterwards. Those differ by more than a factor of two on the worked example,
    which is why neither is ever called "reclaimable".
    """
    if not is_preserved_original(row):
        return {}
    from librairy.optimization_storage import storage_effect

    job = conn.execute(
        "SELECT source_bytes, actual_bytes FROM optimization_jobs WHERE id=?",
        (int(row["optimization_job_id"]),),
    ).fetchone()
    if job is None:
        return {}
    original = int(job["source_bytes"] or 0)
    optimized = int(job["actual_bytes"] or 0)
    if not original or not optimized:
        return {}
    effect = storage_effect(original, optimized, STORAGE_STATE[_disposal_state(conn, row)])
    final = effect.final_net_reduction_bytes
    return {
        "original": human_size(original),
        "active": human_size(optimized),
        "freed_if_removed": human_size(effect.bytes_freed_if_original_removed),
        #  `human_bytes`, not `human_size`: zero is a size. A remux saves
        #  nothing, and `human_size(0)` is the empty string, so the card read
        #  "library ends up  smaller than it started" — no number, and the
        #  wrong word. The direction is carried beside the magnitude for the
        #  same reason: `abs()` alone would call a *larger* result smaller.
        "final_reduction": human_bytes(abs(final)),
        "final_direction": "smaller" if final > 0 else ("larger" if final < 0 else "same"),
        "reclaimed": human_size(effect.reclaimed_now_bytes) or "0 B",
        "stored_now": human_size(effect.physical_bytes_now),
        "extra_now": human_size(effect.current_extra_storage_bytes) or "0 B",
        "note": effect.notes[0] if effect.notes else "",
        #  The one number that may ever be called a saving, and only here.
        "realized": human_size(effect.reclaimed_now_bytes) if effect.reclaimed_now_bytes else "",
    }


def _basename(relpath: object) -> str:
    return str(relpath or "").rstrip("/").rsplit("/", 1)[-1]


def _similar_flags(conn: sqlite3.Connection) -> list[dict[str, object]]:
    rows = list(
        conn.execute(
            """
            SELECT f.*, a.relpath AS item_relpath, b.relpath AS similar_relpath,
                   a.size AS item_size, b.size AS similar_size
            FROM similar_media_flags f
            JOIN items a ON a.id = f.item_id
            JOIN items b ON b.id = f.similar_item_id
            WHERE f.status='review'
            ORDER BY f.id DESC
            """
        )
    )
    return [
        {
            **dict(row),
            "item_size_label": human_size(row["item_size"]),
            "similar_size_label": human_size(row["similar_size"]),
        }
        for row in rows
    ]
