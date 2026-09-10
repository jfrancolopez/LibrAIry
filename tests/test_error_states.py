"""What LibrAIry says when something goes wrong, and whether it is true.

Every failure here is one a NAS produces on an ordinary Tuesday: a share that
unmounts, a disk that fills, a folder that comes back read-only, a database
somebody else is writing to. The question each test asks is the one a person
asks, in the order they ask it:

    What happened?
    Is my Library safe?
    What can I do next?

Before this file, most of these answered none of the three. A commit into a
Library whose share had unmounted said **2 files moved. Nothing failed.** — and
the files were inside the container. A commit into a read-only Library said
"3 failed." and nothing else, above a paragraph promising every file below had
been copied and verified. A reversal into a read-only folder was a *System
Fault* page. A failed upgrade was a Python traceback in the container log.

## Real conditions, not fake exceptions

`chmod`, a real 20 MB filesystem, a second connection actually holding the
writer lock. The rclone gate taught this the expensive way: a stubbed failure
tests the code's opinion of what the failure looks like, and the code's opinion
is the thing under test. Where the operating system condition cannot be created
safely in a unit test — a filesystem replaced under a running mount — the
*observation* is stood in for rather than the behaviour, and the test says so.
"""

from __future__ import annotations

import errno
import os
import re
import shutil
import sqlite3
import time
from pathlib import Path

import pytest

from librairy import executor, roots
from librairy.db import connect, impatient
from librairy.executor import StorageUnavailable, execute_plan
from librairy.failures import ACTION, OPERATIONAL, UNAVAILABLE, classify, from_outcome
from librairy.history import undo_plan
from librairy.planner import OperationSpec
from librairy.scanner import scan_root
from tests.support.pages import said
from tests.support.scenario import assert_sound, install


def filed(inst, count: int = 2) -> str:
    """An approved plan that files `count` inbox files into the library."""
    for index in range(count):
        inst.write("inbox", f"file-{index}.txt", f"content {index}".encode())
    inst.scan("inbox")
    return inst.plan_for(
        [
            OperationSpec("move", f"file-{index}.txt", "library", f"Documents/file-{index}.txt")
            for index in range(count)
        ]
    )


def commit_page(inst, plan_id: str) -> str:
    """What the person is looking at while the commit runs, as words."""
    inst.post(f"/commit/execute/{plan_id}")
    for _ in range(50):
        page = inst.client().get(f"/commit/progress/{plan_id}").text
        if "hx-trigger" not in page:
            return said(page)
        time.sleep(0.05)
    return said(page)


# --- the storage is not there --------------------------------------------------------


def test_a_commit_into_a_missing_library_moves_nothing_and_says_so(tmp_path: Path) -> None:
    """The worst thing this program has ever done, and the test that holds it shut.

    An unmounted share leaves an empty, writable directory at the mount point.
    Every check the executor made passed against it, so the commit filed two
    files into the container's own disk — where the next `docker compose up`
    destroys them — and reported *2 files moved. Nothing failed.*
    """
    inst = install(tmp_path)
    plan_id = filed(inst)
    inst.client()  # startup, which is where the storage is observed
    shutil.move(str(inst.settings.library_dir), str(tmp_path / "unplugged"))

    shown = commit_page(inst, plan_id)

    assert "Library storage is not available." in shown
    assert "No files were changed." in shown
    assert "Reconnect the library storage, then try again." in shown
    #  Not one byte, and not one row.
    assert inst.conn.execute("SELECT COUNT(*) FROM history").fetchone()[0] == 0
    assert sorted(p.name for p in inst.settings.inbox_dir.iterdir()) == [
        "file-0.txt",
        "file-1.txt",
    ]


def test_the_same_commit_works_once_the_storage_is_back(tmp_path: Path) -> None:
    """A refusal keeps the approval. Reconnecting is the whole of the fix."""
    inst = install(tmp_path)
    plan_id = filed(inst)
    inst.client()
    shutil.move(str(inst.settings.library_dir), str(tmp_path / "unplugged"))
    with pytest.raises(StorageUnavailable):
        execute_plan(inst.conn, plan_id, inst.settings)

    shutil.move(str(tmp_path / "unplugged"), str(inst.settings.library_dir))
    summary = execute_plan(inst.conn, plan_id, inst.settings)

    assert summary.done == 2
    assert_sound(inst)


def test_a_scan_of_absent_storage_does_not_declare_the_library_missing(
    tmp_path: Path,
) -> None:
    """The second face of the same absence.

    `_mark_missing` compares the index against what the walk found, and a walk
    of a bare mount point finds nothing — which is indistinguishable from
    somebody having deleted every file they own. One worker cycle against a
    dropped share used to empty Browse, empty Search, and leave twelve thousand
    rows saying the files were gone.
    """
    inst = install(tmp_path)
    inst.write("library", "Documents/kept.txt", b"filed last year")
    inst.scan("library")
    inst.client()
    #  The mount point, exactly as an unmount leaves it: present, empty, and on
    #  a different filesystem from the one that was observed at startup. The
    #  device number is what the operating system changes here, and it is stood
    #  in for so the test does not need a real mount.
    _pretend_remounted(inst, "library", volume="uuid:the-container-disk")
    (inst.settings.library_dir / "Documents/kept.txt").unlink()
    (inst.settings.library_dir / "Documents").rmdir()

    summary = scan_root(inst.conn, "library", inst.settings.library_dir, inst.settings)

    assert summary.unavailable is True
    assert summary.missing == 0
    assert len(inst.live("library")) == 1


