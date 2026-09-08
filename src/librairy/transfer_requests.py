"""Send this, once — and the policy it must never quietly become.

*Send to Offline Backup → WD-8TB* on a folder is a thing somebody asked for
once. `Category → Destination → Mode` is a standing instruction. They share
every dangerous piece of machinery and none of the intent, and keeping that
straight is the whole job of this module.

## What a send may not do

    create a policy                 pressing a button is not configuring one
    change an existing policy       nor is it editing one
    move or rename a Library file   nothing here touches the Library
    change the taxonomy             a send is not a filing decision
    remove anything, anywhere       there is still no verb for it

The way that stays true is structural rather than promised: nothing in this
module imports `destinations.set_policy`, and a request is its own row with its
own life. There is no code path from here into `backup_policies`, and
`tests/test_transfer_requests.py` reads the source for the absence.

## What it shares

Everything below the decision: `transfer_plan.Scope`, the comparison, the four
answers, `transfer_run.run_scope`, `tools/rclone.py`, and one history table.
Two transfer systems would be two places to get deletion wrong, and this way a
send inherits every refusal a scheduled backup has — path containment, drive
identity, the argv allowlist — without any of them being written twice.

## One row, whatever it covers

A folder of a hundred thousand photographs is a path and a flag. It is expanded
by an indexed query at the moment the worker reaches it, and never into a
hundred thousand rows, form fields or Python objects because somebody pressed a
button. The confirmation somebody sees is two aggregates over the same index.

## The gap between the click and the copy

A page shows the action because the drive was here when it rendered. By the
time the worker picks the request up, the drive may be in a bag. So presence
and identity are checked again immediately before anything moves — the same
check `transfer_run` makes for a scheduled offline backup — and a drive that
was pulled or swapped is a refusal that writes nothing into the directory the
mount point left behind.

See `docs/ROADMAP.md` M3-03.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

from librairy.config import Settings
from librairy.destinations import OFFLINE, Destination
from librairy.planner import utc_now
from librairy.transfer_paths import TransferRefused
from librairy.transfer_plan import MANUAL, Extent, Scope, extent

LOGGER = logging.getLogger(__name__)

#  A request's life. `requested` and nothing else means the worker has not
#  reached it; there is no `queued` distinct from `requested`, because there is
#  no queue — there is a row and a worker that looks for rows.
REQUESTED = "requested"
RUNNING = "running"
DONE = "done"
FAILED = "failed"

STATES = (REQUESTED, RUNNING, DONE, FAILED)

#  How many finished requests are kept. Somebody sending a folder to a drive
#  does it a handful of times a month; this is generous and still bounded.
KEEP = 200


@dataclass(frozen=True)
class Request:
    """One explicit send, and what became of it."""

    id: int
    destination_id: int
    relpath: str
    exact: bool
    state: str
    requested_at: str
    started_at: str = ""
    finished_at: str = ""
    outcome: str = ""
    detail: str = ""
    run_id: int = 0

    @property
    def pending(self) -> bool:
        return self.state in (REQUESTED, RUNNING)

    @property
    def scope(self) -> Scope:
        """What this covers, in the only vocabulary the planner has.

        `origin=MANUAL` rides along into history and is read by nothing that
        decides anything — a send and a schedule move bytes identically.
        """
        maker = Scope.file if self.exact else Scope.folder
        return maker(self.relpath, OFFLINE, MANUAL)


def ask(
    conn: sqlite3.Connection,
    *,
    destination_id: int,
    relpath: str,
    exact: bool,
) -> Request:
    """Record that somebody asked for this to be sent. Nothing moves yet.

    Deliberately dull, and deliberately *only* this: a row saying what was
    asked for. It does not plan, transfer, or configure anything, and there is
    nothing here that could write a policy.

    Pressing the button twice is one request. A unique index over the pending
    ones enforces it rather than a check that could race with itself.
    """
    path = str(relpath).strip().strip("/")
    if not path:
        raise TransferRefused("that is not a path in your library")
    existing = conn.execute(
        "SELECT * FROM transfer_requests WHERE destination_id=? AND relpath=?"
        " AND state=? ORDER BY id DESC LIMIT 1",
        (destination_id, path, REQUESTED),
    ).fetchone()
    if existing is not None:
        return _request(existing)
    cursor = conn.execute(
        """
        INSERT INTO transfer_requests(destination_id, relpath, exact, state,
                                      requested_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (destination_id, path, int(exact), REQUESTED, utc_now()),
    )
    asked = request(conn, int(cursor.lastrowid))
    prune(conn)
    return asked


def eligible(
    conn: sqlite3.Connection, relpath: str, *, exact: bool
) -> bool:
    """May this be sent at all?

    Authoritative Library content only. Something in the inbox with a proposed
    destination has a path and is not a Library file, and a send that copied
    one outward would be acting on a decision nobody has made yet.

    A folder is eligible when the index holds *any* live Library file beneath
    it — which is also what stops a Project or a tag view being mistaken for a
    place. Those are views over files that live elsewhere; a path that no
    Library file sits under is not a folder anybody can send.
    """
    scope = Scope.file(relpath, OFFLINE) if exact else Scope.folder(relpath, OFFLINE)
    return extent(conn, scope).any


