from __future__ import annotations

import json
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from librairy import commit_state
from librairy.config import Settings
from librairy.humanize import human_bytes
from librairy.lifecycle import state_counts, vanished_count
from librairy.live import LIVE
from librairy.resources import modes_view
from librairy.web.charts import DEFAULT_RANGE, history


@dataclass(frozen=True)
class DiskStat:
    root: str
    free_gb: float
    total_gb: float
    percent_free: int
    # st_dev of the filesystem this root sits on. On a laptop all four roots are
    # usually one volume, and reporting it four times reads as four problems.
    device: int = 0
    #  Whether there is a directory there at all. False means every number above
    #  is zero because nothing was measured — not because the disk is empty.
    present: bool = True


@dataclass(frozen=True)
class Volume:
    """One physical filesystem, and which of our roots live on it."""

    roots: tuple[str, ...]
    free_gb: float
    total_gb: float
    percent_free: int

    @property
    def percent_used(self) -> int:
        return 100 - self.percent_free

    @property
    def label(self) -> str:
        return " + ".join(self.roots)

    @property
    def low(self) -> bool:
        return self.percent_free < 10


# The item states are the database's vocabulary, not a person's. "proposed"
# and "postponed" are both waiting for you and read as unrelated; "discovered"
# sounds like an achievement rather than a queue.
LIFECYCLE_LABELS = {
    "discovered": "found, not yet identified",
    "unstable": "still being written",
    "pending": "needs more information",
    "proposed": "waiting for your review",
    "postponed": "put off for later",
    "approved": "approved, ready to commit",
    "quarantine-proposed": "duplicate, waiting for a decision",
    "quarantined": "moved to quarantine",
    "committed": "filed in your library",
}
# Reading order is the order a file travels, so the column reads as a pipeline
# rather than as whatever sequence GROUP BY happened to return.
LIFECYCLE_ORDER = tuple(LIFECYCLE_LABELS)


def dashboard_data(
    conn: sqlite3.Connection, settings: Settings, *, days: int = 0
) -> dict[str, object]:
    worker_state = _worker_state(conn)
    counts = state_counts(conn)
    # The card is about work in progress, which means the inbox. Library items
    # rest in 'discovered' by design and are not a backlog.
    inbox_counts = state_counts(conn, "inbox")
    proposals = conn.execute(
        "SELECT COUNT(*) FROM proposals WHERE status='proposed'"
    ).fetchone()[0]
    backup = {
        row["state"]: row["count"]
        for row in conn.execute("SELECT state, COUNT(*) AS count FROM backup_queue GROUP BY state")
    }
    disks = _disk_stats(settings)
    return {
        #  Small on purpose. A dashboard that grows a backup administration
        #  console stops being a dashboard, and every number here has a page
        #  that says it better. See `librairy/transfer_status.py`.
        "backups": _backup_overview(conn, settings),
        #  Which Projects are asking for a person, and which are simply being
        #  used. Ranked in SQL and bounded to a handful — this is a pointer at
        #  the ones that matter, not a list of everything. See
        #  `librairy/project_status.py`.
        **_projects(conn),
        "worker_state": worker_state,
        "current_phase": worker_state.get("current_phase", "unknown"),
        "counts": counts,
        "lifecycle": lifecycle_rows(inbox_counts),
        "library_count": _count(
            conn, f"SELECT COUNT(*) FROM items WHERE root='library' AND {LIVE}"
        ),
        "proposal_count": proposals,
        "approved_count": _count(conn, "SELECT COUNT(*) FROM proposals WHERE status='approved'"),
        "recent_history": _recent_history(conn),
        "providers": _providers(conn),
        "disks": disks,
        "volumes": volumes(disks),
        "host_inbox_dir": settings.host_inbox_dir,
        "backup_counts": backup,
        # Filtered out of the inbox card's totals, so the number is short by
        # this much and nothing said why. One line, and only when there are
        # any — a nought here would be a card about nothing.
        "vanished_count": vanished_count(conn),
        #  How hard LibrAIry is currently allowed to work. Shown here so the
        #  answer to "why is nothing happening?" does not require opening
        #  Settings — and only when it is somewhere other than the defaults,
        #  because a line reading "Balanced · Normal" on every installation
        #  that never touched it is furniture. See `librairy/resources.py`.
        **modes_view(conn),
        # What is happening with your files, which is the question this page
        # answers. Health answers a different one — is LibrAIry itself well —
        # and the two were converging.
        **operations_overview(conn, settings),
        #  The third band, and the only part of this page that reads recorded
        #  history rather than the library. Everything above is a live
        #  aggregate and stays that way: a rollup that answered "what needs me
        #  now" would be a cache that can be wrong about the present.
        #  See `librairy/web/charts.py`.
        "history": history(conn, days or DEFAULT_RANGE),
    }


