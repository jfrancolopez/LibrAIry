"""The orchestration layer, proven without spending a CPU cycle.

Every behaviour worth being sure about — priority, the night window, disk
headroom, load, protected roots, a changed source, cancellation, the
concurrency limit — is decided before any encoder exists. That is the point of
`decide` being a pure function of a job and a `SystemState`: each branch is
testable by building a state rather than by arranging a machine.

Two claims are load-bearing.

`test_run_now_lifts_the_clock_and_nothing_else` — a button that says "sooner"
must not quietly mean "and without the safety checks".

`test_a_settled_inbox_does_not_starve_optimization` — the audit shipped with
exactly this bug once: "files exist" is not "work happened", and a NAS whose
inbox is never empty would otherwise never run maintenance at all.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from librairy import optimization_queue as queue
from librairy.config import Settings
from librairy.db import connect
from librairy.planner import utc_now
from librairy.protected import set_protected_roots
from librairy.scanner import scan_root

MB = 1024 * 1024
GB = 1024 * MB
PLENTY = 500 * GB
NIGHT = datetime(2026, 8, 13, 2, 30)
AFTERNOON = datetime(2026, 8, 13, 14, 0)


def settings_for(tmp_path: Path) -> Settings:
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        FILE_STABILITY_SECONDS=0,
        AUTH_REQUIRED=False,
        OLLAMA_HOST="",
        _env_file=None,
    )
    for directory in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        directory.mkdir(parents=True, exist_ok=True)
    return settings


def scene(tmp_path: Path, *, protected: list[str] | None = None):
    settings = settings_for(tmp_path)
    track = settings.library_dir / "Music" / "concert.wav"
    track.parent.mkdir(parents=True, exist_ok=True)
    track.write_bytes(b"RIFF-deterministic-bytes")
    conn = connect(settings)
    scan_root(conn, "library", settings.library_dir, settings)
    if protected:
        set_protected_roots(conn, protected, library_dir=settings.library_dir)
    return conn, settings


def opportunity(
    conn,
    relpath="Music/concert.wav",
    kind="audio-to-flac",
    protected_by="",
    fingerprint="fp-original",
    current=842 * MB,
    estimated=510 * MB,
    status="open",
) -> int:
    row = conn.execute(
        "SELECT id FROM items WHERE relpath=?", (relpath,)
    ).fetchone()
    cursor = conn.execute(
        """
        INSERT INTO optimization_opportunities(
          item_id, root, relpath, kind, quality, current_bytes, estimated_bytes,
          summary, reason, compute, from_label, to_label, protected_by, facts,
          fingerprint, rule_version, status, detected_at, updated_at
        ) VALUES (?, 'library', ?, ?, 'lossless', ?, ?, '', '', 'low', 'WAV',
                  'FLAC', ?, '[]', ?, 1, ?, ?, ?)
        """,
        (
            row["id"] if row else None, relpath, kind, current, estimated,
            protected_by, fingerprint, status, utc_now(), utc_now(),
        ),
    )
    return int(cursor.lastrowid)


def state(**kwargs) -> queue.SystemState:
    """A machine with nothing wrong with it, unless a test says otherwise."""
    defaults = {
        "now": NIGHT,
        "free_bytes": PLENTY,
        "current_fingerprint": "fp-original",
        "load_per_cpu": 0.1,
    }
    return queue.SystemState(**{**defaults, **kwargs})


def job_row(conn, job_id: int):
    return conn.execute("SELECT * FROM optimization_jobs WHERE id=?", (job_id,)).fetchone()


# --- the immutable job -------------------------------------------------------------


def test_queuing_freezes_what_was_approved(tmp_path: Path) -> None:
    conn, _ = scene(tmp_path)
    job_id = queue.enqueue(conn, opportunity(conn))

    job = job_row(conn, job_id)

    assert job["fingerprint"] == "fp-original"
    assert job["kind"] == "audio-to-flac"
    assert job["preset"] == "flac-lossless"
    assert job["source_bytes"] == 842 * MB
    assert job["estimated_bytes"] == 510 * MB
    assert job["state"] == queue.QUEUED


def test_the_job_records_no_command_only_intent(tmp_path: Path) -> None:
    """A job describes what to do. The argv is built later by trusted code, so
    nothing a form can post ever reaches a subprocess."""
    conn, _ = scene(tmp_path)
    job = job_row(conn, queue.enqueue(conn, opportunity(conn)))

    assert "ffmpeg" not in " ".join(str(value) for value in job)
    assert job["preset"] in queue.PRESETS.values()


def test_a_later_advisor_change_does_not_rewrite_a_queued_job(tmp_path: Path) -> None:
    """Somebody approved a remux. An upgrade must not turn it into a re-encode."""
    conn, _ = scene(tmp_path)
    opportunity_id = opportunity(conn)
    job_id = queue.enqueue(conn, opportunity_id)
    before = dict(job_row(conn, job_id))

    conn.execute(
        "UPDATE optimization_opportunities SET kind='video-transcode', "
        "quality='lossy', estimated_bytes=1 WHERE id=?",
        (opportunity_id,),
    )

    assert dict(job_row(conn, job_id)) == before


def test_the_same_opportunity_cannot_be_queued_twice(tmp_path: Path) -> None:
    conn, _ = scene(tmp_path)
    opportunity_id = opportunity(conn)
    queue.enqueue(conn, opportunity_id)

    with pytest.raises(queue.QueueRefused, match="already queued"):
        queue.enqueue(conn, opportunity_id)


def test_the_duplicate_rule_lives_in_the_database(tmp_path: Path) -> None:
    """"The UI disables the button" is not a constraint."""
    import sqlite3

    conn, _ = scene(tmp_path)
    queue.enqueue(conn, opportunity(conn))

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO optimization_jobs(relpath, kind, quality, preset, "
            "state, queued_at, updated_at) VALUES "
            "('Music/concert.wav', 'audio-to-flac', 'lossless', 'flac-lossless', "
            "'queued', '2026-01-01', '2026-01-01')"
        )


def test_a_cancelled_job_frees_the_file_to_be_queued_again(tmp_path: Path) -> None:
    conn, _ = scene(tmp_path)
    opportunity_id = opportunity(conn)
    job_id = queue.enqueue(conn, opportunity_id)

    assert queue.cancel(conn, job_id) is True
    assert queue.enqueue(conn, opportunity_id)


# --- what may not be queued at all --------------------------------------------------


def test_a_protected_source_is_refused_with_a_reason(tmp_path: Path) -> None:
    conn, _ = scene(tmp_path)
    protected = opportunity(
        conn, relpath="Photos/Memories/clip.wav", protected_by="Photos/Memories"
    )

    with pytest.raises(queue.QueueRefused, match="protected root"):
        queue.enqueue(conn, protected)


def test_an_unsupported_operation_is_refused_rather_than_attempted(tmp_path: Path) -> None:
    """Better to say "not yet" than to drop a subtitle track and call it a
    success."""
    conn, _ = scene(tmp_path)
    exotic = opportunity(conn, kind="video-something-clever")

    with pytest.raises(queue.QueueRefused, match="not supported"):
        queue.enqueue(conn, exotic)


def test_an_already_dismissed_opportunity_cannot_be_queued(tmp_path: Path) -> None:
    conn, _ = scene(tmp_path)
    dismissed = opportunity(conn, status="dismissed")

    with pytest.raises(queue.QueueRefused, match="already been answered"):
        queue.enqueue(conn, dismissed)


# --- the decision -------------------------------------------------------------------


def test_a_healthy_job_at_night_is_eligible(tmp_path: Path) -> None:
    conn, _ = scene(tmp_path)
    job = job_row(conn, queue.enqueue(conn, opportunity(conn)))

    assert queue.decide(job, state()).eligible


def test_a_protected_source_never_becomes_eligible(tmp_path: Path) -> None:
    conn, _ = scene(tmp_path)
    job = job_row(conn, queue.enqueue(conn, opportunity(conn)))

    decision = queue.decide(job, state(protected_by="Photos/Memories"))

    assert decision.reason == queue.PROTECTED


def test_a_changed_source_stops_the_job(tmp_path: Path) -> None:
    """The decision was made about bytes that no longer exist."""
    conn, _ = scene(tmp_path)
    job = job_row(conn, queue.enqueue(conn, opportunity(conn)))

    decision = queue.decide(job, state(current_fingerprint="fp-something-else"))

    assert decision.reason == queue.SOURCE_CHANGED


def test_higher_priority_work_stops_a_new_start(tmp_path: Path) -> None:
    conn, _ = scene(tmp_path)
    job = job_row(conn, queue.enqueue(conn, opportunity(conn)))

    assert queue.decide(job, state(higher_priority_work=True)).reason == (
        queue.HIGHER_PRIORITY
    )


def test_only_one_optimization_runs(tmp_path: Path) -> None:
    conn, _ = scene(tmp_path)
    job = job_row(conn, queue.enqueue(conn, opportunity(conn)))

    assert queue.MAX_CONCURRENT == 1
    assert queue.decide(job, state(running_jobs=1)).reason == queue.ANOTHER_RUNNING


def test_a_busy_machine_waits(tmp_path: Path) -> None:
    conn, _ = scene(tmp_path)
    job = job_row(conn, queue.enqueue(conn, opportunity(conn)))

    assert queue.decide(job, state(load_per_cpu=8.0)).reason == queue.HIGH_LOAD


def test_a_full_disk_waits_rather_than_failing(tmp_path: Path) -> None:
    conn, _ = scene(tmp_path)
    job = job_row(conn, queue.enqueue(conn, opportunity(conn)))

    decision = queue.decide(job, state(free_bytes=100 * MB))

    assert decision.eligible is False
    assert decision.reason == queue.NO_DISK


def test_enough_disk_is_eligible(tmp_path: Path) -> None:
    conn, _ = scene(tmp_path)
    job = job_row(conn, queue.enqueue(conn, opportunity(conn)))
    needed = queue.required_bytes(job) + queue.DISK_RESERVE_BYTES

    assert queue.decide(job, state(free_bytes=needed + MB)).eligible


def test_the_reserve_is_never_spent(tmp_path: Path) -> None:
    """Maintenance is not worth an unbootable NAS."""
    conn, _ = scene(tmp_path)
    job = job_row(conn, queue.enqueue(conn, opportunity(conn)))
    exactly_the_job = queue.required_bytes(job)

    assert not queue.decide(job, state(free_bytes=exactly_the_job)).eligible


def test_a_missing_estimate_assumes_the_output_is_no_smaller(tmp_path: Path) -> None:
    """Assuming smaller is how a disk fills up."""
    conn, _ = scene(tmp_path)
    job_id = queue.enqueue(conn, opportunity(conn, estimated=0))
    conn.execute("UPDATE optimization_jobs SET estimated_bytes=0 WHERE id=?", (job_id,))

    assert queue.required_bytes(job_row(conn, job_id)) >= 842 * MB


# --- the maintenance window ----------------------------------------------------------


def test_inside_the_window_a_job_may_start(tmp_path: Path) -> None:
    conn, _ = scene(tmp_path)
    job = job_row(conn, queue.enqueue(conn, opportunity(conn)))

    assert queue.decide(job, state(now=NIGHT), window=("01:00", "06:00")).eligible


def test_outside_the_window_it_waits(tmp_path: Path) -> None:
    conn, _ = scene(tmp_path)
    job = job_row(conn, queue.enqueue(conn, opportunity(conn)))

    decision = queue.decide(job, state(now=AFTERNOON), window=("01:00", "06:00"))

    assert decision.reason == queue.OUTSIDE_WINDOW


@pytest.mark.parametrize(
    ("moment", "inside"),
    [
        (datetime(2026, 8, 13, 23, 0), True),
        (datetime(2026, 8, 13, 2, 0), True),
        (datetime(2026, 8, 13, 4, 59), True),
        (datetime(2026, 8, 13, 5, 1), False),
        (datetime(2026, 8, 13, 12, 0), False),
        (datetime(2026, 8, 13, 21, 59), False),
    ],
)
def test_a_window_that_crosses_midnight_is_one_window(moment, inside: bool) -> None:
    """`22:00–05:00` is not two windows, and a naive `start <= now <= end`
    says a job may never run at all."""
    assert queue.in_window(moment, "22:00", "05:00") is inside


def test_manual_only_never_starts_on_its_own(tmp_path: Path) -> None:
    conn, _ = scene(tmp_path)
    job = job_row(conn, queue.enqueue(conn, opportunity(conn), run_policy="manual"))

    assert queue.decide(job, state(), run_policy="manual").reason == queue.MANUAL_ONLY


def test_run_now_lifts_the_clock(tmp_path: Path) -> None:
    conn, _ = scene(tmp_path)
    job = job_row(conn, queue.enqueue(conn, opportunity(conn)))

    assert queue.decide(job, state(now=AFTERNOON), forced=True).eligible
    assert queue.decide(job, state(), run_policy="manual", forced=True).eligible


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"protected_by": "Photos/Memories"}, queue.PROTECTED),
        ({"current_fingerprint": "changed"}, queue.SOURCE_CHANGED),
        ({"running_jobs": 1}, queue.ANOTHER_RUNNING),
        ({"higher_priority_work": True}, queue.HIGHER_PRIORITY),
        ({"free_bytes": 10 * MB}, queue.NO_DISK),
        ({"load_per_cpu": 9.0}, queue.HIGH_LOAD),
    ],
)
def test_run_now_lifts_the_clock_and_nothing_else(
    tmp_path: Path, kwargs: dict, reason: str
) -> None:
    """A button that says "sooner" must not quietly mean "and without the
    safety checks"."""
    conn, _ = scene(tmp_path)
    job = job_row(conn, queue.enqueue(conn, opportunity(conn)))

    decision = queue.decide(job, state(now=AFTERNOON, **kwargs), forced=True)

    assert decision.eligible is False
    assert decision.reason == reason


