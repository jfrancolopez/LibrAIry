"""Is the storage LibrAIry is about to write to the storage it knows?

A NAS share unmounts. What is left at `/library` is an empty directory on the
container's own disk, and it is writable, and it is a directory, and every check
LibrAIry made before this module said it was fine. So a commit moved somebody's
files into it and reported: **2 files moved. Nothing failed.** Those files are
inside a container that is recreated on the next `docker compose up`, and the
person was told it worked.

The same absence has a second face. A scan of that empty mount point marks every
row in the Library missing — twelve thousand files declared gone, Browse empty,
Search empty — because "I walked the tree and did not find it" is exactly what a
deleted file looks like too.

Neither is a bug in the executor or in the scanner. Both did what they were
asked. It is a missing question: *is this the same storage?*

LibrAIry already knew how to ask it. `librairy/offline_drives.py` identifies a
removable drive, and `librairy/volumes.py` is the durable half of that identity —
what the operating system calls the filesystem, read rather than written.

**Nothing here writes to the Library.** The other half of an offline drive's
identity is a marker file, and a marker in the Library would be LibrAIry adding
a file to somebody's collection — which it does not do, for anything, ever. So
the identity used here is the one that can be *observed*: the filesystem under
the root, and the index's own account of what should be there.

Three questions, cheapest first, and a refusal needs only one of them:

    1. Is there a directory at all?
    2. Does the database list live files here while the folder holds none?
    3. Is the filesystem under it a different filesystem from the one this
       installation started against?

The third is one `stat` in the ordinary case: the device number is compared
first, and the platform is only asked for a durable volume id when that number
has actually changed — which is the one moment the answer can differ, and the
moment a share legitimately remounted needs to be told from a share replaced.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from librairy import volumes
from librairy.config import Settings
from librairy.failures import ACTION, UNAVAILABLE, Failure

#  The three roots LibrAIry moves files between. `appdata` is deliberately not
#  here: it holds the database, and a database that is not there fails loudly on
#  its first query — there is nothing for this to protect.
ROOTS = ("inbox", "library", "quarantine")

STATE_KEY = "roots.storage"

#  What each root is called in a sentence a person reads.
LABEL = {"inbox": "Inbox", "library": "Library", "quarantine": "Quarantine"}


@dataclass(frozen=True)
class RootState:
    """One root, and whether LibrAIry recognises the storage under it."""

    name: str
    path: Path
    present: bool
    recognised: bool
    detail: str = ""
    #  Why, when it is not recognised — the same `Failure` the operation would
    #  refuse with, so Health and Commit cannot describe one situation in two
    #  different ways.
    failure: Failure | None = None

    @property
    def label(self) -> str:
        return LABEL.get(self.name, self.name)


def path_for(settings: Settings, name: str) -> Path:
    return {
        "inbox": settings.inbox_dir,
        "library": settings.library_dir,
        "quarantine": settings.quarantine_dir,
    }[name]


def unavailable(name: str, detail: str = "") -> Failure:
    """The refusal, in the sentences a person needs, for one named root.

    The safety half is the caller's to add — this one is *what* and *next* only,
    for the same reason the rest of `librairy/failures.py` leaves it out.
    """
    label = LABEL.get(name, name)
    return Failure(
        UNAVAILABLE,
        "storage-unavailable",
        f"{label} storage is not available.",
        f"Reconnect the {label.lower()} storage, then try again.",
        detail,
    )


def mismatched(name: str, detail: str = "") -> Failure:
    label = LABEL.get(name, name)
    return Failure(
        ACTION,
        "storage-not-recognised",
        f"A different filesystem is mounted as your {label.lower()}.",
        "Mount the storage LibrAIry was started against, or restart LibrAIry "
        "to use this one instead.",
        detail,
    )


# --- what was observed ---------------------------------------------------------------


def recorded(conn: sqlite3.Connection) -> dict[str, dict[str, object]]:
    row = conn.execute(
        "SELECT value FROM worker_state WHERE key=?", (STATE_KEY,)
    ).fetchone()
    if row is None:
        return {}
    try:
        payload = json.loads(str(row["value"]))
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return {}
    if not isinstance(payload, dict):  # pragma: no cover - defensive
        return {}
    return {str(k): v for k, v in payload.items() if isinstance(v, dict)}


def _record(conn: sqlite3.Connection, seen: dict[str, dict[str, object]]) -> None:
    conn.execute(
        "INSERT INTO worker_state(key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (STATE_KEY, json.dumps(seen, sort_keys=True)),
    )


def observe(conn: sqlite3.Connection, settings: Settings) -> None:
    """Write down which filesystem each root is on, so a change can be noticed.

    Called when LibrAIry starts, and only then. A check that re-observes what it
    is checking is a check that passes.

    A root that already looks like a bare mount point is written down as
    `suspect`. An installation whose share happened to be down at the moment it
    restarted must not have an empty mount point silently accepted as its
    Library — but it must not be *forgotten* either, or there would be nothing
    to compare against when the share came back. So it is recorded and marked,
    and every operation refuses while it still looks like that.
    """
    before = recorded(conn)
    seen = dict(before)
    for name in ROOTS:
        path = path_for(settings, name)
        if not path.is_dir():
            continue
        try:
            device = path.stat().st_dev
        except OSError:  # pragma: no cover - is_dir() has just succeeded
            continue
        suspect = _empty_but_indexed(conn, name, path)
        previous = seen.get(name, {})
        if previous.get("dev") == device and previous.get("suspect") == suspect:
            continue
        #  The durable identity, but only where reading it costs nothing. On
        #  Linux it is two file reads; on a platform that needs a subprocess to
        #  name a filesystem this would be three five-second timeouts on every
        #  startup. Where it is absent the device number carries the question
        #  alone and `_different_filesystem` falls back to what it can observe —
        #  "" has always been a legitimate answer here. See `librairy/volumes.py`.
        seen[name] = {
            "dev": device,
            "volume": volumes.identity_for(path) if volumes.readable_cheaply() else "",
            "suspect": suspect,
        }
    #  Compared against what was read at the top rather than read again. A
    #  startup that changed nothing writes nothing.
    if seen != before:
        _record(conn, seen)


# --- verifying -----------------------------------------------------------------------


def check(
    conn: sqlite3.Connection,
    settings: Settings,
    *names: str,
    path: Path | None = None,
) -> Failure | None:
    """Is every named root still the storage LibrAIry started against?

    `None` means yes. Anything else is a refusal to be shown *before* the
    operation touches anything, which is the whole value of it: a failure that
    happens before the first byte moves is the one failure whose safety sentence
    needs no qualification at all.
    """
    #  Read once for the whole question rather than once per root: the
    #  Dashboard asks this on every poll, and three identical `worker_state`
    #  lookups is three queries out of a bounded budget.
    known = recorded(conn)
    for name in names or ROOTS:
        found = _gone(conn, settings, name, known, path=path)
        if found is not None:
            return found
    return None


def _gone(
    conn: sqlite3.Connection,
    settings: Settings,
    name: str,
    known: dict[str, dict[str, object]],
    *,
    path: Path | None = None,
) -> Failure | None:
    """The whole rule, in the order it is cheapest to be certain in.

    Emptiness is deliberately *not* a signal on its own. Somebody who deletes
    every file in their inbox by hand has an empty inbox, and refusing to scan it
    would leave the index insisting those files are still there. It is only
    evidence when the filesystem underneath has also changed — or when LibrAIry
    started against a root that already looked like a bare mount point, which is
    what `suspect` records.
    """
    #  The caller's path where it gave one. `scan_root` is handed the directory
    #  to walk rather than deriving it, and a check that re-derived it from
    #  settings would be asking about a different folder than the one being
    #  scanned.
    path = path or path_for(settings, name)
    if not path.is_dir():
        return unavailable(name, f"there is no directory at {path}")
    previous = known.get(name)
    if not previous or "dev" not in previous:
        #  LibrAIry has never started against this root, so there is nothing to
        #  compare it with and nothing honest to say about it.
        return None
    if previous.get("suspect"):
        #  What was written down at startup is not evidence of what this root
        #  should be — it is a note that the root already looked wrong. So it
        #  refuses while it still looks wrong, and it is *not* compared against
        #  once the storage arrives: an installation that restarted while its
        #  share was down would otherwise record the empty mount point's own
        #  filesystem and then refuse the real Library for not matching it.
        if _empty_but_indexed(conn, name, path):
            return unavailable(
                name,
                f"{path} held no files when LibrAIry started, and the index "
                "lists files there",
            )
        return None
    return _different_filesystem(conn, name, path, previous)


def _empty_but_indexed(conn: sqlite3.Connection, name: str, path: Path) -> bool:
    """Nothing here, and the database says there should be something.

    Deliberately unanimous. A Library with one file in it and eleven thousand
    rows is a Library somebody has been rearranging by hand, and refusing to work
    over that would be its own kind of damage. *Nothing visible at all* against a
    non-empty index is the bare-mount-point signature and nothing else.

    Hidden entries do not count as content: an unmounted mount point often keeps
    a `.DS_Store` or a `lost+found`, and one of those is not somebody's library.
    """
    if not _has_visible_entry(path):
        return _indexed(conn, name)
    return False


def _has_visible_entry(path: Path) -> bool:
    try:
        return any(
            not entry.name.startswith(".") and entry.name != "lost+found"
            for entry in path.iterdir()
        )
    except OSError:
        return False


def _indexed(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM items WHERE root=? AND missing_since IS NULL LIMIT 1",
            (name,),
        ).fetchone()
        is not None
    )


def _different_filesystem(
    conn: sqlite3.Connection, name: str, path: Path, previous: dict[str, object]
) -> Failure | None:
    """Something else is mounted here — and not merely mounted again.

    The device number is not durable: unmount and remount the same share and
    Linux hands out a new one. Refusing that would demand a restart every time a
    NAS reconnects, and the message would be a lie. So a changed device number is
    a *question*, not an answer, and the durable volume id — which survives a
    remount and is what tells one disk from another — is what answers it.
    """
    try:
        device = path.stat().st_dev
    except OSError:  # pragma: no cover - is_dir() has just succeeded
        return unavailable(name, f"{path} could not be read")
    if previous["dev"] == device:
        return None
    recorded_volume = str(previous.get("volume") or "")
    found = volumes.identity_for(path) if recorded_volume else ""
    if recorded_volume and found and not volumes.matches(recorded_volume, found):
        return mismatched(name, f"{path} is on {found}, not {recorded_volume}")
    if recorded_volume and found:
        return None
    #  A different filesystem is here and the platform cannot say which one, so
    #  "remounted" and "replaced" are indistinguishable by identity. Emptiness
    #  decides it: an empty directory under a *changed* filesystem, against an
    #  index that lists files, is a bare mount point and nothing else.
    if _empty_but_indexed(conn, name, path):
        return unavailable(
            name, f"{path} holds no files, and the index lists files there"
        )
    return None


def state(conn: sqlite3.Connection, settings: Settings) -> list[RootState]:
    """Every root and what is under it, for Health to report without judging."""
    known = recorded(conn)
    rows: list[RootState] = []
    for name in ROOTS:
        path = path_for(settings, name)
        present = path.is_dir()
        if not present:
            missing = unavailable(name, f"there is no directory at {path}")
            rows.append(RootState(name, path, False, False, missing.what, missing))
            continue
        found = _gone(conn, settings, name, known)
        if found is None:
            rows.append(RootState(name, path, True, True))
        else:
            rows.append(RootState(name, path, True, False, found.what, found))
    return rows
