"""The M3-03 gate: every way this feature could destroy something, asked once.

The rest of the suite tests each piece where it lives. This asks the questions
that only make sense about the whole thing, and it is deliberately paranoid,
because an innocent `rclone sync` violates the product in one command.

Four questions:

    can any path in the program produce a destructive command
    can any path write outside where it is allowed to write
    can a transfer change the Library
    does an interrupted transfer converge, and say so honestly

**With real rclone.** The two drills at the bottom run the actual binary
against real directories, because a command that is correct in a stub and
wrong in the world is exactly what a stub cannot catch — and one already was:
the stubs happily recorded a copy that would have flattened every category into
the root of the drive.
"""

from __future__ import annotations

import ast
import inspect
import shutil
import subprocess
from pathlib import Path

import pytest

from librairy import (
    backup_runs,
    divergence,
    offline_drives,
    transfer_listing,
    transfer_paths,
    transfer_plan,
    transfer_requests,
    transfer_run,
    transfer_status,
)
from librairy import (
    destinations as dest,
)
from librairy.config import Settings
from librairy.db import connect
from librairy.tools import rclone
from librairy.transfer_paths import MARKER, TransferRefused

rclone_installed = pytest.mark.skipif(
    shutil.which("rclone") is None, reason="rclone is not installed"
)

#  Every module that can put bytes on a destination or decide that something
#  should be. If a fifth one appears it belongs in this list.
TRANSFER_STACK = (
    destinations := dest,
    transfer_paths,
    transfer_plan,
    transfer_listing,
    transfer_run,
    transfer_requests,
    offline_drives,
    backup_runs,
    divergence,
    transfer_status,
)

#  What rclone calls the things that remove files. None of these may appear as
#  a verb anywhere in the stack, in an allowlist, or in an argv.
DESTRUCTIVE = ("sync", "delete", "deletefile", "purge", "rmdir", "rmdirs", "move", "moveto")


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


class Recorder:
    """Stands in for rclone and keeps every argv it was ever handed."""

    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
        del timeout
        self.commands.append(list(command))
        return subprocess.CompletedProcess(command, 0, "", "")


def library(conn, *paths: str, size: int = 100) -> None:  # noqa: ANN001
    for relpath in paths:
        conn.execute(
            "INSERT INTO items(root, relpath, size, mtime_ns, state, first_seen_at,"
            " last_seen_at) VALUES ('library', ?, ?, 0, 'committed', 'now', 'now')",
            (relpath, size),
        )


# --- 1. deletion is unrepresentable ---------------------------------------------------


def test_the_policy_vocabulary_has_no_verb_that_removes_anything() -> None:
    """The root of the whole design. There is no constant to put in a cell, so
    adding one means inventing the concept in a module whose docstring is about
    why it does not exist."""
    answers = {
        dest.ACTIONS[(mode, difference)]
        for mode in dest.MODES
        for difference in dest.DIFFERENCES
    }

    assert answers <= {dest.COPY, dest.UPDATE, dest.KEEP, dest.REPORT}
    assert len(dest.ACTIONS) == len(dest.MODES) * len(dest.DIFFERENCES), "a cell is missing"
    for name, value in vars(dest).items():
        if name.isupper() and isinstance(value, str):
            assert value not in DESTRUCTIVE, f"{name} = {value!r}"


def test_no_module_in_the_stack_names_a_destructive_rclone_verb() -> None:
    """Read from the syntax, so prose explaining why there is no `sync` here
    does not count as one."""
    for module in TRANSFER_STACK:
        tree = ast.parse(inspect.getsource(module))
        strings = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        ]
        for text in strings:
            words = set(text.replace("-", " ").replace("/", " ").split())
            leaked = words & {"sync", "purge", "rmdirs", "moveto", "deletefile"}
            assert not leaked, f"{module.__name__}: {leaked} in {text[:60]!r}"


def test_the_adapter_allows_no_destructive_verb_or_option() -> None:
    assert not set(rclone.ALLOWED_VERBS) & set(DESTRUCTIVE)
    for flag in rclone.ALLOWED_FLAGS:
        assert not any(flag.startswith(bad) for bad in rclone.DESTRUCTIVE_FLAGS), flag


