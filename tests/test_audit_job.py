"""A reconciliation that runs behind the inbox, in slices, and can be stopped.

The load-bearing test is the priority one: a cycle that found inbox work must
not spend a millisecond on the library. Everything else here — slicing,
resuming, cancelling — is about making a long job survivable, but that one is
about not making the important job slower.
"""

from __future__ import annotations

from pathlib import Path

from librairy.audit_job import (
    CANCELLED,
    COMPLETE,
    QUEUED,
    RUNNING,
    STAGE_ORDER,
    advance,
    cancel,
    counters_by_root,
    current,
    enqueue,
    progress,
)
from librairy.config import Settings
from librairy.db import connect
from librairy.scanner import scan_root


def settings_for(tmp_path: Path) -> Settings:
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        FILE_STABILITY_SECONDS=0,
        AUTH_REQUIRED=False,
        # No provider, so the catalog stage is a no-op and nothing dials out.
        OLLAMA_HOST="",
        _env_file=None,
    )
    for directory in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        directory.mkdir(parents=True, exist_ok=True)
    return settings


def library(tmp_path: Path, files: int = 6):
    settings = settings_for(tmp_path)
    for number in range(files):
        path = settings.library_dir / "Music" / "Pop" / "Abba" / f"{number:02d} - Song.flac"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"audio" + bytes([number]))
    (settings.library_dir / "Music" / "Pop" / ".DS_Store").write_bytes(b"junk")
    conn = connect(settings)
    scan_root(conn, "library", settings.library_dir, settings)
    return conn, settings


def drain(conn, settings, limit: int = 40):
    """Run slices until the run finishes. Returns how many it took."""
    for count in range(1, limit + 1):
        if advance(conn, settings).finished:
            return count
    raise AssertionError("the audit never finished")


def state(conn) -> str:
    return current(conn)["state"]


# --- the queue ----------------------------------------------------------------


def test_asking_for_an_audit_returns_at_once(tmp_path: Path) -> None:
    conn, _ = library(tmp_path)

    run_id = enqueue(conn)

    assert run_id
    assert state(conn) == QUEUED
    assert conn.execute("SELECT count(*) FROM audit_findings").fetchone()[0] == 0


def test_asking_twice_for_the_same_scope_asks_once(tmp_path: Path) -> None:
    """An audit is idempotent, so a second press is the same question."""
    conn, _ = library(tmp_path)

    first, second = enqueue(conn, "Music"), enqueue(conn, "Music")

    assert first == second
    assert conn.execute("SELECT count(*) FROM audit_runs").fetchone()[0] == 1


def test_a_different_scope_is_a_different_question(tmp_path: Path) -> None:
    conn, _ = library(tmp_path)

    assert enqueue(conn, "Music") != enqueue(conn, "Photos")


def test_advancing_with_nothing_queued_does_nothing(tmp_path: Path) -> None:
    """The worker asks on every cycle, so asking has to be free."""
    conn, settings = library(tmp_path)

    result = advance(conn, settings)

    assert result.ran is False
    assert current(conn) is None


# --- running it ----------------------------------------------------------------


def test_a_run_reaches_every_stage_and_completes(tmp_path: Path) -> None:
    conn, settings = library(tmp_path)
    enqueue(conn)

    drain(conn, settings)

    row = current(conn)
    assert row["state"] == COMPLETE
    assert row["stage"] == STAGE_ORDER[-1]
    assert row["finished_at"]


def test_the_findings_appear_only_when_the_run_finishes(tmp_path: Path) -> None:
    """`record_findings` retires every open row it was not told about, so
    recording per stage would have each stage deleting the one before it."""
    conn, settings = library(tmp_path)
    enqueue(conn)
    advance(conn, settings, seconds=0)

    assert conn.execute("SELECT count(*) FROM audit_findings").fetchone()[0] == 0

    drain(conn, settings)

    assert conn.execute("SELECT count(*) FROM audit_findings").fetchone()[0] >= 1


def test_a_slice_that_runs_out_of_time_resumes_where_it_stopped(tmp_path: Path) -> None:
    """A deadline in the past: every slice does one step and returns."""
    conn, settings = library(tmp_path)
    enqueue(conn)

    stages = []
    for _ in range(40):
        result = advance(conn, settings, seconds=0)
        stages.append(result.stage)
        if result.finished:
            break

    assert len(stages) > 1, "the whole run happened in one slice despite the deadline"
    assert state(conn) == COMPLETE
    # It moved forward every time rather than restarting.
    positions = [STAGE_ORDER.index(stage) for stage in stages if stage in STAGE_ORDER]
    assert positions == sorted(positions)


