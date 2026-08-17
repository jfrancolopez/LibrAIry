from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from librairy.config import Settings
from librairy.executor import _move_verified, _root_path
from librairy.fingerprint import blake2b_file
from librairy.lifecycle import assert_transition
from librairy.locks import acquire_lock
from librairy.paths import resolve_collision, validate_dest, validate_relpath
from librairy.planner import utc_now
from librairy.search import sync_search_item


class UndoError(RuntimeError):
    pass


@dataclass(frozen=True)
class UndoResult:
    history_id: int
    outcome: str
    dest_relpath: str | None = None


# What the journal can actually tell apart, and nothing more. There is no
# approval or re-analysis event in here — approvals are proposal state changes
# and were never journalled — so offering those as filters would be offering
# categories the data cannot fill. Each of these is a predicate over columns
# that exist, which is why the page can show an honest count beside every one.
HISTORY_KINDS: dict[str, tuple[str, str | None]] = {
    "all": ("Everything", None),
    "filed": ("Filed", "action='move' AND dest_root='library' AND outcome='ok'"),
    "quarantined": ("Quarantined", "action='move' AND dest_root='quarantine' AND outcome='ok'"),
    "undone": ("Undone", "action='undo_move'"),
    "settings": ("Settings", "action='settings_change'"),
    # A settings change stores its before -> after in `outcome`, not a status,
    # so "everything that is not ok" counted all 27 of them as failures and the
    # page opened claiming 27 things had gone wrong. Only moves can fail.
    "failed": ("Failed", "action != 'settings_change' AND outcome != 'ok'"),
}


def kind_counts(conn: sqlite3.Connection, query: str = "") -> dict[str, int]:
    """How many entries each filter would show, against the same search."""
    counts: dict[str, int] = {}
    for kind, (_, predicate) in HISTORY_KINDS.items():
        clauses = [predicate] if predicate else []
        params: list[object] = []
        if query.strip():
            clauses.append(_QUERY_CLAUSE)
            params.extend([f"%{query.strip()}%"] * 3)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        counts[kind] = int(
            conn.execute(
                f"SELECT COUNT(*) FROM history {where}",  # noqa: S608
                params,
            ).fetchone()[0]
        )
    return counts


_QUERY_CLAUSE = "(src_relpath LIKE ? OR dest_relpath LIKE ? OR action LIKE ?)"


def list_history(
    conn: sqlite3.Connection,
    plan_id: str | None = None,
    limit: int = 50,
    query: str = "",
    kind: str = "all",
    offset: int = 0,
) -> list[sqlite3.Row]:
    """The journal, newest first. `query` matches either path or the action.

    The journal only grows, and "scroll until you find it" stops working
    somewhere in the low hundreds. Matching is a plain substring on the paths
    rather than FTS: the useful question here is "where did that file go", and
    the answer is half a filename you remember.
    """
    clauses: list[str] = []
    params: list[object] = []
    if plan_id:
        clauses.append("plan_id = ?")
        params.append(plan_id)
    if query.strip():
        clauses.append(_QUERY_CLAUSE)
        # LIKE wildcards in the query are the user's business; escaping them
        # would only stop someone searching for a literal underscore, which is
        # in half the filenames here.
        like = f"%{query.strip()}%"
        params.extend([like, like, like])
    # Filter and search compose: narrowing to Filed and then typing a name
    # searches within the filter rather than replacing it.
    predicate = HISTORY_KINDS.get(kind, (None, None))[1]
    if predicate:
        clauses.append(predicate)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.extend([limit, offset])
    return list(
        conn.execute(
            f"SELECT * FROM history {where} ORDER BY id DESC LIMIT ? OFFSET ?",  # noqa: S608
            params,
        )
    )


def undo_plan(conn: sqlite3.Connection, plan_id: str, settings: Settings) -> list[UndoResult]:
    rows = conn.execute(
        "SELECT id FROM history WHERE plan_id=? AND outcome='ok' ORDER BY id DESC",
        (plan_id,),
    ).fetchall()
    return [undo_op(conn, row["id"], settings) for row in rows]