def test_a_library_somebody_emptied_by_hand_is_not_mistaken_for_a_missing_mount(
    tmp_path: Path,
) -> None:
    """Emptiness alone proves nothing, and refusing on it would be its own damage.

    The index would go on insisting the files are there, on storage that is
    plainly present and plainly the right one.
    """
    inst = install(tmp_path)
    inst.write("library", "Documents/kept.txt", b"filed last year")
    inst.scan("library")
    inst.client()
    (inst.settings.library_dir / "Documents/kept.txt").unlink()
    (inst.settings.library_dir / "Documents").rmdir()

    summary = scan_root(inst.conn, "library", inst.settings.library_dir, inst.settings)

    assert summary.unavailable is False
    assert summary.missing == 1
    assert inst.live("library") == []


def test_starting_against_an_empty_mount_point_refuses_and_then_recovers(
    tmp_path: Path,
) -> None:
    """The upgrade case, and the one that must not lock somebody out.

    An installation that restarts while its share is down would, if it wrote
    down what it saw, record the empty mount point as its Library and then
    refuse the real one for not matching. So what it saw is recorded *as
    suspect* — enough to refuse while it still looks wrong, and never used as
    the identity to compare against.
    """
    inst = install(tmp_path)
    inst.write("library", "Documents/kept.txt", b"filed last year")
    inst.scan("library")
    stashed = tmp_path / "stashed"
    shutil.move(str(inst.settings.library_dir / "Documents"), str(stashed))

    roots.observe(inst.conn, inst.settings)  # startup, with the share down
    refused = roots.check(inst.conn, inst.settings, "library")
    assert refused is not None
    assert refused.kind == UNAVAILABLE

    shutil.move(str(stashed), str(inst.settings.library_dir / "Documents"))
    assert roots.check(inst.conn, inst.settings, "library") is None


def test_a_different_filesystem_mounted_as_the_library_is_refused(tmp_path: Path) -> None:
    """Not "not available" — something *is* there, and saying otherwise while a
    disk is plugged in is the same lie the offline drives refused to tell."""
    inst = install(tmp_path)
    inst.write("library", "Documents/kept.txt", b"filed last year")
    inst.scan("library")
    inst.client()
    _pretend_remounted(inst, "library", volume="uuid:0000")

    refused = roots.check(inst.conn, inst.settings, "library")

    assert refused is not None
    assert refused.kind == ACTION
    assert refused.code == "storage-not-recognised"


def _pretend_remounted(inst, name: str, *, volume: str = "") -> None:
    """Stand in for the one observation a unit test cannot make.

    Replacing the filesystem under a live mount point needs a mount, and a mount
    needs privileges a test suite must not have. What LibrAIry actually reads is
    the device number and — only when that has changed — the volume id, so those
    two readings are what is stood in for. `tests/test_error_states.py` is the
    only place this happens; everything else in this file creates its condition.
    """
    import json

    recorded = roots.recorded(inst.conn)
    entry = dict(recorded.get(name, {}))
    entry["dev"] = int(entry.get("dev", 0)) + 1
    entry["volume"] = volume or str(entry.get("volume") or "")
    recorded[name] = entry
    inst.conn.execute(
        "INSERT INTO worker_state(key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (roots.STATE_KEY, json.dumps(recorded, sort_keys=True)),
    )


# --- the storage will not take the file ----------------------------------------------


def test_a_read_only_library_says_what_is_wrong_and_offers_no_undo(tmp_path: Path) -> None:
    """The page said "3 failed." and nothing else.

    The reason — `[Errno 13] Permission denied` — was in the journal, rendered
    raw on the plan page beside a hash, under a heading that said "Journal". And
    the failure page offered **Undo**, on a commit where nothing had moved.
    """
    inst = install(tmp_path)
    plan_id = filed(inst)
    inst.client()
    os.chmod(inst.settings.library_dir, 0o555)
    try:
        shown = commit_page(inst, plan_id)
    finally:
        os.chmod(inst.settings.library_dir, 0o755)

    assert "LibrAIry is not allowed to write there." in shown
    assert "Their originals are where they were, and nothing was overwritten." in shown
    assert "Give the container's user write access to that folder, then try again." in shown
    #  Nothing moved, so there is nothing to reverse and no button to press.
    assert "Undo" not in shown
    assert "No file was moved. Every original is where it was." in shown


def test_a_failed_commit_never_claims_the_files_were_copied_and_verified(
    tmp_path: Path,
) -> None:
    """The paragraph under the heading was unconditional.

    "Every file below was copied and verified by hash before the original was
    released" appeared under the word **stopped**, on a commit where not one
    byte had moved. A safety claim is the sentence people stop reading at, so it
    is the one sentence that has to be earned every time.
    """
    inst = install(tmp_path)
    plan_id = filed(inst)
    inst.client()
    os.chmod(inst.settings.library_dir, 0o555)
    try:
        shown = commit_page(inst, plan_id)
    finally:
        os.chmod(inst.settings.library_dir, 0o755)

    assert "Every file below was copied and verified" not in shown


