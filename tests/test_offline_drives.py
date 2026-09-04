"""A backup drive in a drawer: absent is normal, wrong is a refusal, stale stays.

Three sentences hold the whole feature, and each one is a way to get it wrong:

    disconnected            is a normal state, not a failure to report
    the wrong drive         is a refusal, not a disconnected drive
    only at the destination is never removed, and stays readable after the
                            drive goes back in the drawer

The last is the one that makes an Offline Backup worth having: the drive is
usually not here, so an answer that only exists while it is plugged in is an
answer nobody can use.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from librairy import destinations as dest
from librairy import divergence, offline_drives, transfer_paths, transfer_run, volumes
from librairy.config import Settings
from librairy.db import connect
from librairy.transfer_paths import MARKER, TransferRefused
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


def drive(tmp_path: Path, name: str = "wd") -> Path:
    path = tmp_path / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def scene(tmp_path: Path):  # noqa: ANN201
    settings = settings_for(tmp_path)
    conn = connect(settings)
    mount = drive(tmp_path)
    registered = offline_drives.register(conn, settings, name="WD-8TB", path=str(mount))
    return conn, settings, registered, mount


def library(conn, *paths: str, size: int = 100) -> None:  # noqa: ANN001
    for relpath in paths:
        conn.execute(
            "INSERT INTO items(root, relpath, size, mtime_ns, state, first_seen_at,"
            " last_seen_at) VALUES ('library', ?, ?, 0, 'committed', 'now', 'now')",
            (relpath, size),
        )


def no_volume_ids(monkeypatch) -> None:  # noqa: ANN001
    """A platform that cannot say what filesystem this is. Not a failure — it is
    where this started, and the marker carries the identity on its own."""
    monkeypatch.setattr(volumes, "identity_for", lambda _path: "")


def volume_id(monkeypatch, mapping: dict[Path, str]) -> None:  # noqa: ANN001
    """A platform that can. Keyed by path so two drives can be told apart."""
    monkeypatch.setattr(
        volumes, "identity_for", lambda path: mapping.get(Path(path), "")
    )


# --- registration --------------------------------------------------------------------


def test_registering_writes_both_halves_of_the_identity(
    tmp_path: Path, monkeypatch
) -> None:
    """A marker that says it was registered with us, and what the operating
    system calls this filesystem. Each covers the other's hole."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    mount = drive(tmp_path)
    volume_id(monkeypatch, {mount: "uuid:AAAA"})

    registered = offline_drives.register(conn, settings, name="WD-8TB", path=str(mount))

    assert registered.modes == (dest.OFFLINE,)
    assert registered.identity.startswith("librairy:")
    assert registered.volume == "uuid:AAAA"
    assert (mount / MARKER).read_text(encoding="utf-8").strip() == registered.identity


def test_a_drive_that_is_not_connected_cannot_be_registered(tmp_path: Path) -> None:
    """A marker cannot be written to a drawer, and pretending otherwise would
    make a destination that can never be recognised."""
    settings = settings_for(tmp_path)
    conn = connect(settings)

    try:
        offline_drives.register(
            conn, settings, name="WD-8TB", path=str(tmp_path / "never-plugged-in")
        )
    except TransferRefused as refusal:
        assert "not connected" in str(refusal)
    else:
        raise AssertionError("registered a drive that was not there")


def test_a_drive_inside_the_library_is_refused(tmp_path: Path) -> None:
    """Every path refusal applies before anything is written anywhere."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    inside = settings.library_dir / "Backup"
    inside.mkdir()

    try:
        offline_drives.register(conn, settings, name="Bad", path=str(inside))
    except TransferRefused as refusal:
        assert "inside your library" in str(refusal)
    else:
        raise AssertionError("registered a backup inside the library")
    assert not (inside / MARKER).exists(), "a marker was written before the refusal"


def test_registering_the_same_drive_twice_is_refused(tmp_path: Path) -> None:
    conn, settings, registered, mount = scene(tmp_path)

    try:
        offline_drives.register(conn, settings, name="Again", path=str(mount))
    except TransferRefused as refusal:
        assert "already registered" in str(refusal)
        assert registered.name in str(refusal)
    else:
        raise AssertionError("registered the same drive twice")


def test_forgetting_a_drive_touches_nothing_on_it(tmp_path: Path) -> None:
    """Forgetting a destination forgets what LibrAIry knows. A drive full of
    somebody's photographs is not what LibrAIry knows."""
    conn, settings, registered, mount = scene(tmp_path)
    (mount / "holiday.jpg").write_bytes(b"theirs")

    offline_drives.forget(conn, registered.id)

    assert dest.destination(conn, registered.id) is None
    assert (mount / "holiday.jpg").read_bytes() == b"theirs"
    assert (mount / MARKER).exists(), "the marker on their drive was removed"


