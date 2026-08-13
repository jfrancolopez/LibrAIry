from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any

from librairy.audit import FOLDER_KINDS, KINDS, open_findings
from librairy.audit_job import progress as audit_progress
from librairy.classify.images import vision_disagrees, vision_for_items
from librairy.config import Settings
from librairy.corrections import (
    CURRENT,
    MISSING,
    STALE,
    STATE_LABEL,
    CorrectionRefused,
    describe_state,
    finding_state,
    is_executable,
    plan_files,
    resolve_group,
)
from librairy.duplicates import items_with_reports, reports_for_item
from librairy.flags import flags_for, unhidden_name
from librairy.lifecycle import (
    resolved_missing_count,
    transition_item,
    vanished_count,
    vanished_entries,
)
from librairy.paths import PathValidationError, sanitize_component, validate_dest
from librairy.planner import utc_now
from librairy.proposals import decode_evidence, proposal_label
from librairy.quarantine import (
    deletion_operation,
    destination_intent,
    quarantine_operation,
)
from librairy.review_undo import latest as latest_undo
from librairy.review_undo import record as record_undo
from librairy.review_undo import snapshot_proposals
from librairy.search import sync_search_item
from librairy.taxonomy import CATEGORIES, render_destination
from librairy.web.evidence import (
    confidence_caption,
    confidence_segments,
    evidence_caption,
    evidence_mix,
    humanize_evidence,
)

PAGE_SIZE = 50
DEFAULT_CATEGORY_FIELDS = {
    "music": {"artist": "Unknown Artist", "album": "Unknown Album", "genre": "General"},
    "movies": {"title": "Unknown Movie", "year": 0, "genre": "General"},
    "shows": {"show": "Unknown Show", "season": 1, "genre": "General"},
    "photos": {"year": 0, "event": "Unknown Event"},
    "documents": {"year": 0},
    "books": {"author": "Unknown Author", "title": "Unknown Book", "genre": "General"},
    "projects": {"project": "Unknown Project"},
    "misc": {},
}


# The default keeps albums and seasons together, because deciding a whole album
# at once is the fastest way through a queue. Every other sort is a question
# about individual files ("what is eating my disk?"), and answering it while
# still herding rows into their groups would scatter the order you asked for —
# so an explicit sort gives you one flat list.
SORTS = {
    "confidence": ("Best guess first", "p.confidence DESC, p.id DESC"),
    "doubtful": ("Least sure first", "p.confidence ASC, p.id DESC"),
    "name": ("Name A–Z", "i.relpath COLLATE NOCASE ASC"),
    "largest": ("Largest first", "i.size DESC, p.id DESC"),
    "smallest": ("Smallest first", "i.size ASC, p.id DESC"),
    "newest": ("Newest first", "i.first_seen_at DESC, p.id DESC"),
}
DEFAULT_SORT = "confidence"

# What "Approve all confident" means, in one place, so the button can say it.
# A threshold nobody can see is a bulk action taken on trust.
CONFIDENT = 0.85

# The state filter was a free-text box over a database enum: type a sixth word
# and you got an empty list with nothing to say why.
STATES = {
    "proposed": "Waiting on you",
    "postponed": "Put off for later",
    "approved": "Approved, not yet committed",
    "rejected": "Rejected",
    "committed": "Already filed",
}


@dataclass(frozen=True)
class ReviewFilters:
    category: str | None = None
    state: str = "proposed"
    min_confidence: float | None = None
    max_confidence: float | None = None
    has_destination: bool | None = None
    page: int = 1
    sort: str = DEFAULT_SORT

    @property
    def grouped(self) -> bool:
        return self.sort == DEFAULT_SORT

    @property
    def narrowed(self) -> bool:
        """Anything beyond the default view. Keeps the filter panel open when
        it is doing something, so a short list never looks like an empty one."""
        return bool(
            self.category
            or self.state != "proposed"
            or self.min_confidence is not None
            or self.max_confidence is not None
            or self.has_destination is not None
        )


