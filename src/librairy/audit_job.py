"""A whole-library audit, run in slices, behind the inbox.

Pressing *Audit the whole library* used to hold the HTTP request open for the
whole reconciliation. That was survivable while the audit was filesystem and
tags; it stops being survivable the moment it asks MusicBrainz about four
hundred albums. So the button now writes a row and returns, and the worker
that already exists picks it up.

Three properties matter more than speed:

**Inbox work always wins.** The worker runs its own cycle first — scan, dedup,
analyse, content, backup — and only then, only if that cycle found nothing to
do, spends a bounded slice on the audit. A newly dropped file is never behind
a library reconciliation. This is why the audit is a row in a table and not a
thread: a thread would have to be throttled against the rest of the worker,
and a row is simply not looked at until the important work is finished.

**A slice is bounded by time, not by count.** `advance()` runs until its
deadline and then returns, leaving the stage where it was. Nothing is lost,
because every expensive answer is already persisted — `catalog_identity` for
what a provider said, `items.fingerprint` for what a file is — so resuming a
stage re-reads from cache rather than re-asking. The cache *is* the resume
mechanism, which is why there is no cursor to keep in step with anything.

**Stopping is safe by construction.** An audit only ever reads the library, so
cancellation cannot leave a job half done in any sense that matters. The flag
is read between stages and between items, and the run ends where it stands.

There is no daemon, no schedule and no second process. Nothing here starts
itself; a run exists because somebody pressed a button.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from librairy.planner import utc_now

if TYPE_CHECKING:  # pragma: no cover - typing only
    from librairy.config import Settings

LOGGER = logging.getLogger(__name__)

QUEUED, RUNNING, COMPLETE, FAILED, CANCELLED = (
    "queued",
    "running",
    "complete",
    "failed",
    "cancelled",
)
LIVE_STATES = (QUEUED, RUNNING)

# The order stages run in, and the words the page uses for them. Cheap and
# certain first: nothing should wait on MusicBrainz to find out it has a
# `.DS_Store`. Each stage's findings are recorded as it finishes, so a run
# that is cancelled half way still leaves everything it had already concluded.
STAGES = (
    ("scan", "Scanning"),
    ("metadata", "Reading metadata"),
    ("structure", "Structure and convention"),
    ("catalogs", "Catalogs"),
    ("artwork", "Artwork"),
    ("duplicates", "Duplicates"),
    ("ai", "AI"),
    ("record", "Finishing"),
)
STAGE_ORDER = [name for name, _ in STAGES]
STAGE_LABEL = dict(STAGES)

# How long one slice may hold the worker. Short enough that a file dropped in
# the inbox waits seconds rather than minutes; long enough that a slice does
# useful work rather than paying setup costs over and over.
SLICE_SECONDS = 6.0


@dataclass
class Counters:
    """What to say instead of "working".

    Deliberately plain integers with plain names: every one of these is a
    number a person could check by hand, which is the point — a progress
    display nobody can verify is decoration.
    """

    files_seen: int = 0
    files_checked: int = 0
    albums: int = 0
    albums_identified: int = 0
    collections: int = 0
    collections_judged: int = 0
    catalog_requests: int = 0
    catalog_matches: int = 0
    artwork_checked: int = 0
    artwork_found: int = 0
    duplicate_clusters: int = 0
    ai_candidates: int = 0
    ai_calls: int = 0
    ai_answers: int = 0
    ai_unavailable: int = 0
    findings: int = 0
    per_root: dict[str, list[int]] = field(default_factory=dict)

    def as_json(self) -> str:
        return json.dumps(self.__dict__, sort_keys=True)

    @classmethod
    def from_json(cls, payload: str) -> Counters:
        try:
            data = json.loads(payload or "{}")
        except ValueError:
            return cls()
        known = {key: value for key, value in data.items() if key in cls().__dict__}
        return cls(**known)


# --- the row ------------------------------------------------------------------


def enqueue(conn: sqlite3.Connection, scope: str = "") -> int:
    """Ask for an audit. Asking twice for the same scope asks once.

    An audit is idempotent and a queued one has not started, so a second press
    is the same question — stacking them would only mean waiting through the
    same work twice.
    """
    existing = conn.execute(
        "SELECT id FROM audit_runs WHERE state IN (?, ?) AND scope=? ORDER BY id LIMIT 1",
        (QUEUED, RUNNING, scope),
    ).fetchone()
    if existing is not None:
        return int(existing["id"])
    cursor = conn.execute(
        "INSERT INTO audit_runs(scope, state, stage, counters, requested_at) "
        "VALUES (?, ?, ?, '{}', ?)",
        (scope, QUEUED, STAGE_ORDER[0], utc_now()),
    )
    return int(cursor.lastrowid)


def current(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """The run worth showing: the live one, else the most recent finished."""
    row = conn.execute(
        "SELECT * FROM audit_runs WHERE state IN (?, ?) ORDER BY id LIMIT 1",
        LIVE_STATES,
    ).fetchone()
    if row is not None:
        return row
    return conn.execute("SELECT * FROM audit_runs ORDER BY id DESC LIMIT 1").fetchone()


def cancel(conn: sqlite3.Connection, run_id: int | None = None) -> bool:
    """Ask a run to stop. A queued run stops immediately; a running one stops
    at its next check, which is never more than one album away."""
    row = (
        conn.execute("SELECT * FROM audit_runs WHERE id=?", (run_id,)).fetchone()
        if run_id is not None
        else conn.execute(
            "SELECT * FROM audit_runs WHERE state IN (?, ?) ORDER BY id LIMIT 1", LIVE_STATES
        ).fetchone()
    )
    if row is None or row["state"] not in LIVE_STATES:
        return False
    if row["state"] == QUEUED:
        conn.execute(
            "UPDATE audit_runs SET state=?, finished_at=? WHERE id=?",
            (CANCELLED, utc_now(), row["id"]),
        )
    else:
        conn.execute("UPDATE audit_runs SET cancel_requested=1 WHERE id=?", (row["id"],))
    return True


def _save(conn: sqlite3.Connection, run_id: int, **columns) -> None:
    assignments = ", ".join(f"{name}=?" for name in columns)
    conn.execute(
        f"UPDATE audit_runs SET {assignments} WHERE id=?",  # noqa: S608 - names are literals
        (*columns.values(), run_id),
    )


def _cancelled(conn: sqlite3.Connection, run_id: int) -> bool:
    row = conn.execute("SELECT cancel_requested FROM audit_runs WHERE id=?", (run_id,)).fetchone()
    return bool(row and row["cancel_requested"])


# --- running it ---------------------------------------------------------------


@dataclass
class SliceResult:
    """What one slice did, for the worker's own summary."""

    ran: bool = False
    stage: str = ""
    finished: bool = False


