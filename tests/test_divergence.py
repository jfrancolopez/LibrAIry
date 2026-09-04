"""Mirror: the same machinery, one different cell, and no deletion anywhere.

Mirror means LibrAIry knows the destination differs from the Library. It does
not mean LibrAIry may erase the difference — and the way that stays true is
that Mirror is not a second system. It is Backup with one answer changed in one
cell of the policy matrix:

    Backup   only at destination → keep, quietly
    Mirror   only at destination → keep, and say so

So most of what is tested here is that saying so is *durable and honest*: a
file sitting at a destination across ten runs is one fact about today rather
than ten findings, a file somebody removed by hand clears without anybody
telling this program about it, and a destination holding four hundred thousand
of them produces a number rather than four hundred thousand rows.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from librairy import backup_runs, divergence, transfer_run
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


def scene(tmp_path: Path, mode: str = dest.MIRROR):  # noqa: ANN201
    settings = settings_for(tmp_path)
    conn = connect(settings)
    target = tmp_path / "mirror"
    target.mkdir(exist_ok=True)
    made = dest.add_destination(
        conn, name="Studio", kind=dest.LOCAL, target=str(target), modes=[mode]
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


def run(conn, settings, policy, destination, there, runner=None):  # noqa: ANN001, ANN201
    return transfer_run.run_policy(
        conn, settings, policy, destination, list(there), runner=runner or Stub()
    )


# --- one cell of the matrix ----------------------------------------------------------


def test_mirror_and_backup_differ_in_exactly_one_answer() -> None:
    """The simplicity is the safety feature. A second architecture for Mirror
    is how "represent the current library" becomes "make the destination
    match", which is one word away from deleting things."""
    same = [
        (difference, dest.ACTIONS[(dest.BACKUP, difference)])
        for difference in dest.DIFFERENCES
        if dest.ACTIONS[(dest.BACKUP, difference)] == dest.ACTIONS[(dest.MIRROR, difference)]
    ]
    differ = [
        difference
        for difference in dest.DIFFERENCES
        if dest.ACTIONS[(dest.BACKUP, difference)] != dest.ACTIONS[(dest.MIRROR, difference)]
    ]

    assert len(same) == 3  # noqa: PLR2004
    assert differ == [dest.EXTRA]
    assert dest.ACTIONS[(dest.MIRROR, dest.EXTRA)] == dest.REPORT


def test_mirror_uses_the_same_transfer_adapter_as_backup(tmp_path: Path) -> None:
    conn, settings, policy, destination, _target = scene(tmp_path, dest.MIRROR)
    library(conn, "Photos/a.jpg")
    mirror = Stub()
    run(conn, settings, policy, destination, [], runner=mirror)

    conn2, settings2, policy2, destination2, _t = scene(tmp_path / "b", dest.BACKUP)
    library(conn2, "Photos/a.jpg")
    backup = Stub()
    run(conn2, settings2, policy2, destination2, [], runner=backup)

    #  Identical argv but for the directories: one adapter, one command shape.
    assert mirror.commands[0][:2] == backup.commands[0][:2] == ["rclone", "copy"]
    assert len(mirror.commands[0]) == len(backup.commands[0])


def test_a_file_only_at_the_destination_survives_a_mirror(tmp_path: Path) -> None:
    conn, settings, policy, destination, target = scene(tmp_path)
    library(conn, "Photos/a.jpg")
    (target / "gone.jpg").write_bytes(b"kept")
    stub = Stub()

    run(
        conn,
        settings,
        policy,
        destination,
        [DestinationFile("Photos/a.jpg", 100), DestinationFile("Photos/gone.jpg", 4)],
        runner=stub,
    )

    assert (target / "gone.jpg").read_bytes() == b"kept"
    assert stub.commands == []


# --- the divergence record -----------------------------------------------------------


def test_the_count_is_complete_and_the_paths_are_a_sample(tmp_path: Path) -> None:
    """A destination holding four hundred thousand files the library no longer
    has is worth being told about. Four hundred thousand rows is not the way to
    tell somebody."""
    conn, settings, policy, destination, _target = scene(tmp_path)
    library(conn, "Photos/a.jpg")
    there = [DestinationFile("Photos/a.jpg", 100)] + [
        DestinationFile(f"Photos/old-{index}.jpg", 5)
        for index in range(divergence.KEEP + 250)
    ]

    run(conn, settings, policy, destination, there)

    found = divergence.summary(conn, destination.id)
    assert found.count == divergence.KEEP + 250
    assert found.kept == divergence.KEEP
    assert found.partial, "a page could not say there were more than it holds"
    assert conn.execute("SELECT COUNT(*) FROM backup_divergence").fetchone()[0] == (
        divergence.KEEP
    )


