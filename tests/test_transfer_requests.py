"""Send this once — and every way that must not become a standing policy.

The whole of increment 7 is one boundary. *Send to Offline Backup → WD-8TB* and
`Category → Destination → Mode` share every dangerous piece of machinery — the
same comparison, the same adapter, the same argv, the same history — and share
none of the intent. So most of what is tested here is what pressing a button
does *not* do:

    it does not create a policy
    it does not change one
    it does not move, rename or re-file anything in the Library
    it does not remove anything, anywhere

and the rest is that a folder of a hundred thousand files is a path and a flag.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from librairy import (
    backup_runs,
    divergence,
    offline_drives,
    transfer_plan,
    transfer_requests,
)
from librairy import destinations as dest
from librairy.config import Settings
from librairy.db import connect
from librairy.transfer_paths import MARKER
from librairy.web.offline_send import offer_for, offers


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


class Stub:
    def __init__(self, returncode: int = 0, stderr: str = "") -> None:
        self.commands: list[list[str]] = []
        self.returncode = returncode
        self.stderr = stderr

    def __call__(self, command: list[str], timeout: int):  # noqa: ANN204
        del timeout
        self.commands.append(list(command))
        return subprocess.CompletedProcess(command, self.returncode, "", self.stderr)


def scene(tmp_path: Path):  # noqa: ANN201
    settings = settings_for(tmp_path)
    conn = connect(settings)
    mount = tmp_path / "wd"
    mount.mkdir()
    drive = offline_drives.register(conn, settings, name="WD-8TB", path=str(mount))
    return conn, settings, drive, mount


def library(conn, *paths: str, size: int = 100) -> None:  # noqa: ANN001
    for relpath in paths:
        conn.execute(
            "INSERT INTO items(root, relpath, size, mtime_ns, state, first_seen_at,"
            " last_seen_at) VALUES ('library', ?, ?, 0, 'committed', 'now', 'now')",
            (relpath, size),
        )


def real_file(settings: Settings, relpath: str, body: bytes = b"x" * 100) -> None:
    path = settings.library_dir / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)


# --- the boundary --------------------------------------------------------------------


def test_a_send_creates_no_policy(tmp_path: Path) -> None:
    """The whole of increment 7 in one assertion. Pressing a button on a folder
    is not configuring a recurring backup, and there is no path from here that
    could write one."""
    conn, _settings, drive, _mount = scene(tmp_path)
    library(conn, "Books/Programming/Rust/book.pdf")

    transfer_requests.ask(
        conn, destination_id=drive.id, relpath="Books/Programming/Rust", exact=False
    )

    assert dest.policies(conn) == []
    assert dest.active(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM backup_policies").fetchone()[0] == 0


def test_a_send_does_not_touch_an_existing_policy(tmp_path: Path) -> None:
    conn, _settings, drive, _mount = scene(tmp_path)
    dest.set_policy(
        conn, category="books", destination_id=drive.id, mode=dest.OFFLINE
    )
    before = [tuple(row) for row in conn.execute("SELECT * FROM backup_policies")]
    library(conn, "Books/Programming/Rust/book.pdf")

    transfer_requests.ask(
        conn, destination_id=drive.id, relpath="Books/Programming/Rust", exact=False
    )

    after = [tuple(row) for row in conn.execute("SELECT * FROM backup_policies")]
    assert after == before


def test_the_module_has_no_way_to_write_a_policy() -> None:
    """Structural rather than promised. There is no import, and no call."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(transfer_requests))
    called = {
        node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    for verb in ("set_policy", "clear_policy", "add_destination", "remove_destination"):
        assert verb not in called, verb
    #  Every statement this module runs, and not its prose: a docstring saying
    #  it never writes a policy would otherwise fail a test for never writing
    #  one.
    statements = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and any(verb in node.value for verb in ("SELECT", "INSERT", "UPDATE", "DELETE"))
    ]
    assert statements, "no SQL found — this test would pass on an empty module"
    for statement in statements:
        assert "backup_policies" not in statement
        assert "backup_destinations" not in statement


