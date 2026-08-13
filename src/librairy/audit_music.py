"""Music reconciliation: does this library agree with itself?

The general audit asks whether a file is damaged — a bad character, a missing
cover, a byte-for-byte twin. This asks a harder question that only makes sense
for music, where the same album can be described three ways at once: by the
folders it sits in, by the tags inside it, and by the shape of the filenames.
When those three disagree, one of them is wrong, and which one is usually
obvious from the other two.

The detectors here were written against a real library, and the real library
immediately proved the point. Forty-five of its forty-eight tracks are one
compilation — *Best Road Trip Disco Fever Classics* — filed as twenty-seven
separate artist folders each containing an album folder of that name. Every
track is tagged `album_artist: V.A.`. It is one album pretending to be
twenty-seven, and nothing in the audit noticed, because the existing
tag/folder detector requires the tagged artist to already own a folder and
nobody has a folder called `V.A.`.

Three rules, inherited and worth restating:

* **One problem is one finding.** The split compilation produces a single row
  naming twenty-seven folders, not twenty-seven rows, and not forty-five. A
  detector that cannot group is a detector that will be turned off.
* **Your convention wins.** MusicBrainz calling something disco is not a
  reason to move it out of `Music/Pop`. The question is only ever whether this
  file is out of step with the way *this library* is organised.
* **Nothing here is executable.** Every finding in this module is about a
  folder, or about a set of folders, and the correction plan resolves a file
  plus its companions in one directory — not a subtree. These are observations
  with a suggestion attached. `audit.EXECUTABLE_KINDS` is the allowlist and
  none of these kinds are in it.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from librairy.models import EvidenceEntry

if TYPE_CHECKING:  # pragma: no cover - typing only
    from librairy.audit import Finding, LibraryView

AUDIO = {".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".wma", ".aiff", ".alac"}

# What a tagger writes into `album_artist` when the album has no single artist.
# Matched on the normalised key, so "V.A." and "VA" and "v/a" are one thing.
COMPILATION_ARTISTS = {"va", "various", "variousartists", "variousartist", "unknownartist"}

# `01 - Title`, `01 Title`, `1-01 Title`. Enough to read a track number off a
# filename without pretending to parse every naming scheme in the world.
TRACK_NUMBER = re.compile(r"^(?:(\d{1,2})\s*[-.]\s*)?(\d{1,3})(?=\s*[-_. ])")


@dataclass(frozen=True)
class Album:
    """One album folder: where it is, what is in it, what the tags claim."""

    folder: str
    tracks: tuple[str, ...]
    album_tag: str
    artists: frozenset[str]
    album_artists: frozenset[str]

    @property
    def artist_folder(self) -> str:
        """`Music/Pop/Abba/Album` -> `Abba`. Empty for a shallower layout."""
        parts = self.folder.split("/")
        return parts[-2] if len(parts) >= 3 else ""

    @property
    def branch(self) -> str:
        """Everything above the artist: `Music/Pop`."""
        parts = self.folder.split("/")
        return "/".join(parts[:-2]) if len(parts) >= 3 else ""

    @property
    def is_compilation(self) -> bool:
        return any(key(name) in COMPILATION_ARTISTS for name in self.album_artists)


def albums_in(view: LibraryView) -> list[Album]:
    """Every folder under Music that holds audio, with its tags gathered."""
    folders: dict[str, list[str]] = defaultdict(list)
    for relpath in view.files:
        if view.top(relpath) != "music":
            continue
        if PurePosixPath(relpath).suffix.lower() not in AUDIO:
            continue
        folders[view.parent(relpath)].append(relpath)

    found = []
    for folder, tracks in sorted(folders.items()):
        tags = [view.tags.get(track) or {} for track in tracks]
        album_tags = {(tag.get("album") or "").strip() for tag in tags} - {""}
        found.append(
            Album(
                folder=folder,
                tracks=tuple(sorted(tracks)),
                # One album tag or none. Two means the folder is not an album,
                # and every detector below wants to know that.
                album_tag=next(iter(album_tags)) if len(album_tags) == 1 else "",
                artists=frozenset(
                    (tag.get("artist") or "").strip()
                    for tag in tags
                    if (tag.get("artist") or "").strip()
                ),
                album_artists=frozenset(
                    (tag.get("album_artist") or "").strip()
                    for tag in tags
                    if (tag.get("album_artist") or "").strip()
                ),
            )
        )
    return found


def detect(
    view: LibraryView,
    *,
    identities: dict[str, tuple] | None = None,
    collections: bool = True,
) -> list[Finding]:
    """Every music detector, and the order matters.

    A split compilation explains most of what the later detectors would
    otherwise report about the same files — twenty-seven "this folder is
    missing tracks 1 to 8" rows are one problem seen twenty-seven times. So
    the split runs first and the folders it claims are excluded from the rest.

    `collections=False` finds the multi-artist groups and stays silent about
    them, which is what the staged audit wants: the verdict on a collection
    depends on what the catalogs say, and the catalog stage has not run yet
    when structure does. The folders are still claimed either way, so the
    later detectors do not fill Review with the consequences of a question
    nobody has answered yet.
    """
    albums = albums_in(view)
    groups = album_groups(view, albums)
    findings = list(
        _split_albums(view, groups, identities=identities, collections=collections)
    )
    claimed = {
        album.folder for members in groups.values() for album in members
    }
    rest = [album for album in albums if album.folder not in claimed]
    findings.extend(_artist_in_two_branches(view, rest))
    findings.extend(_album_folder_disagrees_with_tags(view, rest))
    findings.extend(_track_numbering(view, rest))
    findings.extend(_filename_outliers(view, rest))
    findings.extend(_loose_tracks(view, rest))
    return findings


def _folders_of(finding: Finding) -> set[str]:
    """The folders a grouped finding speaks for, read back off its evidence."""
    return {
        entry.detail
        for entry in finding.evidence
        if entry.source == "filesystem" and entry.field == "folder"
    }


# --- the big one: one album filed as many -------------------------------------


def album_groups(view: LibraryView, albums: list[Album]) -> dict[str, list[Album]]:
    """Album titles that live in more than one folder, keyed by normalised name.

    Exposed because the staged audit needs the *groups* before it needs the
    findings: the catalog stage looks each one up as a release, and the
    structure stage has to know which folders those are so it can stay quiet
    about them in the meantime.
    """
    by_album: dict[str, list[Album]] = defaultdict(list)
    for album in albums:
        if album.album_tag:
            by_album[key(album.album_tag)].append(album)
    return {name: members for name, members in by_album.items() if len(members) > 1}


def is_multi_artist(view: LibraryView, members: list[Album]) -> bool:
    """More than one performer actually named in the tags.

    Two artists with no compilation flag is still a collection, so the flag is
    not required. But the flag is not sufficient either: one artist across two
    folders with `album_artist: V.A.` is a *mistag*, and sending it to the
    compilation policy would answer a Queen album with `Various Artists/`. The
    flag only decides it when the tracks name no performer at all, because
    then there is nothing better to go on.
    """
    performers = {
        (view.tags.get(track) or {}).get("artist", "").strip()
        for album in members
        for track in album.tracks
    } - {""}
    if performers:
        return len(performers) > 1
    return any(album.is_compilation for album in members)


def _split_albums(
    view: LibraryView,
    groups: dict[str, list[Album]],
    *,
    identities: dict[str, tuple] | None = None,
    collections: bool = True,
) -> list[Finding]:
    """The same album, in more than one folder.

    Two shapes, and only one of them has an obvious answer:

    * an *album* whose tracks ended up in two folders for the same artist,
      usually a half-finished copy or a second rip. Put them back together.
    * a *multi-artist collection* scattered one-artist-per-folder, which is
      what the real library does. Whether it should be put back together at
      all is a real question, and `audit_compilation` answers it rather than
      this function assuming.

    The suggestion is a destination, but the kind is not in
    `audit.EXECUTABLE_KINDS`: gathering forty-five files out of twenty-seven
    directories is a subtree restructure, and the correction plan resolves a
    file plus its companions in one directory. Showing the answer is useful;
    pretending there is a button for it would not be.

    `identities` is what the catalog tier found, keyed by normalised album
    title. It arrives already looked up because this runs inside the structure
    stage, which is not allowed to touch the network.
    """
    from librairy.audit import Finding
    from librairy.audit_compilation import (
        classify_collection,
        evidence_for,
        library_convention,
        summarize,
    )

    convention = library_convention(view)
    findings = []
    for album_key, members in groups.items():
        folders = sorted(album.folder for album in members)
        tracks = sorted(track for album in members for track in album.tracks)
        title = members[0].album_tag

        if is_multi_artist(view, members):
            if not collections:
                continue
            verdict = classify_collection(
                view,
                members,
                catalogs=(identities or {}).get(album_key, ()),
                convention=convention,
            )
            findings.append(
                Finding(
                    relpath=folders[0],
                    kind=f"collection-{verdict.kind}",
                    severity="review",
                    summary=summarize(verdict),
                    dest_relpath=verdict.home,
                    evidence=evidence_for(verdict),
                )
            )
            continue

        evidence = [
            EvidenceEntry("tags", "album", title, 0.95),
            EvidenceEntry("tags", "album artist", _album_artist_label(members), 0.9),
            EvidenceEntry("filesystem", "folders", str(len(folders)), 0.9),
            EvidenceEntry("filesystem", "tracks", str(len(tracks)), 0.9),
        ]
        branches = {album.branch for album in members}
        if len(branches) == 1:
            evidence.append(
                EvidenceEntry("library-pattern", "all under", next(iter(branches)), 0.85)
            )
        run = _contiguous_run(tracks)
        if run:
            evidence.append(EvidenceEntry("filesystem", "track numbers", run, 0.9))
        # Every folder, so `detect` can exclude them and so Why can list them.
        evidence.extend(
            EvidenceEntry("filesystem", "folder", folder, 0.9) for folder in folders
        )

        findings.append(
            Finding(
                relpath=folders[0],
                kind="split-album",
                severity="review",
                summary=(
                    f"{title!r} is split across {len(folders)} folders "
                    f"holding {len(tracks)} tracks between them."
                ),
                dest_relpath=_suggested_home(members, compilation=False),
                evidence=evidence,
            )
        )
    return findings


def _suggested_home(members: list[Album], *, compilation: bool) -> str | None:
    """Where a single-artist album would live if it were one folder.

    Only proposed when every piece already sits under the same branch, so the
    suggestion never moves music between the genre folders the library owner
    chose. Multi-artist collections do not come through here at all — where
    one of those belongs, or whether it belongs anywhere as a unit, is
    `audit_compilation`'s question.
    """
    if compilation:
        return None
    branches = {album.branch for album in members}
    if len(branches) != 1:
        return None
    branch = next(iter(branches))
    if not branch:
        return None
    artists = {album.artist_folder for album in members if album.artist_folder}
    if len(artists) != 1:
        return None
    return f"{branch}/{next(iter(artists))}/{members[0].album_tag}"


def _album_artist_label(members: list[Album]) -> str:
    names = sorted({name for album in members for name in album.album_artists})
    if not names:
        return "not tagged"
    return names[0] if len(names) == 1 else f"{names[0]} and {len(names) - 1} more"


def _contiguous_run(tracks: list[str]) -> str:
    """"1-45, complete" — the strongest evidence that this is one album.

    Twenty-seven folders whose track numbers interleave into an unbroken run
    are not twenty-seven albums that happen to share a name.
    """
    numbers = sorted(number for number in map(track_number, tracks) if number)
    if len(numbers) < 3:
        return ""
    low, high = numbers[0], numbers[-1]
    missing = len(set(range(low, high + 1)) - set(numbers))
    if missing == 0:
        return f"{low}-{high}, complete"
    if missing <= max(2, (high - low) // 10):
        return f"{low}-{high}, {missing} missing"
    return ""


def track_number(relpath: str) -> int | None:
    match = TRACK_NUMBER.match(PurePosixPath(relpath).name)
    return int(match.group(2)) if match else None


# --- the artist is in two places ----------------------------------------------


def _artist_in_two_branches(view: LibraryView, albums: list[Album]) -> list[Finding]:
    """`Music/Pop/Queen` and `Music/Rock/Queen`.

    One artist, two genre branches, and the library has already answered which
    one it prefers — whichever holds more of them. This is the case the brief
    calls consolidation, and it is exactly the shape where a catalog genre
    would be the wrong evidence: the point is not what genre Queen is, it is
    that this library cannot have decided both.
    """
    from librairy.audit import Finding

    homes: dict[str, dict[str, list[Album]]] = defaultdict(lambda: defaultdict(list))
    for album in albums:
        if album.artist_folder and album.branch:
            homes[key(album.artist_folder)][album.branch].append(album)

    findings = []
    for branches in homes.values():
        if len(branches) < 2:
            continue
        ranked = sorted(branches.items(), key=lambda item: (-len(item[1]), item[0]))
        (winner, kept), *others = ranked
        stray = [album for _, group in others for album in group]
        name = kept[0].artist_folder
        findings.append(
            Finding(
                relpath=sorted(album.folder for album in stray)[0],
                kind="artist-split",
                severity="review",
                summary=(
                    f"{name!r} has folders under {len(branches)} different sections. "
                    f"{len(kept)} album(s) under {winner}, "
                    f"{len(stray)} elsewhere."
                ),
                evidence=[
                    EvidenceEntry("filesystem", "artist", name, 0.9),
                    EvidenceEntry("library-pattern", "mostly under", winner, 0.85),
                    *[
                        EvidenceEntry("filesystem", "also under", album.branch, 0.9)
                        for album in stray[:4]
                    ],
                ],
            )
        )
    return findings


# --- the folder and the tags disagree -----------------------------------------


def _album_folder_disagrees_with_tags(view: LibraryView, albums: list[Album]) -> list[Finding]:
    """The album tag and the album folder are different names.

    One finding per album folder, never per track: forty tracks in a
    mis-titled folder is one mis-titled folder. Compilations are exempt from
    the artist half of the comparison, because a compilation folder is not
    supposed to match its tracks' artists.
    """
    from librairy.audit import Finding

    findings = []
    for album in albums:
        name = PurePosixPath(album.folder).name
        if not album.album_tag or same(album.album_tag, name):
            continue
        if len(album.tracks) < 2:
            continue
        findings.append(
            Finding(
                relpath=album.folder,
                kind="album-name-mismatch",
                severity="review",
                summary=(
                    f"The folder is called {name!r} but all "
                    f"{len(album.tracks)} tracks are tagged {album.album_tag!r}."
                ),
                evidence=[
                    EvidenceEntry("tags", "album", album.album_tag, 0.9),
                    EvidenceEntry("filesystem", "folder", name, 0.9),
                    EvidenceEntry("filesystem", "tracks", str(len(album.tracks)), 0.8),
                ],
            )
        )
    return findings


# --- the numbers and the names ------------------------------------------------


def _track_numbering(view: LibraryView, albums: list[Album]) -> list[Finding]:
    """Tracks missing from the middle of an album.

    Only where every file in the folder is numbered, and only for a gap big
    enough to mean something — a 12-track album missing track 7 is worth a
    line; an album ending at 11 because it has 11 tracks is not.
    """
    from librairy.audit import Finding

    findings = []
    for album in albums:
        numbers = [track_number(track) for track in album.tracks]
        if len(album.tracks) < 4 or not all(numbers):
            continue
        present = sorted(number for number in numbers if number)
        missing = sorted(set(range(1, present[-1] + 1)) - set(present))
        repeated = sorted({n for n in present if present.count(n) > 1})
        if not missing and not repeated:
            continue
        # A run starting at 2 is usually a missing first track; a run starting
        # at 30 is a piece of something bigger, and that is the split-album
        # detector's business, not this one.
        if present[0] > 2 and not repeated:
            continue
        parts = []
        if missing:
            parts.append(f"no track {_ranges(missing)}")
        if repeated:
            parts.append(f"two files numbered {_ranges(repeated)}")
        findings.append(
            Finding(
                relpath=album.folder,
                kind="track-numbering",
                severity="review",
                summary=(
                    f"{PurePosixPath(album.folder).name!r} has "
                    f"{len(album.tracks)} tracks but {' and '.join(parts)}."
                ),
                evidence=[
                    EvidenceEntry("filesystem", "tracks", str(len(album.tracks)), 0.9),
                    EvidenceEntry("filesystem", "numbered", f"{present[0]}-{present[-1]}", 0.9),
                ],
            )
        )
    return findings


def _ranges(numbers: list[int]) -> str:
    if len(numbers) == 1:
        return str(numbers[0])
    if len(numbers) <= 3:
        return ", ".join(str(number) for number in numbers)
    return f"{numbers[0]}, {numbers[1]} and {len(numbers) - 2} more"


def _filename_outliers(view: LibraryView, albums: list[Album]) -> list[Finding]:
    """One file named unlike every one of its neighbours.

    The convention is read from the folder, never imposed on it. If eleven
    tracks are `01 - Title` and one is `track_final2`, that is the twelfth
    file being wrong; if all twelve are `track_finalN`, that is a naming style
    and none of this module's business.
    """
    from librairy.audit import Finding

    findings = []
    for album in albums:
        if len(album.tracks) < 5:
            continue
        shapes = defaultdict(list)
        for track in album.tracks:
            shapes[_shape(track)].append(track)
        ranked = sorted(shapes.items(), key=lambda item: -len(item[1]))
        (dominant, majority), *others = ranked
        strays = [track for _, group in others for track in group]
        # Unanimous but for one or two, and the majority has to be a shape
        # rather than the absence of one.
        if dominant == "unnumbered" or not strays or len(strays) > 2:
            continue
        if len(majority) < len(album.tracks) - 2:
            continue
        for track in strays:
            findings.append(
                Finding(
                    relpath=track,
                    kind="naming-outlier",
                    severity="review",
                    summary=(
                        f"Named unlike the other {len(majority)} tracks in this folder."
                    ),
                    evidence=[
                        EvidenceEntry("library-pattern", "folder uses", _example(majority), 0.85),
                        EvidenceEntry("filesystem", "this file", PurePosixPath(track).name, 0.9),
                    ],
                )
            )
    return findings


def _shape(relpath: str) -> str:
    name = PurePosixPath(relpath).name
    if re.match(r"^\d{1,3}\s*-\s*\S", name):
        return "number - title"
    if re.match(r"^\d{1,2}[-.]\d{1,3}\s", name):
        return "disc-number title"
    if re.match(r"^\d{1,3}\s+\S", name):
        return "number title"
    return "unnumbered"


def _example(tracks: list[str]) -> str:
    return PurePosixPath(sorted(tracks)[0]).name


def _loose_tracks(view: LibraryView, albums: list[Album]) -> list[Finding]:
    """Tracks lying directly in an artist folder that otherwise uses albums.

    The convention is this artist's own. An artist whose every track is loose
    is filed consistently, and consistency is not a finding.
    """
    from librairy.audit import Finding

    by_artist: dict[str, list[Album]] = defaultdict(list)
    for album in albums:
        parts = album.folder.split("/")
        if len(parts) >= 3:
            by_artist["/".join(parts[:3])].append(album)

    findings = []
    for artist_folder, group in sorted(by_artist.items()):
        loose = [album for album in group if album.folder == artist_folder]
        nested = [album for album in group if album.folder != artist_folder]
        if not loose or not nested:
            continue
        tracks = loose[0].tracks
        findings.append(
            Finding(
                relpath=artist_folder,
                kind="loose-tracks",
                severity="review",
                summary=(
                    f"{len(tracks)} track(s) sit directly in this artist folder, "
                    f"which otherwise uses {len(nested)} album folder(s)."
                ),
                evidence=[
                    EvidenceEntry("filesystem", "loose tracks", str(len(tracks)), 0.9),
                    EvidenceEntry(
                        "library-pattern", "album folders here", str(len(nested)), 0.85
                    ),
                ],
            )
        )
    return findings


# --- shared -------------------------------------------------------------------


def key(value: str) -> str:
    return "".join(char for char in value.lower() if char.isalnum())


def same(left: str, right: str) -> bool:
    return key(left) == key(right)
