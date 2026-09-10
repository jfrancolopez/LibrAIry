"""What went wrong, said to a person, in the three sentences that matter.

Every failure in LibrAIry eventually reaches somebody who wants to know three
things, in this order:

    What happened?
    Is my Library safe?
    What can I do next?

`[Errno 28] No space left on device: '/library/Documents/big.bin.part-…'`
answers none of them. It was, until this module, the entire explanation a person
got when a commit failed — rendered raw on the plan page, and reduced to the
word "failed" on the page they were actually looking at.

**The middle question is deliberately not answered here.** Whether the Library
is safe is a fact about what the operation had already done when it failed, and
only the caller knows that: the same `ENOSPC` means "nothing moved" from the
executor's first operation and "eleven files moved, this one did not" from its
twelfth. A classifier that guessed would be inventing the one sentence people
act on. So a `Failure` carries *what* and *next*, and the surface supplies the
safety sentence from state it can prove — see `librairy/commit_state.py`, which
had to learn the same lesson about interrupted commits.

Four kinds, because they want four different treatments and one of them is not
an error at all:

    unavailable   an expected absent state. A drive that is not plugged in, a
                  provider that is switched off. Never coloured like a fault.
    operational   a recoverable failure of the operation. Retry converges once
                  the underlying condition changes.
    action        a problem only the person can clear: a permission, a
                  read-only mount, a wrong drive.
    fault         something LibrAIry did not anticipate. The only kind that
                  gets a correlation id and a "this is a bug" tone.
"""

from __future__ import annotations

import errno
import sqlite3
from dataclasses import dataclass

#  The four kinds, as words rather than as bare strings at every call site.
UNAVAILABLE = "unavailable"
OPERATIONAL = "operational"
ACTION = "action"
FAULT = "fault"

#  How much of a technical detail is worth keeping. The journal is a permanent
#  record and a diagnostic string is not the interesting part of it; long enough
#  for an errno, a path and a hash, short enough that a row stays a row.
DETAIL_LIMIT = 300

#  The words a journalled failure can begin with. Two, because a reversal that
#  fails is not a move that fails and History filters on the action, not on the
#  reason — see `librairy/history.py`.
MARKERS = ("failed", "undo_failed")


@dataclass(frozen=True)
class Failure:
    """One failure, ready to be shown. `detail` is for diagnosis, never for the
    first line: it is the raw text, and it is what a person pastes into an
    issue rather than what they read to decide what to do."""

    kind: str
    code: str
    what: str
    next: str
    detail: str = ""

    @property
    def expected(self) -> bool:
        """True when this is a state rather than a fault. Nothing about an
        unplugged drive should be red."""
        return self.kind == UNAVAILABLE

    @property
    def needs_you(self) -> bool:
        """True when retrying changes nothing until somebody does something."""
        return self.kind == ACTION

    def journalled(self, marker: str = "failed") -> str:
        """`failed <code> <detail>` — the shape the journal already uses.

        `refused_collision <code>` and `undo_refused_changed expected=… actual=…`
        were written this way before this module existed: a token first so a
        reader can classify without parsing prose, diagnostics after. Same
        spelling, so `from_outcome` can read every one of them.
        """
        detail = " ".join(self.detail.split())[:DETAIL_LIMIT]
        return f"{marker} {self.code} {detail}".strip()


