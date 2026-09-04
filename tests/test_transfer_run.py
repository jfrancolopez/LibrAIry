"""Executing a plan, and being unable to execute anything else.

The adapter takes a plan and copies what the plan named. It has no function
that accepts a command, a verb or a list of options — the same pattern the
policy vocabulary uses, carried through to execution: the dangerous thing is
not forbidden, it is *unrepresentable*.

Most of these run a stub instead of rclone, so the exact argv can be inspected
without a network. One at the end runs the real thing against two temporary
directories, because a command that is correct in a test and wrong in the world
is the failure mode a stub cannot catch. It skips cleanly when rclone is not
installed.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from librairy import destinations as dest
from librairy import transfer_paths, transfer_plan, transfer_run
from librairy.config import Settings
from librairy.db import connect
from librairy.transfer_plan import DestinationFile

rclone_installed = pytest.mark.skipif(
    shutil.which("rclone") is None, reason="rclone is not installed"
)


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
    """Stands in for rclone, and records exactly what it was asked to run."""

    def __init__(self, returncode: int = 0, stderr: str = "") -> None:
        self.commands: list[list[str]] = []
        self.returncode = returncode
        self.stderr = stderr

    def __call__(self, command: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
        del timeout
        self.commands.append(list(command))
        return subprocess.CompletedProcess(command, self.returncode, "", self.stderr)


def scene(tmp_path: Path, mode: str = dest.BACKUP, *, identity: str = ""):  # noqa: ANN201
    settings = settings_for(tmp_path)
    conn = connect(settings)
    target = tmp_path / "backup"
    target.mkdir(exist_ok=True)
    if identity:
        transfer_paths.register(target, identity)
    made = dest.add_destination(
        conn,
        name="Spare",
        kind=dest.LOCAL,
        target=str(target),
        modes=[mode],
        identity=identity,
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


def plan_for(conn, policy, destination, there=()):  # noqa: ANN001, ANN201
    return transfer_plan.plan_for(conn, policy, destination, list(there))


# --- only copies, only from a plan --------------------------------------------------


def test_the_only_command_it_can_run_is_a_copy(tmp_path: Path) -> None:
    conn, settings, policy, destination, _target = scene(tmp_path)
    library(conn, "Photos/a.jpg")
    stub = Stub()

    transfer_run.send(conn, settings, plan_for(conn, policy, destination), runner=stub)

    assert len(stub.commands) == 1
    assert stub.commands[0][:2] == ["rclone", "copy"]


def test_there_is_no_way_to_ask_it_to_run_something_else() -> None:
    """The pattern the policy vocabulary uses, carried into execution.

    No public function here takes a command, a verb, or options. The dangerous
    thing is not forbidden — it cannot be expressed.
    """
    import inspect

    public = {
        name: value
        for name, value in vars(transfer_run).items()
        if not name.startswith("_")
        and inspect.isfunction(value)
        #  Defined here, not imported into here. `utc_now` is somebody else's
        #  function that happens to be reachable through this namespace.
        and value.__module__ == transfer_run.__name__
    }
    assert set(public) == {"send", "redact", "redacted"}
    parameters = inspect.signature(public["send"]).parameters
    assert set(parameters) == {"conn", "settings", "plan", "runner"}
    #  And `runner` is not a way in: whatever it receives has already been
    #  built and checked by `tools/rclone.py`.
    assert "command" not in parameters
    assert "verb" not in parameters


def test_a_plan_with_nothing_to_do_runs_nothing(tmp_path: Path) -> None:
    conn, settings, policy, destination, _target = scene(tmp_path)
    library(conn, "Photos/a.jpg")
    stub = Stub()

    result = transfer_run.send(
        conn,
        settings,
        plan_for(conn, policy, destination, [DestinationFile("Photos/a.jpg", 100)]),
        runner=stub,
    )

    assert result.ok
    assert stub.commands == []


def test_a_destination_only_file_never_becomes_work(tmp_path: Path) -> None:
    """The whole product, at the point where it would be violated.

    The destination holds a file the library no longer has. Every mode reports
    or keeps it, and the executor is handed nothing about it at all.
    """
    for mode in (dest.BACKUP, dest.MIRROR):
        conn, settings, policy, destination, _target = scene(tmp_path / mode, mode)
        library(conn, "Photos/a.jpg")
        stub = Stub()
        plan = plan_for(
            conn,
            policy,
            destination,
            [DestinationFile("Photos/a.jpg", 100), DestinationFile("Photos/gone.jpg", 9)],
        )
        assert plan.destination_only == 1

        transfer_run.send(conn, settings, plan, runner=stub)

        assert stub.commands == [], mode


# --- the world may have moved since the plan ----------------------------------------


def test_a_source_replaced_by_a_symlink_is_refused(tmp_path: Path) -> None:
    """A plan is not trusted forever. The check happens immediately before the
    copy, not once when the plan was made."""
    conn, settings, policy, destination, _target = scene(tmp_path)
    library(conn, "Photos/a.jpg")
    plan = plan_for(conn, policy, destination)
    outside = tmp_path / "outside"
    outside.mkdir()
    shutil.rmtree(settings.library_dir / "Photos")
    (settings.library_dir / "Photos").symlink_to(outside)
    stub = Stub()

    result = transfer_run.send(conn, settings, plan, runner=stub)

    assert result.outcome == transfer_run.REFUSED
    assert stub.commands == []


def test_an_offline_drive_pulled_after_planning_is_refused(tmp_path: Path) -> None:
    """The sharp case. A drive removed between planning and copying leaves a
    mount point behind, and a mount point is a directory that will happily
    accept files — onto the system disk, looking like it worked."""
    conn, settings, policy, destination, target = scene(
        tmp_path, dest.OFFLINE, identity="wd-8tb"
    )
    library(conn, "Photos/a.jpg")
    plan = plan_for(conn, policy, destination)
    (target / transfer_paths.MARKER).unlink()  # the drive went; the folder stayed
    stub = Stub()

    result = transfer_run.send(conn, settings, plan, runner=stub)

    assert result.outcome == transfer_run.REFUSED
    assert "registered" in result.detail
    assert stub.commands == []


def test_a_destination_that_vanished_is_refused(tmp_path: Path) -> None:
    conn, settings, policy, destination, target = scene(tmp_path)
    library(conn, "Photos/a.jpg")
    plan = plan_for(conn, policy, destination)
    shutil.rmtree(target)
    stub = Stub()

    result = transfer_run.send(conn, settings, plan, runner=stub)

    assert result.outcome == transfer_run.REFUSED
    assert stub.commands == []


def test_an_unreachable_destination_is_not_an_empty_success(tmp_path: Path) -> None:
    conn, settings, policy, destination, _target = scene(tmp_path)
    library(conn, "Photos/a.jpg")
    plan = transfer_plan.plan_for(conn, policy, destination, None)
    stub = Stub()

    result = transfer_run.send(conn, settings, plan, runner=stub)

    assert result.outcome == transfer_run.UNAVAILABLE
    assert not result.ok
    assert not result.current
    assert stub.commands == []


# --- failure ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stderr", "outcome"),
    [
        ("Failed to copy: no space left on device", transfer_run.FULL),
        ("quota exceeded", transfer_run.FULL),
        ("dial tcp: connection refused", transfer_run.INTERRUPTED),
        ("read: connection reset by peer", transfer_run.INTERRUPTED),
        ("directory not found", transfer_run.UNAVAILABLE),
        ("something else entirely", transfer_run.FAILED),
    ],
)
def test_a_failure_is_categorised_rather_than_summarised(
    tmp_path: Path, stderr: str, outcome: str
) -> None:
    """A full destination and a dropped connection want different things from a
    person. "Backup failed" wants nothing from them but worry."""
    conn, settings, policy, destination, _target = scene(tmp_path)
    library(conn, "Photos/a.jpg")

    result = transfer_run.send(
        conn,
        settings,
        plan_for(conn, policy, destination),
        runner=Stub(returncode=1, stderr=stderr),
    )

    assert result.outcome == outcome
    assert not result.current


def test_a_failed_run_is_never_recorded_as_current(tmp_path: Path) -> None:
    """The one bookkeeping rule: a backup that is behind and says it is fine is
    worse than one that is behind."""
    conn, settings, policy, destination, _target = scene(tmp_path)
    library(conn, "Photos/a.jpg")

    for outcome in (1, 2, 143):
        result = transfer_run.send(
            conn,
            settings,
            plan_for(conn, policy, destination),
            runner=Stub(returncode=outcome, stderr="broke"),
        )
        assert not result.current
        assert result.files == 0


def test_a_timeout_is_an_interruption_and_not_a_failure(tmp_path: Path) -> None:
    conn, settings, policy, destination, _target = scene(tmp_path)
    library(conn, "Photos/a.jpg")

    def timing_out(command: list[str], timeout: int):  # noqa: ANN202
        raise subprocess.TimeoutExpired(command, timeout)

    result = transfer_run.send(
        conn, settings, plan_for(conn, policy, destination), runner=timing_out
    )

    assert result.outcome == transfer_run.INTERRUPTED
    assert not result.current


def test_nothing_a_run_does_reaches_the_library(tmp_path: Path) -> None:
    """A backup succeeding or failing changes what is known about a
    *destination*, and nothing about what a file is."""
    conn, settings, policy, destination, _target = scene(tmp_path)
    library(conn, "Photos/a.jpg")
    before = [tuple(row) for row in conn.execute("SELECT * FROM items")]

    transfer_run.send(conn, settings, plan_for(conn, policy, destination), runner=Stub())
    transfer_run.send(
        conn,
        settings,
        plan_for(conn, policy, destination),
        runner=Stub(returncode=1, stderr="broke"),
    )

    assert [tuple(row) for row in conn.execute("SELECT * FROM items")] == before
    assert conn.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 0


# --- secrets ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "gone"),
    [
        ("--password hunter2", "hunter2"),
        ("--pass=s3cret", "s3cret"),
        ("--token abcdef123456", "abcdef123456"),
        ("--client-secret=zzzTOPzzz", "zzzTOPzzz"),
        ("https://bob:swordfish@example.com/x", "swordfish"),
    ],
)
def test_a_secret_is_removed_before_it_is_ever_stored(text: str, gone: str) -> None:
    """Exercised on real strings rather than asserted to exist.

    Redaction happens on the way *into* the record, so a secret that never
    reached it cannot be leaked later by a page, a log line or an export that
    forgot to hide it.
    """
    assert gone not in transfer_run.redact(text)
    assert "***" in transfer_run.redact(text)


def test_a_stored_command_carries_no_credential(tmp_path: Path) -> None:
    conn, settings, policy, destination, _target = scene(tmp_path)
    library(conn, "Photos/a.jpg")

    result = transfer_run.send(
        conn,
        settings,
        plan_for(conn, policy, destination),
        runner=Stub(returncode=1, stderr="auth failed for --password hunter2"),
    )

    assert "hunter2" not in " ".join(result.command)
    assert "hunter2" not in result.detail


# --- and once, for real --------------------------------------------------------------


@rclone_installed
def test_a_real_copy_converges_and_leaves_the_extra_file_alone(tmp_path: Path) -> None:
    """The one test that runs rclone. A command correct in a stub and wrong in
    the world is exactly what a stub cannot catch.

    Three things at once: the copy works, a file the library no longer has
    survives it, and running again is safe.
    """
    conn, settings, policy, destination, target = scene(tmp_path, dest.MIRROR)
    photos = settings.library_dir / "Photos"
    (photos / "a.jpg").write_bytes(b"a" * 100)
    (photos / "b.jpg").write_bytes(b"b" * 100)
    library(conn, "Photos/a.jpg", "Photos/b.jpg")
    #  Something at the destination that the library no longer has.
    (target / "gone.jpg").write_bytes(b"still here")

    first = transfer_run.send(conn, settings, plan_for(conn, policy, destination))

    assert first.ok, first.detail
    assert (target / "a.jpg").read_bytes() == b"a" * 100
    assert (target / "gone.jpg").read_bytes() == b"still here", (
        "a mirror removed a file the library no longer has"
    )

    #  And again: the second run finds everything current and does nothing.
    there = [
        DestinationFile(f"Photos/{path.name}", path.stat().st_size)
        for path in target.iterdir()
        if path.is_file() and path.name != transfer_paths.MARKER
    ]
    second = plan_for(conn, policy, destination, there)

    assert second.to_copy == 0
    assert second.destination_only == 1
    assert transfer_run.send(conn, settings, second).ok
    assert (target / "gone.jpg").exists()


@rclone_installed
def test_a_rerun_after_an_interrupted_copy_finishes_the_job(tmp_path: Path) -> None:
    """Convergence comes from the comparison being repeatable, not from a
    resume protocol. Half a transfer plus another run is a whole transfer."""
    conn, settings, policy, destination, target = scene(tmp_path)
    photos = settings.library_dir / "Photos"
    for name in ("a.jpg", "b.jpg", "c.jpg"):
        (photos / name).write_bytes(name.encode() * 50)
    library(conn, "Photos/a.jpg", "Photos/b.jpg", "Photos/c.jpg", size=150)
    #  What an interrupted run leaves: some of the files, none of the rest.
    (target / "a.jpg").write_bytes(b"a" * 150)

    there = [DestinationFile("Photos/a.jpg", 150)]
    resumed = plan_for(conn, policy, destination, there)
    assert resumed.to_copy == 2  # noqa: PLR2004

    assert transfer_run.send(conn, settings, resumed).ok

    assert {path.name for path in target.iterdir() if path.is_file()} >= {
        "a.jpg",
        "b.jpg",
        "c.jpg",
    }
