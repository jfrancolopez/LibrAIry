"""Which findings are about the same real-world thing, and which one leads.

The report that produced this file names two rows in the live library:

    Music/Pop/A Taste Of Honey                     (a dismissal, and nothing else)
    Music/Pop/A Taste Of Honey -> Various Artists  (rich evidence, a destination)

They are both true, and the database is right to hold two rows: one is
`artwork-not-on-disk`, one is `collection-custom`, and two detectors found two
different things. But a person reading Review sees two cards asking two
overlapping questions about one folder, with no way to tell whether answering
one answers the other. That is a presentation failure, not a data failure, and
so it is fixed here rather than by deleting a detector's work.

Three ideas, and the order matters:

**A subject is a thing, not a path prefix.** `Music/Pop/A Taste Of Honey/Best
Road Trip Disco Fever Classics` is one album folder; two findings naming it are
about the same album. Two findings that merely share `Music/Pop/` are not about
anything in common, and grouping them by prefix would invent a relationship.

**Precedence is a named rule, never a severity number.** `severity` exists to
sort a list; it cannot say that a correction outranks the observation it
explains. So the rules are written down, in words, below — and a kind that is
in none of them simply stays independent, which is the safe default.

**Subsumption is presentation only.** A subsumed finding keeps its row, its
evidence, its status and its own resolution. It moves inside a collapsed group;
it is not answered, closed, or merged. Approving the primary must never quietly
resolve something nobody looked at.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import PurePosixPath

# Which kind explains which. Read as: "if both are open about the same subject,
# the first one is the question worth asking, and the others are symptoms of it
# that its answer would address."
#
# Deliberately short, and deliberately not a rule about categories. Every entry
# here is a claim that one finding's correction makes another finding moot, and
# a wrong entry hides a decision from its owner.
#
# `artwork-not-on-disk` is the instructive absence. It sits on the same folder
# as `collection-custom` and looks like a candidate — but consolidating a
# compilation does not put a cover image beside the tracks. Two facts about one
# folder that happen to be neighbours are still two facts.
SUBSUMES: dict[str, frozenset[str]] = {
    # A verdict on what a multi-artist folder *is* explains complaints about
    # the folder's name and about tracks being scattered: its destination is
    # the answer to both.
    "collection-recognized": frozenset({"album-name-mismatch", "naming-outlier"}),
    "collection-custom": frozenset({"album-name-mismatch", "naming-outlier"}),
    "collection-loose": frozenset({"album-name-mismatch", "naming-outlier"}),
    # One album in several folders explains a folder whose name disagrees with
    # its tags: the tags belong to the album, and the album is elsewhere too.
    "split-album": frozenset({"album-name-mismatch", "naming-outlier"}),
    # A named duplicate pair explains a generic "named unlike its neighbours".
    "duplicate": frozenset({"naming-outlier"}),
}

# When several findings survive as decisions about one subject, this is the
# order they lead in. Identity first ("what is this?"), then structure ("where
# does it belong?"), then everything a detector merely noticed.
#
# A kind absent from this list sorts after every kind in it, by id, which keeps
# the order stable rather than arbitrary.
LEADS = (
    "collection-recognized",
    "collection-custom",
    "collection-loose",
    "split-album",
    "duplicate",
    "tag-path-mismatch",
    "artist-split",
    "album-name-mismatch",
    "track-numbering",
    "naming-cleanup",
    "naming-inconsistency",
    "naming-outlier",
    "loose-tracks",
    "catalog-name-mismatch",
    "artwork-not-on-disk",
    "missing-artwork",
    "unindexed",
    "system-junk",
)


@dataclass
class Subject:
    """One real-world thing, and every open finding about it."""

    key: str
    label: str
    primary: dict
    # Independent decisions about the same thing. Each keeps its own row, its
    # own actions and its own answer.
    related: list[dict] = field(default_factory=list)
    # Explained by the primary. Also full rows, also individually resolvable —
    # they are simply not a second top-level question.
    subsumed: list[dict] = field(default_factory=list)

    @property
    def others(self) -> list[dict]:
        return [*self.related, *self.subsumed]

    @property
    def count(self) -> int:
        return 1 + len(self.others)


def subject_key(row: sqlite3.Row) -> str:
    """The thing this finding is about.

    A folder finding is about its folder. A file finding is about its file —
    not about the folder it happens to sit in, because two unrelated files in
    one album are two unrelated problems, and a row that swallowed both would
    make approving one look like approving the other.
    """
    from librairy.audit import FOLDER_KINDS

    relpath = row["relpath"]
    if row["kind"] in FOLDER_KINDS:
        return f"folder:{relpath}"
    return f"file:{relpath}"


def subject_label(key: str) -> str:
    """What to call the group. The folder's or the file's own name.

    Never the whole path: the path is on every row inside, and a heading that
    repeats it is a heading nobody reads.
    """
    _, _, relpath = key.partition(":")
    return PurePosixPath(relpath).name or relpath


def _lead_rank(kind: str) -> int:
    try:
        return LEADS.index(kind)
    except ValueError:
        return len(LEADS)


def group(rows: list[dict]) -> list[Subject]:
    """Arrange rendered finding rows into one group per subject.

    Takes rows that have already been built for the template, so that nothing
    here reads the database or the filesystem — grouping is a decision about
    presentation and must not become another pass over the library.

    The primary is chosen by two rules in order: something that can actually be
    approved leads over something that cannot, and then the named order above
    decides. "This is a compilation, and here is where it goes" is a better
    thing to put in front of someone than "this folder has no cover image",
    even though both are true and both stay.
    """
    buckets: dict[str, list[dict]] = {}
    for row in rows:
        buckets.setdefault(str(row["subject_key"]), []).append(row)

    subjects = []
    for key, found in buckets.items():
        ordered = sorted(
            found,
            key=lambda row: (
                not row["can_approve"],
                _lead_rank(str(row["kind"])),
                int(row["id"]),
            ),
        )
        primary, rest = ordered[0], ordered[1:]
        explained = SUBSUMES.get(str(primary["kind"]), frozenset())
        subjects.append(
            Subject(
                key=key,
                label=subject_label(key),
                primary=primary,
                related=[row for row in rest if row["kind"] not in explained],
                subsumed=[row for row in rest if row["kind"] in explained],
            )
        )
    # By what the group is called, so the page reads in the same order twice
    # running. Sorting by severity would move rows as their evidence changed.
    return sorted(subjects, key=lambda subject: (subject.label.lower(), subject.key))
