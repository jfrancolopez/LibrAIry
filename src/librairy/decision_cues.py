"""What LibrAIry may read off a file, and what outranks a habit.

Two jobs, deliberately in one place so the ordering is written once:

* **cues** — the small, recurring facts a decision could sensibly be repeated
  from. A document's type and the organization that wrote it recur; the words
  on its second page do not, and are never stored. `invoice-82741.pdf` as an
  equality rule would match one file for ever.

* **authority** — when a learned habit must stay quiet. A pattern is the
  weakest thing in the program: it loses to a safety invariant, to a setting
  the owner configured, and to strong evidence about *this* file. Six Queen
  tracks filed under one release is a habit. MusicBrainz saying this recording
  belongs to another release is a fact about the file in front of you.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import PurePosixPath

from librairy.catalogs import CATALOGS_BY_SLUG
from librairy.decisions import DESTINATION, Cue, generalize, render
from librairy.naming import tidy_component

#  Evidence that answers the destination question about *this* file better
#  than any habit could. A catalog match is an identity, not a resemblance —
#  and an ISBN or a DOI is an identifier somebody printed in the document.
STRONG_SOURCES = frozenset(CATALOGS_BY_SLUG) | {"acoustid"}
STRONG_FIELDS = frozenset({"isbn", "doi"})

#  Cue names, so the extractor, the templates and the explanation agree.
CATEGORY = "category"
DOCUMENT_TYPE = "document_type"
ORGANIZATION = "organization"
AUTHOR = "author"
YEAR = "year"
FORMAT = "format"
FOLDER = "folder"
#  An explicit hint somebody wrote on the file or its folder. A cue like any
#  other, deliberately: this is how a tag comes to influence a *destination*
#  without becoming a second authority beside Decision Memory. Repeated
#  decisions about `#ProjectHouse` files teach an answer, and that answer is
#  offered and promotable exactly like every other learned one.
TAG = "tag"

#  Cues whose value can legitimately appear inside a destination and so may be
#  turned back into a placeholder. `category` may not: `Documents/...` contains
#  no category value, and a coincidence there would produce a template nothing
#  can fill.
TEMPLATE_CUES = (ORGANIZATION, AUTHOR, YEAR)


def features_for(row: sqlite3.Row) -> dict[str, str]:
    """The recurring facts about one proposal, in the words its evidence used.

    Read from the evidence the classifier already recorded — never by opening
    the file again, and never from anything the document *says*. A manual's
    manufacturer recurs across manuals; the serial number on page four does
    not, and storing it would put a fact about one machine into a rule.
    """
    features: dict[str, str] = {CATEGORY: str(row["category"] or "")}
    for entry in _evidence(row):
        source = str(entry.get("source") or "")
        field = str(entry.get("field") or "")
        detail = " ".join(str(entry.get("detail") or "").split())
        if source != "document" or not detail:
            continue
        if field == "type":
            features[DOCUMENT_TYPE] = detail
        elif field == "organization":
            features[ORGANIZATION] = detail
        elif field == "author":
            features[AUTHOR] = detail
    #  The year the destination itself used, when the policy has one. Taken
    #  from the path rather than re-derived, so the cue and the template it
    #  produces cannot disagree about which year was meant.
    year = _year_in(str(row["dest_relpath"] or ""))
    if year:
        features[YEAR] = year
    return {name: value for name, value in features.items() if value}


def cues_for(row: sqlite3.Row) -> list[Cue]:
    """The candidate patterns for this file, narrowest first.

    A ladder rather than one signature, because "Honda manuals go here" and
    "manuals go here" are both things somebody might have taught, and the
    narrower one has to be asked first. Every rung is a real subset of the
    cues, so a match at any of them is a match on facts this file actually has.
    """
    features = features_for(row)
    ladder: list[dict[str, str]] = []
    if DOCUMENT_TYPE in features and (ORGANIZATION in features or AUTHOR in features):
        who = ORGANIZATION if ORGANIZATION in features else AUTHOR
        ladder.append(
            {
                CATEGORY: features[CATEGORY],
                DOCUMENT_TYPE: features[DOCUMENT_TYPE],
                who: features[who],
            }
        )
    if DOCUMENT_TYPE in features:
        ladder.append(
            {CATEGORY: features[CATEGORY], DOCUMENT_TYPE: features[DOCUMENT_TYPE]}
        )
    #  A tag before a folder. Somebody who wrote `#ProjectHouse` said something
    #  more deliberate than "this arrived in a folder called scans", and the
    #  ladder is ordered by how specific a cue is rather than by where it came
    #  from — so the narrower claim is asked first.
    tag = _nearest_tag(row)
    if tag:
        ladder.append({CATEGORY: features[CATEGORY], TAG: tag})
    folder = _source_folder(str(row["item_relpath"] or ""))
    if folder:
        #  Where it arrived from. An import folder is something the person
        #  made, and "everything off this card went to Photos" is a real
        #  lesson — see `inbox_collections` for why the folder and not the
        #  timestamp.
        ladder.append({CATEGORY: features[CATEGORY], FOLDER: folder})
    return [Cue(DESTINATION, entry) for entry in ladder if entry]


def _nearest_tag(row: sqlite3.Row) -> str:
    """The most specific hashtag on this file, from its evidence.

    Read from the evidence the classifier recorded rather than from
    `item_tags`, for the same reason every other cue is: a cue has to describe
    what the person was looking at when they decided, and the durable store is
    what is true *now*. `extract_hashtags` writes them most-specific-first, so
    the first one is the nearest.
    """
    for entry in _evidence(row):
        if str(entry.get("source") or "") == "hashtag":
            detail = " ".join(str(entry.get("detail") or "").split())
            if detail:
                return detail
    return ""


def outcome_for(row: sqlite3.Row) -> str:
    """What this decision chose, as a template wherever the policy is one.

    The folder, not the filename: the lesson is *where files like this go*, and
    a filename is a fact about one file. Cue values in it become placeholders,
    so four statements from 2024 teach `Documents/Financial/{year}` rather than
    a drawer labelled with the wrong year in 2026.
    """
    destination = str(row["dest_relpath"] or "")
    if not destination:
        return ""
    folder = PurePosixPath(destination).parent.as_posix()
    if folder in (".", "/", ""):
        return ""
    features = features_for(row)
    return generalize(folder, _as_written(folder, features))


def _as_written(folder: str, features: dict[str, str]) -> dict[str, str]:
    """The cue values in the spelling the *path* actually uses.

    A destination is built through the filename sanitizer, so
    `Honda Motor Co.` becomes the folder `Honda Motor Co` — and matching the
    evidence's spelling against the path found nothing, which quietly turned
    a policy into the literal `Documents/Manuals/Honda Motor Co`. That is the
    stale-year bug wearing a different hat: it would file a Netgear manual
    under Honda.
    """
    found: dict[str, str] = {}
    for name in TEMPLATE_CUES:
        value = features.get(name)
        if not value:
            continue
        for spelling in _spellings(value):
            if len(spelling) >= 2 and spelling in folder:
                found[name] = spelling
                break
    return found


def _spellings(value: str) -> tuple[str, ...]:
    """How one cue value can legitimately appear in a path.

    Two: as written, and as the existing sanitizer would write it. Not a third
    — inventing spellings is how a substitution starts matching by accident.
    """
    safe = tidy_component(str(value))
    return (str(value), safe) if safe != str(value) else (str(value),)


def path_value(value: str) -> str:
    """The spelling a suggested path should use. The existing sanitizer, once."""
    return tidy_component(str(value or ""))


def destination_from(suggestion, row: sqlite3.Row) -> str:  # noqa: ANN001
    """The suggested folder with this file's own values in it, or "".

    Empty when the template needs a cue this file does not carry, which is the
    same as having no suggestion: a path with an unfilled placeholder in it is
    not a destination.
    """
    features = features_for(row)
    return render(
        suggestion.outcome,
        {name: path_value(value) for name, value in features.items()},
    )


def outranked(row: sqlite3.Row) -> str:
    """Why a learned suggestion must stay quiet about this file, or "".

    Strong current evidence beats a habit, always. A catalog identity, an ISBN
    or a DOI is a statement about *this* file; a pattern is a statement about
    files that resembled it. Returning the reason rather than a boolean so the
    row can say which fact won, if it ever needs to.
    """
    for entry in _evidence(row):
        source = str(entry.get("source") or "")
        field = str(entry.get("field") or "")
        if source in STRONG_SOURCES and str(entry.get("status") or "") != "no-match":
            return f"{source} identified this file"
        if field in STRONG_FIELDS:
            return f"this file carries an {field.upper()}"
    return ""


def _evidence(row: sqlite3.Row) -> list[dict]:
    try:
        payload = json.loads(str(row["evidence"] or "[]"))
    except (TypeError, ValueError):
        return []
    return [entry for entry in payload if isinstance(entry, dict)]


def _source_folder(relpath: str) -> str:
    """The inbox folder a file arrived in, or "" for a loose arrival.

    The same definition `inbox_collections` uses, and for the same reason: a
    folder is something the person made.
    """
    head, slash, _rest = str(relpath).replace("\\", "/").partition("/")
    return head if slash else ""


def _year_in(destination: str) -> str:
    for part in PurePosixPath(destination).parts:
        if len(part) == 4 and part.isdigit() and part[:2] in ("19", "20"):
            return part
    return ""
