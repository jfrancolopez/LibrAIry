"""Online Backup, and the flag this feature deliberately does not have.

The question a backup is easiest to get wrong: *a run that copied 73 of 100
files and then died.* A stored "this destination is up to date" flag has to be
right about every way a transfer can end, and only has to be wrong once for a
backup to sit there saying it is fine.

So nothing stores one. **Whether a destination is current is answered by
comparing, every time it is asked** — and the comparison is cheap, repeatable,
and reads authoritative state on both sides. What is stored is what happened:
this run planned 100, moved 73, and stopped because the disk filled. That is
true regardless of how it ended and stays true afterwards.

Everything below is either that claim, or one of the four things Backup does
about a difference — and the fourth is the one worth guarding.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from librairy import backup_runs, transfer_listing, transfer_run
from librairy import destinations as dest
from librairy.config import Settings
from librairy.db import connect
from librairy.transfer_plan import DestinationFile


def settings_for(tmp_path: Path) -> Settings:
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        FILE_STABILITY_SECONDS=0,
        AUTH_REQUIRED=False,
        _env_file=None,
    )
    for directory in (
        settings.inbox_dir,
        settings.library_dir,
        settings.quarantine_dir,
        settings.appdata_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    (settings.library_dir / "Photos").mkdir(parents=True, exist_ok=True)
    return settings


class Stub:
    def __init__(self, returncode: int = 0, stderr: str = "") -> None:
        self.commands: list[list[str]] = []
        self.returncode = returncode
        self.stderr = stderr

    def __call__(self, command: list[str], timeout: int):  # noqa: ANN204
        del timeout
        self.commands.append(list(command))
        return subprocess.CompletedProcess(command, self.returncode, "", self.stderr)


def scene(tmp_path: Path, mode: str = dest.BACKUP):  # noqa: ANN201
    settings = settings_for(tmp_path)
    conn = connect(settings)
    target = tmp_path / "backup"
    target.mkdir(exist_ok=True)
    made = dest.add_destination(
        conn, name="NAS", kind=dest.LOCAL, target=str(target), modes=[mode]
    )
    dest.set_policy(conn, category="photos", destination_id=made, mode=mode)
    return conn, settings, dest.policies(conn)[0], dest.destination(conn, made), target


def library(conn, *paths: str, size: int = 100) -> None:  # noqa: ANN001
    for relpath in paths:
        conn.execute(
            "INSERT INTO items(root, relpath, size, mtime_ns, state, first_seen_at,"
            " last_seen_at) VALUES ('library', ?, ?, 0, 'committed', 'now', 'now')",
            (relpath, size),
        )


# --- the four things Backup does ----------------------------------------------------


def test_a_file_the_destination_does_not_have_is_copied(tmp_path: Path) -> None:
    conn, settings, policy, destination, _target = scene(tmp_path)
    library(conn, "Photos/a.jpg")
    stub = Stub()

    plan, result = transfer_run.run_policy(
        conn, settings, policy, destination, [], runner=stub
    )

    assert plan.to_copy == 1
    assert result.ok
    assert stub.commands[0][:2] == ["rclone", "copy"]


def test_a_file_that_changed_is_sent_outward(tmp_path: Path) -> None:
    """The library's version wins. Always, and in the only direction there is."""
    conn, settings, policy, destination, _target = scene(tmp_path)
    library(conn, "Photos/a.jpg", size=400)
    stub = Stub()

    plan, _result = transfer_run.run_policy(
        conn,
        settings,
        policy,
        destination,
        [DestinationFile("Photos/a.jpg", 100)],
        runner=stub,
    )

    assert plan.to_update == 1
    assert plan.to_copy == 0
    assert len(stub.commands) == 1