def test_a_send_leaves_the_library_exactly_as_it_was(tmp_path: Path) -> None:
    """Not the files, not the rows, not a proposal, not the taxonomy. A copy
    going outward is not a filing decision."""
    conn, settings, drive, _mount = scene(tmp_path)
    library(conn, "Photos/2024/a.jpg")
    real_file(settings, "Photos/2024/a.jpg")
    items = [tuple(row) for row in conn.execute("SELECT * FROM items")]

    asked = transfer_requests.ask(
        conn, destination_id=drive.id, relpath="Photos/2024", exact=False
    )
    transfer_requests.send(conn, settings, asked, drive, runner=Stub())

    assert [tuple(row) for row in conn.execute("SELECT * FROM items")] == items
    assert conn.execute("SELECT COUNT(*) FROM history").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 0
    assert (settings.library_dir / "Photos/2024/a.jpg").exists()


# --- what may be sent ----------------------------------------------------------------


def test_only_library_content_may_be_sent(tmp_path: Path) -> None:
    """Something in the inbox with a proposed destination has a path and is not
    a Library file. Sending one would act on a decision nobody has made."""
    conn, _settings, _drive, _mount = scene(tmp_path)
    conn.execute(
        "INSERT INTO items(root, relpath, size, mtime_ns, state, first_seen_at,"
        " last_seen_at) VALUES ('inbox', 'holiday.jpg', 10, 0, 'proposed', 'n', 'n')"
    )
    conn.execute(
        "INSERT INTO items(root, relpath, size, mtime_ns, state, first_seen_at,"
        " last_seen_at) VALUES ('quarantine', 'Photos/old.jpg', 10, 0, 'held', 'n', 'n')"
    )

    assert not transfer_requests.eligible(conn, "holiday.jpg", exact=True)
    assert not transfer_requests.eligible(conn, "Photos/old.jpg", exact=True)
    assert not transfer_requests.eligible(conn, "Photos", exact=False)


def test_a_missing_library_file_may_not_be_sent(tmp_path: Path) -> None:
    conn, _settings, _drive, _mount = scene(tmp_path)
    library(conn, "Photos/gone.jpg")
    conn.execute("UPDATE items SET missing_since='now' WHERE relpath='Photos/gone.jpg'")

    assert not transfer_requests.eligible(conn, "Photos/gone.jpg", exact=True)


def test_a_virtual_view_is_not_a_folder(tmp_path: Path) -> None:
    """A Project and a tag view gather files that live in different places.
    "Send this" against a filter would mean something nobody asked for, so
    eligibility is a question about paths, which a view cannot satisfy."""
    conn, _settings, _drive, _mount = scene(tmp_path)
    library(conn, "Photos/2024/a.jpg", "Documents/2024/b.pdf")
    conn.execute(
        "INSERT INTO projects(tag, name, created_at) VALUES ('ProjectHouse','House','n')"
    )
    for row in conn.execute("SELECT id FROM items"):
        conn.execute(
            "INSERT INTO item_tags(item_id, tag, label, source, added_at)"
            " VALUES (?, 'projecthouse', 'ProjectHouse', 'user', 'n')",
            (row["id"],),
        )

    #  The two files are one Project and are in no shared folder.
    assert not transfer_requests.eligible(conn, "Projects/House", exact=False)
    assert not transfer_requests.eligible(conn, "ProjectHouse", exact=False)
    #  Their real folders are sendable, which is the distinction.
    assert transfer_requests.eligible(conn, "Photos/2024", exact=False)


def test_a_similar_folder_name_is_not_swept_in(tmp_path: Path) -> None:
    conn, _settings, _drive, _mount = scene(tmp_path)
    library(conn, "Photos/a.jpg", "Photos Archive/b.jpg")

    assert transfer_requests.covers(conn, "Photos", exact=False).files == 1


# --- what the action offers ----------------------------------------------------------


def test_no_drive_no_action(tmp_path: Path) -> None:
    """Not rendered, rather than rendered and disabled. A greyed-out button for
    a drive in a drawer explains a thing that is not wrong."""
    conn, settings, drive, mount = scene(tmp_path)
    library(conn, "Photos/a.jpg")
    assert offers(conn, "Photos")

    (mount / MARKER).unlink()
    mount.rmdir()
    offline_drives.look(conn, settings, drive)

    assert offers(conn, "Photos") == []


def test_the_wrong_drive_offers_nothing(tmp_path: Path) -> None:
    conn, settings, drive, mount = scene(tmp_path)
    library(conn, "Photos/a.jpg")
    (mount / MARKER).write_text("librairy:not-yours\n", encoding="utf-8")
    offline_drives.look(conn, settings, drive)

    assert offers(conn, "Photos") == []
    assert offer_for(conn, "Photos", drive.id) is None


