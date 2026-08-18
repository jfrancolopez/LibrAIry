from __future__ import annotations

import json
import logging
import sqlite3
from configparser import ConfigParser
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

from librairy.config import Settings
from librairy.db import database_path
from librairy.fingerprint import blake2b_file
from librairy.live import LIVE, live
from librairy.paths import validate_relpath
from librairy.planner import utc_now
from librairy.taxonomy import CATEGORIES
from librairy.tools.rclone import RcloneStatus, check_command, copy_command, rclone_status, run

LOGGER = logging.getLogger(__name__)

MAX_BACKUP_ATTEMPTS = 3


@dataclass(frozen=True)
class BackupRunSummary:
    copied: int = 0
    failed: int = 0
    paused: bool = False
    warning: str = ""
    #  Whether the index went up with the files this time.
    snapshot: bool = False
    #  Requests that were discarded because the bytes they name are no longer
    #  at that path. Not failures: nothing went wrong with the backup, the
    #  request simply became impossible to satisfy honestly.
    superseded: int = 0


# The schedule was stored and never read: the worker drained the queue on
# every cycle whatever it said, so "daily@02:00" was decoration. These are the
# four shapes that answer a real question about a real connection.
SCHEDULES: dict[str, str] = {
    "after_commit": "As soon as there is something new",
    "hourly": "At most once an hour",
    "daily": "Once a day, at the time below",
    "manual": "Only when you press Back up now",
}
DEFAULT_SCHEDULE = "after_commit"
LAST_RUN_KEY = "backup.last_run_at"
RUN_REQUESTED_KEY = "backup.run_requested"


def backup_due(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> bool:
    """Whether this worker cycle should drain the backup queue.

    "Back up now" beats every schedule, including manual: it is the button
    that exists so a schedule you have set is never a thing you are stuck
    behind.
    """
    if not settings.backup_enabled:
        return False
    if _flag(conn, RUN_REQUESTED_KEY):
        return True
    schedule = settings.backup_schedule or DEFAULT_SCHEDULE
    if schedule == "after_commit":
        return True
    if schedule == "manual":
        return False
    moment = now or datetime.now(UTC)
    last = _last_run(conn)
    if schedule == "hourly":
        return last is None or (moment - last) >= timedelta(hours=1)
    if schedule == "daily":
        if last is not None and last.date() == moment.date():
            return False
        return moment.time() >= _daily_time(settings.backup_daily_at)
    # An unrecognised value must not silently mean "never back anything up".
    return True


def request_backup_now(conn: sqlite3.Connection) -> None:
    """Ask the worker to run on its next pass, rather than copying here.

    A batch can be gigabytes, and a web request is the wrong place to find
    that out. The worker picks this up within one cycle.
    """
    _set(conn, RUN_REQUESTED_KEY, "1")


def record_backup_run(conn: sqlite3.Connection, *, at: datetime | None = None) -> None:
    _set(conn, LAST_RUN_KEY, at.isoformat() if at else utc_now())
    _set(conn, RUN_REQUESTED_KEY, "")


def backup_run_pending(conn: sqlite3.Connection) -> bool:
    return _flag(conn, RUN_REQUESTED_KEY)


def last_backup_run(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (LAST_RUN_KEY,)).fetchone()
    return json.loads(row["value"]) if row else ""


def _last_run(conn: sqlite3.Connection) -> datetime | None:
    raw = last_backup_run(conn)
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _daily_time(value: str) -> time:
    try:
        hours, _, minutes = value.strip().partition(":")
        return time(int(hours), int(minutes or 0))
    except ValueError:
        return time(2, 0)


def _flag(conn: sqlite3.Connection, key: str) -> bool:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return bool(row and json.loads(row["value"]))


def _set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
        (key, json.dumps(value)),
    )


def rclone_config_path(settings: Settings) -> Path:
    return settings.appdata_dir / "rclone" / "rclone.conf"