def operations_overview(
    conn: sqlite3.Connection, settings: Settings
) -> dict[str, object]:
    """Where the work is, what needs a person, and what just happened.

    Every number here is a SQL aggregate over an indexed column. Nothing in
    this function probes a file, calls a provider, asks a catalog or walks the
    library — a dashboard that costs a filesystem traversal is a dashboard
    people stop opening, and this one polls every five seconds.
    """
    from librairy.web.commit_queue import queue_summary

    queue = queue_summary(conn)
    findings = {
        row["status"]: row["count"]
        for row in conn.execute(
            "SELECT status, COUNT(*) AS count FROM audit_findings GROUP BY status"
        )
    }
    #  `i.missing_since IS NULL` throughout, and it is not decoration: emptying
    #  the delete queue is something LibrAIry asks people to do themselves, and
    #  without this the tile went on counting the files they had just deleted —
    #  and adding their sizes into a total describing disk that was no longer in
    #  use. Same predicate `live.py` owns, same one Quarantine's own views use.
    quarantine = conn.execute(
        """
        SELECT
          SUM(CASE WHEN present THEN 1 ELSE 0 END) AS held,
          SUM(CASE WHEN present AND queued THEN 1 ELSE 0 END) AS delete_queue,
          SUM(CASE WHEN NOT present THEN 1 ELSE 0 END) AS removed,
          COALESCE(SUM(CASE WHEN present THEN size ELSE 0 END), 0) AS bytes
        FROM (
          SELECT
            (qe.restored_at IS NULL AND i.id IS NOT NULL
             AND i.missing_since IS NULL) AS present,
            (i.relpath LIKE '_to-delete/%' ESCAPE '\\') AS queued,
            qe.restored_at IS NULL AS active,
            i.size AS size
          FROM quarantine_entries qe LEFT JOIN items i ON i.id = qe.item_id
        ) WHERE active
        """
    ).fetchone()
    library = conn.execute(
        "SELECT COUNT(*) AS files, COALESCE(SUM(size), 0) AS bytes FROM items"
        " WHERE root='library' AND missing_since IS NULL"
    ).fetchone()
    inbox_waiting = _count(
        conn, "SELECT COUNT(*) FROM proposals WHERE status='proposed'"
    )

    #  Asked once and answered for both bands below. `executing` is a plan
    #  status, not a running process: the count is what the row says, and
    #  `commit_state.unfinished` is what is actually true — see there for why
    #  the lock is the evidence. A library with no commit in flight pays for
    #  this count alone, which the activity band was already asking for.
    executing = _count(conn, "SELECT COUNT(*) FROM plans WHERE status='executing'")
    stopped = commit_state.unfinished(conn, settings) if executing else []
    running = executing - len(stopped)

    surfaces = [
        {"label": "Inbox", "count": inbox_waiting, "note": "waiting for review",
         "href": "/review"},
        {"label": "Library Review", "count": findings.get("open", 0),
         "note": f"{findings.get('kept', 0)} dismissed", "href": "/review#audit"},
        {"label": "Commit", "count": queue["decisions"], "note": queue["size"],
         "href": "/commit"},
        {"label": "Quarantine", "count": int(quarantine["held"] or 0),
         "note": human_bytes(int(quarantine["bytes"] or 0)), "href": "/quarantine"},
        {"label": "Library", "count": int(library["files"]),
         "note": human_bytes(int(library["bytes"])), "href": "/browse"},
    ]
    return {
        "surfaces": surfaces,
        "needs_attention": _needs_attention(
            conn, queue, findings, quarantine, stopped, away=_storage_away(conn, settings)
        ),
        "activity": _activity(conn, running),
        "recent": _recent(conn),
        "delete_queue_count": int(quarantine["delete_queue"] or 0),
    }