def test_a_marker_only_drive_is_offered_and_says_so(tmp_path: Path, monkeypatch) -> None:
    """Reduced verification does not hide the action. It is said in it."""
    from librairy import volumes

    settings = settings_for(tmp_path)
    conn = connect(settings)
    mount = tmp_path / "wd"
    mount.mkdir()
    monkeypatch.setattr(volumes, "identity_for", lambda _p: "uuid:AAAA")
    drive = offline_drives.register(conn, settings, name="WD-8TB", path=str(mount))
    library(conn, "Photos/a.jpg")

    monkeypatch.setattr(volumes, "identity_for", lambda _p: "")
    offline_drives.look(conn, settings, drive)

    [offer] = offers(conn, "Photos")
    assert offer.reduced


def test_the_offer_counts_from_the_index_and_not_the_drive(tmp_path: Path) -> None:
    """`842 files · 11.6 GB` is about their files. Showing it must never wait
    on a drive being enumerated."""
    conn, _settings, _drive, _mount = scene(tmp_path)
    for index in range(842):
        library(conn, f"Photos/2024/IMG_{index:04d}.jpg", size=13_775_534)

    [offer] = offers(conn, "Photos/2024")

    assert offer.files == 842  # noqa: PLR2004
    assert "842 files" in offer.summary
    assert offer.bytes == 842 * 13_775_534


def test_a_folder_of_a_hundred_thousand_files_is_one_row(tmp_path: Path) -> None:
    """A path and a flag. Not a hundred thousand rows, form fields or Python
    objects because somebody pressed a button."""
    conn, _settings, drive, _mount = scene(tmp_path)
    conn.executemany(
        "INSERT INTO items(root, relpath, size, mtime_ns, state, first_seen_at,"
        " last_seen_at) VALUES ('library', ?, 1000, 0, 'committed', 'n', 'n')",
        [(f"Photos/2024/IMG_{index:06d}.jpg",) for index in range(100_000)],
    )

    asked = transfer_requests.ask(
        conn, destination_id=drive.id, relpath="Photos/2024", exact=False
    )

    assert conn.execute("SELECT COUNT(*) FROM transfer_requests").fetchone()[0] == 1
    assert asked.relpath == "Photos/2024"
    assert transfer_requests.covers(conn, "Photos/2024", exact=False).files == 100_000  # noqa: PLR2004


def test_pressing_the_button_twice_is_one_request(tmp_path: Path) -> None:
    conn, _settings, drive, _mount = scene(tmp_path)
    library(conn, "Photos/a.jpg")

    first = transfer_requests.ask(
        conn, destination_id=drive.id, relpath="Photos", exact=False
    )
    second = transfer_requests.ask(
        conn, destination_id=drive.id, relpath="Photos", exact=False
    )

    assert first.id == second.id
    assert conn.execute("SELECT COUNT(*) FROM transfer_requests").fetchone()[0] == 1


# --- what a send actually does -------------------------------------------------------


def test_one_file_goes_out_under_its_library_path(tmp_path: Path) -> None:
    """`Books/Programming/Rust/book.pdf` lands at
    `<root>/Books/Programming/Rust/book.pdf`, which is what keeps a one-off
    send comparable with the backup that covers the same files."""
    conn, settings, drive, mount = scene(tmp_path)
    library(conn, "Books/Programming/Rust/book.pdf")
    real_file(settings, "Books/Programming/Rust/book.pdf")
    stub = Stub()

    asked = transfer_requests.ask(
        conn,
        destination_id=drive.id,
        relpath="Books/Programming/Rust/book.pdf",
        exact=True,
    )
    transfer_requests.send(conn, settings, asked, drive, runner=stub)

    [command] = stub.commands
    assert command[1] == "copy"
    assert command[2].endswith("Books/Programming/Rust/book.pdf")
    assert command[3] == str(mount / "Books/Programming/Rust")


def test_a_folder_goes_out_under_its_library_path(tmp_path: Path) -> None:
    conn, settings, drive, mount = scene(tmp_path)
    library(conn, "Books/Programming/Rust/book.pdf")
    real_file(settings, "Books/Programming/Rust/book.pdf")
    stub = Stub()

    asked = transfer_requests.ask(
        conn, destination_id=drive.id, relpath="Books/Programming", exact=False
    )
    transfer_requests.send(conn, settings, asked, drive, runner=stub)

    [command] = stub.commands
    assert command[2] == str(settings.library_dir / "Books/Programming")
    assert command[3] == str(mount / "Books/Programming")