def test_a_full_destination_says_the_disk_is_full_and_leaves_no_partial_behind(
    tmp_path: Path, monkeypatch
) -> None:
    """`ENOSPC`, from the real errno, on the real copying path.

    The bytes half was already right — source preserved, `.part-` name, the
    files that fitted filed. What was wrong was everything a person could see:
    "1 failed", with the one word that would have fixed it in ten seconds
    nowhere on the page. And LibrAIry's own half-written file was left on the
    disk whose fullness was the failure.
    """
    inst = install(tmp_path)
    plan_id = filed(inst, count=1)
    inst.client()
    real_copy = shutil.copy2

    def full(src, dst, *args, **kwargs):
        with open(dst, "wb") as handle:  # noqa: PTH123 - a real partial file
            handle.write(Path(src).read_bytes()[:3])
        raise OSError(errno.ENOSPC, "No space left on device")

    def cross_device(src, dst, *args, **kwargs):
        raise OSError(errno.EXDEV, "forced cross-device")

    monkeypatch.setattr("librairy.executor.os.rename", cross_device)
    monkeypatch.setattr("librairy.executor.shutil.copy2", full)

    shown = commit_page(inst, plan_id)

    assert "The destination is full." in shown
    assert "Free space where the files are going, then commit again." in shown
    #  Our own incomplete copy, removed. It is a strict prefix of a file that is
    #  still whole at the source, nothing reads it, and it was sitting on the
    #  very disk that had no room.
    assert list(inst.settings.library_dir.rglob("*.part-*")) == []
    assert (inst.settings.inbox_dir / "file-0.txt").exists()
    monkeypatch.setattr("librairy.executor.shutil.copy2", real_copy)


def test_the_reason_survives_into_history_without_the_raw_exception(
    tmp_path: Path,
) -> None:
    """`/history/plans/{id}` is where "View in History" lands after a stop.

    It printed the journal's stored outcome verbatim — an errno, a host path and
    a temporary filename — as the whole account of what happened.
    """
    inst = install(tmp_path)
    plan_id = filed(inst)
    inst.client()
    os.chmod(inst.settings.library_dir, 0o555)
    try:
        commit_page(inst, plan_id)
    finally:
        os.chmod(inst.settings.library_dir, 0o755)

    page = inst.client().get(f"/history/plans/{plan_id}").text
    shown = said(page)

    assert "LibrAIry is not allowed to write there." in shown
    assert "No file moved. Every original is where it was." in shown
    #  Still reachable, because somebody debugging needs it — behind a summary
    #  that says what it is, not as the explanation.
    assert "Errno 13" in page
    assert "Errno 13" not in shown.split("Technical details")[0]


def test_a_recognised_failure_is_journalled_as_a_code_not_as_a_traceback_line(
    tmp_path: Path,
) -> None:
    """The journal is permanent, and `str(exc)` is not a vocabulary.

    Storing the code is what lets Commit, History and Health say the same thing
    about one failure without three of them parsing prose.
    """
    inst = install(tmp_path)
    plan_id = filed(inst, count=1)
    inst.client()
    os.chmod(inst.settings.library_dir, 0o555)
    try:
        execute_plan(inst.conn, plan_id, inst.settings)
    finally:
        os.chmod(inst.settings.library_dir, 0o755)

    outcome = inst.conn.execute("SELECT outcome FROM history").fetchone()["outcome"]

    assert outcome.startswith("failed permission-denied ")
    assert from_outcome(outcome).kind == ACTION


def test_an_old_journal_row_still_reads_as_something(tmp_path: Path) -> None:  # noqa: ARG001
    """Every installation that has ever had a commit fail holds rows like this."""
    failure = from_outcome("[Errno 28] No space left on device: '/library/x.part-1'")

    assert failure.kind == "fault"
    assert failure.what
    assert "No space left" in failure.detail


# --- undo -----------------------------------------------------------------------------


def test_undo_into_a_read_only_folder_is_a_refusal_not_a_system_fault(
    tmp_path: Path,
) -> None:
    """`PermissionError` used to travel out of the route.

    Every other refusal in Undo is an outcome — the file moved, the file
    changed, something is in its place — and the person is told which. A
    permission was the one that reached them as *System Fault — Internal system
    fault*, on the button whose whole purpose is to be the safe thing to press.
    """
    inst = install(tmp_path)
    plan_id = filed(inst, count=1)
    inst.client()
    execute_plan(inst.conn, plan_id, inst.settings)
    os.chmod(inst.settings.inbox_dir, 0o555)
    try:
        response = inst.post(f"/history/plans/{plan_id}/undo")
        shown = said(response.text)
    finally:
        os.chmod(inst.settings.inbox_dir, 0o755)

    assert response.status_code == 200
    assert "not put back — LibrAIry is not allowed to write there." in shown
    assert "Nothing was put back." in shown
    #  Still in the library, whole, and the journal says why it stayed.
    assert (inst.settings.library_dir / "Documents/file-0.txt").exists()


