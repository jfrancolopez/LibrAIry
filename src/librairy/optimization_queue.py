"""The optimization queue: approving work, and waiting patiently to do it.

Nothing here runs ffmpeg. This module decides *whether* a job may start, and
that decision is a pure function of the job and a snapshot of the system —
which is the whole reason it is a separate module. Almost every interesting
behaviour of this feature (priority, the night window, disk headroom, load,
protected roots, a changed source, cancellation) can be proven without
spending a single CPU cycle on an encoder, and burying that logic inside
`Worker.run_once` would make all of it untestable at once.

Three ideas hold it together.

**A queued job is frozen.** It records what the user approved and what the
file was when they approved it. The worker never asks the advisor again: an
application upgrade could change what the advisor recommends, and someone who
approved a remux must not come back to a re-encode. The same rule as an
immutable commit plan, for the same reason.

**Waiting is not failing.** A job that cannot start right now is `waiting`
with a reason, forever if necessary. There is no retry counter, no backoff, no
error state for "the NAS is busy" — those are all ways of turning patience
into noise.

**Lowest priority means "does not start", not "gets murdered".** Higher-
priority work prevents a *new* optimization from beginning. It does not
suspend or kill one already running. Repeatedly stopping and restarting an
encoder every time a file lands in the inbox would add a great many failure
modes — half-written output, orphaned processes, corrupt staging — in exchange
for very little, given that the encoder is separately constrained to a small
share of the machine.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from typing import TYPE_CHECKING

from librairy.planner import utc_now

if TYPE_CHECKING:  # pragma: no cover - typing only
    from librairy.config import Settings

# --- states --------------------------------------------------------------------
#
# Deliberately few. The *reason* a job is waiting lives in its own column, so a
# new reason never needs a new state — which is how state machines grow to
# twenty-five members that nobody can draw.

QUEUED = "queued"
WAITING = "waiting"
RUNNING = "running"
VERIFYING = "verifying"
READY = "ready"
FAILED = "failed"
CANCELLED = "cancelled"
STALE = "stale"

LIVE_STATES = (QUEUED, WAITING, RUNNING, VERIFYING, READY)
ACTIVE_STATES = (RUNNING, VERIFYING)

STATE_LABEL = {
    QUEUED: "Queued",
    WAITING: "Waiting",
    RUNNING: "Running",
    VERIFYING: "Verifying",
    READY: "Ready for review",
    FAILED: "Failed",
    CANCELLED: "Cancelled",
    STALE: "Source changed",
}

# --- why a job is not starting ---------------------------------------------------
#
# Structured rather than free text, so the UI can phrase each one properly and
# a test can assert which gate fired rather than matching a sentence.

HIGHER_PRIORITY = "higher_priority_work"
OUTSIDE_WINDOW = "outside_window"
MANUAL_ONLY = "manual_only"
PROTECTED = "protected_source"
SOURCE_CHANGED = "source_changed"
ANOTHER_RUNNING = "another_job_running"
HIGH_LOAD = "high_system_load"
NO_DISK = "insufficient_disk_space"
UNSUPPORTED = "unsupported_operation"

WAIT_TEXT = {
    HIGHER_PRIORITY: "Waiting for LibrAIry's other work to finish",
    OUTSIDE_WINDOW: "Waiting for the maintenance window",
    MANUAL_ONLY: "Waiting to be started by hand",
    PROTECTED: "Inside a protected root",
    SOURCE_CHANGED: "The file changed after this was queued",
    ANOTHER_RUNNING: "Another optimization is running",
    HIGH_LOAD: "Waiting for the system to be less busy",
    NO_DISK: "Waiting for temporary disk space",
    UNSUPPORTED: "Automatic conversion is not supported for this file yet",
}

# What `Run now` writes into a job's run policy. Not a state: the job is still
# queued and still behind every other gate, it has simply stopped waiting for
# the clock.
FORCED = "forced"

# One at a time, and not configurable. A single ffmpeg process is already a
# significant share of a NAS; letting somebody set four before the resource
# policy has been measured would make the guarantee meaningless.
MAX_CONCURRENT = 1

# Space for the output plus room to be wrong about its size. A transcode that
# overshoots its estimate must not be the thing that fills the disk.
DISK_SAFETY_FACTOR = 1.25
# Never take the last of the filesystem, whatever the job needs. Maintenance
# is not worth an unbootable NAS.
DISK_RESERVE_BYTES = 2 * 1024 * 1024 * 1024

# Load average per CPU above which starting something expensive is obviously a
# bad idea. Deliberately generous: this is a "not now" gate, not a scheduler,
# and inside a container the number it reads may be the host's. See
# `system_snapshot`.
LOAD_CEILING = 2.0

# Which operations the executor can perform safely today. An opportunity the
# advisor found but the executor cannot yet run is shown and refused, never
# queued — dropping a subtitle track and calling it a success is the outcome
# this list exists to prevent.
EXECUTABLE_KINDS = frozenset({"audio-to-flac", "video-remux", "video-transcode"})

PRESETS = {
    "audio-to-flac": "flac-lossless",
    "video-remux": "mp4-stream-copy",
    "video-transcode": "hevc-1080p-low",
}


@dataclass(frozen=True)
class SystemState:
    """A snapshot of everything the decision depends on.

    Passed in rather than read inside the decision, so every branch is
    testable by constructing a state rather than by arranging a machine.
    """

    now: datetime
    higher_priority_work: bool = False
    running_jobs: int = 0
    free_bytes: int = 0
    load_per_cpu: float = 0.0
    current_fingerprint: str = ""
    protected_by: str = ""


@dataclass(frozen=True)
class Decision:
    """May it start, and if not, which gate stopped it."""

    eligible: bool
    reason: str = ""

    @property
    def text(self) -> str:
        return WAIT_TEXT.get(self.reason, "")


# --- queuing ---------------------------------------------------------------------


class QueueRefused(RuntimeError):
    """Why an opportunity could not be queued, in words for the user."""


def enqueue(
    conn: sqlite3.Connection,
    opportunity_id: int,
    *,
    run_policy: str = "window",
) -> int:
    """Freeze one opportunity into a job.

    Refuses rather than silently skipping. A bulk action that queued three of
    four and said nothing about the fourth is the worst outcome on a page that
    is about to spend an hour of CPU.
    """
    row = conn.execute(
        "SELECT * FROM optimization_opportunities WHERE id=?", (opportunity_id,)
    ).fetchone()
    if row is None:
        raise QueueRefused("that opportunity no longer exists")
    if row["status"] != "open":
        raise QueueRefused("that opportunity has already been answered")
    if row["protected_by"]:
        raise QueueRefused(f"it is inside the protected root {row['protected_by']}")
    if row["kind"] not in EXECUTABLE_KINDS:
        raise QueueRefused("automatic conversion is not supported for this file yet")
    if _live_job_for(conn, row["relpath"], row["kind"]) is not None:
        raise QueueRefused("it is already queued")

    now = utc_now()
    cursor = conn.execute(
        """
        INSERT INTO optimization_jobs(
          opportunity_id, item_id, root, relpath, fingerprint, kind, quality,
          from_label, to_label, preset, preset_version, rule_version,
          source_bytes, estimated_bytes, run_policy, state, queued_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            opportunity_id, row["item_id"], row["root"], row["relpath"],
            row["fingerprint"] or "", row["kind"], row["quality"],
            row["from_label"], row["to_label"], PRESETS[row["kind"]],
            row["rule_version"], row["current_bytes"], row["estimated_bytes"],
            run_policy, QUEUED, now, now,
        ),
    )
    return int(cursor.lastrowid)