def test_the_work_already_done_survives_a_slice_boundary(tmp_path: Path) -> None:
    """Tags read in one slice are not re-read in the next.

    Every slice runs with a deadline already past, so each does one step. The
    file count must climb monotonically to the total — if a slice restarted
    its stage the count would fall back, and if it double-counted it would
    overshoot.
    """
    conn, settings = library(tmp_path, files=8)
    enqueue(conn)

    counts = []
    for _ in range(40):
        result = advance(conn, settings, seconds=0)
        counts.append(progress(conn)["counters"].files_checked)
        if result.finished:
            break

    assert counts == sorted(counts), "a slice restarted work already done"
    final = progress(conn)["counters"]
    # Eight tracks. The `.DS_Store` is walked separately — Browse hides it and
    # the audit finds it as junk — so it is not one of the files being checked.
    assert final.files_seen == 8
    assert final.files_checked == final.files_seen, "some file was never looked at"
    assert max(counts) == final.files_seen, "a file was counted twice"


# --- stopping it ---------------------------------------------------------------


def test_a_queued_run_can_be_cancelled_before_it_starts(tmp_path: Path) -> None:
    conn, settings = library(tmp_path)
    enqueue(conn)

    assert cancel(conn) is True
    assert state(conn) == CANCELLED

    assert advance(conn, settings).ran is False


def test_a_running_audit_stops_at_its_next_check(tmp_path: Path) -> None:
    conn, settings = library(tmp_path)
    enqueue(conn)
    advance(conn, settings, seconds=0)
    assert state(conn) == RUNNING

    cancel(conn)
    advance(conn, settings)

    assert state(conn) == CANCELLED


def test_cancelling_leaves_the_library_untouched(tmp_path: Path) -> None:
    """Safe by construction: an audit only ever reads."""
    conn, settings = library(tmp_path)
    before = sorted(
        (path.relative_to(settings.library_dir).as_posix(), path.stat().st_size)
        for path in settings.library_dir.rglob("*")
        if path.is_file()
    )
    enqueue(conn)
    advance(conn, settings, seconds=0)
    cancel(conn)
    advance(conn, settings)

    after = sorted(
        (path.relative_to(settings.library_dir).as_posix(), path.stat().st_size)
        for path in settings.library_dir.rglob("*")
        if path.is_file()
    )
    assert after == before


def test_cancelling_nothing_says_so(tmp_path: Path) -> None:
    conn, _ = library(tmp_path)

    assert cancel(conn) is False


# --- the priority rule ---------------------------------------------------------


def test_a_cycle_with_inbox_work_does_not_touch_the_audit(tmp_path: Path) -> None:
    """The rule the whole design exists for.

    A file in the inbox means the worker found work, and a worker that found
    work spends none of that cycle on the library. The audit waits.
    """
    from librairy.worker import Worker

    conn, settings = library(tmp_path)
    (settings.inbox_dir / "arrived.txt").write_text("new", encoding="utf-8")
    enqueue(conn)

    summary = Worker(conn, settings).run_once()

    assert summary.work_found, "the inbox file should have been picked up"
    assert state(conn) == QUEUED, "the audit ran while there was inbox work to do"


def test_a_settled_inbox_does_not_starve_the_audit(tmp_path: Path) -> None:
    """The live bug: an inbox with files in it is not an inbox doing work.

    The scan counts every file it walks, not the new ones, so "did the worker
    find work" was true for as long as the inbox was not empty. On the real
    installation that meant 95 every cycle, forever, and a queued audit that
    never ran. What matters is whether the cycle *changed* anything.
    """
    from librairy.worker import Worker

    conn, settings = library(tmp_path)
    (settings.inbox_dir / "already-here.txt").write_text("settled", encoding="utf-8")
    worker = Worker(conn, settings)
    # First cycles do the real work: discover, hash, classify.
    for _ in range(6):
        worker.run_once()
    settled = worker.run_once()

    assert settled.scanned, "the file is still in the inbox and still counted"
    assert not settled.did_work, "nothing actually changed this cycle"

    enqueue(conn)
    for _ in range(40):
        worker.run_once()
        if state(conn) == COMPLETE:
            break

    assert state(conn) == COMPLETE, "the audit was starved by a settled inbox"


def test_an_idle_cycle_gives_the_audit_a_slice(tmp_path: Path) -> None:
    from librairy.worker import Worker

    conn, settings = library(tmp_path)
    worker = Worker(conn, settings)
    worker.run_once()  # let the first cycle settle the library scan

    enqueue(conn)
    for _ in range(30):
        worker.run_once()
        if state(conn) == COMPLETE:
            break

    assert state(conn) == COMPLETE