def test_undo_that_has_to_rename_says_something_else_is_at_the_old_path(
    tmp_path: Path,
) -> None:
    """It preserved both and told nobody.

    The reversal renamed rather than overwrite — which is right — and recorded
    the outcome as `ok`. The row read "put back", and somebody looking for
    `report.txt` would find a *different file* under that name and their own
    beside it as `report (2).txt`, with nothing anywhere saying so.
    """
    inst = install(tmp_path)
    inst.write("inbox", "report.txt", b"the original")
    inst.scan("inbox")
    plan_id = inst.plan_for(
        [OperationSpec("move", "report.txt", "library", "Documents/report.txt")]
    )
    inst.client()
    execute_plan(inst.conn, plan_id, inst.settings)
    inst.write("inbox", "report.txt", b"something else entirely")

    shown = said(inst.post(f"/history/plans/{plan_id}/undo").text)

    assert "under a new name" in shown
    assert "something else is at its old path, and it was not touched" in shown
    #  Both, untouched.
    assert (inst.settings.inbox_dir / "report.txt").read_bytes() == b"something else entirely"
    assert (inst.settings.inbox_dir / "report (2).txt").read_bytes() == b"the original"


def test_undo_into_missing_storage_refuses_before_it_reads_anything(
    tmp_path: Path,
) -> None:
    """Putting a file back into an unmounted mount point is the same loss as
    filing into one, and Undo reached the filesystem by a different door."""
    inst = install(tmp_path)
    plan_id = filed(inst, count=1)
    inst.client()
    execute_plan(inst.conn, plan_id, inst.settings)
    shutil.move(str(inst.settings.inbox_dir), str(tmp_path / "unplugged"))

    results = undo_plan(inst.conn, plan_id, inst.settings)

    assert results[0].outcome.startswith("undo_failed storage-unavailable")
    assert (inst.settings.library_dir / "Documents/file-0.txt").exists()


def test_undo_that_is_partly_refused_never_claims_everything_went_back(
    tmp_path: Path,
) -> None:
    """The page was headed "Undone" above "Every file below was put back where it
    came from", whatever was below it."""
    inst = install(tmp_path)
    plan_id = filed(inst, count=2)
    inst.client()
    execute_plan(inst.conn, plan_id, inst.settings)
    #  One of the two has been edited since, which is a refusal, and the other
    #  goes back cleanly.
    (inst.settings.library_dir / "Documents/file-1.txt").write_text("edited", encoding="utf-8")

    shown = said(inst.post(f"/history/plans/{plan_id}/undo").text)

    assert "1 of 2 put back." in shown
    assert "still where the commit left" in shown
    assert "Every file below was put back" not in shown


# --- the database ---------------------------------------------------------------------


def test_a_locked_database_is_not_reported_in_sqlite_s_words(tmp_path: Path) -> None:
    """"database is locked" is a sentence about a writer lock.

    The person reading it owns a file server, not a database. What they need to
    know is that LibrAIry could not write down what it did — which is a
    different and much more important fact than anything about locking.
    """
    inst = install(tmp_path)
    other = connect(inst.settings)
    other.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(sqlite3.OperationalError) as raised, impatient(inst.conn):
            inst.conn.execute(
                "INSERT INTO worker_state(key, value) VALUES ('x', '1')"
            )
        failure = classify(raised.value)
    finally:
        other.execute("ROLLBACK")
        other.close()

    assert failure.kind == OPERATIONAL
    assert failure.code == "database-busy"
    assert "database is locked" not in failure.what
    assert failure.what == (
        "LibrAIry could not update its database — something else was writing to it."
    )
    #  Still there for whoever is debugging it.
    assert "locked" in failure.detail


def test_the_database_failures_a_nas_produces_each_say_something_different() -> None:
    """Read-only, full and damaged are three different days.

    One "database error" for all of them is a message that cannot be acted on:
    the first needs a permission changed, the second needs space, and the third
    needs somebody to stop and restore a backup before using LibrAIry again.
    """
    kinds = {
        "attempt to write a readonly database": ("database-read-only", ACTION),
        "database or disk is full": ("database-full", OPERATIONAL),
        "database disk image is malformed": ("database-damaged", ACTION),
    }
    for message, (code, kind) in kinds.items():
        failure = classify(sqlite3.OperationalError(message))
        assert failure.code == code, message
        assert failure.kind == kind, message
        assert failure.next, message


