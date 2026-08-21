"""Loose tracks, and why one answer for the group is the wrong answer.

    Music/Rock/Queen/
        01 - Death on Two Legs.flac        <- loose
        02 - Sheer Heart Attack.flac       <- loose
        A Night at the Opera/              <- an album folder
        News of the World/                 <- another one

`audit_music` has reported this since the first release and could never do
anything about it, for a reason that is not a limitation. A folder rename is
one answer for every file beneath it. A merge is one destination for every file
in a folder. **This is not that shape.** Two loose tracks beside two album
folders are commonly two different albums, and a control that filed all of them
into one place would be wrong in exactly the case it was built for.

So this is the third shape of destination choice, and the first per-*item* one:

    artist-split    one answer, for the whole finding
    loose-tracks    one answer per track, and they differ

Three things follow from that, and all three are why this is not `artist-split`
with a loop around it.

**`Leave here` is a real answer.** A track that belongs loose — a single, a
stray B-side, something the person put there on purpose — is answered, not
unanswered, and it produces no plan operation at all. A no-op move exists in no
plan LibrAIry has ever written and is not going to start here. If every track is
left, the finding is resolved with no plan whatsoever.

**Changing one answer changes one answer.** Reversing an `artist-split`
direction swaps the role of every file in it, so every collision answer has to
be asked again. Moving one track from one album to another says nothing about
any other track, and clearing the rest would be the software punishing somebody
for changing their mind.

**Nothing is invented.** Every candidate is either an album folder that already
exists under this artist, read from the index, or one the file itself names in
its own tags. `Unknown Album`, `Album (2)` and `Misc` are the destinations a
rule would produce to make the row actionable, and a row that is actionable
because it invented somewhere to put things is worse than an observation.

That second kind of candidate — a folder that does not exist yet — is the one
addition, and it is the common case rather than an edge one. A shelf of loose
Queen tracks tagged `A Night at the Opera` with no such folder is what a messy
library actually looks like, and refusing it means the feature only works for
libraries already half-organised. The line it does not cross is evidence: an
**embedded album tag that agrees with the artist folder it is sitting in** is
the file saying where it belongs, recorded by `audit_music` at the pass that
already read it. A filename, a title resemblance and a model's opinion are not,
and none of them will create a folder here. If the evidence is not there, the
track keeps the candidates that exist and `Leave here`, and nothing is offered
that somebody would have to undo.

Creating the folder is not an operation. A move already makes its parent
directory, so `Album/01 - Song.flac` brings `Album/` into being as a
consequence of the file arriving — there is no `mkdir` in any plan, nothing to
roll back separately, and an approved plan that turns out to move no files
leaves nothing behind.

What happens to one track once it has a destination is not new either. A track
whose destination is free is an ordinary move; a track whose destination is
occupied is the collision `merge.py` already answers, with the same three
outcomes, the same wording and the same storage. Two files wanting one name is
the same problem however they came to be pointed at it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from pathlib import PurePosixPath

from librairy.config import Settings
from librairy.merge import Member, classify, specs_for
from librairy.merge import choices_for as merge_choices
from librairy.planner import OperationSpec

KIND = "loose-tracks"

# Past this the row is a form, not a choice. Twelve albums times eight loose
# tracks is ninety-six buttons, and somebody reading that is not choosing, they
# are filling something in.
MAX_ALBUMS = 8
MAX_TRACKS = 25

# What the form posts for "this one belongs where it is". Empty rather than a
# word, so the value can never collide with a real relative path.
LEAVE = ""

# How `audit_music` records one loose track's album tag, and how it is read
# back. The filename follows the prefix, because that is what identifies the
# track inside a finding anchored at the artist folder.
_ALBUM_OF = "album of "


@dataclass(frozen=True)
class Album:
    """One album folder this artist already has."""

    relpath: str
    files: int

    @property
    def name(self) -> str:
        return PurePosixPath(self.relpath).name


@dataclass(frozen=True)
class ProposedAlbum:
    """An album folder this artist does not have, named by the tracks themselves.

    `tracks` is which loose files carry the tag, and it is the whole warrant
    for offering the folder: the button appears on those rows and nowhere else,
    and the page can say how many files agree without anything having to be
    counted twice.
    """

    relpath: str
    name: str
    tracks: tuple[str, ...]

    @property
    def files(self) -> int:
        """Nothing is in it. It does not exist."""
        return 0


@dataclass(frozen=True)
class Track:
    """One loose track, where it could go, and what was said about it."""

    relpath: str
    size: int
    #  "" when unanswered, the destination folder when answered to move, and
    #  `LEAVE` is indistinguishable from "" by design — so `answered` is a
    #  separate flag rather than a test on this string.
    chosen: str = ""
    answered: bool = False
    #  Only once a destination is chosen: what is waiting at the other end.
    member: Member | None = None

    @property
    def name(self) -> str:
        return PurePosixPath(self.relpath).name

    @property
    def leaving(self) -> bool:
        return self.answered and not self.chosen

    @property
    def moving(self) -> bool:
        return self.answered and bool(self.chosen)

    @property
    def needs_choice(self) -> bool:
        """Unanswered, or answered into a collision nobody has resolved."""
        if not self.answered:
            return True
        return self.member is not None and self.member.needs_choice and not self.member.choice


@dataclass(frozen=True)
class FilingView:
    """Everything filing these tracks would do, before anyone approves it."""

    finding_id: int
    artist: str
    albums: tuple[Album, ...]
    tracks: tuple[Track, ...]
    #  Folders that would come into being if somebody chose them. Empty when
    #  the tags said nothing, said something useless, or named a folder the
    #  artist already has — in which case it is an ordinary candidate above.
    proposed: tuple[ProposedAlbum, ...] = ()

    def offered(self, track: Track) -> tuple[ProposedAlbum, ...]:
        """The new folders this particular track's own tags asked for."""
        return tuple(album for album in self.proposed if track.relpath in album.tracks)

    @property
    def moving(self) -> tuple[Track, ...]:
        return tuple(track for track in self.tracks if track.moving)

    @property
    def leaving(self) -> tuple[Track, ...]:
        return tuple(track for track in self.tracks if track.leaving)

    @property
    def unresolved(self) -> tuple[Track, ...]:
        return tuple(track for track in self.tracks if track.needs_choice)

    @property
    def settled(self) -> bool:
        return not self.unresolved

    @property
    def operations(self) -> int:
        return sum(len(specs_for(track.member)) for track in self.moving if track.member)