def test_a_failing_stage_fails_the_run_and_not_the_worker(tmp_path: Path) -> None:
    import librairy.audit_stages as stages
    from librairy.worker import Worker

    conn, settings = library(tmp_path)
    enqueue(conn)
    original = stages.STAGE_HANDLERS["structure"]

    def explode(_context):
        raise RuntimeError("detector blew up")

    stages.STAGE_HANDLERS["structure"] = explode
    try:
        summary = Worker(conn, settings).run_once()
    finally:
        stages.STAGE_HANDLERS["structure"] = original

    assert summary is not None, "the worker cycle survived"
    assert state(conn) == "failed"
    assert "detector blew up" in current(conn)["error"]


# --- what it reports -----------------------------------------------------------


def test_progress_is_none_until_an_audit_is_asked_for(tmp_path: Path) -> None:
    """"No issues" and "nobody has looked" are different claims."""
    conn, _ = library(tmp_path)

    assert progress(conn) is None


def test_progress_names_the_stage_in_words(tmp_path: Path) -> None:
    conn, settings = library(tmp_path)
    enqueue(conn)
    advance(conn, settings, seconds=0)

    shown = progress(conn)

    assert shown["stage_label"] in {
        "Scanning", "Reading metadata", "Structure and convention",
        "Catalogs", "Artwork", "Duplicates", "AI", "Finishing",
    }
    assert shown["live"] is True


def test_a_finished_run_reports_a_hundred_percent(tmp_path: Path) -> None:
    conn, settings = library(tmp_path)
    enqueue(conn)
    drain(conn, settings)

    shown = progress(conn)

    assert shown["percent"] == 100
    assert shown["live"] is False


def test_only_counters_that_happened_are_listed(tmp_path: Path) -> None:
    """A line saying "0 catalog requests" reads as a failure, not a fact."""
    conn, settings = library(tmp_path)
    enqueue(conn)
    drain(conn, settings)

    labels = [label for label, _ in progress(conn)["rows"]]

    assert "Catalog requests" not in labels
    assert "Issues found" in labels


def test_counters_are_broken_down_by_top_level_folder() -> None:
    files = ["Music/a.flac", "Music/b.flac", "Photos/c.jpg"]

    assert counters_by_root(files, 3) == {"Music": [2, 2], "Photos": [1, 1]}
    assert counters_by_root(files, 2) == {"Music": [2, 2], "Photos": [0, 1]}
    assert counters_by_root(files, 0) == {"Music": [0, 2], "Photos": [0, 1]}


# --- three tiers ---------------------------------------------------------------


def test_a_targeted_audit_is_worked_before_the_whole_library(tmp_path: Path) -> None:
    """Somebody who pressed Audit on a folder is probably watching the page.
    The sweep is maintenance and can wait for them."""
    conn, settings = library(tmp_path)
    whole = enqueue(conn)
    targeted = enqueue(conn, "Music")

    assert whole < targeted, "the sweep was asked for first"
    assert advance(conn, settings, seconds=0).ran
    assert current(conn)["id"] == targeted


def test_the_whole_library_run_resumes_once_the_targeted_one_is_done(
    tmp_path: Path,
) -> None:
    conn, settings = library(tmp_path)
    whole = enqueue(conn)
    targeted = enqueue(conn, "Music")

    def state_of(run_id: int) -> str:
        return conn.execute(
            "SELECT state FROM audit_runs WHERE id=?", (run_id,)
        ).fetchone()["state"]

    finished_first = None
    for _ in range(80):
        advance(conn, settings)
        if finished_first is None and state_of(targeted) == COMPLETE:
            finished_first = state_of(whole)
        if state_of(whole) == COMPLETE:
            break

    # The sweep had not started when the targeted run finished, and it did
    # finish afterwards. Waiting its turn, not skipped.
    assert finished_first == QUEUED
    assert state_of(whole) == COMPLETE


def test_neither_tier_runs_on_a_cycle_that_did_inbox_work(tmp_path: Path) -> None:
    """Priority 1 is not in the table at all. It is the ordering in the
    worker, and it applies to targeted audits exactly as it does to sweeps."""
    from librairy.worker import Worker

    conn, settings = library(tmp_path)
    (settings.inbox_dir / "arrived.txt").write_text("new", encoding="utf-8")
    enqueue(conn, "Music")

    Worker(conn, settings).run_once()

    assert state(conn) == QUEUED


