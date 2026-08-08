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
    if not settings.backup_enabled:
        return False
    if not should_back_up(conn, settings, item_id):
        return False
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO backup_queue(
          item_id, relpath, fingerprint, state, attempts, created_at, updated_at
        ) VALUES (?, ?, ?, 'queued', 0, ?, ?)
        """,
        (item_id, relpath, fingerprint, utc_now(), utc_now()),
    )
    return cursor.rowcount == 1


def run_backup_once(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    batch_size: int = 50,
) -> BackupRunSummary:
    status = backup_status(settings)
    if not status.available:
        return BackupRunSummary(paused=True, warning=status.detail)
    rows = conn.execute(
        """
        SELECT * FROM backup_queue
        WHERE state IN ('queued','failed') AND attempts < ?
        ORDER BY id
        LIMIT ?
        """,
        (MAX_BACKUP_ATTEMPTS, batch_size),
    ).fetchall()
    copied = failed = 0
    for row in rows:
        if _copy_and_verify(conn, settings, row):
            copied += 1
        else:
            failed += 1
    return BackupRunSummary(
        copied=copied,
        failed=failed,
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


def _copy_and_verify(conn: sqlite3.Connection, settings: Settings, row: sqlite3.Row) -> bool:
    source = validate_relpath(settings.library_dir, row["relpath"], kind="source")
    remote = _remote_path(settings.backup_remote, row["relpath"])
    conn.execute(
        "UPDATE backup_queue SET state='copying', updated_at=? WHERE id=?",
        (utc_now(), row["id"]),
    )
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
    if copy.returncode == 0 and check is not None and check.returncode == 0:
        conn.execute(
            """
            UPDATE backup_queue
            SET state='done', last_error=NULL, updated_at=?
            WHERE id=?
            """,
            (utc_now(), row["id"]),
        )
        return True
    error = _process_error(copy, check)
    attempts = int(row["attempts"]) + 1
    state = "failed"
    conn.execute(
        """
        UPDATE backup_queue
        SET state=?, attempts=?, last_error=?, updated_at=?
        WHERE id=?
        """,
        (state, attempts, error[:500], utc_now(), row["id"]),
    )
    return False


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
