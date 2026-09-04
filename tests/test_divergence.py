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


def _extra(relpath: str, size: int = 5):  # noqa: ANN202
    from librairy.transfer_plan import Entry

    return Entry(
        relpath=relpath, difference=dest.EXTRA, action=dest.REPORT, destination_size=size
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


def test_every_divergent_file_is_recorded_not_a_sample(tmp_path: Path) -> None:
    """The count and the set are the same thing.

    The first version kept a thousand paths beside a complete count, which is
    true about both numbers and useless to somebody who intends to go and look
    at the files. A bounded response is a different requirement from a
    truncated record of the world.
    """
    conn, settings, policy, destination, _target = scene(tmp_path)
    library(conn, "Photos/a.jpg")
    there = [DestinationFile("Photos/a.jpg", 100)] + [
        DestinationFile(f"Photos/old-{index:06d}.jpg", 5) for index in range(4_250)
    ]

    run(conn, settings, policy, destination, there)

    found = divergence.summary(conn, destination.id)
    assert found.count == 4_250  # noqa: PLR2004
    stored = conn.execute("SELECT COUNT(*) FROM backup_divergence").fetchone()[0]
    assert stored == found.count, "the count promised more than was written down"


def test_all_of_them_can_be_walked_a_bounded_page_at_a_time(tmp_path: Path) -> None:
    """Bounded pages, and every row reachable through them. Both, not either."""
    conn, settings, policy, destination, _target = scene(tmp_path)
    library(conn, "Photos/a.jpg")
    there = [DestinationFile(f"Photos/old-{index:05d}.jpg", 5) for index in range(1_337)]

    run(conn, settings, policy, destination, there)

    walked: list[str] = []
    cursor = ""
    pages = 0
    while True:
        found = divergence.page(conn, destination.id, after=cursor)
        assert len(found) <= divergence.PAGE
        walked.extend(row.relpath for row in found)
        pages += 1
        if not found.more:
            break
        cursor = found.next
        assert pages < 100, "paging did not terminate"  # noqa: PLR2004

    assert len(walked) == 1_337  # noqa: PLR2004
    assert len(set(walked)) == len(walked), "a page repeated a row"
    assert walked == sorted(walked), "the cursor depends on the order"
    assert set(walked) == {found.relpath for found in there}


def test_a_page_says_whether_there_is_another_one(tmp_path: Path) -> None:
    conn, settings, policy, destination, _target = scene(tmp_path)
    library(conn, "Photos/a.jpg")
    run(
        conn,
        settings,
        policy,
        destination,
        [DestinationFile(f"Photos/old-{index:03d}.jpg", 5) for index in range(60)],
    )

    first = divergence.page(conn, destination.id)
    second = divergence.page(conn, destination.id, after=first.next)

    assert first.more and len(first) == divergence.PAGE
    assert not second.more and len(second) == 10  # noqa: PLR2004


def test_the_page_order_matches_sqlites_so_the_merge_is_sound(tmp_path: Path) -> None:
    """`destination_only` walks two sorted streams instead of building a set of
    a million library paths. That only works while SQLite's byte ordering and
    Python's code-point ordering agree, so here are the paths that would show
    they do not."""
    from librairy.transfer_plan import destination_only

    conn, settings, policy, destination, _target = scene(tmp_path)
    awkward = [
        "Photos/Zebra.jpg",
        "Photos/apple.jpg",
        "Photos/Ångström.jpg",
        "Photos/[bracket].jpg",
        "Photos/_underscore.jpg",
        "Photos/éclair.jpg",
        "Photos/日本.jpg",
    ]
    library(conn, *awkward)
    ours = [row.relpath for row in __import__(
        "librairy.transfer_plan", fromlist=["library_files"]
    ).library_files(conn, "photos")]
    assert ours == sorted(awkward), "SQLite and Python disagree about order"

    #  Every library file is present at the destination, so a sound merge finds
    #  nothing divergent. A broken one would report the ones it walked past.
    listing = [DestinationFile(path, 100) for path in awkward]
    assert list(destination_only(conn, policy, listing)) == []

    run(conn, settings, policy, destination, listing)
    assert divergence.summary(conn, destination.id).count == 0


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
    first = divergence.page(conn, destination.id).rows[0]

    time.sleep(1.1)
    run(conn, settings, policy, destination, there)

    again = divergence.page(conn, destination.id).rows[0]
    assert again.first_seen_at == first.first_seen_at, "the sighting was re-dated"
    assert again.last_seen_at > first.last_seen_at


def test_a_recheck_in_the_same_second_still_clears_what_went(
    tmp_path: Path,
) -> None:
    """`utc_now()` has one-second granularity.

    The first version deleted "anything not seen since now", which in the same
    second is nothing at all — so a file removed by hand stayed on the page
    until the clock happened to tick. Membership is a generation counter, which
    always moves.
    """
    conn, settings, policy, destination, _target = scene(tmp_path)
    library(conn, "Photos/a.jpg")
    run(conn, settings, policy, destination, [DestinationFile("Photos/gone.jpg", 5)])

    run(conn, settings, policy, destination, [])  # no sleep: same second

    assert divergence.summary(conn, destination.id).count == 0


# --- a comparison that did not finish -------------------------------------------------


def test_an_incomplete_comparison_erases_nothing(tmp_path: Path) -> None:
    """Half a listing is not evidence that the other half is gone.

    A drive pulled mid-scan would otherwise read as somebody having tidied up,
    and the last set anybody could trust would be replaced by whatever the scan
    happened to reach before the cable moved.
    """
    conn, settings, policy, destination, _target = scene(tmp_path)
    library(conn, "Photos/a.jpg")
    run(
        conn,
        settings,
        policy,
        destination,
        [DestinationFile(f"Photos/gone-{index}.jpg", 5) for index in range(6)],
    )
    whole = divergence.summary(conn, destination.id)
    assert whole.count == 6  # noqa: PLR2004

    divergence.record(
        conn,
        destination_id=destination.id,
        category="photos",
        entries=[_extra("Photos/gone-0.jpg")],
        complete=False,
    )

    found = divergence.summary(conn, destination.id)
    assert found.count == 6, "an unfinished scan deleted five files it never saw"  # noqa: PLR2004
    assert found.unverified, "and it did not admit that it had not finished"
    #  "When anybody last looked" moved; "when the set was last known whole"
    #  did not, because it was not.
    assert found.verified_at == whole.verified_at


def test_a_finished_comparison_after_an_unfinished_one_recovers(tmp_path: Path) -> None:
    conn, settings, policy, destination, _target = scene(tmp_path)
    library(conn, "Photos/a.jpg")
    run(conn, settings, policy, destination, [DestinationFile("Photos/gone.jpg", 5)])
    divergence.record(
        conn,
        destination_id=destination.id,
        category="photos",
        entries=[],
        complete=False,
    )
    assert divergence.summary(conn, destination.id).unverified

    run(conn, settings, policy, destination, [DestinationFile("Photos/gone.jpg", 5)])

    found = divergence.summary(conn, destination.id)
    assert found.count == 1
    assert found.complete and found.verified_at


def test_saying_whether_a_scan_finished_has_no_default() -> None:
    """The one way to lose information here is to claim a partial listing was
    the whole destination. So every call site has to say which it made."""
    import inspect

    signature = inspect.signature(divergence.record)

    assert signature.parameters["complete"].default is inspect.Parameter.empty


def test_one_scope_is_reconciled_without_touching_another(tmp_path: Path) -> None:
    """A comparison of Photos knows nothing about Music, and must not act as
    though the absence of Music files from its listing means anything."""
    conn, settings, policy, destination, _target = scene(tmp_path)
    divergence.record(
        conn,
        destination_id=destination.id,
        category="music",
        entries=[_extra("Music/old.mp3")],
        complete=True,
    )
    divergence.record(
        conn,
        destination_id=destination.id,
        category="photos",
        entries=[_extra("Photos/old.jpg")],
        complete=True,
    )

    divergence.record(
        conn,
        destination_id=destination.id,
        category="photos",
        entries=[],
        complete=True,
    )

    remaining = [row.relpath for row in divergence.page(conn, destination.id)]
    assert remaining == ["Music/old.mp3"]


def test_the_last_known_set_survives_the_drive_going_back_in_the_drawer(
    tmp_path: Path,
) -> None:
    """The whole point of Offline Backup: the answer has to still be there when
    the drive is not. A comparison nobody could make records nothing, so what
    the last one found stays exactly where it was."""
    conn, settings, policy, destination, _target = scene(tmp_path, dest.OFFLINE)
    library(conn, "Photos/a.jpg")
    run(conn, settings, policy, destination, [DestinationFile("Photos/gone.jpg", 5)])
    checked = divergence.summary(conn, destination.id).checked_at

    #  `None` is the listing that means nobody could look.
    plan, result = transfer_run.run_policy(conn, settings, policy, destination, None)

    assert plan.unavailable and result.outcome == transfer_run.UNAVAILABLE
    found = divergence.summary(conn, destination.id)
    assert found.count == 1, "the drive being away emptied what it had found"
    assert found.checked_at == checked, "an unreachable drive re-dated the check"
    assert divergence.page(conn, destination.id).rows[0].relpath == "Photos/gone.jpg"


def test_offline_reports_what_is_only_there_exactly_as_mirror_does(
    tmp_path: Path,
) -> None:
    """Two modes, one behaviour, read off the matrix rather than listed twice."""
    assert tuple(
        mode
        for mode in dest.MODES
        if dest.ACTIONS[(mode, dest.EXTRA)] == dest.REPORT
    ) == dest.REPORTING
    assert dest.MIRROR in dest.REPORTING
    assert dest.OFFLINE in dest.REPORTING
    assert dest.BACKUP not in dest.REPORTING

    conn, settings, policy, destination, _target = scene(tmp_path, dest.OFFLINE)
    library(conn, "Photos/a.jpg")
    run(conn, settings, policy, destination, [DestinationFile("Photos/gone.jpg", 5)])

    assert divergence.summary(conn, destination.id).count == 1


def test_a_plain_backup_writes_nothing_down(tmp_path: Path) -> None:
    """Backup keeps them quietly. That is the cell that differs."""
    conn, settings, policy, destination, _target = scene(tmp_path, dest.BACKUP)
    library(conn, "Photos/a.jpg")

    plan, _result = run(
        conn, settings, policy, destination, [DestinationFile("Photos/gone.jpg", 5)]
    )

    assert plan.destination_only == 1
    assert divergence.summary(conn, destination.id).count == 0


# --- a destination copy that differs -------------------------------------------------

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
