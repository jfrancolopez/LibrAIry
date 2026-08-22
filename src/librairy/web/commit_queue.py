"""What is waiting to be committed, by what it will actually do.

The Commit page could tell you *which files* were waiting. It could not tell
you what kind of change each one was without reading a source path and a
destination path and working it out — which is fine for three and useless for
three hundred, and actively misleading for the one that had just arrived from
Quarantine, because "a file moving from one folder to another" describes a
library correction and a delete-queue move equally well.

So every pending decision now declares its type, and the type comes from what
the plan actually is rather than from where the row happened to be rendered:

    new-file      an approved inbox proposal        inbox     -> library
    correction    an approved audit finding         library   -> library
    restore       a quarantine restore request      quarantine-> wherever it came from
    delete-queue  a quarantine delete request       quarantine-> quarantine/_to-delete
    optimization  an adopted optimized version      two ops: preserve, then admit

Two numbers matter and they are not the same number. A *decision* is one thing
the owner chose; an *operation* is one file being moved. One correction to a
twelve-track album is one decision and twelve operations. The headline counts
decisions, because that is what was agreed to, and the operation count is
available beside it — reporting twelve where somebody made one choice makes a
tidy afternoon look like a catastrophe.

Everything here is bounded. Counts come from SQL over the whole queue; rows
come from one LIMIT-ed query per page. Neither grows with the size of the
database, which is the entire scalability story for this page.
"""

from __future__ import annotations

import sqlite3
from pathlib import PurePosixPath
from typing import Any

from librairy.config import Settings
from librairy.humanize import human_bytes

NEW_FILE = "new-file"
CORRECTION = "correction"
#  A duplicate the owner chose to set aside. It comes from a Library Review
#  finding, exactly like a correction, and it is not one: a correction moves a
#  file to a better place in the library, and this takes a file *out* of the
#  library. Filing it under "Files already in your library, moved or renamed"
#  would be the page telling somebody that a quarantine is a rename.
SET_ASIDE = "set-aside"
RESTORE = "restore"
DELETE_QUEUE = "delete-queue"
OPTIMIZATION = "optimization"

# The order the page reads in: what arrives, what is being tidied, what is
# going back, what you are finished with.
TYPE_ORDER = (NEW_FILE, CORRECTION, SET_ASIDE, OPTIMIZATION, RESTORE, DELETE_QUEUE)

# The heading for a group of them.
TYPE_LABEL = {
    NEW_FILE: "New files",
    CORRECTION: "Library corrections",
    SET_ASIDE: "Set aside",
    RESTORE: "Restores",
    DELETE_QUEUE: "Delete queue",
    OPTIMIZATION: "Optimizations",
}

# The same heading with one thing under it. "1 library corrections" is the kind
# of small wrongness that makes a page feel unfinished.
TYPE_LABEL_ONE = {
    NEW_FILE: "New file",
    CORRECTION: "Library correction",
    SET_ASIDE: "Set aside",
    RESTORE: "Restore",
    DELETE_QUEUE: "Delete queue",
    OPTIMIZATION: "Optimization",
}

# The badge on one row. Says what Commit will do to this file, in one word,
# as text — never colour alone, which is invisible to a colourblind reader and
# to anyone printing the page.
TYPE_BADGE = {
    NEW_FILE: "FILE",
    CORRECTION: "MOVE",
    SET_ASIDE: "SET ASIDE",
    RESTORE: "RESTORE",
    DELETE_QUEUE: "DELETE QUEUE",
    OPTIMIZATION: "OPTIMIZE",
}

# What the group means, under its heading. The delete-queue sentence is the
# most important one on the page.
TYPE_NOTE = {
    NEW_FILE: "New files from your inbox, filed into the library.",
    CORRECTION: "Files already in your library, moved or renamed.",
    SET_ASIDE: "Files leaving, rather than being filed: a copy you already "
    "have, or something you sent here yourself. They go to Quarantine, nothing "
    "is deleted, and they can be restored.",
    RESTORE: "Held files going back where they came from.",
    DELETE_QUEUE: "Moved into one folder for you to empty yourself. "
    "LibrAIry never deletes anything.",
    OPTIMIZATION: "Smaller versions replacing what is in your library. "
    "The original is preserved in Quarantine, not deleted.",
}

# One page of rows, whatever is waiting. The same 50 every other list in
# LibrAIry uses — Review, Quarantine, History, Search and Browse all bound a
# page at fifty rows, and `tests/test_scale.py` pins them together.
PAGE_SIZE = 50