def _live_job_for(conn: sqlite3.Connection, relpath: str, kind: str) -> sqlite3.Row | None:
    placeholders = ",".join("?" * len(LIVE_STATES))
    return conn.execute(
        f"SELECT * FROM optimization_jobs WHERE relpath=? AND kind=? "  # noqa: S608
        f"AND state IN ({placeholders})",
        (relpath, kind, *LIVE_STATES),
    ).fetchone()


def cancel(conn: sqlite3.Connection, job_id: int) -> bool:
    """Remove a job from the queue. Running jobs are stopped by the executor.

    A job that has not started leaves nothing behind, which is why this can be
    a single update. Once an encoder exists, a running job additionally has a
    process to terminate and a staging directory to clear.
    """
    cursor = conn.execute(
        "UPDATE optimization_jobs SET state=?, finished_at=?, updated_at=? "
        "WHERE id=? AND state IN (?, ?)",
        (CANCELLED, utc_now(), utc_now(), job_id, QUEUED, WAITING),
    )
    return cursor.rowcount > 0


def set_waiting(conn: sqlite3.Connection, job_id: int, reason: str) -> None:
    """Record why a job is not starting. Idempotent and cheap: the worker
    writes this on every cycle a job stays ineligible."""
    conn.execute(
        "UPDATE optimization_jobs SET state=?, wait_reason=?, updated_at=? "
        "WHERE id=? AND state IN (?, ?)",
        (WAITING, reason, utc_now(), job_id, QUEUED, WAITING),
    )


