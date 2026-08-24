"""Thirty-seven photographs that look alike, and one decision about them.

    Photos/2024/Backyard/
        IMG_5100.jpg  IMG_5101.jpg  IMG_5102.jpg  ... IMG_5136.jpg

`similar_media` was built for the other shape of this question — a FLAC beside
an MP3, an H.264 beside an HEVC — where the answer comes from a table of six
measured numbers and there are two rows in it. A burst off a phone is the same
*evidence* and a completely different *decision*, and until now the difference
was handled by dropping the group: past eight members no finding was written at
all, so thirty-seven files nobody wanted twice were silently invisible.

**Nothing about the grouping changes.** The members still come from czkawka's
own pairs joined into connected components, and from nowhere else — not from
filenames, not from timestamps, not from a model looking at pictures. What
changes is that a large group is *presented* rather than dropped, and presented
as what it is: pictures, on a page, with the facts underneath each one.

**The question is inverted, because thirty-seven is not two.** `Keep A / Keep
B` does not scale to a set; asking it thirty-six times is not a decision, it is
data entry. So the page asks the opposite: **everything is kept, and you untick
what you want set aside.** That direction is chosen deliberately. A group
opened and half-answered and approved by accident sets aside only what somebody
explicitly chose; the other direction would set aside everything they had not
got to yet. Conservative in exactly the place where being wrong is expensive.

**Nothing is preselected and nothing is ranked.** Not by size, not by
resolution, not by date, and certainly not by anything resembling a judgement
about which photograph is better — that is the whole content of the decision
and it is not a fact this program has. The facts under each picture are the
ones something measured: pixels, format, bytes, when the file says it was
taken. `Best`, `recommended` and `low quality` appear nowhere.

**Bounded at every scale.** Five hundred members is five hundred rows in the
database and twenty-four pictures on the screen. The whole-group facts —
counts, formats, which members are byte-identical — come from columns the index
already has, so they cost one query. The measured facts cost **one** exiftool
call for the page, never one per photograph, and never one per member of the
group. Thumbnails are the browser's problem, fetched lazily from the route that
already renders them one at a time.

**Exact copies stay exact.** A group can contain files with identical bytes —
they arrive here because each is *similar* to some third file — and making
somebody eyeball two pictures whose fingerprints match would be asking a
question that has already been answered. They are labelled, counted and can be
filtered to; they are never resolved automatically, because deleting is not
something this program does.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import PurePosixPath

from librairy.config import Settings
from librairy.planner import utc_now
from librairy.similar_media import (
    LARGE,
    PAGE_MEMBERS,
    SMALL,
    SMALL_GROUP,
    _connected,
    is_similar_finding,
)

KEEP = "keep"
SET_ASIDE = "set-aside"

#  Sorting is offered only over facts the index already holds, and that is a
#  boundedness argument rather than a shortage of imagination. Sorting five
#  hundred photographs by capture time means reading five hundred files, which
#  is the one thing this design refuses to do — so capture time is *shown* for
#  the page and never sorted on, and the date offered here is the file's own,
#  named as such rather than dressed up as when the shutter fired.
SORTS = {
    "path": "where they are",
    "name": "filename",
    "size": "file size",
    "date": "file date",
}
DEFAULT_SORT = "path"

FILTERS = {"exact": "byte-identical copies"}


@dataclass(frozen=True)
class Photo:
    """One member of a visual group, and what is known about it cheaply."""

    item_id: int
    relpath: str
    size: int
    mtime_ns: int
    fingerprint: str
    #  0 when these bytes are unique in the group; otherwise which set of
    #  byte-identical copies it belongs to, numbered from 1 so a template can
    #  say "copy set 2" without knowing a hash.
    exact_set: int = 0
    decision: str = KEEP
    #  Measured, and only for the members on the page somebody is looking at.
    facts: tuple[tuple[str, str], ...] = ()

    @property
    def name(self) -> str:
        return PurePosixPath(self.relpath).name

    @property
    def folder(self) -> str:
        return str(PurePosixPath(self.relpath).parent)

    @property
    def format(self) -> str:
        """`JPG`. From the extension, which costs nothing and is not a guess."""
        return PurePosixPath(self.relpath).suffix.lstrip(".").upper()

    @property
    def file_date(self) -> str:
        """When the filesystem says the file was last written. Not capture time.

        Named `File date` wherever it is printed. For a photo straight off a
        card the two are often the same, and "often" is not a fact worth
        printing under somebody's photographs as if it were one.
        """
        if not self.mtime_ns:
            return ""
        return datetime.fromtimestamp(self.mtime_ns / 1e9, tz=UTC).strftime(
            "%Y-%m-%d %H:%M"
        )

    @property
    def kept(self) -> bool:
        return self.decision != SET_ASIDE

    @property
    def exact(self) -> bool:
        return self.exact_set > 0


@dataclass(frozen=True)
class Group:
    """A visual group, one page of it, and the answer so far."""

    finding_id: int
    total: int
    members: tuple[Photo, ...]
    kept: int
    set_aside: int
    exact_sets: int
    exact_members: int
    formats: tuple[tuple[str, int], ...]
    page: int = 1
    sort: str = DEFAULT_SORT
    only: str = ""
    has_next: bool = False
    matching: int = 0

    @property
    def shape(self) -> str:
        return LARGE if self.total > SMALL_GROUP else SMALL

    @property
    def pages(self) -> int:
        return max(1, -(-self.matching // PAGE_MEMBERS))

    @property
    def resolvable(self) -> bool:
        """Something has to be kept. Setting aside every one is not tidying."""
        return self.kept >= 1

    @property
    def anything_to_do(self) -> bool:
        return self.set_aside > 0


# --- reading the group ---------------------------------------------------------------


def size_of(conn: sqlite3.Connection, row: sqlite3.Row) -> int:
    """How many members, without loading any of them.

    Review draws this for every row on the page, so it must not turn into a
    page of group loads. One walk of the pairs and a length.
    """
    if not is_similar_finding(row):
        return 0
    anchor = conn.execute(
        "SELECT id FROM items WHERE root='library' AND relpath=? AND missing_since IS NULL",
        (row["relpath"],),
    ).fetchone()
    if anchor is None:
        return 0
    return len(_connected(conn, int(anchor["id"])))


def is_large(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    return size_of(conn, row) > SMALL_GROUP


def load(
    conn: sqlite3.Connection,
    settings: Settings,
    row: sqlite3.Row,
    *,
    page: int = 1,
    sort: str = DEFAULT_SORT,
    only: str = "",
    fmt: str = "",
    measure: bool = True,
) -> Group | None:
    """One page of a visual group, with the whole group's counts.

    The counts are over every member and the pictures are over one page — which
    is the shape the decision needs. "37 photos, 4 exact-copy sets, 2 formats"
    is what tells somebody whether this is worth ten minutes; twenty-four
    thumbnails is what they can actually look at.
    """
    if not is_similar_finding(row):
        return None
    everyone = _all(conn, settings, row)
    if len(everyone) < 2:
        return None
    chosen = choices(conn, int(row["id"]))
    everyone = [
        replace(photo, decision=chosen.get(photo.item_id, KEEP)) for photo in everyone
    ]
    matching = _filtered(everyone, only=only, fmt=fmt)
    ordered = _sorted(matching, sort)
    page = max(1, page)
    window = ordered[(page - 1) * PAGE_MEMBERS : page * PAGE_MEMBERS]
    if measure:
        window = _measured(settings, window)
    return Group(
        finding_id=int(row["id"]),
        total=len(everyone),
        members=tuple(window),
        kept=sum(1 for photo in everyone if photo.kept),
        set_aside=sum(1 for photo in everyone if not photo.kept),
        exact_sets=len({photo.exact_set for photo in everyone if photo.exact_set}),
        exact_members=sum(1 for photo in everyone if photo.exact),
        formats=_formats(everyone),
        page=page,
        sort=sort if sort in SORTS else DEFAULT_SORT,
        only=only if only in FILTERS else "",
        has_next=len(ordered) > page * PAGE_MEMBERS,
        matching=len(ordered),
    )


def _all(
    conn: sqlite3.Connection, settings: Settings, row: sqlite3.Row
) -> list[Photo]:
    """Every member, from the index, with no file touched.

    Deliberately one query per member and nothing more. The fingerprint comes
    from the index because that is what makes exact copies findable without
    reading anything, and `mtime_ns` because the scanner already recorded it.
    """
    from librairy.paths import PathValidationError, validate_relpath

    anchor = conn.execute(
        "SELECT id FROM items WHERE root='library' AND relpath=? AND missing_since IS NULL",
        (row["relpath"],),
    ).fetchone()
    if anchor is None:
        return []
    ids = _connected(conn, int(anchor["id"]))
    found: list[Photo] = []
    for item_id in ids:
        item = conn.execute(
            "SELECT id, relpath, size, mtime_ns, fingerprint FROM items"
            " WHERE id=? AND root='library' AND missing_since IS NULL",
            (item_id,),
        ).fetchone()
        if item is None:
            continue
        try:
            path = validate_relpath(
                settings.library_dir, str(item["relpath"]), kind="finding"
            )
        except PathValidationError:
            continue
        if not path.is_file():
            continue
        found.append(
            Photo(
                item_id=int(item["id"]),
                relpath=str(item["relpath"]),
                size=int(item["size"] or 0),
                mtime_ns=int(item["mtime_ns"] or 0),
                fingerprint=str(item["fingerprint"] or ""),
            )
        )
    return _numbered(sorted(found, key=lambda photo: photo.relpath))


def _numbered(photos: list[Photo]) -> list[Photo]:
    """Mark the members whose bytes are identical to another member's.

    Exact duplicate semantics are stronger than similarity, and this is where
    the two meet: a group is built from *similar* pairs, so two byte-identical
    files can end up in one when each resembles some third picture. Asking
    somebody to compare two files whose fingerprints match would be asking a
    question the hashes already answered.
    """
    counted: dict[str, int] = {}
    for photo in photos:
        if photo.fingerprint:
            counted[photo.fingerprint] = counted.get(photo.fingerprint, 0) + 1
    #  Numbered by first appearance, so the labels read in the order somebody
    #  scrolls past them rather than in hash order.
    numbers: dict[str, int] = {}
    next_number = 0
    for index, photo in enumerate(photos):
        if not photo.fingerprint or counted.get(photo.fingerprint, 0) < 2:
            continue
        if photo.fingerprint not in numbers:
            next_number += 1
            numbers[photo.fingerprint] = next_number
        photos[index] = replace(photo, exact_set=numbers[photo.fingerprint])
    return photos


def _filtered(photos: list[Photo], *, only: str, fmt: str) -> list[Photo]:
    """Factual narrowing only. Nothing here is a judgement about a picture."""
    found = photos
    if only == "exact":
        found = [photo for photo in found if photo.exact]
    if fmt:
        found = [photo for photo in found if photo.format == fmt.upper()]
    return found


def _sorted(photos: list[Photo], sort: str) -> list[Photo]:
    """Deterministic in every case, and never an implied recommendation.

    Every order ties-breaks on the path, so two photographs of the same size
    taken in the same second do not swap places between one page load and the
    next — which would move a tick somebody had just placed.
    """
    keys = {
        "path": lambda photo: (photo.relpath,),
        "name": lambda photo: (photo.name.lower(), photo.relpath),
        "size": lambda photo: (photo.size, photo.relpath),
        "date": lambda photo: (photo.mtime_ns, photo.relpath),
    }
    return sorted(photos, key=keys.get(sort, keys["path"]))


def _formats(photos: list[Photo]) -> tuple[tuple[str, int], ...]:
    counted: dict[str, int] = {}
    for photo in photos:
        counted[photo.format] = counted.get(photo.format, 0) + 1
    return tuple(sorted(counted.items()))


def _measured(settings: Settings, photos: list[Photo]) -> list[Photo]:
    """Pixels, format and capture time — for this page, in one subprocess.

    One exiftool call for the whole page rather than one per picture: at
    twenty-four members that is the difference between a page and a wait. A
    file exiftool cannot read simply has no facts, which is a normal outcome
    for a group and never an error.

    No network, no catalog, no model, and nothing that decodes an image.
    """
    from librairy.tools.exiftool import extract_many

    if not photos:
        return photos
    paths = [settings.library_dir / photo.relpath for photo in photos]
    try:
        measured = extract_many(paths, settings)
    except Exception:  # noqa: BLE001 - a missing binary means "no facts"
        return photos
    return [
        replace(photo, facts=_facts(found)) for photo, found in zip(photos, measured, strict=False)
    ]


def _facts(measured) -> tuple[tuple[str, str], ...]:  # noqa: ANN001
    """What exiftool said, in the words a person reads. Facts, never verdicts."""
    if measured is None:
        return ()
    tags = measured.tags or {}
    found: list[tuple[str, str]] = []
    if tags.get("ImageWidth") and tags.get("ImageHeight"):
        found.append(("Pixels", f"{tags['ImageWidth']}×{tags['ImageHeight']}"))
    if measured.created_at:
        found.append(("Taken", str(measured.created_at)))
    if measured.camera:
        found.append(("Camera", str(measured.camera)))
    return tuple(found)


# --- the answer, built up a page at a time --------------------------------------------


def choices(conn: sqlite3.Connection, finding_id: int) -> dict[int, str]:
    """What has been decided about this group so far. Absent means keep."""
    return {
        int(row["item_id"]): str(row["decision"])
        for row in conn.execute(
            "SELECT item_id, decision FROM similar_media_choices WHERE audit_finding_id=?",
            (finding_id,),
        )
    }


def choose(
    conn: sqlite3.Connection,
    settings: Settings,
    finding_id: int,
    item_id: int,
    decision: str,
) -> None:
    """Record one member's answer, checked against the group it belongs to.

    An item that is not part of this group cannot be answered here, however the
    request arrived. That is not paranoia about the form: the identifiers are
    in the page, and a stale page from before somebody deleted half the group
    is the ordinary way a request arrives about a member that is no longer one.
    """
    from librairy.corrections import CorrectionRefused, load_finding

    if decision not in {KEEP, SET_ASIDE}:
        raise CorrectionRefused("that is not an answer about a photo")
    row = load_finding(conn, finding_id)
    found = load(conn, settings, row, measure=False)
    if found is None:
        raise CorrectionRefused("there is nothing left to compare here")
    if item_id not in {photo.item_id for photo in _all(conn, settings, row)}:
        raise CorrectionRefused("that file is not part of this group")
    conn.execute(
        "INSERT INTO similar_media_choices(audit_finding_id, item_id, decision,"
        " created_at) VALUES (?, ?, ?, ?)"
        " ON CONFLICT(audit_finding_id, item_id) DO UPDATE SET"
        " decision=excluded.decision, created_at=excluded.created_at",
        (finding_id, item_id, decision, utc_now()),
    )


def forget(conn: sqlite3.Connection, finding_id: int) -> None:
    """Throw away a half-made selection. Used when the decision is settled."""
    conn.execute(
        "DELETE FROM similar_media_choices WHERE audit_finding_id=?", (finding_id,)
    )


def approve(
    conn: sqlite3.Connection, settings: Settings, finding_id: int
) -> str:
    """Turn the selection into the ordinary similar-media decision.

    No new planner and no new executor: what comes out is exactly what keeping
    one of four produces, with twenty-nine quarantine operations in it instead
    of three. One plan, one Commit card, one journal entry, one Undo — because
    the person made **one** comparison decision, however many files it moves.
    """
    from librairy.corrections import CorrectionRefused, load_finding
    from librairy.similar_media import resolve

    row = load_finding(conn, finding_id)
    found = load(conn, settings, row, measure=False)
    if found is None:
        raise CorrectionRefused("there is nothing left to compare here")
    everyone = _all(conn, settings, row)
    chosen = choices(conn, finding_id)
    keep = [
        photo.relpath
        for photo in everyone
        if chosen.get(photo.item_id, KEEP) != SET_ASIDE
    ]
    if not keep:
        #  `resolve` refuses this too. Saying it here means the message names
        #  photographs rather than representations.
        raise CorrectionRefused("keep at least one of these photos")
    plan_id = resolve(conn, settings, finding_id, keep)
    forget(conn, finding_id)
    return plan_id
