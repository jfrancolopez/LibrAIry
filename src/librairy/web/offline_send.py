"""Whether *Send to Offline Backup* can be offered here, and what it would say.

One question, asked from a page render, so the rules are about what a render is
allowed to do as much as about what the action means.

## It is offered only where it can actually work

    a destination is configured
    it is an Offline Backup
    it is enabled
    it was here the last time anybody looked
    it was identified

All five, and any of them missing means the action is **not rendered** rather
than rendered and disabled. A greyed-out button for a drive in a drawer is a
row of furniture explaining a thing that is not wrong.

The one thing that does *not* hide it is reduced verification. A drive
identified by its marker alone — because the operating system stopped
answering — may still be sent to, and the reduction is said in the offer rather
than concealed by it.

## A render never touches a disk

Presence comes from what the worker last recorded, not from a stat and
certainly not from `diskutil`. A page that probed hardware would block on an
unresponsive mount and would run a subprocess per render, and the answer it got
would be no fresher than the one that matters — the check made immediately
before anything is copied. See `librairy/offline_drives.py`.

## Only real Library folders

A Project and a tag view are *views*: files that live in different places,
gathered by what they are about. A page showing one is not showing a folder,
and "send this" against a filter would mean something nobody asked for. So
eligibility is a question about the Library index — does any live Library file
sit under this path — which a virtual view cannot accidentally satisfy.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from librairy import offline_drives, transfer_requests
from librairy.destinations import Destination
from librairy.transfer_paths import FULLY_VERIFIED, MARKER_ONLY
from librairy.web.browse import human_size


@dataclass(frozen=True)
class Offer:
    """What the action would send, and where."""

    destination_id: int
    drive: str
    relpath: str
    exact: bool
    files: int = 0
    bytes: int = 0
    #  Said, never hidden. A drive recognised by its marker alone has had less
    #  checking than it had at registration, and somebody sending twenty
    #  thousand photographs to it should be told that in the same breath.
    reduced: bool = False

    @property
    def label(self) -> str:
        return self.relpath.rstrip("/").rpartition("/")[2] or self.relpath

    @property
    def size(self) -> str:
        return human_size(self.bytes)

    @property
    def summary(self) -> str:
        """`842 files · 11.6 GB`. From the Library index, never from the drive."""
        plural = "file" if self.files == 1 else "files"
        return f"{self.files:,} {plural} · {self.size}"


def offers(conn: sqlite3.Connection, relpath: str, *, exact: bool = False) -> list[Offer]:
    """Every drive this can be sent to right now. Usually none, sometimes one.

    A list because somebody may register two drives, and an empty list is the
    ordinary answer — the drives are in a drawer.
    """
    if not relpath.strip("/"):
        return []
    #  Asked once, before any drive is considered: a path with no live Library
    #  file under it is not a folder anybody can send, whatever is on screen.
    if not transfer_requests.eligible(conn, relpath, exact=exact):
        return []
    covers = transfer_requests.covers(conn, relpath, exact=exact)
    found = []
    for drive in offline_drives.attached(conn):
        here = offline_drives.presence(conn, drive.id)
        if here.verification not in (FULLY_VERIFIED, MARKER_ONLY):
            #  Registered with no identity at all. Nothing to compare against,
            #  so nothing to be confident about, so no action.
            continue
        found.append(
            Offer(
                destination_id=drive.id,
                drive=drive.name,
                relpath=relpath.strip("/"),
                exact=exact,
                files=covers.files,
                bytes=covers.bytes,
                reduced=here.verification == MARKER_ONLY,
            )
        )
    return found


def offer_for(
    conn: sqlite3.Connection, relpath: str, destination_id: int, *, exact: bool = False
) -> Offer | None:
    """The one offer a submitted form names, re-checked rather than trusted.

    A form is somebody's browser telling us what a page said some time ago.
    Every condition is asked again here, and again by the worker before a byte
    moves.
    """
    for found in offers(conn, relpath, exact=exact):
        if found.destination_id == destination_id:
            return found
    return None


def drive_named(conn: sqlite3.Connection, destination_id: int) -> Destination | None:
    for drive in offline_drives.registered(conn):
        if drive.id == destination_id:
            return drive
    return None