def review_data(
    conn: sqlite3.Connection, filters: ReviewFilters, settings: Settings | None = None
) -> dict[str, object]:
    rows = _proposal_rows(conn, filters)
    total = _proposal_count(conn, filters)
    audit_groups = audit_view(conn, settings)
    return {
        "filters": filters,
        "groups": _group_rows(rows) if filters.grouped else _flat_group(rows),
        "sorts": SORTS,
        "states": STATES,
        "filtered": filters.narrowed,
        "categories": CATEGORIES,
        "dest_folders": destination_folders(conn),
        "total": total,
        "page_size": PAGE_SIZE,
        "has_next": filters.page * PAGE_SIZE < total,
        "has_prev": filters.page > 1,
        # "page 3" alone tells you nothing about how much is left.
        "page_count": max(1, -(-total // PAGE_SIZE)),
        "range_start": 0 if not total else (filters.page - 1) * PAGE_SIZE + 1,
        "range_end": min(filters.page * PAGE_SIZE, total),
        # Filtered out of the list above; without a number the totals would
        # simply be wrong and nothing would say why.
        "vanished": vanished_count(conn),
        "vanished_groups": vanished_view(conn),
        # Nothing in the portal could take a decision back, and "Not this" in
        # particular dropped a file out of the queue with no way to return it.
        "undo": latest_undo(conn),
        # "Approve all confident" gave no clue which rows it meant, so the only
        # honest way to use it was not to.
        "confident": CONFIDENT,
        "confident_ready": _confident_count(conn, filters),
        # A separate list for a separate question. See audit_view.
        "audit_groups": audit_groups,
        "audit_open": sum(len(group["findings"]) for group in audit_groups),
        # None until an audit has ever been asked for, which is what lets the
        # empty state distinguish "nothing is wrong" from "nobody has looked".
        "progress": audit_progress(conn),
    }


def _confident_count(conn: sqlite3.Connection, filters: ReviewFilters) -> int:
    if filters.state != "proposed":
        return 0
    floor = max(CONFIDENT, filters.min_confidence or 0.0)
    return _proposal_count(conn, replace(filters, min_confidence=floor))


def apply_review_action(
    conn: sqlite3.Connection,
    action: str,
    filters: ReviewFilters,
    *,
    proposal_ids: list[int] | None = None,
    all_matching: bool = False,
) -> int:
    if action in {"discard", "mark_delete"}:
        return discard_proposals(
            conn,
            _matching_ids(conn, filters) if all_matching else proposal_ids or [],
            to_delete_pile=action == "mark_delete",
        )
    if action == "reanalyze":
        return reanalyze_proposals(
            conn,
            _matching_ids(conn, filters) if all_matching else proposal_ids or [],
        )
    if action not in {"approve", "reject", "postpone"}:
        raise ValueError(f"unknown review action: {action}")
    targets = _matching_ids(conn, filters) if all_matching else proposal_ids or []
    if not targets:
        return 0
    status = {"approve": "approved", "reject": "rejected", "postpone": "postponed"}[action]
    item_state = {"approve": "approved", "reject": "pending", "postpone": "postponed"}[action]
    sql = f"""
        SELECT id, item_id
        FROM proposals
        WHERE status='proposed' AND id IN ({_placeholders(targets)})
        """
    rows = conn.execute(
        sql,
        targets,
    ).fetchall()
    # Photographed before anything changes, so one press of Undo puts the whole
    # batch back — approving forty by accident is exactly when you need it.
    record_undo(conn, action, snapshot_proposals(conn, [int(row["id"]) for row in rows]))
    for row in rows:
        transition_item(conn, row["item_id"], item_state)
        conn.execute(
            "UPDATE proposals SET status=?, updated_at=? WHERE id=?",
            (status, utc_now(), row["id"]),
        )
    return len(rows)


DEST_FOLDER_LIMIT = 200


def destination_folders(conn: sqlite3.Connection) -> list[str]:
    """Folders that already exist in the library, for the destination box.

    Typing a path from memory is the worst part of correcting a guess. These
    feed a `<datalist>`, so the field stays a plain text input you can type
    anything into — it just suggests the places you already keep things.
    """
    seen: dict[str, None] = {}
    rows = conn.execute(
        "SELECT DISTINCT relpath FROM items WHERE root='library' AND missing_since IS NULL"
    )
    for row in rows:
        parts = PurePosixPath(row["relpath"]).parts[:-1]
        for depth in range(1, len(parts) + 1):
            seen.setdefault("/".join(parts[:depth]) + "/", None)
            if len(seen) >= DEST_FOLDER_LIMIT:
                return sorted(seen)
    return sorted(seen)


#  "3 proposal(s) updated" is the database's account of what happened, not an
#  answer to what was just pressed -- and after Re-analyse, which leaves the old
#  guess on screen on purpose, it reads as nothing having happened at all.
ACTION_TOASTS = {
    "approve": "{n} approved. Nothing has moved yet — commit to file {them}.",
    "reject": "{n} set aside.",
    "postpone": "{n} put off. Filter State to “Put off for later” to find {them} again.",
    "discard": "{n} headed for quarantine on the next commit. Nothing is deleted.",
    "mark_delete": "{n} marked for deletion. {They} move to quarantine/_to-delete on the "
    "next commit — LibrAIry still deletes nothing.",
    "reanalyze": "{n} back in the queue. The old guess stays until a better one lands, "
    "usually within a cycle or two.",
}


def action_toast(action: str, changed: int) -> str:
    plural = changed != 1
    noun = f"{changed} file{'s' if plural else ''}"
    template = ACTION_TOASTS.get(action, "{n} updated.")
    return template.format(n=noun, them="them" if plural else "it", They="They" if plural else "It")


def reanalyze_proposals(conn: sqlite3.Connection, proposal_ids: list[int]) -> int:
    """Look again, with everything.

    This replaced "Not this", which answered a wrong guess by setting the file
    aside and never guessing again — a dead end reachable in one click and
    escapable only from the command line. The useful thing to say to a wrong
    guess is *try harder*: the item goes back to 'discovered', so the next
    worker cycle runs the whole cascade over it again — tags, catalogs,
    library patterns, the duplicate detectors and any AI provider now
    configured. Keys added after the first scan are the usual reason a second
    pass does better than the first.

    The proposal row is left alone rather than superseded. `upsert_proposal`
    updates the live proposal in place, so the old guess stays visible, marked
    as being re-checked, until a better one lands on top of it.
    """
    if not proposal_ids:
        return 0
    rows = conn.execute(
        f"""
        SELECT p.id, p.item_id
        FROM proposals p JOIN items i ON i.id = p.item_id
        WHERE p.status IN ('proposed', 'postponed') AND i.missing_since IS NULL
          AND p.id IN ({_placeholders(proposal_ids)})
        """,  # noqa: S608 - placeholders are generated from the id count
        proposal_ids,
    ).fetchall()
    record_undo(conn, "reanalyze", snapshot_proposals(conn, [int(row["id"]) for row in rows]))
    for row in rows:
        transition_item(conn, row["item_id"], "discovered")
        conn.execute(
            "UPDATE proposals SET status='proposed', updated_at=? WHERE id=?",
            (utc_now(), row["id"]),
        )
    return len(rows)


def discard_proposals(
    conn: sqlite3.Connection, proposal_ids: list[int], *, to_delete_pile: bool = False
) -> int:
    """"I don't want this file" — which means quarantine, not delete.

    LibrAIry does not delete, and this is the one place a person is most likely
    to wish it did. Quarantine is the honest version of that wish: the file
    leaves the inbox, stops appearing in Review, and is restorable from the
    Quarantine page for as long as you like. Emptying it is a decision you make
    in your own file manager, deliberately, not one this app makes for you on a
    single click.

    Nothing moves here. The proposal is retargeted at quarantine and approved,
    so the move goes through the same planner and executor as everything else:
    hash-verified, journaled, undoable.

    `to_delete_pile` aims it at the shelf inside quarantine for files you have
    finished with, so the ones you mean to remove gather in one folder instead
    of mixed in with the ones you are only setting aside. Still not a delete,
    and still restorable.
    """
    if not proposal_ids:
        return 0
    rows = conn.execute(
        f"""
        SELECT p.id, p.item_id, i.relpath
        FROM proposals p JOIN items i ON i.id = p.item_id
        WHERE p.status IN ('proposed', 'postponed') AND p.id IN ({_placeholders(proposal_ids)})
        """,  # noqa: S608 - placeholders are generated from the id count
        proposal_ids,
    ).fetchall()
    # This one rewrites the destination as well as the status, so the snapshot
    # is the only record of where the file was originally going.
    record_undo(
        conn,
        "mark_delete" if to_delete_pile else "discard",
        snapshot_proposals(conn, [int(row["id"]) for row in rows]),
    )
    for row in rows:
        stage = deletion_operation if to_delete_pile else quarantine_operation
        operation = stage(row["relpath"])
        conn.execute(
            """
            UPDATE proposals
            SET status='approved', action='quarantine', dest_root='quarantine',
                dest_relpath=?, updated_at=?
            WHERE id=?
            """,
            (operation.dest_relpath, utc_now(), row["id"]),
        )
        transition_item(conn, row["item_id"], "approved")
    return len(rows)


def edit_proposal(
    conn: sqlite3.Connection,
    settings: Settings,
    proposal_id: int,
    *,
    category: str,
    clean_name: str,
    dest_relpath: str | None,
) -> tuple[dict[str, Any], str | None]:
    if category not in CATEGORIES:
        raise ValueError(f"unknown category: {category}")
    row = conn.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
    if row is None:
        raise ValueError("proposal not found")
    safe_name = sanitize_component(clean_name)
    dest_root = row["dest_root"]
    destination = _validated_destination(
        conn,
        settings,
        proposal_id,
        dest_root,
        category,
        safe_name,
        dest_relpath,
    )
    conn.execute(
        """
        UPDATE proposals
        SET category=?, clean_name=?, dest_relpath=?, updated_at=?
        WHERE id=?
        """,
        (category, safe_name, destination, utc_now(), proposal_id),
    )
    sync_search_item(conn, row["item_id"])
    updated = _proposal_rows(
        conn,
        ReviewFilters(state=row["status"], page=1),
        proposal_ids=[proposal_id],
    )[0]
    warning = "collision suffix applied" if destination != (dest_relpath or destination) else None
    return updated, warning


def evidence_lines(payload: str) -> list[str]:
    lines: list[str] = []
    for entry in decode_evidence(payload):
        source = entry.source.upper()
        if entry.source == "ai":
            source = f"AI:{entry.detail.split(':', 1)[0]}"
            if "/cloud" in entry.detail:
                source = f"CLOUD {source}"
        lines.append(f"[{source}] {entry.field} {entry.detail} {entry.weight:.2f}")
    return lines


def filters_from_query(
    *,
    category: str | None = None,
    state: str = "proposed",
    min_confidence: float | None = None,
    max_confidence: float | None = None,
    has_destination: str | None = None,
    page: int = 1,
    sort: str | None = None,
) -> ReviewFilters:
    destination_filter = None
    if has_destination == "yes":
        destination_filter = True
    elif has_destination == "no":
        destination_filter = False
    return ReviewFilters(
        category=category or None,
        state=state,
        min_confidence=min_confidence,
        max_confidence=max_confidence,
        has_destination=destination_filter,
        page=max(1, page),
        # An unknown sort is a typo or a stale bookmark, not a reason to fail.
        sort=sort if sort in SORTS else DEFAULT_SORT,
    )


def _proposal_rows(
    conn: sqlite3.Connection,
    filters: ReviewFilters,
    *,
    proposal_ids: list[int] | None = None,
) -> list[dict[str, Any]]:
    where, params = _where(filters)
    if proposal_ids:
        where = f"{where} AND p.id IN ({_placeholders(proposal_ids)})"
        params.extend(proposal_ids)
    params = [*params, PAGE_SIZE, (filters.page - 1) * PAGE_SIZE]
    rows = conn.execute(
        f"""
        SELECT p.*, i.relpath AS item_relpath, i.state AS item_state,
               i.size AS item_size, i.first_seen_at AS item_first_seen_at,
               g.kind AS group_kind, g.label AS group_label
        FROM proposals p
        JOIN items i ON i.id = p.item_id
        LEFT JOIN groups g ON g.id = p.group_id
        WHERE {where}
        ORDER BY {_order_by(filters)}
        LIMIT ? OFFSET ?
        """,  # noqa: S608 - _order_by only ever returns a value from SORTS
        params,
    ).fetchall()
    item_ids = [int(row["item_id"]) for row in rows]
    compared = items_with_reports(conn, item_ids)
    seen = vision_for_items(conn, item_ids)
    return [
        {
            **dict(row),
            # What a local model saw in the picture, when one was asked. Lives
            # inside Why: a caption and a screenshot's text are worth reading
            # once, and worth nothing on every row of a page of fifty.
            "vision": (looked := seen.get(int(row["item_id"]))),
            # Surfaced, never acted on. The category dropdown is already in
            # the edit panel below, so the useful thing to do with "that is a
            # receipt, not a photo" is say it and leave the file alone.
            "vision_disagrees": vision_disagrees(looked, row["category"]),
            "evidence_lines": evidence_lines(row["evidence"]),
            "evidence_views": (views := humanize_evidence(row["evidence"])),
            # The score broken into where it came from. A bar of one length
            # says how sure; the same bar in pieces says why, which is what
            # actually decides whether this row needs a closer look.
            "confidence_segments": confidence_segments(views, row["confidence"]),
            "confidence_caption": confidence_caption(views, row["confidence"]),
            "size_label": human_size(row["item_size"]),
            # Advisories, not classification: a wallet or a hidden file should
            # not disappear into a bulk approve unnoticed.
            "flags": flags_for(row["item_relpath"]),
            "unhidden_name": unhidden_name(row["item_relpath"]),
            # The comparison itself is loaded on demand — it carries two
            # previews, and a page of fifty rows must not fetch a hundred.
            "has_duplicate": int(row["item_id"]) in compared,
            # Re-analyse puts the item back to 'discovered' and leaves the old
            # guess on screen until the worker replaces it in place. Without
            # saying so, pressing it looks like it did nothing.
            "rechecking": row["item_state"] == "discovered",
        }
        for row in rows
    ]


def duplicate_comparison(conn: sqlite3.Connection, settings: Settings, item_id: int) -> dict:
    """The inbox copy against each library copy it was matched with.

    Both sides get the same preview machinery the rest of Review uses, because
    "which of these two do I want?" is a question you answer by looking, and
    the numbers underneath are there to settle it when looking is not enough.
    """
    from librairy.web.thumbs import PreviewError, preview_for_item

    comparisons = []
    for report in reports_for_item(conn, item_id):
        sides = []
        for side, other in (("inbox copy", report.item_id), ("library copy", report.other_id)):
            row = conn.execute(
                "SELECT root, relpath FROM items WHERE id=?", (other,)
            ).fetchone()
            try:
                preview = preview_for_item(conn, settings, other)
            except PreviewError:
                # A vanished or unreadable file still deserves its column and
                # its facts; only the picture is missing.
                preview = None
            sides.append(
                {
                    "side": side,
                    "item_id": other,
                    "root": row["root"] if row else "",
                    "relpath": row["relpath"] if row else "",
                    "preview": preview,
                }
            )
        comparisons.append({"report": report, "inbox": sides[0], "library": sides[1]})
    return {"item_id": item_id, "comparisons": comparisons}


def _order_by(filters: ReviewFilters) -> str:
    """Never interpolates user input — the key selects a fixed clause."""
    _, clause = SORTS.get(filters.sort) or SORTS[DEFAULT_SORT]
    if filters.grouped:
        # Group first so an album arrives as an album, then confidence inside it.
        return f"COALESCE(g.kind, 'ungrouped'), COALESCE(g.label, 'Ungrouped'), {clause}"
    return clause


def human_size(size: object) -> str:
    """"1.4 GB", not "1503238553 bytes". Sizes are for comparing at a glance."""
    try:
        value = float(size)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return ""


def _proposal_count(conn: sqlite3.Connection, filters: ReviewFilters) -> int:
    where, params = _where(filters)
    return int(
        conn.execute(
            f"""
            SELECT COUNT(*)
            FROM proposals p
            JOIN items i ON i.id = p.item_id
            WHERE {where}
            """,
            params,
        ).fetchone()[0]
    )


def _matching_ids(conn: sqlite3.Connection, filters: ReviewFilters) -> list[int]:
    where, params = _where(filters)
    return [
        int(row["id"])
        for row in conn.execute(
            f"""
            SELECT p.id
            FROM proposals p
            JOIN items i ON i.id = p.item_id
            WHERE {where}
            """,
            params,
        )
    ]


def _flat_group(rows: list[dict[str, Any]]) -> list[dict[str, object]]:
    """One list, in exactly the order asked for. See the note on SORTS."""
    if not rows:
        return []
    return [{"kind": "sorted", "label": "", "rows": rows}]


def _group_rows(rows: list[dict[str, Any]]) -> list[dict[str, object]]:
    groups: list[dict[str, object]] = []
    by_key: dict[tuple[str, str], dict[str, object]] = {}
    for row in rows:
        key = (row["group_kind"] or "ungrouped", row["group_label"] or "Ungrouped")
        group = by_key.get(key)
        if group is None:
            group = {"kind": key[0], "label": key[1], "rows": []}
            by_key[key] = group
            groups.append(group)
        group_rows = group["rows"]
        assert isinstance(group_rows, list)
        group_rows.append(row)
    return _fold_singletons(groups)


def _fold_singletons(groups: list[dict[str, object]]) -> list[dict[str, object]]:
    """A group of one is not a group.

    The point of a group is deciding a whole album at once; a heading, a
    select-all checkbox and a section margin above a single file is three
    pieces of furniture for a decision you were going to make anyway. It also
    changes as files arrive, so it belongs here rather than in the database:
    the second track of an album turns its group real without re-analysing the
    first. Loose files gather into one section at the end.
    """
    real = [group for group in groups if len(group["rows"]) > 1]  # type: ignore[arg-type]
    loose = [row for group in groups if len(group["rows"]) == 1 for row in group["rows"]]  # type: ignore[arg-type]
    for group in real:
        _shorten_names(group)
    if loose:
        real.append({"kind": "ungrouped", "label": "Ungrouped", "rows": loose})
    return real


def _shorten_names(group: dict[str, object]) -> None:
    """Inside a named group, show only what tells the rows apart.

    A DVD's nine rows each began with the same 52-character folder — the same
    one already spelled out in the heading above them — and ended in the twelve
    characters that actually differ. The full path stays in the title attribute
    and in the destination underneath.
    """
    rows = group["rows"]
    assert isinstance(rows, list)
    if group["kind"] in ("ungrouped", "sorted"):
        return
    parents = [PurePosixPath(row["item_relpath"]).parent.as_posix() for row in rows]
    shared = _common_prefix(parents)
    if not shared:
        return
    for row in rows:
        row["display_name"] = row["item_relpath"][len(shared) + 1 :]


def _common_prefix(paths: list[str]) -> str:
    parts = [path.split("/") for path in paths]
    shared: list[str] = []
    for index in range(min(len(part) for part in parts)):
        step = parts[0][index]
        if any(part[index] != step for part in parts):
            break
        shared.append(step)
    return "/".join(shared) if shared and shared != ["."] else ""


def _where(filters: ReviewFilters) -> tuple[str, list[object]]:
    # A proposal for a file that is no longer on disk is not a decision anyone
    # can make: approving it produces a commit operation that fails, which is
    # exactly what happened -- "Review the exact plan" answered with a raw
    # "source not ready" and no page. The scanner already knows they are gone;
    # nothing was asking it.
    clauses = ["p.status = ?", "i.missing_since IS NULL"]
    params: list[object] = [filters.state]
    if filters.category:
        clauses.append("p.category = ?")
        params.append(filters.category)
    if filters.min_confidence is not None:
        clauses.append("p.confidence >= ?")
        params.append(filters.min_confidence)
    if filters.max_confidence is not None:
        clauses.append("p.confidence <= ?")
        params.append(filters.max_confidence)
    if filters.has_destination is True:
        clauses.append("p.dest_relpath IS NOT NULL")
    elif filters.has_destination is False:
        clauses.append("p.dest_relpath IS NULL")
    return " AND ".join(clauses), params


def _placeholders(values: list[int]) -> str:
    return ",".join("?" for _ in values)


def _validated_destination(
    conn: sqlite3.Connection,
    settings: Settings,
    proposal_id: int,
    dest_root: str,
    category: str,
    clean_name: str,
    dest_relpath: str | None,
) -> str:
    raw_dest = dest_relpath.strip() if dest_relpath else ""
    if not raw_dest:
        rendered = render_destination(
            category,
            {"clean_name": clean_name, **DEFAULT_CATEGORY_FIELDS[category]},
            library_root=settings.library_dir,
            conn=conn,
        )
        if rendered.relpath is None:
            raise PathValidationError(rendered.reason or "destination cannot be rendered")
        raw_dest = rendered.relpath
    if "{" in raw_dest or "}" in raw_dest:
        raise PathValidationError("template tokens are not allowed in destinations")
    root = settings.quarantine_dir if dest_root == "quarantine" else settings.library_dir
    dest = validate_dest(root, raw_dest)
    relpath = dest.relative_to(root.resolve()).as_posix()
    if dest.exists() or _live_destination_exists(conn, proposal_id, dest_root, relpath):
        return _available_relpath(conn, proposal_id, dest_root, root, relpath)
    return relpath


def _live_destination_exists(
    conn: sqlite3.Connection, proposal_id: int, dest_root: str, relpath: str
) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM proposals
        WHERE id != ? AND status IN ('proposed', 'approved')
          AND dest_root=? AND dest_relpath=?
        """,
        (proposal_id, dest_root, relpath),
    ).fetchone()
    return row is not None


def _available_relpath(
    conn: sqlite3.Connection,
    proposal_id: int,
    dest_root: str,
    root: Path,
    relpath: str,
) -> str:
    parsed = PurePosixPath(relpath)
    stem, suffix = _collision_parts(parsed.name)
    counter = 2
    while True:
        candidate = parsed.with_name(f"{stem} ({counter}){suffix}").as_posix()
        dest = validate_dest(root, candidate)
        if not dest.exists() and not _live_destination_exists(
            conn, proposal_id, dest_root, candidate
        ):
            return candidate
        counter += 1


def _collision_parts(name: str) -> tuple[str, str]:
    path = PurePosixPath(name)
    return path.stem, path.suffix


ROOT_LABELS = {
    "inbox": "waiting to be filed",
    "library": "already in your library",
    "quarantine": "in quarantine",
}


def vanished_view(conn: sqlite3.Connection) -> list[dict[str, object]]:
    """Entries whose file is gone, grouped by root and ready to look at.

    Grouped because clearing is scoped that way, and the two groups mean
    different things: an inbox entry that vanished is usually a file you tidied
    away before deciding, a library one is usually a share that is not mounted.
    One count and one button per group, so a number never covers rows the
    button beside it would not touch.

    Library-relative paths only. The point of the list is to recognise what you
    are resolving, which the filename and the destination do; where the file
    used to live on the host does not, and it is not this box's to publish.
    """
    groups: dict[str, list[dict[str, object]]] = {}
    for row in vanished_entries(conn):
        groups.setdefault(row["root"], []).append(
            {
                "item_id": row["item_id"],
                "root": row["root"],
                "name": PurePosixPath(row["relpath"]).name,
                "relpath": row["relpath"],
                "missing_since": (row["missing_since"] or "")[:10],
                # Readable, not raw: "proposed" is the machine's word for it.
                "status": proposal_label(row["status"]),
                "category": row["category"],
                # What that destination *means*. Six of the author's seven were
                # bound for the library and one had been set aside; printed as
                # bare paths under one heading they read as seven filing
                # decisions, which is wrong about the seventh.
                "intent": destination_intent(row["dest_root"], row["dest_relpath"]),
                "destination": f"{row['dest_root']}/{row['dest_relpath']}"
                if row["dest_relpath"]
                else "",
                # Last known, not current — there is no file left to measure.
                "size": human_size(row["size"]),
                # The one line that says why LibrAIry thought what it thought.
                # It is what makes clearing a decision rather than a shrug.
                "why": _why_summary(row["evidence"]),
            }
        )
    return [
        {
            "root": root,
            "label": ROOT_LABELS.get(root, root),
            "count": len(entries),
            "entries": entries,
            # Missing records in this root with nothing left to clear. Without
            # it the page says seven while a diagnostic says eight, and nothing
            # on screen reconciles them.
            "resolved": resolved_missing_count(conn, root=root),
        }
        for root, entries in sorted(groups.items())
    ]


def _why_summary(evidence: str | None) -> str:
    views = humanize_evidence(evidence) if evidence else []
    return " · ".join(view.text for view in views[:2])


def audit_view(
    conn: sqlite3.Connection, settings: Settings | None = None
) -> list[dict[str, object]]:
    """Library Audit findings, grouped by folder, for the Review page.

    Kept in its own table and rendered outside the inbox form on purpose. The
    inbox bulk actions ("Approve all confident") select checkboxes inside that
    form; a finding about a file you already own must not be reachable by a
    button meant for files arriving. Structure enforces it, not a filter
    somebody could forget.

    Every row also carries whether it is still true. A finding is a statement
    about a file at a moment, and the file can be re-tagged or replaced between
    the audit and the button — so the row says *Needs re-analysis* rather than
    offering a correction that would move something nobody looked at. Reads
    only: rendering this page must not write, which is what keeps a page load
    from competing with the worker for SQLite's one writer lock.
    """
    groups: dict[str, list[dict[str, object]]] = {}
    for row in open_findings(conn, include_accepted=True):
        folder = row["relpath"].rpartition("/")[0] or row["relpath"]
        groups.setdefault(folder, []).append(_audit_row(conn, settings, row))
    return [
        {"folder": folder, "count": len(findings), "findings": findings}
        for folder, findings in sorted(groups.items())
    ]


AUDIT_BULK_ACTIONS = ("accept", "keep", "reaudit")


def apply_audit_bulk(
    conn: sqlite3.Connection,
    settings: Settings,
    action: str,
    finding_ids: list[int],
) -> str:
    """Act on selected Library Audit findings, and say exactly what happened.

    Only ever reads `audit_findings`. There is no path from here to a proposal:
    every id is looked up in the findings table, so an inbox id simply does not
    resolve — and the caller's field is named `finding_id`, so one cannot be
    passed by accident either.

    Returns a sentence for the page, because "accepted 1 of 3" is the whole
    point of allowing a mixed selection at all.
    """
    from librairy.audit import audit_library, keep_as_is, sanitize_scope
    from librairy.corrections import CorrectionRefused, accept_correction

    if action not in AUDIT_BULK_ACTIONS:
        raise ValueError(f"unknown audit action: {action}")
    rows = [
        row
        for row in (
            conn.execute("SELECT * FROM audit_findings WHERE id=?", (finding_id,)).fetchone()
            for finding_id in finding_ids
        )
        if row is not None
    ]
    if not rows:
        return "Nothing was selected."

    if action == "keep":
        for row in rows:
            keep_as_is(conn, row["id"])
        return f"Marked {len(rows)} as no change."

    if action == "reaudit":
        # One audit per distinct folder, not one per row: re-auditing the same
        # folder five times is the same answer five times, slowly.
        folders = {row["relpath"].rpartition("/")[0] for row in rows}
        for folder in sorted(folders):
            audit_library(
                conn, settings, scope=sanitize_scope(folder, settings.library_dir)
            )
        return f"Looked again at {len(folders)} folder(s)."

    accepted, refused = 0, []
    for row in rows:
        try:
            accept_correction(conn, settings, row["id"])
        except CorrectionRefused as exc:
            refused.append(str(exc))
        else:
            accepted += 1
    if not refused:
        return f"Accepted {accepted} correction(s). Nothing has moved yet — commit to apply."
    # Every distinct reason, not just the first. "4 could not be accepted:
    # already waiting for Commit" is actively misleading when two of them were
    # actually observations and one had changed on disk.
    reasons = "; ".join(dict.fromkeys(refused))
    if not accepted:
        return f"Nothing was accepted. {reasons}."
    return f"Accepted {accepted} of {len(rows)}. {len(refused)} could not be: {reasons}."


def _corrigible(row: sqlite3.Row) -> bool:
    """Could this kind of finding ever produce a move, staleness aside?"""
    from librairy.audit import EXECUTABLE_KINDS

    return row["kind"] in EXECUTABLE_KINDS and bool(row["dest_relpath"])


def _audit_status(
    row: sqlite3.Row, state: str, executable: bool, stale_matters: bool
) -> tuple[str, str]:
    """The chip on the row, in words a person would use.

    Never the stored status value. `open`, `accepted` and `kept` are database
    states; "Waiting for Commit" is what someone is actually looking at.

    Staleness is only mentioned where it changes what you can do. An unindexed
    file has no recorded hash, so it can never be *proved* unchanged — but
    "Needs re-analysis" on an observation that is perfectly accurate would be
    a warning about nothing, followed by a button that re-finds the same thing.
    """
    if row["status"] == "accepted":
        return "waiting", "Waiting for Commit"
    if row["status"] == "corrected":
        return "corrected", "Corrected"
    if row["status"] == "kept":
        return "kept", "No change"
    if state == MISSING:
        return "missing", "Not on disk"
    if state == STALE and stale_matters:
        return "stale", "Needs re-analysis"
    return ("correction", "Correction") if executable else ("observation", "Observation")


def _audit_title(relpath: str, kind: str) -> str:
    """What to call this row. A filename for a file, the folder's own name for
    a finding about a folder — never an id, and never "audit finding #7"."""
    name = PurePosixPath(relpath).name
    return name or relpath if kind != "missing-artwork" else PurePosixPath(relpath).name


def _audit_row(
    conn: sqlite3.Connection, settings: Settings | None, row: sqlite3.Row
) -> dict[str, object]:
    accepted = row["status"] == "accepted"
    state = CURRENT if settings is None else finding_state(settings, row)
    executable = settings is not None and is_executable(row, state)
    affected: list[dict[str, str]] = []
    blocked = ""
    if executable and not accepted:
        try:
            group = resolve_group(conn, settings, row)
        except CorrectionRefused as exc:
            # Resolvable in principle, not resolvable today — an unindexed
            # companion, a disc structure. Say so instead of offering a button
            # that would refuse.
            executable = False
            blocked = str(exc)
        else:
            affected = [
                {
                    "name": item.name,
                    "relpath": item.relpath,
                    "dest_relpath": item.dest_relpath,
                    "role": item.role,
                    "reason": item.reason,
                }
                for item in group.files
            ]
    elif accepted:
        affected = [
            {
                "name": PurePosixPath(op["src_relpath"]).name,
                "relpath": op["src_relpath"],
                "dest_relpath": op["dest_relpath"],
                "role": op["role"],
                "reason": "",
            }
            for op in plan_files(conn, row["plan_id"])
        ]
    views = humanize_evidence(row["evidence"]) if row["evidence"] else []
    # A stale observation is still a true observation. Only a finding that
    # could otherwise move a file has anything to gain from being looked at
    # again, so only that one says so and only that one offers Re-audit.
    stale_matters = state == STALE and _corrigible(row)
    status_kind, status_label = _audit_status(row, state, executable, stale_matters)
    # Preview resolves the item, which carries its *current* root and relpath —
    # so it can only ever show the file where it is now, never the destination
    # being suggested for it. A file that is not there gets no control at all.
    can_preview = bool(row["item_id"]) and state != MISSING and not _is_folder(row)
    return {
        "id": row["id"],
        "kind": row["kind"],
        "label": KINDS.get(row["kind"], row["kind"]),
        "severity": row["severity"],
        "summary": row["summary"],
        "title": _audit_title(row["relpath"], row["kind"]),
        "is_file": not _is_folder(row),
        "current": row["relpath"],
        # Empty for an observation. The template must not render a
        # "suggested" line for a finding that has nowhere to suggest.
        "suggested": row["dest_relpath"] or "",
        "change": path_change(row["relpath"], row["dest_relpath"] or ""),
        "why": _why_summary(row["evidence"]),
        "evidence_views": views,
        "evidence_mix": evidence_mix(views),
        "evidence_caption": evidence_caption(views),
        "sources": _sources(views),
        "state": state,
        "state_label": STATE_LABEL[state],
        "state_detail": describe_state(row, state),
        # What the template branches on: say "this changed" only where saying
        # it leads somewhere.
        "show_stale": stale_matters or state == MISSING,
        "offer_reaudit": stale_matters,
        "status_kind": status_kind,
        "status_label": status_label,
        "executable": executable,
        "accepted": accepted,
        "blocked": blocked,
        "affected": affected,
        "affected_count": len(affected),
        "item_id": row["item_id"],
        "can_preview": can_preview,
        "browse_href": _audit_browse_href(row["relpath"], state),
        # A grouped finding has to say so. One row that speaks for twenty-seven
        # folders still has to be anchored at one of them, and showing that one
        # path alone reads as an accusation against Abba specifically.
        "spans": _spans(row),
    }


# The evidence fields that name another place this finding is also about, and
# what to call the list. Two detectors have the same problem — one row anchored
# at one path, speaking for several — and it is the same tray in both.
GROUPED_FIELDS = {
    "folder": "Spans {count} folders",
    "also at": "{count} identical copies",
}


def _spans(row: sqlite3.Row) -> dict[str, object]:
    """Every other place a grouped finding speaks for, the anchor first.

    Read back off the evidence the detector recorded rather than stored beside
    it, so the list and the reasoning cannot drift apart. The raw entries are
    needed rather than the humanised views, because the view keeps the label
    and drops the field name.
    """
    from librairy.proposals import decode_evidence

    if not row["evidence"]:
        return {}
    try:
        entries = decode_evidence(row["evidence"])
    except Exception:  # noqa: BLE001 - a bad row renders plainly, never 500s
        return {}
    for field, template in GROUPED_FIELDS.items():
        paths = [
            entry.detail
            for entry in entries
            if entry.source == "filesystem" and entry.field == field
        ]
        anchor = row["relpath"]
        paths = [anchor, *[path for path in paths if path != anchor]]
        if len(paths) > 1:
            return {"label": template.format(count=len(paths)), "paths": paths}
    return {}


def path_change(current: str, suggested: str) -> dict[str, str] | None:
    """The part of the path that actually changes, and its context.

    `Music/Pop/JAMES BROWN/Album/song.flac` against
    `Music/Pop/James Brown/Album/song.flac` is one component moving, and
    showing two full paths one above the other makes a person diff them by
    eye. Component comparison is enough — there is no need for a real diff
    algorithm — and when more than one component moves, or the depth changes,
    it falls back to the whole path rather than inventing a summary.
    """
    if not suggested or current == suggested:
        return None
    before, after = current.split("/"), suggested.split("/")
    if len(before) == len(after):
        differing = [index for index in range(len(before)) if before[index] != after[index]]
        if len(differing) == 1:
            index = differing[0]
            return {
                "context": "/".join(before[:index]),
                "before": before[index],
                "after": after[index],
            }
    return {"context": "", "before": current, "after": suggested}


def _is_folder(row: sqlite3.Row) -> bool:
    """Findings about an album or a folder have no filename and no extension."""
    return row["kind"] in FOLDER_KINDS


def _sources(views: list) -> list[str]:
    """The distinct evidence labels, in order, for the one-line summary.

    Only what was actually recorded. A source that contributed nothing is not
    listed as having contributed nothing.
    """
    seen: list[str] = []
    for view in views:
        if view.label not in seen:
            seen.append(view.label)
    return seen


def _audit_browse_href(relpath: str, state: str) -> str | None:
    """Browse, at the folder the file is in *now*.

    Built from the physical path, not from a category the classifier would
    compute — the whole point of the link is to show where the file actually
    is. Nothing to open if nothing is there.
    """
    if state == MISSING:
        return None
    parts = relpath.split("/")
    if len(parts) < 2:
        return None
    category = parts[0].lower()
    folder = "/".join(parts[1:-1])
    return f"/browse/{category}?folder={folder}" if folder else f"/browse/{category}"