def test_sending_the_same_thing_again_is_a_truthful_no_op(tmp_path: Path) -> None:
    """Already current, so nothing runs — and nothing is duplicated because the
    action was repeated."""
    conn, settings, drive, mount = scene(tmp_path)
    library(conn, "Photos/2024/a.jpg", size=100)
    real_file(settings, "Photos/2024/a.jpg")
    there = mount / "Photos/2024"
    there.mkdir(parents=True)
    (there / "a.jpg").write_bytes(b"x" * 100)
    stub = Stub()

    asked = transfer_requests.ask(
        conn, destination_id=drive.id, relpath="Photos/2024", exact=False
    )
    done = transfer_requests.send(conn, settings, asked, drive, runner=stub)

    assert done.state == transfer_requests.DONE
    assert stub.commands == [], "an up-to-date folder was copied again"
    assert sorted(path.name for path in there.iterdir()) == ["a.jpg"]


def test_a_stale_destination_copy_is_updated_from_the_library(tmp_path: Path) -> None:
    conn, settings, drive, mount = scene(tmp_path)
    library(conn, "Photos/2024/a.jpg", size=400)
    real_file(settings, "Photos/2024/a.jpg", b"y" * 400)
    there = mount / "Photos/2024"
    there.mkdir(parents=True)
    (there / "a.jpg").write_bytes(b"old")
    stub = Stub()

    asked = transfer_requests.ask(
        conn, destination_id=drive.id, relpath="Photos/2024", exact=False
    )
    transfer_requests.send(conn, settings, asked, drive, runner=stub)

    assert len(stub.commands) == 1


def test_what_is_only_on_the_drive_survives_a_send(tmp_path: Path) -> None:
    conn, settings, drive, mount = scene(tmp_path)
    library(conn, "Photos/2024/a.jpg")
    real_file(settings, "Photos/2024/a.jpg")
    there = mount / "Photos/2024"
    there.mkdir(parents=True)
    (there / "from-2019.jpg").write_bytes(b"theirs")

    asked = transfer_requests.ask(
        conn, destination_id=drive.id, relpath="Photos/2024", exact=False
    )
    transfer_requests.send(conn, settings, asked, drive, runner=Stub())

    assert (there / "from-2019.jpg").read_bytes() == b"theirs"


def test_a_send_of_one_subtree_never_clears_the_rest_of_a_category(
    tmp_path: Path,
) -> None:
    """A send looked at `Photos/2024` and saw nothing whatever about the rest of
    `Photos`. Reconciling the category from it would report the other years as
    having been tidied up."""
    conn, settings, drive, mount = scene(tmp_path)
    library(conn, "Photos/2024/a.jpg")
    real_file(settings, "Photos/2024/a.jpg")
    #  A whole-category comparison first, which found something in 2019.
    divergence.record(
        conn,
        destination_id=drive.id,
        category="photos",
        entries=[
            transfer_plan.Entry(
                relpath="Photos/2019/old.jpg",
                difference=dest.EXTRA,
                action=dest.REPORT,
                destination_size=5,
            )
        ],
        complete=True,
    )
    assert divergence.summary(conn, drive.id).count == 1
    (mount / "Photos/2024").mkdir(parents=True)

    asked = transfer_requests.ask(
        conn, destination_id=drive.id, relpath="Photos/2024", exact=False
    )
    transfer_requests.send(conn, settings, asked, drive, runner=Stub())

    found = divergence.summary(conn, drive.id)
    assert found.count == 1, "a send of one folder emptied the category's record"
    assert found.unverified, "and it did not admit it had seen only part"


# --- the gap between the click and the copy ------------------------------------------