# What a run has gathered so far, kept between slices of the same run.
#
# In memory rather than in the database, and that is a deliberate limit: the
# view is a few hundred kilobytes of paths and tags, it belongs to one worker
# process, and serialising it would cost more than re-reading it. If the
# worker restarts mid-run the entry is gone and the stage starts again —
# correct, because every stage is idempotent, and cheap, because the expensive
# answers are in `catalog_identity` and the fingerprints are in `items`.
_carried: dict[int, object] = {}


def _forget(run_id: int) -> None:
    _carried.pop(run_id, None)


def advance(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    seconds: float = SLICE_SECONDS,
    now=time.monotonic,
) -> SliceResult:
    """Do a bounded amount of audit work. Safe to call when there is none.

    Returns without touching anything if no run is waiting, which is what
    makes it cheap enough for the worker to ask on every cycle.
    """
    row = conn.execute(
        "SELECT * FROM audit_runs WHERE state IN (?, ?) ORDER BY id LIMIT 1", LIVE_STATES
    ).fetchone()
    if row is None:
        return SliceResult()
    run_id = int(row["id"])
    if row["cancel_requested"]:
        _save(conn, run_id, state=CANCELLED, finished_at=utc_now())
        _forget(run_id)
        return SliceResult(ran=True, stage=row["stage"], finished=True)
    if row["state"] == QUEUED:
        _save(conn, run_id, state=RUNNING, started_at=utc_now())

    deadline = now() + seconds
    counters = Counters.from_json(row["counters"])
    stage = row["stage"] if row["stage"] in STAGE_ORDER else STAGE_ORDER[0]
    from librairy.audit_stages import Context, run_stage

    context = _carried.get(run_id)
    if context is None:
        context = Context(
            conn=conn,
            settings=settings,
            scope=row["scope"],
            counters=counters,
            deadline=deadline,
            now=now,
            cancelled=lambda: _cancelled(conn, run_id),
        )
        _carried[run_id] = context
    else:
        # Same run, next slice: keep the gathered view, the tags already read
        # and the findings already reached, and move the deadline forward.
        context.counters = counters
        context.deadline = deadline
        context.now = now
    try:
        while stage in STAGE_ORDER:
            done = run_stage(stage, context)
            _save(conn, run_id, counters=counters.as_json(), stage=stage)
            if context.cancelled():
                _save(conn, run_id, state=CANCELLED, finished_at=utc_now())
                _forget(run_id)
                return SliceResult(ran=True, stage=stage, finished=True)
            if not done:
                # Out of time inside the stage. Leave it where it is; the next
                # slice resumes, reading every expensive answer from cache.
                break
            index = STAGE_ORDER.index(stage) + 1
            if index >= len(STAGE_ORDER):
                _save(
                    conn,
                    run_id,
                    state=COMPLETE,
                    stage=STAGE_ORDER[-1],
                    counters=counters.as_json(),
                    finished_at=utc_now(),
                )
                _forget(run_id)
                return SliceResult(ran=True, stage=stage, finished=True)
            stage = STAGE_ORDER[index]
            _save(conn, run_id, stage=stage)
            if now() >= deadline:
                break
    except Exception as exc:  # noqa: BLE001 - a failed audit must not stop the worker
        LOGGER.exception("audit run %s failed in stage %s", run_id, stage)
        _save(
            conn,
            run_id,
            state=FAILED,
            error=f"{exc.__class__.__name__}: {exc}"[:400],
            counters=counters.as_json(),
            finished_at=utc_now(),
        )
        _forget(run_id)
        return SliceResult(ran=True, stage=stage, finished=True)
    return SliceResult(ran=True, stage=stage)


