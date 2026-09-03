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
#  An explicit hint somebody wrote on the file or its folder. A cue like every
#  other in shape, and unlike every other in where it came from: the rest are
#  inferred off the file, and this one was typed by a person about this file.
#  That is why it is asked *first* — see `cues_for`.
TAG = "tag"

#  How many of a file's tags become cues. Every tag is kept, searchable, and
#  evidence in its own right; this bounds only how many rungs one row adds to
#  the page's tally. A file carrying nine tags has a context that is not one
#  thing, and the three written closest to it are the ones about it.
TAG_CUES = 3

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
    #  Every rung is scoped by category, so a lesson about documents says
    #  nothing about music. A row with no category has no rung it could be
    #  scoped by, and an unscoped one would be exactly that mistake.
    category = features.get(CATEGORY, "")
    if not category:
        return []
    ladder: list[dict[str, str]] = []
    #  Tags first, at every width. `suggest` breaks a tie between two rungs of
    #  the same specificity by the order they arrive in, and this is the whole
    #  of "an explicit hint outranks an inferred one": what somebody wrote on
    #  *this* file is asked before what LibrAIry worked out about files that
    #  looked like it. Not a new authority — the same rung of the same ladder,
    #  asked in the right order.
    written = _tags(row)
    for tag in written:
        if DOCUMENT_TYPE in features:
            ladder.append(
                {
                    CATEGORY: category,
                    TAG: tag,
                    DOCUMENT_TYPE: features[DOCUMENT_TYPE],
                }
            )
    if DOCUMENT_TYPE in features and (ORGANIZATION in features or AUTHOR in features):
        who = ORGANIZATION if ORGANIZATION in features else AUTHOR
        ladder.append(
            {
                CATEGORY: category,
                DOCUMENT_TYPE: features[DOCUMENT_TYPE],
                who: features[who],
            }
        )
    #  **Every** tag, not only the nearest. `nearest` decides which is *the*
    #  context for the one caller that needs a single answer; it was never
    #  meant to make the others weaker evidence, and a rule about `#Taxes2026`
    #  has to be findable on a file that also carries `#ProjectHouse`.
    ladder.extend({CATEGORY: category, TAG: tag} for tag in written)
    if DOCUMENT_TYPE in features:
        ladder.append({CATEGORY: category, DOCUMENT_TYPE: features[DOCUMENT_TYPE]})
    folder = _source_folder(str(row["item_relpath"] or ""))
    if folder:
        #  Where it arrived from. An import folder is something the person
        #  made, and "everything off this card went to Photos" is a real
        #  lesson — see `inbox_collections` for why the folder and not the
        #  timestamp.
        ladder.append({CATEGORY: category, FOLDER: folder})
    return [Cue(DESTINATION, entry) for entry in ladder if entry]


def _tags(row: sqlite3.Row) -> list[str]:
    """The hashtags on this file, most specific first, from its evidence.

    Read from the evidence the classifier recorded rather than from
    `item_tags`, for the same reason every other cue is: a cue has to describe
    what the person was looking at when they decided, and the durable store is
    what is true *now*. `extract_hashtags` writes them most-specific-first, so
    the order here is that order.
    """
    found: list[str] = []
    for entry in _evidence(row):
        if str(entry.get("source") or "") != "hashtag":
            continue
        if str(entry.get("field") or "") != "tag":
            #  A `project` entry names the Project the file joined, which is
            #  the same tag said a second way. One cue, not two.
            continue
        detail = " ".join(str(entry.get("detail") or "").split())
        if detail and detail not in found:
            found.append(detail)
    return found[:TAG_CUES]


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