def undo_op(conn: sqlite3.Connection, history_id: int, settings: Settings) -> UndoResult:
    with acquire_lock(settings):
        return _undo_op_unlocked(conn, history_id, settings)


def _undo_op_unlocked(
    conn: sqlite3.Connection,
    history_id: int,
    settings: Settings,
) -> UndoResult:
    entry = conn.execute("SELECT * FROM history WHERE id=?", (history_id,)).fetchone()
    if entry is None:
        raise UndoError(f"history entry not found: {history_id}")
    src = validate_relpath(
        _root_path(settings, entry["dest_root"]),
        entry["dest_relpath"],
        kind="source",
    )
    if not src.exists():
        return _record_refused(conn, entry, "undo_refused_missing")
    current_fingerprint = blake2b_file(src)
    if entry["fingerprint"] and current_fingerprint != entry["fingerprint"]:
        return _record_refused(
            conn,
            entry,
            f"undo_refused_changed expected={entry['fingerprint']} actual={current_fingerprint}",
        )

    dest = validate_dest(_root_path(settings, entry["src_root"]), entry["src_relpath"])
    final_dest = resolve_collision(dest)
    final_dest.parent.mkdir(parents=True, exist_ok=True)
    _move_verified(src, final_dest, current_fingerprint, f"undo-{history_id}")
    src_root = _root_path(settings, entry["src_root"]).resolve()
    final_relpath = final_dest.relative_to(src_root).as_posix()
    _record_undo(conn, entry, final_relpath, current_fingerprint, "ok")
    _update_item_after_undo(conn, entry, final_relpath, final_dest)
    return UndoResult(history_id, "ok", final_relpath)


def _record_refused(conn: sqlite3.Connection, entry: sqlite3.Row, outcome: str) -> UndoResult:
    _record_undo(conn, entry, entry["src_relpath"], entry["fingerprint"], outcome)
    return UndoResult(entry["id"], outcome)


def _record_undo(
    conn: sqlite3.Connection,
    entry: sqlite3.Row,
    final_relpath: str,
    fingerprint: str | None,
    outcome: str,
) -> None:
    action = "undo_quarantine" if entry["action"] == "quarantine" else "undo_move"
    conn.execute(
        """
        INSERT INTO history(
          ts, plan_id, op_id, action, src_root, src_relpath, dest_root, dest_relpath,
          fingerprint, outcome
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            utc_now(),
            entry["plan_id"],
            entry["op_id"],
            action,
            entry["dest_root"],
            entry["dest_relpath"],
            entry["src_root"],
            final_relpath,
            fingerprint,
            outcome,
        ),
    )


def _update_item_after_undo(
    conn: sqlite3.Connection,
    entry: sqlite3.Row,
    final_relpath: str,
    final_dest,
) -> None:
    stat = final_dest.stat()
    # The state has to come back too. Undoing a quarantine moved the file to
    # the inbox and left the item reading `quarantined`, which is not a
    # cosmetic disagreement: `quarantined` may legally only become
    # `discovered`, so the row was very nearly frozen, and every count that
    # asks "what is in quarantine" was answering yes about a file in the
    # inbox. A file that has been put back is an ordinary undecided file.
    state = "quarantined" if entry["src_root"] == "quarantine" else "discovered"
    current = conn.execute(
        "SELECT id, state FROM items WHERE root=? AND relpath=?",
        (entry["dest_root"], entry["dest_relpath"]),
    ).fetchone()
    if current is not None:
        assert_transition(current["state"], state)
    conn.execute(
        """
        UPDATE items SET root=?, relpath=?, size=?, mtime_ns=?, state=?,
          last_seen_at=?, missing_since=NULL
        WHERE root=? AND relpath=?
        """,
        (
            entry["src_root"],
            final_relpath,
            stat.st_size,
            stat.st_mtime_ns,
            state,
            utc_now(),
            entry["dest_root"],
            entry["dest_relpath"],
        ),
    )
    # The index copies the item's root and path, so it has to be told as well:
    # without this, a file put back in the inbox went on appearing in Search as
    # a quarantined one.
    if current is not None:
        sync_search_item(conn, int(current["id"]))
