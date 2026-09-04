"""A backup drive that lives in a drawer: registering it, and noticing it.

An Offline Backup is the same outward transfer as every other destination, with
one thing in front of it — the drive is usually **not there**, and that is not
a failure. It is where a backup drive is supposed to be.

Almost everything in this module follows from taking that sentence literally.

## Absent is not an error

A drive in a drawer produces no failed run, no alert, no retry, and no red
anything. `backup_runs` never opens a row for it, because nothing was
attempted; Health does not call it overdue; the Browse action is not rendered
at all rather than rendered and disabled. The only thing an absent drive
produces is a date — *last seen 12 June* — which is genuinely useful and is the
whole reason presence is stored rather than probed.

## Three states, because two would lie

    absent        nothing there, or a mount point with nothing of ours in it
    present       there, and it is the drive that was registered
    wrong drive   something is there, and it is **not** that drive

The third is the one that has to exist separately. Collapsing it into *absent*
would tell somebody their drive is unplugged while a drive is plugged in, and
collapsing it into *present* is how a backup gets written onto a stranger's
disk. It is a refusal, and it says so.

The distinction between the first two is deliberate and specific: a directory
at the mount point **with no marker at all** is a leftover mount point, which
is what an unplugged USB disk usually leaves behind — that is *absent*. A
directory with a marker that says something else, or on a filesystem whose id
disagrees, is a different drive — that is *wrong drive*.

## Two tiers of looking, because one of them costs a subprocess

    probe    `is_dir()` and a marker file. Two stats and a short read
    look     the above, plus asking the operating system what filesystem this
             is — which on macOS is `diskutil`, a subprocess

The cheap one runs on a poll; the expensive one runs when the cheap one says
something changed, and again immediately before any transfer. A drive that
appears is noticed within `POLL_SECONDS`; a drive that sits in a drawer for
three months costs two stats every half minute and nothing else.

**The full check is never skipped where it matters.** `transfer_paths.
checked_offline` runs it again just before rclone is launched, because a drive
can be pulled between being noticed and being written to, and the mount point
it leaves behind is a directory that will happily accept files.

## What this module cannot do

It does not transfer, compare, or remove anything. It answers *is the drive
here* and *is it the right one*, and hands both answers to the machinery that
already exists. What happens when a drive appears is one `run_policy` per
policy — the same function an online destination uses, with the same plan, the
same adapter, and the same four answers, none of which is a deletion.

See `docs/ROADMAP.md` M3-03.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

from librairy import destinations as destinations_module
from librairy import transfer_paths, volumes
from librairy.config import Settings
from librairy.destinations import LOCAL, OFFLINE, Destination
from librairy.planner import utc_now
from librairy.transfer_paths import (
    FULLY_VERIFIED,
    MARKER_ONLY,
    UNVERIFIED,
    TransferRefused,
)

LOGGER = logging.getLogger(__name__)

#  Where a registered drive is.
ABSENT = "absent"
PRESENT = "present"
WRONG_DRIVE = "wrong-drive"

STATES = (ABSENT, PRESENT, WRONG_DRIVE)

#  What each state is called where somebody reads it. "Not connected" and not
#  "unavailable" or "missing": a drive in a drawer has not gone wrong.
STATE_LABEL = {
    ABSENT: "Not connected",
    PRESENT: "Connected",
    WRONG_DRIVE: "A different drive is at that path",
}

#  How much of the identity check ran, in words. The middle one is why this is
#  shown at all — it is a real reduction in checking and must not look like a
#  full check.
VERIFICATION_LABEL = {
    FULLY_VERIFIED: "Identity confirmed",
    MARKER_ONLY: "Identified by its marker file only",
    UNVERIFIED: "Not identified",
}

#  How often the cheap probe runs. A drive plugged in is noticed within half a
#  minute, which is far faster than anybody needs, and a drive that has been in
#  a drawer since March costs two stats a minute rather than two a cycle.
POLL_SECONDS = 30

#  What goes in the marker file. Readable on purpose: somebody who plugs this
#  drive into another machine can open it and see what wrote it.
_PREFIX = "librairy"


@dataclass(frozen=True)
class Presence:
    """Where a registered drive is, and how well that is known."""

    destination_id: int
    state: str = ABSENT
    verification: str = UNVERIFIED
    detail: str = ""
    checked_at: str = ""
    #  The last time it was actually here. The half of the answer that cannot
    #  be probed once the drive is gone, and usually the half worth showing.
    present_at: str = ""

    @property
    def here(self) -> bool:
        return self.state == PRESENT

    @property
    def refused(self) -> bool:
        """Something is at that path and it is not the registered drive."""
        return self.state == WRONG_DRIVE

    @property
    def label(self) -> str:
        return STATE_LABEL.get(self.state, STATE_LABEL[ABSENT])

    @property
    def sentence(self) -> str:
        """One line, in the words `docs/ui-vocabulary.md` pins."""
        if self.state == WRONG_DRIVE:
            return self.detail or STATE_LABEL[WRONG_DRIVE]
        if self.here:
            note = VERIFICATION_LABEL.get(self.verification, "")
            return f"{STATE_LABEL[PRESENT]} — {note}" if note else STATE_LABEL[PRESENT]
        if self.present_at:
            return f"{STATE_LABEL[ABSENT]} — last seen {self.present_at[:10]}"
        return STATE_LABEL[ABSENT]


# --- registration --------------------------------------------------------------------


def register(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    name: str,
    path: str,
    replace: bool = False,
) -> Destination:
    """Make a connected drive into a destination LibrAIry can recognise later.

    Both halves of the identity are established here and only here: the marker
    file is written, and whatever the operating system calls this filesystem is
    read and stored beside it. Doing it at registration rather than at transfer
    time is the point — afterwards there is something to *compare against*, and
    a comparison is what makes plugging in the wrong disk a refusal instead of
    a surprise.

    The drive has to be connected, because a marker cannot be written to a
    drawer. Every path refusal `local_destination` makes applies first: a
    destination inside the Library, containing it, or in any other managed
    folder is refused before anything is written anywhere.
    """
    target = transfer_paths.local_destination(settings, path)
    if not target.path.is_dir():
        raise TransferRefused(
            "that drive is not connected — plug it in, then register it"
        )
    existing = transfer_paths.identify(target.path)
    if existing and not replace:
        known = _by_identity(conn, existing)
        where = f" as {known.name}" if known else ""
        raise TransferRefused(
            f"that drive is already registered{where}; nothing was changed"
        )
    if _registered_at(conn, target.path):
        raise TransferRefused("that path is already a destination")

    identity = f"{_PREFIX}:{uuid.uuid4().hex}"
    #  The marker first, then the row. A marker with no row is a stray file
    #  somebody can delete; a row with no marker is a destination that can
    #  never be recognised, and the failure would only appear at backup time.
    transfer_paths.register(target.path, identity)
    volume = volumes.identity_for(target.path)
    destination_id = destinations_module.add_destination(
        conn,
        name=name,
        kind=LOCAL,
        target=str(target.path),
        modes=[OFFLINE],
        identity=identity,
        volume=volume,
    )
    look(conn, settings, _destination(conn, destination_id))
    return _destination(conn, destination_id)


def forget(conn: sqlite3.Connection, destination_id: int) -> None:
    """Stop treating a drive as a destination. **Nothing on it is touched.**

    Not even the marker file, which is on the drive and therefore not ours to
    remove — and which somebody may want in order to re-register it. Forgetting
    a destination forgets what LibrAIry knows, and a backup drive full of
    somebody's photographs is not what LibrAIry knows.
    """
    conn.execute(
        "DELETE FROM offline_presence WHERE destination_id=?", (destination_id,)
    )
    destinations_module.remove_destination(conn, destination_id)


# --- presence ------------------------------------------------------------------------


def probe(settings: Settings, destination: Destination) -> str:
    """The cheap question: which state does this *look* like?

    Two stats and a short read, and no subprocess, so it can run on a poll. It
    can say `absent` and `wrong-drive` on its own; the `present` it returns is
    provisional and gets confirmed by `look`, which is the one that asks the
    operating system what filesystem this actually is.
    """
    try:
        target = transfer_paths.local_destination(settings, destination.target).path
    except TransferRefused:
        return ABSENT
    if not target.is_dir():
        return ABSENT
    found = transfer_paths.identify(target)
    if not found:
        #  A directory with nothing of ours in it. This is what an unplugged
        #  USB disk leaves behind, and calling it a wrong drive would turn the
        #  ordinary case into an alarm.
        return ABSENT
    return PRESENT if found == destination.identity else WRONG_DRIVE


def look(
    conn: sqlite3.Connection, settings: Settings, destination: Destination
) -> Presence:
    """The whole question, asked and written down.

    Everything `probe` does, plus the half that costs a subprocess: is this the
    same *filesystem* it was registered on. A marker can be copied — clone a
    backup drive and the clone claims to be the original — and that is the only
    thing the volume id catches.
    """
    state = probe(settings, destination)
    verification = UNVERIFIED
    detail = ""
    if state == PRESENT:
        target = transfer_paths.local_destination(settings, destination.target).path
        here = volumes.identity_for(target)
        if not volumes.matches(destination.volume, here):
            state = WRONG_DRIVE
            detail = (
                "the drive at that path carries our marker but is a different"
                " filesystem — it is a copy, not the drive that was registered"
            )
        else:
            verification = transfer_paths.verification(
                destination.identity, destination.volume, here
            )
    elif state == WRONG_DRIVE:
        detail = "a different drive is mounted at that path"
    return _remember(conn, destination.id, state, verification, detail)


def presence(conn: sqlite3.Connection, destination_id: int) -> Presence:
    """What was last known about this drive, without going and looking.

    A page render reads this. Rendering must never wait on a stat of something
    that may be an unresponsive mount, and it must never run `diskutil`.
    """
    row = conn.execute(
        "SELECT * FROM offline_presence WHERE destination_id=?", (destination_id,)
    ).fetchone()
    if row is None:
        return Presence(destination_id=destination_id)
    return Presence(
        destination_id=destination_id,
        state=str(row["state"]),
        verification=str(row["verification"] or UNVERIFIED),
        detail=str(row["detail"] or ""),
        checked_at=str(row["checked_at"] or ""),
        present_at=str(row["present_at"] or ""),
    )


def attached(conn: sqlite3.Connection) -> list[Destination]:
    """Every registered drive that was here the last time anybody looked.

    What the Browse quick action is built on: an action for a drive in a drawer
    is not offered disabled, it is not rendered at all.
    """
    return [
        found
        for found in registered(conn)
        if presence(conn, found.id).here
    ]


def registered(conn: sqlite3.Connection) -> list[Destination]:
    """Every offline destination, connected or not."""
    return [
        found
        for found in destinations_module.destinations(conn, enabled_only=True)
        if OFFLINE in found.modes
    ]


# --- what the worker does with all of that -------------------------------------------


def appeared(
    conn: sqlite3.Connection, settings: Settings, destination: Destination
) -> bool:
    """Did this drive just turn up? Cheap enough to ask on a poll.

    The transition is what matters, not the state: a drive that has been
    plugged in since Tuesday should not be re-compared on every cycle, and one
    that was in a drawer a minute ago should be compared now.

    The expensive check only runs when the cheap one disagrees with what was
    last recorded, which is a few times a year rather than a few times a
    minute.
    """
    was = presence(conn, destination.id)
    seems = probe(settings, destination)
    if seems == was.state:
        return False
    now = look(conn, settings, destination)
    if now.state == WRONG_DRIVE:
        #  Worth a line in the log, because somebody plugged something in and
        #  nothing is going to happen. It is not an error: refusing is the
        #  feature working.
        LOGGER.info("%s: %s", destination.name, now.detail or now.label)
    return now.here


def _remember(
    conn: sqlite3.Connection,
    destination_id: int,
    state: str,
    verification: str,
    detail: str,
) -> Presence:
    now = utc_now()
    #  `present_at` only ever moves forward, and only when the drive was really
    #  here. It is the answer to "how long has this been in a drawer", so a
    #  check that found nothing must not overwrite it.
    conn.execute(
        """
        INSERT INTO offline_presence(destination_id, state, verification, detail,
                                     checked_at, present_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(destination_id) DO UPDATE SET
          state=excluded.state,
          verification=excluded.verification,
          detail=excluded.detail,
          checked_at=excluded.checked_at,
          present_at=CASE WHEN excluded.present_at <> ''
                          THEN excluded.present_at
                          ELSE offline_presence.present_at END
        """,
        (
            destination_id,
            state,
            verification,
            detail[:300],
            now,
            now if state == PRESENT else "",
        ),
    )
    return presence(conn, destination_id)


def _destination(conn: sqlite3.Connection, destination_id: int) -> Destination:
    found = destinations_module.destination(conn, destination_id)
    if found is None:  # pragma: no cover - written and read in one transaction
        raise TransferRefused("that destination was not saved")
    return found


def _by_identity(conn: sqlite3.Connection, identity: str) -> Destination | None:
    for found in destinations_module.destinations(conn):
        if found.identity and found.identity == identity:
            return found
    return None


def _registered_at(conn: sqlite3.Connection, path: Path) -> bool:
    return any(
        str(found.target) == str(path) for found in destinations_module.destinations(conn)
    )
