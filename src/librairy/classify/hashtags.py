"""`#ProjectHouse` — the one thing in a filename somebody typed on purpose.

Every other signal LibrAIry reads is inferred: an extension suggests a kind, a
year in a name suggests a date, a catalog match suggests an identity. A hashtag
is different in kind. Nobody types `#Taxes2026` by accident, and nobody types it
about a file they were not thinking about — it is the closest thing in a
filesystem to being told.

So it is read from **everywhere a person can write it**:

    Inbox/#ProjectHouse/quotes/roof.pdf     an ancestor folder
    Inbox/scans/roof #ProjectHouse.pdf      the file's own name

The second was missed entirely. `extract_hashtags` read `parent.parts` and
nothing else, so `IMG_4421 #Vacation2026.jpg` carried no hint at all — which is
the way most people actually tag one file rather than a folder of them.

## Which one is "nearest", and why it is not luck

A file can carry several, and they can disagree about which is the *context*:

    Inbox/#ProjectHouse/receipts #Taxes2026/invoice #Roofing.pdf

`nearest` used to be `tags[0]` of the deepest folder that had any — first item
of a list, which is an accident of ordering rather than a rule. It is now
stated: **the most specific place wins, and within one name the first tag
written wins.** Specificity is the file's own name, then the deepest folder,
outward to the shallowest. Somebody who writes a tag on the file is saying
something about the file; somebody who writes one on a folder is saying
something about everything under it, and the file is closer.

**Every tag is kept, and every tag stays first-class.** `nearest` is a
tie-break, and only for the callers that genuinely need exactly one answer — a
photo group has one heading, and "which is *the* context" has to be decided by
a rule rather than by list order. It is not a ranking: the others are not
weaker evidence for having lost it. Every tag is stored, searchable, evidence
on the proposal, and its own rung of the cue ladder
(`decision_cues.cues_for`), so a rule about `#Taxes2026` is still found on a
file that also carries `#ProjectHouse`.

## What a hashtag is worth

Explicit user evidence: stronger than a habit and stronger than a model's
guess, and weaker than a fact about the file's identity. It counts **now**, in
the decision being made, and not only once enough decisions have been watched
to learn something about it — those are two different facts and the program
keeps both. See `librairy/tags.py`.

It is a statement about *context*, not about content — `#ProjectHouse` on a
`.exe` does not make the executable a house document, and nothing here lets a
tag pick a category or name a destination on its own.

Where it becomes durable, searchable and promotable to a Project is
`librairy/tags.py`. This module only reads names.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from librairy.models import EvidenceEntry
from librairy.paths import sanitize_component

TAG_RE = re.compile(r"#([^\s#/\\]+)")

#  Where a tag was written. Kept because "you tagged this file" and "it is in a
#  tagged folder" are different claims, and the second is inherited by
#  everything beneath it.
FILENAME = "filename"
FOLDER = "folder"
MANUAL = "manual"


@dataclass(frozen=True)
class Tag:
    """One hashtag, as written and as matched."""

    #  Normalised: lowercase and hyphenated, so `#ProjectHouse`,
    #  `#projecthouse` and `#Project House` are one tag.
    tag: str
    #  As somebody wrote it, for the places that show it back to them.
    label: str
    source: str
    #  Which folder, or the filename it came from. Provenance a person can
    #  check without hunting for the file.
    detail: str = ""


@dataclass(frozen=True)
class HashtagHints:
    tags: tuple[str, ...]
    nearest: str | None
    evidence: tuple[EvidenceEntry, ...]
    #  Every tag with where it came from. `tags` above stays a tuple of plain
    #  strings because several callers already treat it as one.
    found: tuple[Tag, ...] = field(default_factory=tuple)


def extract_hashtags(relpath: str) -> HashtagHints:
    """Every hashtag on this path, most specific first.

    Ordered rather than merely collected: the first element is the tag from the
    most specific place a person wrote one, which is what makes `nearest`
    a rule instead of an accident of list order.
    """
    path = PurePosixPath(relpath)
    #  The file's own name first, then folders from the deepest outwards. The
    #  suffix is dropped so a tag is never read out of `.tar#gz` nonsense.
    places: list[tuple[str, str, str]] = [(FILENAME, path.stem, path.name)]
    places.extend(
        (FOLDER, folder, folder) for folder in reversed(path.parent.parts)
    )

    found: list[Tag] = []
    seen: set[str] = set()
    for source, text, detail in places:
        for written in TAG_RE.findall(text):
            tag = _sanitize_tag(written)
            if not tag or tag in seen:
                continue
            seen.add(tag)
            found.append(Tag(tag, written, source, detail))

    return HashtagHints(
        tags=tuple(item.tag for item in found),
        #  Deterministic: the most specific place, and within one name the
        #  first tag written. Never `tags[0]` of whatever list came back.
        nearest=found[0].tag if found else None,
        evidence=tuple(
            #  Explicit user evidence. Above a learned habit and above a
            #  model's guess, below anything that identifies the file — see
            #  `docs/architecture/decision-memory.md`.
            EvidenceEntry("hashtag", "tag", item.label, 0.9, note=_note(item))
            for item in found
        ),
        found=tuple(found),
    )


def _note(item: Tag) -> str:
    if item.source == FILENAME:
        return "you tagged this file"
    return f"in the folder {item.detail}"


def strip_hashtags(value: str) -> str:
    return TAG_RE.sub("", value).replace("#", "").strip()


def strip_hashtags_from_relpath(relpath: str) -> str:
    raw_parts = PurePosixPath(relpath).parts
    parts: list[str] = []
    for index, part in enumerate(raw_parts):
        stripped = strip_hashtags(part).strip()
        if index == len(raw_parts) - 1:
            suffix = PurePosixPath(part).suffix
            if suffix and stripped and not stripped.endswith(suffix):
                stripped = f"{stripped}{suffix}"
        parts.append(stripped)
    return PurePosixPath(*[part for part in parts if part]).as_posix()


def _sanitize_tag(tag: str) -> str:
    tag = tag.replace("/", " ").replace("\\", " ").replace(".", " ")
    try:
        return sanitize_component(tag).lower().replace(" ", "-")
    except ValueError:
        return ""