def test_the_window_is_a_clock_and_not_a_resource_permit() -> None:
    """Nighttime decides *when* work may begin. It never means the limits are
    off: the same policy applies at 02:00 as at 14:00, and this is asserted
    because it is the kind of thing a later change would quietly relax."""
    import inspect

    source = inspect.getsource(queue.decide)
    clock_block = source.split("if not forced:", 1)[1].split("if state.free_bytes", 1)[0]

    assert "run_policy" in clock_block
    assert "load" not in clock_block
    assert "free_bytes" not in clock_block


# --- waiting is not failing -----------------------------------------------------------


def test_a_waiting_job_stays_in_the_queue(tmp_path: Path) -> None:
    conn, _ = scene(tmp_path)
    job_id = queue.enqueue(conn, opportunity(conn))

    queue.set_waiting(conn, job_id, queue.OUTSIDE_WINDOW)

    job = job_row(conn, job_id)
    assert job["state"] == queue.WAITING
    assert job["wait_reason"] == queue.OUTSIDE_WINDOW
    assert queue.next_job(conn)["id"] == job_id


def test_waiting_can_be_recorded_repeatedly_without_harm(tmp_path: Path) -> None:
    """The worker writes this on every cycle a job stays ineligible."""
    conn, _ = scene(tmp_path)
    job_id = queue.enqueue(conn, opportunity(conn))

    for _ in range(5):
        queue.set_waiting(conn, job_id, queue.HIGH_LOAD)

    assert job_row(conn, job_id)["state"] == queue.WAITING
    assert len(queue.jobs(conn)) == 1


