from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any

from librairy.arrival_comparison import describe as describe_arrival
from librairy.audit import FOLDER_KINDS, KINDS, findings_by_ids
from librairy.audit_duplicates import copies as duplicate_copies
from librairy.audit_job import progress as audit_progress
from librairy.classify.images import vision_disagrees, vision_for_items
from librairy.config import Settings
from librairy.correction_state import active_plan, active_plans_for
from librairy.corrections import (
    CURRENT,
    MISSING,
    STATE_LABEL,
    CorrectionRefused,
    describe_state,
    finding_state,
    group_size,
    is_executable,
    plan_files,
    resolve_group,
)
from librairy.destination_choice import ChildFolders
from librairy.document_works import is_work_finding
from librairy.duplicates import items_with_reports, reports_for_item
from librairy.flags import flags_for, unhidden_name
from librairy.inbox_duplicates import describe as describe_duplicate
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
from librairy.web.actionability import (
    APPLYING,
    DISMISSED,
    NEEDS_ANALYSIS,
    OUTDATED,
    READY,
    WAITING,
    actionability,
    can_approve,
    summarize,
)
from librairy.web.actionability import (
    EXPLANATION as ACTION_NOTE,
)
from librairy.web.actionability import (
    LABEL as ACTION_LABEL,
)
from librairy.web.collections import collections_view
from librairy.web.evidence import (
    confidence_caption,
    confidence_segments,
    evidence_caption,
    evidence_mix,
    humanize_evidence,
)
from librairy.web.subjects import group as group_subjects
from librairy.web.subjects import subject_key