@pytest.mark.parametrize(
    "command",
    [
        ["rclone", "sync", "/a", "b:/c"],
        ["rclone", "purge", "b:/c"],
        ["rclone", "delete", "b:/c"],
        ["rclone", "move", "/a", "b:/c"],
        ["rclone", "copy", "/a", "b:/c", "--delete-excluded"],
        ["rclone", "copy", "/a", "b:/c", "--max-delete=0"],
        ["rclone", "copy", "/a", "b:/c", "--backup-dir", "/tmp"],
        ["rclone", "copy", "/a", "b:/c", "--rmdirs"],
        #  Not destructive, just unknown. The allowlist is the boundary, so a
        #  harmless-looking option nobody vetted is refused too.
        ["rclone", "copy", "/a", "b:/c", "--drive-trashed-only"],
    ],
)
def test_the_adapter_refuses_it(command: list[str]) -> None:
    with pytest.raises(rclone.RcloneError):
        rclone.run(command)


def test_every_argv_the_whole_system_can_produce_is_a_copy(tmp_path: Path) -> None:
    """Sweep every entry point — a scheduled policy in each mode, an appearing
    offline drive, and an explicit send — and check each argv against both
    gates. One command shape, and no fifth thing that can emit one."""
    recorder = Recorder()
    settings = settings_for(tmp_path)
    conn = connect(settings)
    photos = settings.library_dir / "Photos"
    (photos / "a.jpg").write_bytes(b"a" * 100)
    library(conn, "Photos/a.jpg")

    for index, mode in enumerate(dest.MODES):
        target = tmp_path / f"place-{index}"
        target.mkdir()
        if mode == dest.OFFLINE:
            drive = offline_drives.register(
                conn, settings, name=f"Drive {index}", path=str(target)
            )
            destination_id = drive.id
        else:
            destination_id = dest.add_destination(
                conn,
                name=f"Place {index}",
                kind=dest.LOCAL,
                target=str(target),
                modes=[mode],
            )
        dest.set_policy(
            conn, category="photos", destination_id=destination_id, mode=mode
        )
        found = dest.destination(conn, destination_id)
        transfer_run.run_policy(
            conn, settings, dest.policies(conn)[-1], found, [], runner=recorder
        )
        #  And the same destination reached the other way, by somebody pressing
        #  a button in Browse.
        if mode == dest.OFFLINE:
            asked = transfer_requests.ask(
                conn, destination_id=destination_id, relpath="Photos", exact=False
            )
            transfer_requests.send(conn, settings, asked, found, runner=recorder)

    assert recorder.commands, "the sweep exercised nothing"
    for command in recorder.commands:
        assert command[0] == "rclone"
        assert command[1] == "copy", command
        assert not set(command) & set(DESTRUCTIVE)
        for argument in command[2:]:
            if argument.startswith("-"):
                assert argument.split("=", 1)[0] in rclone.ALLOWED_FLAGS, argument
        #  And it survives the adapter's own gate, which is the one that runs.
        rclone._assert_safe(command)  # noqa: SLF001


# --- 2. where it may write ------------------------------------------------------------