def backup_status(settings: Settings) -> RcloneStatus:
    if not settings.backup_enabled:
        return RcloneStatus(False, "backup disabled")
    if not settings.backup_remote:
        return RcloneStatus(False, "backup remote not configured")
    return rclone_status(rclone_config_path(settings))


def configured_remotes(settings: Settings) -> list[str]:
    config_path = rclone_config_path(settings)
    if not config_path.exists():
        return []
    parser = ConfigParser()
    parser.read(config_path)
    return [f"{section}:" for section in parser.sections()]


@dataclass(frozen=True)
class CategorySize:
    """What one category would cost to back up."""

    category: str
    files: int
    bytes: int
    selected: bool

    @property
    def size_label(self) -> str:
        value = float(self.bytes)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if value < 1024 or unit == "TB":
                return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
            value /= 1024
        return ""


def selected_categories(settings: Settings) -> set[str]:
    """Categories to back up. Empty configuration means all of them.

    Empty has to mean "everything" rather than "nothing": it is the default,
    and a default that silently backs up nothing is the worst possible answer
    to "is my library safe".
    """
    chosen = {part.strip() for part in settings.backup_categories.split(",") if part.strip()}
    return chosen or set(CATEGORIES)


def category_sizes(conn: sqlite3.Connection, settings: Settings) -> list[CategorySize]:
    """Per-category file count and bytes in the library, for the picker.

    Counting from the index rather than the filesystem: this renders on every
    Settings page load, and walking a NAS-backed library to draw a form is not
    a trade anyone would make.
    """
    chosen = selected_categories(settings)
    rows = {
        row["category"]: (row["files"], row["bytes"] or 0)
        for row in conn.execute(
            """
            SELECT s.category, COUNT(*) AS files, SUM(i.size) AS bytes
            FROM search_fts s JOIN items i ON i.id = s.item_id
            WHERE i.root = 'library' AND i.missing_since IS NULL
            GROUP BY s.category
            """
        )
    }
    return [
        CategorySize(
            category=category,
            files=rows.get(category, (0, 0))[0],
            bytes=rows.get(category, (0, 0))[1],
            selected=category in chosen,
        )
        for category in CATEGORIES
    ]


def item_category(conn: sqlite3.Connection, item_id: int) -> str:
    row = conn.execute(
        """
        SELECT category FROM proposals
        WHERE item_id=? AND status != 'superseded'
        ORDER BY id DESC LIMIT 1
        """,
        (item_id,),
    ).fetchone()
    return str(row["category"]) if row else ""


def should_back_up(conn: sqlite3.Connection, settings: Settings, item_id: int) -> bool:
    """Whether this file is in the selected set.

    Unconfigured means everything, and everything includes files with no
    category at all — the default must never quietly leave something out of a
    backup you believe is complete. Once a deliberate subset is chosen, an
    uncategorised file is not in it, because nobody asked for it and off-site
    storage is metered.
    """
    if not settings.backup_categories.strip():
        return True
    return item_category(conn, item_id) in selected_categories(settings)