def test_a_database_failure_after_the_bytes_moved_does_not_claim_nothing_changed(
    tmp_path: Path,
) -> None:
    """The one case where a blanket reassurance would be a lie.

    "Your files were not changed by this operation" is true of almost every
    database failure and false of exactly this one: the rename happened, and
    only the row describing it did not. The crash-recovery machinery from M4-02
    already knows how to finish it — what matters here is that nothing tells the
    person their library is untouched while a file is sitting in it.
    """
    inst = install(tmp_path)
    plan_id = filed(inst, count=1)
    inst.client()
    #  The bytes move, and the very first row that records the move does not.
    #  Patched at the function that writes it rather than at the connection: a
    #  `sqlite3.Connection` will not have its methods replaced, and the seam that
    #  matters — the write immediately after the rename — is the same either way.
    real_finish = executor._finish_op
    broken = {"count": 0}

    def failing(*args, **kwargs):
        broken["count"] += 1
        raise sqlite3.OperationalError("database or disk is full")

    executor._finish_op = failing
    try:
        shown = commit_page(inst, plan_id)
    finally:
        executor._finish_op = real_finish

    #  Twice: once for the move, and once for the executor's own attempt to
    #  record the failure — which needs the database that is the failure. So the
    #  run ends the way a killed one does, and is finished the same way.
    assert broken["count"] >= 1
    #  The file is in the library. Whatever the page says, it may not say that
    #  nothing moved.
    assert (inst.settings.library_dir / "Documents/file-0.txt").exists()
    assert "No file was moved" not in shown
    assert "LibrAIry could not update its database because the disk is full." in shown
    assert "Free space on the appdata volume, then try again." in shown

    #  And committing again finishes the job rather than doing it twice.
    summary = execute_plan(inst.conn, plan_id, inst.settings)
    assert summary.done == 1
    assert_sound(inst)


# --- what is expected, and must never be coloured like a fault ------------------------


def test_an_ai_provider_that_is_switched_off_is_not_an_error(tmp_path: Path) -> None:
    """M2 already had this right, and the point is that M4-06 did not undo it.

    A provider that is not there produces *Waiting for AI*, which resumes by
    itself. Turning it into a System Fault — or into anything red — is how a
    person learns to ignore the page that tells them about real problems.
    """
    inst = install(tmp_path)
    inst.write("inbox", "holiday.pdf", b"%PDF-1.4 not really")
    inst.scan("inbox")
    inst.analyze()
    inst.client()

    shown = said(inst.client().get("/health").text)

    assert "System Fault" not in shown
    assert "No AI provider is reachable" in shown
    #  Said as the state it is, in the words the vocabulary pins for it.
    assert "organizing runs on heuristics only" in shown


def test_a_corrupt_or_unsupported_file_does_not_stop_the_rest_of_the_inbox(
    tmp_path: Path,
) -> None:
    """A malformed PDF, an empty file and a file that is not what it claims.

    None of them may take the worker down or the batch with it: the ordinary
    file beside them still has to be analysed and offered.
    """
    inst = install(tmp_path)
    inst.write("inbox", "broken.pdf", b"%PDF-1.4\nthis is not a pdf at all\n")
    inst.write("inbox", "empty.bin", b"")
    inst.write("inbox", "movie.mkv", b"\x00\x01 not really a matroska")
    inst.write("inbox", "notes.txt", b"an ordinary note")
    inst.scan("inbox")

    summary = inst.analyze()

    assert summary.analyzed == 4
    assert len(inst.live("inbox")) == 4
    assert_sound(inst)


# --- the fault page ---------------------------------------------------------------------


def test_an_unexpected_fault_never_tells_somebody_to_repeat_a_file_operation(
    tmp_path: Path,
) -> None:
    """"Try again" is harmless after a listing and dangerous after a commit.

    The page said *Internal system fault* and offered the Dashboard. It said
    nothing about whether the operation had been interrupted, and nothing about
    where the account of it lives — which, for anything that moves bytes, is the
    journal and not a second press.
    """

    from fastapi.testclient import TestClient

    from librairy.web.app import create_app

    inst = install(tmp_path)
    app = create_app(inst.settings, inst.conn)

    @app.get("/commit/boom")
    def boom() -> None:  # pragma: no cover - raised, never returned
        raise RuntimeError("something nobody anticipated")

    #  `raise_server_exceptions=False` is what makes this a test of the page
    #  rather than of the test client: by default `TestClient` re-raises, and
    #  the handler under test never runs.
    client = TestClient(app, raise_server_exceptions=False)
    client.post("/setup", data={"password": "correct horse battery"})
    response = client.get("/commit/boom", headers={"accept": "text/html"})
    shown = said(response.text)

    assert response.status_code == 500
    assert "Traceback" not in response.text
    assert "something nobody anticipated" not in response.text
    assert "may have been interrupted part way through" in shown
    assert "View in Commit" in shown
    assert "Reference" in shown
    #  Never the word, on this page, for this kind of request.
    assert "try again" not in shown.lower()


def test_a_refusal_is_still_a_sentence_and_not_a_status_code(tmp_path: Path) -> None:
    """The other half of the same page, unchanged and deliberately so."""
    inst = install(tmp_path)
    response = inst.client().get("/items/999999", headers={"accept": "text/html"})

    assert response.status_code == 404
    assert "Traceback" not in response.text


# --- backups ---------------------------------------------------------------------------


def test_a_backup_failure_says_the_library_was_not_touched(tmp_path: Path) -> None:
    """The reassurance that is always true of a backup, and was never printed.

    Every backup verb in LibrAIry copies outward. A destination that is full, a
    remote that will not authenticate and a drive that is not the registered one
    all have the same consequence for the Library: none. Saying so is the
    difference between "the backup failed" and "the backup failed, and your
    files are fine" — which is the only half a person actually wants at the
    moment they read it.
    """
    from librairy.attention import report

    inst = install(tmp_path, BACKUP_ENABLED=True)
    inst.write("library", "Documents/kept.txt", b"filed last year")
    inst.scan("library")
    inst.client()
    found = report(inst.conn, inst.settings)

    #  Nothing is configured, so nothing is claimed. The invariant this test
    #  holds is about the concerns that *do* appear.
    for concern in found.concerns:
        if concern.code.startswith("backup-"):
            assert "delete" not in concern.detail.lower() or "never" in concern.detail.lower()