def covers(conn: sqlite3.Connection, relpath: str, *, exact: bool) -> Extent:
    """`842 files · 11.6 GB`, for the confirmation. Two numbers, one statement.

    From the Library index and never from the destination: showing somebody
    what they are about to send must not wait on a drive being enumerated, and
    the count they need is the count of *their files*.
    """
    scope = Scope.file(relpath, OFFLINE) if exact else Scope.folder(relpath, OFFLINE)
    return extent(conn, scope)


def next_request(conn: sqlite3.Connection) -> Request | None:
    """The oldest thing anybody is waiting for. One at a time, on purpose."""
    row = conn.execute(
        "SELECT * FROM transfer_requests WHERE state=? ORDER BY id LIMIT 1",
        (REQUESTED,),
    ).fetchone()
    return _request(row) if row is not None else None


def request(conn: sqlite3.Connection, request_id: int) -> Request:
    row = conn.execute(
        "SELECT * FROM transfer_requests WHERE id=?", (request_id,)
    ).fetchone()
    if row is None:
        raise TransferRefused("that request is not there")
    return _request(row)


def recent(conn: sqlite3.Connection, destination_id: int = 0, limit: int = 20) -> list[Request]:
    sql = "SELECT * FROM transfer_requests"
    args: list[object] = []
    if destination_id:
        sql += " WHERE destination_id=?"
        args.append(destination_id)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(limit, KEEP)))
    return [_request(row) for row in conn.execute(sql, args)]  # noqa: S608


def send(
    conn: sqlite3.Connection,
    settings: Settings,
    found: Request,
    destination: Destination,
    *,
    runner=None,  # noqa: ANN001 - handed straight to `transfer_run`
) -> Request:
    """Do what was asked, through the machinery a scheduled backup uses.

    Presence and identity are checked here and again inside `transfer_run`,
    immediately before rclone is launched. Twice, because the page that offered
    the action rendered from a stored presence that may be minutes old, and the
    window between checking and copying is the one thing that cannot be closed
    — only made small and admitted to.
    """
    from librairy import offline_drives, transfer_listing, transfer_run

    _begin(conn, found.id)
    scope = found.scope
    here = offline_drives.look(conn, settings, destination)
    if not here.here:
        #  The drive left between the click and now. Nothing is written into
        #  the directory a mount point leaves behind.
        return _finish(
            conn, found.id, FAILED, transfer_run.UNAVAILABLE, here.sentence
        )
    listing = transfer_listing.listing(conn, settings, destination, scope)
    plan, result = transfer_run.run_scope(
        conn, settings, scope, destination, listing, runner=runner
    )
    del plan
    return _finish(
        conn,
        found.id,
        DONE if result.ok else FAILED,
        result.outcome,
        result.detail,
    )


def _begin(conn: sqlite3.Connection, request_id: int) -> None:
    conn.execute(
        "UPDATE transfer_requests SET state=?, started_at=? WHERE id=?",
        (RUNNING, utc_now(), request_id),
    )


def _finish(
    conn: sqlite3.Connection,
    request_id: int,
    state: str,
    outcome: str,
    detail: str,
) -> Request:
    conn.execute(
        "UPDATE transfer_requests SET state=?, finished_at=?, outcome=?, detail=?"
        " WHERE id=?",
        (state, utc_now(), outcome, detail[:400], request_id),
    )
    done = request(conn, request_id)
    #  Here rather than only at `ask`, because this is the moment a row becomes
    #  history. Pruning at the other end left the bound one behind for as long
    #  as nobody asked for anything else.
    prune(conn)
    return done


def prune(conn: sqlite3.Connection, keep: int = KEEP) -> int:
    cursor = conn.execute(
        """
        DELETE FROM transfer_requests WHERE state IN (?, ?) AND id NOT IN (
          SELECT id FROM transfer_requests WHERE state IN (?, ?)
          ORDER BY id DESC LIMIT ?
        )
        """,
        (DONE, FAILED, DONE, FAILED, keep),
    )
    return int(cursor.rowcount or 0)


def _request(row: sqlite3.Row) -> Request:
    return Request(
        id=int(row["id"]),
        destination_id=int(row["destination_id"]),
        relpath=str(row["relpath"]),
        exact=bool(row["exact"]),
        state=str(row["state"]),
        requested_at=str(row["requested_at"] or ""),
        started_at=str(row["started_at"] or ""),
        finished_at=str(row["finished_at"] or ""),
        outcome=str(row["outcome"] or ""),
        detail=str(row["detail"] or ""),
        run_id=int(row["run_id"] or 0),
    )
