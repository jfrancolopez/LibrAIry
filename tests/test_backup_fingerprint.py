"""A backup row says *which bytes* it copied. That has to be true.

`backup_queue` records a fingerprint, and `state='done'` is LibrAIry asserting
that those bytes are on the remote. Until this pass the code never read that
field during a copy: it ran `rclone check <source> <remote>` afterwards, which
answers "is the file at this path on the remote" — a different question, and
one whose answer stays yes while the row's own answer quietly becomes no.

    row says fingerprint A
    the file at that path becomes B
    the copy sends B, check compares B against B, both agree
    the A row is marked done

The database then claims A was backed up when A was never sent anywhere. A
failed backup is loud and recoverable. A backup record that is untrue is
neither, and this is the one table whose entire purpose is to be believed on
the day the original is gone.

The rclone here is a simulator rather than a script: `copy` stores whatever
bytes are at the source *at the moment it runs*, and `check` compares the
source against what was stored. So the race is not staged — it emerges from
the same ordering a real transfer has, and a test that passes only because the
fake was told the right answer is not possible here.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from librairy.backup import enqueue_backup_item, run_backup_once
from librairy.config import Settings
from librairy.db import connect
from librairy.fingerprint import blake2b_file

RELPATH = "Music/Live/concert.flac"
BYTES_A = b"the original recording" * 64
BYTES_B = b"a different recording!" * 64


class AvailableStatus:
    available = True
    detail = "ok"


class FakeRclone:
    """A remote that really holds something, so `check` can be genuinely wrong.

    `copy` reads the source when it is called and keeps those bytes; `check`
    re-reads the source and compares. Both take a hook that fires *before* they
    touch the file, which is how each timing in the race is expressed: a hook on
    `copy` means the file changed while the transfer was reading it, a hook on
    `check` means it changed after the transfer and before the verification.
    """

    def __init__(
        self,
        *,
        on_copy: Callable[[], None] | None = None,
        on_check: Callable[[], None] | None = None,
        check_output: str = "0 differences found",
        check_always_ok: bool = False,
    ) -> None:
        self.remote: dict[str, bytes] = {}
        self.commands: list[list[str]] = []
        self.on_copy = on_copy
        self.on_check = on_check
        self.check_output = check_output
        self.check_always_ok = check_always_ok

    def __call__(self, command: list[str]) -> CompletedProcess[str]:
        self.commands.append(command)
        verb, source, remote = command[1], Path(command[2]), command[3]
        if verb == "copy":
            if "librairy.db" in str(source):
                return CompletedProcess(command, 0, "ok", "")
            if self.on_copy:
                self.on_copy()
            try:
                self.remote[remote] = source.read_bytes()
            except OSError as exc:
                return CompletedProcess(command, 1, "", str(exc))
            return CompletedProcess(command, 0, "ok", "")
        if self.on_check:
            self.on_check()
        if self.check_always_ok:
            return CompletedProcess(command, 0, self.check_output, "")
        try:
            current = source.read_bytes()
        except OSError as exc:
            return CompletedProcess(command, 1, "", str(exc))
        if self.remote.get(remote) == current:
            return CompletedProcess(command, 0, self.check_output, "")
        return CompletedProcess(command, 1, "1 differences found", "")

    @property
    def uploaded(self) -> list[bytes]:
        return list(self.remote.values())


@pytest.fixture
def queued(tmp_path: Path, monkeypatch):
    """One library file holding BYTES_A, with a request for exactly those bytes."""
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        BACKUP_ENABLED=True,
        BACKUP_REMOTE="remote:library",
        BACKUP_INCLUDE_DB_SNAPSHOT=False,
        _env_file=None,
    )
    config = settings.appdata_dir / "rclone" / "rclone.conf"
    config.parent.mkdir(parents=True)
    config.write_text("[remote]\n", encoding="utf-8")
    source = settings.library_dir / RELPATH
    source.parent.mkdir(parents=True)
    source.write_bytes(BYTES_A)
    hash_a = blake2b_file(source)
    conn = connect(settings)
    conn.execute(
        """
        INSERT INTO items(id, root, relpath, size, mtime_ns, fingerprint,
                          first_seen_at, last_seen_at)
        VALUES (42, 'library', ?, ?, 1, ?, 'now', 'now')
        """,
        (RELPATH, len(BYTES_A), hash_a),
    )
    enqueue_backup_item(conn, settings, item_id=42, relpath=RELPATH, fingerprint=hash_a)
    monkeypatch.setattr("librairy.backup.rclone_status", lambda path: AvailableStatus())
    return conn, settings, source, hash_a


def install(monkeypatch, rclone: FakeRclone) -> FakeRclone:
    monkeypatch.setattr("librairy.backup.run", rclone)
    return rclone


def rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM backup_queue ORDER BY id"))


def become(source: Path, data: bytes) -> Callable[[], None]:
    return lambda: source.write_bytes(data)


# --- the request is satisfied honestly ---------------------------------------------


def test_the_exact_requested_bytes_may_be_marked_done(queued, monkeypatch) -> None:
    conn, settings, _source, hash_a = queued
    rclone = install(monkeypatch, FakeRclone())

    summary = run_backup_once(conn, settings)

    row = rows(conn)[0]
    assert summary.copied == 1
    assert (row["state"], row["fingerprint"]) == ("done", hash_a)
    assert rclone.uploaded == [BYTES_A]


def test_done_records_how_the_remote_was_compared(queued, monkeypatch) -> None:
    """`rclone check` compares hashes where both sides can produce one and
    falls back to size where they cannot, and it says which. Recording the
    weaker answer as the stronger one would be the same kind of untruth this
    whole file exists to prevent."""
    conn, settings, _source, _hash_a = queued
    install(monkeypatch, FakeRclone(check_output="1 hashes could not be checked"))

    run_backup_once(conn, settings)

    assert rows(conn)[0]["verified"] == "size"


def test_a_hash_compared_remote_says_so(queued, monkeypatch) -> None:
    conn, settings, _source, _hash_a = queued
    install(monkeypatch, FakeRclone())

    run_backup_once(conn, settings)

    assert rows(conn)[0]["verified"] == "hash"


# --- CASE 1: the file changes before the copy opens it ------------------------------


def test_a_request_for_bytes_that_are_no_longer_there_uploads_nothing(
    queued, monkeypatch
) -> None:
    """Caught before the transfer, which is the cheap place to catch it: the
    wrong file is never sent at all."""
    conn, settings, source, _hash_a = queued
    rclone = install(monkeypatch, FakeRclone())
    source.write_bytes(BYTES_B)

    summary = run_backup_once(conn, settings)

    assert summary.copied == 0
    assert summary.failed == 0
    assert summary.superseded == 1
    assert rclone.commands == [], "no copy, no check, nothing sent"


def test_the_unsatisfiable_request_is_discarded_rather_than_retried(
    queued, monkeypatch
) -> None:
    """`failed` means try again, and no number of attempts can put bytes on the
    remote that are not on the disk. Three retries would mean three uploads of
    the wrong file before the queue gave up."""
    conn, settings, source, hash_a = queued
    install(monkeypatch, FakeRclone())
    source.write_bytes(BYTES_B)

    run_backup_once(conn, settings)

    assert [row["fingerprint"] for row in rows(conn)] != [hash_a]
    assert not conn.execute(
        "SELECT 1 FROM backup_queue WHERE fingerprint=?", (hash_a,)
    ).fetchall()


def test_the_bytes_that_are_actually_there_get_their_own_request(
    queued, monkeypatch
) -> None:
    conn, settings, source, hash_a = queued
    install(monkeypatch, FakeRclone())
    source.write_bytes(BYTES_B)
    hash_b = blake2b_file(source)
    conn.execute("UPDATE items SET fingerprint=? WHERE id=42", (hash_b,))

    run_backup_once(conn, settings)

    queue = rows(conn)
    assert [(row["fingerprint"], row["state"]) for row in queue] == [(hash_b, "queued")]
    assert hash_b != hash_a


def test_the_new_request_is_carried_out_on_the_next_run(queued, monkeypatch) -> None:
    conn, settings, source, _hash_a = queued
    rclone = install(monkeypatch, FakeRclone())
    source.write_bytes(BYTES_B)
    hash_b = blake2b_file(source)
    conn.execute("UPDATE items SET fingerprint=? WHERE id=42", (hash_b,))

    run_backup_once(conn, settings)
    summary = run_backup_once(conn, settings)

    assert summary.copied == 1
    assert rows(conn)[0]["state"] == "done"
    assert rclone.uploaded == [BYTES_B]


def test_new_bytes_are_not_attributed_to_whoever_held_the_path_before(
    queued, monkeypatch
) -> None:
    """A path changes hands. Adoption puts the optimized version at the
    original's own path under a *different* item id, so reusing the discarded
    request's item would file the backup against the wrong item — fixing one
    untrue record by writing another."""
    conn, settings, source, _hash_a = queued
    install(monkeypatch, FakeRclone())
    source.write_bytes(BYTES_B)
    hash_b = blake2b_file(source)
    #  The index says somebody else owns this path now.
    conn.execute("UPDATE items SET relpath='Music/Live/old.flac' WHERE id=42")
    conn.execute(
        """
        INSERT INTO items(id, root, relpath, size, mtime_ns, fingerprint,
                          first_seen_at, last_seen_at)
        VALUES (99, 'library', ?, ?, 1, ?, 'now', 'now')
        """,
        (RELPATH, len(BYTES_B), hash_b),
    )

    run_backup_once(conn, settings)

    queue = rows(conn)
    assert [(row["item_id"], row["fingerprint"]) for row in queue] == [(99, hash_b)]


def test_nothing_is_requeued_while_the_index_and_the_disk_disagree(
    queued, monkeypatch
) -> None:
    """The scanner has not caught up, so who owns these bytes is a guess.
    Waiting is more honest than inventing a request; the Health panel reports
    the gap meanwhile."""
    conn, settings, source, _hash_a = queued
    install(monkeypatch, FakeRclone())
    source.write_bytes(BYTES_B)

    run_backup_once(conn, settings)

    assert rows(conn) == []


# --- CASE 2: the file changes while the copy is running -----------------------------


def test_a_file_swapped_mid_copy_cannot_complete_the_old_request(
    queued, monkeypatch
) -> None:
    """The transfer carries B, the check compares B against B and agrees, and
    the row still says A. This is the exact shape of the old defect."""
    conn, settings, source, hash_a = queued
    rclone = install(monkeypatch, FakeRclone(on_copy=become(source, BYTES_B)))

    summary = run_backup_once(conn, settings)

    assert rclone.uploaded == [BYTES_B], "the remote really did receive B"
    assert summary.copied == 0
    assert not conn.execute(
        "SELECT 1 FROM backup_queue WHERE fingerprint=? AND state='done'", (hash_a,)
    ).fetchall()


def test_a_remote_that_agrees_is_not_enough_on_its_own(queued, monkeypatch) -> None:
    """Even where the remote verification passes — a remote that cannot hash,
    a check that compares the new bytes against the new bytes — the row is
    judged against its own fingerprint and not against the remote's opinion."""
    conn, settings, source, hash_a = queued
    install(monkeypatch, FakeRclone(on_copy=become(source, BYTES_B), check_always_ok=True))

    run_backup_once(conn, settings)

    assert not conn.execute(
        "SELECT 1 FROM backup_queue WHERE fingerprint=? AND state='done'", (hash_a,)
    ).fetchall()


