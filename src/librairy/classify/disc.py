"""A ripped disc is one thing, not forty files.

A DVD folder arrives as VIDEO_TS.IFO, VIDEO_TS.BUP, VTS_01_0.IFO, a string of
VTS_01_n.VOB and so on. Classified one by one they are unidentifiable — nothing
about "VTS_01_3.VOB" says what film it is — so every one of them scored 0.3 as
misc, got no destination, and sat in Review forever. Nine files, one question,
and no way to answer it.

Two things fix that. The disc's identity is in the *folder above* the disc
directory, which is the only place anybody wrote the title down. And the names
inside it must survive the move untouched: they are a contract with a player,
not a description of anything, so a tidied VIDEO_TS folder is a folder that no
longer plays. `tidy_relpath` protects the second half; this module supplies the
first.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from librairy.config import Settings
from librairy.models import EvidenceEntry
from librairy.naming import DISC_DIRECTORIES
from librairy.taxonomy import render_destination

#  "DVD5", "DVD-9", "Disc 2", "CD1" — how the medium was written down, which is
#  not part of what the thing is called.
MEDIUM = re.compile(
    r"(?i)[\s._-]*\b(dvd[\s._-]?[59rw]?|bluray|blu-ray|bdmv|iso|ntsc|pal|"
    r"disc[\s._-]?\d+|cd[\s._-]?\d+)\b[\s._-]*$"
)
ISO_DATE = re.compile(r"\b(19\d{2}|20\d{2})-\d{2}-\d{2}\b")
YEAR = re.compile(r"\b(19\d{2}|20\d{2})\b")
DISC_CONFIDENCE = 0.82


@dataclass(frozen=True)
class DiscPart:
    """One file inside a disc structure, and the disc it belongs to."""

    title_folder: str
    inner_relpath: str

    @property
    def disc_directory(self) -> str:
        return self.inner_relpath.split("/", 1)[0]


@dataclass(frozen=True)
class DiscClassification:
    category: str
    clean_name: str
    dest_relpath: str | None
    confidence: float
    evidence: tuple[EvidenceEntry, ...]
    fields: dict[str, object]
    group_key: str | None = None
    reason: str | None = None


def disc_part(relpath: str) -> DiscPart | None:
    """Split a path at its disc directory, or None if there isn't one.

    Only a directory counts. A stray VIDEO_TS.IFO sitting in a downloads folder
    is just a file, and treating it as a disc would invent a movie out of
    whatever folder happened to contain it.
    """
    parts = PurePosixPath(relpath.replace("\\", "/")).parts
    for index, part in enumerate(parts[:-1]):
        if part.upper() in DISC_DIRECTORIES:
            return DiscPart(
                title_folder=parts[index - 1] if index else "",
                inner_relpath="/".join(parts[index:]),
            )
    return None


def disc_title(folder: str) -> tuple[str, int]:
    """The disc's name and year, from the folder somebody typed it into.

    Deliberately not `parse_video_name`: that truncates the title at the first
    year, which turns "Queen - 1979-12-26 - The Queen Special on TV" into
    "Queen". A disc folder is usually a whole sentence and the year is in the
    middle of it.
    """
    name = MEDIUM.sub("", folder).strip(" ._-")
    year = 0
    iso = ISO_DATE.search(name)
    if iso:
        year = int(iso.group(1))
        name = name.replace(iso.group(0), " ")
    else:
        found = YEAR.search(name)
        if found:
            year = int(found.group(1))
            # The template puts the year back on the end. Leaving it in the
            # title as well gave "The-Matrix-(1999)-(1999)".
            name = re.sub(rf"[\s._-]*\(?{year}\)?[\s._-]*$", " ", name)
    name = re.sub(r"[\s._-]+", " ", name).strip(" -")
    return name, year


def classify_disc(relpath: str, *, settings: Settings) -> DiscClassification | None:
    part = disc_part(relpath)
    if part is None:
        return None
    title, year = disc_title(part.title_folder)
    if not title:
        # A disc directory with nothing above it to name it. Better to leave it
        # in Review unfiled than to invent a title for a whole folder of files.
        return None
    fields: dict[str, object] = {
        "title": title,
        "year": year,
        "genre": "General",
        # The whole structure below the disc directory, kept verbatim by
        # tidy_relpath — this is one proposal that happens to carry a folder.
        "clean_name": part.inner_relpath,
    }
    rendered = render_destination(
        "movies", fields, library_root=settings.library_dir, conn=None
    )
    return DiscClassification(
        "movies",
        part.inner_relpath,
        rendered.relpath,
        DISC_CONFIDENCE,
        (
            EvidenceEntry(
                "heuristic",
                "disc",
                f"{part.disc_directory} structure under “{part.title_folder}”",
                DISC_CONFIDENCE,
            ),
        ),
        fields,
        group_key=f"disc:{part.title_folder}",
        reason=rendered.reason,
    )
