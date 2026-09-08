"""What a transfer intends to do, worked out before anything is transferred.

The same shape as Commit: know the answer, show the answer, then act. A backup
that starts copying and finds out what it did afterwards is a backup nobody can
check, and this is the one part of the program where "it seemed to work" is
indistinguishable from "it silently stopped a fortnight ago".

A comparison produces four numbers and nothing else needs to be said:

    84 to copy          in the library, not at the destination
     7 to update        at both, and the copy is stale
     3 already current  nothing to do
    11 destination-only there, and the library no longer has it

**The fourth number is information, not permission.** It is the whole reason
this file exists rather than a call to `rclone sync`: every mode reports it,
none of them acts on it, and `destinations.ACTIONS` has no verb that could.

## What is compared, and what it costs

Not hashes. Twenty terabytes hashed on a schedule is a machine that does
nothing else, and the evidence is already there in cheaper forms:

    what LibrAIry recorded transferring, and the bytes it verified
    size
    modification time, where the destination is one that keeps a real one

So a comparison is a *catalogue* difference, and the catalogue on the Library
side is the `items` table — which is indexed, already correct, and never walked
from disk for this. On the destination side it is one listing, and listings are
what destinations are good at.

Hashing stays available for the case it is actually for: `backup.py` already
verifies bytes at the moment of copying, four ways, which is where a hash is
worth paying for because it is one file that just moved.

## Bounded, and paged

A plan against a library of a million files is counts plus one page. Nothing
here builds a list of every file to be copied and hands it to a template: the
counts come from SQL, the page comes from SQL with a LIMIT, and the transfer
itself is rclone's problem and streams.

A destination that has been unplugged for three months and holds forty thousand
files the library no longer has produces the number forty thousand and fifty
rows to look at. Not forty thousand rows.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

from librairy.destinations import (
    CHANGED,
    COPY,
    CURRENT,
    DIFFERENCES,
    EXTRA,
    MISSING,
    REPORT,
    TRANSFERS,
    UPDATE,
    Destination,
    Policy,
    action_for,
)
from librairy.live import LIVE
from librairy.taxonomy import TEMPLATES

#  How many members of any one difference a plan carries as rows. The counts
#  are complete; the rows are a sample to look at, the same bargain every page
#  in this program makes. See `docs/performance.md` on the bounded-page rule.
PAGE = 50

#  Who asked for a transfer. Carried into history and **never** into behaviour:
#  nothing below this line branches on it, because a standing instruction and a
#  one-off request should move bytes in exactly the same way. What differs is
#  what they *are*, and that difference lives above the planner.
POLICY = "policy"
MANUAL = "manual"

ORIGINS = (POLICY, MANUAL)


@dataclass(frozen=True)
class Scope:
    """What one transfer covers. The only thing the planner needs to know.

    A standing policy makes one of these, and so does somebody pressing *Send
    to Offline Backup* on a folder. **That is the whole of what they share.**
    Below this line the machinery is identical — the same comparison, the same
    four answers, the same adapter, the same argv — and above it the intent is
    not: a policy is a standing instruction and a send is a thing somebody
    asked for once. Neither may become the other by accident, and the way that
    stays true is that a send has no way to write a policy row and a policy has
    no way to be created by pressing a button on a folder.

    `origin` rides along so that history can say which it was. Nothing reads it
    to decide anything.
    """

    #  Library-relative: a folder like `Photos` or `Books/Programming/Rust`, or
    #  one file's path when `exact`.
    prefix: str
    mode: str
    origin: str = POLICY
    exact: bool = False
    #  Which policy category this sits under, where one applies. Used to scope
    #  a divergence record and to label history — never to select files, which
    #  is what `prefix` is for.
    category: str = ""

    @property
    def directory(self) -> str:
        """The Library-relative directory this transfer's source is.

        The prefix itself for a subtree; the containing folder for one file.
        It is also the destination path beneath the backup root, which is what
        keeps a one-off send comparable with the scheduled backup that covers
        the same files.
        """
        if not self.exact:
            return self.prefix
        head, _, _tail = self.prefix.rpartition("/")
        return head

    @property
    def label(self) -> str:
        """What to call this scope where somebody reads it."""
        return self.prefix.rstrip("/").rpartition("/")[2] or self.prefix

    @classmethod
    def of(cls, policy: Policy) -> Scope:
        """The scope a standing policy covers: one whole category folder."""
        return cls(
            prefix=_folder(policy.category),
            mode=policy.mode,
            origin=POLICY,
            category=policy.category,
        )

    @classmethod
    def folder(cls, relpath: str, mode: str, origin: str = POLICY) -> Scope:
        return cls(
            prefix=relpath.strip("/"),
            mode=mode,
            origin=origin,
            category=category_of(relpath),
        )

    @classmethod
    def file(cls, relpath: str, mode: str, origin: str = POLICY) -> Scope:
        return cls(
            prefix=relpath.strip("/"),
            mode=mode,
            origin=origin,
            exact=True,
            category=category_of(relpath),
        )


def category_of(relpath: str) -> str:
    """Which policy category a Library path belongs to, or "".

    Read backwards out of the taxonomy's own templates, the same way `_folder`
    reads them forwards, so there is one answer to "where do photos go" rather
    than two that are free to disagree.
    """
    top = relpath.strip("/").split("/", 1)[0]
    for category in TEMPLATES:
        if _folder(category) == top:
            return category
    return ""


@dataclass(frozen=True)
class Entry:
    """One file, and how the destination differs about it."""

    relpath: str
    difference: str
    action: str
    size: int = 0
    #  What the destination has, when it has something and it is not the same.
    destination_size: int = 0


@dataclass(frozen=True)
class Plan:
    """What one transfer would do, and what it would leave alone."""

    scope: Scope
    destination: Destination
    counts: dict[str, int] = field(default_factory=dict)
    entries: tuple[Entry, ...] = ()
    #  Set when the comparison could not be made at all — the drive is not
    #  attached, the remote did not answer. Not an empty plan: "nothing to do"
    #  and "nobody could look" are different, and only one of them is good news.
    unavailable: str = ""

    @property
    def to_copy(self) -> int:
        return self.counts.get(MISSING, 0)

    @property
    def to_update(self) -> int:
        return self.counts.get(CHANGED, 0)

    @property
    def current(self) -> int:
        return self.counts.get(CURRENT, 0)

    @property
    def destination_only(self) -> int:
        return self.counts.get(EXTRA, 0)

    @property
    def transfers(self) -> int:
        """How many files would actually move. The only number that is work."""
        return self.to_copy + self.to_update

    @property
    def empty(self) -> bool:
        return not self.transfers

    @property
    def bytes_to_send(self) -> int:
        return sum(
            entry.size for entry in self.entries if entry.action in TRANSFERS
        )

    @property
    def reported(self) -> tuple[Entry, ...]:
        """The destination-only files, which are shown and never acted on."""
        return tuple(entry for entry in self.entries if entry.action == REPORT)

    @property
    def summary(self) -> str:
        parts = [
            f"{self.to_copy} to copy",
            f"{self.to_update} to update",
            f"{self.current} already current",
        ]
        if self.destination_only:
            #  Named for what it is, every time it is said. "11 extra" invites
            #  somebody to tidy them; "11 only here" says what is true.
            parts.append(f"{self.destination_only} only at the destination")
        return ", ".join(parts)


@dataclass(frozen=True)
class LibraryFile:
    relpath: str
    size: int


@dataclass(frozen=True)
class DestinationFile:
    relpath: str
    size: int


def library_files(
    conn: sqlite3.Connection, scope: Scope, *, limit: int = 0
) -> Iterator[LibraryFile]:
    """The library files one scope covers, from the index and never from disk.

    A scope is a path prefix — a whole category folder for a policy, an
    explicitly chosen subtree or one file for a send — so the covered set is a
    prefix match on an indexed column rather than a walk. At a million files
    this is the difference between a query and an afternoon, and it is why
    pressing a button on a folder of a hundred thousand photographs does not
    have to enumerate them anywhere.

    **Authoritative rows only.** `root='library'` and `LIVE`: something in the
    inbox with a proposed destination is not Library content, and a transfer
    that copied one out would be acting on a decision nobody made.

    **Yields.** Three hundred thousand photographs are three hundred thousand
    rows, and building a list of them to work out that four need copying is a
    Python object per file for no reason. `compare` consumes this one row at a
    time and keeps only counts and a page.
    """
    sql = (
        "SELECT relpath, size FROM items"  # noqa: S608 - `LIVE` is a module constant
        f" WHERE root='library' AND {LIVE} AND "
    )
    if scope.exact:
        sql += "relpath = ? ORDER BY relpath"
        args: tuple[str, ...] = (scope.prefix,)
    else:
        sql += "relpath LIKE ? ESCAPE '\\' ORDER BY relpath"
        args = (f"{_escaped(scope.prefix.rstrip('/'))}/%",)
    if limit:
        sql += f" LIMIT {int(limit)}"
    for row in conn.execute(sql, args):
        yield LibraryFile(relpath=str(row["relpath"]), size=int(row["size"] or 0))


@dataclass(frozen=True)
class Extent:
    """How much a scope covers, without listing any of it.

    Two aggregates over an indexed prefix, which is what a confirmation needs
    and the whole of what it needs. A folder of a hundred thousand files
    produces two numbers here and nowhere produces a hundred thousand of
    anything.
    """

    files: int = 0
    bytes: int = 0

    @property
    def any(self) -> bool:
        return self.files > 0


def extent(conn: sqlite3.Connection, scope: Scope) -> Extent:
    """`842 files · 11.6 GB`, from the index, in one statement."""
    where = "relpath = ?" if scope.exact else "relpath LIKE ? ESCAPE '\\'"
    args = (
        (scope.prefix,)
        if scope.exact
        else (f"{_escaped(scope.prefix.rstrip('/'))}/%",)
    )
    row = conn.execute(
        "SELECT COUNT(*) AS files, COALESCE(SUM(size), 0) AS bytes FROM items"  # noqa: S608
        f" WHERE root='library' AND {LIVE} AND {where}",
        args,
    ).fetchone()
    return Extent(files=int(row["files"] or 0), bytes=int(row["bytes"] or 0))


def compare(
    library: Iterable[LibraryFile],
    destination: list[DestinationFile],
    mode: str,
    *,
    keep: int = PAGE,
) -> tuple[dict[str, int], list[Entry]]:
    """Two catalogues in, four counts and a bounded sample of rows out.

    Deterministic and inspectable: same inputs, same answer, in a function that
    touches nothing. Every test about what a mode does can be written against
    this without a filesystem, a remote, or a subprocess anywhere near it.

    The library side streams; the destination side does not, because it is
    looked up by path and needs random access. That listing is therefore the
    memory bound of a comparison, and it is the destination's own size rather
    than the library's — which is the right way round, since the transfer
    itself never sees either list.
    """
    theirs = {found.relpath: found for found in destination}
    counts = dict.fromkeys(DIFFERENCES, 0)
    entries: list[Entry] = []
    per_difference = dict.fromkeys(DIFFERENCES, 0)
    ours_by_path: set[str] = set()

    for ours in library:
        ours_by_path.add(ours.relpath)
        there = theirs.get(ours.relpath)
        if there is None:
            difference = MISSING
        elif there.size != ours.size:
            #  Size, and not a hash. The cheapest evidence that is actually
            #  evidence: a file whose length changed is certainly different,
            #  and one whose length matches is compared properly at the moment
            #  it is copied, four ways, by `backup.py`.
            difference = CHANGED
        else:
            difference = CURRENT
        counts[difference] += 1
        if per_difference[difference] < keep:
            per_difference[difference] += 1
            entries.append(
                Entry(
                    relpath=ours.relpath,
                    difference=difference,
                    action=action_for(mode, difference),
                    size=ours.size,
                    destination_size=there.size if there else 0,
                )
            )

    for there in destination:
        if there.relpath in ours_by_path:
            continue
        counts[EXTRA] += 1
        if per_difference[EXTRA] < keep:
            per_difference[EXTRA] += 1
            entries.append(
                Entry(
                    relpath=there.relpath,
                    difference=EXTRA,
                    #  `REPORT` or `KEEP`, decided by the mode and by nothing
                    #  here. There is no branch in this function that could
                    #  produce a removal, because there is no such action.
                    action=action_for(mode, EXTRA),
                    size=0,
                    destination_size=there.size,
                )
            )
    return counts, entries


def destination_only(
    conn: sqlite3.Connection,
    scope: Scope,
    listing: list[DestinationFile],
) -> Iterator[Entry]:
    """Every file that is at the destination and not in the library. All of them.

    `compare` answers *how many*, in a bounded page, which is what a screen
    needs. This answers *which ones*, without a bound, which is what
    `divergence.record` needs in order to store the whole set — and it yields,
    so that four hundred thousand of them are four hundred thousand INSERTs in
    batches rather than four hundred thousand objects in a list.

    ## A merge, not a set

    Both sides are already in path order — the library from an indexed range
    scan, a destination listing because listings are sorted — so membership is
    decided by walking them together. Nothing accumulates: no dictionary of the
    destination, no set of a million library paths. It is the one part of a
    comparison that is bounded on *both* sides, and it is bounded because the
    ordering was already paid for.

    SQLite's default collation compares text by UTF-8 bytes and Python compares
    by code point, and for UTF-8 those are the same order. The merge depends on
    that agreement; `tests/test_divergence.py` pins it with paths that would
    expose a disagreement.
    """
    ours = iter(library_files(conn, scope))
    mine = next(ours, None)
    for there in sorted(listing, key=lambda found: found.relpath):
        while mine is not None and mine.relpath < there.relpath:
            mine = next(ours, None)
        if mine is not None and mine.relpath == there.relpath:
            continue
        yield Entry(
            relpath=there.relpath,
            difference=EXTRA,
            #  Whatever the mode says, which for every mode is `keep` or
            #  `report`. There is no third possibility to yield here, because
            #  `destinations.ACTIONS` has no fourth answer to this question.
            action=action_for(scope.mode, EXTRA),
            size=0,
            destination_size=there.size,
        )


def plan_for(
    conn: sqlite3.Connection,
    scope: Scope,
    destination: Destination,
    listing: list[DestinationFile] | None,
) -> Plan:
    """One transfer's intention, given what is at the destination.

    The same function for a scheduled policy and for an explicit send, because
    a plan is a statement about files and does not care who asked. What asked
    is in `scope.origin`, and nothing here reads it.

    `listing` of `None` means nobody could look — a drive in a drawer, a remote
    that did not answer. That is not an empty plan and must never render as
    one: "nothing to do" and "nothing could be checked" are different, and only
    the first is good news.
    """
    if listing is None:
        return Plan(
            scope=scope,
            destination=destination,
            unavailable=f"{destination.name} could not be reached",
        )
    counts, entries = compare(
        library_files(conn, scope),
        listing,
        scope.mode,
    )
    return Plan(
        scope=scope,
        destination=destination,
        counts=counts,
        entries=tuple(entries),
    )


def transfers(plan: Plan) -> tuple[Entry, ...]:
    """The entries that would actually put bytes somewhere.

    Everything the executor is allowed to act on, and it is a filter on
    `action` rather than on `difference` — so a mode that decided to leave
    something alone is obeyed here without this function knowing why.
    """
    return tuple(entry for entry in plan.entries if entry.action in (COPY, UPDATE))


def _folder(category: str) -> str:
    """The top-level folder a category files into.

    Derived from the taxonomy's own destination template rather than written
    out again here. A second list would be a second answer to "where do photos
    go", free to disagree with the one that actually files them — and it would
    disagree silently, by backing up a folder nothing is in.
    """
    template = TEMPLATES.get(category, {}).get("conventional", "")
    return template.split("/", 1)[0] if template else category.title()


def _escaped(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
