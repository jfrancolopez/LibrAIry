"""Bringing an album that was filed years ago up to the current naming, on request.

    Music/Rock/Queen/A Night at the Opera/
        01-Death-on-Two-Legs.flac          ->  01 - Death on Two Legs.flac
        02-Lazing-on-a-Sunday-Afternoon.flac -> 02 - Lazing on a Sunday Afternoon.flac
        cover.jpg                          ->  untouched

The naming policy deliberately applies to *new* filing decisions only, and that
is not going to change: an audit that reported thousands of files for not
matching a convention invented after they were filed would be house style
wearing a defect's clothes, and Library Review would open on eight hundred rows
saying "this is not how I would have spelled it". `naming.py` has refused that
since it was written and still does.

But some people do want the old files brought into line, and there was no way
to say so. So this is the other half, and every property of it follows from
being **asked for**:

* **one folder at a time.** Not the library. A preview of one album is a thing
  somebody can read and judge; a preview of four thousand files is a thing
  nobody reads and everybody approves.
* **nothing happens on the way to the preview.** It reads and reports. The
  rename is an ordinary Library correction plan, approved and committed like
  every other move, and undone from History like every other move.
* **names, and nothing else.** No folder is renamed, no category is decided, no
  tag is written, no catalog is asked and no model is consulted. A file that
  moves, moves from one name to another name in the folder it is already in.

**What counts as knowing the title.** A rename needs the title from somewhere
that recorded it — the embedded tags, or a catalog identity somebody asked for
and LibrAIry stored. The current filename is not such a source, and the reason
is specific rather than fussy: house style turned every space into a dash and
dropped every apostrophe, so `Jay-Z-Song.flac` could have been `Jay-Z Song` or
`Jay Z Song` and nothing in the name says which. Guessing would produce a
worse filename than the one it replaced, which is the one outcome this tool
must never have. Those files are listed, left alone, and the reason is printed
next to them.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import PurePosixPath

from librairy.config import Settings
from librairy.musicnames import canonical_name, parse
from librairy.planner import OperationSpec, approve_plan, create_plan

#  The one category this applies to. Music videos are named
#  `Artist - Title (Version).ext` by their own formatter and are read back by
#  their own parser; running an album-track formatter over them would rename a
#  file out of the shape the code that reads it depends on.
MUSIC_ROOT = "Music"

#  A folder, not a library. Past this many files it is not a preview any more,
#  and a person approving a list they did not read is the failure this whole
#  tool is shaped to avoid.
MAX_FILES = 200

#  How many albums one page of a branch shows. Same bound Browse pages by, and
#  for the same reason: a screen listing four hundred rows is not a decision
#  surface, whatever the database can do.
PAGE_ALBUMS = 50

RENAME = "rename"
CURRENT = "current"
UNKNOWN = "unknown"
COLLIDES = "collides"


@dataclass(frozen=True)
class Member:
    """One file in the folder and what would happen to it."""

    relpath: str
    proposed: str
    state: str
    reason: str = ""
    source: str = ""

    @property
    def name(self) -> str:
        return PurePosixPath(self.relpath).name

    @property
    def dest_relpath(self) -> str:
        return f"{PurePosixPath(self.relpath).parent}/{self.proposed}"


@dataclass(frozen=True)
class Preview:
    """Everything normalizing one folder would and would not do."""

    folder: str
    members: tuple[Member, ...]

    @property
    def renaming(self) -> tuple[Member, ...]:
        return tuple(m for m in self.members if m.state == RENAME)

    @property
    def unchanged(self) -> tuple[Member, ...]:
        return tuple(m for m in self.members if m.state == CURRENT)

    @property
    def unknown(self) -> tuple[Member, ...]:
        return tuple(m for m in self.members if m.state == UNKNOWN)

    @property
    def blocked(self) -> tuple[Member, ...]:
        return tuple(m for m in self.members if m.state == COLLIDES)

    @property
    def anything_to_do(self) -> bool:
        return bool(self.renaming)


class NormalizeError(Exception):
    """This folder cannot be normalized, and why."""


def preview(
    conn: sqlite3.Connection,
    settings: Settings,
    folder: str,
    *,
    read_tags=None,  # noqa: ANN001
) -> Preview:
    """What the current naming policy would call every audio file in one folder.

    Reads. Writes nothing, moves nothing, and is the whole of what happens
    before somebody approves.
    """
    from librairy.mediakind import kind_for

    folder = folder.strip("/")
    _assert_music(folder)
    base = settings.library_dir / folder
    if not base.is_dir():
        raise NormalizeError("that folder is not in your library")
    files = sorted(path for path in base.iterdir() if path.is_file())
    if len(files) > MAX_FILES:
        raise NormalizeError(
            f"this folder holds {len(files)} files, which is more than a preview "
            f"anybody would read. Normalize an album at a time."
        )
    tags_of = read_tags or _tags_of
    members: list[Member] = []
    wanted: dict[str, int] = {}
    for path in files:
        if kind_for(path) != "audio":
            #  Cover art, a playlist, a log. This tool is about track names and
            #  a cover is not a track.
            continue
        relpath = f"{folder}/{path.name}"
        members.append(_member(conn, settings, relpath, tags_of))
    for member in members:
        if member.state == RENAME:
            wanted[member.proposed] = wanted.get(member.proposed, 0) + 1
    checked = tuple(
        _checked(settings, member, wanted, folder) for member in members
    )
    return Preview(folder=folder, members=checked)


# --- a branch of albums ---------------------------------------------------------------


@dataclass(frozen=True)
class AlbumSummary:
    """One album under a branch, counted rather than read.

    `off_form` is the cheap, exact question: how many filenames are not in the
    album-track grammar LibrAIry writes. It is deliberately **not** a count of
    what would change — knowing that means reading every file's tags, and
    forty albums of that is forty albums of `ffprobe` to draw a summary. So
    the row says what it measured, and opening the album does the reading and
    shows the exact names.
    """

    relpath: str
    name: str
    parent: str
    tracks: int
    off_form: int

    @property
    def current(self) -> bool:
        return self.off_form == 0


@dataclass(frozen=True)
class BranchPreview:
    """The albums under one artist or section, a page at a time."""

    folder: str
    albums: tuple[AlbumSummary, ...]
    total: int
    page: int = 1
    has_next: bool = False

    @property
    def tracks(self) -> int:
        return sum(album.tracks for album in self.albums)

    @property
    def off_form(self) -> int:
        return sum(album.off_form for album in self.albums)

    @property
    def current_albums(self) -> int:
        return sum(1 for album in self.albums if album.current)

    @property
    def single(self) -> bool:
        """One album, and it is the folder itself.

        The page that suits a folder is the page for what is in it: an album
        shows its filenames, an artist shows its albums. This is how the two
        are told apart, and it is a property of the folder rather than a mode
        anybody has to choose.
        """
        return self.total == 1 and bool(self.albums) and self.albums[0].relpath == self.folder


def branch_preview(
    conn: sqlite3.Connection,  # noqa: ARG001
    settings: Settings,
    folder: str,
    *,
    page: int = 1,
) -> BranchPreview:
    """Every album under this folder, with how many names are off the grammar.

    One directory walk and no file reads. That is the whole scale argument:
    five hundred albums cost one walk and a page of rows, and the expensive
    part — the tags — happens for one album, when somebody opens it.
    """
    albums = _albums(settings, folder)
    folder = folder.strip("/")
    page = max(1, page)
    window = albums[(page - 1) * PAGE_ALBUMS : page * PAGE_ALBUMS]
    return BranchPreview(
        folder=folder,
        albums=tuple(window),
        total=len(albums),
        page=page,
        has_next=len(albums) > page * PAGE_ALBUMS,
    )


def _albums(settings: Settings, folder: str) -> list[AlbumSummary]:
    """Every album under this folder, from one directory walk and no file reads.

    An "album" is any directory that directly holds audio, including the
    folder itself when tracks are lying loose in it — which is what an artist
    folder with singles beside its albums really looks like.
    """
    from librairy.mediakind import kind_for
    from librairy.scanner import visible_files

    folder = folder.strip("/")
    _assert_music(folder)
    base = settings.library_dir / folder
    if not base.is_dir():
        raise NormalizeError("that folder is not in your library")
    counted: dict[str, list[int]] = {}
    for relpath in visible_files(base, settings.ignore_patterns):
        if kind_for(relpath) != "audio":
            continue
        parent = str(PurePosixPath(relpath).parent)
        album = folder if parent == "." else f"{folder}/{parent}"
        tallies = counted.setdefault(album, [0, 0])
        tallies[0] += 1
        #  A leading track number in the current grammar. Read by the parser
        #  that writes it, so "is this the current form" has one answer in
        #  this program rather than a regular expression per caller.
        if not parse(PurePosixPath(relpath).name).track:
            tallies[1] += 1
    return [
        AlbumSummary(
            relpath=album,
            name=PurePosixPath(album).name,
            parent=(
                str(PurePosixPath(album).parent)[len(folder) + 1 :]
                if album != folder
                else ""
            ),
            tracks=tracks,
            off_form=off_form,
        )
        for album, (tracks, off_form) in sorted(counted.items())
    ]


@dataclass(frozen=True)
class Approved:
    """What happened to one album somebody selected."""

    relpath: str
    name: str
    plan_id: str = ""
    renamed: int = 0
    refused: int = 0
    note: str = ""


def approve_branch(
    conn: sqlite3.Connection,
    settings: Settings,
    folder: str,
    albums: list[str],
    *,
    read_tags=None,  # noqa: ANN001
) -> list[Approved]:
    """One plan per album, and one report line per album. Never one big plan.

    Four thousand renames in a single transaction would be one Commit card
    nobody can check and one Undo that either puts back everything or nothing.
    An album is the unit somebody thinks in, so it is the unit of the decision
    — and each one keeps the semantics the single-folder tool already has:
    the safe members proceed, a collision is refused by name, and a file with
    no trustworthy title is left alone.

    Nothing is silently included and nothing is silently dropped. An album
    that turns out to have nothing to do says so rather than producing an
    empty plan, and an album that refuses everything says that too.
    """
    folder = folder.strip("/")
    _assert_music(folder)
    #  Every album in the branch, not the page somebody was looking at: a
    #  selection is checked against what is really there, and a name that is
    #  not one of them is refused rather than normalized.
    known = {album.relpath for album in _albums(settings, folder)}
    found: list[Approved] = []
    for album in albums:
        album = album.strip("/")
        if album not in known:
            #  A folder that is not one of this branch's albums. Refused
            #  rather than normalized: the form said what it was about.
            raise NormalizeError(f"{album} is not an album under {folder}")
        found.append(_approve_album(conn, settings, album, read_tags=read_tags))
    return found


def _approve_album(
    conn: sqlite3.Connection,
    settings: Settings,
    album: str,
    *,
    read_tags=None,  # noqa: ANN001
) -> Approved:
    """One album's plan, or the reason there is not one.

    A refusal for one album is not a refusal for the others. They are separate
    decisions on purpose, so one collision cannot stop the four albums beside
    it from being tidied.
    """
    name = PurePosixPath(album).name
    try:
        found = preview(conn, settings, album, read_tags=read_tags)
    except NormalizeError as exc:
        return Approved(relpath=album, name=name, note=str(exc))
    if not found.anything_to_do:
        return Approved(
            relpath=album,
            name=name,
            refused=len(found.blocked),
            note=(
                "nothing to change here"
                if not found.blocked
                else "every rename here would land on another file"
            ),
        )
    try:
        plan_id = plan_normalization(conn, settings, album, read_tags=read_tags)
    except NormalizeError as exc:
        return Approved(relpath=album, name=name, note=str(exc))
    return Approved(
        relpath=album,
        name=name,
        plan_id=plan_id,
        renamed=len(found.renaming),
        refused=len(found.blocked),
    )


def _assert_music(folder: str) -> None:
    parts = [part for part in folder.split("/") if part]
    if not parts or parts[0] != MUSIC_ROOT:
        raise NormalizeError(
            "this is a tool for Music. Music Videos have their own naming, and "
            "nothing else is renamed by it."
        )


def _member(
    conn: sqlite3.Connection, settings: Settings, relpath: str, tags_of
) -> Member:  # noqa: ANN001
    """One file's proposed name, or the reason it has none."""
    name = PurePosixPath(relpath).name
    suffix = PurePosixPath(name).suffix
    known = _known(conn, settings, relpath, tags_of)
    if known is None:
        return Member(
            relpath=relpath,
            proposed=name,
            state=UNKNOWN,
            reason=(
                "LibrAIry has no title for this file other than its current "
                "name, and a name cannot be read back into one it can trust."
            ),
        )
    title, track, disc, discs, source = known
    proposed = canonical_name(title, suffix, track=track, disc=disc, discs=discs)
    if proposed == name:
        return Member(relpath=relpath, proposed=name, state=CURRENT, source=source)
    return Member(relpath=relpath, proposed=proposed, state=RENAME, source=source)