def test_a_stale_job_stops_rather_than_waiting(tmp_path: Path) -> None:
    """No amount of patience brings the old bytes back."""
    conn, _ = scene(tmp_path)
    job_id = queue.enqueue(conn, opportunity(conn))

    queue.mark_stale(conn, job_id)

    assert job_row(conn, job_id)["state"] == queue.STALE
    assert queue.next_job(conn) is None


def test_every_wait_reason_has_words(tmp_path: Path) -> None:
    for reason in (
        queue.HIGHER_PRIORITY, queue.OUTSIDE_WINDOW, queue.MANUAL_ONLY,
        queue.PROTECTED, queue.SOURCE_CHANGED, queue.ANOTHER_RUNNING,
        queue.HIGH_LOAD, queue.NO_DISK, queue.UNSUPPORTED,
    ):
        assert queue.WAIT_TEXT[reason]


def test_the_queue_survives_a_restart(tmp_path: Path) -> None:
    """State is a row, not memory. A worker restart loses nothing."""
    conn, settings = scene(tmp_path)
    job_id = queue.enqueue(conn, opportunity(conn))
    queue.set_waiting(conn, job_id, queue.OUTSIDE_WINDOW)
    conn.close()

    reopened = connect(settings)

    job = job_row(reopened, job_id)
    assert job["state"] == queue.WAITING
    assert job["wait_reason"] == queue.OUTSIDE_WINDOW


