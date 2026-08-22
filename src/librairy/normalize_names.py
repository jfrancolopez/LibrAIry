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