# --- describing it ------------------------------------------------------------


def progress(conn: sqlite3.Connection) -> dict[str, object] | None:
    """What the page shows. None when no audit has ever been asked for."""
    row = current(conn)
    if row is None:
        return None
    counters = Counters.from_json(row["counters"])
    stage = row["stage"] if row["stage"] in STAGE_ORDER else STAGE_ORDER[0]
    index = STAGE_ORDER.index(stage)
    live = row["state"] in LIVE_STATES
    # Stage position, not file position: the stages cost wildly different
    # amounts and a bar that pretended otherwise would sit at 90% through the
    # slowest half. Files checked is reported as a number beside it, which is
    # the honest form of the same information.
    percent = 100 if row["state"] == COMPLETE else round(index / len(STAGE_ORDER) * 100)
    return {
        "id": row["id"],
        "state": row["state"],
        "live": live,
        "scope": row["scope"] or "the whole library",
        "stage": stage,
        "stage_label": STAGE_LABEL.get(stage, stage),
        "percent": percent,
        "error": row["error"],
        "counters": counters,
        "rows": _counter_rows(counters),
        "requested_at": row["requested_at"],
        "finished_at": row["finished_at"],
    }


def _counter_rows(counters: Counters) -> list[tuple[str, str]]:
    """Only what actually happened. A run with no catalog requests does not
    get a line saying it made none — an empty row reads as a failure."""
    rows: list[tuple[str, str]] = []
    for label, count in sorted((counters.per_root or {}).items()):
        checked, total = (count + [0, 0])[:2] if isinstance(count, list) else (0, 0)
        rows.append((label, f"{checked} / {total}"))
    for label, value in (
        ("Albums", counters.albums),
        ("Collections", counters.collections),
        ("Catalog requests", counters.catalog_requests),
        ("Catalog matches", counters.catalog_matches),
        ("Artwork checked", counters.artwork_checked),
        ("Duplicate sets", counters.duplicate_clusters),
        ("Issues found", counters.findings),
    ):
        if value:
            rows.append((label, str(value)))
    # The AI line is the exception to "only what happened". `AI candidates 0`
    # is the claim that nothing was ambiguous enough to need a model, and a
    # run that silently omits the line is indistinguishable from one whose AI
    # stage is a stub — which this one was, and said nothing about it.
    if counters.ai_candidates or counters.ai_calls:
        rows.append(("Sent to AI", f"{counters.ai_answers} / {counters.ai_candidates}"))
    return rows


def counters_by_root(files: list[str], checked: int) -> dict[str, list[int]]:
    """`{"Music": [48, 48], "Photos": [31, 89]}` — per top-level folder."""
    totals: dict[str, int] = defaultdict(int)
    for relpath in files:
        totals[relpath.split("/", 1)[0]] += 1
    done = checked
    result: dict[str, list[int]] = {}
    for name, total in sorted(totals.items()):
        taken = min(total, max(0, done))
        result[name] = [taken, total]
        done -= taken
    return result