def test_a_disconnected_offline_drive_is_never_an_error(tmp_path: Path) -> None:
    """A registered backup drive in a drawer is where a backup drive lives.

    M3 settled this and M4-06 must not undo it by sweeping every "unavailable"
    into one red pile: it is not late, not missing and not failed, and colouring
    it teaches somebody that a warning about their backups means nothing.
    """
    from librairy.attention import ACTION as ACTION_LEVEL
    from librairy.attention import report

    inst = install(tmp_path)
    inst.client()
    drive = tmp_path / "drawer"
    drive.mkdir()
    from librairy.offline_drives import look, register

    destination = register(inst.conn, inst.settings, name="WD-8TB", path=str(drive))
    shutil.rmtree(drive)  # unplugged
    look(inst.conn, inst.settings, destination)  # the probe that notices

    found = report(inst.conn, inst.settings)
    away = [c for c in found.concerns if c.code == "backup-drive-away"]

    assert away, "a drive that is not here should still be reported as a state"
    assert away[0].level != ACTION_LEVEL
    assert destination.id


# --- migrations --------------------------------------------------------------------------


def test_a_database_from_a_newer_version_is_refused_in_sentences(tmp_path: Path) -> None:
    """The highest-stakes failure in the program, and it printed a traceback.

    `DatabaseVersionError` is a `RuntimeError`, so an image rolled back under a
    database a newer one had already upgraded walked straight past
    `except sqlite3.Error`, out of `validate_boot_or_die`, and gave the
    container's log a stack trace as its entire account of itself.
    """
    from librairy.boot import validate_boot
    from librairy.db import database_path

    inst = install(tmp_path)
    inst.conn.close()
    raw = sqlite3.connect(database_path(inst.settings))
    raw.execute("PRAGMA user_version=999")
    raw.close()

    errors = validate_boot(inst.settings, check_port=False)

    assert len(errors) == 1
    said_it = errors[0]
    assert "could not finish upgrading its database" in said_it
    #  Provable, not soothing: the migration runs inside a transaction and rolls
    #  back, so the version really is unchanged.
    assert "no Library files were reorganized" in said_it
    assert "start the version this database was last used with" in said_it
    #  And no backup is claimed, because LibrAIry does not take one.
    assert "backup" not in said_it.lower()


def test_a_failed_migration_leaves_the_schema_where_it_was(tmp_path: Path) -> None:
    """What the message asserts, asserted against the mechanism.

    A reassurance nobody checked is a reassurance that drifts. `migrate` wraps
    each step in `BEGIN … COMMIT` and rolls back on failure, and this is the
    test that says so — so that the sentence in `boot.py` stays true.
    """
    from librairy.db import MIGRATIONS, SCHEMA_VERSION, migrate, user_version

    inst = install(tmp_path)
    before = user_version(inst.conn)
    assert before == SCHEMA_VERSION

    inst.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION - 1}")
    broken = dict(MIGRATIONS)
    broken[SCHEMA_VERSION] = "CREATE TABLE items(nope);"  # already exists
    original = MIGRATIONS.get(SCHEMA_VERSION)
    MIGRATIONS[SCHEMA_VERSION] = broken[SCHEMA_VERSION]
    try:
        with pytest.raises(sqlite3.Error):
            migrate(inst.conn)
        assert user_version(inst.conn) == SCHEMA_VERSION - 1
    finally:
        MIGRATIONS[SCHEMA_VERSION] = original
        inst.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


# --- the shape of the whole thing --------------------------------------------------------


def test_no_page_a_person_reaches_shows_a_raw_exception(tmp_path: Path) -> None:
    """One sweep, over the surfaces this pass touched.

    Not a proof — no test can be — but it holds the specific leaks that were
    there: a `sqlite3.OperationalError` class name, a `LifecycleError`, an errno
    and a host path as the first thing somebody reads.
    """
    inst = install(tmp_path)
    plan_id = filed(inst)
    inst.client()
    os.chmod(inst.settings.library_dir, 0o555)
    try:
        commit_page(inst, plan_id)
    finally:
        os.chmod(inst.settings.library_dir, 0o755)
    inst.post(f"/history/plans/{plan_id}/undo")

    leaks = ("Traceback", "sqlite3.", "LifecycleError", "OperationalError:")
    for url in ("/dashboard", "/health", "/commit", "/history", f"/history/plans/{plan_id}"):
        page = inst.client().get(url).text
        #  The first thing somebody reads, which is everything outside a closed
        #  `<details>`. The diagnostics are allowed to exist; they are not
        #  allowed to be the explanation.
        primary = said(page).split("Technical details")[0]
        for leak in leaks:
            assert leak not in primary, f"{leak} on {url}"