#  errno → what a person needs to know. The list is short on purpose: these are
#  the ones a NAS actually produces, and every other errno is a fault until it
#  proves it belongs on a list of things we tell people how to fix.
_BY_ERRNO: dict[int, tuple[str, str, str, str]] = {
    errno.ENOSPC: (
        OPERATIONAL,
        "destination-full",
        "The destination is full.",
        "Free space where the files are going, then commit again.",
    ),
    errno.EDQUOT: (
        OPERATIONAL,
        "quota-exceeded",
        "The destination has no quota left for this user.",
        "Raise the quota or free space, then commit again.",
    ),
    errno.EACCES: (
        ACTION,
        "permission-denied",
        "LibrAIry is not allowed to write there.",
        "Give the container's user write access to that folder, then try again.",
    ),
    errno.EPERM: (
        ACTION,
        "permission-denied",
        "LibrAIry is not allowed to write there.",
        "Give the container's user write access to that folder, then try again.",
    ),
    errno.EROFS: (
        ACTION,
        "read-only",
        "The destination is read-only.",
        "Make the storage writable — a share remounted read-only is the usual "
        "cause — then try again.",
    ),
    errno.ENAMETOOLONG: (
        ACTION,
        "name-too-long",
        "The name is longer than that filesystem allows.",
        "Shorten the name in Review, then commit again.",
    ),
    errno.EIO: (
        OPERATIONAL,
        "storage-error",
        "The storage reported a read/write error.",
        "Check the disk and the connection to it before trying again.",
    ),
    errno.EMFILE: (
        OPERATIONAL,
        "too-many-open-files",
        "LibrAIry ran out of open files.",
        "Raise the container's open-file limit, then try again.",
    ),
    errno.ENFILE: (
        OPERATIONAL,
        "too-many-open-files",
        "The system ran out of open files.",
        "Try again once the system is less busy.",
    ),
}

#  Errnos that all say the same thing: the storage LibrAIry was writing to is
#  not there any more. A share that unmounted, a device that went away, an NFS
#  handle that went stale, a server that stopped answering.
_GONE = {
    errno.ENOENT,
    errno.ENOTDIR,
    errno.ENODEV,
    errno.ENXIO,
    errno.ESTALE,
    errno.ENOTCONN,
    errno.EHOSTDOWN,
    errno.EHOSTUNREACH,
    errno.ETIMEDOUT,
    errno.ECONNABORTED,
    errno.ECONNRESET,
}

VERIFICATION_FAILED = Failure(
    OPERATIONAL,
    "verification-failed",
    "The copy that arrived did not match the file it came from.",
    "The incomplete copy was removed and the original left alone. "
    "Check the destination storage, then commit again.",
)

STORAGE_GONE = Failure(
    UNAVAILABLE,
    "storage-unavailable",
    "The storage is not available.",
    "Reconnect the storage, then try again.",
)


def classify(exc: BaseException) -> Failure:
    """One raised exception, as something worth reading.

    Deliberately total: everything that is not recognised comes back as a
    `fault`, with its class name and message kept as the detail. An unknown
    failure must still produce the three sentences, because the alternative is
    the raw exception reaching the page — which is where this started.
    """
    #  An exception that already knows what it is keeps it. `StorageUnavailable`
    #  is raised carrying the refusal that was decided in `librairy/roots.py`,
    #  and re-deriving it from the class name would lose the root's name.
    carried = getattr(exc, "failure", None)
    if isinstance(carried, Failure):
        return carried
    detail = f"{exc.__class__.__name__}: {exc}"
    if isinstance(exc, sqlite3.Error):
        return _database(exc, detail)
    if isinstance(exc, OSError) and exc.errno is not None:
        known = _BY_ERRNO.get(exc.errno)
        if known is not None:
            kind, code, what, action = known
            return Failure(kind, code, what, action, detail)
        if exc.errno in _GONE:
            return Failure(
                STORAGE_GONE.kind, STORAGE_GONE.code, STORAGE_GONE.what,
                STORAGE_GONE.next, detail,
            )
    return Failure(
        FAULT,
        "unexpected",
        "LibrAIry hit a problem it does not recognise.",
        "Nothing here needs guessing at — the details below are what to report.",
        detail,
    )