# How many of a group the unfiltered view shows.
#
# Fifty is the bound on *a list*. The All view is not one list, it is one per
# decision kind, so its real bound was fifty times however many kinds happen to
# be populated — a hundred cards with two kinds, and two hundred and fifty with
# all five, at roughly 2 KB each. That is not a checkpoint page, it is a scroll.
#
# So All previews each kind and the filtered view is the working list. The page
# is bounded at `PREVIEW_SIZE * len(TYPE_ORDER)` — fifty again, and this time it
# stays fifty when a sixth kind of decision is invented.
PREVIEW_SIZE = 10


#  What makes a correction plan a set-aside rather than a move.
#
#  Asked of the *plan* and never of one operation, which is the difference
#  between a category and a bug. A merge is a correction that quarantines the
#  copies it displaces, so it holds operations of both shapes — and a CASE that
#  read them one at a time counted one merge as two decisions in two groups,
#  with its headline saying so. A correction is a set-aside when it has no
#  library-to-library move in it at all: nothing is being rearranged, something
#  is leaving.
_SET_ASIDE_CASE = """
              WHEN (p.audit_finding_id IS NOT NULL OR p.coherent=1) AND NOT EXISTS (
                SELECT 1 FROM plan_ops m WHERE m.plan_id = p.id
                  AND m.op_type='move' AND m.dest_root='library'
              ) THEN 'set-aside'
"""

#  The same rule as a WHERE fragment, for one type's page of rows.
_HAS_LIBRARY_MOVE = (
    "EXISTS (SELECT 1 FROM plan_ops m WHERE m.plan_id = p.id"
    " AND m.op_type='move' AND m.dest_root='library')"
)


def queue_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    """Decisions, operations and bytes per type — counted in SQL.

    Four aggregate queries over indexed columns, and no row objects built in
    Python. This is what stays honest when the queue is large: `SELECT
    COUNT(*)` does not care how many rows it counted.
    """
    #  Split by where the file is going and not by where it came from. An
    #  approved inbox proposal whose destination is quarantine is not a new file
    #  being filed into the library, and counting it under a heading that says
    #  so was the page describing an arrival being set aside as an arrival being
    #  kept.
    inbox = {
        row["kind"]: row
        for row in conn.execute(
            """
            SELECT CASE WHEN p.dest_root='quarantine' THEN 'set-aside'
                        ELSE 'new-file' END AS kind,
                   COUNT(*) AS decisions, COALESCE(SUM(i.size), 0) AS bytes
            FROM proposals p JOIN items i ON i.id = p.item_id
            WHERE p.status='approved' AND p.dest_relpath IS NOT NULL
              AND i.missing_since IS NULL
            GROUP BY kind
            """
        )
    }
    plans = conn.execute(
        f"""
        SELECT kind, COUNT(DISTINCT plan_id) AS decisions, COUNT(*) AS operations,
               COALESCE(SUM(bytes), 0) AS bytes
        FROM (
          SELECT p.id AS plan_id, o.id AS op_id, i.size AS bytes,
            CASE
              WHEN p.optimization_job_id IS NOT NULL THEN 'optimization'
              {_SET_ASIDE_CASE}
              WHEN p.audit_finding_id IS NOT NULL OR p.coherent=1 THEN 'correction'
              WHEN o.dest_root='quarantine' AND o.dest_relpath LIKE '_to-delete/%'
                THEN 'delete-queue'
              ELSE 'restore'
            END AS kind
          FROM plans p
          JOIN plan_ops o ON o.plan_id = p.id
          LEFT JOIN items i ON i.id = o.item_id
          WHERE p.status IN ('approved','executing')
            AND (p.audit_finding_id IS NOT NULL OR p.quarantine_entry_id IS NOT NULL
                 OR p.optimization_job_id IS NOT NULL OR p.coherent=1)
        )
        GROUP BY kind
        """  # noqa: S608 - one interpolated constant, no user input
    ).fetchall()

    types: dict[str, dict[str, int]] = {}
    arrivals: dict[str, int] = {}
    for kind, row in inbox.items():
        types[kind] = {
            "decisions": int(row["decisions"]),
            # One inbox proposal is one file: decision and operation coincide.
            "operations": int(row["decisions"]),
            "bytes": int(row["bytes"]),
        }
        #  How many of this group's decisions are approved inbox proposals, and
        #  so part of the one batch plan. The group action bar is drawn from
        #  this rather than from the group's own total, because `Set aside`
        #  holds both arrivals and library copies and only the arrivals commit
        #  as a batch.
        arrivals[kind] = int(row["decisions"])
    for row in plans:
        #  Both halves of the set-aside group are real at once: an arrival that
        #  is already in the library, and a library copy chosen over another.
        totals = types.setdefault(
            row["kind"], {"decisions": 0, "operations": 0, "bytes": 0}
        )
        totals["decisions"] += int(row["decisions"])
        totals["operations"] += int(row["operations"])
        totals["bytes"] += int(row["bytes"])
    groups = [
        {
            "type": key,
            "label": TYPE_LABEL[key],
            "label_one": TYPE_LABEL_ONE[key],
            "note": TYPE_NOTE[key],
            "badge": TYPE_BADGE[key],
            "arrivals": arrivals.get(key, 0),
            **types.get(key, {"decisions": 0, "operations": 0, "bytes": 0}),
        }
        for key in TYPE_ORDER
    ]
    for group in groups:
        #  Said per group as well as in total, because the New files group is
        #  the one whose decisions commit as a batch and so needs its own
        #  headline figure beside the button that commits them.
        group["size"] = human_bytes(group["bytes"])
    total_decisions = sum(group["decisions"] for group in groups)
    total_bytes = sum(group["bytes"] for group in groups)
    return {
        "groups": [group for group in groups if group["decisions"]],
        "all_groups": groups,
        "decisions": total_decisions,
        "operations": sum(group["operations"] for group in groups),
        "bytes": total_bytes,
        "size": human_bytes(total_bytes),
    }


