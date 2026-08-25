from __future__ import annotations

import sqlite3
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta

from librairy.config import Settings
from librairy.history import HISTORY_KINDS, kind_counts, list_history, undo_op, undo_plan

#  What an adoption's reversal is called and whether it can happen at all,
#  derived from where the preserved original is right now rather than from the
#  fact that a plan once ran. A journal row is permanent; the ability to undo it
#  is not, and rendering a button that can only fail is the thing this replaces.
#  What a reversal did, said to a person. The stored outcome is a code, and
#  two of them carry diagnostics: `undo_refused_changed expected=<hash>
#  actual=<hash>` was rendered verbatim on the Undone page, two full BLAKE2b
#  digests wide, beside a bare journal id and the word "ok".
UNDO_OUTCOMES = {
    "ok": "put back",
    "undo_refused_missing": "not put back — the file is no longer where LibrAIry left it",
    "undo_refused_occupied": "not put back — something is already at its old path",
    "undo_refused_changed": "not put back — the file has been edited since",
    "undo_refused_source": "not put back — the file could not be read safely",
}


def undo_outcome_text(outcome: str) -> str:
    """One sentence for a reversal's outcome, whatever diagnostics follow it."""
    code = str(outcome or "").split(" ", 1)[0]
    return UNDO_OUTCOMES.get(code, "not put back")


def _adoption_undo(conn: sqlite3.Connection, plan_id: object) -> dict[str, object]:
    from librairy.optimization_disposal import (
        IN_DELETE_QUEUE,
        REMOVED,
        RESTORED,
        WAITING,
        preserved_state,
    )

    if not plan_id:
        return {}
    entry = conn.execute(
        "SELECT * FROM quarantine_entries"
        " WHERE plan_id=? AND optimization_job_id IS NOT NULL"
        " ORDER BY id DESC LIMIT 1",
        (plan_id,),
    ).fetchone()
    if entry is None:
        return {}
    state = preserved_state(conn, entry)
    if state == REMOVED:
        return {
            "available": False,
            "note": "Original removed from storage. This optimization can no "
            "longer restore the original automatically.",
        }
    if state == RESTORED:
        return {"available": False, "note": "The original is already back in the library."}
    if state == WAITING:
        return {
            "available": False,
            "note": "A decision about the original is waiting for Commit.",
        }
    return {
        "available": True,
        "label": "Restore original",
        "url": f"/quarantine/restore-original/{int(entry['id'])}",
        "note": (
            "The original is in the delete queue. This takes it back out first."
            if state == IN_DELETE_QUEUE
            else ""
        ),
    }


def _plan_undo(
    conn: sqlite3.Connection, plan_id: object, *, adoption: bool
) -> dict[str, object]:
    """Whether this plan can still be reversed. Empty means "ask the usual way".

    Index-only, deliberately. Answering it in general would mean hashing every
    file on the page, and History is a page somebody scrolls. The two cases that
    can be answered from rows already in the database are the ones where the
    answer is most often no: an adoption whose preserved original has been
    removed, and a quarantine decision whose file is no longer there.
    """
    if adoption:
        return _adoption_undo(conn, plan_id)
    row = conn.execute(
        """
        SELECT i.id AS item_id, i.missing_since
        FROM plans p JOIN quarantine_entries qe ON qe.id = p.quarantine_entry_id
        LEFT JOIN items i ON i.id = qe.item_id
        WHERE p.id = ?
        """,
        (plan_id,),
    ).fetchone()
    if row is None:
        return {}
    if row["item_id"] is None or row["missing_since"] is not None:
        return {
            "available": False,
            "note": "That file is no longer on disk, so this cannot be put back.",
        }
    return {}


def history_data(
    conn: sqlite3.Connection,
    limit: int = 50,
    query: str = "",
    kind: str = "all",
    page: int = 1,
) -> dict[str, object]:
    if kind not in HISTORY_KINDS:
        kind = "all"
    page = max(1, page)
    offset = (page - 1) * limit
    # One extra row, only to answer "is there another page" without a second
    # COUNT over a table that only grows.
    fetched = [
        _augment(dict(row))
        for row in list_history(
            conn, limit=limit + 1, query=query, kind=kind, offset=offset
        )
    ]
    has_next = len(fetched) > limit
    rows = fetched[:limit]
    # A settings change is journalled for audit, but it is not a move: it has
    # no plan, nothing to undo here, and no source and destination worth the
    # arrow between them. Eighteen of them under a heading reading "Plan None ·
    # 18 file(s) · Undo plan" pushed every real move off the page.
    entries = [row for row in rows if row.get("action") != "settings_change"]
    changes = [row for row in rows if row.get("action") == "settings_change"]
    plans = {row["id"]: row for row in _plans(conn)}
    counts = kind_counts(conn, query)
    return {
        "entries": entries,
        "settings_changes": changes,
        "plans": list(plans.values()),
        "timeline": _timeline(entries, plans),
        "days": _days(conn, entries, plans),
        "query": query,
        "kind": kind,
        "kinds": [
            {"key": key, "label": label, "count": counts.get(key, 0)}
            for key, (label, _) in HISTORY_KINDS.items()
        ],
        "page": page,
        "has_next": has_next,
        "has_prev": page > 1,
        "showing": len(rows),
        "matching": counts.get(kind, 0),
        "total": _journal_size(conn),
    }