def _known(
    conn: sqlite3.Connection, settings: Settings, relpath: str, tags_of
):  # noqa: ANN001, ANN202
    """Title, track, disc and where they came from — or None.

    Two sources, in this order, and both of them are records rather than
    readings: what the file says about itself, and what a catalog said about
    it when somebody asked. The filename contributes only its track number,
    and only when it is in the grammar LibrAIry itself writes — a number it
    wrote is a number it can read.
    """
    from librairy.audit_music import track_number

    tags = tags_of(settings, relpath) or {}
    read = parse(PurePosixPath(relpath).name)
    #  A leading number is the one thing a filename says unambiguously, in the
    #  current grammar and in the house style that came before it. Read by the
    #  same function the numbering detector uses, so "what track is this" has
    #  one answer in this program rather than two.
    numbered = read.track or (track_number(relpath) or 0)
    title = str(tags.get("title") or "").strip()
    if title:
        return (
            title,
            _number(tags.get("track") or tags.get("tracknumber")) or numbered,
            _number(tags.get("discnumber") or tags.get("disc")),
            _number(tags.get("disctotal") or tags.get("totaldiscs"))
            or _total(tags.get("discnumber") or tags.get("disc")),
            "the file's own tags",
        )
    identified = _identified(conn, relpath)
    if identified:
        return (identified, numbered, read.disc, 0, "a catalog identity you asked for")
    return None