def queue_rows(
    conn: sqlite3.Connection,
    settings: Settings | None,
    *,
    kind: str,
    page: int = 1,
    page_size: int = PAGE_SIZE,
) -> list[dict[str, Any]]:
    """One bounded page of pending decisions of one type.

    Every row carries the same five things — what it is, what it is called,
    where it is now, where it will be, and how big it is — because one shape
    renders in one component, and four bespoke row layouts are four places for
    the vocabulary to drift.
    """
    offset = max(0, (max(1, page) - 1) * page_size)
    if kind == NEW_FILE:
        return _inbox_rows(conn, kind, page_size, offset)
    if kind == SET_ASIDE:
        #  The one group with two sources. An arrival being set aside is an
        #  approved proposal; a library copy being set aside is an approved
        #  plan. They are the same decision seen from the two places a file can
        #  be leaving from, and one heading is the honest number.
        rows = _inbox_rows(conn, kind, page_size, offset)
        return [*rows, *_plan_rows(conn, settings, kind, page_size - len(rows), offset)]
    return _plan_rows(conn, settings, kind, page_size, offset)


def _inbox_rows(
    conn: sqlite3.Connection, kind: str, limit: int, offset: int
) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    leaving = "=" if kind == SET_ASIDE else "!="
    rows = conn.execute(
        f"""
        SELECT p.id, p.dest_root, p.dest_relpath, p.item_id,
               i.relpath AS src_relpath, i.size
        FROM proposals p JOIN items i ON i.id = p.item_id
        WHERE p.status='approved' AND p.dest_relpath IS NOT NULL
          AND i.missing_since IS NULL AND p.dest_root {leaving} 'quarantine'
        ORDER BY p.id
        LIMIT ? OFFSET ?
        """,  # noqa: S608 - one operator from a two-way branch
        (limit, offset),
    ).fetchall()
    return [
        {
            "type": kind,
            "badge": TYPE_BADGE[kind],
            "subject": PurePosixPath(row["src_relpath"]).name,
            "current": f"inbox/{row['src_relpath']}",
            "after": f"{row['dest_root']}/{row['dest_relpath']}",
            "size": human_bytes(row["size"]),
            "reason": _inbox_reason(conn, kind, row),
            "op_count": 1,
            "plan_id": "",
            "back_url": "/commit/unapprove",
            "back_label": "Send back to Review",
            #  Scoped to this row. Without it the control on one card posted the
            #  page-wide withdrawal: "send this one back" sent all of them back,
            #  and the card carried no label at all to warn anybody.
            "back_id": row["id"],
            "item_id": row["item_id"],
        }
        for row in rows
    ]


def _inbox_reason(conn: sqlite3.Connection, kind: str, row: sqlite3.Row) -> str:
    """Why this arrival is here, in the words its own row used.

    "You approved this in Review" is true of a file being filed and unhelpful
    about one being set aside — what a person wants to see there is the file it
    is a copy of, which is the whole reason they pressed the button.
    """
    from librairy.inbox_duplicates import describe

    if kind != SET_ASIDE:
        return "You approved this in Review."
    described = describe(conn, int(row["item_id"]))
    if described and described["match"]:
        return f"Identical to {described['match']}, which you already have."
    compared = _compared_with(conn, int(row["item_id"]))
    if compared:
        #  Not the same sentence as an identical copy, and not the vague one
        #  either: this file was measured against a version already filed and
        #  that version is the reason it is leaving. Saying "you sent it here"
        #  loses the comparison the person actually made.
        return f"You kept {compared} after comparing the two. Nothing is deleted."
    return "You sent this to Quarantine from Review."


def _compared_with(conn: sqlite3.Connection, item_id: int) -> str:
    """The filed version this arrival was compared with, by name."""
    from librairy.arrival_comparison import compared_with

    twin = compared_with(conn, item_id)
    if twin is None:
        return ""
    row = conn.execute("SELECT relpath FROM items WHERE id=?", (twin,)).fetchone()
    return PurePosixPath(str(row["relpath"])).name if row else ""


