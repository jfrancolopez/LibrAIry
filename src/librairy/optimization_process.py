"""Owning one FFmpeg child, and never anybody else's.

This is the only module in LibrAIry that starts an encoder. Two rules shape all
of it.

**The worker must not be inside the encode.** `run_once()` launches a child and
returns. Later cycles poll it. An hour-long transcode that occupied the worker
loop would stop the inbox being filed, stop audits, stop backups and stop
thumbnails for that hour — which would make every priority decision made in
`optimization_queue` theoretical.

**Own your child, and only your child.** A NAS running LibrAIry is usually also
running Plex or Jellyfin, which means there are other `ffmpeg` processes on the
box and some of them matter more than this one. Nothing here greps a process
table. A job records the identity of the worker *run* that started it, the PID,
and the kernel's start time for that PID; a stranger's process cannot match all
three, and PID reuse — the reason a bare PID is not identity — cannot either.

## Restart

If the worker or the container stops mid-encode, the child dies with it in the
normal case, and in the abnormal case it is an orphan we can still recognise.
Either way the job does not resume: it becomes `failed` with "Worker stopped
during conversion.", the staging directory is cleared, and retrying is
something the user asks for. Silently restarting an hour of encoding because a
container was updated is not a kindness.
"""

from __future__ import annotations

import logging
import os
import signal
import sqlite3
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from librairy.config import Settings
from librairy.db import transaction
from librairy.optimization_exec import (
    LOW,
    ExecutionRefused,
    ResourcePolicy,
    build_ffmpeg_command,
    output_path,
    priority_prefix,
    probe_streams,
    source_path,
    verify_output,
)
from librairy.optimization_queue import (
    ACTIVE_STATES,
    FAILED,
    MAX_CONCURRENT,
    READY,
    RUNNING,
    VERIFYING,
    clear_staging,
    job_staging_dir,
)
from librairy.planner import utc_now

LOGGER = logging.getLogger(__name__)

# One per worker *process*. A job stamped with a token that is not this one was
# started by a worker that is no longer running, whatever the process table
# says. Minted at import so a single worker keeps one identity for its life.
WORKER_TOKEN = uuid.uuid4().hex

INTERRUPTED_MESSAGE = "Worker stopped during conversion."

# How often the running job's progress is written to SQLite. FFmpeg emits a
# progress block every second and would otherwise produce thousands of writes
# over a long encode, every one of them competing with the worker for the
# single writer lock.
PROGRESS_PERSIST_SECONDS = 2.0

# What is kept in the database when a job fails. The whole stderr goes to the
# staging directory and is removed with it; a TEXT column is not a log store.
LOG_TAIL_BYTES = 2000

# How long a terminated child is given to exit before it is killed. FFmpeg
# closes its output file on SIGTERM, which is worth waiting a moment for.
TERMINATE_GRACE_SECONDS = 10.0


@dataclass
class Owned:
    """A child process this worker started, and the paths that go with it."""

    job_id: int
    process: subprocess.Popen
    staging: Path
    progress_file: Path
    log_file: Path
    output: Path
    started_at: float
    last_persist: float = 0.0
    last_progress_offset: int = 0


# The worker is single-threaded and runs one optimization at a time, so this is
# a dict of at most one entry. It is a dict anyway because "at most one" is a
# policy in `optimization_queue`, not an assumption this module should bake in.
_OWNED: dict[int, Owned] = {}


def owned_jobs() -> list[int]:
    return sorted(_OWNED)


def forget_all() -> None:
    """Drop every handle without touching a process. Tests only."""
    _OWNED.clear()


# --- kernel-level process identity -------------------------------------------------