def test_the_paths_can_be_read_a_bounded_page_at_a_time(tmp_path: Path) -> None:
    conn, settings, policy, destination, _target = scene(tmp_path)
    library(conn, "Photos/a.jpg")
    there = [DestinationFile(f"Photos/old-{index:04d}.jpg", 5) for index in range(120)]

    run(conn, settings, policy, destination, there)

    first = divergence.page(conn, destination.id, page_number=1)
    third = divergence.page(conn, destination.id, page_number=3)
    assert len(first) == divergence.PAGE
    assert len(third) == 20  # noqa: PLR2004
    assert not set(path.relpath for path in first) & set(path.relpath for path in third)


def test_running_ten_times_does_not_make_ten_findings(tmp_path: Path) -> None:
    """A file sitting there across ten runs is one fact about today, not ten.

    Run history and current divergence answer different questions, and a table
    that blurred them would grow a row per observation for ever.
    """
    conn, settings, policy, destination, _target = scene(tmp_path)
    library(conn, "Photos/a.jpg")
    there = [DestinationFile("Photos/gone.jpg", 5)]

    for _ in range(10):
        run(conn, settings, policy, destination, there)

    assert divergence.summary(conn, destination.id).count == 1
    assert conn.execute("SELECT COUNT(*) FROM backup_divergence").fetchone()[0] == 1
    #  And the run history *did* record ten runs, because that is the other
    #  question and it has a different answer.
    assert len(backup_runs.recent(conn, destination.id, limit=20)) == 10  # noqa: PLR2004


def test_a_file_removed_by_hand_clears_on_the_next_comparison(
    tmp_path: Path,
) -> None:
    """Nothing has to notice the deletion. The absence is simply not
    re-recorded, which is the only bookkeeping that cannot drift."""
    conn, settings, policy, destination, _target = scene(tmp_path)
    library(conn, "Photos/a.jpg")
    run(conn, settings, policy, destination, [DestinationFile("Photos/gone.jpg", 5)])
    assert divergence.summary(conn, destination.id).count == 1

    run(conn, settings, policy, destination, [])

    found = divergence.summary(conn, destination.id)
    assert found.count == 0
    assert found.kept == 0
    assert not found.any


def test_clearing_a_divergence_is_not_a_library_change(tmp_path: Path) -> None:
    """A file vanishing from a destination is news about the destination."""
    conn, settings, policy, destination, _target = scene(tmp_path)
    library(conn, "Photos/a.jpg")
    run(conn, settings, policy, destination, [DestinationFile("Photos/gone.jpg", 5)])
    before = [tuple(row) for row in conn.execute("SELECT * FROM items")]

    run(conn, settings, policy, destination, [])

    assert [tuple(row) for row in conn.execute("SELECT * FROM items")] == before
    assert conn.execute("SELECT COUNT(*) FROM history").fetchone()[0] == 0


def test_since_when_survives_a_recheck_and_last_seen_moves(tmp_path: Path) -> None:
    """Both dates are worth having, and they are different reassurances:
    "there since March" and "we checked twenty minutes ago"."""
    conn, settings, policy, destination, _target = scene(tmp_path)
    library(conn, "Photos/a.jpg")
    there = [DestinationFile("Photos/gone.jpg", 5)]
    run(conn, settings, policy, destination, there)
    first = divergence.page(conn, destination.id)[0]

    time.sleep(1.1)
    run(conn, settings, policy, destination, there)

    again = divergence.page(conn, destination.id)[0]
    assert again.first_seen_at == first.first_seen_at, "the sighting was re-dated"
    assert again.last_seen_at > first.last_seen_at


def test_a_recheck_in_the_same_second_still_clears_what_went(
    tmp_path: Path,
) -> None:
    """`utc_now()` has one-second granularity.

    The first version deleted "anything not seen since now", which in the same
    second is nothing at all — so a file removed by hand stayed on the page
    until the clock happened to tick. Set arithmetic, not timestamps.
    """
    conn, settings, policy, destination, _target = scene(tmp_path)
    library(conn, "Photos/a.jpg")
    run(conn, settings, policy, destination, [DestinationFile("Photos/gone.jpg", 5)])

    run(conn, settings, policy, destination, [])  # no sleep: same second

    assert divergence.summary(conn, destination.id).kept == 0