def _storage_away(conn: sqlite3.Connection, settings) -> list:  # noqa: ANN001
    """Roots whose storage is not the storage LibrAIry started against.

    A couple of `stat` calls per root and one row out of `worker_state`, on a
    page that polls every five seconds — and worth it: a Library that is not
    there is the first thing somebody needs to be told and the last thing any
    other query on this page would reveal. Reads only, and the one statement it
    costs is counted in `tests/test_dashboard_operations.py`.
    """
    from librairy import roots

    if settings is None:
        return []
    return [row for row in roots.state(conn, settings) if not row.recognised]


def _needs_attention(
    conn: sqlite3.Connection, queue, findings, quarantine, stopped=(), away=()  # noqa: ANN001
) -> list[dict[str, str]]:
    """Only things a person has to do something about.

    Deliberately not a status board. "Everything is fine" repeated in five
    cards teaches people to stop reading the one card that is not, so a healthy
    system produces an empty list here and the section does not render at all.
    """
    from librairy.web.quarantine import held_count

    items: list[dict[str, str]] = []
    #  First, above everything. Every other line here is about work waiting for
    #  a decision; this one is about the decisions being impossible to carry out,
    #  and reading "12 changes waiting for Commit" above it would be reading them
    #  in the wrong order.
    for root in away:
        #  The failure's own next step rather than a fixed clause: "reconnect
        #  it" is right for storage that is gone and wrong for storage that is
        #  present and is something else.
        items.append({
            "text": f"{root.detail} Nothing has been moved into it. "
                    f"{root.failure.next if root.failure else ''}".strip(),
            "href": "/health",
        })
    if queue["decisions"]:
        items.append({
            "text": f"{queue['decisions']} change"
                    f"{'' if queue['decisions'] == 1 else 's'} waiting for Commit",
            "href": "/commit",
        })
    #  The Quarantine page's own definition of Held, not a second one. This
    #  used to be "everything present, minus the delete queue", which counted
    #  files whose decision was already approved and waiting for Commit — so
    #  the same two files appeared under "5 changes waiting for Commit" and
    #  under "4 quarantined files with no decision yet", and the second claim
    #  was untrue of them.
    held = held_count(conn)
    if held:
        items.append({
            "text": f"{held} quarantined file{'' if held == 1 else 's'} "
                    "with no decision yet",
            "href": "/quarantine",
        })
    if findings.get("open"):
        items.append({
            "text": f"{findings['open']} library finding"
                    f"{'' if findings['open'] == 1 else 's'} to look at",
            "href": "/review#audit",
        })
    #  Files LibrAIry declined to guess at. They need somebody only when they
    #  are not going to resume on their own — a provider that is merely down
    #  puts them back by itself, and a line saying "3 files need you" about
    #  something that will fix itself in ten minutes is the kind of alert
    #  people learn to ignore. See `librairy/waiting.py`.
    stuck = waiting_for_you(conn)
    if stuck:
        items.append({
            "text": f"{stuck} file{'' if stuck == 1 else 's'} "
                    f"{'needs' if stuck == 1 else 'need'} more than LibrAIry "
                    "could work out",
            "href": "/review#review-waiting",
        })
    #  A commit whose process stopped. It is here rather than under "what
    #  LibrAIry is doing now" because it is not doing it: the run is gone, the
    #  files it had not reached are untouched, and committing again finishes the
    #  job. See `librairy/commit_state.py` for why the lock is the evidence.
    for interrupted in stopped:
        items.append({
            "text": f"A commit was interrupted — {interrupted.sentence}",
            "href": "/commit",
        })

    from librairy.search_health import recorded_health

    # Read, not checked: the dashboard polls every five seconds and rendering
    # must not write.
    if not recorded_health(conn).ok:
        items.append({
            "text": "Search index needs rebuild — results may be incomplete",
            "href": "/health",
        })
    return items