def _days(
    conn: sqlite3.Connection, entries: list[dict[str, object]], plans: dict
) -> list[dict[str, object]]:
    """Plans, bucketed by the day they ran.

    Presentation only — no journal row is merged, dropped or rewritten. A day
    heading and a one-line plan summary are what turn 145 identical rows into
    "yesterday you filed 12 files", and both are computed here at render time
    from the rows themselves.
    """
    days: list[dict[str, object]] = []
    by_day: dict[str, dict[str, object]] = {}
    for group in _timeline(entries, plans):
        day = str(group.get("ts") or "")[:10]
        bucket = by_day.get(day)
        if bucket is None:
            bucket = {"day": day, "label": _day_label(day), "plans": [], "files": 0}
            by_day[day] = bucket
            days.append(bucket)
        group["time"] = str(group.get("ts") or "")[11:16]
        group["summary"] = _plan_summary(
            group["entries"], _restored_decision(conn, group["plan_id"])
        )
        group["correction"] = _is_correction(group["entries"])
        group["adoption"] = _is_adoption(group["entries"])
        group["undo"] = _plan_undo(
            conn, group["plan_id"], adoption=bool(group["adoption"])
        )
        bucket["plans"].append(group)
        bucket["files"] = int(bucket["files"]) + len(group["entries"])
    return days


def _day_label(day: str) -> str:
    try:
        parsed = date.fromisoformat(day)
    except ValueError:
        return day or "Undated"
    today = datetime.now(UTC).date()
    if parsed == today:
        return "Today"
    if parsed == today - timedelta(days=1):
        return "Yesterday"
    return parsed.strftime("%-d %B %Y")


def _is_correction(entries: list[dict[str, object]]) -> bool:
    """Library in, library out: a file the owner already had was moved.

    Read from the journal rather than from a new column, because the journal
    already records both roots and has since the first release. "Filed 4 files"
    is the wrong sentence for a correction — nothing was filed, something was
    rearranged.
    """
    moves = [entry for entry in entries if entry.get("action") == "move"]
    return bool(moves) and all(
        entry.get("src_root") == "library" and entry.get("dest_root") == "library"
        for entry in moves
    )


def _is_adoption(entries: list[dict[str, object]]) -> bool:
    """One of the two operations reads from the encoder's workspace.

    Read off the journal, like `_is_correction`, rather than from a new column.
    The forward direction has a source in `optimization`; the reverse has a
    destination there.
    """
    return any(
        "optimization" in {entry.get("src_root"), entry.get("dest_root")}
        for entry in entries
    )


def _restored_decision(conn: sqlite3.Connection, plan_id: object) -> str:
    """What kind of decision this plan put back, when it put one back.

    The journal stays precise — eighteen rows, eighteen paths, eighteen
    fingerprints. This is presentation: "Restored 18 files from a previous
    similar-files decision" is the sentence a person can act on, and it is
    read off the plan's own provenance rather than guessed from the paths.
    """
    if not plan_id:
        return ""
    from librairy.quarantine_groups import ORIGIN_LABEL, restored_by

    origin = restored_by(conn, str(plan_id))
    if not origin:
        return ""
    found = conn.execute(
        "SELECT f.kind FROM plans p LEFT JOIN audit_findings f ON f.id = p.audit_finding_id"
        " WHERE p.id=?",
        (origin,),
    ).fetchone()
    kind = str(found["kind"] or "") if found else ""
    return ORIGIN_LABEL.get(kind, "a previous").lower()