def is_filing_finding(row: sqlite3.Row) -> bool:
    try:
        return row["kind"] == KIND
    except (KeyError, IndexError):
        return False


# --- reading the question -----------------------------------------------------------


def plan_filing(
    conn: sqlite3.Connection, settings: Settings, row: sqlite3.Row, *, verify: bool
) -> FilingView | None:
    """The tracks, the albums they could go to, and every answer so far.

    None when this is not a filing question at all, or has stopped being one —
    the loose files were filed by hand, the album folders were removed, or
    there are so many of either that the row would be an exam rather than a
    decision.
    """
    from librairy.destination_choice import answers

    if not is_filing_finding(row):
        return None
    artist = str(row["relpath"])
    found = _albums(conn, artist)
    loose = _loose(conn, settings, artist)
    if not found or not loose:
        return None
    if len(found) > MAX_ALBUMS or len(loose) > MAX_TRACKS:
        return None
    proposed = _proposed(row, artist, loose, found)
    given = answers(conn, int(row["id"]))
    valid = {album.relpath for album in found} | {album.relpath for album in proposed}
    collisions = merge_choices(conn, int(row["id"]))
    tracks = tuple(
        _with_answer(conn, settings, track, given, valid, collisions, verify=verify)
        for track in loose
    )
    return FilingView(
        finding_id=int(row["id"]),
        artist=artist,
        albums=found,
        tracks=tracks,
        proposed=proposed,
    )


def _proposed(
    row: sqlite3.Row, artist: str, loose: tuple[Track, ...], found: tuple[Album, ...]
) -> tuple[ProposedAlbum, ...]:
    """Album folders the loose tracks name in their own tags and do not have.

    The evidence is `audit_music`'s, written when it had the files open. Three
    things happen to it here and each one is a way of not inventing something:

    * an album whose folder now exists is dropped, because the folder is a
      candidate already and offering to create it would be a lie about the
      library;
    * spellings that differ only in case and punctuation are one album, with
      the spelling most of the tracks used — so three tracks answering
      separately cannot produce `A Night at the Opera` beside
      `A Night At The Opera`;
    * spellings that differ in **words** stay separate. `Night at the Opera`
      and `A Night at the Opera` are close strings, and close is not the same
      release. Two candidates is honest; silently merging them is a guess with
      a folder in it.
    """
    from librairy.audit_music import key
    from librairy.naming import tidy_component

    tagged: dict[str, str] = {}
    for entry in _evidence(row):
        if entry.source == "tags" and entry.field.startswith(_ALBUM_OF):
            tagged[entry.field[len(_ALBUM_OF) :]] = str(entry.detail)
    if not tagged:
        return ()
    existing = {key(album.name) for album in found}
    groups: dict[str, list[tuple[str, str]]] = {}
    for track in loose:
        album = tagged.get(track.name, "").strip()
        if not album or key(album) in existing:
            continue
        groups.setdefault(key(album), []).append((album, track.relpath))
    proposed = []
    for members in groups.values():
        spelling = _commonest(spelling for spelling, _ in members)
        name = tidy_component(spelling)
        if not name:
            continue
        proposed.append(
            ProposedAlbum(
                relpath=f"{artist}/{name}",
                name=name,
                tracks=tuple(relpath for _, relpath in members),
            )
        )
    proposed.sort(key=lambda album: album.relpath)
    #  The cap is about the width of the row, so it counts every button on it.
    if len(found) + len(proposed) > MAX_ALBUMS:
        return ()
    return tuple(proposed)


