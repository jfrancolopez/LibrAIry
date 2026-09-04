"""Where a transfer may read from, and where it may write to. Nothing else.

A path arriving from a form is somebody's typing. It is not authority, and the
one place that distinction has to hold absolutely is the code that hands a
directory to a program whose job is to copy files into it.

Everything here answers one of two questions:

    is this source really inside the Library
    is this destination really somewhere else

and refuses rather than guessing. A refusal costs somebody a second attempt at
a settings field; the alternative costs them their library.

## What is checked, and why each one is here

**Resolved, not compared as text.** `library/../library/Photos` is inside the
Library and `Photos/../../etc` is not, and neither is decidable by looking at
the string. Every path is resolved before anything is asked about it, so `..`
is arithmetic that has already happened rather than a token to be matched.

**Symlinks are followed and then judged.** `Path.resolve()` follows them, which
is what makes a symlink out of the Library a path that fails containment
instead of a hole in it. A source that resolves outside the Library is refused
even when the link that got there was inside it.

**The destination may not be the Library, contain it, or sit inside it.** All
three directions, because all three are ways to copy a library onto itself: a
destination inside the Library duplicates it into itself on every run; a
destination containing it invites a later tool to treat the whole tree as one
managed directory; and the same tree both ways is a transfer that can only
either do nothing or corrupt something.

**An offline drive is identified before it is written to, two ways.**
`/Volumes/Backup` is whatever was plugged in most recently, and a stale mount
point pointing at a different disk is the ordinary case rather than an exotic
one. Both facts are checked and each covers the other's hole:

    the volume            is this the same filesystem? From the operating
                          system, where it will say — `librairy/volumes.py`
    the marker file       was this filesystem registered with LibrAIry?
                          Written by us, works everywhere

A marker alone can be cloned: copy a backup drive and the copy claims to be the
original. A volume id alone says nothing about whether this program has ever
seen the drive. Either one disagreeing refuses the transfer; a platform that
cannot answer the first falls back to the second, which is not a regression
because it is where this started.

## What is not checked here

Whether the destination is reachable, has room, or is fast. Those are questions
for the moment of transfer and they have answers that change; these have
answers that are true before anything starts, which is when a refusal is cheap.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from librairy import volumes
from librairy.config import Settings

#  The file that says which registered drive this is. A plain text file, on
#  purpose: somebody looking at the drive on another machine can read it, and
#  nothing about it is magic enough to be worth hiding.
MARKER = ".librairy-destination"

#  Remote targets that are not remote targets. rclone reads `name:path`, and a
#  string with no colon is a local path pretending — which would silently make
#  a relative directory next to the working directory into a backup.
_REMOTE_SEPARATOR = ":"


class TransferRefused(RuntimeError):
    """A transfer that will not be attempted, and the reason in one sentence."""


@dataclass(frozen=True)
class LocalTarget:
    """A checked local destination directory."""

    path: Path
    identity: str = ""
    #  What the operating system says the filesystem is, where it says
    #  anything. Empty is a legitimate answer — see `librairy/volumes.py`.
    volume: str = ""


def library_source(settings: Settings, relpath: str = "") -> Path:
    """A directory inside the Library to copy *from*, resolved and checked.

    The empty relpath is the whole Library, which is what a category-wide
    policy uses. Anything else has to still be inside it after resolution —
    which is where `..`, an absolute path pasted into a form, and a symlink
    pointing out of the tree all fail together.
    """
    library = _resolved(settings.library_dir)
    if not relpath:
        return library
    candidate = _resolved(library / relpath)
    if not _within(candidate, library):
        raise TransferRefused(
            f"{relpath!r} is not inside your library once resolved; nothing was copied"
        )
    return candidate


def local_destination(settings: Settings, target: str) -> LocalTarget:
    """A local destination directory, checked against the Library three ways.

    Same tree, inside it, or containing it — all three refused, because all
    three are ways of copying a library onto itself.
    """
    if not str(target).strip():
        raise TransferRefused("that destination has no path")
    library = _resolved(settings.library_dir)
    path = _resolved(Path(str(target).strip()).expanduser())
    if path == library:
        raise TransferRefused("that destination is your library itself")
    if _within(path, library):
        raise TransferRefused(
            "that destination is inside your library — a backup kept in the thing"
            " it is backing up is not a backup"
        )
    if _within(library, path):
        raise TransferRefused(
            "that destination contains your library; choose a folder beside it"
            " rather than above it"
        )
    #  Every other managed root, for the same reason and with less ceremony: a
    #  destination inside the inbox would have LibrAIry back up files it is
    #  about to file, and one inside quarantine would copy what somebody set
    #  aside.
    for name, root in (
        ("inbox", settings.inbox_dir),
        ("quarantine", settings.quarantine_dir),
        ("LibrAIry's own data", settings.appdata_dir),
    ):
        managed = _resolved(root)
        if path == managed or _within(path, managed):
            raise TransferRefused(f"that destination is inside your {name} folder")
    return LocalTarget(path=path)


def remote_destination(target: str) -> str:
    """An rclone remote, checked for being one at all.

    `name:path`, with a name. A string with no colon is a *local* path as far
    as rclone is concerned, so accepting one here would quietly turn a typo in
    a settings field into a directory created next to whatever the working
    directory happened to be.
    """
    text = str(target).strip()
    if _REMOTE_SEPARATOR not in text:
        raise TransferRefused(
            f"{text!r} is not an rclone remote — those look like `name:path`"
        )
    name, _, _rest = text.partition(_REMOTE_SEPARATOR)
    if not name or name.startswith("-") or "/" in name or os.sep in name:
        raise TransferRefused(f"{text!r} does not name an rclone remote")
    return text


def register(path: Path, identity: str) -> None:
    """Write the marker that makes this drive identifiable later.

    Done once, when somebody registers the drive, and never during a transfer.
    """
    (path / MARKER).write_text(f"{identity}\n", encoding="utf-8")


def identify(path: Path) -> str:
    """What the drive at this path says it is, or "" if it says nothing.

    Half of offline-drive identity, and deliberately the dull half: a marker
    file written at registration is checkable on every platform and every
    filesystem, survives being unplugged, and answers a question no volume id
    can — *was this drive registered with LibrAIry*. The other half, *is this
    the same filesystem*, is `librairy/volumes.py`, and it is the one that
    catches a clone.
    """
    marker = path / MARKER
    try:
        return marker.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""


def attached(path: Path, identity: str, volume: str = "") -> bool:
    """Is the registered drive here right now?

    Every half. A directory existing at the mount point is not the drive — an
    unplugged USB disk often leaves its folder behind, empty, and a backup
    written into that folder goes onto the system disk and looks like it
    worked.
    """
    if not path.is_dir():
        return False
    if identity and identify(path) != identity:
        return False
    return volumes.matches(volume, volumes.identity_for(path))


def checked_offline(
    settings: Settings, target: str, identity: str, volume: str = ""
) -> LocalTarget:
    """A registered offline drive, checked for being present *and* being itself.

    Called immediately before a transfer and not once at planning time. A drive
    can be pulled between deciding to copy and copying, and the mount point it
    leaves behind is a directory that will happily accept files.
    """
    found = local_destination(settings, target)
    if not found.path.is_dir():
        raise TransferRefused("that drive is not connected")
    if identity and identify(found.path) != identity:
        raise TransferRefused(
            "the drive at that path is not the one that was registered — nothing"
            " was copied"
        )
    here = volumes.identity_for(found.path)
    if not volumes.matches(volume, here):
        #  The case the marker cannot catch: a *clone*. Same marker, different
        #  filesystem, and it is the copy rather than the drive that was
        #  registered.
        raise TransferRefused(
            "the filesystem at that path is not the one that was registered —"
            " nothing was copied"
        )
    return LocalTarget(path=found.path, identity=identity, volume=here or volume)


def volume_of(path: Path) -> str:
    """What the operating system calls the filesystem here, or "".

    Read at registration and stored beside the marker. Re-exported from here so
    that everything about identifying a destination is reachable from the
    module that decides whether to write to one.
    """
    return volumes.identity_for(path)


def _resolved(path: Path) -> Path:
    #  `strict=False`: a destination that does not exist yet is a normal thing
    #  to configure, and resolving it still collapses `..` and follows every
    #  symlink that *does* exist along the way.
    return Path(path).expanduser().resolve()


def _within(candidate: Path, root: Path) -> bool:
    """Is `candidate` at or below `root`? Both already resolved.

    `is_relative_to` and not a string prefix: `/data/library-backup` starts
    with `/data/library` and is not inside it, which is the bug every hand-
    written version of this function has had at least once.
    """
    return candidate == root or candidate.is_relative_to(root)