def test_a_current_file_is_not_transferred(tmp_path: Path) -> None:
    conn, settings, policy, destination, _target = scene(tmp_path)
    library(conn, "Photos/a.jpg")
    stub = Stub()

    plan, result = transfer_run.run_policy(
        conn,
        settings,
        policy,
        destination,
        [DestinationFile("Photos/a.jpg", 100)],
        runner=stub,
    )

    assert plan.current == 1
    assert result.ok
    assert stub.commands == []


def test_a_destination_only_file_is_kept_and_counted(tmp_path: Path) -> None:
    """Valid retained recovery material. It is what a backup is *for*, it is
    recorded so it can be shown, and it never becomes work."""
    conn, settings, policy, destination, _target = scene(tmp_path)
    library(conn, "Photos/a.jpg")
    stub = Stub()

    plan, _result = transfer_run.run_policy(
        conn,
        settings,
        policy,
        destination,
        [DestinationFile("Photos/a.jpg", 100), DestinationFile("Photos/last-year.jpg", 9)],
        runner=stub,
    )

    assert plan.destination_only == 1
    assert stub.commands == []
    run = backup_runs.last_run(conn, destination.id)
    assert run is not None
    assert run.destination_only == 1
    assert "only at the destination" in run.summary


# --- partial success ----------------------------------------------------------------


def test_a_run_that_moved_some_and_then_broke_says_exactly_that(
    tmp_path: Path,
) -> None:
    """The case this whole model is shaped around.

    Seventy-three files did reach the destination, and that is true whatever
    happened next. It is recorded, and it is not permission to call the
    destination current — which nothing can, because nothing stores it.
    """
    conn, settings, policy, destination, _target = scene(tmp_path)
    library(conn, *[f"Photos/a{index}.jpg" for index in range(100)])

    _plan, result = transfer_run.run_policy(
        conn,
        settings,
        policy,
        destination,
        [],
        runner=Stub(
            returncode=1,
            stderr="Transferred: 73 / 100, 73%\nFatal error: no space left on device",
        ),
    )

    run = backup_runs.last_run(conn, destination.id)
    assert run is not None
    assert run.state == backup_runs.FAILED
    assert run.planned_copies == 100  # noqa: PLR2004
    assert run.transferred == 73, "rclone's own evidence was thrown away"  # noqa: PLR2004
    assert run.partial
    assert result.outcome == transfer_run.FULL
    assert not result.current


def test_there_is_no_up_to_date_flag_anywhere(tmp_path: Path) -> None:
    """Asserted against the schema, because the guarantee is the *absence*.

    A column called `current`, `up_to_date`, `synced` or `last_good_state` is
    exactly the thing that would have to be right about every partial failure.
    Answering by comparing cannot be stale, because it is not stored.
    """
    conn, _settings, _policy, _destination, _target = scene(tmp_path)

    columns = {
        str(row["name"]).lower()
        for row in conn.execute("PRAGMA table_info(backup_runs)")
    } | {
        str(row["name"]).lower()
        for row in conn.execute("PRAGMA table_info(backup_destinations)")
    }

    for forbidden in ("current", "up_to_date", "uptodate", "synced", "in_sync", "clean"):
        assert forbidden not in columns, f"a {forbidden!r} flag appeared"


def test_a_failed_run_is_followed_by_a_comparison_that_finds_the_rest(
    tmp_path: Path,
) -> None:
    """Convergence comes from comparing again, not from remembering.

    The second run is handed what actually arrived and plans exactly the
    remainder — no resume protocol, no bookkeeping that could be wrong.
    """
    conn, settings, policy, destination, _target = scene(tmp_path)
    library(conn, "Photos/a.jpg", "Photos/b.jpg", "Photos/c.jpg")

    transfer_run.run_policy(
        conn, settings, policy, destination, [], runner=Stub(returncode=1, stderr="broke")
    )
    #  What an interrupted run left behind.
    arrived = [DestinationFile("Photos/a.jpg", 100)]
    plan, result = transfer_run.run_policy(
        conn, settings, policy, destination, arrived, runner=Stub()
    )

    assert plan.to_copy == 2  # noqa: PLR2004
    assert result.ok
    assert backup_runs.last_success(conn, destination.id) is not None