def test_a_drive_removed_after_the_click_is_a_safe_refusal(tmp_path: Path) -> None:
    """The page offered this because the drive was here when it rendered. A
    page is not evidence about now, and nothing is written into the directory a
    mount point leaves behind."""
    conn, settings, drive, mount = scene(tmp_path)
    library(conn, "Photos/2024/a.jpg")
    real_file(settings, "Photos/2024/a.jpg")
    asked = transfer_requests.ask(
        conn, destination_id=drive.id, relpath="Photos/2024", exact=False
    )

    #  Unplugged. The mount point is left behind, empty, as it usually is.
    (mount / MARKER).unlink()
    stub = Stub()
    done = transfer_requests.send(conn, settings, asked, drive, runner=stub)

    assert done.state == transfer_requests.FAILED
    assert stub.commands == []
    assert list(mount.iterdir()) == [], "something was written to a leftover mount"


def test_a_drive_swapped_after_the_click_is_a_safe_refusal(tmp_path: Path) -> None:
    conn, settings, drive, mount = scene(tmp_path)
    library(conn, "Photos/2024/a.jpg")
    real_file(settings, "Photos/2024/a.jpg")
    asked = transfer_requests.ask(
        conn, destination_id=drive.id, relpath="Photos/2024", exact=False
    )

    (mount / MARKER).write_text("librairy:somebody-elses\n", encoding="utf-8")
    stub = Stub()
    done = transfer_requests.send(conn, settings, asked, drive, runner=stub)

    assert done.state == transfer_requests.FAILED
    assert stub.commands == []
    assert offline_drives.presence(conn, drive.id).refused
    assert [path.name for path in mount.iterdir()] == [MARKER]


# --- history -------------------------------------------------------------------------


def test_history_says_which_kind_of_thing_asked(tmp_path: Path) -> None:
    """One history, two origins. A second history system for manual sends would
    be a second place to be wrong about what reached a destination."""
    conn, settings, drive, _mount = scene(tmp_path)
    library(conn, "Photos/2024/a.jpg")
    real_file(settings, "Photos/2024/a.jpg")
    asked = transfer_requests.ask(
        conn, destination_id=drive.id, relpath="Photos/2024", exact=False
    )
    transfer_requests.send(conn, settings, asked, drive, runner=Stub())

    manual = backup_runs.last_run(conn, drive.id)
    assert manual is not None
    assert manual.origin == transfer_plan.MANUAL

    dest.set_policy(
        conn, category="photos", destination_id=drive.id, mode=dest.OFFLINE
    )
    from librairy import transfer_run

    transfer_run.run_policy(
        conn, settings, dest.policies(conn)[0], drive, [], runner=Stub()
    )

    scheduled = backup_runs.last_run(conn, drive.id)
    assert scheduled.origin == transfer_plan.POLICY
    assert scheduled.id != manual.id


def test_history_never_says_a_destination_is_current(tmp_path: Path) -> None:
    """The absence that increment 4 established, unchanged by a second origin."""
    import inspect

    source = inspect.getsource(backup_runs) + inspect.getsource(transfer_requests)
    for word in ("is_current", "synced", "in_sync", "up_to_date"):
        assert word not in source, word


def test_a_stored_command_carries_no_credential(tmp_path: Path) -> None:
    conn, settings, drive, _mount = scene(tmp_path)
    library(conn, "Photos/2024/a.jpg")
    real_file(settings, "Photos/2024/a.jpg")
    asked = transfer_requests.ask(
        conn, destination_id=drive.id, relpath="Photos/2024", exact=False
    )

    done = transfer_requests.send(
        conn,
        settings,
        asked,
        drive,
        runner=Stub(returncode=1, stderr="failed: --password hunter2 rejected"),
    )

    assert "hunter2" not in done.detail
    stored = " ".join(
        str(tuple(row)) for row in conn.execute("SELECT * FROM backup_runs")
    )
    assert "hunter2" not in stored


def test_a_request_is_bounded_history(tmp_path: Path) -> None:
    conn, _settings, drive, _mount = scene(tmp_path)
    library(conn, "Photos/a.jpg")
    for index in range(transfer_requests.KEEP + 40):
        asked = transfer_requests.ask(
            conn, destination_id=drive.id, relpath=f"Photos/{index}", exact=False
        )
        transfer_requests._finish(  # noqa: SLF001
            conn, asked.id, transfer_requests.DONE, "ok", ""
        )

    #  `KEEP` *finished* ones. A pending request is never pruned, whatever the
    #  history looks like — somebody is waiting for it.
    finished = conn.execute(
        "SELECT COUNT(*) FROM transfer_requests WHERE state IN ('done','failed')"
    ).fetchone()[0]
    assert finished <= transfer_requests.KEEP