# --- staging containment ---------------------------------------------------------------


def test_staging_lives_under_appdata_never_the_library(tmp_path: Path) -> None:
    """An incomplete encode must not be visible to Browse, to Search, or to
    whoever is looking at the folder over SMB while it is written."""
    _, settings = scene(tmp_path)

    root = queue.staging_root(settings)

    assert settings.appdata_dir in root.parents or root.parent == settings.appdata_dir
    assert settings.library_dir not in root.parents


def test_a_job_gets_its_own_directory(tmp_path: Path) -> None:
    _, settings = scene(tmp_path)

    first, second = queue.job_staging_dir(settings, 1), queue.job_staging_dir(settings, 2)

    assert first != second
    assert first.parent == queue.staging_root(settings)


def test_a_job_id_cannot_escape_the_staging_root(tmp_path: Path) -> None:
    """The id is coerced to an integer, so no path fragment survives it."""
    _, settings = scene(tmp_path)

    with pytest.raises((ValueError, TypeError)):
        queue.job_staging_dir(settings, "../../etc")  # type: ignore[arg-type]


# --- tier five ---------------------------------------------------------------------
#
# Priority is the ordering inside `Worker.run_once`, not a scheduler. These
# tests drive the real worker rather than the decision function, because the
# ordering is the guarantee and a mocked worker would prove nothing about it.