# --- presence: the three states ------------------------------------------------------


def test_a_connected_registered_drive_is_present(tmp_path: Path, monkeypatch) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    mount = drive(tmp_path)
    volume_id(monkeypatch, {mount: "uuid:AAAA"})
    registered = offline_drives.register(conn, settings, name="WD-8TB", path=str(mount))

    found = offline_drives.look(conn, settings, registered)

    assert found.here and found.state == offline_drives.PRESENT
    assert found.verification == transfer_paths.FULLY_VERIFIED
    assert not found.refused


def test_an_unplugged_drive_is_absent_and_that_is_not_a_failure(
    tmp_path: Path,
) -> None:
    conn, settings, registered, mount = scene(tmp_path)
    for child in mount.iterdir():
        child.unlink()
    mount.rmdir()

    found = offline_drives.look(conn, settings, registered)

    assert found.state == offline_drives.ABSENT
    assert not found.refused, "a drawer is not a refusal"
    assert found.label == "Not connected"
    #  Nothing anywhere calls this a failure.
    for word in ("error", "failed", "overdue", "missing", "unavailable"):
        assert word not in found.sentence.lower()


def test_a_leftover_mount_point_is_absent_not_a_wrong_drive(tmp_path: Path) -> None:
    """The ordinary case: an unplugged USB disk leaves its folder behind, empty.

    Calling that a wrong drive would turn "your drive is not plugged in" into
    an alarm, every single time.
    """
    conn, settings, registered, mount = scene(tmp_path)
    (mount / MARKER).unlink()

    found = offline_drives.look(conn, settings, registered)

    assert found.state == offline_drives.ABSENT
    assert not found.refused


def test_a_different_drive_at_the_same_path_is_a_refusal_not_an_absence(
    tmp_path: Path,
) -> None:
    """Two states would collapse this into "not connected", which would be a
    lie told while a drive is plugged in."""
    conn, settings, registered, mount = scene(tmp_path)
    (mount / MARKER).write_text("librairy:somebody-elses-drive\n", encoding="utf-8")

    found = offline_drives.look(conn, settings, registered)

    assert found.state == offline_drives.WRONG_DRIVE
    assert found.refused and not found.here
    assert "different drive" in found.sentence


def test_a_cloned_drive_carries_the_marker_and_is_still_refused(
    tmp_path: Path, monkeypatch
) -> None:
    """The case the marker cannot catch, and the only reason the volume id
    exists: copy a backup drive and the copy claims to be the original."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    original = drive(tmp_path, "original")
    volume_id(monkeypatch, {original: "uuid:ORIGINAL"})
    registered = offline_drives.register(
        conn, settings, name="WD-8TB", path=str(original)
    )

    #  The clone: same path, same marker, different filesystem.
    volume_id(monkeypatch, {original: "uuid:CLONE"})
    found = offline_drives.look(conn, settings, registered)

    assert found.state == offline_drives.WRONG_DRIVE
    assert "copy" in found.detail


def test_a_platform_that_stops_answering_falls_back_to_the_marker(
    tmp_path: Path, monkeypatch
) -> None:
    """An absence is not a disagreement. Refusing every backup because
    `diskutil` changed would be worse than falling back to the marker — but the
    reduction has to be visible."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    mount = drive(tmp_path)
    volume_id(monkeypatch, {mount: "uuid:AAAA"})
    registered = offline_drives.register(conn, settings, name="WD-8TB", path=str(mount))

    no_volume_ids(monkeypatch)
    found = offline_drives.look(conn, settings, registered)

    assert found.here, "the drive stopped working because the platform did"
    assert found.verification == transfer_paths.MARKER_ONLY
    assert "marker file only" in found.sentence


def test_the_last_time_it_was_here_survives_it_going_away(tmp_path: Path) -> None:
    """The half of the answer that cannot be probed once the drive is gone, and
    usually the half worth showing."""
    conn, settings, registered, mount = scene(tmp_path)
    offline_drives.look(conn, settings, registered)
    seen = offline_drives.presence(conn, registered.id).present_at
    assert seen

    (mount / MARKER).unlink()
    mount.rmdir()
    found = offline_drives.look(conn, settings, registered)

    assert found.state == offline_drives.ABSENT
    assert found.present_at == seen, "a check that found nothing erased the date"
    assert "last seen" in found.sentence