def _commonest(spellings) -> str:  # noqa: ANN001
    """The spelling most tracks used, and the first alphabetically on a tie.

    Deterministic on purpose: the folder a plan creates must not depend on the
    order rows came back in.
    """
    counted: dict[str, int] = {}
    for spelling in spellings:
        counted[spelling] = counted.get(spelling, 0) + 1
    return sorted(counted.items(), key=lambda item: (-item[1], item[0]))[0][0]


def _evidence(row: sqlite3.Row) -> list:
    from librairy.proposals import decode_evidence

    try:
        return decode_evidence(row["evidence"]) if row["evidence"] else []
    except (TypeError, ValueError):
        return []


def _with_answer(
    conn: sqlite3.Connection,
    settings: Settings,
    track: Track,
    given: dict[str, str | None],
    valid: set[str],
    collisions: dict[str, str],
    *,
    verify: bool,
) -> Track:
    """One track's stored answer, checked against the library as it is now.

    An answer naming an album folder that has since been renamed or emptied is
    not an answer any more. It goes back to being the question rather than
    quietly becoming an approval nobody could review.
    """
    if track.relpath not in given:
        return track
    destination = given[track.relpath]
    if destination is None:
        return replace(track, answered=True, chosen="")
    if destination not in valid:
        return track
    dest_relpath = f"{destination}/{track.name}"
    if verify:
        _assert_current(conn, settings, track.relpath)
    member = classify(conn, settings, track.relpath, dest_relpath, verify=verify)
    if member.needs_choice:
        member = replace(member, choice=collisions.get(track.relpath, ""))
    return replace(track, answered=True, chosen=destination, member=member)


def _assert_current(
    conn: sqlite3.Connection, settings: Settings, relpath: str
) -> None:
    """Still the bytes the index recorded, checked at approval and not before.

    The page render asks `stat`; this reads the file, and only for the tracks
    that are actually going somewhere. A track re-ripped between choosing an
    album for it and approving that choice is a different track, and filing it
    would be carrying out a decision about a file that no longer exists.
    """
    from librairy.corrections import CorrectionRefused
    from librairy.fingerprint import blake2b_file
    from librairy.paths import PathValidationError, validate_relpath

    row = conn.execute(
        "SELECT fingerprint FROM items WHERE root='library' AND relpath=?", (relpath,)
    ).fetchone()
    if row is None or not row["fingerprint"]:
        raise CorrectionRefused(f"{PurePosixPath(relpath).name} has not been indexed")
    try:
        path = validate_relpath(settings.library_dir, relpath, kind="finding")
    except PathValidationError as exc:
        raise CorrectionRefused(
            f"{PurePosixPath(relpath).name} is not a path inside the library"
        ) from exc
    if not path.is_file() or blake2b_file(path) != row["fingerprint"]:
        raise CorrectionRefused(
            f"{PurePosixPath(relpath).name} changed since you chose where it goes"
        )


def _albums(conn: sqlite3.Connection, artist: str) -> tuple[Album, ...]:
    """The album folders under this artist, with what is in each.

    Read from the index, never invented. A row that offers `Unknown Album`
    because it had nothing else to offer is a row that has made something up.
    """
    counts: dict[str, int] = {}
    for item in conn.execute(
        "SELECT relpath FROM items"
        " WHERE root='library' AND missing_since IS NULL AND relpath LIKE ?",
        (f"{artist}/%",),
    ):
        rest = str(item["relpath"])[len(artist) + 1 :].split("/")
        if len(rest) > 1:
            counts[f"{artist}/{rest[0]}"] = counts.get(f"{artist}/{rest[0]}", 0) + 1
    return tuple(
        Album(relpath=folder, files=files) for folder, files in sorted(counts.items())
    )