# --- CASE 3: the file changes after the transfer, before the verification -----------


def test_a_change_between_transfer_and_verification_is_caught(queued, monkeypatch) -> None:
    conn, settings, source, hash_a = queued
    rclone = install(monkeypatch, FakeRclone(on_check=become(source, BYTES_B)))

    run_backup_once(conn, settings)

    assert rclone.uploaded == [BYTES_A], "A did reach the remote"
    #  And A is still not marked done, because from here it cannot be
    #  distinguished from the case where it did not.
    assert not conn.execute(
        "SELECT 1 FROM backup_queue WHERE fingerprint=? AND state='done'", (hash_a,)
    ).fetchall()


def test_that_case_is_superseded_rather_than_failed(queued, monkeypatch) -> None:
    conn, settings, source, _hash_a = queued
    install(monkeypatch, FakeRclone(on_check=become(source, BYTES_B), check_always_ok=True))

    summary = run_backup_once(conn, settings)

    assert (summary.copied, summary.failed, summary.superseded) == (0, 0, 1)


# --- CASE 4: the file disappears --------------------------------------------------


def test_a_source_that_vanishes_during_the_copy_fails_and_stays_retryable(
    queued, monkeypatch
) -> None:
    """Retryable on purpose: a source that cannot be read is usually a share
    that is not mounted, not a request that was wrong."""
    conn, settings, source, _hash_a = queued
    install(monkeypatch, FakeRclone(on_copy=lambda: source.unlink()))

    summary = run_backup_once(conn, settings)

    row = rows(conn)[0]
    assert (summary.failed, summary.superseded) == (1, 0)
    assert row["state"] == "failed"
    assert row["attempts"] == 1