def test_a_destination_may_not_be_the_library_or_anywhere_managed(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    for bad in (
        settings.library_dir,
        settings.library_dir / "Photos",
        settings.library_dir.parent,
        settings.inbox_dir,
        settings.quarantine_dir,
        settings.appdata_dir,
        settings.appdata_dir / "rclone",
    ):
        with pytest.raises(TransferRefused):
            transfer_paths.local_destination(settings, str(bad))


def test_a_source_that_resolves_out_of_the_library_is_refused(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (settings.library_dir / "escape").symlink_to(outside)

    for bad in ("../elsewhere", "escape", "/etc", "Photos/../../elsewhere"):
        with pytest.raises(TransferRefused):
            transfer_paths.library_source(settings, bad)


def test_a_remote_that_is_not_a_remote_is_refused() -> None:
    for bad in ("/tmp/backup", "backup", "-flag:path", "/a/b:c"):
        with pytest.raises(TransferRefused):
            transfer_paths.remote_destination(bad)


def test_an_offline_drive_is_refused_unless_it_is_the_one_registered(
    tmp_path: Path,
) -> None:
    """Four cases, and the third is the one only a volume id can catch."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    mount = tmp_path / "wd"
    mount.mkdir()
    drive = offline_drives.register(conn, settings, name="WD", path=str(mount))

    #  The registered drive, present.
    assert transfer_paths.checked_offline(
        settings, drive.target, drive.identity, drive.volume
    ).path == mount

    #  Somebody else's marker at the same mount point.
    (mount / MARKER).write_text("librairy:not-ours\n", encoding="utf-8")
    with pytest.raises(TransferRefused):
        transfer_paths.checked_offline(settings, drive.target, drive.identity, drive.volume)

    #  Our marker, a different filesystem: a clone.
    (mount / MARKER).write_text(f"{drive.identity}\n", encoding="utf-8")
    with pytest.raises(TransferRefused):
        transfer_paths.checked_offline(
            settings, drive.target, drive.identity, "uuid:SOMETHING-ELSE"
        )

    #  A leftover mount point with nothing in it.
    (mount / MARKER).unlink()
    with pytest.raises(TransferRefused):
        transfer_paths.checked_offline(settings, drive.target, drive.identity, drive.volume)


# --- 3. nothing reaches the Library ---------------------------------------------------


def test_no_transfer_path_writes_to_the_library(tmp_path: Path) -> None:
    """Every entry point, run against a real filesystem, with the Library
    photographed before and after."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    photos = settings.library_dir / "Photos"
    (photos / "a.jpg").write_bytes(b"a" * 100)
    library(conn, "Photos/a.jpg")
    target = tmp_path / "wd"
    target.mkdir()
    drive = offline_drives.register(conn, settings, name="WD", path=str(target))
    dest.set_policy(conn, category="photos", destination_id=drive.id, mode=dest.OFFLINE)

    def snapshot() -> tuple:
        files = {
            path.relative_to(settings.library_dir).as_posix(): path.read_bytes()
            for path in settings.library_dir.rglob("*")
            if path.is_file()
        }
        rows = [tuple(row) for row in conn.execute("SELECT * FROM items ORDER BY id")]
        history = conn.execute("SELECT COUNT(*) FROM history").fetchone()[0]
        proposals = conn.execute("SELECT COUNT(*) FROM proposals").fetchone()[0]
        return files, rows, history, proposals

    before = snapshot()
    recorder = Recorder()
    transfer_run.run_policy(
        conn, settings, dest.policies(conn)[0], drive, [], runner=recorder
    )
    asked = transfer_requests.ask(
        conn, destination_id=drive.id, relpath="Photos", exact=False
    )
    transfer_requests.send(conn, settings, asked, drive, runner=recorder)
    offline_drives.look(conn, settings, drive)
    divergence.record(
        conn,
        destination_id=drive.id,
        category="photos",
        entries=[],
        complete=True,
    )

    assert snapshot() == before


# --- 4. interruption, and saying so honestly ------------------------------------------


def test_a_failed_run_is_never_recorded_as_current(tmp_path: Path) -> None:
    """The one bookkeeping rule, and there is nothing that could store it."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(backup_runs)")
    }

    for absent in ("current", "is_current", "synced", "up_to_date", "in_sync"):
        assert absent not in columns, absent


@rclone_installed
def test_a_real_transfer_converges_after_an_interruption(tmp_path: Path) -> None:
    """The drill. Real rclone, real files, a run cut short, and a rerun.

    Convergence comes from the comparison being cheap and repeatable, not from
    a resume protocol — so the way to prove it is to break the destination and
    ask again.
    """
    settings = settings_for(tmp_path)
    conn = connect(settings)
    photos = settings.library_dir / "Photos" / "2024"
    photos.mkdir(parents=True)
    for index in range(6):
        (photos / f"IMG_{index}.jpg").write_bytes(f"photo {index}".encode() * 20)
    library(conn, *[f"Photos/2024/IMG_{index}.jpg" for index in range(6)], size=140)
    target = tmp_path / "wd"
    target.mkdir()
    drive = offline_drives.register(conn, settings, name="WD", path=str(target))
    dest.set_policy(conn, category="photos", destination_id=drive.id, mode=dest.OFFLINE)
    policy = dest.policies(conn)[0]

    def listing() -> list:
        return transfer_listing.listing(
            conn, settings, drive, transfer_plan.Scope.of(policy)
        )

    first = transfer_run.run_policy(conn, settings, policy, drive, listing())
    assert first[1].ok, first[1].detail
    there = target / "Photos" / "2024"
    assert len(list(there.iterdir())) == 6  # noqa: PLR2004

    #  What an interruption leaves behind: some of it, and a file the library
    #  no longer has, sitting beside it.
    for index in (1, 3, 5):
        (there / f"IMG_{index}.jpg").unlink()
    (there / "from-2019.jpg").write_bytes(b"theirs")

    plan, result = transfer_run.run_policy(conn, settings, policy, drive, listing())

    assert plan.to_copy == 3, "the rerun did not notice what was missing"  # noqa: PLR2004
    assert result.ok, result.detail
    assert {path.name for path in there.iterdir()} == {
        *(f"IMG_{index}.jpg" for index in range(6)),
        "from-2019.jpg",
    }
    #  Reported, and still there. This is the whole product in one assertion.
    assert (there / "from-2019.jpg").read_bytes() == b"theirs"
    assert divergence.summary(conn, drive.id).count == 1


@rclone_installed
def test_a_real_send_from_browse_lands_at_the_library_path(tmp_path: Path) -> None:
    """The one-off, end to end, with the binary that actually moves the bytes."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    rust = settings.library_dir / "Books" / "Programming" / "Rust"
    rust.mkdir(parents=True)
    (rust / "book.pdf").write_bytes(b"%PDF-1.7" + b"x" * 200)
    library(conn, "Books/Programming/Rust/book.pdf", size=208)
    target = tmp_path / "wd"
    target.mkdir()
    drive = offline_drives.register(conn, settings, name="WD", path=str(target))

    asked = transfer_requests.ask(
        conn,
        destination_id=drive.id,
        relpath="Books/Programming/Rust/book.pdf",
        exact=True,
    )
    done = transfer_requests.send(conn, settings, asked, drive)

    assert done.state == transfer_requests.DONE, done.detail
    landed = target / "Books" / "Programming" / "Rust" / "book.pdf"
    assert landed.read_bytes() == (rust / "book.pdf").read_bytes()
    #  And no policy was created by any of it.
    assert dest.policies(conn) == []


@rclone_installed
def test_a_real_mirror_never_removes_what_is_only_at_the_destination(
    tmp_path: Path,
) -> None:
    """Ten runs, real rclone, and the file the library lost is still there."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    photos = settings.library_dir / "Photos"
    (photos / "a.jpg").write_bytes(b"a" * 100)
    library(conn, "Photos/a.jpg")
    target = tmp_path / "mirror"
    target.mkdir()
    (target / "Photos").mkdir()
    (target / "Photos" / "gone.jpg").write_bytes(b"kept")
    destination_id = dest.add_destination(
        conn, name="Mirror", kind=dest.LOCAL, target=str(target), modes=[dest.MIRROR]
    )
    dest.set_policy(
        conn, category="photos", destination_id=destination_id, mode=dest.MIRROR
    )
    policy = dest.policies(conn)[0]
    found = dest.destination(conn, destination_id)

    for _ in range(10):
        listing = transfer_listing.listing(
            conn, settings, found, transfer_plan.Scope.of(policy)
        )
        _plan, result = transfer_run.run_policy(conn, settings, policy, found, listing)
        assert result.ok, result.detail

    assert (target / "Photos" / "gone.jpg").read_bytes() == b"kept"
    #  One fact about today, not ten findings.
    assert divergence.summary(conn, destination_id).count == 1
    assert len(backup_runs.recent(conn, destination_id, limit=20)) == 10  # noqa: PLR2004


# --- what the gate found --------------------------------------------------------------


def test_a_volume_id_is_read_for_a_folder_on_a_drive_not_only_its_root() -> None:
    """`diskutil info` exits non-zero for an ordinary directory.

    So a destination at `/Volumes/WD-8TB/librairy` — the most ordinary way
    there is to set one up — read no volume id at all and fell back to the
    marker alone, silently. Asking about the mount point fixes it, and this is
    the assertion that says the answer is about a *filesystem* rather than
    about whether the path happened to be a volume root.
    """
    from librairy import volumes

    root = volumes.identity_for(Path("/"))
    if not root:
        pytest.skip("this platform does not name filesystems")
    nested = Path("/") / "tmp"
    assert volumes.identity_for(nested) == root


def test_a_drive_with_no_recorded_volume_is_never_called_fully_verified() -> None:
    """Nothing was recorded to compare a filesystem against, so nothing ever
    will be. Saying "identity confirmed" would claim a check that cannot run."""
    assert transfer_paths.verification("librairy:x", "", "uuid:A") == transfer_paths.MARKER_ONLY
    assert transfer_paths.verification("librairy:x", "uuid:A", "") == transfer_paths.MARKER_ONLY
    assert (
        transfer_paths.verification("librairy:x", "uuid:A", "uuid:A")
        == transfer_paths.FULLY_VERIFIED
    )
    assert transfer_paths.verification("", "", "") == transfer_paths.UNVERIFIED
