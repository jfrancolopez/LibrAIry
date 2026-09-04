"""What Backup, Mirror and Offline Backup mean — asserted, not documented.

This is the first area since Commit where a single innocent argument violates
the whole product. `rclone sync` is one word longer than `rclone copy` and it
removes files. So the semantics are pinned before there is anything to run
them, and most of this file is about what cannot happen:

    no mode deletes anything, anywhere, for any reason
    no destination state reaches back into the Library
    no destructive command can be constructed, let alone run
    no path from a form is trusted as authority

The distinction worth stating out loud, because it is the one a "perfect sync"
instinct erodes: **Mirror means LibrAIry knows the destination differs from the
Library. It does not mean LibrAIry may erase the difference.**
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from librairy import destinations as dest
from librairy import transfer_paths
from librairy.config import Settings
from librairy.db import connect
from librairy.tools import rclone
from librairy.transfer_paths import TransferRefused


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
    return settings


# --- nothing here can delete --------------------------------------------------------


def test_no_mode_ever_removes_anything() -> None:
    """The whole decision table, and the fact that it has four answers.

    Every combination of a mode and a way the destination can differ, and not
    one of them says remove. A backup exists precisely to still hold the file
    you no longer have.
    """
    assert set(dest.ACTIONS.values()) == {dest.COPY, dest.UPDATE, dest.KEEP, dest.REPORT}
    for mode in dest.MODES:
        for difference in dest.DIFFERENCES:
            assert (mode, difference) in dest.ACTIONS, (mode, difference)


def test_the_modes_module_cannot_reach_a_filesystem_at_all() -> None:
    """Structural rather than conventional.

    A future edit that wanted a destination file gone would have to reach a
    filesystem from the module that defines what the modes mean — and it has no
    way to. `remove_destination` deletes a *row*, which is why the SQL word is
    allowed here and every way of removing a *file* is not.
    """
    source = inspect.getsource(dest)
    body = source.split('"""', 2)[2]
    for call in ("unlink(", "rmtree", "os.remove", "shutil", "subprocess", "Path("):
        assert call not in body, f"{call} appeared in the module that defines the modes"


def test_no_action_a_mode_can_take_removes_anything() -> None:
    """The other half, on the values rather than on the file.

    Four verbs, and the vocabulary itself has no fifth: there is no constant to
    put in a cell of the table, so adding one is inventing a concept rather
    than editing a dict.
    """
    vocabulary = {
        name
        for name, value in vars(dest).items()
        if name.isupper() and isinstance(value, str)
    }
    for word in ("DELETE", "REMOVE", "PURGE", "PRUNE", "ERASE"):
        assert word not in vocabulary
    assert set(dest.ACTIONS.values()) <= {dest.COPY, dest.UPDATE, dest.KEEP, dest.REPORT}


def test_a_file_that_left_the_library_stays_in_a_backup() -> None:
    """Safety beats equivalence, said as an assertion.

    This is the difference between a backup and a copy, and it is the one
    somebody reaches for a "sync" button to break.
    """
    assert dest.action_for(dest.BACKUP, dest.EXTRA) == dest.KEEP


def test_mirror_reports_the_difference_and_does_not_close_it() -> None:
    """Mirror knows the destination differs. It may not erase the difference.

    Some "perfect sync" purity is given up here on purpose. It fits a program
    whose entire premise is that it does not delete your files.
    """
    assert dest.action_for(dest.MIRROR, dest.EXTRA) == dest.REPORT
    assert dest.action_for(dest.MIRROR, dest.MISSING) == dest.COPY
    assert dest.action_for(dest.MIRROR, dest.CHANGED) == dest.UPDATE


def test_a_stale_file_on_an_offline_drive_is_shown_and_kept() -> None:
    """A drive out of a drawer after three months is exactly where somebody
    wants to be told what has drifted — and exactly where deleting on their
    behalf would be worst."""
    assert dest.action_for(dest.OFFLINE, dest.EXTRA) == dest.REPORT