def test_arriving_inbox_work_gets_the_next_cycle(tmp_path: Path) -> None:
    """The other direction: a running audit must not hold up a new file."""
    from librairy.worker import Worker

    conn, settings = library(tmp_path)
    worker = Worker(conn, settings)
    worker.run_once()
    enqueue(conn)
    advance(conn, settings, seconds=0)
    assert state(conn) == RUNNING

    (settings.inbox_dir / "arrived.txt").write_text("new", encoding="utf-8")
    summary = worker.run_once()

    assert summary.did_work, "the new file was picked up on the very next cycle"
    assert state(conn) == RUNNING, "and the audit was left exactly where it was"


def test_yielding_is_recorded_by_the_worker_not_guessed_from_a_clock(
    tmp_path: Path,
) -> None:
    """`Paused` because no slice ran for half a second would be inventing a
    state. Only the worker knows whether it chose the inbox instead."""
    from librairy.worker import Worker

    conn, settings = library(tmp_path)
    enqueue(conn)
    worker = Worker(conn, settings)
    (settings.inbox_dir / "arrived.txt").write_text("new", encoding="utf-8")

    worker.run_once()
    assert progress(conn)["yielding"] is True

    for _ in range(10):
        worker.run_once()
        if not progress(conn)["yielding"]:
            break
    assert progress(conn)["yielding"] is False


# --- progress that means something ---------------------------------------------


def test_no_fake_percentage_where_nothing_is_being_counted(tmp_path: Path) -> None:
    """Scanning is one directory walk. A bar on it is an animation."""
    conn, settings = library(tmp_path)
    enqueue(conn)

    shown = progress(conn)

    assert shown["percent"] is None
    assert shown["stage_number"] == 1
    assert shown["stage_count"] == len(STAGE_ORDER)


def test_the_metadata_stage_counts_real_files(tmp_path: Path) -> None:
    conn, settings = library(tmp_path, files=8)
    enqueue(conn)
    for _ in range(40):
        advance(conn, settings, seconds=0)
        shown = progress(conn)
        if shown["stage"] == "metadata" and shown["counters"].files_checked:
            break

    assert shown["percent"] is not None
    assert 0 <= shown["percent"] <= 100
    assert "files read" in shown["stage_detail"]
    assert str(shown["counters"].files_seen) in shown["stage_detail"]


def test_stage_counters_do_not_reset_across_slices(tmp_path: Path) -> None:
    conn, settings = library(tmp_path, files=8)
    enqueue(conn)
    seen = []
    for _ in range(40):
        result = advance(conn, settings, seconds=0)
        seen.append(progress(conn)["counters"].files_checked)
        if result.finished:
            break

    assert seen == sorted(seen)
    assert max(seen) == progress(conn)["counters"].files_seen


def test_a_finished_run_stops_asking_to_be_polled(tmp_path: Path) -> None:
    conn, settings = library(tmp_path)
    enqueue(conn)
    drain(conn, settings)

    shown = progress(conn)

    assert shown["live"] is False
    assert shown["percent"] == 100
    assert shown["yielding"] is False


def test_the_stage_label_is_words_not_a_function_name(tmp_path: Path) -> None:
    conn, settings = library(tmp_path)
    enqueue(conn)
    for _ in range(40):
        result = advance(conn, settings, seconds=0)
        label = progress(conn)["stage_label"]
        assert not label.startswith("_"), label
        assert label[0].isupper(), label
        if result.finished:
            break


def test_the_progress_panel_can_prove_which_tools_ran(tmp_path: Path) -> None:
    """A stage name is not evidence. An `AI` stage that called nothing looked
    exactly like one that called something, which is how a stub survived."""
    from librairy.audit_job import Counters, _tool_rows

    tools = dict(
        _tool_rows(
            Counters(
                files_checked=140,
                catalog_requests=2,
                catalog_matches=1,
                artwork_checked=1,
                artwork_total=2,
                duplicate_clusters=0,
                ai_candidates=1,
                ai_calls=1,
                ai_answers=0,
                ai_unavailable=1,
            )
        )
    )

    assert tools["Catalogs"] == "2 requests · 1 match"
    assert tools["Artwork"] == "1 of 2 albums checked"
    assert tools["Duplicates"] == "0 exact sets"
    assert tools["AI"] == "0 of 1 answered · 1 not reviewed"


def test_a_tool_that_did_nothing_is_not_listed(tmp_path: Path) -> None:
    """Except AI, which reports at zero on purpose — see `_counter_rows`."""
    from librairy.audit_job import Counters, _tool_rows

    tools = dict(_tool_rows(Counters(files_checked=10)))

    assert "Catalogs" not in tools
    assert "Artwork" not in tools
    assert "AI" not in tools