def process_start_time(pid: int) -> int | None:
    """The kernel's start time for a PID, or None if there is no such process.

    Field 22 of `/proc/<pid>/stat`, in clock ticks since boot. This is what
    makes a PID safe to act on: a recycled PID belongs to a process that
    started later, so the pair (pid, start time) is unique for the life of the
    machine. Absent on platforms without procfs, where this returns None and
    the caller treats the process as gone — the correct conservative answer,
    since the alternative is signalling an unidentified process.
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except (OSError, ValueError):
        return None
    # The command name is in parentheses and may itself contain spaces, so the
    # fields are counted from the last ')' rather than by splitting the line.
    try:
        fields = stat[stat.rindex(")") + 2 :].split()
        return int(fields[19])
    except (ValueError, IndexError):  # pragma: no cover - defensive
        return None


def still_ours(job) -> bool:
    """Whether the recorded process is alive and is the one we started."""
    pid = job["pid"]
    if not pid:
        return False
    started = process_start_time(int(pid))
    return started is not None and started == job["pid_started"]


# --- launching -----------------------------------------------------------------------


def launch(
    conn: sqlite3.Connection,
    settings: Settings,
    job,
    policy: ResourcePolicy = LOW,
) -> bool:
    """Start the encoder for one job and return immediately.

    The concurrency limit is re-checked here, inside the same transaction that
    marks the job running, rather than trusted from the eligibility decision
    that led here. Between deciding and launching there is a gap, and a gap is
    where two encoders come from.
    """
    job_id = int(job["id"])
    if _OWNED:
        return False
    source = source_path(settings, job)
    streams = probe_streams(source)
    from librairy.optimization_exec import check_executable

    check_executable(job["preset"], streams)

    staging = job_staging_dir(settings, job_id)
    # A fresh directory every time. Whatever a previous attempt left behind is
    # not part of this one.
    clear_staging(settings, job_id)
    staging.mkdir(parents=True, exist_ok=True)
    progress_file = staging / "progress"
    progress_file.touch()
    log_file = staging / "ffmpeg.log"
    output = output_path(settings, job)

    argv = priority_prefix(policy) + build_ffmpeg_command(
        settings, job, policy, progress_path=progress_file
    )
    with transaction(conn):
        running = conn.execute(
            f"SELECT COUNT(*) FROM optimization_jobs "  # noqa: S608 - constant
            f"WHERE state IN ({','.join('?' * len(ACTIVE_STATES))})",
            ACTIVE_STATES,
        ).fetchone()[0]
        if running >= MAX_CONCURRENT:
            return False
        # `stdin` is closed and `-nostdin` is passed: an encoder that decides to
        # ask a question would otherwise wait forever on a terminal that is not
        # there.
        with log_file.open("wb") as log:
            process = subprocess.Popen(  # noqa: S603 - argv built above, no shell
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=log,
                # Deliberately *not* a new session: the child stays in the
                # worker's process group so that killing the group on shutdown
                # takes the encoder with it.
                start_new_session=False,
            )
        conn.execute(
            """
            UPDATE optimization_jobs
            SET state=?, owner_token=?, pid=?, pid_started=?, started_at=?,
                staging_dir=?, output_relpath=?, duration_seconds=?,
                wait_reason='', message='', progress=0, out_time_seconds=0,
                updated_at=?
            WHERE id=?
            """,
            (
                RUNNING, WORKER_TOKEN, process.pid, process_start_time(process.pid),
                utc_now(), str(staging), output.name, streams.duration,
                utc_now(), job_id,
            ),
        )
    _OWNED[job_id] = Owned(
        job_id=job_id,
        process=process,
        staging=staging,
        progress_file=progress_file,
        log_file=log_file,
        output=output,
        started_at=time.monotonic(),
    )
    LOGGER.info("optimization job %s started (pid %s)", job_id, process.pid)
    return True


# --- polling ---------------------------------------------------------------------------


def poll(conn: sqlite3.Connection, settings: Settings) -> str:
    """Advance whatever is running, without waiting for it.

    Called on **every** worker cycle, including busy ones. Launching is gated
    behind an idle cycle; noticing that a job has finished is not, or a job
    that completed during a busy hour would sit in `running` until the inbox
    went quiet.

    Returns a short word for the caller's log: "" when there is nothing owned.
    """
    if not _OWNED:
        return ""
    job_id = next(iter(_OWNED))
    handle = _OWNED[job_id]
    code = handle.process.poll()
    if code is None:
        _ingest_progress(conn, handle)
        return "running"
    # Reaped by `poll` returning a code; `wait` here cannot block.
    handle.process.wait()
    _ingest_progress(conn, handle, force=True)
    del _OWNED[job_id]
    if code == 0:
        return _verify(conn, settings, job_id, handle)
    return _fail(conn, settings, job_id, handle, _failure_message(handle, code))


def _failure_message(handle: Owned, code: int) -> str:
    tail = ""
    try:
        data = handle.log_file.read_bytes()[-LOG_TAIL_BYTES:]
        tail = data.decode("utf-8", "replace").strip().splitlines()[-1:]
        tail = tail[0] if tail else ""
    except OSError:  # pragma: no cover - defensive
        pass
    return f"The converter stopped with error {code}." + (f" {tail}" if tail else "")


def _ingest_progress(conn: sqlite3.Connection, handle: Owned, *, force: bool = False) -> None:
    """Read what FFmpeg has appended, and persist at most every couple of seconds.

    Reading is a bounded, non-blocking file read from a known offset — not a
    pipe. FFmpeg's `-progress` writes key=value lines to a path, which sidesteps
    the whole question of a worker loop blocking on `readline()` against a
    process that has stopped emitting. No reader thread, no select loop, no
    asyncio anywhere near the rest of LibrAIry.
    """
    try:
        with handle.progress_file.open("rb") as stream:
            stream.seek(handle.last_progress_offset)
            chunk = stream.read()
            handle.last_progress_offset = stream.tell()
    except OSError:  # pragma: no cover - the directory is ours
        return
    out_time = 0.0
    finished = False
    for line in chunk.decode("utf-8", "replace").splitlines():
        key, _, value = line.partition("=")
        if key == "out_time_us" and value.strip().isdigit():
            out_time = int(value) / 1_000_000
        elif key == "progress" and value.strip() == "end":
            finished = True
    if not out_time and not finished:
        return
    now = time.monotonic()
    if not force and now - handle.last_persist < PROGRESS_PERSIST_SECONDS:
        return
    handle.last_persist = now
    row = conn.execute(
        "SELECT duration_seconds FROM optimization_jobs WHERE id=?", (handle.job_id,)
    ).fetchone()
    duration = float(row["duration_seconds"] or 0) if row else 0.0
    # Without a known duration there is no honest percentage, and a made-up one
    # is worse than a running time. `progress` stays 0 and the UI says elapsed.
    percent = min(100.0, out_time / duration * 100) if duration else 0.0
    conn.execute(
        "UPDATE optimization_jobs SET progress=?, out_time_seconds=?, progress_at=?,"
        " updated_at=? WHERE id=?",
        (percent, out_time, utc_now(), utc_now(), handle.job_id),
    )


# --- verifying --------------------------------------------------------------------------


def _verify(conn: sqlite3.Connection, settings: Settings, job_id: int, handle: Owned) -> str:
    """Exit code 0 is not success. This is the state that decides.

    A separate state rather than a step inside the running one, because the
    question a person is asking at this moment — "is my file all right?" — is
    genuinely not answered yet, and `Ready` while an ffprobe is still running
    would be a claim nobody had checked.
    """
    conn.execute(
        "UPDATE optimization_jobs SET state=?, progress=100, updated_at=? WHERE id=?",
        (VERIFYING, utc_now(), job_id),
    )
    job = conn.execute("SELECT * FROM optimization_jobs WHERE id=?", (job_id,)).fetchone()
    try:
        source = probe_streams(source_path(settings, job))
    except ExecutionRefused as exc:
        return _fail(conn, settings, job_id, handle, str(exc))
    verdict = verify_output(job, source, handle.output)
    if not verdict.ok:
        return _fail(conn, settings, job_id, handle, verdict.detail)
    actual = handle.output.stat().st_size
    conn.execute(
        """
        UPDATE optimization_jobs
        SET state=?, verified='passed', actual_bytes=?, runtime_seconds=?,
            finished_at=?, updated_at=?, message=''
        WHERE id=?
        """,
        (READY, actual, time.monotonic() - handle.started_at, utc_now(), utc_now(), job_id),
    )
    LOGGER.info("optimization job %s ready for review", job_id)
    return "ready"


def _fail(
    conn: sqlite3.Connection,
    settings: Settings,
    job_id: int,
    handle: Owned | None,
    message: str,
) -> str:
    """Give up on this job and leave nothing behind but the explanation.

    The incomplete output goes; the source was never touched, because nothing
    in this module ever writes outside the staging directory.
    """
    tail = ""
    if handle is not None:
        try:
            tail = handle.log_file.read_bytes()[-LOG_TAIL_BYTES:].decode("utf-8", "replace")
        except OSError:  # pragma: no cover - defensive
            tail = ""
        clear_staging(settings, job_id)
    conn.execute(
        """
        UPDATE optimization_jobs
        SET state=?, verified='failed', message=?, log_tail=?, finished_at=?,
            updated_at=?, pid=NULL, pid_started=NULL
        WHERE id=?
        """,
        (FAILED, message, tail, utc_now(), utc_now(), job_id),
    )
    LOGGER.warning("optimization job %s failed: %s", job_id, message)
    return "failed"


# --- stopping ---------------------------------------------------------------------------


def stop(conn: sqlite3.Connection, settings: Settings, job_id: int, *, state: str, message: str):
    """Terminate this job's child, if we own it, and clear its staging.

    `SIGTERM`, a bounded wait, then `SIGKILL`. Both are sent to a process
    identified by the handle we are holding — not by a name, not by a pattern.
    There is no code path here that could reach a process LibrAIry did not
    start.
    """
    handle = _OWNED.pop(job_id, None)
    if handle is not None:
        _terminate(handle.process)
    clear_staging(settings, job_id)
    conn.execute(
        """
        UPDATE optimization_jobs
        SET state=?, message=?, finished_at=?, updated_at=?, pid=NULL, pid_started=NULL
        WHERE id=?
        """,
        (state, message, utc_now(), utc_now(), job_id),
    )


def _terminate(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    with_suppress = (ProcessLookupError, PermissionError, OSError)
    try:
        process.terminate()
    except with_suppress:  # pragma: no cover - already gone
        return
    try:
        process.wait(timeout=TERMINATE_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        LOGGER.warning("encoder did not stop on SIGTERM; killing pid %s", process.pid)
    try:
        process.kill()
        # Reaped, so it does not stay a zombie for the life of the worker.
        process.wait(timeout=TERMINATE_GRACE_SECONDS)
    except (subprocess.TimeoutExpired, *with_suppress):  # pragma: no cover - defensive
        LOGGER.error("could not reap encoder pid %s", process.pid)


# --- after a restart ----------------------------------------------------------------------


def reconcile(conn: sqlite3.Connection, settings: Settings) -> int:
    """Settle jobs left `running` by a worker that is no longer running.

    Called once when a worker starts. A row saying `running` with nobody
    behind it is the state that makes a UI lie, and the honest resolution is
    not to resume: nothing knows how much of the output is valid, and an
    automatic restart would spend another hour of CPU that nobody asked for
    twice.

    An orphaned child — possible if the worker died but the container did not —
    is stopped first, and only ever by (pid, kernel start time) recorded when
    *this* application launched it.
    """
    rows = conn.execute(
        f"SELECT * FROM optimization_jobs "  # noqa: S608 - constant tuple
        f"WHERE state IN ({','.join('?' * len(ACTIVE_STATES))}) AND owner_token != ?",
        (*ACTIVE_STATES, WORKER_TOKEN),
    ).fetchall()
    for job in rows:
        if still_ours(job):
            LOGGER.warning(
                "adopting orphaned encoder for job %s (pid %s) to stop it",
                job["id"], job["pid"],
            )
            _signal_orphan(int(job["pid"]))
        clear_staging(settings, int(job["id"]))
        conn.execute(
            """
            UPDATE optimization_jobs
            SET state=?, verified='', message=?, finished_at=?, updated_at=?,
                pid=NULL, pid_started=NULL
            WHERE id=?
            """,
            (FAILED, INTERRUPTED_MESSAGE, utc_now(), utc_now(), job["id"]),
        )
    return len(rows)


def _signal_orphan(pid: int) -> None:
    """SIGTERM then SIGKILL a process we have already proved is ours."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        for _ in range(int(TERMINATE_GRACE_SECONDS * 4)):
            if process_start_time(pid) is None:
                return
            time.sleep(0.25)