def worker_scene(tmp_path: Path):
    from librairy.worker import Worker

    conn, settings = scene(tmp_path)
    return Worker(conn, settings), conn, settings


def matching_opportunity(conn, **kwargs) -> int:
    """An opportunity whose frozen fingerprint is the file's actual one.

    The decision helpers take a fingerprint directly, so they do not care. The
    worker reads the real index, so a made-up value there is a *changed
    source* and every worker test would prove only that staleness works.
    """
    relpath = kwargs.get("relpath", "Music/concert.wav")
    row = conn.execute(
        "SELECT fingerprint FROM items WHERE relpath=?", (relpath,)
    ).fetchone()
    return opportunity(conn, fingerprint=(row["fingerprint"] or ""), **kwargs)


def test_optimization_sits_below_the_whole_library_audit(tmp_path: Path) -> None:
    """An audit waiting for its turn goes first. Optimization is offered only
    on a cycle that had no audit slice to run."""
    from librairy.audit_job import enqueue as enqueue_audit

    worker, conn, _ = worker_scene(tmp_path)
    queue.enqueue(conn, matching_opportunity(conn))
    enqueue_audit(conn)

    worker.run_once()

    assert job_row(conn, 1)["state"] == queue.QUEUED, "the audit should have gone first"


def test_inbox_work_prevents_a_new_optimization_start(tmp_path: Path) -> None:
    worker, conn, settings = worker_scene(tmp_path)
    queue.enqueue(conn, matching_opportunity(conn))
    (settings.inbox_dir / "arrived.txt").write_text("new", encoding="utf-8")

    summary = worker.run_once()

    assert summary.did_work, "the inbox file should have been picked up"
    assert job_row(conn, 1)["state"] == queue.QUEUED


def test_a_settled_inbox_does_not_starve_optimization(tmp_path: Path) -> None:
    """The bug the audit shipped with once: "files exist" is not "work
    happened", and a NAS whose inbox is never empty would otherwise never run
    maintenance at all."""
    worker, conn, settings = worker_scene(tmp_path)
    (settings.inbox_dir / "already-here.txt").write_text("settled", encoding="utf-8")
    for _ in range(6):
        worker.run_once()
    queue.enqueue(conn, matching_opportunity(conn))

    for _ in range(20):
        worker.run_once()
        if job_row(conn, 1)["state"] != queue.QUEUED:
            break

    assert job_row(conn, 1)["state"] != queue.QUEUED, "never evaluated"


def test_the_worker_records_why_a_job_is_waiting(tmp_path: Path) -> None:
    worker, conn, _ = worker_scene(tmp_path)
    conn.execute(
        "INSERT OR REPLACE INTO settings(key, value) VALUES "
        "('optimization.window', '[\"03:00\", \"03:01\"]')"
    )
    queue.enqueue(conn, matching_opportunity(conn))

    for _ in range(4):
        worker.run_once()

    job = job_row(conn, 1)
    # Either it is inside that one-minute window and eligible, or it is not
    # and says so. Both are correct; silently doing neither is not.
    assert job["state"] in {queue.QUEUED, queue.WAITING}
    if job["state"] == queue.WAITING:
        assert job["wait_reason"] == queue.OUTSIDE_WINDOW


def test_a_changed_source_is_marked_stale_by_the_worker(tmp_path: Path) -> None:
    worker, conn, settings = worker_scene(tmp_path)
    queue.enqueue(conn, opportunity(conn, fingerprint="fp-that-never-existed"))

    for _ in range(4):
        worker.run_once()

    assert job_row(conn, 1)["state"] == queue.STALE