def test_a_source_that_vanishes_after_the_transfer_still_cannot_be_done(
    queued, monkeypatch
) -> None:
    conn, settings, source, _hash_a = queued
    install(monkeypatch, FakeRclone(on_check=lambda: source.unlink(), check_always_ok=True))

    run_backup_once(conn, settings)

    row = rows(conn)[0]
    assert row["state"] == "failed"
    assert "during the copy" in row["last_error"]


def test_an_unreadable_source_is_never_uploaded(queued, monkeypatch) -> None:
    conn, settings, source, _hash_a = queued
    rclone = install(monkeypatch, FakeRclone())
    source.unlink()

    run_backup_once(conn, settings)

    assert rclone.commands == []
    assert rows(conn)[0]["state"] == "failed"


# --- CASE 5: the path is reused ---------------------------------------------------


def test_a_done_row_stays_true_when_the_path_takes_new_bytes(queued, monkeypatch) -> None:
    """A `done` row is history, not a description of the file at that path. A
    was backed up; that stays a fact whatever happens to the path afterwards,
    and rewriting the row to say B would destroy the only record that A ever
    existed off-site."""
    conn, settings, source, hash_a = queued
    install(monkeypatch, FakeRclone())
    run_backup_once(conn, settings)

    source.write_bytes(BYTES_B)
    hash_b = blake2b_file(source)
    conn.execute("UPDATE items SET fingerprint=? WHERE id=42", (hash_b,))
    enqueue_backup_item(conn, settings, item_id=42, relpath=RELPATH, fingerprint=hash_b)

    queue = {row["fingerprint"]: row["state"] for row in rows(conn)}
    assert queue == {hash_a: "done", hash_b: "queued"}