def waiting_for_you(conn: sqlite3.Connection) -> int:
    """Held files that will not resume by themselves.

    The distinction M2-01 built and this page has to respect: a file held
    because a provider is down is waiting on the provider, and one held because
    the evidence genuinely ran out is waiting on a person. Only the second is
    something to put in front of somebody.
    """
    from librairy.waiting import EVIDENCE

    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM processing_waits WHERE reason=? AND paused=0"
            " AND released_at IS NULL",
            (EVIDENCE,),
        ).fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row[0] or 0) if row else 0


def _held(conn: sqlite3.Connection) -> dict[str, int]:
    """How many files are held, and how many of those resume on their own."""
    try:
        rows = conn.execute(
            "SELECT reason, COUNT(*) AS n FROM processing_waits"
            " WHERE released_at IS NULL GROUP BY reason"
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {str(row["reason"]): int(row["n"]) for row in rows}


def _activity(conn: sqlite3.Connection, running: int = 0) -> list[dict[str, str]]:
    """What LibrAIry is doing now, from state it already keeps."""
    from librairy.audit_job import progress as audit_progress
    from librairy.waiting import RESUMABLE

    rows: list[dict[str, str]] = []
    #  Waiting on AI is a state of the *program*, not a queue of work — so it
    #  belongs here, beside what the worker is doing, and says which way it
    #  will resolve. Without it "idle" and "stuck behind a dead provider" look
    #  identical on this page, which is the question somebody opens it to ask.
    held = _held(conn)
    resuming = sum(count for reason, count in held.items() if reason in RESUMABLE)
    if resuming:
        rows.append({
            "what": "Waiting for AI",
            "detail": f"{resuming} file{'' if resuming == 1 else 's'} · resumes on its own",
        })
    audit = audit_progress(conn)
    if audit and audit.get("state") in {"running", "queued"}:
        done, total = audit.get("done", 0), audit.get("total", 0)
        rows.append({
            "what": "Library audit",
            "detail": f"{audit.get('phase') or 'working'} · {done}/{total}",
        })
    #  Counted by the caller, and deliberately not the number of plans that say
    #  `executing`: that status is written before the first file moves and
    #  rewritten when the run ends, so a killed process leaves it behind for
    #  ever, and this line said "1 running" about a process that had not existed
    #  for a week. An interrupted commit belongs under the things that need a
    #  person, not under what LibrAIry is doing now.
    if running > 0:
        rows.append({"what": "Commit", "detail": f"{running} running"})
    return rows


def _recent(conn: sqlite3.Connection) -> list[dict[str, str]]:
    """A few lines from the journal. Not a second History page.

    Grouped by what happened rather than listed file by file: "12 files filed"
    is the sentence, and History is one click away for the other 11 rows.
    """
    rows = conn.execute(
        """
        SELECT action, outcome, COUNT(*) AS count, MAX(ts) AS ts
        FROM history
        GROUP BY action, outcome
        ORDER BY ts DESC
        LIMIT 4
        """
    ).fetchall()
    said = {
        ("move", "ok"): "filed",
        ("move", "skipped_changed"): "not moved — changed since",
        ("move", "skipped_missing"): "not moved — missing",
        ("quarantine", "ok"): "moved to quarantine",
        ("undo", "ok"): "put back",
    }
    return [
        {
            "text": f"{row['count']} file{'' if row['count'] == 1 else 's'} "
                    f"{said.get((row['action'], row['outcome']), row['action'])}",
            "when": row["ts"],
        }
        for row in rows
    ]


def lifecycle_rows(counts: dict[str, int]) -> list[tuple[str, int]]:
    """Plain-language lifecycle counts, in the order a file travels.

    States with nothing in them are dropped: a column of zeroes is noise you
    have to read past to find the one number that moved.
    """
    rows = [
        (LIFECYCLE_LABELS[state], counts[state])
        for state in LIFECYCLE_ORDER
        if counts.get(state)
    ]
    # Anything the database grew that this map has not caught up with still
    # deserves to be shown, under its own name, rather than vanishing.
    rows.extend(
        (state, count) for state, count in sorted(counts.items())
        if count and state not in LIFECYCLE_LABELS
    )
    return rows


def volumes(disks: list[DiskStat]) -> list[Volume]:
    """One row per filesystem, not per configured root.

    On a single-disk box inbox, library, quarantine and appdata are all the
    same volume, and listing "8GB free" four times reads as four separate
    warnings about four separate disks.
    """
    grouped: dict[int, list[DiskStat]] = {}
    for disk in disks:
        grouped.setdefault(disk.device, []).append(disk)
    merged = [
        Volume(
            roots=tuple(disk.root for disk in group),
            free_gb=group[0].free_gb,
            total_gb=group[0].total_gb,
            percent_free=group[0].percent_free,
        )
        for group in grouped.values()
    ]
    # Fullest first: the one that will stop a commit is the one to show first.
    return sorted(merged, key=lambda volume: volume.percent_free)


def _count(conn: sqlite3.Connection, sql: str) -> int:
    row = conn.execute(sql).fetchone()
    return int(row[0]) if row else 0


def _worker_state(conn: sqlite3.Connection) -> dict[str, object]:
    state: dict[str, object] = {}
    for row in conn.execute("SELECT key, value FROM worker_state"):
        try:
            state[row["key"]] = json.loads(row["value"])
        except json.JSONDecodeError:
            state[row["key"]] = row["value"]
    return state


def _recent_history(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM history ORDER BY id DESC LIMIT 5"))


def _providers(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM provider_status ORDER BY name LIMIT 8"))


def _disk_stats(settings: Settings) -> list[DiskStat]:
    roots = {
        "inbox": settings.inbox_dir,
        "library": settings.library_dir,
        "quarantine": settings.quarantine_dir,
        "appdata": settings.appdata_dir,
    }
    stats: list[DiskStat] = []
    for name, path in roots.items():
        if not path.is_dir():
            #  Not the nearest existing parent's disk. `_existing_path` walks up
            #  until something exists, so a Library whose share had unmounted was
            #  reported as "library — 312GB free of 460GB": the container's own
            #  disk, wearing the Library's name, on the panel somebody checks to
            #  find out whether their storage is all right.
            stats.append(DiskStat(name, 0.0, 0.0, 0, 0, present=False))
            continue
        resolved = _existing_path(path)
        usage = shutil.disk_usage(resolved)
        free_gb = usage.free / 1024**3
        total_gb = usage.total / 1024**3
        percent_free = round((usage.free / usage.total) * 100) if usage.total else 0
        try:
            device = resolved.stat().st_dev
        except OSError:
            device = 0
        stats.append(
            DiskStat(name, round(free_gb, 1), round(total_gb, 1), percent_free, device)
        )
    return stats


def _existing_path(path: Path) -> Path:
    current = path
    while not current.exists() and current != current.parent:
        current = current.parent
    return current if current.exists() else Path("/")


def _backup_overview(conn: sqlite3.Connection, settings: Settings):  # noqa: ANN202
    """Counts for one small block, or nothing at all if there are no backups.

    Wrapped, because a dashboard that fails to render because a drive is odd
    would be a worse outcome than a dashboard with one fewer block on it.
    """
    from librairy import transfer_status

    try:
        return transfer_status.overview(conn, settings)
    except Exception:  # noqa: BLE001 - the page must render regardless
        return transfer_status.Overview()


def _projects(conn: sqlite3.Connection) -> dict[str, object]:
    """The Projects worth a glance, and how many there are altogether."""
    from librairy import project_status

    try:
        return {
            "project_cards": project_status.cards(conn),
            "project_total": project_status.counted(conn),
        }
    except Exception:  # noqa: BLE001 - the page must render regardless
        return {"project_cards": [], "project_total": 0}