def _plan_rows(
    conn: sqlite3.Connection,
    settings: Settings | None,
    kind: str,
    limit: int,
    offset: int,
) -> list[dict[str, Any]]:
    where = {
        CORRECTION: (
            f"(p.audit_finding_id IS NOT NULL OR p.coherent=1) AND {_HAS_LIBRARY_MOVE}"
        ),
        SET_ASIDE: (
            f"(p.audit_finding_id IS NOT NULL OR p.coherent=1) AND NOT {_HAS_LIBRARY_MOVE}"
        ),
        #  `coherent=0` on both: a plan about a held file that has to run as a
        #  unit is a replacement, and it is counted and listed as a correction.
        #  Without this it appeared twice — once truthfully, and once under
        #  `Restores`, which is the one word it must not be called.
        DELETE_QUEUE: (
            "p.quarantine_entry_id IS NOT NULL AND p.coherent=0"
            " AND o.dest_root='quarantine' AND o.dest_relpath LIKE '_to-delete/%'"
        ),
        RESTORE: (
            "p.quarantine_entry_id IS NOT NULL AND p.coherent=0"
            " AND NOT (o.dest_root='quarantine' AND o.dest_relpath LIKE '_to-delete/%')"
        ),
        OPTIMIZATION: "p.optimization_job_id IS NOT NULL",
    }[kind]
    rows = conn.execute(
        f"""
        SELECT p.id AS plan_id, p.status, p.approved_at, p.coherent,
               p.audit_finding_id, p.quarantine_entry_id, p.optimization_job_id,
               o.src_root, o.src_relpath, o.dest_root, o.dest_relpath, o.item_id,
               (SELECT COUNT(*) FROM plan_ops WHERE plan_id=p.id) AS op_count,
               (SELECT COALESCE(SUM(i2.size), 0) FROM plan_ops o2
                  LEFT JOIN items i2 ON i2.id = o2.item_id
                 WHERE o2.plan_id = p.id) AS bytes
        FROM plans p
        JOIN plan_ops o ON o.id = (
          SELECT id FROM plan_ops WHERE plan_id = p.id ORDER BY seq LIMIT 1
        )
        WHERE p.status IN ('approved','executing') AND {where}
        ORDER BY p.approved_at, p.id
        LIMIT ? OFFSET ?
        """,  # noqa: S608 — `where` comes from this function's own dict
        (limit, offset),
    ).fetchall()
    return [_plan_row(conn, settings, row, kind) for row in rows]