def _plan_summary(entries: list[dict[str, object]], restored: str = "") -> str:
    """What this plan did, in the words the buttons that caused it used."""
    count = len(entries)
    files = "file" if count == 1 else "files"
    if restored:
        #  One sentence for one decision. Before this it read "Filed 18 files",
        #  which is true of the moves and false about what happened: nothing
        #  was filed, a decision was reversed.
        return f"Restored {count} {files} from {restored} decision"
    if _is_adoption(entries):
        #  Named before the generic branches below, which would otherwise call
        #  it "Filed 1 file, quarantined 1" — two true halves that together
        #  describe nothing a person did.
        undone = any(entry.get("dest_root") == "optimization" for entry in entries)
        return (
            "Optimization undone · original restored"
            if undone
            else "Optimized version adopted · original preserved"
        )
    if all(entry.get("action") == "undo_move" for entry in entries):
        return f"Put {count} {files} back"
    if _is_correction(entries):
        return f"Library correction · moved {count} {files}"
    quarantined = sum(1 for entry in entries if entry.get("dest_root") == "quarantine")
    if quarantined == count:
        return f"Quarantined {count} {files}"
    filed = count - quarantined
    if quarantined:
        return f"Filed {filed} {files}, quarantined {quarantined}"
    return f"Filed {count} {files}"


def _journal_size(conn: sqlite3.Connection) -> int:
    """So "12 of 4,318" tells you the search worked, not that history is small."""
    return int(conn.execute("SELECT COUNT(*) FROM history").fetchone()[0])


#  What the encoder's workspace is called when a person reads about it. The
#  real path is `appdata/optimization/jobs/<id>/output.flac`, which is a fact
#  about LibrAIry's internals and not about the reader's library — and it is
#  inside the container, so it would not even help them find anything.
INTERNAL_LABEL = "LibrAIry's optimization workspace"


def _augment(entry: dict[str, object]) -> dict[str, object]:
    entry["browse_href"] = _browse_href(entry.get("dest_root"), entry.get("dest_relpath"))
    #  One place, so no template has to remember. The journal keeps the real
    #  path — Undo needs it — and the page says where it is in words.
    entry["src_label"] = _path_label(entry.get("src_root"), entry.get("src_relpath"))
    entry["dest_label"] = _path_label(entry.get("dest_root"), entry.get("dest_relpath"))
    entry["internal"] = "optimization" in {entry.get("src_root"), entry.get("dest_root")}
    return entry


def _path_label(root: object, relpath: object) -> str:
    if root == "optimization":
        return INTERNAL_LABEL
    return f"{root}/{relpath}"


def _browse_href(dest_root: object, dest_relpath: object) -> str | None:
    """Deep-link a committed destination to Browse at its containing folder."""
    if dest_root != "library" or not dest_relpath:
        return None
    parts = str(dest_relpath).split("/")
    if len(parts) < 2:
        return None
    category = parts[0].lower()
    folder = "/".join(parts[1:-1])
    return f"/browse/{category}?folder={folder}" if folder else f"/browse/{category}"


def _timeline(entries: list[dict[str, object]], plans: dict) -> list[dict[str, object]]:
    """Group journal entries by plan, newest first, git-log style."""
    groups: list[dict[str, object]] = []
    by_plan: dict[object, dict[str, object]] = {}
    for entry in entries:
        plan_id = entry.get("plan_id")
        group = by_plan.get(plan_id)
        if group is None:
            plan = plans.get(plan_id)
            group = {
                "plan_id": plan_id,
                "status": plan["status"] if plan else None,
                "ts": entry.get("ts"),
                "entries": [],
            }
            by_plan[plan_id] = group
            groups.append(group)
        group["entries"].append(entry)
    return groups


def plan_detail_data(conn: sqlite3.Connection, plan_id: str) -> dict[str, object]:
    plan = conn.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
    if plan is None:
        raise ValueError("plan not found")
    ops = conn.execute("SELECT * FROM plan_ops WHERE plan_id=? ORDER BY seq", (plan_id,)).fetchall()
    entries = list_history(conn, plan_id=plan_id, limit=200)
    return {"plan": plan, "ops": ops, "entries": entries}


def undo_history_entry(
    conn: sqlite3.Connection, settings: Settings, history_id: int
) -> dict[str, object]:
    return asdict(undo_op(conn, history_id, settings))


def undo_history_plan(
    conn: sqlite3.Connection, settings: Settings, plan_id: str
) -> list[dict[str, object]]:
    return [asdict(result) for result in undo_plan(conn, plan_id, settings)]


def _plans(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT p.*, COUNT(op.id) AS op_count
            FROM plans p
            LEFT JOIN plan_ops op ON op.plan_id = p.id
            GROUP BY p.id
            ORDER BY p.created_at DESC
            LIMIT 25
            """
        )
    )