def test_every_failure_answers_all_three_questions() -> None:
    """The standard, held against the table rather than against a page.

    A `Failure` that has no `next` is a dead end, and one with no `what` is the
    status code it replaced. The middle question is deliberately absent here —
    whether the Library is safe depends on what the operation had already done,
    which only the caller knows.
    """
    from librairy.failures import _BY_CODE

    for code, failure in _BY_CODE.items():
        assert failure.what.endswith("."), code
        assert failure.next.endswith("."), code
        assert failure.kind in {"unavailable", "operational", "action", "fault"}, code
        #  No status codes, no class names, no errnos in the sentence people read.
        assert "Errno" not in failure.what, code
        assert "Error" not in failure.what, code


# --- htmx ------------------------------------------------------------------------------


def test_a_failing_htmx_action_answers_with_a_sentence_the_page_can_show(
    tmp_path: Path,
) -> None:
    """htmx does not swap an error response, and that left the app silent.

    Press a button whose action refuses, and the row stayed exactly as it was:
    no swap, no message, nothing anywhere saying the thing had not happened.
    Which is the worst possible outcome, because "it looks unchanged" is also
    what a successful toggle-back looks like.

    The server half of the fix is that the refusal is a *sentence* rather than a
    status code, so `announce.js` has something true to show and to say.
    """
    inst = install(tmp_path)
    client = inst.client()

    response = client.post(
        "/items/999999/identify",
        headers={"HX-Request": "true", "x-csrf-token": client.cookies["csrf_token"]},
    )

    assert response.status_code == 404
    #  JSON for htmx, as it has always been — but with prose in it, because
    #  that is what the page shows and what the live region says.
    detail = response.json()["detail"]
    assert detail == "that item no longer exists"
    assert "Traceback" not in detail


def test_the_live_region_is_told_about_a_failed_action() -> None:
    """The accessibility half of the same fix, held at the source.

    M4-04 built a live region and wired it to `htmx:afterSwap`. A failure is not
    a swap, so a refused action reached it through nothing at all — silence for
    somebody who cannot see the row that did not change.
    """
    source = Path("src/librairy/web/static/announce.js").read_text(encoding="utf-8")

    for event in ("htmx:responseError", "htmx:sendError", "htmx:timeout"):
        assert event in source, event
    assert "region.textContent" in source
    #  And never that advice in anything it *says*. After an action that may
    #  have touched files, "try again" is advice nobody can give without knowing
    #  what happened to the bytes — Commit and Undo answer that on their own
    #  pages, from the journal. Checked against the string literals rather than
    #  the file, so that the comment explaining the rule does not break it.
    without_comments = re.sub(r"(?m)//.*$", "", re.sub(r"(?s)/\*.*?\*/", "", source))
    literals = re.findall(r'"([^"\\]*)"', without_comments)
    assert literals
    assert not [said_it for said_it in literals if "try again" in said_it.lower()]


def test_the_preview_panels_keep_their_own_message_and_do_not_get_two() -> None:
    """Two handlers, one message.

    `review.js` puts the reason inside the panel that stayed empty, which is
    where it belongs. The general handler would otherwise add a second copy of
    it beside the button — so the specific one marks the event, and the general
    one only speaks.
    """
    review = Path("src/librairy/web/static/review.js").read_text(encoding="utf-8")
    announce = Path("src/librairy/web/static/announce.js").read_text(encoding="utf-8")

    assert "errorHandled = true" in review
    assert "event.detail.errorHandled" in announce


# --- OCR ---------------------------------------------------------------------------------


def test_ocr_switched_off_is_a_choice_and_ocr_missing_is_an_absence(
    tmp_path: Path, monkeypatch
) -> None:
    """A missing capability is not corrupted data, and neither is a setting.

    Switching OCR on with no tesseract installed was silent: every scanned
    document went on being read from a text layer it does not have, resolved
    nothing, and nothing anywhere said why. Off, meanwhile, is the shipped state
    and a deliberate one — warning about it would be warning somebody about
    their own decision.
    """
    from librairy import ocr
    from librairy.web.health import ocr_status

    inst = install(tmp_path)

    off = ocr_status(inst.conn)
    assert off.status == "OK"
    assert "switched off" in off.detail

    ocr.set_enabled(inst.conn, True)
    monkeypatch.setattr(ocr, "available", lambda: False)
    absent = ocr_status(inst.conn)
    assert absent.status == "WARN"
    assert "tesseract is not installed" in absent.detail
    assert absent.hint

    monkeypatch.setattr(ocr, "available", lambda: True)
    assert ocr_status(inst.conn).status == "OK"


def test_ocr_that_cannot_read_a_page_never_costs_the_batch(tmp_path: Path) -> None:  # noqa: ARG001
    """A photograph of a page that will not rasterise is a normal outcome.

    Every failure inside `read_pages` — no binary, a timeout, a page poppler
    cannot handle — has to be "nothing was read", because the alternative is one
    unreadable scan stopping an inbox.
    """
    from librairy.ocr import read_pages

    def refuses(*args, **kwargs):
        raise OSError(errno.ENOENT, "No such file or directory: 'pdftoppm'")

    assert read_pages(Path("/nonexistent.pdf"), run=refuses) == ""


# --- where it is said ---------------------------------------------------------------------