# --- the cheap probe -----------------------------------------------------------------


def test_the_poll_never_asks_the_operating_system_anything(
    tmp_path: Path, monkeypatch
) -> None:
    """`probe` runs on a timer, and on macOS a volume id is a subprocess. A
    stat and a short read is what a drive in a drawer may cost."""
    conn, settings, registered, _mount = scene(tmp_path)
    del conn

    def explode(_path):  # noqa: ANN001, ANN202
        raise AssertionError("the cheap probe asked for a volume id")

    monkeypatch.setattr(volumes, "identity_for", explode)
    assert offline_drives.probe(settings, registered) == offline_drives.PRESENT


def test_a_drive_that_stays_connected_is_not_compared_every_cycle(
    tmp_path: Path,
) -> None:
    """`appeared` reports the transition, not the state. Otherwise a drive left
    plugged in would be re-compared for ever.

    Registering already looked — that is how the Browse action appears without
    waiting for a poll — so a drive is not "appearing" the moment after it was
    registered. The worker covers that separately: a policy with no runs yet is
    due, and due is the other trigger.
    """
    conn, settings, registered, _mount = scene(tmp_path)

    assert offline_drives.presence(conn, registered.id).here
    assert offline_drives.appeared(conn, settings, registered) is False
    assert offline_drives.appeared(conn, settings, registered) is False


def test_a_drive_reappearing_is_noticed_again(tmp_path: Path) -> None:
    conn, settings, registered, mount = scene(tmp_path)
    identity = (mount / MARKER).read_text(encoding="utf-8")

    (mount / MARKER).unlink()
    assert offline_drives.appeared(conn, settings, registered) is False
    assert not offline_drives.presence(conn, registered.id).here
    (mount / MARKER).write_text(identity, encoding="utf-8")

    assert offline_drives.appeared(conn, settings, registered) is True


def test_the_wrong_drive_appearing_does_not_count_as_the_drive_appearing(
    tmp_path: Path,
) -> None:
    conn, settings, registered, mount = scene(tmp_path)
    (mount / MARKER).write_text("librairy:another\n", encoding="utf-8")

    assert offline_drives.appeared(conn, settings, registered) is False
    assert offline_drives.presence(conn, registered.id).refused


def test_only_attached_drives_are_offered(tmp_path: Path) -> None:
    """What the Browse quick action is built on: an action for a drive in a
    drawer is not offered disabled, it is not rendered at all."""
    conn, settings, registered, mount = scene(tmp_path)
    offline_drives.look(conn, settings, registered)
    assert [found.id for found in offline_drives.attached(conn)] == [registered.id]

    (mount / MARKER).unlink()
    offline_drives.look(conn, settings, registered)

    assert offline_drives.attached(conn) == []
    assert [found.id for found in offline_drives.registered(conn)] == [registered.id]