def _identified(conn: sqlite3.Connection, relpath: str) -> str:
    from librairy.track_identity import recall

    row = conn.execute(
        "SELECT id, fingerprint FROM items WHERE root='library' AND relpath=?",
        (relpath,),
    ).fetchone()
    if row is None:
        return ""
    identity = recall(
        conn, int(row["id"]), fingerprint=str(row["fingerprint"] or "")
    )
    return identity.title if identity and identity.matched else ""


def _checked(
    settings: Settings, member: Member, wanted: dict[str, int], folder: str
) -> Member:
    """A rename that would land on something is not a rename.

    No auto-numbering and no overwrite — the same rule the rest of LibrAIry
    keeps. Two files wanting one name is refused for both of them, because
    picking a winner is a decision nobody asked this tool to make.
    """
    if member.state != RENAME:
        return member
    if wanted.get(member.proposed, 0) > 1:
        return Member(
            relpath=member.relpath,
            proposed=member.proposed,
            state=COLLIDES,
            reason=f"Another file in this folder would also be called {member.proposed}.",
            source=member.source,
        )
    if (settings.library_dir / folder / member.proposed).exists():
        return Member(
            relpath=member.relpath,
            proposed=member.proposed,
            state=COLLIDES,
            reason=f"{member.proposed} already exists in this folder.",
            source=member.source,
        )
    return member