def _plan_row(
    conn: sqlite3.Connection,
    settings: Settings | None,
    row: sqlite3.Row,
    kind: str,
) -> dict[str, Any]:
    subject = PurePosixPath(row["src_relpath"]).name or row["src_relpath"]
    #  A folder rename is fourteen operations and one decision, and the plan's
    #  first operation is a file. Titling the card `01 - Funkytown.flac` and
    #  showing that file's before and after describes one fourteenth of what
    #  pressing Commit will do — a person reading it would not know a folder was
    #  being renamed at all. The finding is the decision, so it is what the card
    #  names.
    folder = _folder_subject(conn, row, kind)
    reason = _reason(conn, row, kind)
    stale = ""
    if settings is not None and kind in (CORRECTION, SET_ASIDE):
        from librairy.correction_state import plan_drift

        stale = plan_drift(conn, settings, row["plan_id"])
    extra = (
        _optimization_fields(conn, row)
        if kind == OPTIMIZATION
        else {"optimization": None}
    )
    if kind == DELETE_QUEUE:
        extra = {**extra, "preserved": _preserved_original_fields(conn, row)}
    return {
        **extra,
        "type": kind,
        "badge": TYPE_BADGE[kind],
        "subject": folder["subject"] if folder else subject,
        #  Whether this card is about a file. A folder rename and a folder
        #  merge are not, and the extension badge beside the title has nothing
        #  to explain about a directory.
        "is_file": folder is None or bool(folder.get("is_file")),
        #  "After Commit" for everything but a merge, which goes *into* a folder
        #  that is already there.
        "after_label": (folder or {}).get("verb", ""),
        #  For an optimization the first operation preserves the original, so
        #  its destination is the quarantine path rather than what the library
        #  will hold. `_optimization_fields` supplies the real After.
        "current": folder["current"] if folder else f"{row['src_root']}/{row['src_relpath']}",
        "after": (
            folder["after"]
            if folder
            else (
                f"library/{extra['optimization']['target_relpath']}"
                if kind == OPTIMIZATION
                else f"{row['dest_root']}/{row['dest_relpath']}"
            )
        ),
        "size": human_bytes(row["bytes"]),
        "reason": reason,
        "op_count": int(row["op_count"]),
        "plan_id": row["plan_id"],
        "applying": row["status"] == "executing",
        "stale": bool(stale),
        "finding_id": row["audit_finding_id"],
        "entry_id": row["quarantine_entry_id"],
        # Where "send this back" goes for this kind of decision. One row
        # component, one attribute, rather than a chain of ifs in the template.
        "back_url": (
            (
                f"/review/audit/{row['audit_finding_id']}/unapprove"
                if row["audit_finding_id"]
                #  A replacement decided on the Quarantine page is recalled
                #  there. Sending it through the comparison withdrawal would
                #  turn a held file back into an inbox Review row, which is a
                #  place it is not and cannot be filed from.
                else (
                    f"/quarantine/cancel/{row['quarantine_entry_id']}"
                    if row["quarantine_entry_id"]
                    else f"/commit/withdraw/{row['plan_id']}"
                )
            )
            if kind in (CORRECTION, SET_ASIDE)
            else (
                #  The same withdrawal the optimization page's Cancel request
                #  calls. One implementation, one `plan_withdrawals` record.
                f"/maintenance/optimization/{row['optimization_job_id']}/send-back"
                if kind == OPTIMIZATION
                else f"/quarantine/cancel/{row['quarantine_entry_id']}"
            )
        ),
        # A stale approval is not sent back to be reconsidered — it can no
        # longer run at all, so the honest offer is to remove it.
        "back_label": (
            "Remove old approval"
            if stale
            else (
                "Cancel request"
                if kind in (CORRECTION, SET_ASIDE) and row["quarantine_entry_id"]
                else "Send back to Review"
                if kind in (CORRECTION, SET_ASIDE)
                #  The optimization card posts to the same handler the
                #  optimization page's `Cancel request` posts to, and said so
                #  in a comment while wearing a different label. The same act
                #  seen from two pages gets the same words.
                else "Cancel request"
            )
        ),
        # Every file this decision touches, on demand. A correction to an album
        # is one decision and twelve moves, and "twelve files" is a number
        # until you can see which twelve.
        "files": _files(conn, row["plan_id"]) if int(row["op_count"]) > 1 else [],
        #  So the card can show you the thing rather than its path. Resolved
        #  from the plan's first operation, which is the file the card is
        #  headed by; a decision over several files says so under Files and the
        #  preview follows the one being named.
        "item_id": row["item_id"],
    }


def _optimization_fields(
    conn: sqlite3.Connection, row: sqlite3.Row
) -> dict[str, Any]:
    """What an OPTIMIZE card needs that no other kind does.

    Three things, and the third is the one that is easy to get wrong. An HEVC
    re-encode of an MP4 produces an MP4 in the same folder, so `Current` and
    `After Commit` are the *same path* — rendering only those two makes the card
    say that nothing changes. The codec change is what changed, so it is shown
    beside the paths rather than instead of them.

    Storage comes from `optimization_storage` and is computed nowhere else. The
    card carries the two lines a person needs at the moment of deciding; the
    rest of the accounting is under Details.
    """
    from librairy.optimization import CLASS_LABEL
    from librairy.optimization_storage import READY, storage_effect

    job = conn.execute(
        "SELECT * FROM optimization_jobs WHERE id=?", (row["optimization_job_id"],)
    ).fetchone()
    if job is None:  # pragma: no cover - the FK makes this unreachable
        return {"optimization": None}
    adopt = conn.execute(
        "SELECT dest_relpath FROM plan_ops WHERE plan_id=? AND src_root='optimization'",
        (row["plan_id"],),
    ).fetchone()
    target = adopt["dest_relpath"] if adopt is not None else job["relpath"]
    source_bytes = int(job["source_bytes"] or 0)
    optimized_bytes = int(job["actual_bytes"] or 0)
    effect = (
        storage_effect(source_bytes, optimized_bytes, READY)
        if source_bytes and optimized_bytes
        else None
    )
    return {
        "optimization": {
            "job_id": int(job["id"]),
            "target_relpath": target,
            "preserved_relpath": row["dest_relpath"],
            "quality": job["quality"],
            "quality_label": CLASS_LABEL.get(job["quality"], job["quality"].upper()),
            "change": f"{job['from_label']} → {job['to_label']}",
            #  True when the filename does not change, which is exactly when the
            #  paths alone would be misleading.
            "same_path": target == row["src_relpath"],
            "original_label": human_bytes(source_bytes),
            "optimized_label": human_bytes(optimized_bytes),
            "reduction_label": (
                human_bytes(abs(effect.representation_reduction_bytes))
                if effect
                else ""
            ),
            "reduction_negative": bool(
                effect and effect.representation_reduction_bytes < 0
            ),
            #  Zero, and shown anyway. Adoption frees nothing: both copies are
            #  on the disk until somebody removes the preserved original.
            "reclaimed_label": human_bytes(effect.reclaimed_now_bytes) if effect else "0 B",
            "extra_label": (
                human_bytes(effect.current_extra_storage_bytes) if effect else ""
            ),
            "freed_if_removed_label": (
                human_bytes(effect.bytes_freed_if_original_removed) if effect else ""
            ),
            "final_reduction_label": (
                human_bytes(abs(effect.final_net_reduction_bytes)) if effect else ""
            ),
            #  A remux re-encodes nothing, so it saves nothing and is offered
            #  for compatibility. Counting it as storage reduction would be the
            #  same dishonesty as calling it an optimization.
            "compatibility_only": job["quality"] == "remux",
        }
    }