def test_forgetting_a_destination_touches_nothing_on_it(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "already-there.jpg").write_bytes(b"kept")
    made = dest.add_destination(
        conn, name="Spare", kind="local", target=str(elsewhere), modes=[dest.BACKUP]
    )
    dest.set_policy(conn, category="photos", destination_id=made, mode=dest.BACKUP)

    dest.remove_destination(conn, made)

    assert dest.destinations(conn) == []
    assert dest.policies(conn) == []
    assert (elsewhere / "already-there.jpg").read_bytes() == b"kept"


# --- no destructive command can be built --------------------------------------------


@pytest.mark.parametrize("verb", sorted(rclone.DESTRUCTIVE_VERBS))
def test_a_destructive_verb_cannot_be_run(verb: str) -> None:
    with pytest.raises(rclone.RcloneError):
        rclone.run(["rclone", verb, "/tmp/a", "remote:b"])


@pytest.mark.parametrize(
    "flag",
    [
        "--delete",
        "--delete-before",
        "--delete-during",
        "--delete-after",
        "--delete-excluded",
        "--rmdirs",
        "--purge",
        "--backup-dir",
    ],
)
def test_a_destructive_option_on_a_safe_verb_is_refused(flag: str, tmp_path: Path) -> None:
    """The hole the verb allowlist could not see.

    `rclone copy --delete-excluded` removes files at the destination, and every
    check was on the verb — so a destructive option travelling as an argument
    went straight through.
    """
    with pytest.raises(rclone.RcloneError):
        rclone.run(["rclone", "copy", str(tmp_path), "remote:b", flag])


def test_the_commands_this_program_builds_are_reads_and_copies(tmp_path: Path) -> None:
    config = tmp_path / "rclone.conf"
    built = [
        rclone.version_command(),
        rclone.listremotes_command(config),
        rclone.copy_command(config, tmp_path, "remote:b"),
        rclone.check_command(config, tmp_path, "remote:b"),
        rclone.lsjson_command(config, "remote:b"),
    ]

    for command in built:
        assert command[1] in rclone.ALLOWED_VERBS
        assert not rclone.DESTRUCTIVE_VERBS.intersection(command)
        for argument in command[2:]:
            assert not any(
                argument.startswith(flag) for flag in rclone.DESTRUCTIVE_FLAGS
            )


def test_nothing_in_the_program_builds_an_rclone_command_by_hand() -> None:
    """One gate, and everything goes through it.

    A command assembled somewhere else and handed to `subprocess` would bypass
    every check above, so the check is that nobody does — the transfer modules
    call `tools/rclone.py` and it refuses on both construction and execution.
    """
    from librairy import backup

    for module in (backup, dest, transfer_paths):
        source = inspect.getsource(module)
        assert "subprocess" not in source, f"{module.__name__} runs commands itself"
        #  A list literal beginning with the binary is what building a command
        #  by hand looks like. `appdata_dir / "rclone"` is a config path and is
        #  not that, which is why this looks for the shape rather than the word.
        assert '["rclone"' not in source, f"{module.__name__} builds a command by hand"


# --- paths -------------------------------------------------------------------------


def test_a_source_must_resolve_inside_the_library(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)

    assert transfer_paths.library_source(settings, "Photos") == (
        settings.library_dir.resolve() / "Photos"
    )
    for escape in ("../outside", "Photos/../../etc", "/etc", "Photos/../.."):
        with pytest.raises(TransferRefused):
            transfer_paths.library_source(settings, escape)