def test_running_twice_against_a_current_destination_moves_nothing(
    tmp_path: Path,
) -> None:
    conn, settings, policy, destination, _target = scene(tmp_path)
    library(conn, "Photos/a.jpg")
    there = [DestinationFile("Photos/a.jpg", 100)]
    stub = Stub()

    transfer_run.run_policy(conn, settings, policy, destination, there, runner=stub)
    transfer_run.run_policy(conn, settings, policy, destination, there, runner=stub)

    assert stub.commands == []
    assert len(backup_runs.recent(conn, destination.id)) == 2  # noqa: PLR2004


# --- the record -----------------------------------------------------------------------


def test_a_run_is_opened_before_anything_moves(tmp_path: Path) -> None:
    """So a process killed mid-transfer leaves a row saying it was running.

    An absence cannot be told from a run that never started, and "we do not
    know how that ended" is a thing this has to be able to say.
    """
    conn, settings, policy, destination, _target = scene(tmp_path)
    library(conn, "Photos/a.jpg")
    seen: list[str] = []

    def dying(command: list[str], timeout: int):  # noqa: ANN202
        del timeout
        row = conn.execute("SELECT state FROM backup_runs ORDER BY id DESC LIMIT 1").fetchone()
        seen.append(str(row["state"]) if row else "no row")
        raise subprocess.TimeoutExpired(command, 1)

    transfer_run.run_policy(conn, settings, policy, destination, [], runner=dying)

    assert seen == [backup_runs.RUNNING]
    assert backup_runs.last_run(conn, destination.id).state == backup_runs.FAILED


def test_last_attempted_and_last_succeeded_are_different_questions(
    tmp_path: Path,
) -> None:
    """A destination attempted hourly and last successful in March is the exact
    state somebody needs to see, and one number cannot say it."""
    conn, settings, policy, destination, _target = scene(tmp_path)
    library(conn, "Photos/a.jpg")

    transfer_run.run_policy(conn, settings, policy, destination, [], runner=Stub())
    transfer_run.run_policy(
        conn, settings, policy, destination, [], runner=Stub(returncode=1, stderr="broke")
    )

    assert backup_runs.last_run(conn, destination.id).state == backup_runs.FAILED
    assert backup_runs.last_success(conn, destination.id).state == backup_runs.SUCCEEDED


def test_an_unreachable_destination_records_no_run_at_all(tmp_path: Path) -> None:
    """A drive that lives in a drawer would otherwise accumulate a history of
    failures that are just Tuesdays."""
    conn, settings, policy, destination, _target = scene(tmp_path)
    library(conn, "Photos/a.jpg")

    _plan, result = transfer_run.run_policy(
        conn, settings, policy, destination, None, runner=Stub()
    )

    assert result.outcome == transfer_run.UNAVAILABLE
    assert backup_runs.recent(conn, destination.id) == []


def test_a_run_holds_no_secret(tmp_path: Path) -> None:
    conn, settings, policy, destination, _target = scene(tmp_path)
    library(conn, "Photos/a.jpg")

    transfer_run.run_policy(
        conn,
        settings,
        policy,
        destination,
        [],
        runner=Stub(returncode=1, stderr="failed: --password hunter2 rejected"),
    )

    stored = " ".join(
        str(value)
        for row in conn.execute("SELECT * FROM backup_runs")
        for value in tuple(row)
    )
    assert "hunter2" not in stored


def test_transfer_history_is_not_library_history(tmp_path: Path) -> None:
    """"312 copied, 9 updated, 14 only at the destination" is not a claim that
    335 things happened to the library. Nothing in it changed."""
    conn, settings, policy, destination, _target = scene(tmp_path)
    library(conn, "Photos/a.jpg")
    before = conn.execute("SELECT COUNT(*) FROM history").fetchone()[0]

    transfer_run.run_policy(conn, settings, policy, destination, [], runner=Stub())

    assert conn.execute("SELECT COUNT(*) FROM history").fetchone()[0] == before
    assert conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 0
    assert backup_runs.recent(conn, destination.id)