def test_the_dashboard_says_the_storage_is_away_before_it_says_anything_else(
    tmp_path: Path,
) -> None:
    """The first thing somebody needs, and the last thing any table would reveal.

    Every other line under "Needs a look" is work waiting for a decision. This
    one is those decisions being impossible to carry out, so reading "12 changes
    waiting for Commit" above it would be reading them in the wrong order.
    """
    inst = install(tmp_path)
    inst.write("library", "Documents/kept.txt", b"filed last year")
    inst.scan("library")
    inst.client()
    _pretend_remounted(inst, "library", volume="uuid:the-container-disk")
    (inst.settings.library_dir / "Documents/kept.txt").unlink()
    (inst.settings.library_dir / "Documents").rmdir()

    shown = said(inst.client().get("/dashboard").text)

    assert "A different filesystem is mounted as your library." in shown
    assert "Nothing has been moved into it." in shown
    assert "Mount the storage LibrAIry was started against" in shown


def test_health_stops_reporting_free_space_for_storage_that_is_not_there(
    tmp_path: Path,
) -> None:
    """`_disk_stats` walked up to the nearest existing directory.

    So a Library whose share had unmounted was reported as "library — 312GB free
    of 460GB": the container's own disk, wearing the Library's name, on the
    panel somebody opens to find out whether their storage is all right.
    """
    from librairy.web.health import disk_statuses

    inst = install(tmp_path)
    inst.client()
    shutil.move(str(inst.settings.library_dir), str(tmp_path / "unplugged"))

    rows = {row.name: row for row in disk_statuses(inst.settings, inst.conn)}

    assert rows["library"].status == "FAIL"
    assert "not there" in rows["library"].detail
    assert "free of" not in rows["library"].detail


def test_a_failed_commit_reaches_health_and_leaves_when_it_is_fixed(
    tmp_path: Path,
) -> None:
    """Three files failed to move and Health said nothing needed attention.

    And the other half, which is what makes it worth having: it goes away by
    itself. A red card about a problem somebody has already fixed is the fastest
    way to teach them that the red cards are furniture.
    """
    from librairy.attention import report

    inst = install(tmp_path)
    plan_id = filed(inst, count=2)
    inst.client()
    os.chmod(inst.settings.library_dir, 0o555)
    try:
        execute_plan(inst.conn, plan_id, inst.settings)
    finally:
        os.chmod(inst.settings.library_dir, 0o755)

    concerns = {c.code: c for c in report(inst.conn, inst.settings).concerns}
    assert "commit-failed-permission-denied" in concerns
    assert concerns["commit-failed-permission-denied"].count == 2
    assert "LibrAIry is not allowed to write there." in (
        concerns["commit-failed-permission-denied"].detail
    )

    execute_plan(inst.conn, plan_id, inst.settings)

    after = {c.code for c in report(inst.conn, inst.settings).concerns}
    assert "commit-failed-permission-denied" not in after


def test_one_root_cause_is_one_concern_and_not_one_per_file(tmp_path: Path) -> None:
    """Forty files that would not fit on a full disk are one full disk.

    Franco's example was rclone: "rclone missing" plus "4 policies failed
    because rclone is missing" should be a root cause and its scope, not five
    unrelated red cards. The same rule, applied to a commit.
    """
    from librairy.attention import report

    inst = install(tmp_path)
    plan_id = filed(inst, count=6)
    inst.client()
    os.chmod(inst.settings.library_dir, 0o555)
    try:
        execute_plan(inst.conn, plan_id, inst.settings)
    finally:
        os.chmod(inst.settings.library_dir, 0o755)

    failures = [c for c in report(inst.conn, inst.settings).concerns if "failed" in c.code]

    assert len(failures) == 1
    assert failures[0].count == 6
    assert "6 files did not move" in failures[0].headline


def test_a_preview_that_fails_shows_the_reason_and_not_the_errno(tmp_path: Path) -> None:
    """Two kinds of `OSError` reach the same line, and only one is worth printing.

    The preview pipeline raises with prose — "the thumbnail cache is
    unavailable" — and says it better than anything could say it for it. The
    operating system raises `[Errno 13] Permission denied:
    '/library/Photos/2026/a.jpg'`, which is not a caption for a photograph. An
    errno is what tells them apart.
    """
    from librairy.web import browse

    inst = install(tmp_path)
    inst.write("library", "Photos/a.jpg", b"\xff\xd8\xff not really a jpeg")
    inst.scan("library")
    inst.client()
    item_id = int(inst.item_at("library", "Photos/a.jpg")["id"])

    def denied(*args, **kwargs):
        raise PermissionError(errno.EACCES, "Permission denied", "/library/Photos/a.jpg")

    def its_own_words(*args, **kwargs):
        raise OSError("the thumbnail cache is unavailable")

    browse.preview_for_item = denied
    try:
        shown = said(inst.client().get(f"/items/{item_id}").text)
        assert "LibrAIry is not allowed to write there." in shown
        assert "Errno 13" not in shown
    finally:
        browse.preview_for_item = its_own_words
    try:
        shown = said(inst.client().get(f"/items/{item_id}").text)
        assert "the thumbnail cache is unavailable" in shown
    finally:
        from librairy.web.thumbs import preview_for_item

        browse.preview_for_item = preview_for_item