def _database(exc: sqlite3.Error, detail: str) -> Failure:
    """SQLite, without SQLite's words.

    "database is locked" is a sentence about a writer lock, and the person
    reading it owns a file server, not a database. What they need to know is
    that LibrAIry could not write down what it did — which is a different and
    much more important fact than anything about locking.
    """
    from librairy.db import is_locked

    message = str(exc).lower()
    if is_locked(exc):
        return Failure(
            OPERATIONAL,
            "database-busy",
            "LibrAIry could not update its database — something else was writing to it.",
            "Try again in a moment.",
            detail,
        )
    if "readonly" in message or "read-only" in message:
        return Failure(
            ACTION,
            "database-read-only",
            "LibrAIry could not update its database because it is read-only.",
            "Make the appdata folder writable, then try again.",
            detail,
        )
    if "disk is full" in message or "disk full" in message:
        return Failure(
            OPERATIONAL,
            "database-full",
            "LibrAIry could not update its database because the disk is full.",
            "Free space on the appdata volume, then try again.",
            detail,
        )
    if "malformed" in message or "not a database" in message or "corrupt" in message:
        return Failure(
            ACTION,
            "database-damaged",
            "LibrAIry's database is damaged.",
            "Restore the appdata folder from a backup before using LibrAIry again.",
            detail,
        )
    return Failure(
        OPERATIONAL,
        "database-error",
        "LibrAIry could not update its database.",
        "Try again once the database and its storage are available.",
        detail,
    )


#  Codes this module produced before an exception was involved at all, so that a
#  journalled outcome reads back as the same sentences the failure was shown
#  with. Anything not here is looked up by classifying its own code.
_BY_CODE: dict[str, Failure] = {
    failure.code: failure
    for failure in (
        STORAGE_GONE,
        *(Failure(kind, code, what, act) for kind, code, what, act in _BY_ERRNO.values()),
    )
}
_BY_CODE.update(
    {
        failure.code: failure
        for failure in (
            Failure(
                OPERATIONAL,
                "database-busy",
                "LibrAIry could not update its database — something else was writing to it.",
                "Try again in a moment.",
            ),
            Failure(
                ACTION,
                "database-read-only",
                "LibrAIry could not update its database because it is read-only.",
                "Make the appdata folder writable, then try again.",
            ),
            Failure(
                OPERATIONAL,
                "database-full",
                "LibrAIry could not update its database because the disk is full.",
                "Free space on the appdata volume, then try again.",
            ),
            Failure(
                ACTION,
                "database-damaged",
                "LibrAIry's database is damaged.",
                "Restore the appdata folder from a backup before using LibrAIry again.",
            ),
            Failure(
                OPERATIONAL,
                "database-error",
                "LibrAIry could not update its database.",
                "Try again once the database and its storage are available.",
            ),
            VERIFICATION_FAILED,
        )
    }
)

UNRECOGNISED = Failure(
    FAULT,
    "unexpected",
    "LibrAIry hit a problem it does not recognise.",
    "Nothing here needs guessing at — the details below are what to report.",
)


def from_outcome(outcome: str) -> Failure:
    """Read a journalled `failed <code> <detail>` back into its sentences.

    Journal rows written before this module said `[Errno 13] Permission denied:
    '/library/…'` and nothing else. Those still exist in every installation that
    has ever had a commit fail, so they have to come back as *something*: they
    come back as a fault carrying their own text as the detail, which is exactly
    what they are.
    """
    words = str(outcome or "").split(" ", 2)
    if not words or words[0] not in MARKERS:
        return Failure(
            UNRECOGNISED.kind, UNRECOGNISED.code, UNRECOGNISED.what,
            UNRECOGNISED.next, str(outcome or ""),
        )
    code = words[1] if len(words) > 1 else ""
    detail = words[2] if len(words) > 2 else ""
    known = _BY_CODE.get(code)
    if known is None:
        return Failure(
            UNRECOGNISED.kind, UNRECOGNISED.code, UNRECOGNISED.what,
            UNRECOGNISED.next, (f"{code} {detail}").strip(),
        )
    return Failure(known.kind, known.code, known.what, known.next, detail)