def enqueue_backup_item(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    item_id: int,
    relpath: str,
    fingerprint: str,
) -> bool:
    """Ask for these bytes, at this path, to be copied off-site.

    Backup identity is `UNIQUE (item_id, relpath, fingerprint)`, so it is
    genuinely fingerprint-aware and `INSERT OR IGNORE` does the right thing in
    both directions: the same bytes at the same path are already requested (or
    already done) and nothing is added, while *different* bytes are a new
    request even though the item and the path are unchanged.

    That matters because one item's bytes really do change under a stable id:
    adopting an optimized version, undoing it, re-running the encode and
    adopting again reuses the same `items` row by design.

    What `INSERT OR IGNORE` alone does not handle is the row left behind. A
    still-pending request for the *previous* fingerprint at this same path is a
    request to copy bytes that are no longer there — and `_copy_and_verify`
    checks the source against the remote, not against the recorded hash, so it
    would happily upload the new file and mark the old fingerprint `done`. That
    is a backup record asserting something untrue, which is worse than a
    failure. Those rows are discarded.

    `done` is left alone because it is a fact: those bytes are on the remote,
    and they still are whatever happens at that path afterwards.

    `copying` is left alone for a different reason. It is one synchronous
    `rclone copy` inside the worker's own loop — there is no process to cancel
    and no manager that owns it — and interfering with a transfer that is
    already running to make a bookkeeping row tidier trades a real risk for a
    cosmetic one. It is also no longer necessary: `_copy_and_verify` re-hashes
    the source against this row's own fingerprint after the copy, so a request
    whose file changed mid-flight can no longer end in `done`. The copy is
    allowed to finish and is then judged honestly.
    """
    if not settings.backup_enabled:
        return False
    if not should_back_up(conn, settings, item_id):
        return False
    conn.execute(
        """
        DELETE FROM backup_queue
        WHERE item_id=? AND relpath=? AND fingerprint != ?
          AND state IN ('queued','failed')
        """,
        (item_id, relpath, fingerprint),
    )
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO backup_queue(
          item_id, relpath, fingerprint, state, attempts, created_at, updated_at
        ) VALUES (?, ?, ?, 'queued', 0, ?, ?)
        """,
        (item_id, relpath, fingerprint, utc_now(), utc_now()),
    )
    return cursor.rowcount == 1


def _due_backups(conn: sqlite3.Connection, *, batch_size: int) -> list[sqlite3.Row]:
    """The queue rows a run would actually hand to rclone.

    Joined against `items` because a queued row is a request to copy a file,
    and a file that is not there cannot be copied. Without the join the run
    burns an rclone invocation per poll on a source that does not exist, fails,
    increments `attempts`, and eventually exhausts a perfectly good backup
    request — for an unmounted share, or for an optimized copy that was
    adopted, queued, and then un-adopted, whose bytes are back in the job's
    staging directory.

    The row is left alone rather than deleted. If the share comes back, or the
    optimized version is adopted again, the next scan clears `missing_since`
    and this picks the request up where it was.
    """
    return list(
        conn.execute(
            f"""
            SELECT q.* FROM backup_queue q JOIN items i ON i.id = q.item_id
            WHERE q.state IN ('queued','failed') AND q.attempts < ? AND {live()}
            ORDER BY q.id
            LIMIT ?
            """,  # noqa: S608 - a module constant
            (MAX_BACKUP_ATTEMPTS, batch_size),
        )
    )


def run_backup_once(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    batch_size: int = 50,
) -> BackupRunSummary:
    status = backup_status(settings)
    if not status.available:
        return BackupRunSummary(paused=True, warning=status.detail)
    rows = _due_backups(conn, batch_size=batch_size)
    outcomes = [_copy_and_verify(conn, settings, row) for row in rows]
    copied = outcomes.count(COPIED)
    return BackupRunSummary(
        copied=copied,
        failed=outcomes.count(FAILED),
        superseded=outcomes.count(SUPERSEDED),
        snapshot=_copy_snapshot(conn, settings) if copied else False,
    )


#  Where the index lands on the remote. Leading underscore so it sorts away
#  from the library's own folders, the same convention quarantine's delete pile
#  uses.
SNAPSHOT_REMOTE_RELPATH = "_librairy/librairy.db"


def _copy_snapshot(conn: sqlite3.Connection, settings: Settings) -> bool:
    """Send a consistent copy of the index up with the files. Never raises.

    `snapshot_database` was written, tested, given a default-on setting, a
    documented environment variable and a checkbox reading "Include SQLite
    snapshot" — and never called by anything. So every backup ever taken
    contained the files and not the index: restore onto a new machine and you
    have your library and no history, no undo journal, no quarantine records
    and no record of what came from where. The one thing a backup is for is
    the case where the original is gone.

    Only when files actually moved. The worker polls on a timer, and
    re-uploading the database every poll to say nothing changed is how you
    make somebody's metered connection hate this feature.
    """
    if not settings.backup_include_db_snapshot:
        return False
    staging = settings.appdata_dir / "backup" / "librairy.db"
    try:
        snapshot_database(settings, staging)
        remote = _remote_path(settings.backup_remote, SNAPSHOT_REMOTE_RELPATH)
        copy = run(
            copy_command(
                rclone_config_path(settings),
                staging,
                remote,
                settings.backup_bandwidth_limit,
            )
        )
    except (OSError, sqlite3.Error) as exc:
        LOGGER.warning("index snapshot failed: %s", exc)
        return False
    finally:
        staging.unlink(missing_ok=True)
    if copy.returncode != 0:
        LOGGER.warning("index snapshot upload failed: %s", copy.stderr.strip()[:200])
        return False
    return True


#  What one run did with one request. Three outcomes, not two: a request that
#  can no longer be satisfied is not a failure of the backup — nothing went
#  wrong with the copy, the bytes it names simply are not at that path any
#  more.
COPIED = "copied"
FAILED = "failed"
SUPERSEDED = "superseded"

#  rclone says so itself when it could not compare hashes, which is the only
#  honest source for whether a `done` row rests on a checksum or on a size.
HASHES_UNCHECKED = "hashes could not be checked"


def _copy_and_verify(conn: sqlite3.Connection, settings: Settings, row: sqlite3.Row) -> str:
    """Copy these exact bytes off-site, or do not mark this request done.

    A queue row records a `fingerprint`, and marking it `done` is LibrAIry
    asserting that *those bytes* are on the remote. That is a much stronger
    claim than "the file at this path is on the remote", and until now the code
    only ever established the weaker one: it ran `rclone check <source>
    <remote>` after the copy and never read `row["fingerprint"]` at all.

        row says fingerprint A
        copy starts
        the file at that path becomes B
        the copy sends B; check compares B against B and they agree
        the A row is marked done

    The database then asserts that A was backed up when A was never sent
    anywhere. A failed backup is visible and recoverable; a backup record that
    is quietly untrue is neither, and this is the one table whose whole purpose
    is to be believed when the original is gone.

    So the expectation comes from the request and never from the live file:

        1. hash the source and require it to equal `row["fingerprint"]`
           — the bytes about to be sent are the bytes that were asked for
        2. copy
        3. `rclone check` — the remote's content matches that local file
        4. hash the source again and require it to equal `row["fingerprint"]`
           — the file check compared is still the file step 1 approved, so
           nothing swapped underneath the window

    Steps 1 and 4 are the new ones, and step 4 is what closes the race: it is
    entirely possible for the copy to succeed, the check to pass, and the bytes
    involved to be the wrong ones.

    Together they give: the remote holds the bytes recorded on this row, to the
    strength of what `rclone check` can actually compare — see
    `_verification_kind`, which records which of the two it was rather than
    assuming the better one.
    """
    expected = str(row["fingerprint"])
    source = validate_relpath(settings.library_dir, row["relpath"], kind="source")
    if not expected:
        _fail(conn, row, "this backup request records no fingerprint to verify against")
        return FAILED

    before = _read_fingerprint(source)
    if before is None:
        #  Retryable, and deliberately so: an unreadable source is usually an
        #  unmounted share, not a wrong request.
        _fail(conn, row, f"source could not be read: {row['relpath']}")
        return FAILED
    if before != expected:
        _discard(conn, settings, row, before, "the file changed before the copy began")
        return SUPERSEDED

    conn.execute(
        "UPDATE backup_queue SET state='copying', updated_at=? WHERE id=?",
        (utc_now(), row["id"]),
    )
    remote = _remote_path(settings.backup_remote, row["relpath"])
    copy = run(
        copy_command(
            rclone_config_path(settings),
            source,
            remote,
            settings.backup_bandwidth_limit,
        )
    )
    check = None
    if copy.returncode == 0:
        check = run(check_command(rclone_config_path(settings), source, remote))
        #  Only on this path, because it is the only path that could end in
        #  `done`. A copy that already failed needs no second opinion, and
        #  re-reading the whole file to produce one costs real I/O on a
        #  NAS-backed library.
        after = _read_fingerprint(source)
        if after is None:
            _fail(conn, row, "the source disappeared or stopped being readable during the copy")
            return FAILED
        if after != expected:
            #  Whatever reached the remote, it cannot be proven to be the bytes
            #  this row names — and it very likely is not.
            _discard(conn, settings, row, after, "the file changed while the copy was running")
            return SUPERSEDED

    if copy.returncode == 0 and check is not None and check.returncode == 0:
        conn.execute(
            """
            UPDATE backup_queue
            SET state='done', verified=?, last_error=NULL, updated_at=?
            WHERE id=?
            """,
            (_verification_kind(check), utc_now(), row["id"]),
        )
        return COPIED
    _fail(conn, row, _process_error(copy, check))
    return FAILED


def _read_fingerprint(source: Path) -> str | None:
    """The hash of what is at that path right now, or None if that is unknowable."""
    try:
        return blake2b_file(source)
    except OSError:
        return None


def _fail(conn: sqlite3.Connection, row: sqlite3.Row, error: str) -> None:
    conn.execute(
        """
        UPDATE backup_queue
        SET state='failed', attempts=?, last_error=?, updated_at=?
        WHERE id=?
        """,
        (int(row["attempts"]) + 1, error[:500], utc_now(), row["id"]),
    )


def _discard(
    conn: sqlite3.Connection,
    settings: Settings,
    row: sqlite3.Row,
    actual: str,
    reason: str,
) -> None:
    """Drop a request whose bytes are no longer at its path, and ask for what is.

    Not `failed`: failure means try again, and there is nothing here that
    retrying could ever fix — the file this row describes is gone from this
    path, so no number of attempts will put those bytes on the remote. Leaving
    it to burn its three attempts would also mean three more uploads of the
    *wrong* file before the queue gave up.

    Deleting it is the same thing `enqueue_backup_item` already does when it
    notices the same condition from the other side, so a request superseded by
    a commit and a request superseded by the disk end up in the same state.
    """
    conn.execute("DELETE FROM backup_queue WHERE id=?", (row["id"],))
    LOGGER.warning(
        "backup request for %s (%s) discarded: %s", row["relpath"], row["fingerprint"][:12], reason
    )
    _requeue_current_bytes(conn, settings, row, actual)


def _requeue_current_bytes(
    conn: sqlite3.Connection,
    settings: Settings,
    row: sqlite3.Row,
    fingerprint: str,
) -> None:
    """Ask for whatever is at that path now — but only where nothing is a guess.

    The discarded request's `item_id` cannot be reused as the owner of these
    bytes. A path changes hands: adoption puts the optimized version at the
    original's own path under a *different* item id, so attributing the new
    bytes to the old id would file a backup against the wrong item and make the
    record wrong in a second way while fixing the first.

    `UNIQUE (root, relpath)` means at most one live row can claim that path. If
    that row's fingerprint is the hash just measured, then the index and the
    disk agree about both the owner and the bytes and there is nothing left to
    infer. If they disagree the scanner has not caught up yet, and waiting for
    it is more honest than inventing a request; `backup_queue_issues` reports
    the gap in the meantime.
    """
    owner = conn.execute(
        f"SELECT id, fingerprint FROM items WHERE root='library' AND relpath=? AND {LIVE}",  # noqa: S608
        (row["relpath"],),
    ).fetchone()
    if owner is None or str(owner["fingerprint"]) != fingerprint:
        return
    enqueue_backup_item(
        conn,
        settings,
        item_id=int(owner["id"]),
        relpath=str(row["relpath"]),
        fingerprint=fingerprint,
    )


def _verification_kind(check) -> str:  # noqa: ANN001 - CompletedProcess[str]
    """Which comparison the remote was actually able to make.

    LibrAIry's own fingerprint is blake2b, and no rclone backend offers blake2b,
    so `backup_queue.fingerprint` can never be compared against a remote hash
    directly — that link is closed locally instead, by hashing the source on
    both sides of the copy. What `rclone check` adds is remote-vs-local, and how
    strong *that* is depends on the backend: hashes when both sides can produce
    a common one, size when they cannot.

    rclone reports the difference itself rather than making the caller guess, so
    the answer is read from its output instead of being assumed to be the better
    of the two.
    """
    output = f"{check.stdout or ''}\n{check.stderr or ''}".lower()
    return "size" if HASHES_UNCHECKED in output else "hash"


def snapshot_database(settings: Settings, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(database_path(settings))
    target = sqlite3.connect(destination)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    return destination


def _remote_path(remote: str, relpath: str) -> str:
    return f"{remote.rstrip('/')}/{relpath}"


def _process_error(copy, check) -> str:  # noqa: ANN001
    failed = check if check is not None and check.returncode != 0 else copy
    return failed.stderr.strip() or failed.stdout.strip() or f"rclone exited {failed.returncode}"


#  A `copying` row is only ever written by a run that is in flight, in the one
#  worker process, and `_due_backups` never picks one up. So a row that has sat
#  in `copying` for hours is not a slow copy, it is a worker that died holding
#  one — and those bytes will never be retried until somebody looks.
STALLED_COPY_HOURS = 6


@dataclass(frozen=True)
class BackupIssue:
    """One suspicious thing about the queue, in terms a person can act on."""

    code: str
    count: int
    detail: str


def backup_queue_issues(conn: sqlite3.Connection) -> list[BackupIssue]:
    """What is wrong, or looks wrong, about what the queue claims.

    Read-only and index-only. Health is a page somebody loads, so nothing here
    hashes a file, reaches a remote, or walks a directory; every question is
    answered by SQL against rows that already exist.

    Deliberately not a repair. Two of these have no single correct fix — which
    of a stalled copy's bytes reached the remote is not knowable from here — and
    a page that quietly rewrites rows as a side effect of being read destroys
    the evidence of the thing that made them wrong.
    """
    cutoff = (datetime.now(UTC) - timedelta(hours=STALLED_COPY_HOURS)).isoformat()
    issues = [
        BackupIssue(
            "done-without-fingerprint",
            _count(
                conn,
                "SELECT COUNT(*) FROM backup_queue WHERE state='done' AND fingerprint=''",
            ),
            "backed up, with no record of which bytes",
        ),
        BackupIssue(
            "stalled-copy",
            _count(
                conn,
                "SELECT COUNT(*) FROM backup_queue WHERE state='copying' AND updated_at < ?",
                (cutoff,),
            ),
            f"copying for more than {STALLED_COPY_HOURS} hours, so not retried",
        ),
        BackupIssue(
            "shadowed-request",
            _count(
                conn,
                f"""
                SELECT COUNT(*) FROM backup_queue q JOIN items i ON i.id = q.item_id
                WHERE q.state IN ('queued','failed') AND i.relpath = q.relpath
                  AND i.fingerprint != q.fingerprint AND {live()}
                """,  # noqa: S608 - a module constant
            ),
            "waiting to copy bytes that are no longer at that path",
        ),
        BackupIssue(
            "backed-up-under-older-bytes",
            _count(
                conn,
                f"""
                SELECT COUNT(*) FROM items i
                WHERE i.root = 'library' AND {live()}
                  AND EXISTS (
                    SELECT 1 FROM backup_queue q
                    WHERE q.item_id = i.id AND q.relpath = i.relpath
                      AND q.state = 'done' AND q.fingerprint != i.fingerprint)
                  AND NOT EXISTS (
                    SELECT 1 FROM backup_queue q
                    WHERE q.item_id = i.id AND q.relpath = i.relpath
                      AND q.fingerprint = i.fingerprint)
                """,  # noqa: S608 - a module constant
            ),
            "changed since their backup, with nothing queued for the new bytes",
        ),
        BackupIssue(
            "verified-by-size-only",
            _count(
                conn,
                "SELECT COUNT(*) FROM backup_queue WHERE state='done' AND verified='size'",
            ),
            "compared by size, because this remote cannot produce a hash",
        ),
    ]
    return [issue for issue in issues if issue.count]


def _count(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    return int(conn.execute(sql, params).fetchone()[0])