def test_the_same_bytes_are_not_uploaded_twice(queued, monkeypatch) -> None:
    """Re-adopting an identical result, or any second request for bytes that
    are already off-site, costs nothing. `UNIQUE (item_id, relpath,
    fingerprint)` is the whole mechanism."""
    conn, settings, _source, hash_a = queued
    rclone = install(monkeypatch, FakeRclone())
    run_backup_once(conn, settings)

    enqueue_backup_item(conn, settings, item_id=42, relpath=RELPATH, fingerprint=hash_a)
    run_backup_once(conn, settings)

    assert len(rows(conn)) == 1
    assert rclone.uploaded == [BYTES_A]
    assert len([c for c in rclone.commands if c[1] == "copy"]) == 1


def test_a_pending_request_is_superseded_by_a_commit_before_any_run(
    queued, monkeypatch
) -> None:
    """The other direction: `enqueue_backup_item` notices from the writing side
    what `_copy_and_verify` notices from the reading side, and both leave the
    queue in the same state."""
    conn, settings, source, hash_a = queued
    source.write_bytes(BYTES_B)
    hash_b = blake2b_file(source)

    enqueue_backup_item(conn, settings, item_id=42, relpath=RELPATH, fingerprint=hash_b)

    assert [(row["fingerprint"], row["state"]) for row in rows(conn)] == [(hash_b, "queued")]
    assert hash_a not in {row["fingerprint"] for row in rows(conn)}


def test_a_copy_already_in_flight_is_left_alone(queued, monkeypatch) -> None:
    """Nothing cancels it: it is one synchronous rclone inside the worker's own
    loop, with no manager and no process to signal. It no longer needs to be
    cancelled either — it is re-judged against its own fingerprint when it
    finishes, so it can only end in `done` if it copied what it said."""
    conn, settings, source, hash_a = queued
    conn.execute("UPDATE backup_queue SET state='copying'")
    source.write_bytes(BYTES_B)
    hash_b = blake2b_file(source)

    enqueue_backup_item(conn, settings, item_id=42, relpath=RELPATH, fingerprint=hash_b)

    assert {row["fingerprint"]: row["state"] for row in rows(conn)} == {
        hash_a: "copying",
        hash_b: "queued",
    }


def test_a_request_with_no_fingerprint_is_never_copied(queued, monkeypatch) -> None:
    """There is nothing to verify it against, so there is no honest way to
    complete it. Rows like this predate the identity and should stop, loudly."""
    conn, settings, _source, _hash_a = queued
    rclone = install(monkeypatch, FakeRclone())
    conn.execute("UPDATE backup_queue SET fingerprint=''")

    run_backup_once(conn, settings)

    assert rclone.commands == []
    assert rows(conn)[0]["state"] == "failed"