def test_reading_presence_goes_nowhere_near_the_filesystem(
    tmp_path: Path, monkeypatch
) -> None:
    """A page render reads this. It must not wait on a stat of something that
    may be an unresponsive mount."""
    conn, settings, registered, _mount = scene(tmp_path)
    offline_drives.look(conn, settings, registered)

    def explode(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("rendering touched the drive")

    monkeypatch.setattr(transfer_paths, "identify", explode)
    monkeypatch.setattr(volumes, "identity_for", explode)

    assert offline_drives.presence(conn, registered.id).here


# --- the reconnect comparison --------------------------------------------------------


def test_a_reconnected_drive_reports_all_four_answers(tmp_path: Path) -> None:
    """What to add, what to update, what is already there, and what is only
    there. The same four a Mirror produces, from the same comparison."""
    conn, settings, registered, _mount = scene(tmp_path)
    dest.set_policy(
        conn, category="photos", destination_id=registered.id, mode=dest.OFFLINE
    )
    library(conn, "Photos/new.jpg", "Photos/same.jpg")
    conn.execute(
        "UPDATE items SET size=400 WHERE relpath='Photos/changed.jpg'"
    )
    library(conn, "Photos/changed.jpg", size=400)
    policy = dest.policies(conn)[0]

    plan, _result = transfer_run.run_policy(
        conn,
        settings,
        policy,
        registered,
        [
            DestinationFile("Photos/same.jpg", 100),
            DestinationFile("Photos/changed.jpg", 12),
            DestinationFile("Photos/from-2019.jpg", 7),
        ],
        runner=Stub(),
    )

    assert plan.to_copy == 1
    assert plan.to_update == 1
    assert plan.current == 1
    assert plan.destination_only == 1


def test_what_is_only_on_the_drive_is_still_readable_from_the_drawer(
    tmp_path: Path,
) -> None:
    """The whole point. An answer that only exists while the drive is plugged
    in is an answer nobody can use, because the drive is usually not plugged
    in."""
    conn, settings, registered, mount = scene(tmp_path)
    dest.set_policy(
        conn, category="photos", destination_id=registered.id, mode=dest.OFFLINE
    )
    library(conn, "Photos/a.jpg")
    policy = dest.policies(conn)[0]
    transfer_run.run_policy(
        conn,
        settings,
        policy,
        registered,
        [DestinationFile(f"Photos/from-2019-{index}.jpg", 7) for index in range(80)],
        runner=Stub(),
    )

    #  Back in the drawer.
    (mount / MARKER).unlink()
    mount.rmdir()
    offline_drives.look(conn, settings, registered)

    found = divergence.summary(conn, registered.id)
    assert found.count == 80  # noqa: PLR2004
    assert found.sentence == "80 only at the destination"
    page = divergence.page(conn, registered.id)
    assert len(page) == divergence.PAGE and page.more
    assert not offline_drives.presence(conn, registered.id).here


def test_a_drive_in_a_drawer_produces_no_run_and_no_failure(tmp_path: Path) -> None:
    """No retry storm, and nothing that reads as broken. A drawer is where a
    backup drive is supposed to be."""
    from librairy import backup_runs

    conn, settings, registered, mount = scene(tmp_path)
    dest.set_policy(
        conn, category="photos", destination_id=registered.id, mode=dest.OFFLINE
    )
    library(conn, "Photos/a.jpg")
    (mount / MARKER).unlink()
    mount.rmdir()
    policy = dest.policies(conn)[0]

    plan, result = transfer_run.run_policy(conn, settings, policy, registered, None)

    assert plan.unavailable
    assert result.outcome == transfer_run.UNAVAILABLE
    assert backup_runs.recent(conn, registered.id) == []
    assert backup_runs.last_run(conn, registered.id) is None


def test_nothing_only_on_the_drive_is_ever_removed(tmp_path: Path) -> None:
    """Reported for as long as it is there, and never acted on. Checked at both
    gates, because the argv is where a promise becomes a fact."""
    from librairy.tools import rclone

    conn, settings, registered, mount = scene(tmp_path)
    dest.set_policy(
        conn, category="photos", destination_id=registered.id, mode=dest.OFFLINE
    )
    library(conn, "Photos/a.jpg")
    (mount / "from-2019.jpg").write_bytes(b"theirs")
    stub = Stub()

    transfer_run.run_policy(
        conn,
        settings,
        dest.policies(conn)[0],
        registered,
        [DestinationFile("Photos/from-2019.jpg", 7)],
        runner=stub,
    )

    assert (mount / "from-2019.jpg").read_bytes() == b"theirs"
    for command in stub.commands:
        assert command[1] in rclone.ALLOWED_VERBS
        assert not rclone.DESTRUCTIVE_VERBS.intersection(command)
        for argument in command[2:]:
            assert not any(
                argument.startswith(flag) for flag in rclone.DESTRUCTIVE_FLAGS
            )


def test_this_module_moves_nothing_and_removes_nothing() -> None:
    """It answers "is the drive here" and "is it the right one". Everything
    else is the machinery that already exists.

    Read from the syntax rather than from the text, so that prose explaining
    why there is no subprocess here does not pass for one.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(offline_drives))
    imported = {
        name.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for name in node.names
    } | {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert not imported & {"subprocess", "shutil", "os", "tempfile"}, imported

    called = {
        node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    #  The marker at registration is written by `transfer_paths`, which owns
    #  every question about which paths may be touched. Nothing here opens a
    #  file of its own, and nothing here removes one at all.
    for verb in ("unlink", "rmtree", "remove", "rename", "replace", "write_text", "open"):
        assert verb not in called, verb


# --- what the worker actually does with it --------------------------------------------


def test_the_worker_compares_a_drive_the_moment_it_turns_up(tmp_path: Path) -> None:
    """Not on the hourly guard. A drive plugged in at five past ten would wait
    for eleven o'clock while it sat there connected, and by eleven it is
    usually back in the drawer."""
    from librairy import backup_runs
    from librairy.worker import Worker

    conn, settings, registered, mount = scene(tmp_path)
    dest.set_policy(
        conn, category="photos", destination_id=registered.id, mode=dest.OFFLINE
    )
    library(conn, "Photos/a.jpg")
    worker = Worker(conn, settings)

    #  Once, with the drive here: the policy has never run, so it is due.
    worker._offline_drives(settings)  # noqa: SLF001
    first = backup_runs.last_run(conn, registered.id)
    assert first is not None, "a connected drive with a due policy was not compared"

    #  Again, immediately. Nothing changed and the guard holds.
    worker._offline_checked = 0.0  # noqa: SLF001
    worker._offline_drives(settings)  # noqa: SLF001
    assert backup_runs.last_run(conn, registered.id).id == first.id

    #  Unplugged, then plugged back in. That is an appearance, and an
    #  appearance does not wait for the hour.
    identity = (mount / MARKER).read_text(encoding="utf-8")
    (mount / MARKER).unlink()
    worker._offline_checked = 0.0  # noqa: SLF001
    worker._offline_drives(settings)  # noqa: SLF001
    (mount / MARKER).write_text(identity, encoding="utf-8")
    worker._offline_checked = 0.0  # noqa: SLF001
    worker._offline_drives(settings)  # noqa: SLF001

    assert backup_runs.last_run(conn, registered.id).id != first.id


def test_the_worker_does_not_poll_a_drawer_every_cycle(tmp_path: Path) -> None:
    """Two stats every half minute, not two every five seconds. A drive in a
    drawer changes state a few times a year."""
    from librairy.worker import Worker

    conn, settings, registered, mount = scene(tmp_path)
    #  Already known to be away, so nothing here is a transition — this counts
    #  the steady state, which is the one that repeats for months.
    (mount / MARKER).unlink()
    mount.rmdir()
    offline_drives.look(conn, settings, registered)
    worker = Worker(conn, settings)
    looks = 0

    def counted(_settings, _destination):  # noqa: ANN001, ANN202
        nonlocal looks
        looks += 1
        return offline_drives.ABSENT

    import pytest

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(offline_drives, "probe", counted)
        for _ in range(20):
            worker._offline_drives(settings)  # noqa: SLF001

    assert looks == 1, f"a drawer was stat-ed {looks} times in twenty cycles"
    assert offline_drives.POLL_SECONDS >= 5  # noqa: PLR2004


def test_an_absent_drive_never_becomes_a_failed_run(tmp_path: Path) -> None:
    """No retry storm and nothing that reads as broken, cycle after cycle."""
    from librairy import backup_runs
    from librairy.worker import Worker

    conn, settings, registered, mount = scene(tmp_path)
    dest.set_policy(
        conn, category="photos", destination_id=registered.id, mode=dest.OFFLINE
    )
    library(conn, "Photos/a.jpg")
    (mount / MARKER).unlink()
    mount.rmdir()
    worker = Worker(conn, settings)

    for _ in range(10):
        worker._offline_checked = 0.0  # noqa: SLF001
        worker._offline_drives(settings)  # noqa: SLF001

    assert backup_runs.recent(conn, registered.id) == []
    assert not offline_drives.presence(conn, registered.id).here


def test_the_wrong_drive_plugged_in_transfers_nothing(tmp_path: Path) -> None:
    """Refusing is the feature working. Nothing is copied onto a stranger's
    disk, and no run is opened claiming otherwise."""
    from librairy import backup_runs
    from librairy.worker import Worker

    conn, settings, registered, mount = scene(tmp_path)
    dest.set_policy(
        conn, category="photos", destination_id=registered.id, mode=dest.OFFLINE
    )
    library(conn, "Photos/a.jpg")
    (mount / MARKER).write_text("librairy:not-yours\n", encoding="utf-8")
    worker = Worker(conn, settings)

    for _ in range(3):
        worker._offline_checked = 0.0  # noqa: SLF001
        worker._offline_drives(settings)  # noqa: SLF001

    assert backup_runs.recent(conn, registered.id) == []
    assert offline_drives.presence(conn, registered.id).refused
    assert not list(mount.glob("*.jpg")), "something was written to the wrong drive"