def test_a_symlink_out_of_the_library_is_followed_and_then_refused(
    tmp_path: Path,
) -> None:
    """Resolution is what turns a symlink from a hole into a failed check.

    A link inside the Library pointing outside it is the ordinary way a source
    check gets bypassed, and comparing strings would not notice.
    """
    settings = settings_for(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (settings.library_dir / "escape").symlink_to(outside)

    with pytest.raises(TransferRefused):
        transfer_paths.library_source(settings, "escape")


def test_a_destination_may_not_be_the_library_or_touch_it(tmp_path: Path) -> None:
    """All three directions, because all three copy a library onto itself."""
    settings = settings_for(tmp_path)

    for target in (
        settings.library_dir,  # the same tree
        settings.library_dir / "Backups",  # inside it
        settings.library_dir.parent,  # containing it
        settings.inbox_dir / "backup",  # another managed root
        settings.quarantine_dir,
        settings.appdata_dir,
    ):
        with pytest.raises(TransferRefused):
            transfer_paths.local_destination(settings, str(target))


def test_a_neighbouring_folder_with_a_similar_name_is_fine(tmp_path: Path) -> None:
    """`/data/library-backup` starts with `/data/library` and is not inside it.

    The bug every hand-written containment check has had at least once, which
    is why this one compares resolved paths rather than strings.
    """
    settings = settings_for(tmp_path)
    beside = tmp_path / f"{settings.library_dir.name}-backup"

    found = transfer_paths.local_destination(settings, str(beside))

    assert found.path == beside.resolve()


def test_a_remote_that_is_not_a_remote_is_refused() -> None:
    """A string with no colon is a *local* path as far as rclone is concerned,
    so accepting one would turn a typo into a directory beside the working
    directory that quietly looks like a backup."""
    assert transfer_paths.remote_destination("nas:library") == "nas:library"
    for target in ("", "   ", "/mnt/nas", "library", "-nas:x", "na/s:x"):
        with pytest.raises(TransferRefused):
            transfer_paths.remote_destination(target)


# --- an offline drive is identified before it is written to -------------------------


def test_a_drive_is_recognised_by_what_it_says_it_is(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    drive = tmp_path / "Volumes" / "WD"
    drive.mkdir(parents=True)
    transfer_paths.register(drive, "wd-8tb-2026")

    assert transfer_paths.identify(drive) == "wd-8tb-2026"
    assert transfer_paths.attached(drive, "wd-8tb-2026")
    found = transfer_paths.checked_offline(settings, str(drive), "wd-8tb-2026")
    assert found.path == drive.resolve()


def test_a_different_drive_at_the_same_mount_point_is_refused(tmp_path: Path) -> None:
    """`/Volumes/Backup` is whatever was plugged in most recently.

    A stale mount point pointing at another disk is the ordinary case, not an
    exotic one, and writing a backup onto the wrong drive is how somebody ends
    up with two half-backups and no whole one.
    """
    settings = settings_for(tmp_path)
    drive = tmp_path / "Volumes" / "WD"
    drive.mkdir(parents=True)
    transfer_paths.register(drive, "some-other-disk")

    assert not transfer_paths.attached(drive, "wd-8tb-2026")
    with pytest.raises(TransferRefused):
        transfer_paths.checked_offline(settings, str(drive), "wd-8tb-2026")


def test_an_empty_folder_left_behind_by_an_unplugged_drive_is_not_the_drive(
    tmp_path: Path,
) -> None:
    """An unplugged USB disk often leaves its mount folder behind, empty — and
    a backup written into that folder goes onto the system disk and looks like
    it worked."""
    settings = settings_for(tmp_path)
    ghost = tmp_path / "Volumes" / "WD"
    ghost.mkdir(parents=True)

    assert not transfer_paths.attached(ghost, "wd-8tb-2026")
    with pytest.raises(TransferRefused):
        transfer_paths.checked_offline(settings, str(ghost), "wd-8tb-2026")


def test_a_drive_that_is_not_connected_at_all_is_refused(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)

    with pytest.raises(TransferRefused):
        transfer_paths.checked_offline(settings, str(tmp_path / "nothing"), "wd-8tb")


def test_a_cloned_marker_on_a_different_filesystem_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hole the marker alone cannot cover.

    Copy a backup drive and the copy carries the same marker, so it claims to
    be the original. The volume id is what tells them apart, and it is why the
    identity is two facts rather than one.
    """
    from librairy import volumes

    settings = settings_for(tmp_path)
    drive = tmp_path / "Volumes" / "WD"
    drive.mkdir(parents=True)
    transfer_paths.register(drive, "wd-8tb-2026")
    monkeypatch.setattr(volumes, "identity_for", lambda _path: "uuid:THE-CLONE")

    assert not transfer_paths.attached(drive, "wd-8tb-2026", "uuid:THE-ORIGINAL")
    with pytest.raises(TransferRefused, match="filesystem"):
        transfer_paths.checked_offline(
            settings, str(drive), "wd-8tb-2026", "uuid:THE-ORIGINAL"
        )


def test_the_registered_drive_is_accepted_on_both_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from librairy import volumes

    settings = settings_for(tmp_path)
    drive = tmp_path / "Volumes" / "WD"
    drive.mkdir(parents=True)
    transfer_paths.register(drive, "wd-8tb-2026")
    monkeypatch.setattr(volumes, "identity_for", lambda _path: "uuid:THE-ORIGINAL")

    assert transfer_paths.attached(drive, "wd-8tb-2026", "uuid:THE-ORIGINAL")
    found = transfer_paths.checked_offline(
        settings, str(drive), "wd-8tb-2026", "uuid:THE-ORIGINAL"
    )
    assert found.volume == "uuid:THE-ORIGINAL"


def test_a_drive_that_moved_to_another_mount_point_is_still_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hole the *volume id* covers and the marker cannot: the operating
    system mounted it somewhere else this time, and it is the same disk."""
    from librairy import volumes

    settings = settings_for(tmp_path)
    moved = tmp_path / "Volumes" / "WD 1"
    moved.mkdir(parents=True)
    transfer_paths.register(moved, "wd-8tb-2026")
    monkeypatch.setattr(volumes, "identity_for", lambda _path: "uuid:THE-ORIGINAL")

    found = transfer_paths.checked_offline(
        settings, str(moved), "wd-8tb-2026", "uuid:THE-ORIGINAL"
    )

    assert found.path == moved.resolve()


def test_a_platform_that_cannot_say_falls_back_to_the_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An absence is not a disagreement.

    Refusing every backup because `diskutil` changed, or because the filesystem
    exposes no id, would make a drive stop working on the day somebody upgraded
    their operating system. Only an actual mismatch refuses.
    """
    from librairy import volumes

    settings = settings_for(tmp_path)
    drive = tmp_path / "Volumes" / "WD"
    drive.mkdir(parents=True)
    transfer_paths.register(drive, "wd-8tb-2026")
    monkeypatch.setattr(volumes, "identity_for", lambda _path: "")

    assert volumes.matches("uuid:RECORDED", "")
    assert volumes.matches("", "uuid:FOUND")
    assert not volumes.matches("uuid:RECORDED", "uuid:OTHER")
    assert transfer_paths.attached(drive, "wd-8tb-2026", "uuid:RECORDED")
    assert transfer_paths.checked_offline(
        settings, str(drive), "wd-8tb-2026", "uuid:RECORDED"
    )


def test_volume_identity_never_raises_into_a_caller(tmp_path: Path) -> None:
    """It runs immediately before deciding whether to copy somebody's photos.

    Every way of failing is the same failure — the platform did not answer —
    and the answer to that is the marker on its own, not an exception.
    """
    from librairy import volumes

    assert volumes.identity_for(tmp_path / "nothing-here") in ("", volumes.identity_for(tmp_path))
    assert isinstance(volumes.identity_for(tmp_path), str)


def test_a_destination_remembers_both_halves_of_the_identity(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path))

    made = dest.add_destination(
        conn,
        name="WD 8TB",
        kind=dest.LOCAL,
        target="/Volumes/WD",
        modes=[dest.OFFLINE],
        identity="wd-8tb-2026",
        volume="uuid:THE-ORIGINAL",
    )

    found = dest.destination(conn, made)
    assert found is not None
    assert found.identity == "wd-8tb-2026"
    assert found.volume == "uuid:THE-ORIGINAL"


# --- the policy model ---------------------------------------------------------------


def test_a_category_and_a_destination_have_exactly_one_mode(tmp_path: Path) -> None:
    """Overlap is deterministic by construction rather than by precedence.

    Two rows disagreeing about what a destination is *for* is the one ambiguity
    nobody could resolve by reading the screen, so setting it again replaces it.
    """
    conn = connect(settings_for(tmp_path))
    nas = dest.add_destination(
        conn, name="NAS", kind="remote", target="nas:lib", modes=[dest.BACKUP, dest.MIRROR]
    )

    dest.set_policy(conn, category="photos", destination_id=nas, mode=dest.BACKUP)
    dest.set_policy(conn, category="photos", destination_id=nas, mode=dest.MIRROR)

    found = dest.policies(conn)
    assert len(found) == 1
    assert found[0].mode == dest.MIRROR


def test_one_category_may_go_to_several_destinations(tmp_path: Path) -> None:
    """Fan-out is the point, and it is not overlap: a photo can be backed up to
    a NAS and mirrored to a second machine without anything being ambiguous."""
    conn = connect(settings_for(tmp_path))
    nas = dest.add_destination(
        conn, name="NAS", kind="remote", target="nas:lib", modes=[dest.BACKUP]
    )
    other = dest.add_destination(
        conn, name="Studio", kind="remote", target="studio:lib", modes=[dest.MIRROR]
    )

    dest.set_policy(conn, category="photos", destination_id=nas, mode=dest.BACKUP)
    dest.set_policy(conn, category="photos", destination_id=other, mode=dest.MIRROR)

    assert sorted(policy.mode for policy, _ in dest.active(conn)) == [
        dest.BACKUP,
        dest.MIRROR,
    ]


def test_a_destination_cannot_be_asked_for_a_mode_it_cannot_do(tmp_path: Path) -> None:
    """A drive that lives in a drawer cannot be a Mirror. A policy it could
    never satisfy would fail permanently, and a permanently failing policy
    teaches people to ignore the page that reports it."""
    conn = connect(settings_for(tmp_path))
    drive = dest.add_destination(
        conn, name="WD 8TB", kind="local", target="/Volumes/WD", modes=[dest.OFFLINE]
    )

    with pytest.raises(ValueError, match="cannot be used as"):
        dest.set_policy(conn, category="photos", destination_id=drive, mode=dest.MIRROR)


def test_a_disabled_destination_runs_nothing(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path))
    nas = dest.add_destination(
        conn, name="NAS", kind="remote", target="nas:lib", modes=[dest.BACKUP]
    )
    dest.set_policy(conn, category="photos", destination_id=nas, mode=dest.BACKUP)
    assert dest.active(conn)

    dest.set_enabled(conn, nas, enabled=False)

    assert dest.active(conn) == []


def test_a_disabled_policy_runs_nothing(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path))
    nas = dest.add_destination(
        conn, name="NAS", kind="remote", target="nas:lib", modes=[dest.BACKUP]
    )
    dest.set_policy(conn, category="photos", destination_id=nas, mode=dest.BACKUP, enabled=False)

    assert dest.active(conn) == []


def test_an_unknown_category_or_mode_is_refused(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path))
    nas = dest.add_destination(
        conn, name="NAS", kind="remote", target="nas:lib", modes=[dest.BACKUP]
    )

    with pytest.raises(ValueError, match="unknown category"):
        dest.set_policy(conn, category="nonsense", destination_id=nas, mode=dest.BACKUP)
    with pytest.raises(ValueError, match="unknown mode"):
        dest.set_policy(conn, category="photos", destination_id=nas, mode="sync")


def test_every_mode_says_what_it_means_in_a_sentence() -> None:
    """"Backup" and "mirror" mean whatever the last program somebody used meant
    by them, so the meaning travels with the mode rather than living in a manual."""
    for mode in dest.MODES:
        assert dest.MODE_LABEL[mode]
        assert dest.MODE_MEANING[mode]
        assert "delete" not in dest.MODE_MEANING[mode].lower() or "never" in (
            dest.MODE_MEANING[mode].lower()
        )
