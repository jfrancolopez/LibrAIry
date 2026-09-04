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

from librairy import divergence
from librairy.destinations import (
    CHANGED,
    COPY,
    CURRENT,
    DIFFERENCES,
    EXTRA,
    MIRROR,
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
    """What one policy would do, and what it would leave alone."""

    policy: Policy
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
    conn: sqlite3.Connection, category: str, *, limit: int = 0
) -> Iterator[LibraryFile]:
    """The library files one policy covers, from the index and never from disk.

    A category is a top-level folder — the taxonomy files into `Photos/`,
    `Music/` and so on — so the covered set is a prefix match on an indexed
    column rather than a walk. At a million files this is the difference
    between a query and an afternoon.

    **Yields.** Three hundred thousand photographs are three hundred thousand
    rows, and building a list of them to work out that four need copying is a
    Python object per file for no reason. `compare` consumes this one row at a
    time and keeps only counts and a page.
    """
    prefix = f"{_folder(category)}/"
    sql = (
        "SELECT relpath, size FROM items"  # noqa: S608 - `LIVE` is a module constant
        f" WHERE root='library' AND {LIVE} AND relpath LIKE ? ESCAPE '\\'"
        " ORDER BY relpath"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"
    for row in conn.execute(sql, (f"{_escaped(prefix)}%",)):
        yield LibraryFile(relpath=str(row["relpath"]), size=int(row["size"] or 0))


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


def plan_for(
    conn: sqlite3.Connection,
    policy: Policy,
    destination: Destination,
    listing: list[DestinationFile] | None,
) -> Plan:
    """One policy's intention, given what is at the destination.

    `listing` of `None` means nobody could look — a drive in a drawer, a remote
    that did not answer. That is not an empty plan and must never render as
    one: "nothing to do" and "nothing could be checked" are different, and only
    the first is good news.
    """
    if listing is None:
        return Plan(
            policy=policy,
            destination=destination,
            unavailable=f"{destination.name} could not be reached",
        )
    counts, entries = compare(
        library_files(conn, policy.category),
        listing,
        policy.mode,
        #  A Mirror keeps a larger sample of what is only at the destination,
        #  because that sample is what somebody reads. Every other difference
        #  is still one page: they are illustrations of a number, and the
        #  number is what matters.
        keep=divergence.KEEP if policy.mode == MIRROR else PAGE,
    )
    return Plan(
        policy=policy,
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