# --- what the queue looks like from outside ----------------------------------------


def test_a_clean_queue_reports_nothing(queued, monkeypatch) -> None:
    from librairy.backup import backup_queue_issues

    conn, settings, _source, _hash_a = queued
    install(monkeypatch, FakeRclone())
    run_backup_once(conn, settings)

    assert backup_queue_issues(conn) == []


def test_a_done_row_with_no_fingerprint_is_reported(queued) -> None:
    """It asserts that something was backed up without saying what, which is
    the one claim this table cannot be allowed to make quietly."""
    from librairy.backup import backup_queue_issues

    conn, _settings, _source, _hash_a = queued
    conn.execute("UPDATE backup_queue SET state='done', fingerprint=''")

    codes = {issue.code: issue.count for issue in backup_queue_issues(conn)}

    assert codes["done-without-fingerprint"] == 1


def test_a_stalled_copy_is_reported(queued) -> None:
    """`copying` is only ever written by a run in flight in the single worker
    process, and `_due_backups` never picks one up. A row that has sat there for
    hours is a worker that died holding it, and those bytes are now never
    retried until somebody looks."""
    from librairy.backup import backup_queue_issues

    conn, _settings, _source, _hash_a = queued
    conn.execute("UPDATE backup_queue SET state='copying', updated_at='2020-01-01T00:00:00+00:00'")

    codes = {issue.code: issue.count for issue in backup_queue_issues(conn)}

    assert codes["stalled-copy"] == 1


def test_a_copy_that_started_a_minute_ago_is_not_reported(queued) -> None:
    from datetime import UTC, datetime

    from librairy.backup import backup_queue_issues

    conn, _settings, _source, _hash_a = queued
    conn.execute(
        "UPDATE backup_queue SET state='copying', updated_at=?",
        (datetime.now(UTC).isoformat(),),
    )

    assert backup_queue_issues(conn) == []


def test_a_request_shadowed_by_newer_bytes_is_reported(queued) -> None:
    from librairy.backup import backup_queue_issues

    conn, _settings, source, _hash_a = queued
    source.write_bytes(BYTES_B)
    conn.execute("UPDATE items SET fingerprint=? WHERE id=42", (blake2b_file(source),))

    codes = {issue.code: issue.count for issue in backup_queue_issues(conn)}

    assert codes["shadowed-request"] == 1


def test_a_file_backed_up_only_under_older_bytes_is_reported(queued, monkeypatch) -> None:
    """The gap the queue cannot close by itself: a file changed outside a
    commit, so nothing enqueued the new bytes, and the only `done` row is for
    bytes that no longer exist. The backup is not wrong — it is just not of this
    file any more, and that is worth saying out loud."""
    from librairy.backup import backup_queue_issues

    conn, settings, source, _hash_a = queued
    install(monkeypatch, FakeRclone())
    run_backup_once(conn, settings)
    source.write_bytes(BYTES_B)
    conn.execute("UPDATE items SET fingerprint=? WHERE id=42", (blake2b_file(source),))

    codes = {issue.code: issue.count for issue in backup_queue_issues(conn)}

    assert codes["backed-up-under-older-bytes"] == 1
    assert "shadowed-request" not in codes, "nothing is queued, so nothing is shadowed"


def test_size_only_verification_is_reported_rather_than_assumed_good(
    queued, monkeypatch
) -> None:
    from librairy.backup import backup_queue_issues

    conn, settings, _source, _hash_a = queued
    install(monkeypatch, FakeRclone(check_output="1 hashes could not be checked"))
    run_backup_once(conn, settings)

    codes = {issue.code: issue.count for issue in backup_queue_issues(conn)}

    assert codes["verified-by-size-only"] == 1


def test_the_diagnostic_touches_no_file_and_no_remote(queued, monkeypatch) -> None:
    """Health is loaded to find out whether things are all right, not to spend
    a NAS's morning proving it."""
    import librairy.backup as backup_module
    from librairy.backup import backup_queue_issues

    conn, _settings, _source, _hash_a = queued

    def refuse(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("Health reached outside the index")

    monkeypatch.setattr(backup_module, "run", refuse)
    monkeypatch.setattr(backup_module, "blake2b_file", refuse)

    backup_queue_issues(conn)
