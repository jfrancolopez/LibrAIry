"""Is this music video where a music video goes?

Three questions, and only one of them has an automatic answer.

* **A music video filed as a film.** For years `.mp4` plus a dash in the name
  meant "movie" to the classifier and nothing said otherwise, so a collection
  that predates `classify/musicvideos.py` has music videos under `Movies/`. One
  file, one move, and the plan has always been able to express that.
* **A phone clip under `Music Videos/`.** It arrived with a folder somebody
  dragged in. Where it belongs depends on when it was taken and what it is of,
  and choosing a year and an event is a decision about somebody's holiday
  rather than a filing rule. Reported, never corrected.
* **A name nobody can read, in a folder that does not say either.** There is no
  artist to file it under, and inventing one produces a directory that outlives
  the guess.

**What this deliberately does not do is restyle filenames.** A hand-made
collection spells things the way a person spelled them, and LibrAIry's naming
policy turns every space into a dash — so a detector that compared each file
against its canonical form would propose to rewrite an entire library, which is
the exact mistake `naming.py` documents at length. Filing a *new* file applies
the naming policy, because there is no existing name to respect. Auditing an old
one asks whether it is in the right place, not whether it is spelled the house
way. So a move here **keeps the filename it found** and changes only the folder.

That leaves one asymmetry worth stating plainly: LibrAIry's own naming policy
removes the ` - ` between artist and title, so a file it filed itself cannot be
re-parsed afterwards. The artist folder is what identifies it instead, and a
file whose name begins with its own artist folder is treated as consistent
rather than unreadable. Changing the naming policy is a separate decision with
a much larger blast radius; `test_music_video_paths.py` records it.

Nothing here looks at a frame. `classify/video_vision.py` explains why at
length; the short version is that a performer on a stage is equally consistent
with a family video, a concert bootleg and a music video, and only one of those
has an architecture.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

from librairy.classify.musicvideos import FALLBACK_GENRE, read
from librairy.models import EvidenceEntry
from librairy.musicvideo import parse

# The one folder this module is about, spelled the way the destination template
# spells it. Compared exactly: a differently spelled folder is a naming finding
# for the naming detector, not a music-video finding.
ROOT = "Music Videos"

# Where a music video most often is when it is in the wrong place. Restricted on
# purpose — reading one in `Photos/` as a music video would more likely mean the
# reading is wrong than the filing is.
MISFILED_ROOTS = ("Movies", "Shows")

VIDEO_SUFFIXES = frozenset({".mp4", ".mkv", ".mov", ".avi", ".m4v", ".webm"})

#  A phone names its own files, and it is the only naming convention here that
#  is machine-generated. Shared in spirit with `classify/heuristics.CAMERA_RE`;
#  spelled separately because that one decides a category from a name and this
#  one disbelieves a folder.
CAMERA_RE = re.compile(r"^(IMG|DSC|DSCN|DSCF|PIC|PICT|GOPR|DJI|MVIMG)[-_]?\d", re.I)


def detect(view) -> list:
    """Every music-video finding in this view, in one pass over the files."""
    from librairy.audit import Finding

    on_disk = set(view.files)
    folders = _artist_folders(view.files)
    findings: list[Finding] = []
    for relpath in view.files:
        if PurePosixPath(relpath).suffix.lower() not in VIDEO_SUFFIXES:
            continue
        parts = relpath.split("/")
        if parts[0] == ROOT:
            findings.extend(_inside(relpath, parts, folders, on_disk))
        elif parts[0] in MISFILED_ROOTS:
            findings.extend(_outside(relpath, folders, on_disk))
    return findings


def _outside(relpath: str, folders: dict[str, str], on_disk: set[str]) -> list:
    """A file that reads as a music video, filed as something else.

    The bar is the folder-independent one: the name has to parse confidently
    *and* carry a version marker that only a video has. A dash in a filename
    under `Movies/` is evidence of nothing — a good deal of cinema is named
    `Director - Title` by somebody's ripping script.
    """
    from librairy.audit import Finding

    found = read(relpath)
    if found is None or not found.named:
        return []
    dest = _destination(found.artist, FALLBACK_GENRE, relpath, folders)
    if not dest or dest in on_disk:
        return []
    return [
        Finding(
            relpath=relpath,
            kind="music-video-misfiled",
            severity="review",
            summary=(
                f"Named as a music video by {found.artist}, but filed under "
                f"{relpath.split('/')[0]}."
            ),
            dest_relpath=dest,
            evidence=[
                *found.evidence,
                EvidenceEntry("filesystem", "move", dest, 0.8, note="where it would go"),
            ],
        )
    ]


def _inside(
    relpath: str, parts: list[str], folders: dict[str, str], on_disk: set[str]
) -> list:
    """Something already under `Music Videos/`. Three ways it can be wrong."""
    from librairy.audit import Finding

    name = parts[-1]
    below = parts[1:-1]
    if CAMERA_RE.match(PurePosixPath(name).stem):
        return [
            Finding(
                relpath=relpath,
                kind="music-video-personal",
                severity="review",
                summary=(
                    "This looks like a clip off a phone or a camera, not a music "
                    "video. Where it belongs depends on when it was taken."
                ),
                evidence=[
                    EvidenceEntry("filesystem", "name", name, 0.85),
                    EvidenceEntry("filesystem", "folder", "/".join(parts[:-1]), 0.6),
                ],
            )
        ]
    parsed = parse(name)
    parent = below[-1] if below else ""
    if not parsed.confident:
        if below and _key(name).startswith(_key(parent)):
            # Filed under an artist folder whose name the file repeats. This is
            # what LibrAIry's own filing produces, and the folder is the
            # identity — reporting it as unreadable would mean the audit
            # objecting to the filing policy on every file it filed.
            return []
        return [
            Finding(
                relpath=relpath,
                kind="music-video-unreadable",
                severity="review",
                summary=(
                    "The name does not say who this is by, and neither does the "
                    "folder, so LibrAIry will not choose an artist for it."
                ),
                evidence=[
                    EvidenceEntry("filesystem", "name", name, 0.9),
                    *[
                        EvidenceEntry("heuristic", "reading", note, 0.5)
                        for note in parsed.notes
                    ],
                ],
            )
        ]
    artist = parsed.primary_artist
    if parent and _key(parent) == _key(artist):
        return []  # right artist folder; the spelling of the file is not this
        # detector's business — see the module docstring.
    genre = below[0] if len(below) > 1 else FALLBACK_GENRE
    if _key(genre) == _key(artist):
        genre = FALLBACK_GENRE
    dest = _destination(artist, genre, relpath, folders)
    if not dest or dest == relpath or dest in on_disk:
        return []
    return [
        Finding(
            relpath=relpath,
            kind="music-video-misfiled",
            severity="review",
            summary=(
                f"The name says {artist}, but this is filed under {parent!r}."
                if parent
                else f"The name says {artist}, but this is loose at the top level."
            ),
            evidence=[
                EvidenceEntry("filesystem", "name", name, 0.9),
                EvidenceEntry("heuristic", "artist", artist, 0.8),
                EvidenceEntry("filesystem", "move", dest, 0.8, note="where it would go"),
            ],
            dest_relpath=dest,
        )
    ]


def _destination(
    artist: str, genre: str, relpath: str, folders: dict[str, str]
) -> str:
    """Where this file would go, keeping the name it already has.

    The folder comes from the library first and the template second. If there is
    already a `Music Videos/House/Fatboy Slim/`, that is where a Fatboy Slim
    video belongs — spelled the way the person spelled it. Only when no such
    folder exists does the destination policy invent one, and it invents it
    through `render_destination`, so a correction can never land somewhere the
    classifier would not have put the same file.

    Either way the **filename is the one that was found**. Only the directory
    part of the rendered path is used. A file already in the library has a name
    somebody chose, and an audit that answered "in the wrong folder" by also
    restyling the name would be doing two things to a file when asked about one.
    """
    from librairy.taxonomy import render_destination

    name = PurePosixPath(relpath).name
    existing = folders.get(_key(artist))
    if existing:
        return f"{existing}/{name}"
    rendered = render_destination(
        "music_videos",
        {"genre": genre or FALLBACK_GENRE, "artist": artist, "clean_name": name},
        library_root=Path("/"),
    )
    if not rendered.relpath:
        return ""
    return f"{PurePosixPath(rendered.relpath).parent}/{name}"


def _artist_folders(files: list[str]) -> dict[str, str]:
    """Artist folders that already exist under `Music Videos/`, by name.

    Read off the file list rather than the filesystem, like every other
    detector, so this stays a pure function of what the audit gathered. The
    shallowest match wins so that a nested duplicate cannot claim the artist.
    """
    found: dict[str, str] = {}
    for relpath in files:
        parts = relpath.split("/")
        if parts[0] != ROOT or len(parts) < 3:
            continue
        folder = "/".join(parts[:-1])
        key = _key(parts[-2])
        if key and (key not in found or folder.count("/") < found[key].count("/")):
            found[key] = folder
    return found


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", PurePosixPath(text).stem.casefold())