# --- a destination copy that differs -------------------------------------------------


def test_a_destination_file_that_differs_is_updated_outward(tmp_path: Path) -> None:
    """Not a two-way conflict. The library's version wins, in the only
    direction there is, and a destination copy is never read as truth."""
    conn, settings, policy, destination, _target = scene(tmp_path)
    library(conn, "Photos/a.jpg", size=400)
    stub = Stub()

    plan, _result = transfer_run.run_policy(
        conn,
        settings,
        policy,
        destination,
        [DestinationFile("Photos/a.jpg", 999_999)],
        runner=stub,
    )

    assert plan.to_update == 1
    assert plan.destination_only == 0
    assert len(stub.commands) == 1


def test_a_failed_update_leaves_the_library_alone(tmp_path: Path) -> None:
    conn, settings, policy, destination, _target = scene(tmp_path)
    library(conn, "Photos/a.jpg", size=400)
    before = [tuple(row) for row in conn.execute("SELECT * FROM items")]

    _plan, result = transfer_run.run_policy(
        conn,
        settings,
        policy,
        destination,
        [DestinationFile("Photos/a.jpg", 100)],
        runner=Stub(returncode=1, stderr="permission denied"),
    )

    assert not result.ok
    assert [tuple(row) for row in conn.execute("SELECT * FROM items")] == before


# --- what it may not do ---------------------------------------------------------------


def test_no_mirror_path_can_emit_a_destructive_command(tmp_path: Path) -> None:
    """Every argv a Mirror produces, checked against both gates."""
    from librairy.tools import rclone

    conn, settings, policy, destination, _target = scene(tmp_path)
    library(conn, "Photos/a.jpg", "Photos/b.jpg")
    stub = Stub()

    run(
        conn,
        settings,
        policy,
        destination,
        [DestinationFile("Photos/x.jpg", 1), DestinationFile("Photos/y.jpg", 2)],
        runner=stub,
    )

    for command in stub.commands:
        assert command[1] in rclone.ALLOWED_VERBS
        assert not rclone.DESTRUCTIVE_VERBS.intersection(command)
        for argument in command[2:]:
            assert not any(
                argument.startswith(flag) for flag in rclone.DESTRUCTIVE_FLAGS
            )


def test_the_divergence_module_touches_no_filesystem() -> None:
    """It records what a comparison found. It has no way to act on it."""
    import inspect

    body = inspect.getsource(divergence).split('"""', 2)[2]
    for call in ("unlink(", "rmtree", "os.remove", "shutil", "subprocess", "Path("):
        assert call not in body, call


def test_a_disabled_mirror_does_nothing(tmp_path: Path) -> None:
    conn, _settings, _policy, destination, _target = scene(tmp_path)
    dest.set_policy(
        conn,
        category="photos",
        destination_id=destination.id,
        mode=dest.MIRROR,
        enabled=False,
    )

    assert dest.active(conn) == []


def test_a_failed_mirror_says_nothing_about_currentness(tmp_path: Path) -> None:
    conn, settings, policy, destination, _target = scene(tmp_path)
    library(conn, "Photos/a.jpg")

    _plan, result = transfer_run.run_policy(
        conn,
        settings,
        policy,
        destination,
        [],
        runner=Stub(returncode=1, stderr="broke"),
    )

    assert not result.current
    assert backup_runs.last_success(conn, destination.id) is None


def test_the_words_do_not_invite_tidying(tmp_path: Path) -> None:
    """"Extra" invites somebody to clear it up and "stale" sounds like rot.
    `docs/ui-vocabulary.md` pins the phrase that states a fact instead."""
    conn, settings, policy, destination, _target = scene(tmp_path)
    library(conn, "Photos/a.jpg")
    run(conn, settings, policy, destination, [DestinationFile("Photos/gone.jpg", 5)])

    sentence = divergence.summary(conn, destination.id).sentence

    assert sentence == "1 only at the destination"
    for word in ("extra", "stale", "orphan", "delete", "clean"):
        assert word not in sentence.lower()