def _tags_of(settings: Settings, relpath: str) -> dict[str, str]:
    """Embedded tags, via the probe the audit already uses. Best effort."""
    from librairy.tools.ffprobe import probe

    try:
        result = probe(settings.library_dir / relpath, settings)
    except Exception:  # noqa: BLE001 - an unreadable file simply has no tags
        return {}
    raw = result.data.get("tags") if result.ok and isinstance(result.data, dict) else None
    return {str(k).lower(): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


def _number(value) -> int:  # noqa: ANN001
    text = str(value or "").split("/", 1)[0].strip()
    return int(text) if text.isdigit() else 0


def _total(value) -> int:  # noqa: ANN001
    parts = str(value or "").split("/", 1)
    return int(parts[1].strip()) if len(parts) == 2 and parts[1].strip().isdigit() else 0


# --- the plan -------------------------------------------------------------------------


def plan_normalization(
    conn: sqlite3.Connection,
    settings: Settings,
    folder: str,
    *,
    read_tags=None,  # noqa: ANN001
) -> str:
    """One approved plan holding one move per rename, and nothing else.

    An ordinary Library correction: the same immutable plan, the same
    hash-verified execution, the same Commit card and the same Undo. There is
    no rename operation type and no naming executor — a rename is a move to a
    different name in the same folder, which is what the executor has always
    done.
    """
    found = preview(conn, settings, folder, read_tags=read_tags)
    if not found.anything_to_do:
        #  Nothing to approve is not an empty plan. A plan with no operations
        #  would still appear on Commit, still be committed, and still write a
        #  History entry saying a decision was carried out.
        raise NormalizeError("these filenames already match the current convention")
    specs = [
        OperationSpec(
            op_type="move",
            src_root="library",
            src_relpath=member.relpath,
            dest_root="library",
            dest_relpath=member.dest_relpath,
        )
        for member in found.renaming
    ]
    plan_id = create_plan(conn, specs, settings)
    #  Coherent: eleven halves of an album renamed and the rest not is a folder
    #  in two conventions at once, which is worse than the one convention it
    #  started in. One decision, and the executor revalidates it as one.
    conn.execute("UPDATE plans SET coherent=1 WHERE id=?", (plan_id,))
    try:
        approve_plan(conn, plan_id, settings)
    except sqlite3.IntegrityError as exc:
        conn.execute("DELETE FROM plan_ops WHERE plan_id=?", (plan_id,))
        conn.execute("DELETE FROM plans WHERE id=?", (plan_id,))
        raise NormalizeError(
            "one of these files is already waiting for Commit"
        ) from exc
    return plan_id