def _loose(
    conn: sqlite3.Connection, settings: Settings, artist: str
) -> tuple[Track, ...]:
    """Files sitting directly in the artist folder, as the index has them now."""
    from librairy.mediakind import kind_for

    found: list[Track] = []
    for item in conn.execute(
        "SELECT relpath, size FROM items"
        " WHERE root='library' AND missing_since IS NULL AND relpath LIKE ?"
        " ORDER BY relpath",
        (f"{artist}/%",),
    ):
        relpath = str(item["relpath"])
        if "/" in relpath[len(artist) + 1 :]:
            continue
        if kind_for(settings.library_dir / relpath) != "audio":
            #  A cover or a playlist sitting beside the albums describes the
            #  artist, not a track, and filing it into one album would be a
            #  decision about the other albums as well.
            continue
        if not (settings.library_dir / relpath).is_file():
            continue
        found.append(Track(relpath=relpath, size=int(item["size"] or 0)))
    return tuple(found)


# --- the answers --------------------------------------------------------------------


def answer(
    conn: sqlite3.Connection,
    settings: Settings,
    finding_id: int,
    relpath: str,
    dest_relpath: str,
) -> None:
    """Where one track goes, or that it stays where it is.

    Only this track's answer is touched. Reversing an `artist-split` clears
    every collision answer because the folders swapped roles; moving one track
    from one album to another says nothing whatever about the next track, and
    clearing the rest would punish somebody for changing their mind.
    """
    from librairy.corrections import CorrectionRefused, load_finding
    from librairy.destination_choice import record

    row = load_finding(conn, finding_id)
    view = plan_filing(conn, settings, row, verify=False)
    if view is None:
        raise CorrectionRefused("there is nothing left to file here")
    if not any(track.relpath == relpath for track in view.tracks):
        raise CorrectionRefused("that file is not one of these loose tracks")
    if dest_relpath == LEAVE:
        record(conn, finding_id, relpath, None)
        #  A track that is staying has no destination, so any collision answer
        #  about its destination is about a question that no longer exists.
        _forget_collision(conn, finding_id, relpath)
        return
    if not _may_use(view, relpath, dest_relpath):
        raise CorrectionRefused("that is not one of this track's album folders")
    previous = next((t for t in view.tracks if t.relpath == relpath), None)
    if previous is not None and previous.chosen and previous.chosen != dest_relpath:
        #  The destination moved, so what was at the old one is no longer what
        #  this track collides with. Only this track's collision answer goes.
        _forget_collision(conn, finding_id, relpath)
    record(conn, finding_id, relpath, dest_relpath)


def _may_use(view: FilingView, relpath: str, dest_relpath: str) -> bool:
    """Whether this track may be sent to this folder.

    An existing album folder is available to every track under the artist. A
    folder that does not exist yet is available only to the tracks whose own
    tags named it — otherwise one track's evidence would be creating a folder
    for a file that never claimed to belong in it, which is the invention this
    whole feature is built not to do.
    """
    if any(album.relpath == dest_relpath for album in view.albums):
        return True
    return any(
        album.relpath == dest_relpath and relpath in album.tracks
        for album in view.proposed
    )


def _forget_collision(
    conn: sqlite3.Connection, finding_id: int, relpath: str
) -> None:
    conn.execute(
        "DELETE FROM merge_choices WHERE audit_finding_id=? AND relpath=?",
        (finding_id, relpath),
    )


def resolve_collision(
    conn: sqlite3.Connection, settings: Settings, finding_id: int, relpath: str, choice: str
) -> None:
    """Answer the collision one chosen destination turned out to have.

    The same three outcomes as a folder merge, stored in the same table, with
    the same guarantee that nothing loses bytes. See `librairy/merge.py`.
    """
    from librairy.merge import record_choice

    record_choice(conn, finding_id, relpath, choice)


# --- what it becomes ----------------------------------------------------------------


def operations(view: FilingView) -> list[OperationSpec]:
    """The tracks that move, and nothing for the tracks that stay.

    Quarantines first, exactly as a merge orders them, so the destination check
    before execution is a single statement about the state the moves will find.
    """
    from librairy.corrections import CorrectionRefused

    if not view.settled:
        raise CorrectionRefused(
            f"{len(view.unresolved)} of these tracks still need an answer"
        )
    quarantines: list[OperationSpec] = []
    moves: list[OperationSpec] = []
    for track in view.moving:
        if track.member is None:
            continue
        for spec in specs_for(track.member):
            (quarantines if spec.op_type == "quarantine" else moves).append(spec)
    if not moves and not quarantines:
        raise CorrectionRefused("every one of these tracks is staying where it is")
    return [*quarantines, *moves]
