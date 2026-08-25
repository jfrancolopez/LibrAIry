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
    #  Reads the filed documents nobody has read yet, and groups the ones that
    #  carry the same ISBN or DOI. Its own stage because it opens files — two
    #  subprocesses each — which is work the duplicate stage advertises that it
    #  does not do.
    ("documents", "Documents"),
    ("duplicates", "Duplicates"),
    #  Groups of files that *resemble* each other, from the pairs czkawka
    #  already wrote. Its own stage, after the exact matches, because the two
    #  answer different questions: byte-identical copies belong to the
    #  duplicate workflow, which knows what rmlint said.
    #
    #  It was missing for four releases and the omission was invisible, because
    #  `audit.detect` gates similar media behind a connection and the staged
    #  runner calls it without one. The photo comparison existed, worked, and
    #  was only ever reachable from a direct call.
    ("similar", "Similar media"),
    #  Reads capture metadata for pictures nobody has measured, and pairs the
    #  ones the metadata proves belong together — a RAW with its JPEG, the two
    #  halves of a Live Photo. Its own stage because it opens files, which is
    #  work the stages around it advertise that they do not do.
    ("companions", "Photo companions"),
    # Cheap on purpose: cached probe data and arithmetic. This stage finds
    # things that *could* be smaller and records them. It never encodes
    # anything — pressing Audit must not make a NAS start transcoding.
    ("storage", "Storage opportunities"),
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
    artwork_total: int = 0
    artwork_found: int = 0
    duplicate_clusters: int = 0
    similar_pairs: int = 0
    similar_groups: int = 0
    photos_measured: int = 0
    companions_found: int = 0
    storage_checked: int = 0
    storage_total: int = 0
    storage_probes: int = 0
    storage_opportunities: int = 0
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


# Three tiers, and the first one is not in this table at all.
#
#   1. The inbox and anything the user is actively doing. Never represented
#      here, because the worker does not reach this module on a cycle that
#      changed anything — the ordering in `Worker.run_once` is the guarantee.
#   2. A targeted audit. Somebody pressed Audit on a folder and is probably
#      looking at the page, so it goes before the sweep.
#   3. The whole library. Maintenance, and it can wait for both of the above.
#
# Expressed as SQL rather than a column so there is nothing to migrate and
# nothing that can disagree with itself: every query that picks "the run to
# work on" or "the run to show" orders by this same clause.
PRIORITY_ORDER = "CASE WHEN scope='' THEN 3 ELSE 2 END, id"