def test_the_worker_never_starts_an_encoder(tmp_path: Path) -> None:
    """There is no executor yet, and an eligible job must stay queued rather
    than claim a `running` state with no process behind it."""
    import subprocess

    worker, conn, _ = worker_scene(tmp_path)
    queue.enqueue(conn, matching_opportunity(conn))
    started: list[list[str]] = []
    real_run = subprocess.run
    original = subprocess.run
    subprocess.run = lambda cmd, **kw: started.append(list(cmd)) or real_run(cmd, **kw)
    try:
        for _ in range(3):
            worker.run_once()
    finally:
        subprocess.run = original

    assert all("ffmpeg" not in str(command[0]) for command in started), started
    assert job_row(conn, 1)["state"] in {queue.QUEUED, queue.WAITING, queue.STALE}


def test_a_broken_governor_does_not_stop_the_worker(tmp_path: Path) -> None:
    """The inbox is the job; maintenance is the extra."""
    worker, conn, _ = worker_scene(tmp_path)
    queue.enqueue(conn, matching_opportunity(conn))
    original = queue.decide
    queue.decide = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        summary = worker.run_once()
    finally:
        queue.decide = original

    assert summary is not None


# --- Run now, end to end through the worker ----------------------------------------


def test_run_now_records_a_forced_policy_rather_than_starting_anything(
    tmp_path: Path,
) -> None:
    """A request handler cannot own a child process, so `Run now` does not
    start one. It records that the clock no longer applies and the worker
    picks the job up on its next idle cycle."""
    from librairy.web.review import apply_queue_action

    conn, settings = scene(tmp_path)
    job_id = queue.enqueue(conn, opportunity(conn))

    result = apply_queue_action(conn, "run-now", [job_id], settings)

    assert "will start" in result
    assert job_row(conn, job_id)["run_policy"] == queue.FORCED
    assert job_row(conn, job_id)["state"] == queue.QUEUED
    assert job_row(conn, job_id)["pid"] is None


def test_a_forced_job_still_waits_for_everything_that_is_not_the_clock(
    tmp_path: Path,
) -> None:
    conn, _ = scene(tmp_path)
    job = job_row(conn, queue.enqueue(conn, opportunity(conn), run_policy=queue.FORCED))

    # Afternoon: the clock alone would have refused this.
    assert queue.decide(job, state(now=AFTERNOON), forced=True).eligible is True
    blocked = queue.decide(job, state(now=AFTERNOON, running_jobs=1), forced=True)

    assert blocked.eligible is False
    assert blocked.reason == queue.ANOTHER_RUNNING


def test_run_now_does_nothing_to_a_job_that_is_not_waiting(tmp_path: Path) -> None:
    from librairy.web.review import apply_queue_action

    conn, settings = scene(tmp_path)
    job_id = queue.enqueue(conn, opportunity(conn))
    conn.execute(
        "UPDATE optimization_jobs SET state=? WHERE id=?", (queue.RUNNING, job_id)
    )

    result = apply_queue_action(conn, "run-now", [job_id], settings)

    assert "not waiting" in result
    assert job_row(conn, job_id)["run_policy"] == "window"


# --- the window closes; the job does not stop --------------------------------------


def test_a_job_that_started_in_the_window_is_not_killed_when_it_closes(
    tmp_path: Path,
) -> None:
    """A transcode begun at 05:55 runs past 06:00. Killing it at the boundary
    would throw away an hour of work and leave a half-written file, and the
    window was never a statement about how much machine a job may use — only
    about when one may begin.

    Asserted structurally as well as behaviourally: nothing in the eligibility
    module reaches a process at all, so there is no code that *could* stop one
    on a clock.
    """
    import inspect

    conn, _ = scene(tmp_path)
    job_id = queue.enqueue(conn, opportunity(conn))
    conn.execute(
        "UPDATE optimization_jobs SET state=? WHERE id=?", (queue.RUNNING, job_id)
    )

    # Long past the end of the window, `decide` is not even asked about it:
    # `next_job` only ever returns something still waiting to start.
    assert queue.next_job(conn) is None
    assert job_row(conn, job_id)["state"] == queue.RUNNING

    source = inspect.getsource(queue)
    assert "import signal" not in source
    assert ".terminate()" not in source
    assert ".kill()" not in source
    assert "os.kill" not in source