def _preserved_original_fields(
    conn: sqlite3.Connection, row: sqlite3.Row
) -> dict[str, Any] | None:
    """Whether this delete-queue move is disposing of an optimization original.

    It matters on this card more than anywhere else. Every other row in this
    group is a file the owner rejected; this one is a file they *kept* on
    purpose, and the version that replaced it is still in the library. Reading
    "concert.wav → delete queue" without that context is how somebody talks
    themselves into thinking they are about to lose a recording.

    Nothing new is stored to answer this. The plan names the quarantine entry,
    and the entry has named its optimization job since adoption wrote it.
    """
    from librairy.quarantine import is_preserved_original

    if not row["quarantine_entry_id"]:
        return None
    entry = conn.execute(
        "SELECT * FROM quarantine_entries WHERE id=?", (row["quarantine_entry_id"],)
    ).fetchone()
    if entry is None or not is_preserved_original(entry):
        return None
    active = conn.execute(
        "SELECT i.relpath FROM optimization_jobs j JOIN items i ON i.id = j.result_item_id"
        " WHERE j.id=? AND i.missing_since IS NULL",
        (int(entry["optimization_job_id"]),),
    ).fetchone()
    return {
        "job_id": int(entry["optimization_job_id"]),
        "active_relpath": str(active["relpath"]) if active else "",
    }


#  What the encoder's workspace is called when a person reads about it. The real
#  path is inside the container and would not help anybody find anything, so the
#  journal keeps it — Undo needs it — and the page says where it is in words.
INTERNAL_LABEL = "LibrAIry's optimization workspace"


def _files(conn: sqlite3.Connection, plan_id: str) -> list[dict[str, str]]:
    return [
        {
            "role": op["role"],
            "src": (
                INTERNAL_LABEL
                if op["src_root"] == "optimization"
                else op["src_relpath"]
            ),
            "dest": op["dest_relpath"],
            "internal": op["src_root"] == "optimization",
        }
        for op in conn.execute(
            "SELECT role, src_root, src_relpath, dest_relpath FROM plan_ops"
            " WHERE plan_id=? ORDER BY seq",
            (plan_id,),
        )
    ]


