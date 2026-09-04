"""When several documents are one decision, and — mostly — when they are not.

Every other medium got this in M1-02. An album is twelve tracks, a season is
ten episodes, a camera card is two hundred photographs, and each is *one*
question. Documents got nothing, because a book is none of those things: the
group grid M1-03 built for them was a page no workflow could reach, and M1-03
has been sitting at PARTIAL ever since saying so.

The reason it sat there is that the hard part is not the grid. It is having a
**defensible reason** for two documents to be one decision. A wrongly grouped
set is worse than an ungrouped one — M1-04 — because a heading is trusted, and
a heading over eleven unrelated PDFs with one Approve button under it is the
most expensive mistake this program could make.

## The test a group has to pass

    Would approving this once make sense?

Not "are these similar". Not "did these arrive together". If the members
plausibly need different destinations, different identity treatment or a
different naming policy, they are not one decision, and the honest answer is
several rows.

## The three reasons, and what each one rests on

    book_series    one title in parts or editions — an explicit volume, part
                   or edition marker over an identical stem
    document_set   the same organization and the same kind of document — a
                   shelf of Honda manuals
    tagged_set     the same explicit tag and the same kind of document — the
                   owner saying so, narrowed to one kind of thing

Each is stored on the group as a sentence, so a heading can always say what
makes it one decision.

**The kind of document has to say more than the category already did**, and
that guard is the one that turned out to matter. "The same kind of document"
looked sufficient for a tagged set until it put a boiler manual and a novel
under one heading: both `.epub`, so both typed `Book`, so the condition was met
by a fact neither file had any choice about. Two *invoices* tagged
`#ProjectHouse` are a set. Two *books* are two books.

A year of bank statements is the case the roadmap named and the one this does
not reach yet: `document_set` needs an organization, and nothing reads a bank's
name off a statement — the classifier extracts one for manuals only. Tagging
them arrives at the same place through `tagged_set`.

**What is deliberately not a reason.** A category. A folder. A classifier that
said "Programming" about eleven unrelated books. And, on its own, **arrival**:
files dropped in together are related more often than not, and "more often than
not" is exactly the standard that produces a wrong heading. Arrival is recorded
as *corroboration* when a group formed for one of the reasons above and its
members also came in together — it strengthens the sentence, and it never
writes one.

## What this is not

`document_works.py` already answers *one work, several containers* — `Dune.epub`
beside `Dune.pdf`, matched on a shared ISBN or DOI, offered as a comparison
where keeping both is a first-class answer. That question is not this one and
the two must not merge:

    same work, several formats   → a comparison. `document_works`
    several works, one set       → a decision group. here

So a shared ISBN is never a key here. Two files with one ISBN are one book in
two containers, and turning them into a "group" would quietly reframe a
keep-both question as an approve-once one.

## Conflict weakens, and a real one prevents

A document whose sources disagree about what it is (`document_identity`) forms
no key at all. Its identity is the open question, and folding it into a group
would answer that question by putting it under somebody else's heading —
losing the disagreement in the exact place a reader would stop looking for it.

With one line drawn through the middle of that, without which the rule would be
useless: **the filename dissenting alone is not a real conflict.** `pr2.epub`
whose metadata says `Programming Rust, 2nd Edition` is contested by M2-02, and
rightly — that comparison is how `CRACKING.pdf` was caught. But an abbreviated
filename is the ordinary case for an ebook, and treating it as a disagreement
about the document made every set of them ungroupable for the one reason that
says least about the files. Those members group, keep the lowered confidence
M2-02 gave them, and show up in the group's "to look at" count. Anything that
actually read the document disagreeing still prevents.

See `docs/ROADMAP.md` M2-06, and M1-03 for the face this finally reaches.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass

from librairy.live import LIVE, live
from librairy.models import EvidenceEntry
from librairy.planner import utc_now

#  Why these files are one decision. The group's `kind`, so the reason is in
#  the data rather than in a sentence somebody has to parse back out of it.
SERIES = "book_series"
SET = "document_set"
TAGGED = "tagged_set"

KINDS = (SERIES, SET, TAGGED)

#  The only categories this looks at. Everything else already has a grouping
#  rule that works, and a second one reaching into it would be two answers to
#  one question.
CATEGORIES = ("documents", "books")

#  How many members make a group. Two: one document is a row, and a heading
#  over a single file is a heading over nothing.
MIN_MEMBERS = 2

#  A group forms out of decisions somebody still has to make. A proposal that
#  was approved or committed is an answer, and counting it here would build a
#  heading over a set of files that are already filed — one live document
#  joining eight settled ones is not a group of nine, it is one document.
#
#  It keeps its `group_id` afterwards, like an album's tracks do: what a group
#  *was* is how Commit and History explain what happened.
UNDECIDED = ("proposed", "postponed", "pending")

#  `Vol. 2`, `Volume II`, `Book 3`, `Part Two`, `2nd Edition`. An explicit
#  marker and nothing looser — a trailing number would make `Report 2024` and
#  `Report 2025` a series, and they are a set at best.
_VOLUME = re.compile(
    r"(?i)\b(?:vol(?:ume)?\.?|book|part|pt\.?)\s*"
    r"(\d{1,3}|[ivxlcdm]{1,6}|one|two|three|four|five|six|seven|eight|nine|ten)\b"
)
_EDITION = re.compile(r"(?i)\b(\d{1,2})(?:st|nd|rd|th)\s+ed(?:ition)?\b")

#  A stem that names nothing. `Manual Vol. 2` and `Manual Vol. 3` are two files
#  called Manual, not a series — the word is the *type*, which every document
#  of that type shares, so it would gather the whole shelf under one heading.
_EMPTY_STEMS = frozenset(
    {
        "book",
        "document",
        "documents",
        "doc",
        "file",
        "manual",
        "notes",
        "paper",
        "report",
        "scan",
        "scanned",
        "untitled",
        "volume",
    }
)

#  A type that only restates the category, per category. `Document` is what
#  `docmeta` says when nothing identified the kind at all; `Book` is what it
#  says about every book, which is the same amount of information about a set
#  of them.
#
#  This is the guard that keeps a Project from becoming a heading. "Books
#  tagged #ProjectHouse" grouped a boiler manual with a novel the first time
#  this ran — both epubs, both typed `Book`, so "the same kind of document" was
#  satisfied by a fact neither of them had any choice about. A type earns a
#  group only when it says more than the category already did: two *invoices*
#  tagged `#ProjectHouse` are a set, two *books* are two books.
_GENERIC_TYPE = {"documents": "Document", "books": "Book"}

#  How the corroboration reads, and the marker that keeps re-analysis from
#  appending it a second time.
_ARRIVED = ", and they all arrived in "


@dataclass(frozen=True)
class Candidate:
    """Why this document might belong with others, if any others turn up."""

    kind: str
    #  What two documents have to share. Normalised, and never shown.
    key: str
    #  The heading, in the words the documents used.
    label: str
    #  The sentence under it. Written here because here is where the reason is
    #  actually known — a page reconstructing it later would be guessing.
    reason: str


def candidate(
    category: str, evidence: list[EvidenceEntry] | tuple[EvidenceEntry, ...]
) -> Candidate | None:
    """The strongest reason this document could be part of a set, or None.

    Reads the evidence analysis already wrote — the same evidence the row
    prints and Why shows — and opens nothing. None is the ordinary answer and
    the safe one: a document with no reason to be grouped is a row, exactly as
    it has always been.
    """
    if category not in CATEGORIES:
        return None
    facts = _facts(evidence)
    if _really_contested(evidence):
        #  Its sources disagree about what it *is*. That is the question in
        #  front of the reader, and a group heading is where a question goes to
        #  stop being noticed.
        return None
    kind = facts.get("type", "")
    title = facts.get("title", "")
    for found in (
        _series(category, title),
        _set(category, kind, facts.get("organization", "")),
        _tagged(category, kind, facts.get("tag", "")),
    ):
        if found is not None:
            return found
    return None


def _really_contested(
    evidence: list[EvidenceEntry] | tuple[EvidenceEntry, ...],
) -> bool:
    """Do the sources disagree about the document, or only with its filename?

    Not the same question, and treating them as one would have made this rule
    useless. `programming-rust-2e.epub` whose metadata says
    `Programming Rust, 2nd Edition` is *contested* by `document_identity` — the
    filename dissents, and M2-02 is right to ask about it, because that is how
    `CRACKING.pdf` was caught. But an abbreviated filename is the ordinary case
    for an ebook, and every set of them would be ungroupable for the one reason
    that says least about what the files are.

    So: the filename dissenting alone weakens — the member keeps the lowered
    confidence M2-02 gave it and shows up in the group's "to look at" count.
    Anything that actually read the document disagreeing prevents. See
    `librairy/document_identity.py` for which sources those are.
    """
    dissent = {
        entry.field.removeprefix("title/")
        for entry in evidence
        if entry.source == "document"
        and entry.field.startswith("title/")
        and entry.note == "disagrees"
    }
    return bool(dissent - {"filename"})


def _series(category: str, title: str) -> Candidate | None:
    #  The marker is what proves there is a series; the *stem* is what two
    #  volumes share, and only the stem is kept. A sentence built from the
    #  marker would say "this one is 2nd Edition" under a heading, which is a
    #  claim about the group and is wrong for every other member of it.
    stem = split_volume(title)[0]
    if not stem:
        return None
    return Candidate(
        SERIES,
        f"{SERIES}|{category}|{_normal(stem)}",
        stem,
        "one title in several parts or editions",
    )


def _set(category: str, kind: str, organization: str) -> Candidate | None:
    if not organization or not _informative(category, kind):
        return None
    return Candidate(
        SET,
        f"{SET}|{category}|{_normal(organization)}|{_normal(kind)}",
        f"{kind}s from {organization}",
        "the same kind of document, from one organization",
    )


def _tagged(category: str, kind: str, tag: str) -> Candidate | None:
    if not tag or not _informative(category, kind):
        return None
    #  The type is required, and it is the whole of why this is safe. A Project
    #  holds invoices, photographs, manuals and permits — that is what a
    #  project *is* — and grouping on the tag alone would put all four under
    #  one heading with one Approve button. See `librairy/tags.py`.
    return Candidate(
        TAGGED,
        f"{TAGGED}|{category}|{_normal(tag)}|{_normal(kind)}",
        f"{kind}s tagged #{tag}",
        "you tagged these, and they are the same kind of document",
    )


def _informative(category: str, kind: str) -> bool:
    """Does this document type say more than its category already did?"""
    return bool(kind) and kind != _GENERIC_TYPE.get(category, "")


def split_volume(title: str) -> tuple[str, str]:
    """`("Programming Rust", "2nd Edition")`, or `("", "")`.

    The stem is what two volumes of one work share. It has to survive on its
    own: a marker over `Manual` says the file is the second manual of
    something, not the second volume of a work called Manual.
    """
    for pattern in (_VOLUME, _EDITION):
        found = pattern.search(title)
        if found is None:
            continue
        stem = _tidy(title[: found.start()] + " " + title[found.end() :])
        if _names_something(stem):
            return stem, _tidy(found.group(0))
    return "", ""


def _names_something(stem: str) -> bool:
    words = [word for word in re.split(r"\W+", stem.lower()) if word]
    if not words or all(word in _EMPTY_STEMS for word in words):
        return False
    return any(len(word) >= 3 for word in words)  # noqa: PLR2004


def _tidy(value: str) -> str:
    #  Punctuation left behind by cutting a marker out of the middle: the
    #  dash in `Programming Rust — Vol. 2` belonged to the marker.
    return re.sub(r"\s+", " ", value).strip(" -–—:,.·|").strip()


def _normal(value: str) -> str:
    return " ".join(str(value).lower().split())


def _facts(
    evidence: list[EvidenceEntry] | tuple[EvidenceEntry, ...],
) -> dict[str, str]:
    """The fields a key can be built from, off the stored evidence.

    The chosen title and not every title: a document whose sources disagreed
    has already been excluded, and among sources that agree the chosen one is
    the wording the rest of the program uses.
    """
    facts: dict[str, str] = {}
    for entry in evidence:
        detail = " ".join(str(entry.detail).split())
        if not detail:
            continue
        if entry.source == "document":
            if entry.field.startswith("title/"):
                if entry.note == "chosen":
                    facts["title"] = detail
            elif entry.field in ("type", "organization", "conflict"):
                facts[entry.field] = detail
        elif entry.source == "hashtag" and entry.field == "tag":
            #  The first, which `extract_hashtags` writes as the nearest — the
            #  tag written closest to this file. Every tag stays evidence and
            #  a cue; a *heading* needs one name, and picking it by a rule is
            #  the same choice `nearest` exists to make.
            facts.setdefault("tag", detail)
    return facts


# --- publishing ------------------------------------------------------------------


def group_documents(conn: sqlite3.Connection, item_ids: list[int]) -> int:
    """Turn the candidate keys in this batch into groups. Returns how many.

    After the analysis loop rather than inside it, for the reason
    `associate_companions` runs there: a set is a fact about several files, and
    no file knows whether it is the second of anything until the others have
    been looked at. Running per item would make the first document of every set
    a group of one.

    Bounded by the batch. It asks only about keys this batch produced, so a
    library of four hundred thousand documents costs the same as one of four —
    the keys are indexed, and nothing here reads a member row.
    """
    keys = _keys_in(conn, item_ids)
    if not keys:
        return 0
    undecided = ",".join("?" for _ in UNDECIDED)
    made = 0
    for key, label, kind, reason in _shared(conn, keys):
        group_id = _ensure_group(conn, kind, label, reason, _dest_base(conn, key))
        conn.execute(
            f"""
            UPDATE proposals SET group_id = ?
            WHERE group_key = ? AND group_id IS NULL AND status IN ({undecided})
              AND item_id IN (SELECT id FROM items WHERE {LIVE})
            """,  # noqa: S608 - placeholders are counted; `LIVE` is a constant
            (group_id, key, *UNDECIDED),
        )
        _note_arrival(conn, group_id)
        made += 1
    return made


def _note_arrival(conn: sqlite3.Connection, group_id: int) -> None:
    """Add "and they arrived together" to a reason that already stood up.

    Corroboration, written only onto a group a real relationship already
    earned. It cannot create one, it cannot keep one alive, and turning it off
    changes no membership — which is the whole difference between evidence and
    authority.
    """
    folder = arrived_together(conn, group_id)
    if not folder:
        return
    row = conn.execute(
        "SELECT reason FROM groups WHERE id=?", (group_id,)
    ).fetchone()
    reason = str(row["reason"] or "") if row else ""
    if not reason or _ARRIVED in reason:
        return
    conn.execute(
        "UPDATE groups SET reason=? WHERE id=?",
        (f"{reason}{_ARRIVED}{folder}", group_id),
    )


def _keys_in(conn: sqlite3.Connection, item_ids: list[int]) -> list[str]:
    if not item_ids:
        return []
    placeholders = ",".join("?" for _ in item_ids)
    return [
        str(row["group_key"])
        for row in conn.execute(
            "SELECT DISTINCT group_key FROM proposals "  # noqa: S608 - placeholders only
            f"WHERE item_id IN ({placeholders}) AND group_key IS NOT NULL"
            " AND status != 'superseded'",
            item_ids,
        )
    ]


def _shared(
    conn: sqlite3.Connection, keys: list[str]
) -> list[tuple[str, str, str, str]]:
    """The keys at least two live, undecided documents actually share.

    The label and the reason come out of the same query rather than being
    rebuilt: they were written when the evidence was read, and re-deriving them
    here would describe today's rules rather than the ones the documents were
    read under.
    """
    placeholders = ",".join("?" for _ in keys)
    undecided = ",".join("?" for _ in UNDECIDED)
    found: list[tuple[str, str, str, str]] = []
    for row in conn.execute(
        f"""
        SELECT p.group_key, MIN(p.group_hint) AS hint, COUNT(*) AS members
        FROM proposals p JOIN items i ON i.id = p.item_id AND {live()}
        WHERE p.group_key IN ({placeholders}) AND p.status IN ({undecided})
        GROUP BY p.group_key
        HAVING members >= ?
        """,  # noqa: S608 - placeholders are counted; `live()` is a constant
        (*keys, *UNDECIDED, MIN_MEMBERS),
    ):
        hint = _hint(row["hint"])
        if hint:
            found.append(
                (str(row["group_key"]), hint["label"], hint["kind"], hint["reason"])
            )
    return found


def hint_for(found: Candidate) -> str:
    """The candidate as one stored column, beside the key that indexes it.

    The words and not only the key: `book_series|books|programming rust` is
    what two files have to share, and `Programming Rust` is what the heading
    says. Rebuilding the second from the first would print a normalised key at
    somebody, and re-deriving it at group time would describe today's rules
    rather than the ones these documents were read under.
    """
    return json.dumps(
        {"kind": found.kind, "label": found.label, "reason": found.reason},
        sort_keys=True,
    )


def _hint(payload: object) -> dict[str, str]:
    try:
        found = json.loads(str(payload or "{}"))
    except (TypeError, ValueError):
        return {}
    if not isinstance(found, dict) or not found.get("label"):
        return {}
    return {
        "kind": str(found.get("kind") or SET),
        "label": str(found["label"]),
        "reason": str(found.get("reason") or ""),
    }


def _dest_base(conn: sqlite3.Connection, key: str) -> str | None:
    """The folder most of this group is going to, or None when there is no most.

    A strict majority, not unanimity. Unanimity was the first answer and it was
    backwards: `groups.dest_base` is what the outlier split reads — a member
    whose destination is not under it is *not going where the group is going* —
    so letting one dissenting member erase the base for everybody switches off
    the split in exactly the case it exists for. Nine Honda manuals filed under
    `Documents/Manuals/Honda Motor Co/` and one heading for `Documents/2025/`
    is one file worth looking at, and it was invisible.

    None when nothing has a majority, which is the ordinary answer for a book
    series: each volume has its own folder, so "not going where the group is
    going" is not an anomaly there, it is the design.

    Two rows, aggregated in SQLite and never read into Python: `rtrim(path,
    replace(path, '/', ''))` strips the trailing basename, which is a folder
    name without a `dirname` function to call. Bounded whatever the group
    holds, and it runs once when a group is made rather than once per render —
    the window-per-page shape M1-01 removed is not this.
    """
    rows = conn.execute(
        "SELECT rtrim(dest_relpath, replace(dest_relpath, '/', '')) AS base,"  # noqa: S608
        " COUNT(*) AS members FROM proposals"
        " WHERE group_key = ?"
        f" AND status IN ({','.join('?' for _ in UNDECIDED)})"
        " AND dest_relpath IS NOT NULL AND dest_relpath LIKE '%/%'"
        " GROUP BY base ORDER BY members DESC, base LIMIT 2",
        (key, *UNDECIDED),
    ).fetchall()
    if not rows:
        return None
    if len(rows) > 1 and int(rows[0]["members"]) == int(rows[1]["members"]):
        return None
    return str(rows[0]["base"]).rstrip("/") or None


def _ensure_group(
    conn: sqlite3.Connection, kind: str, label: str, reason: str, dest_base: str | None
) -> int:
    row = conn.execute(
        """
        SELECT id FROM groups
        WHERE kind=? AND label=? AND COALESCE(dest_base, '')=COALESCE(?, '')
        """,
        (kind, label, dest_base),
    ).fetchone()
    if row is not None:
        return int(row["id"])
    cursor = conn.execute(
        "INSERT INTO groups(kind, label, dest_base, reason, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (kind, label, dest_base, reason, utc_now()),
    )
    return int(cursor.lastrowid)


def arrived_together(conn: sqlite3.Connection, group_id: int) -> str:
    """The inbox folder every member came in from, or "".

    Corroboration and never a reason. Files dropped in together are related
    more often than not, and "more often than not" is the standard that writes
    a wrong heading — so this can strengthen a sentence a real relationship
    already earned, and can never write one.
    """
    folders = {
        str(row["relpath"]).split("/")[0] if "/" in str(row["relpath"]) else ""
        for row in conn.execute(
            f"""
            SELECT i.relpath FROM proposals p
            JOIN items i ON i.id = p.item_id AND {live()}
            WHERE p.group_id = ? AND i.root = 'inbox' AND p.status != 'superseded'
            LIMIT 200
            """,  # noqa: S608 - `live()` is a module constant
            (group_id,),
        )
    }
    if len(folders) != 1:
        return ""
    only = folders.pop()
    return only if only else ""