# The three effective states that mean "an approved plan already owns this".
# All three belong under Waiting for Commit: an outdated approval is still an
# approval, and hiding it back among the undecided rows would offer a second.
PENDING_KINDS = (WAITING, OUTDATED, APPLYING)

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
    total = _proposal_count(conn, filters)
    #  Two views, and they page different things. Grouped, the page is a page
    #  of *decisions* — an album is one whether it holds three files or three
    #  hundred, and dividing it at the fiftieth file was the thing that made a
    #  camera card look like a hundred and fifty unrelated problems. Sorted,
    #  the page is a page of files, because that is what the reader asked for.
    if filters.grouped:
        groups, decisions = grouped_page(conn, filters, settings)
        page_size = UNITS_PAGE
    else:
        groups = _flat_group(_proposal_rows(conn, filters, settings=settings))
        decisions = total
        page_size = PAGE_SIZE
    audit_groups = audit_view(conn, settings)
    return {
        "filters": filters,
        "groups": groups,
        "sorts": SORTS,
        "states": STATES,
        "filtered": filters.narrowed,
        "categories": CATEGORIES,
        "dest_folders": destination_folders(conn),
        # Files, for the sentence that says how much is in the inbox.
        "total": total,
        # Decisions, which is what the pager moves through.
        "decisions": decisions,
        "grouped": filters.grouped,
        "page_size": page_size,
        "has_next": filters.page * page_size < decisions,
        "has_prev": filters.page > 1,
        # "page 3" alone tells you nothing about how much is left.
        "page_count": max(1, -(-decisions // page_size)),
        "range_start": 0 if not decisions else (filters.page - 1) * page_size + 1,
        "range_end": min(filters.page * page_size, decisions),
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
        **audit_groups,
        #  What arrived together, as counts only. The members live on the
        #  collection's own page — this block is four numbers per import and
        #  never a row per file. See `librairy/inbox_collections.py`.
        **collections_view(conn),
        # None until an audit has ever been asked for, which is what lets the
        # empty state distinguish "nothing is wrong" from "nobody has looked".
        "progress": audit_progress(conn),
        # The quietest list on the page. Advisory, optional, and separate from
        # both the inbox and the audit — including in what may select it.
        "storage": storage_view(conn),
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
    if action == "approve":
        #  The explicit half of the evidence. Approving is a choice — the same
        #  press could have been Reject, Later, Analyse again or an edit — and
        #  it is recorded here, where the person made it, because the cues that
        #  produced it are only knowable now. The *other* half is whether the
        #  file actually moved, which the executor stamps on. See
        #  `librairy/decisions.py`.
        remember_approvals(conn, [int(row["id"]) for row in rows])
    return len(rows)


def remember_approvals(conn: sqlite3.Connection, proposal_ids: list[int]) -> None:
    """Write down what was chosen for each of these, and under what cues.

    One query for the batch. Nothing is written for a proposal with no
    destination — "I approve of not knowing where this goes" is not a lesson —
    and nothing is written for a file whose destination came from a catalog
    identity or a printed identifier, because repeating *that* is repeating the
    identity, not a habit.
    """
    if not proposal_ids:
        return
    from librairy.decision_cues import cues_for, outcome_for, outranked
    from librairy.decisions import record

    rows = conn.execute(
        f"""
        SELECT p.*, i.relpath AS item_relpath
        FROM proposals p JOIN items i ON i.id = p.item_id
        WHERE p.id IN ({_placeholders(proposal_ids)})
        """,  # noqa: S608 - `_placeholders` counts, it does not interpolate
        proposal_ids,
    ).fetchall()
    for row in rows:
        if not row["dest_relpath"] or row["dest_root"] != "library":
            continue
        if outranked(row):
            continue
        outcome = outcome_for(row)
        if not outcome:
            continue
        for cue in cues_for(row):
            record(
                conn,
                cue=cue,
                outcome=outcome,
                item_id=int(row["item_id"]),
                dest_relpath=str(row["dest_relpath"]),
            )


DEST_FOLDER_LIMIT = 200


def destination_folders(conn: sqlite3.Connection) -> list[str]:
    """Folders that already exist in the library, for the destination box.

    Typing a path from memory is the worst part of correcting a guess. These
    feed a `<datalist>`, so the field stays a plain text input you can type
    anything into — it just suggests the places you already keep things.
    """
    #  No `DISTINCT`. `UNIQUE (root, relpath)` already guarantees it inside one
    #  root, so it removed nothing — and it made SQLite build a sorted temporary
    #  B-tree over the whole library before yielding a single row, which quietly
    #  defeated the early return below. At a million files that was 539 ms to
    #  find two hundred folder names that were all in the first hundred rows.
    #
    #  `ORDER BY relpath` keeps the order the DISTINCT happened to give and
    #  makes it explicit, and the index supplies it without sorting anything.
    seen: dict[str, None] = {}
    rows = conn.execute(
        "SELECT relpath FROM items WHERE root='library' AND missing_since IS NULL"
        " ORDER BY relpath"
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
    "mark_delete": "{n} headed for the delete queue on the next commit. {They} move to "
    "quarantine/_to-delete — LibrAIry still deletes nothing.",
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


def proposal_row(
    conn: sqlite3.Connection, settings: Settings | None, proposal_id: int
) -> dict[str, Any]:
    """One proposal, rendered exactly as the list renders it."""
    row = conn.execute(
        "SELECT status FROM proposals WHERE id=?", (proposal_id,)
    ).fetchone()
    if row is None:
        raise ValueError("proposal not found")
    found = _proposal_rows(
        conn,
        ReviewFilters(state=row["status"], page=1),
        proposal_ids=[proposal_id],
        settings=settings,
    )
    if not found:
        raise ValueError("proposal not found")
    return found[0]


def suppress_suggestion(
    conn: sqlite3.Connection, settings: Settings | None, proposal_id: int
) -> dict[str, Any]:
    """"Stop offering me this", meaning the answer and not one phrasing of it.

    A file matches a ladder of cues — Honda manuals, then manuals — and more
    than one rung can reach the same conclusion. Turning off only the rung that
    happened to fire leaves the broader one saying exactly the same thing, so
    pressing the button looks like pressing nothing. Every rung that currently
    agrees on the suggested answer is suppressed; a rung that would say
    something *else* is a different conclusion and is left alone.
    """
    from librairy.decision_cues import cues_for
    from librairy.decisions import suggest, suppress, tally

    row = conn.execute(
        """
        SELECT p.*, i.relpath AS item_relpath
        FROM proposals p JOIN items i ON i.id = p.item_id
        WHERE p.id=?
        """,
        (proposal_id,),
    ).fetchone()
    if row is None:
        raise ValueError("proposal not found")
    ladder = cues_for(row)
    answer = suggest(conn, ladder)
    if answer is not None:
        counts = tally(conn, [cue.signature for cue in ladder])
        for cue in ladder:
            outcomes = counts.get(cue.signature) or {}
            if not outcomes:
                continue
            top = max(outcomes.items(), key=lambda pair: (pair[1], pair[0]))[0]
            if top == answer.outcome:
                suppress(conn, cue.signature)
    return proposal_row(conn, settings, proposal_id)


def apply_suggestion(
    conn: sqlite3.Connection, settings: Settings, proposal_id: int
) -> tuple[dict[str, Any], str | None]:
    """Move this proposal to where the owner usually files things like it.

    Resolved here rather than taken from the page: a suggestion rendered ten
    minutes ago may have been suppressed since, or outgrown by an override, and
    a destination posted from a stale form is a destination nobody currently
    stands behind. Everything after that is the ordinary edit — same
    validation, same collision handling, same `proposed` status at the end.
    """
    row = conn.execute(
        """
        SELECT p.*, i.relpath AS item_relpath
        FROM proposals p JOIN items i ON i.id = p.item_id
        WHERE p.id=?
        """,
        (proposal_id,),
    ).fetchone()
    if row is None:
        raise ValueError("proposal not found")
    found = learned_suggestions(conn, [row])
    answer = found.get(proposal_id)
    if answer is None:
        raise ValueError("there is no suggestion for this file any more")
    return edit_proposal(
        conn,
        settings,
        proposal_id,
        category=str(row["category"]),
        clean_name=str(row["clean_name"]),
        dest_relpath=str(answer["relpath"]),
    )


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


def _work_row(conn, settings, row: sqlite3.Row) -> dict[str, object] | None:  # noqa: ANN001
    """One work in several formats, as the page reads it.

    Facts under each format and no badge on any of them: nobody has said which
    document format they would rather keep, and the music preference is about
    music. Reads the cache — nothing here opens a PDF.
    """
    from librairy.document_works import compare as compare_work

    if settings is None:
        return None
    view = compare_work(conn, settings, row)
    if view is None:
        return None
    return {
        "title": view.title,
        "scheme": view.label,
        "identifier": view.identifier,
        "formats": view.formats,
        "members": [
            {
                "relpath": member.relpath,
                "name": member.name,
                "folder": member.folder,
                "format": member.format,
                "size": human_size(member.size),
                "facts": [
                    {"label": label, "value": value} for label, value in member.facts
                ],
            }
            for member in view.members
        ],
        "count": len(view.members),
    }


def _document_row(row: sqlite3.Row) -> dict[str, object] | None:
    """A document's identity for the Review row, or None.

    Read off the evidence the analysis wrote — the same evidence Why shows —
    rather than by opening the file again. `PDF → Documents` was the whole row
    for a scanned manual; this is the three or four facts that make it
    recognisable, and no more. The extracted text is not among them: a row is
    not the place to print somebody's bank statement.
    """
    from librairy.docmeta import TYPE_LABEL

    entries = [
        entry
        for entry in (decode_evidence(row["evidence"]) if row["evidence"] else [])
        if entry.source == "document"
    ]
    if not entries:
        return None
    found = {entry.field: str(entry.detail) for entry in entries}
    kind = found.get("type", "")
    return {
        "type": kind or TYPE_LABEL["document"],
        "title": found.get("pdf title metadata") or found.get("epub metadata") or "",
        "author": found.get("pdf author metadata") or found.get("epub author") or "",
        "isbn": found.get("isbn") or found.get("epub identifier", ""),
        "doi": found.get("doi in the text", ""),
        #  Said plainly rather than hidden. There is no OCR here, so a scan is
        #  a document LibrAIry has looked at and could not read.
        "scanned": "no text layer" in found.get("text", ""),
        "from_first_page": found.get("first page", ""),
    }


def _proposal_rows(
    conn: sqlite3.Connection,
    filters: ReviewFilters,
    *,
    proposal_ids: list[int] | None = None,
    settings: Settings | None = None,
    limit: int | None = None,
    offset: int | None = None,
) -> list[dict[str, Any]]:
    """One page of rows, enriched.

    `limit`/`offset` exist for the grouped view, which has already decided
    exactly which proposals belong on the page — it pages *decisions*, and the
    rows follow from that rather than the other way round.
    """
    where, params = _where(filters)
    if proposal_ids:
        where = f"{where} AND p.id IN ({_placeholders(proposal_ids)})"
        params.extend(proposal_ids)
    limit = len(proposal_ids) if limit is None and proposal_ids else limit
    params = [
        *params,
        PAGE_SIZE if limit is None else limit,
        (filters.page - 1) * PAGE_SIZE if offset is None else offset,
    ]
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
    #  One lookup for the whole page. A history scan per row is the shape that
    #  stops working at fifty rows and is unusable at five hundred.
    suggested = learned_suggestions(conn, rows)
    #  What the destination's Format Policy will mean once the file is there.
    #  Batched for the same reason: the scope table and the protected roots are
    #  read once, not once per row.
    arriving = arriving_policies(conn, rows)
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
            #  What the document said about itself, read at analysis time and
            #  printed here from the stored evidence. Never a lookup: this page
            #  does not open a PDF to draw a row.
            "document": _document_row(row),
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
            # A file you already have, arriving again. Named on the row rather
            # than buried in the comparison panel: "already in your library"
            # is useless without saying where, and where is the only thing
            # that decides whether this arrival is worth keeping.
            "duplicate_of": describe_duplicate(conn, int(row["item_id"])),
            # Not the same bytes, and the same recording. A different question
            # with three different answers — see
            # `librairy/arrival_comparison.py` for why the cross-root version
            # of "which of these do you want" is not the library-to-library one.
            "similar_to": (
                describe_arrival(conn, settings, int(row["item_id"]))
                if settings is not None
                else None
            ),
            # Re-analyse puts the item back to 'discovered' and leaves the old
            # guess on screen until the worker replaces it in place. Without
            # saying so, pressing it looks like it did nothing.
            "rechecking": row["item_state"] == "discovered",
            #  What the owner has repeatedly chosen for files like this, when
            #  that differs from today's guess. Absent otherwise: a suggestion
            #  agreeing with the destination already on the row is a sentence
            #  that changes nothing.
            "suggestion": suggested.get(int(row["id"])),
            #  What this file's *destination* says about originals, said before
            #  it is filed rather than discovered afterwards. Context, never a
            #  refusal: preserve-originals has never meant LibrAIry may not
            #  file a photograph into a better folder — it means nothing may
            #  later decide that photograph is the dispensable copy. Absent on
            #  every ordinary row, because a note saying "no policy applies
            #  here" is noise on the page where somebody is choosing a folder.
            "arriving_policy": arriving.get(int(row["id"]), ""),
        }
        for row in rows
    ]


def arriving_policies(
    conn: sqlite3.Connection, rows: list[sqlite3.Row]
) -> dict[int, str]:
    """The sentence each proposed destination will make true, once filed.

    Keyed by proposal id and empty for every row whose destination has nothing
    to say, which on most libraries is every row.
    """
    from librairy.format_policy import after_filing_among

    wanted = {
        int(row["id"]): (str(row["dest_relpath"]), str(row["category"] or ""))
        for row in rows
        if row["dest_relpath"] and str(row["dest_root"] or "library") == "library"
    }
    return {
        key: note
        for key, policy in after_filing_among(conn, wanted).items()
        if (note := policy.arriving_note)
    }


def learned_suggestions(
    conn: sqlite3.Connection, rows: list[sqlite3.Row]
) -> dict[int, dict[str, object]]:
    """The learned answer for each of these rows, where there is one.

    Three steps and one query: build each row's cue ladder, tally every
    signature on the page at once, then read each row's answer out of the
    tally. Nothing here opens a file, asks a catalog, or runs a model.

    A suggestion is dropped when it agrees with the destination already
    proposed. The point of showing one is that it would change something; a
    row that says "you usually file these exactly where this is going" is
    furniture.
    """
    from librairy.decision_cues import cues_for, destination_from, outranked
    from librairy.decisions import suggest, tally

    if not rows:
        return {}
    ladders = {int(row["id"]): cues_for(row) for row in rows}
    signatures = sorted(
        {cue.signature for cues in ladders.values() for cue in cues}
    )
    counts = tally(conn, signatures)
    found: dict[int, dict[str, object]] = {}
    for row in rows:
        if outranked(row):
            #  A catalog identity or a printed identifier answers this file's
            #  destination better than any habit. See `decision_cues`.
            continue
        answer = suggest(conn, ladders[int(row["id"])], counts=counts)
        if answer is None:
            continue
        destination = destination_from(answer, row)
        current = str(row["dest_relpath"] or "")
        if not destination or destination == PurePosixPath(current).parent.as_posix():
            continue
        found[int(row["id"])] = {
            "signature": answer.signature,
            "folder": destination,
            "relpath": f"{destination}/{PurePosixPath(current).name}"
            if current
            else destination,
            "support": answer.support,
            "contradictions": answer.contradictions,
            "explanation": answer.explanation,
            "described": answer.cue.described,
        }
    return found


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
    """"1.4 GB", not "1503238553 bytes". Sizes are for comparing at a glance.

    One implementation, two conventions. `humanize.human_bytes` says "unknown"
    for a size nobody recorded, which is right beside a fact sheet and wrong
    inline after a filename — `report.pdf · unknown` reads as a warning. This
    renders nothing there instead, and does not carry a second copy of the
    arithmetic to do it.
    """
    from librairy.humanize import human_bytes

    try:
        value = int(float(size))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""
    rendered = human_bytes(value)
    return "" if rendered == "unknown" else rendered


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


#  How many *decisions* a grouped Review page holds, and how much of each one it
#  shows before you ask for more.
#
#  The page used to hold fifty files, which meant a coherent thing — an album, a
#  camera card, a document set — was divided wherever the fiftieth file happened
#  to fall. One thing is one decision, so the page counts things.
#
#  Five members is a preview, not the group: enough to see what the group is
#  made of, few enough that twenty groups is a page and not a scroll. The rest
#  is one click away and counted honestly in the heading.
UNITS_PAGE = 25
MEMBER_PREVIEW = 5
#  How much more arrives each time somebody asks to see more of one group.
MEMBER_PAGE = 25


def _unit_select(filters: ReviewFilters) -> tuple[str, list[object]]:
    """Every decision waiting, one row each, with what the heading needs.

    A *unit* is a group with more than one member, or a single loose proposal.
    Both are one decision, so both are one row here, and the page is a page of
    these rather than a page of files.

    Deliberately an aggregate over the existing tables and not a new one. The
    `groups` table already says what belongs together; nothing here needs
    storing, and a materialised copy would be a second account of the same
    fact, free to disagree with it.
    """
    where, params = _where(filters)
    return (
        f"""
        SELECT COALESCE('g' || p.group_id, 'p' || p.id) AS unit,
               p.group_id AS group_id,
               COALESCE(g.kind, 'ungrouped') AS kind,
               COALESCE(g.label, 'Ungrouped') AS label,
               COUNT(*) AS members,
               MAX(p.confidence) AS best,
               MIN(p.confidence) AS worst,
               AVG(p.confidence) AS mean,
               SUM(CASE WHEN p.confidence < ? THEN 1 ELSE 0 END) AS doubtful,
               SUM(CASE WHEN p.dest_relpath IS NULL OR p.dest_relpath = '' THEN 1 ELSE 0 END)
                 AS undecided,
               COUNT(DISTINCT p.category) AS categories,
               MIN(p.category) AS category,
               MAX(p.id) AS anchor
        FROM proposals p
        JOIN items i ON i.id = p.item_id
        LEFT JOIN groups g ON g.id = p.group_id
        WHERE {where}
        GROUP BY unit
        """,  # noqa: S608 - where comes from _where; every value is bound
        [CONFIDENT, *params],
    )


#  Groups first, in the order they have always been shown, then the loose files
#  — `ungrouped` sorts after every real kind, which is why the heading order has
#  never needed spelling out. Within that, best guess first and then the newest,
#  which is the default row sort applied to whole decisions.
_UNIT_ORDER = "ORDER BY kind, label, best DESC, anchor DESC"


def unit_count(conn: sqlite3.Connection, filters: ReviewFilters) -> int:
    """How many decisions are waiting. Never `len()` of a page."""
    sql, params = _unit_select(filters)
    return int(
        conn.execute(f"SELECT COUNT(*) FROM ({sql})", params).fetchone()[0]  # noqa: S608
    )


def unit_page(conn: sqlite3.Connection, filters: ReviewFilters) -> list[dict[str, Any]]:
    """One bounded page of decisions, without reading a single member."""
    sql, params = _unit_select(filters)
    offset = max(0, (max(1, filters.page) - 1) * UNITS_PAGE)
    rows = conn.execute(
        f"SELECT * FROM ({sql}) {_UNIT_ORDER} LIMIT ? OFFSET ?",  # noqa: S608
        [*params, UNITS_PAGE, offset],
    ).fetchall()
    return [dict(row) for row in rows]


def member_ids(
    conn: sqlite3.Connection,
    filters: ReviewFilters,
    units: list[dict[str, Any]],
    *,
    limit: int = MEMBER_PREVIEW,
) -> dict[str, list[int]]:
    """A bounded slice of each unit's members, in one statement.

    `ROW_NUMBER` per unit is what keeps a three-thousand-file group from
    putting three thousand rows on a page that shows five of them. Without it
    the only ways to bound this are one query per group, or fetching
    everything and throwing most of it away.
    """
    keys = [str(unit["unit"]) for unit in units]
    if not keys:
        return {}
    where, params = _where(filters)
    placeholders = ",".join("?" * len(keys))
    rows = conn.execute(
        f"""
        SELECT unit, id FROM (
          SELECT COALESCE('g' || p.group_id, 'p' || p.id) AS unit, p.id AS id,
                 ROW_NUMBER() OVER (
                   PARTITION BY COALESCE('g' || p.group_id, 'p' || p.id)
                   ORDER BY p.confidence DESC, p.id DESC
                 ) AS seat
          FROM proposals p
          JOIN items i ON i.id = p.item_id
          WHERE {where} AND COALESCE('g' || p.group_id, 'p' || p.id) IN ({placeholders})
        )
        WHERE seat <= ?
        """,  # noqa: S608 - where comes from _where; keys are bound
        [*params, *keys, limit],
    ).fetchall()
    found: dict[str, list[int]] = {}
    for row in rows:
        found.setdefault(str(row["unit"]), []).append(int(row["id"]))
    return found


def group_members(
    conn: sqlite3.Connection,
    filters: ReviewFilters,
    unit: str,
    *,
    page: int = 1,
    settings: Settings | None = None,
) -> dict[str, object]:
    """One bounded page of a single decision's members.

    The rest of a group, asked for rather than sent. A three-thousand-file
    event costs the Review page five rows until somebody wants more, and then
    it costs a page at a time — the expansion is bounded for the same reason
    the list is.

    Filters are the page's, so expanding a group inside a narrowed view shows
    the members that view is about and not the ones it excluded.
    """
    page = max(1, page)
    #  Expansion continues where the preview stopped, not where a page of
    #  twenty-five would have. Page 1 *is* the preview; page 2 starts at seat
    #  six, not seat twenty-six. Getting this wrong leaves a silent hole in the
    #  middle of a group — twenty members no page ever shows — which is the one
    #  failure this whole design exists to prevent.
    lower = 0 if page <= 1 else MEMBER_PREVIEW + (page - 2) * MEMBER_PAGE
    upper = MEMBER_PREVIEW if page <= 1 else lower + MEMBER_PAGE
    where, params = _where(filters)
    rows = conn.execute(
        f"""
        SELECT id FROM (
          SELECT p.id AS id, COALESCE('g' || p.group_id, 'p' || p.id) AS unit,
                 ROW_NUMBER() OVER (
                   PARTITION BY COALESCE('g' || p.group_id, 'p' || p.id)
                   ORDER BY p.confidence DESC, p.id DESC
                 ) AS seat
          FROM proposals p
          JOIN items i ON i.id = p.item_id
          WHERE {where}
        )
        WHERE unit = ? AND seat > ? AND seat <= ?
        ORDER BY seat
        """,  # noqa: S608 - where comes from _where; the rest is bound
        [*params, unit, lower, upper],
    ).fetchall()
    ids = [int(row["id"]) for row in rows]
    if not ids:
        return {"rows": [], "unit": unit, "more": 0, "next_page": 0}
    found = {
        int(row["id"]): row
        for row in _proposal_rows(conn, filters, proposal_ids=ids, settings=settings)
    }
    members = [found[pid] for pid in ids if pid in found]
    total = _unit_total(conn, filters, unit)
    seen = lower + len(members)
    return {
        "rows": members,
        "unit": unit,
        "total": total,
        "shown": seen,
        "more": max(0, total - seen),
        "next_page": page + 1 if total > seen else 0,
    }


def _unit_total(conn: sqlite3.Connection, filters: ReviewFilters, unit: str) -> int:
    where, params = _where(filters)
    return int(
        conn.execute(
            f"""
            SELECT COUNT(*) FROM proposals p JOIN items i ON i.id = p.item_id
            WHERE {where} AND COALESCE('g' || p.group_id, 'p' || p.id) = ?
            """,  # noqa: S608 - where comes from _where
            [*params, unit],
        ).fetchone()[0]
    )


def grouped_page(
    conn: sqlite3.Connection, filters: ReviewFilters, settings: Settings | None = None
) -> tuple[list[dict[str, object]], int]:
    """The grouped view: decisions paged, members previewed.

    Returns the sections the template already knows how to draw, so the page
    keeps its shape — what changed is that a section is chosen as a whole
    rather than assembled from whichever files a row-limit happened to catch.
    """
    units = unit_page(conn, filters)
    total = unit_count(conn, filters)
    if not units:
        return [], total
    by_unit = member_ids(conn, filters, units)
    wanted = [pid for ids in by_unit.values() for pid in ids]
    rows = {
        int(row["id"]): row
        for row in _proposal_rows(conn, filters, proposal_ids=wanted, settings=settings)
    }
    sections: list[dict[str, object]] = []
    loose: list[dict[str, Any]] = []
    for unit in units:
        members = [rows[pid] for pid in by_unit.get(str(unit["unit"]), []) if pid in rows]
        if not members:
            continue
        #  A decision about one file is a row, not a section with a heading, a
        #  select-all and a margin above it. That was true before groups were
        #  paged and is still true; what changed is that "one file" now means
        #  the whole group has one, never "one landed on this page".
        if int(unit["members"]) <= 1:
            loose.extend(members)
            continue
        section = {
            "kind": str(unit["kind"]),
            "label": str(unit["label"]),
            "rows": members,
            "total": int(unit["members"]),
            "unit": str(unit["unit"]),
            "group_id": unit["group_id"],
            "best": float(unit["best"] or 0.0),
            "worst": float(unit["worst"] or 0.0),
            "mean": float(unit["mean"] or 0.0),
            "doubtful": int(unit["doubtful"] or 0),
            "undecided": int(unit["undecided"] or 0),
            "category": "" if int(unit["categories"] or 0) > 1 else str(unit["category"] or ""),
            "more": max(0, int(unit["members"]) - len(members)),
        }
        _shorten_names(section)
        sections.append(section)
    if loose:
        sections.append(
            {"kind": "ungrouped", "label": "Ungrouped", "rows": loose, "total": len(loose)}
        )
    return sections, total


def _flat_group(rows: list[dict[str, Any]]) -> list[dict[str, object]]:
    """One list, in exactly the order asked for. See the note on SORTS."""
    if not rows:
        return []
    return [{"kind": "sorted", "label": "", "rows": rows}]


def group_totals(
    conn: sqlite3.Connection, filters: ReviewFilters, rows: list[dict[str, Any]]
) -> dict[int, int]:
    """How many files each group on this page actually holds.

    One query for the page, and it answers the question the heading could not:
    a group larger than a page shows the members that landed on it, and until
    this there was nothing to say how many did not. "12 shown" of what?

    Counted under the same filters as the page, because a heading that counted
    the whole group while the list below it was narrowed would be a different
    lie in the same place.
    """
    ids = sorted({int(row["group_id"]) for row in rows if row["group_id"] is not None})
    if not ids:
        return {}
    where, params = _where(filters)
    placeholders = ",".join("?" * len(ids))
    return {
        int(row["group_id"]): int(row["total"])
        for row in conn.execute(
            f"""
            SELECT p.group_id AS group_id, COUNT(*) AS total
            FROM proposals p
            JOIN items i ON i.id = p.item_id
            WHERE {where} AND p.group_id IN ({placeholders})
            GROUP BY p.group_id
            """,  # noqa: S608 - where comes from _where, placeholders are counted
            (*params, *ids),
        )
    }


def _group_rows(
    rows: list[dict[str, Any]], totals: dict[int, int] | None = None
) -> list[dict[str, object]]:
    groups: list[dict[str, object]] = []
    by_key: dict[tuple[str, str], dict[str, object]] = {}
    for row in rows:
        key = (row["group_kind"] or "ungrouped", row["group_label"] or "Ungrouped")
        group = by_key.get(key)
        if group is None:
            group = {"kind": key[0], "label": key[1], "rows": [], "total": 0}
            by_key[key] = group
            groups.append(group)
        group_rows = group["rows"]
        assert isinstance(group_rows, list)
        group_rows.append(row)
        if totals and row["group_id"] is not None:
            group["total"] = totals.get(int(row["group_id"]), 0)
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
    #  A group whose *whole* membership is one file is not a group. One member
    #  on this page is a different thing entirely — the rest are on the next
    #  page — and folding those into the loose pile is what made a 150-photo
    #  event look like 150 unrelated files.
    real = [
        group
        for group in groups
        if len(group["rows"]) > 1 or int(group.get("total") or 0) > 1  # type: ignore[arg-type]
    ]
    loose = [
        row
        for group in groups
        if len(group["rows"]) == 1 and int(group.get("total") or 0) <= 1  # type: ignore[arg-type]
        for row in group["rows"]
    ]
    for group in real:
        _shorten_names(group)
    if loose:
        real.append({"kind": "ungrouped", "label": "Ungrouped", "rows": loose, "total": len(loose)})
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


#  How much of the Library Audit one Review page renders. The rest is counted,
#  never drawn: `audit_view` used to build a row for every finding in the
#  database on every render — one query per row, fifty thousand statements
#  before a single proposal had been looked at, and a page that did not finish
#  at any population M1-01 measured. See docs/performance.md.
#
#  Subjects rather than findings, because a subject is the decision: three
#  findings about one album folder are one card, and cutting the page at
#  twenty-five *findings* could show two thirds of a card.
AUDIT_PAGE = 25

#  The bucket rule, in SQL, matching `web/actionability.actionability` exactly:
#  an active plan outranks the stored status, then `accepted` is waiting, `kept`
#  is dismissed, and what is left is open. Two spellings of one rule is one
#  place for them to disagree, so the Python partition below still has the last
#  word — this only decides which rows are worth building.
#
#  Driven from `plans` and never from `audit_findings`. The obvious spelling is
#  a correlated EXISTS per finding:
#
#      EXISTS (SELECT 1 FROM plans p WHERE p.status IN (...)
#                AND (p.audit_finding_id = f.id OR p.id = f.plan_id))
#
#  and the `OR` across two different columns is what ruins it — neither
#  `idx_plans_audit_finding` nor the `plans` primary key can satisfy both arms,
#  so it scans `plans` once per row of `audit_findings`. Measured at a million:
#  54 seconds for the counts alone, which was slower than the unbounded page it
#  replaced. Found by profiling rather than by reading, because the query looks
#  perfectly ordinary.
#
#  Both arms are cheap read the other way round: active plans are the small
#  side — approved or executing, which is the commit queue — so each arm is a
#  seek, and the union of them is the set of claimed findings, computed once.
_CLAIMED = """
    SELECT p.audit_finding_id AS fid FROM plans p
     WHERE p.status IN ('approved','executing') AND p.audit_finding_id IS NOT NULL
    UNION
    SELECT f2.id AS fid FROM audit_findings f2
     WHERE f2.plan_id IN (SELECT id FROM plans WHERE status IN ('approved','executing'))
"""
_HAS_ACTIVE_PLAN = "(f.id IN (SELECT fid FROM claimed))"
_WAITING = "(has_plan OR status='accepted')"
_DISMISSED = "(NOT has_plan AND status='kept')"
_OPEN = "(NOT has_plan AND status='open')"


def _audit_source() -> str:
    """Every audit finding Review can show, with its bucket and its subject.

    `subject` is `web/subjects.subject_key` written in SQL. It reads only
    `kind` and `relpath`, which is what makes bounding the page possible at
    all — the grouping can be decided without building a single row.
    """
    kinds = ",".join(f"'{kind}'" for kind in sorted(FOLDER_KINDS))
    return f"""
        WITH claimed(fid) AS ({_CLAIMED})
        SELECT f.id AS id, f.status AS status, f.severity AS severity,
               f.relpath AS relpath, {_HAS_ACTIVE_PLAN} AS has_plan,
               CASE WHEN f.kind IN ({kinds}) THEN 'folder:' || f.relpath
                    ELSE 'file:' || f.relpath END AS subject
        FROM audit_findings f
        WHERE f.status IN ('open','accepted','kept')
    """  # noqa: S608 — kinds is a module constant, never input


def _audit_totals(conn: sqlite3.Connection) -> dict[str, int]:
    """The counts, over the whole table, in one statement."""
    row = conn.execute(
        f"""
        SELECT
          COALESCE(SUM(CASE WHEN {_WAITING} THEN 1 ELSE 0 END), 0) AS waiting,
          COALESCE(SUM(CASE WHEN {_DISMISSED} THEN 1 ELSE 0 END), 0) AS dismissed,
          COALESCE(SUM(CASE WHEN {_OPEN} THEN 1 ELSE 0 END), 0) AS open_findings,
          COUNT(DISTINCT CASE WHEN {_OPEN} THEN subject END) AS subjects
        FROM ({_audit_source()})
        """  # noqa: S608
    ).fetchone()
    return {key: int(row[key]) for key in ("waiting", "dismissed", "open_findings", "subjects")}


def _audit_page_ids(conn: sqlite3.Connection, limit: int = AUDIT_PAGE) -> list[int]:
    """The findings worth building a row for, and no others.

    Three bounded reads: a page of waiting, a page of dismissed, and every
    finding belonging to a page of open *subjects* — whole cards, never a card
    with its bottom cut off.
    """
    source = _audit_source()
    ids: list[int] = []
    for bucket in (_WAITING, _DISMISSED):
        ids.extend(
            int(row["id"])
            for row in conn.execute(
                f"SELECT id FROM ({source}) WHERE {bucket}"  # noqa: S608
                " ORDER BY severity DESC, relpath LIMIT ?",
                (limit,),
            )
        )
    subjects = [
        str(row["subject"])
        for row in conn.execute(
            f"SELECT subject, MAX(severity) AS severity, MIN(relpath) AS relpath"  # noqa: S608
            f" FROM ({source}) WHERE {_OPEN}"
            " GROUP BY subject ORDER BY severity DESC, relpath LIMIT ?",
            (limit,),
        )
    ]
    if subjects:
        placeholders = ",".join("?" * len(subjects))
        ids.extend(
            int(row["id"])
            for row in conn.execute(
                f"SELECT id FROM ({source})"  # noqa: S608
                f" WHERE {_OPEN} AND subject IN ({placeholders})",
                subjects,
            )
        )
    return ids


def audit_view(conn: sqlite3.Connection, settings: Settings | None = None) -> dict[str, object]:
    """Library Audit findings, split by what each one is waiting for.

    Kept in its own table and rendered outside the inbox form on purpose. The
    inbox bulk actions ("Approve all confident") select checkboxes inside that
    form; a finding about a file you already own must not be reachable by a
    button meant for files arriving. Structure enforces it, not a filter
    somebody could forget.

    Three lists, not one. An approved correction that keeps sitting among the
    undecided ones looks exactly like a correction nobody approved — which is
    what "I approved it and nothing happened" turned out to mean. And a
    dismissed suggestion has to remain reachable, or dismissing is a decision
    you cannot take back.

    Every row also carries whether it is still true. A finding is a statement
    about a file at a moment, and the file can be re-tagged or replaced between
    the audit and the button — so the row says *Needs analysis again* rather
    than offering a correction that would move something nobody looked at.
    Reads only: rendering this page must not write, which is what keeps a page
    load from competing with the worker for SQLite's one writer lock.
    """
    totals = _audit_totals(conn)
    rows = findings_by_ids(conn, _audit_page_ids(conn))
    #  One query for every row's plan, instead of one query per row. The plan
    #  is what decides which of the three lists below a finding belongs in, so
    #  it cannot simply be dropped — but it never needed asking one at a time.
    plans = active_plans_for(conn, [int(row["id"]) for row in rows])
    #  One scan of a section for the whole page instead of one per finding.
    #  Every candidate-folder question on this page asked about the same
    #  section and got the same answer, each answer costing a scan of it.
    folders = ChildFolders()
    views = [_audit_row(conn, settings, row, plans, folders) for row in rows]
    # Which list a row belongs in is decided by its *effective* state, never by
    # the status column it was fetched under. Those two disagree on the live
    # database — a finding reading `open` that an approved plan already claims —
    # and a row that says "Waiting for Commit" while sitting under the heading
    # of things nobody has answered is the same lie in a different place.
    waiting = [view for view in views if view["status_kind"] in PENDING_KINDS]
    dismissed = [view for view in views if view["status_kind"] == DISMISSED]
    pending_or_done = {*PENDING_KINDS, DISMISSED}
    groups = _subject_groups(
        [view for view in views if view["status_kind"] not in pending_or_done]
    )
    return {
        "audit_groups": groups,
        #  From SQL over the whole table, never `len()` of what was rendered.
        #  The heading has to stay true when the page below it is a page.
        "audit_open": totals["open_findings"],
        "audit_waiting": waiting,
        "audit_dismissed": dismissed,
        "audit_subjects_total": totals["subjects"],
        "audit_subjects_shown": len(groups),
        "audit_waiting_total": totals["waiting"],
        "audit_dismissed_total": totals["dismissed"],
        "audit_more": max(0, totals["subjects"] - len(groups)),
    }


def _subject_groups(views: list[dict[str, object]]) -> list[dict[str, object]]:
    """One group per real-world thing, not one per folder path.

    Grouping used to be `relpath.rpartition("/")`, which put every finding
    about `Music/Pop/A Taste Of Honey` under `Music/Pop` alongside every other
    Pop artist, and rendered each as its own top-level card. Two findings about
    one album folder therefore arrived as two competing questions with nothing
    saying they were about the same album. See `web/subjects.py` for what
    replaced it, and for why the two are *not* merged into one decision.
    """
    subjects = group_subjects(views)
    return [
        {
            "key": subject.key,
            "folder": subject.label,
            "count": subject.count,
            "primary": subject.primary,
            "related": subject.related,
            "subsumed": subject.subsumed,
            "others": subject.others,
            # Every row in the group, flat, for the places that just need to
            # walk them — counts, and the bulk selection scope.
            "findings": [subject.primary, *subject.others],
        }
        for subject in subjects
    ]


AUDIT_BULK_ACTIONS = ("accept", "keep", "reaudit", "restore")


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

    Returns a sentence for the page. Never "2 item(s) updated": on a page that
    moves files somebody already owns, the ones that were *not* acted on are
    the interesting half, and each of them gets counted by name.
    """
    from librairy.audit import audit_library, keep_as_is, restore_suggestion, sanitize_scope
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
        # Only an undecided row can be dismissed. Dismissing one that is
        # waiting for Commit would leave an approved plan with no row admitting
        # to it, and dismissing one already applied would claim a decision
        # nobody made.
        kept = 0
        for row in rows:
            if row["status"] != "open":
                continue
            keep_as_is(conn, row["id"])
            kept += 1
        return _plain(
            len(rows), kept, "Dismissed", "They stay in Dismissed and can be restored."
        )

    if action == "restore":
        restored = sum(1 for row in rows if restore_suggestion(conn, row["id"]))
        return _plain(len(rows), restored, "Restored", "They are back in Library Review.")

    if action == "reaudit":
        # One audit per distinct folder, not one per row: re-auditing the same
        # folder five times is the same answer five times, slowly.
        folders = {row["relpath"].rpartition("/")[0] for row in rows}
        for folder in sorted(folders):
            audit_library(
                conn, settings, scope=sanitize_scope(folder, settings.library_dir)
            )
        return f"Looked again at {len(folders)} folder(s)."

    # Approval. Every row is counted under what it actually is, so a selection
    # of one correction and two observations reports three outcomes rather than
    # one number that flatters the result.
    counts: dict[str, int] = {}
    for row in rows:
        try:
            accept_correction(conn, settings, row["id"])
        except CorrectionRefused:
            state = CURRENT if settings is None else finding_state(settings, row)
            counts_key = actionability(row, state, executable=False)
            counts[counts_key] = counts.get(counts_key, 0) + 1
        else:
            counts[READY] = counts.get(READY, 0) + 1
    summary = summarize(counts, len(rows))
    if counts.get(READY):
        return f"{summary}. Approved changes are waiting for Commit — nothing has moved yet."
    return f"{summary}. Nothing was approved."


def _plain(selected: int, changed: int, verb: str, note: str) -> str:
    """A bulk sentence for actions that cannot partially refuse for many reasons."""
    if changed == selected:
        return f"{verb} {changed}. {note}"
    return f"Selected: {selected} · {verb}: {changed} · Unchanged: {selected - changed}. {note}"


def _audit_title(relpath: str, kind: str) -> str:
    """What to call this row. A filename for a file, the folder's own name for
    a finding about a folder — never an id, and never "audit finding #7"."""
    name = PurePosixPath(relpath).name
    return name or relpath if kind != "missing-artwork" else PurePosixPath(relpath).name


def _merge_view(conn: sqlite3.Connection, settings: Settings, row: sqlite3.Row):  # noqa: ANN201
    """What this merge would do, or None if this finding is not one.

    Refusals come back as None rather than as an exception: a merge LibrAIry
    cannot carry out is an observation, and the row already has a place to say
    why — `blocked` is filled in by the ordinary resolution path below.
    """
    from librairy.merge import is_merge_finding, plan_merge

    if not is_merge_finding(row):
        return None
    try:
        return plan_merge(conn, settings, row, verify=False)
    except CorrectionRefused:
        return None


def _merge_row(view, settings: Settings) -> dict[str, object]:  # noqa: ANN001, ARG001
    """One merge, as the page reads it: what is settled and what is not."""
    from librairy.merge import CHOICE_LABEL, CHOICE_NOTE

    return {
        "target": view.target,
        "target_name": PurePosixPath(view.target).name,
        "sources": [
            {"relpath": source, "name": PurePosixPath(source).name}
            for source in view.sources
        ],
        "moving": len(view.moving),
        "conflicts": [
            {
                "relpath": conflict.relpath,
                "name": conflict.name,
                "dest_relpath": conflict.dest_relpath,
                "identical": conflict.state == "identical",
                "size": human_size(conflict.size),
                "occupant_size": human_size(conflict.occupant_size),
                "keep_both_relpath": conflict.keep_both_relpath,
                "keep_both_name": PurePosixPath(conflict.keep_both_relpath).name,
                "choice": conflict.choice,
                "choice_label": CHOICE_LABEL.get(conflict.choice, ""),
                "options": [
                    {
                        "value": option,
                        "label": CHOICE_LABEL[option],
                        "note": CHOICE_NOTE[option],
                        "chosen": option == conflict.choice,
                    }
                    for option in conflict.options
                ],
            }
            for conflict in view.conflicts
        ],
        "unresolved": len(view.unresolved),
        "settled": view.settled,
        "operations": view.operations,
        "size": human_size(view.total_bytes),
    }


def comparison_facts(
    conn: sqlite3.Connection, settings: Settings, finding_id: int
) -> dict[str, object]:
    """The measured table for one comparison, built on demand.

    Fetched rather than rendered with the page, because this is where the
    `ffprobe` calls are. Nothing is written and nothing is fetched from
    outside the machine: the readers are the same ones the inbox comparison
    panel has always used, and they ask the file, not the internet.
    """
    from librairy.corrections import load_finding
    from librairy.similar_media import compare

    view = compare(conn, settings, load_finding(conn, finding_id), measure=True)
    if view is None:
        return {"labels": (), "members": [], "rows": [], "note": "There is only one of these left."}
    differences = set(view.differences)
    rows = [
        {
            "label": label,
            #  `cells`, not `values`: Jinja resolves `line.values` on a dict to
            #  the dict's own `values` method and renders nothing, with no
            #  error until something tries to iterate it.
            "cells": view.values(label),
            "differs": label in differences,
        }
        for label in view.labels
    ]
    return {
        "labels": view.labels,
        "members": [{"name": member.name, "relpath": member.relpath} for member in view.members],
        "rows": rows,
        #  Said plainly, because the absence of a recommendation is itself the
        #  thing a person needs told. A table with no verdict column looks like
        #  a table that has not finished loading.
        "note": (
            "Measured from the files themselves — nothing here is a recommendation. "
            "Which representation you want is a decision about your library, not "
            "about the numbers."
            if rows
            else "Neither file could be measured: the media tools are not available."
        ),
    }


def arrival_facts(
    conn: sqlite3.Connection, settings: Settings, item_id: int
) -> dict[str, object]:
    """The measured table for one arriving file against the copy it resembles.

    The same shape and the same readers as the library-to-library comparison,
    so a FLAC is described identically wherever it is standing. Only the column
    headings differ, because here one side is filed and the other is arriving
    and that is the difference the person is deciding about.
    """
    from librairy.arrival_comparison import similar_arrival
    from librairy.similar_media import technical_facts

    arrival = similar_arrival(conn, settings, item_id)
    if arrival is None:
        return {
            "labels": (),
            "members": [],
            "rows": [],
            "note": "There is nothing to compare this with.",
        }
    left = dict(technical_facts(settings, arrival.relpath, root="inbox"))
    right = dict(technical_facts(settings, arrival.twin.relpath))
    labels = list(left) + [label for label in right if label not in left]
    rows = [
        {
            "label": label,
            "cells": (left.get(label, "—"), right.get(label, "—")),
            "differs": left.get(label, "—") != right.get(label, "—"),
        }
        for label in labels
    ]
    return {
        "labels": tuple(labels),
        "members": [
            {"name": f"Arriving: {arrival.name}", "relpath": arrival.relpath},
            {"name": f"Filed: {arrival.twin.name}", "relpath": arrival.twin.relpath},
        ],
        "rows": rows,
        "note": (
            "Measured from the files themselves — nothing here is a recommendation. "
            "Which representation you want is a decision about your library, not "
            "about the numbers."
            if rows
            else "Neither file could be measured: the media tools are not available."
        ),
    }


def _swaps(conn: sqlite3.Connection, settings: Settings, row: sqlite3.Row):  # noqa: ANN201
    """Replacement options for a filed pair, or none. Never raises on a page."""
    from librairy.corrections import CorrectionRefused
    from librairy.filed_replace import swaps_for

    try:
        return swaps_for(conn, settings, row)
    except CorrectionRefused:
        return ()


def _comparison_row(
    conn: sqlite3.Connection, settings: Settings | None, row: sqlite3.Row
) -> dict[str, object] | None:
    """Two or more encodes of one thing, without the measurements.

    Deliberately without them. Rendering Review must not run an `ffprobe` per
    member per row — forty comparisons would be eighty subprocesses to draw a
    page — and it must not write, which rules out filling the metadata cache
    on the way past. So the row shows what the index already knows, and the
    measured table is fetched when somebody opens it. See
    `/review/audit/{id}/comparison`.
    """
    from librairy.similar_media import compare

    if settings is None:
        return None
    view = compare(conn, settings, row, measure=False)
    if view is None:
        return None
    #  Too many to compare in a table, which for one release meant the group
    #  did not exist at all. It exists now, and the row says what it is and
    #  where to go and look at it — the member list, the technical panel and
    #  the checkboxes all belong to the small shape and are not drawn here.
    large = _large_group(conn, settings, row, view)
    if large is not None:
        return large
    from librairy.format_preference import label_for, sentence

    return {
        "members": [
            {
                "relpath": member.relpath,
                "name": member.name,
                "folder": member.folder,
                "size": human_size(member.size),
                #  The format, printed as a fact beside the size. `FLAC · 31 MB`
                #  is what the reader is choosing between.
                "format": label_for(conn, member.relpath),
                #  Ticked to start with, where the owner's declared preference
                #  points here. A starting point, not a decision: nothing moves
                #  until Approve and Commit.
                "preferred": member.relpath == view.preferred,
                #  Non-empty when the owner's Format Policy says this file's
                #  originals are not to be traded away. Named rather than
                #  merely disabled: a control that has quietly stopped working
                #  is worse than one that says why it will not.
                "protected_by": member.protected_by,
            }
            for member in view.members
        ],
        #  What the policy has to say about this comparison, when it has
        #  something to say. Only then: a card filled with policy text on every
        #  decision the policy does not affect is how the sentence that *does*
        #  matter stops being read.
        "protected": [
            {"name": member.name, "folder": member.protected_by}
            for member in view.members
            if member.protected_by
        ],
        #  Whose preference it is, said out loud. Never "MP3 is better" — see
        #  `format_preference` for why the wording is part of the feature.
        "preference": sentence(conn) if view.preferred else "",
        "preferred": view.preferred,
        "count": len(view.members),
        "pair": len(view.members) == 2,
        #  The other answer, where the evidence supports it: not "set this one
        #  aside" but "make this one the version that lives at the other's
        #  path". Empty for every pair that is only *similar* — see
        #  `filed_replace` for what is strong enough to move a file into a path.
        "swaps": [
            {
                "relpath": swap.chosen.relpath,
                "name": swap.chosen.name,
                "displaced": swap.displaced.name,
                "displaced_relpath": swap.displaced.relpath,
                "dest_relpath": swap.dest_relpath,
                "same_path": swap.same_path,
                "preferred": swap.preferred,
            }
            for swap in _swaps(conn, settings, row)
        ],
    }


def _large_group(conn, settings, row, view) -> dict[str, object] | None:  # noqa: ANN001
    """A visual group too big for the table, summarised in facts and a link.

    Everything here comes from columns the index already has — how many, how
    many share bytes, which formats — so a Review page holding several of
    these costs several queries rather than several hundred file reads. The
    pictures live on the group's own page, which is the one place worth
    spending a thumbnail on.
    """
    from librairy.mediakind import kind_for
    from librairy.photo_group import SET_ASIDE, choices
    from librairy.similar_media import SMALL_GROUP

    if len(view.members) <= SMALL_GROUP:
        return None
    photos = sum(
        1 for member in view.members if kind_for(member.relpath) == "image"
    )
    #  One query, not one per member: this runs while Review draws its list,
    #  and a five-hundred-member group would otherwise be five hundred
    #  round trips to print a count of copies.
    ids = [member.item_id for member in view.members]
    fingerprints: dict[str, int] = {}
    for found in conn.execute(
        f"SELECT fingerprint FROM items WHERE id IN ({','.join('?' * len(ids))})",  # noqa: S608
        ids,
    ):
        key = str(found["fingerprint"] or "")
        if key:
            fingerprints[key] = fingerprints.get(key, 0) + 1
    formats = sorted(
        {PurePosixPath(member.relpath).suffix.lstrip(".").upper() for member in view.members}
    )
    chosen = choices(conn, int(row["id"]))
    return {
        "large": True,
        #  `photos` only when they really are photographs. A group of thirty
        #  video files is the same scale problem and is not called photos.
        "photos": photos == len(view.members),
        "count": len(view.members),
        "pair": False,
        "members": [],
        "swaps": [],
        "exact_sets": sum(1 for count in fingerprints.values() if count > 1),
        "exact_members": sum(count for count in fingerprints.values() if count > 1),
        "formats": formats,
        "folder": _common_folder([member.relpath for member in view.members]),
        "set_aside": sum(1 for value in chosen.values() if value == SET_ASIDE),
    }


def _common_folder(relpaths: list[str]) -> str:
    """The deepest folder every member is under, or "" when they are scattered.

    Shown because "37 photos · Photos/2024/Backyard" is most of what somebody
    needs to recognise a group; when they are spread across folders there is no
    honest one-line answer and the row says nothing rather than picking one.
    """
    if not relpaths:
        return ""
    parts = [str(PurePosixPath(relpath).parent).split("/") for relpath in relpaths]
    shared: list[str] = []
    for pieces in zip(*parts, strict=False):
        if len(set(pieces)) != 1:
            break
        shared.append(pieces[0])
    return "/".join(shared)


def _filing_view(  # noqa: ANN201
    conn: sqlite3.Connection, settings: Settings | None, row: sqlite3.Row
):
    """Loose tracks and the albums they could go to, or None.

    Refusals come back as None for the same reason every other planner's do on
    this page: a question LibrAIry cannot ask cleanly today is a row that shows
    what it found, not an error page over the whole of Review.
    """
    from librairy.corrections import CorrectionRefused
    from librairy.track_filing import plan_filing

    if settings is None:
        return None
    try:
        return plan_filing(conn, settings, row, verify=False)
    except CorrectionRefused:
        return None


def _filing_row(view, settings: Settings, conn=None) -> dict[str, object]:  # noqa: ANN001, ARG001
    """One filing question, as the page reads it: per track, never per group."""
    from librairy.merge import CHOICE_LABEL, CHOICE_NOTE
    from librairy.track_identity import unavailable

    #  Whether the row may offer to identify a track at all, asked once for the
    #  whole finding rather than once per track. Empty means it can; anything
    #  else is the reason, said out loud instead of a button that is silently
    #  missing.
    blocked = unavailable(conn, settings) if conn is not None and settings else "unknown"

    albums = [
        {"relpath": album.relpath, "name": album.name, "files": album.files}
        for album in view.albums
    ]
    return {
        "artist": PurePosixPath(view.artist).name,
        "albums": albums,
        "proposed": [
            {
                "relpath": album.relpath,
                "name": album.name,
                "agreeing": len(album.tracks),
            }
            for album in view.proposed
        ],
        "moving": len(view.moving),
        "leaving": len(view.leaving),
        "unresolved": len(view.unresolved),
        "settled": view.settled,
        "tracks": [
            {
                "relpath": track.relpath,
                "name": track.name,
                "size": human_size(track.size),
                "chosen": track.chosen,
                "chosen_name": PurePosixPath(track.chosen).name if track.chosen else "",
                "leaving": track.leaving,
                "answered": track.answered,
                "albums": albums,
                #  Folders that do not exist. Kept separate from `albums` all
                #  the way to the template, because a control that offers to
                #  create something must not look like one that found it.
                "proposed": [
                    {
                        "relpath": album.relpath,
                        "name": album.name,
                        #  This track's own evidence, not the group's. Two
                        #  tracks can reach one album by different routes and
                        #  a row that credited the wrong one would be telling
                        #  somebody their file has a tag it does not have.
                        "agreeing": album.agreeing(track.relpath),
                        "source": album.source_for(track.relpath),
                        "note": album.note_for(track.relpath),
                    }
                    for album in view.offered(track)
                    if album.relpath != track.chosen
                ],
                #  What a catalog said this recording is, if anybody asked.
                #  Facts only: an identifier, a name and AcoustID's own score.
                #  No invented percentage, and no claim the row cannot show
                #  the working for.
                "identity": (
                    {
                        "artist": track.identity.artist,
                        "title": track.identity.title,
                        "facts": [
                            {"label": label, "value": value}
                            for label, value in track.identity.evidence
                        ],
                        "releases": len(track.identity.releases),
                    }
                    if track.identity is not None and track.identity.matched
                    else None
                ),
                #  Not the collision below — that is two files wanting one
                #  name. This is the catalog naming a different artist from the
                #  folder the file is in, which is a disagreement to show
                #  rather than resolve.
                "artist_conflict": track.conflict,
                #  Offered only where there is nothing else to go on. An album
                #  folder that already exists is a better answer than a network
                #  round trip, and a track that has been asked about is not
                #  asked again by pressing the same button twice.
                "identify": (
                    not view.offered(track)
                    and not track.evidenced
                    and not track.answered
                    and not _identity_asked(conn, track)
                ),
                "identify_blocked": blocked,
                #  Present only when the chosen album already holds a file of
                #  this name. Same three outcomes as a folder merge, same
                #  words, same storage — see `librairy/merge.py`.
                "conflict": (
                    {
                        "choice": track.member.choice,
                        "choice_label": CHOICE_LABEL.get(track.member.choice, ""),
                        "identical": track.member.state == "identical",
                        "size": human_size(track.member.size),
                        "occupant_size": human_size(track.member.occupant_size),
                        "keep_both_name": PurePosixPath(
                            track.member.keep_both_relpath
                        ).name,
                        "options": [
                            {
                                "value": option,
                                "label": CHOICE_LABEL[option],
                                "note": CHOICE_NOTE[option],
                                "chosen": option == track.member.choice,
                            }
                            for option in track.member.options
                        ],
                    }
                    if track.member is not None and track.member.needs_choice
                    else None
                ),
            }
            for track in view.tracks
        ],
    }


def _album_row(conn, view, row: sqlite3.Row) -> dict[str, object] | None:  # noqa: ANN001
    """One conclusion over a group of loose tracks, as the page reads it.

    Built from the filing view that was already assembled for this row rather
    than from a second pass over the database, and from persisted evidence
    only — drawing Review must not fingerprint a file or ask a catalog
    anything, however many tracks are in front of it.
    """
    from librairy.album_identity import from_view

    if conn is None:
        return None
    found = from_view(view, row)
    if found is None:
        return None
    return {
        "artist": found.artist_name,
        "tracks": found.open_tracks,
        "single": found.single,
        "choice": found.choice,
        "releases": [
            {
                "relpath": conclusion.relpath,
                "name": conclusion.name,
                "detail": conclusion.detail,
                "exists": conclusion.exists,
                "members": len(conclusion.members),
                #  Counted, never scored. See `album_identity.Conclusion.counts`
                #  for why there is no single number here.
                "counts": [
                    {"label": label, "count": count}
                    for label, count in conclusion.counts
                ],
                "exceptions": [
                    {
                        "name": member.name,
                        "reason": member.reason,
                        "detail": member.detail,
                    }
                    for member in conclusion.exceptions
                ],
                "repeats": list(conclusion.repeats),
            }
            for conclusion in found.conclusions
        ],
    }


def _identity_asked(conn, track) -> bool:  # noqa: ANN001
    """Whether this file has already been asked about, match or no match.

    A second press of `Identify track` on a file the catalog had nothing for
    would spend a fingerprint and a request to be told the same thing. The
    stored miss expires on its own; until it does, the row says what happened
    instead of offering the button again.
    """
    from librairy.track_identity import asked

    return bool(conn is not None and track.item_id and asked(conn, track.item_id))


def _destination_view(  # noqa: ANN201
    conn: sqlite3.Connection,
    settings: Settings | None,
    row: sqlite3.Row,
    folders: object | None = None,
):
    """The folders this artist could live in, and which one was picked.

    None for every other kind of finding. `()` candidates — one folder left,
    or too many to be a choice — also come back as None, so the row falls
    through to the observation it has always been rather than offering a
    question with no answers in it.
    """
    from librairy.destination_choice import candidates, is_destination_finding, selected

    if settings is None or not is_destination_finding(row):
        return None
    found = candidates(conn, row, folders)
    if len(found) < 2:
        return None
    return found, selected(conn, row)


def _destination_merge(  # noqa: ANN201
    conn: sqlite3.Connection, settings: Settings, row: sqlite3.Row
):
    """The merge the chosen direction produces, or None while it is unchosen.

    Refusals come back as None for the same reason `_merge_view`'s do: a
    direction LibrAIry cannot plan today is a row that shows the choice and no
    merge, not an error page over the whole of Review.
    """
    from librairy.corrections import CorrectionRefused
    from librairy.destination_choice import plan_for

    try:
        return plan_for(conn, settings, row, verify=False)
    except CorrectionRefused:
        return None


def _destination_row(view, row: sqlite3.Row) -> dict[str, object]:  # noqa: ANN001
    """One destination choice, as the page reads it.

    The counts are facts and are shown as facts. Nothing here sorts by them,
    marks one, or calls one recommended — see `librairy/destination_choice.py`.
    """
    from librairy.destination_choice import subject

    found, answer = view
    return {
        "artist": subject(row),
        "chosen": answer,
        "candidates": [
            {
                "relpath": candidate.relpath,
                "section": candidate.section,
                "name": candidate.name,
                "files": candidate.files,
                "albums": candidate.albums,
                "size": human_size(candidate.bytes),
                "chosen": candidate.relpath == answer,
            }
            for candidate in found
        ],
    }


def _audit_row(
    conn: sqlite3.Connection,
    settings: Settings | None,
    row: sqlite3.Row,
    plans: dict[int, list] | None = None,
    folders: object | None = None,
) -> dict[str, object]:
    # The plan, not the status column. A finding can be `open` and still own an
    # approved plan — that is the inconsistency this pass exists to stop
    # rendering as an invitation to approve it a second time.
    #
    # `plans` is the whole page's answer, fetched once by the caller. Without
    # it this line was one query per finding in the database, on every render
    # of Review — see docs/performance.md.
    plan = active_plan(conn, row["id"], settings, prefetched=plans)
    accepted = plan is not None
    state = CURRENT if settings is None else finding_state(settings, row)
    executable = settings is not None and is_executable(row, state)
    affected: list[dict[str, str]] = []
    affected_size = 0
    blocked = ""
    #  The other shape of choice, and the difference matters. A duplicate's
    #  choice *is* the action — pressing a copy approves it. A merge's choices
    #  are prerequisites: answer them all, then one Approve. So this one can
    #  stop being a CHOICE and become approvable, and a duplicate never does.
    #  And the third shape: a choice between two *folders* rather than two
    #  files. Answering it turns the row into a merge, planned by the same
    #  planner — so the destination is read first and the merge asked for
    #  second, with the direction it just supplied.
    #  And the per-item shape: one answer per loose track, which is the whole
    #  reason `loose-tracks` could never be a folder correction.
    filing = _filing_view(conn, settings, row) if not accepted else None
    destination = _destination_view(conn, settings, row, folders) if not accepted else None
    if destination is not None:
        merge = _destination_merge(conn, settings, row)
    else:
        merge = (
            _merge_view(conn, settings, row)
            if settings is not None and not accepted
            else None
        )
    if merge is not None and merge.unresolved:
        # A merge that still has questions is not resolvable into a group, and
        # asking would raise. Its files are listed by the conflict rows.
        executable = False
    elif executable and not accepted:
        try:
            # `verify=False`: this is a page render, not an approval. A file
            # correction reads two or three files either way, but a folder
            # correction is a whole subtree, and fifty rows of those would make
            # Review read the library twice over to draw itself. The page
            # checks what `stat` answers; `accept_correction` reads the bytes.
            group = resolve_group(conn, settings, row, verify=False)
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
            affected_size = group_size(conn, group)
    elif accepted:
        affected = [
            {
                "name": PurePosixPath(op["src_relpath"]).name,
                "relpath": op["src_relpath"],
                "dest_relpath": op["dest_relpath"],
                "role": op["role"],
                "reason": "",
            }
            for op in plan_files(conn, plan.plan_id)
        ]
    #  Byte-identical copies, and which of them could be the one to go. Only a
    #  duplicate finding has any; see `librairy/audit_duplicates.py` for why
    #  LibrAIry will not pick for you.
    duplicates = (
        duplicate_copies(conn, settings, row)
        if settings is not None and not accepted
        else []
    )
    #  And the third duplicate class: the same recording, encoded twice. Not
    #  the same bytes, so no fingerprint pairs them and no rule ranks them —
    #  see `librairy/similar_media.py` for why nothing here says "best".
    comparison = _comparison_row(conn, settings, row) if not accepted else None
    views = humanize_evidence(row["evidence"]) if row["evidence"] else []
    # One value, and every control on the row derives from it. See
    # `web/actionability.py` for why inferring this from button presence was
    # the actual cause of "I approved it and nothing happened".
    status_kind = actionability(
        row,
        state,
        executable=executable,
        blocked=blocked,
        plan=plan,
        #  A destination choice stays a CHOICE even once it is fully answered.
        #  The person resolved it themselves and may approve it themselves —
        #  what must not happen is "approve all confident" reaching a row whose
        #  whole content is a decision only they could make.
        choices=any(copy.removable for copy in duplicates)
        or bool(merge and merge.unresolved)
        or destination is not None
        or comparison is not None
        or is_work_finding(row)
        or filing is not None,
    )
    status_label = ACTION_LABEL[status_kind]
    # A stale observation is still a true observation. Only a finding that
    # could otherwise move a file has anything to gain from being looked at
    # again, so only that one says so and only that one offers Analyse again.
    stale_matters = status_kind == NEEDS_ANALYSIS
    # Preview resolves the item, which carries its *current* root and relpath —
    # so it can only ever show the file where it is now, never the destination
    # being suggested for it. A file that is not there gets no control at all.
    can_preview = bool(row["item_id"]) and state != MISSING and not _is_folder(row)
    return {
        "id": row["id"],
        "kind": row["kind"],
        # What this finding is *about*, so two detectors' answers about one
        # album folder can be shown as one subject with two checks instead of
        # two cards that look like they contradict each other.
        "subject_key": subject_key(row),
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
        # Said on the row, not implied by a missing button. "Observation only —
        # no automatic correction is available" is a fact about the finding;
        # an absent control is a fact about the template.
        "status_note": ACTION_NOTE[status_kind],
        # The only gate on selection and on the Approve control. A row that is
        # not approvable renders a disabled checkbox, so it can never be part
        # of a selection whose button then quietly does nothing.
        "can_approve": can_approve(status_kind),
        "executable": executable,
        "accepted": accepted,
        "blocked": blocked,
        "affected": affected,
        "affected_count": len(affected),
        # One row per identical file, each saying whether it can be the one set
        # aside and why not when it cannot.
        "copies": [
            {
                "relpath": copy.relpath,
                "folder": copy.folder or "the top of the library",
                "name": PurePosixPath(copy.relpath).name,
                "size": human_size(copy.size),
                "removable": copy.removable,
                "reason": copy.reason,
            }
            for copy in duplicates
        ],
        # Two folders becoming one: what moves cleanly, and every collision
        # with the answers already given. Empty for everything else.
        "merge": _merge_row(merge, settings) if merge is not None else None,
        # Which folder this artist should use. Present only on a destination
        # choice; `None` everywhere else, so no other row can grow buttons.
        "destination": _destination_row(destination, row) if destination is not None else None,
        # Several encodes of one thing, and the measured table that is fetched
        # rather than rendered. `None` for every other kind of finding.
        "comparison": comparison,
        # One work filed in two formats, which is neither a duplicate nor an
        # encoding question. `None` for everything else.
        "work": _work_row(conn, settings, row) if not accepted else None,
        # Loose tracks, one question each. `None` for everything else.
        "filing": _filing_row(filing, settings, conn) if filing is not None else None,
        # What those tracks already agree on, if they agree. `None` when they
        # do not — which leaves the per-track question exactly as it was.
        "album": _album_row(conn, filing, row) if filing is not None else None,
        # The explicit Approve a resolved choice earns, and never bulk. See the
        # `choices` argument above for why the two are separate.
        "approve_choice": bool(
            (destination is not None and merge is not None and merge.settled)
            or (filing is not None and filing.settled and filing.moving)
        ),
        # What the correction is, in facts, above the fold. A folder rename
        # whose scale is only visible after opening an expander is a decision
        # made without the number that matters most.
        "affected_size": human_size(affected_size) if affected_size else "",
        "affects_subtree": bool(affected) and affected[0]["role"] == "member",
        "item_id": row["item_id"],
        "can_preview": can_preview,
        "browse_href": _audit_browse_href(row["relpath"], state),
        # A grouped finding has to say so. One row that speaks for twenty-seven
        # folders still has to be anchored at one of them, and showing that one
        # path alone reads as an accusation against Abba specifically.
        "spans": _spans(row),
        # What this finding is *about*, in facts rather than adjectives: how
        # many tracks, how much disk, how many artists. A grouped row used to
        # show no size at all, which made "one album in twenty-seven folders"
        # sound like a filing quirk rather than 1.3 GB of it.
        "facts": _group_facts(row),
        "size_label": (size_label := human_size(_column(row, "item_size"))),
        # The forensic view, behind one expander. The row above stays
        # scannable; this is where the twenty checkable things LibrAIry
        # already knew about this finding finally become reachable.
        **_decision_support(conn, row, size_label),
    }


def _decision_support(
    conn: sqlite3.Connection, row: sqlite3.Row, size_label: str
) -> dict[str, object]:
    """The details panel, the choices, and what each of them would do."""
    from librairy.web import review_details

    entries = _entries(row)
    folders = [
        entry.detail
        for entry in entries
        if entry.source == "filesystem" and entry.field == "folder"
    ]
    moves = [
        (entry.detail, entry.note)
        for entry in entries
        if entry.source == "filesystem" and entry.field == "move" and entry.note
    ]
    return {
        "details": review_details.build(row, entries, size_label=size_label),
        "proposed": review_details.proposed(moves),
        "decisions": review_details.decisions(row["kind"], row["dest_relpath"] or ""),
        "recommendation": review_details.recommendation(
            row["kind"], row["dest_relpath"] or ""
        ),
        "current_shape": review_details.current_shape(row, folders),
        "current_shape_note": review_details.current_shape_note(row["kind"], folders),
        "artwork_item_id": _artwork_item(conn, row),
    }


def _artwork_item(conn: sqlite3.Connection, row: sqlite3.Row) -> int | None:
    """An indexed track inside this folder finding, to render its cover from.

    A folder has no item of its own, so the picture has to come from something
    inside it. Display-only: the thumbnail route reads the file and writes to
    the prunable preview cache, and nothing is ever put beside the music.
    """
    if row["kind"] not in {"artwork-not-on-disk", "missing-artwork"} and not row[
        "kind"
    ].startswith("collection-"):
        return None
    found = conn.execute(
        "SELECT id FROM items WHERE root='library' AND missing_since IS NULL "
        "AND relpath LIKE ? ORDER BY relpath LIMIT 1",
        (f"{row['relpath'].rstrip('/')}/%",),
    ).fetchone()
    return int(found["id"]) if found else None


# The evidence field that names another place a finding is also about, and how
# to describe the set once you know what kind of thing it is.
#
# `Spans 27 items` was the old wording for all of them, and it is the kind of
# phrase that survives review because it is not wrong. It is just useless: it
# names no noun a person recognises, so the reader learns the number 27 and
# nothing else. What they need to know is that forty-five *tracks* are in
# twenty-seven *artist folders* — at which point the problem explains itself
# and the row needs no further reading.
GROUPED_FIELDS = ("folder", "also at")

GROUP_WORDING: dict[str, str] = {
    "collection-recognized": "{tracks} tracks across {count} artist folders",
    "collection-custom": "{tracks} tracks across {count} artist folders",
    "collection-loose": "{tracks} tracks across {count} folders",
    "split-album": "{tracks} tracks across {count} folders",
    "artist-split": "This artist is filed in {count} places",
    "duplicate": "{count} identical copies",
    "missing-artwork": "One album across {count} folders",
    "artwork-not-on-disk": "One album across {count} folders",
}
# When the kind is not listed above. Still a noun, still countable — never
# "items", which is what the code called them and no user ever would.
DEFAULT_WORDING = "This finding covers {count} folders"


def _spans(row: sqlite3.Row) -> dict[str, object]:
    """Every other place a grouped finding speaks for, the anchor first.

    Read back off the evidence the detector recorded rather than stored beside
    it, so the list and the reasoning cannot drift apart. The raw entries are
    needed rather than the humanised views, because the view keeps the label
    and drops the field name.
    """
    entries = _entries(row)
    if not entries:
        return {}
    facts = {
        entry.field: entry.detail
        for entry in entries
        if entry.source == "filesystem" and entry.field in {"tracks", "artists"}
    }
    for field in GROUPED_FIELDS:
        paths = [
            entry.detail
            for entry in entries
            if entry.source == "filesystem" and entry.field == field
        ]
        anchor = row["relpath"]
        paths = [anchor, *[path for path in paths if path != anchor]]
        if len(paths) < 2:
            continue
        template = GROUP_WORDING.get(row["kind"], DEFAULT_WORDING)
        # A template asking for a count the detector did not record would be a
        # KeyError on a page; falling back says less and always renders.
        try:
            label = template.format(count=len(paths), **facts)
        except (KeyError, IndexError):
            label = DEFAULT_WORDING.format(count=len(paths))
        return {"label": label, "paths": paths}
    return {}


def _column(row: sqlite3.Row, name: str) -> object:
    """A column that may not be in this particular query's result.

    Findings are read by more than one caller, and only the Review listing
    joins `items` for the size. Asking for it elsewhere should render nothing,
    not raise on a page.
    """
    try:
        return row[name]
    except (IndexError, KeyError):
        return None


def _entries(row: sqlite3.Row) -> list:
    from librairy.proposals import decode_evidence

    if not row["evidence"]:
        return []
    try:
        return list(decode_evidence(row["evidence"]))
    except Exception:  # noqa: BLE001 - a bad row renders plainly, never 500s
        return []


def _group_facts(row: sqlite3.Row) -> list[str]:
    """`45 tracks · 1.3 GB · 27 artists` — the shape of the thing, in one line.

    Every number here was measured during the audit and written into the
    finding's evidence. Nothing is recounted at render time: a folder finding
    that walked the tree on every page view would make Review slower the
    larger the library got, which is backwards.
    """
    facts: dict[str, str] = {}
    for entry in _entries(row):
        if entry.source == "filesystem":
            facts[entry.field] = entry.detail
    parts = []
    if tracks := facts.get("tracks"):
        parts.append(f"{tracks} tracks")
    if size := human_size(facts.get("total bytes")):
        parts.append(size)
    if artists := facts.get("artists"):
        parts.append(f"{artists} artists")
    if each := human_size(facts.get("each")):
        parts.append(f"{each} each")
    return parts


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


# --- storage opportunities ------------------------------------------------------
#
# A third list on the Review page, and deliberately the quietest one. A badly
# organised album needs attention; a 10 GB film that could be 6 GB is merely an
# opportunity, and giving the two the same visual weight would turn a page of
# problems into a page of suggestions nobody finishes.
#
# Its selection scope is its own. `opportunity_id` is never accepted by the
# inbox form or the audit toolbar, and separate tables plus separate endpoints
# make that structural rather than a convention somebody remembers.


def storage_view(conn: sqlite3.Connection) -> dict[str, object]:
    """What could be smaller, and what that would cost.

    Reads only. Rendering this page must not write, which is what keeps a page
    load from competing with the worker for SQLite's one writer lock.
    """
    from librairy import optimization

    rows = optimization.open_opportunities(conn)
    totals = optimization.summary(rows)
    return {
        "count": totals["count"],
        # Only lossless and lossy contribute. A remux saves nothing and is
        # offered for compatibility; adding it here would be the same
        # dishonesty as calling it an optimization.
        "estimated_saving": human_size(totals["estimated_saving"]),
        "has_saving": totals["estimated_saving"] > 0,
        "compatibility_only": totals["compatibility_only"],
        "protected": totals["protected"],
        "by_quality": _quality_lines(totals["by_quality"]),
        "rows": [_storage_row(row) for row in rows],
    }


def _quality_lines(by_quality: dict[str, int]) -> list[str]:
    """`2 lossless · 1 lossy · 1 compatibility-only suggestion`, in a fixed order.

    Singular and plural are written out rather than derived. `lossless` and
    `lossy` are adjectives and do not take an `s`; deriving one produced
    "2 losslesss", which is the kind of thing that survives review because
    nobody reads a word they wrote.
    """
    from librairy.optimization import DERIVATIVE, LOSSLESS, LOSSY, REMUX

    words = {
        LOSSLESS: ("lossless", "lossless"),
        LOSSY: ("lossy", "lossy"),
        REMUX: ("compatibility-only suggestion", "compatibility-only suggestions"),
        DERIVATIVE: ("derivative", "derivatives"),
    }
    lines = []
    for quality in (LOSSLESS, LOSSY, REMUX, DERIVATIVE):
        count = by_quality.get(quality, 0)
        if not count:
            continue
        singular, plural = words[quality]
        lines.append(f"{count} {singular if count == 1 else plural}")
    return lines


def _storage_row(row: sqlite3.Row) -> dict[str, object]:
    import json

    from librairy.optimization import CLASS_LABEL, CLASS_MEANING, COST_LABEL, REMUX

    saving = row["current_bytes"] - row["estimated_bytes"]
    percent = round(saving / row["current_bytes"] * 100) if row["current_bytes"] else 0
    try:
        facts = [tuple(pair) for pair in json.loads(row["facts"] or "[]")]
    except (TypeError, ValueError):
        facts = []
    return {
        "id": row["id"],
        "name": PurePosixPath(row["relpath"]).name,
        "relpath": row["relpath"],
        "kind": row["kind"],
        "quality": row["quality"],
        "quality_label": CLASS_LABEL.get(row["quality"], row["quality"].upper()),
        "quality_meaning": CLASS_MEANING.get(row["quality"], ""),
        "size_label": human_size(row["current_bytes"]),
        # `Estimated`, never `Actual`. Nothing has encoded anything yet, and a
        # field that swaps meaning once a job runs is a field nobody can read.
        "estimated_label": human_size(row["estimated_bytes"]),
        "saving_label": human_size(saving),
        "saving_percent": percent,
        # A remux genuinely saves nothing, and the row says so rather than
        # hiding the line — `0 B` is a fact, and `unknown` would not be.
        "is_compatibility": row["quality"] == REMUX,
        "change": f"{row['from_label']} → {row['to_label']}",
        "compute": COST_LABEL.get(row["compute"], row["compute"].title()),
        "reason": row["reason"],
        "summary": row["summary"],
        "facts": facts,
        "protected_by": row["protected_by"],
        "eligible": not row["protected_by"],
    }


OPPORTUNITY_BULK_ACTIONS = ("dismiss", "queue")


def apply_opportunity_action(
    conn: sqlite3.Connection, action: str, opportunity_ids: list[int]
) -> str:
    """Bulk action over storage opportunities, and only over those.

    Deliberately a separate function with a separate id list rather than a
    branch inside `apply_review_action`. An `opportunity_id` and a
    `proposal_id` are both small integers, and the only thing stopping one
    reaching the other's handler is that they never share one.

    Ineligible rows are counted and explained rather than skipped: "3 queued,
    1 protected" is an answer, and a silently shorter result is not.
    """
    from librairy import optimization

    if action not in OPPORTUNITY_BULK_ACTIONS:
        raise ValueError(f"unknown opportunity action: {action}")
    if not opportunity_ids:
        return "Nothing was selected."
    if action == "queue":
        return _queue_selected(conn, opportunity_ids)
    dismissed = sum(1 for row_id in opportunity_ids if optimization.dismiss(conn, row_id))
    missing = len(opportunity_ids) - dismissed
    if missing:
        return (
            f"{dismissed} will not be suggested again. "
            f"{missing} had already been answered."
        )
    return f"{dismissed} will not be suggested again."


# --- the optimization queue -------------------------------------------------------


QUEUE_ACTIONS = ("queue", "dismiss")


def queue_data(conn: sqlite3.Connection, settings: Settings | None = None) -> dict:
    """The queue page: what is waiting, and precisely what for.

    Reads only. Every waiting reason is a stored fact the worker wrote on its
    last cycle, never a recomputation at render time — a page that re-decided
    eligibility on every load would disagree with the worker the moment either
    of them blinked.
    """
    from librairy import optimization_queue as queue
    from librairy.optimization_exec import LOW

    rows = [_queue_row(row, conn) for row in queue.jobs(conn)]
    live = [row for row in rows if row["live"]]
    return {
        # `ready` is excluded here because it has its own section above, and a
        # row in both places is the same job asking to be answered twice.
        "jobs": [
            row for row in rows if row["state"] not in {queue.CANCELLED, queue.READY}
        ],
        # Kept in its own section. A finished result is a different question
        # from a job still waiting its turn, and mixing them means the one
        # thing needing an answer is buried among the ones that do not.
        "ready_jobs": [row for row in rows if row["state"] == queue.READY],
        "running": sum(1 for row in live if row["state"] == queue.RUNNING),
        "waiting": sum(
            1 for row in live if row["state"] in {queue.QUEUED, queue.WAITING}
        ),
        "ready": sum(1 for row in live if row["state"] == queue.READY),
        "concurrency": queue.MAX_CONCURRENT,
        "resource_use": LOW.label,
        "window": _window_label(conn),
        #  What has actually come of the optimizations already decided. Counts
        #  first, because they are always true; the byte total only ever
        #  describes originals LibrAIry can see are gone.
        "outcomes": _outcome_summary(conn),
    }


def _outcome_summary(conn: sqlite3.Connection) -> dict[str, object]:
    """Adopted, preserved, queued, removed — and one number, carefully named.

    "Net reduction realized" is the only total on this page that describes disk
    space that actually came back. It is deliberately not summed with the
    estimates or with the representation reductions above it: three numbers
    that mean three things, added together, produce a fourth that means
    nothing.
    """
    from librairy.optimization_disposal import outcomes

    counts = outcomes(conn)
    realized = counts["realized_bytes"]
    return {
        **counts,
        "realized_label": human_size(abs(realized)),
        "realized_negative": realized < 0,
        #  Nothing removed means nothing realized, and the page says so rather
        #  than showing "0 B" beside three encouraging numbers.
        "has_realized": bool(counts["removed"]),
    }


def _window_label(conn: sqlite3.Connection) -> str:
    from librairy.worker import _window

    start, end = _window(conn, None)
    return f"{start}–{end}"


# Below this, a conversion that worked is still not worth keeping. The encoder
# did its job; the job was not worth doing, and saying "completed" without
# saying so would let a 3% result look like a success.
LOW_PAYOFF_PERCENT = 10


def _clock_label(seconds: float) -> str:
    seconds = int(seconds or 0)
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60}m {seconds % 60:02d}s"


def _storage(row: sqlite3.Row) -> dict[str, object]:
    """The storage quantities, from the one helper that knows what they mean.

    Never computed here. `optimization_storage` exists because these numbers
    appear in six places, and recomputing them locally is exactly how "saved
    338 MB" ends up on a screen while 842 MB of original sits in Quarantine.

    A technically valid encode can also still be useless: an estimate of 35%
    against an actual of 3% is a successful run of the encoder and a failed
    optimization, and the page has to be able to say the second thing.
    """
    from librairy.optimization_storage import ADOPTED, READY, storage_effect

    source = int(row["source_bytes"] or 0)
    actual = int(row["actual_bytes"] or 0)
    if not source or not actual:
        return {"known": False}
    # Both copies exist either way; which one is *active* is what differs, and
    # it changes none of these numbers.
    state = ADOPTED if row["state"] == "adopted" else READY
    effect = storage_effect(source, actual, state)
    reduction = effect.representation_reduction_bytes
    return {
        "known": True,
        "reduction_bytes": reduction,
        "reduction_label": human_size(abs(reduction)),
        "percent": round(reduction / source * 100) if source else 0,
        "negative": reduction < 0,
        "extra_label": human_size(effect.current_extra_storage_bytes),
        # Zero for the life of this feature, and shown anyway: a person
        # deserves to see that the answer is nothing rather than infer it.
        "reclaimed_label": human_size(effect.reclaimed_now_bytes) or "0 B",
        "freed_if_removed_label": human_size(effect.bytes_freed_if_original_removed),
        "final_reduction_label": human_size(abs(effect.final_net_reduction_bytes)),
        "low_payoff": not effect.worth_it,
        "estimated_label": human_size(max(0, source - int(row["estimated_bytes"] or 0))),
    }


def _queue_row(
    row: sqlite3.Row, conn: sqlite3.Connection | None = None
) -> dict[str, object]:
    from librairy import optimization_queue as queue
    from librairy.optimization import CLASS_LABEL
    from librairy.optimization_adopt import (
        ADOPTED,
        APPLYING,
        WAITING_FOR_COMMIT,
        active_adoption,
        adoption_state,
    )

    reason = row["wait_reason"] or ""
    #  The plan outranks the column. A verified result whose adoption is
    #  approved is not "ready for review" any more — the decision has been
    #  taken and is waiting for Commit — and `optimization_jobs.state` has no
    #  way to say so.
    effective = adoption_state(conn, row["id"]) if conn is not None else ""
    plan = active_adoption(conn, row["id"]) if conn is not None else None
    #  Cheap facts only. Whether adoption would actually succeed depends on
    #  hashing an 842 MB original and its copy, which is not something to do on
    #  every page render — `adoption_preflight` runs on the POST and returns a
    #  refusal a person can read.
    can_adopt = (
        row["state"] == queue.READY
        and row["verified"] == "passed"
        and bool((row["output_fingerprint"] or "").strip())
        and effective == ""
    )
    return {
        "effective_state": effective,
        "waiting_for_commit": effective == WAITING_FOR_COMMIT,
        "applying": effective == APPLYING,
        "adopted": effective == ADOPTED,
        "adoption_plan_id": plan["id"] if plan is not None else "",
        "can_adopt": can_adopt,
        #  Cancelling is only for a decision that has not started moving files.
        "can_cancel_request": effective == WAITING_FOR_COMMIT,
        "id": row["id"],
        "name": PurePosixPath(row["relpath"]).name,
        "relpath": row["relpath"],
        "state": row["state"],
        "state_label": queue.STATE_LABEL.get(row["state"], row["state"].title()),
        "live": row["state"] in queue.LIVE_STATES,
        "quality": row["quality"],
        "quality_label": CLASS_LABEL.get(row["quality"], row["quality"].upper()),
        "change": f"{row['from_label']} → {row['to_label']}",
        "size_label": human_size(row["source_bytes"]),
        "estimated_label": human_size(row["estimated_bytes"]),
        # Kept apart from the estimate, always. Overwriting one with the other
        # would destroy the only way to find out whether the advisor is good.
        "actual_label": human_size(row["actual_bytes"]) if row["actual_bytes"] else "",
        "wait_reason": reason,
        # Progress as FFmpeg reported it. Without a known duration there is no
        # honest percentage, so the row says elapsed time instead of inventing
        # one from the output file's size.
        "progress": round(row["progress"] or 0),
        "has_progress": bool(row["duration_seconds"]) and bool(row["out_time_seconds"]),
        "elapsed_label": _clock_label(row["out_time_seconds"] or 0),
        "runtime_label": _clock_label(row["runtime_seconds"] or 0),
        "message": row["message"] or "",
        "verified": row["verified"] or "",
        "storage": _storage(row),
        "is_running": row["state"] == queue.RUNNING,
        "is_verifying": row["state"] == queue.VERIFYING,
        "is_ready": row["state"] == queue.READY,
        "is_failed": row["state"] == queue.FAILED,
        "can_cancel": row["state"] in queue.ACTIVE_STATES,
        # The words, not the token. A stored reason is for the code; a person
        # reading the page needs a sentence.
        "wait_text": queue.WAIT_TEXT.get(reason, ""),
        # Waiting is normal, so it is never styled as a failure.
        "is_waiting": row["state"] in {queue.QUEUED, queue.WAITING},
        "is_stale": row["state"] == queue.STALE,
        # Pressing `Run now` changes nothing a person can see otherwise: the
        # job stays queued, because nothing starts until the worker's next idle
        # cycle. Without this the button reads as having done nothing at all.
        "forced": row["run_policy"] == queue.FORCED,
        "can_run_now": (
            row["state"] in {queue.QUEUED, queue.WAITING}
            and row["run_policy"] != queue.FORCED
        ),
        #  Exactly what `queue.cancel` will accept. Two lists that were meant
        #  to agree did not: the checkbox allowed a stale row the update then
        #  refused, and disallowed a failed row nothing else could clear.
        "can_remove": row["state"] in queue.REMOVABLE_STATES,
        "queued_at": row["queued_at"],
    }


QUEUE_ROW_ACTIONS = (
    "run-now",
    "cancel",
    "discard",
    #  Approval only. Neither of these touches a file.
    "use-optimized",
    "cancel-request",
)


def apply_queue_action(
    conn: sqlite3.Connection,
    action: str,
    job_ids: list[int],
    settings: Settings | None = None,
) -> str:
    """Queue-page actions, over `job_id` and nothing else.

    A fourth field name for a fourth workflow. See `apply_opportunity_action`
    for why these are separate functions rather than one with a branch.
    """
    from librairy import optimization_queue as queue

    # A refusal a person can read. A form posted without its submit button —
    # scripted, or `Enter` in an odd place — arrived here as `action=""` and got
    # back "unknown queue action: " with a trailing colon and nothing after it.
    if not action:
        raise ValueError("Choose an action first.")
    if action not in ("remove", *QUEUE_ROW_ACTIONS):
        raise ValueError("That is not something this page can do.")
    if not job_ids:
        return "Nothing was selected."
    if action == "run-now":
        return _run_now(conn, job_ids)
    if action == "cancel":
        return _cancel_running(conn, settings, job_ids)
    if action == "discard":
        return _discard_results(conn, settings, job_ids)
    if action == "use-optimized":
        return _use_optimized(conn, settings, job_ids)
    if action == "cancel-request":
        return _cancel_adoption_requests(conn, job_ids)
    removed = sum(1 for job_id in job_ids if queue.cancel(conn, job_id))
    if removed != len(job_ids):
        return (
            f"{removed} removed. "
            f"{len(job_ids) - removed} is running or has a result to decide about."
        )
    return f"{removed} removed from the queue."


def _use_optimized(
    conn: sqlite3.Connection, settings: Settings | None, job_ids: list[int]
) -> str:
    """Approve an adoption. Moves nothing.

    The whole of the work is `plan_adoption`, which writes two operations and
    approves them. No file is touched here and none could be: this process
    holds no lock, and the executor is the only thing in the application that
    moves a user's file.

    Refusals come back as sentences because they are all things a person can
    act on — the original changed, something is already at the destination, the
    result is no longer the one that was verified.
    """
    from librairy.optimization_adopt import plan_adoption
    from librairy.optimization_preflight import Refusal

    if settings is None:  # pragma: no cover - the routes always pass it
        return "Approving needs the application settings."
    approved, refused = 0, []
    for job_id in job_ids:
        outcome = plan_adoption(conn, settings, job_id)
        if isinstance(outcome, Refusal):
            refused.append(outcome.message)
        else:
            approved += 1
    if approved and not refused:
        return (
            "Approved. Nothing has moved yet — it happens when you commit."
            if approved == 1
            else f"{approved} approved. Nothing has moved yet."
        )
    if approved:
        return f"{approved} approved. {len(refused)} refused: {refused[0]}"
    return refused[0] if refused else "Nothing was selected."


def _cancel_adoption_requests(conn: sqlite3.Connection, job_ids: list[int]) -> str:
    """Take back an approval before Commit. Deliberately not called Undo.

    Undo reverses files that moved; this reverses a decision about files that
    did not. The same withdrawal machinery a correction uses, so there is one
    implementation and one `plan_withdrawals` record.
    """
    from librairy.optimization_adopt import active_adoption, cancel_adoption

    cancelled = 0
    for job_id in job_ids:
        plan = active_adoption(conn, job_id)
        if plan is not None and cancel_adoption(conn, plan["id"]):
            cancelled += 1
    if not cancelled:
        return "Those results are not waiting for Commit."
    return (
        "Request cancelled. Nothing was moved."
        if cancelled == 1
        else f"{cancelled} requests cancelled. Nothing was moved."
    )


def _run_now(conn: sqlite3.Connection, job_ids: list[int]) -> str:
    """Ask for a job to skip the clock, and nothing else.

    This does not start an encoder — a request handler starting FFmpeg is the
    one thing the execution design forbids, because a web process holds no
    lock, owns no child and cannot be polled. It records that the clock no
    longer applies to this job, and the worker picks it up on its next idle
    cycle, still behind every other gate: the fingerprint, protected roots,
    concurrency, the disk reserve, system load and the resource policy.
    """
    from librairy import optimization_queue as queue

    changed = 0
    for job_id in job_ids:
        changed += conn.execute(
            "UPDATE optimization_jobs SET run_policy=?, wait_reason='',"
            " updated_at=? WHERE id=? AND state IN (?, ?)",
            (queue.FORCED, utc_now(), job_id, queue.QUEUED, queue.WAITING),
        ).rowcount
    if not changed:
        return "Those jobs are not waiting to start."
    return f"{changed} will start as soon as the machine is free."


def _cancel_running(
    conn: sqlite3.Connection, settings: Settings | None, job_ids: list[int]
) -> str:
    """Stop an encode that has started, and clear what it had written.

    Only reaches a process this application started; see
    `optimization_process.stop`.
    """
    from librairy import optimization_process as procs
    from librairy import optimization_queue as queue

    if settings is None:  # pragma: no cover - the routes always pass it
        return "Cancelling needs the application settings."
    stopped = 0
    for job_id in job_ids:
        row = conn.execute(
            "SELECT state FROM optimization_jobs WHERE id=?", (job_id,)
        ).fetchone()
        if row is None or row["state"] not in queue.ACTIVE_STATES:
            continue
        procs.stop(
            conn, settings, job_id,
            state=queue.CANCELLED,
            message="Cancelled. The original was never changed.",
        )
        stopped += 1
    if not stopped:
        return "Nothing selected was running."
    return f"{stopped} stopped. Nothing on disk was changed."


def _discard_results(
    conn: sqlite3.Connection, settings: Settings | None, job_ids: list[int]
) -> str:
    """Throw away a converted file. The original was never touched.

    Deliberately the *only* action offered on a finished result while adoption
    does not exist. A second button reading "Keep original" would look like a
    choice and would do nothing at all — the original is already what the
    library holds.
    """
    from librairy import optimization_queue as queue

    if settings is None:  # pragma: no cover - the routes always pass it
        return "Discarding needs the application settings."
    discarded = 0
    for job_id in job_ids:
        row = conn.execute(
            "SELECT state FROM optimization_jobs WHERE id=?", (job_id,)
        ).fetchone()
        if row is None or row["state"] != queue.READY:
            continue
        queue.clear_staging(settings, job_id)
        conn.execute(
            "UPDATE optimization_jobs SET state=?, message=?, staging_dir='',"
            " finished_at=?, updated_at=? WHERE id=?",
            (
                queue.CANCELLED, "Result discarded. The original was never changed.",
                utc_now(), utc_now(), job_id,
            ),
        )
        discarded += 1
    if not discarded:
        return "Nothing selected was ready for review."
    return f"{discarded} discarded. Your original files were never changed."


def _queue_selected(conn: sqlite3.Connection, opportunity_ids: list[int]) -> str:
    """Queue what can be queued, and say why the rest could not.

    A mixed selection that quietly queued three of four is the worst possible
    outcome on a page that is about to spend an hour of CPU. Every refusal
    carries its own reason, and identical reasons are counted together so the
    sentence stays readable when twenty rows are protected.
    """
    from collections import Counter

    from librairy import optimization_queue as queue

    queued = 0
    refused: Counter[str] = Counter()
    for opportunity_id in opportunity_ids:
        try:
            queue.enqueue(conn, opportunity_id)
        except queue.QueueRefused as exc:
            refused[str(exc)] += 1
        else:
            queued += 1
    if not refused:
        return f"{queued} queued. Nothing has been converted yet."
    parts = [f"{count} because {reason}" for reason, count in refused.most_common()]
    return f"{queued} queued. Not queued: " + "; ".join(parts) + "."