def _folder_subject(
    conn: sqlite3.Connection, row: sqlite3.Row, kind: str
) -> dict[str, str] | None:
    """The folder a subtree correction renames, or None for every other plan.

    Read from the finding rather than from the plan's operations: the finding is
    what somebody approved, and the operations are how it is carried out.
    """
    from librairy.destination_choice import (
        DESTINATION_KINDS,
        candidates,
        chosen,
        subject,
    )
    from librairy.merge import MERGE_KINDS
    from librairy.subtree import SUBTREE_KINDS
    from librairy.track_filing import KIND as FILING_KIND

    if kind not in (CORRECTION, SET_ASIDE):
        return None
    if not row["audit_finding_id"]:
        return _renaming_subject(conn, row) or _arrival_subject(conn, row)
    if row["coherent"]:
        #  A comparison answered by *replacement* rather than by setting one
        #  aside. Both shapes come from the same finding, and reading this one
        #  as a set-aside heads the card with the version that is leaving and
        #  an After of `quarantine/…` — the wrong half of the decision, at the
        #  last moment before bytes move.
        replaced = _arrival_subject(conn, row)
        if replaced is not None:
            return replaced
    found = conn.execute(
        "SELECT id, kind, relpath, dest_relpath, evidence FROM audit_findings WHERE id=?",
        (row["audit_finding_id"],),
    ).fetchone()
    if found is None:
        return None
    if found["kind"] == FILING_KIND:
        #  Named after the artist, because "File loose tracks / Queen" is what
        #  was approved and no single folder describes it: the tracks are going
        #  to two or three different albums, and some of them are not going
        #  anywhere at all.
        #
        #  The After has to be the albums and not the artist folder. Printing
        #  the artist folder on both lines made the card read "Music/Rock/Queen
        #  → Music/Rock/Queen", which is a decision that appears to do nothing.
        albums = _filing_albums(conn, row["plan_id"])
        return {
            "subject": "File loose tracks",
            "current": f"library/{found['relpath']}",
            "after": (
                f"library/{albums[0]}"
                if len(albums) == 1
                else f"{len(albums)} albums under library/{found['relpath']}"
            ),
            "verb": "Into",
        }
    if found["kind"] in DESTINATION_KINDS:
        #  A destination choice has no `dest_relpath` of its own — that is the
        #  whole point of it — so the folder it is going into is the one the
        #  person picked. Named after the artist rather than after a folder,
        #  because "Bring Prince together" is what was approved and
        #  `Music/Pop/Prince` is only where.
        destination = chosen(conn, int(found["id"]))
        if not destination:
            return None
        #  The folders that move, not the folder the finding is anchored at.
        #  The anchor is one album somewhere in the split, and after the choice
        #  it is quite often inside the *destination* — so printing it as
        #  "Current" would show a path that is not going anywhere.
        moving = [c.relpath for c in candidates(conn, found) if c.relpath != destination]
        return {
            "subject": f"Bring {subject(found)} together",
            "current": (
                f"library/{moving[0]}"
                if len(moving) == 1
                else f"{len(moving)} folders under library/"
            ),
            "after": f"library/{destination}",
            "verb": "Into",
        }
    if not found["dest_relpath"]:
        return None
    if found["kind"] in MERGE_KINDS:
        #  A merge is one decision however many operations carry it out — 76 is
        #  not 76 cards. `Into` rather than `After Commit`, because the folder
        #  it goes into is somewhere that already exists and probably already
        #  has files in it, which "after commit" would not suggest.
        return {
            "subject": "Merge folders",
            "current": f"library/{found['relpath']}",
            "after": f"library/{found['dest_relpath']}",
            "verb": "Into",
        }
    if found["kind"] not in SUBTREE_KINDS:
        return None
    return {
        "subject": PurePosixPath(found["relpath"]).name or found["relpath"],
        "current": f"library/{found['relpath']}",
        "after": f"library/{found['dest_relpath']}",
    }


def _kept_representations(
    conn: sqlite3.Connection, row: sqlite3.Row, found: sqlite3.Row
) -> str:
    """The representations a similar-media comparison left in the library."""
    from librairy.similar_media import KIND, kept_members

    if found["kind"] != KIND:
        return ""
    ops = conn.execute(
        "SELECT src_root, src_relpath FROM plan_ops WHERE plan_id=?", (row["plan_id"],)
    ).fetchall()
    names = [PurePosixPath(name).name for name in kept_members(conn, row["plan_id"], ops)]
    return ", ".join(names)


def _filing_left_behind(conn: sqlite3.Connection, row: sqlite3.Row) -> str:
    """What a filing decision leaves alone, which the summary cannot say.

    The finding's own sentence counts the loose tracks it found. The decision
    is about what happens to each of them, and "3 found" beside a card that
    moves one of them is the audit talking over the answer.
    """
    from librairy.destination_choice import answers
    from librairy.track_filing import KIND as FILING_KIND

    found = conn.execute(
        "SELECT kind FROM audit_findings WHERE id=?", (row["audit_finding_id"],)
    ).fetchone()
    if found is None or found["kind"] != FILING_KIND:
        return ""
    given = answers(conn, int(row["audit_finding_id"]))
    moving = sum(1 for destination in given.values() if destination)
    staying = sum(1 for destination in given.values() if destination is None)
    if not moving:
        return ""
    tracks = f"{moving} track{'' if moving == 1 else 's'}"
    if not staying:
        return f"You chose where {tracks} should go."
    return (
        f"You chose where {tracks} should go. {staying} "
        f"{'is' if staying == 1 else 'are'} staying where {'it' if staying == 1 else 'they'} "
        f"{'is' if staying == 1 else 'are'}."
    )


def _filing_albums(conn: sqlite3.Connection, plan_id: str) -> list[str]:
    """The album folders this filing decision actually puts tracks into."""
    return sorted(
        {
            str(PurePosixPath(str(op["dest_relpath"])).parent)
            for op in conn.execute(
                "SELECT dest_relpath FROM plan_ops WHERE plan_id=? AND op_type='move'"
                " AND dest_root='library'",
                (plan_id,),
            )
        }
    )


def _comparison_reason(conn: sqlite3.Connection, row: sqlite3.Row) -> str:
    """Why an arriving representation is waiting, and what it will preserve."""
    preserved = conn.execute(
        "SELECT src_relpath FROM plan_ops WHERE plan_id=? AND op_type='quarantine'"
        " AND src_root='library' ORDER BY seq LIMIT 1",
        (row["plan_id"],),
    ).fetchone()
    if preserved is None:
        return "You chose this version after comparing it with the one you have."
    return (
        f"You chose this version. {PurePosixPath(str(preserved['src_relpath'])).name} "
        f"goes to Quarantine first — nothing is overwritten and nothing is deleted."
    )