def mark_stale(conn: sqlite3.Connection, job_id: int) -> None:
    """The file changed after this was approved.

    A terminal state rather than a wait: the decision was made about bytes
    that no longer exist, and no amount of waiting brings them back. The user
    re-analyses, or removes it.
    """
    conn.execute(
        "UPDATE optimization_jobs SET state=?, wait_reason=?, updated_at=? WHERE id=?",
        (STALE, SOURCE_CHANGED, utc_now(), job_id),
    )


def jobs(conn: sqlite3.Connection, *, states: tuple[str, ...] | None = None) -> list:
    placeholders = ",".join("?" * len(states)) if states else ""
    sql = "SELECT * FROM optimization_jobs"
    params: tuple = ()
    if states:
        sql += f" WHERE state IN ({placeholders})"  # noqa: S608
        params = states
    return list(conn.execute(f"{sql} ORDER BY queued_at, id", params))


def next_job(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """The oldest job that could conceivably start. First in, first served —
    there is one at a time, so a cleverer order would only be a way to
    surprise somebody."""
    return conn.execute(
        "SELECT * FROM optimization_jobs WHERE state IN (?, ?) ORDER BY queued_at, id LIMIT 1",
        (QUEUED, WAITING),
    ).fetchone()


# --- the maintenance window ------------------------------------------------------


def in_window(now: datetime, start: str, end: str) -> bool:
    """Whether local `now` falls inside `start`–`end`.

    Handles the case that spans midnight, which is most of them: `22:00–05:00`
    is one window and not two, and a naive `start <= now <= end` says a job may
    never run at all.
    """
    try:
        opens, closes = _clock(start), _clock(end)
    except ValueError:
        return True
    current = now.time()
    if opens == closes:
        return True
    if opens < closes:
        return opens <= current < closes
    return current >= opens or current < closes


def _clock(value: str) -> time:
    hours, _, minutes = value.strip().partition(":")
    return time(int(hours), int(minutes or 0))


# --- may this job start? ---------------------------------------------------------


def decide(
    job: sqlite3.Row,
    state: SystemState,
    *,
    run_policy: str = "window",
    window: tuple[str, str] = ("01:00", "06:00"),
    forced: bool = False,
) -> Decision:
    """The one place that answers "may this start now?".

    Ordered by how permanent the objection is. A protected source or a changed
    file will never become eligible, so those are checked before anything that
    merely needs patience — otherwise a doomed job would report "waiting for
    disk space" forever and read like an infrastructure problem.

    `forced` is *Run now*, and it lifts exactly one gate: the clock. It cannot
    reach past a protected root, a changed source, the concurrency limit, the
    disk reserve or the load ceiling — a button that says "sooner" must not
    quietly mean "and without the safety checks".
    """
    if state.protected_by:
        return Decision(False, PROTECTED)
    if job["kind"] not in EXECUTABLE_KINDS:
        return Decision(False, UNSUPPORTED)
    if job["fingerprint"] and state.current_fingerprint != job["fingerprint"]:
        return Decision(False, SOURCE_CHANGED)
    if state.running_jobs >= MAX_CONCURRENT:
        return Decision(False, ANOTHER_RUNNING)
    if state.higher_priority_work:
        return Decision(False, HIGHER_PRIORITY)
    if not forced:
        if run_policy == "manual":
            return Decision(False, MANUAL_ONLY)
        if run_policy == "window" and not in_window(state.now, *window):
            return Decision(False, OUTSIDE_WINDOW)
    if state.free_bytes and state.free_bytes < required_bytes(job) + DISK_RESERVE_BYTES:
        return Decision(False, NO_DISK)
    if state.load_per_cpu >= LOAD_CEILING:
        return Decision(False, HIGH_LOAD)
    return Decision(True)


def required_bytes(job: sqlite3.Row) -> int:
    """Temporary space this job needs, erring high.

    The estimate can be wrong, and it is only ever wrong in one direction that
    matters — a transcode that comes out bigger than predicted. When there is
    no estimate at all, assume the output is the size of the source, because
    assuming smaller is how a disk fills up.
    """
    estimated = job["estimated_bytes"] or job["source_bytes"]
    return int(max(estimated, job["source_bytes"] * 0.1) * DISK_SAFETY_FACTOR)


def system_snapshot(
    conn: sqlite3.Connection, settings: Settings, *, higher_priority_work: bool = False
) -> SystemState:
    """What the machine looks like right now, as far as it can be known.

    The load figure deserves a caveat that belongs in the code rather than a
    report: inside a container `getloadavg` is usually the *host's*, and cgroup
    quotas are not reflected in it. Trying to derive true container saturation
    from here would be a research project with an unreliable answer on every
    NAS platform.

    So this is not a scheduler. It answers one crude question — "is this
    obviously a bad moment to start something expensive?" — and the real
    protection is elsewhere: one job at a time, a bounded encoder, and low
    process priority where the runtime allows it.
    """
    import os
    import shutil

    try:
        load = os.getloadavg()[0] / max(1, os.cpu_count() or 1)
    except (OSError, AttributeError):  # pragma: no cover - not every platform has it
        load = 0.0
    # The filesystem that will hold the staging directory, which is not
    # necessarily the one holding the library and is very often not `/`.
    staging = staging_root(settings)
    try:
        staging.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(staging).free
    except OSError:  # pragma: no cover - unwritable appdata is a bigger problem
        free = 0
    return SystemState(
        now=datetime.now(),
        higher_priority_work=higher_priority_work,
        running_jobs=len(jobs(conn, states=ACTIVE_STATES)),
        free_bytes=free,
        load_per_cpu=load,
    )


def staging_root(settings: Settings) -> Path:
    """Where generated output lives, and nowhere else.

    Under appdata, never in the library. An incomplete encode must not be
    visible to Browse, to Search, or to whoever is looking at the folder over
    SMB while it is being written.
    """
    return settings.appdata_dir / "optimization" / "jobs"


def job_staging_dir(settings: Settings, job_id: int) -> Path:
    return staging_root(settings) / str(int(job_id))


def clear_staging(settings: Settings, job_id: int) -> None:
    """Remove one job's generated output. The only delete this feature performs.

    Deliberately the single place, and it re-derives the path from the job id
    rather than accepting one: `staging_dir` is a stored column, and a stored
    path is a path somebody could have edited. The containment check below is
    therefore about a value this function computed itself, which is the only
    kind of check worth having.

    `tests/test_adversarial.py::test_safety_invariants_forbid_moves_outside_executor`
    allows exactly this file to name a delete primitive, so the rule "nothing
    outside the executor removes anything" survives with one auditable
    exception rather than three.
    """
    import shutil

    target = job_staging_dir(settings, job_id).resolve()
    root = staging_root(settings).resolve()
    if root not in target.parents:  # pragma: no cover - unreachable by construction
        raise ValueError("staging directory is not inside the optimization workspace")
    shutil.rmtree(target, ignore_errors=True)