# --- scope and cadence ----------------------------------------------------------------


def test_a_policy_covers_its_own_category_only(tmp_path: Path) -> None:
    conn, settings, policy, destination, _target = scene(tmp_path)
    library(conn, "Photos/a.jpg", "Music/b.flac", "Documents/c.pdf")

    plan, _result = transfer_run.run_policy(
        conn, settings, policy, destination, [], runner=Stub()
    )

    assert plan.to_copy == 1


def test_a_disabled_policy_is_never_run(tmp_path: Path) -> None:
    conn, settings, policy, destination, _target = scene(tmp_path)
    library(conn, "Photos/a.jpg")
    dest.set_policy(
        conn, category="photos", destination_id=destination.id, mode=dest.BACKUP, enabled=False
    )

    assert dest.active(conn) == []
    del settings, policy


def test_the_cadence_guard_stops_a_repeated_expensive_comparison(
    tmp_path: Path,
) -> None:
    """The comparison is the expensive half, and comparing three hundred
    thousand photographs every five-second cycle is a machine that does nothing
    else."""
    conn, settings, policy, destination, _target = scene(tmp_path)
    library(conn, "Photos/a.jpg")

    assert backup_runs.due(conn, destination.id, "photos")
    transfer_run.run_policy(conn, settings, policy, destination, [], runner=Stub())

    assert not backup_runs.due(conn, destination.id, "photos")
    assert backup_runs.due(conn, destination.id, "photos", seconds=0)
    #  From the last *attempt*, not the last success: a destination failing
    #  every hour should be retried hourly, and a guard keyed on success would
    #  retry it continuously for as long as it kept failing.
    transfer_run.run_policy(
        conn, settings, policy, destination, [], runner=Stub(returncode=1, stderr="x")
    )
    assert not backup_runs.due(conn, destination.id, "photos")


def test_the_worker_backs_up_after_the_inbox_and_not_instead_of_it(
    tmp_path: Path,
) -> None:
    """Inbox priority, read from the source because the ordering is the
    guarantee: every piece of inbox work in a cycle finishes first."""
    import inspect

    from librairy.worker import Worker

    source = inspect.getsource(Worker.run_once)
    assert source.index("analyze_items") < source.index("self._policy_backups")
    assert source.index("self._inbox_companions") < source.index("self._policy_backups")
    del tmp_path


def test_an_offline_drive_is_not_polled_on_a_schedule() -> None:
    """Polling a drawer every hour to be told it is still a drawer is the retry
    storm this feature exists to avoid. An offline drive is compared when it
    appears, which is presence detection."""
    import inspect

    from librairy.worker import Worker

    source = inspect.getsource(Worker._policy_backups)  # noqa: SLF001
    assert "OFFLINE" in source
    assert "continue" in source


# --- listing --------------------------------------------------------------------------


def test_nobody_could_look_is_not_an_empty_destination(tmp_path: Path) -> None:
    """`None` and `[]` are different answers, and collapsing them would propose
    copying an entire library to somewhere that is not answering."""
    conn, settings, policy, destination, target = scene(tmp_path)

    assert transfer_listing.listing(conn, settings, destination, policy) == []

    target.rmdir()
    assert transfer_listing.listing(conn, settings, destination, policy) is None


def test_a_listing_reads_what_is_there(tmp_path: Path) -> None:
    conn, settings, policy, destination, target = scene(tmp_path)
    (target / "Photos").mkdir()
    (target / "Photos" / "a.jpg").write_bytes(b"x" * 40)

    found = transfer_listing.listing(conn, settings, destination, policy)

    assert found == [DestinationFile("Photos/a.jpg", 40)]