def _renaming_subject(
    conn: sqlite3.Connection, row: sqlite3.Row
) -> dict[str, str] | None:
    """A folder whose filenames are being tidied, headed by the folder.

    Read off the plan's own shape rather than a stored kind: every operation is
    a library-to-library move that changes the filename and not the folder, and
    nothing else in LibrAIry produces that. Titling this card with its first
    file would describe one ninth of what pressing Commit does, which is the
    same defect a folder rename had.
    """
    ops = conn.execute(
        "SELECT op_type, src_root, src_relpath, dest_root, dest_relpath FROM plan_ops"
        " WHERE plan_id=?",
        (row["plan_id"],),
    ).fetchall()
    if not ops:
        return None
    folders = set()
    for op in ops:
        if op["op_type"] != "move" or op["src_root"] != "library":
            return None
        if op["dest_root"] != "library":
            return None
        source, destination = (
            PurePosixPath(str(op["src_relpath"])),
            PurePosixPath(str(op["dest_relpath"])),
        )
        if source.parent != destination.parent or source.name == destination.name:
            return None
        folders.add(str(source.parent))
    if len(folders) != 1:
        return None
    folder = folders.pop()
    return {
        "subject": "Tidy filenames",
        "current": f"library/{folder}",
        "after": (
            f"{len(ops)} filename{'s' if len(ops) != 1 else ''} under library/{folder}"
        ),
        "verb": "Changing",
    }


def _arrival_subject(
    conn: sqlite3.Connection, row: sqlite3.Row
) -> dict[str, str] | None:
    """A comparison one representation won, headed by that representation.

    The plan's first operation preserves the copy being replaced, so reading
    the card off it would head "you chose the FLAC" with the name of the MP3
    that is leaving. The move is the decision; the quarantine is how the
    decision is carried out without losing anything.

    Every direction. The version taking the slot may be arriving from the
    inbox, may be one set aside earlier and brought back in the filed copy's
    place, or may be another copy already filed somewhere else in the library —
    the same decision, made on three different pages, and the card says the
    same true thing about all of them.
    """
    #  The operation that puts something *into* the library, wherever it came
    #  from. That is the decision; the quarantine before it is how the decision
    #  is carried out without losing anything.
    move = conn.execute(
        "SELECT src_root, src_relpath, dest_relpath FROM plan_ops"
        " WHERE plan_id=? AND op_type='move' AND dest_root='library'"
        " ORDER BY seq LIMIT 1",
        (row["plan_id"],),
    ).fetchone()
    if move is None:
        return None
    return {
        "subject": PurePosixPath(str(move["src_relpath"])).name,
        "current": f"{move['src_root']}/{move['src_relpath']}",
        "after": f"library/{move['dest_relpath']}",
        "verb": "",
        "is_file": "yes",
    }


def _reason(conn: sqlite3.Connection, row: sqlite3.Row, kind: str) -> str:
    """Why this is waiting, in the words of the decision that made it."""
    if kind in (CORRECTION, SET_ASIDE) and row["coherent"] and row["audit_finding_id"]:
        #  Same reason as the subject above: this is a replacement, so the
        #  sentence is about what takes the slot and what is preserved, not
        #  about what was kept out of a set.
        return _comparison_reason(conn, row)
    if kind in (CORRECTION, SET_ASIDE) and not row["audit_finding_id"] and row["plan_id"]:
        if _renaming_subject(conn, row) is not None:
            return (
                "You asked for these filenames to match the current Music naming. "
                "Only the names change."
            )
        return _comparison_reason(conn, row)
    if kind == CORRECTION and row["audit_finding_id"]:
        staying = _filing_left_behind(conn, row)
        if staying:
            return staying
    if kind in (CORRECTION, SET_ASIDE) and row["audit_finding_id"]:
        found = conn.execute(
            "SELECT kind, summary FROM audit_findings WHERE id=?",
            (row["audit_finding_id"],),
        ).fetchone()
        if found is None:
            return "Approved in Library Review."
        kept = _kept_representations(conn, row, found)
        if kept:
            #  The finding's summary says what was found; on this page the
            #  useful sentence is what survives it. A card headed by the file
            #  that is leaving has to name the one that is staying, or "set
            #  aside 2 of 3" is a count with no reassurance in it.
            return f"You kept {kept}. Nothing is deleted."
        return found["summary"]
    if kind == DELETE_QUEUE:
        return "You chose Delete queue. Nothing is deleted."
    if kind == RESTORE and row["quarantine_entry_id"]:
        return "You asked for this to go back."
    if kind == OPTIMIZATION:
        return "You chose the optimized version. The original is preserved."
    return "You asked for this to go back."