def current(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """The run worth showing: the live one, else the most recent finished."""
    row = conn.execute(
        f"SELECT * FROM audit_runs WHERE state IN (?, ?) ORDER BY {PRIORITY_ORDER} LIMIT 1",  # noqa: S608
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
            f"SELECT * FROM audit_runs WHERE state IN (?, ?) ORDER BY {PRIORITY_ORDER} LIMIT 1",  # noqa: S608
            LIVE_STATES,
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
        f"SELECT * FROM audit_runs WHERE state IN (?, ?) ORDER BY {PRIORITY_ORDER} LIMIT 1",  # noqa: S608
        LIVE_STATES,
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
    live = row["state"] in LIVE_STATES
    done, total = _stage_fraction(stage, counters)
    percent = 100 if row["state"] == COMPLETE else _percent(done, total)
    return {
        "id": row["id"],
        "state": row["state"],
        "live": live,
        "yielding": live and _yielding(conn),
        "targeted": bool(row["scope"]),
        "scope": row["scope"] or "the whole library",
        "stage": stage,
        "stage_label": STAGE_LABEL.get(stage, stage),
        "stage_number": STAGE_ORDER.index(stage) + 1,
        "stage_count": len(STAGE_ORDER),
        # None where no honest fraction exists. The template draws no bar
        # then, and says which stage is running instead.
        "percent": percent,
        "stage_detail": _stage_detail(stage, done, total),
        "error": row["error"],
        "counters": counters,
        "rows": _counter_rows(counters),
        # Per-tool, so "the catalog tier ran" is a claim you can check rather
        # than one you have to take. A stage name proves nothing; `MusicBrainz
        # 1 request · 0 matches` proves a request left the machine.
        "tools": _tool_rows(counters),
        "requested_at": row["requested_at"],
        "finished_at": row["finished_at"],
    }


# What each stage is counting through, when it is counting through anything.
# Three of them are not: scanning is one directory walk, structure is one pass
# over data already in memory, and finishing is a single write. A bar on those
# would be an animation, not a measurement.
STAGE_FRACTIONS: dict[str, tuple[str, str, str]] = {
    "metadata": ("files_checked", "files_seen", "{done} of {total} files read"),
    "catalogs": ("collections_judged", "collections", "{done} of {total} collections checked"),
    "artwork": ("artwork_checked", "artwork_total", "{done} of {total} albums checked for artwork"),
    "storage": ("storage_checked", "storage_total", "{done} of {total} media files checked"),
    "ai": ("ai_calls", "ai_candidates", "{done} of {total} unresolved items reviewed"),
}


def _stage_fraction(stage: str, counters: Counters) -> tuple[int, int]:
    names = STAGE_FRACTIONS.get(stage)
    if names is None:
        return 0, 0
    done, total, _ = names
    return getattr(counters, done, 0) or 0, getattr(counters, total, 0) or 0


def _percent(done: int, total: int) -> int | None:
    """A real fraction or nothing at all.

    The bar used to show stage position — three stages in of eight, so 38% —
    which is a number with no relationship to how much is left, because the
    stages cost wildly different amounts. A run could sit at "38%" for the
    entire slow half. Better to show what is actually being counted, and
    nothing where nothing is.
    """
    if total <= 0:
        return None
    return max(0, min(100, round(done / total * 100)))


def _stage_detail(stage: str, done: int, total: int) -> str:
    names = STAGE_FRACTIONS.get(stage)
    if names is None or total <= 0:
        return ""
    return names[2].format(done=done, total=total)


def _yielding(conn: sqlite3.Connection) -> bool:
    """Whether the last worker cycle skipped the audit to do inbox work.

    Reported because the alternative is a progress panel that appears frozen,
    and "stalled" and "waiting its turn" are different things a person would
    want to tell apart. This is a fact the worker records when it happens, not
    a guess from how long it has been — a bar that says `Paused` because no
    slice ran for half a second would be inventing a state.
    """
    row = conn.execute(
        "SELECT value FROM worker_state WHERE key='audit_yielding'"
    ).fetchone()
    if row is None:
        return False
    try:
        return bool(json.loads(row["value"]))
    except (TypeError, ValueError):
        return False


def _tool_rows(counters: Counters) -> list[tuple[str, str]]:
    """What each tool did, in the words that prove it did something.

    The progress panel names stages, and a stage name is not evidence — an
    `AI` stage that called nothing looked exactly like one that called
    something, which is how a stub survived several passes. These are request
    and match counts, and a zero here is a claim rather than a silence.
    """
    rows: list[tuple[str, str]] = []
    if counters.catalog_requests or counters.catalog_matches:
        rows.append(
            (
                "Catalogs",
                f"{counters.catalog_requests} request"
                f"{'' if counters.catalog_requests == 1 else 's'} · "
                f"{counters.catalog_matches} match"
                f"{'' if counters.catalog_matches == 1 else 'es'}",
            )
        )
    if counters.artwork_total:
        rows.append(
            ("Artwork", f"{counters.artwork_checked} of {counters.artwork_total} albums checked")
        )
    if counters.files_checked:
        rows.append(("Duplicates", f"{counters.duplicate_clusters} exact sets"))
    if counters.storage_total:
        # Prints at zero on purpose. "48 media files checked, 0 opportunities"
        # is the answer an already-efficient library should get, and hiding
        # the line would make a working advisor indistinguishable from an
        # absent one.
        rows.append(
            (
                "Storage",
                f"{counters.storage_opportunities} "
                f"opportunit{'y' if counters.storage_opportunities == 1 else 'ies'} "
                f"in {counters.storage_checked} files",
            )
        )
    if counters.ai_candidates or counters.ai_calls:
        detail = f"{counters.ai_answers} of {counters.ai_candidates} answered"
        if counters.ai_unavailable:
            detail += f" · {counters.ai_unavailable} not reviewed"
        rows.append(("AI", detail))
    return rows


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
        #  A different question from the line above and worth its own: exact
        #  copies are a fact about bytes, and these are a fact about what two
        #  files look or sound like.
        ("Similar groups", counters.similar_groups),
        ("Photos measured", counters.photos_measured),
        ("Photo companions", counters.companions_found),
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
