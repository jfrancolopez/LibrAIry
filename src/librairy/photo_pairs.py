"""RAW beside its JPEG, and a Live Photo's two halves.

Both pairings were **refused** when relationships were first written down, and
the refusal was correct. The only evidence available then was a shared filename
stem, and the counterexample is the ordinary case rather than the exotic one:

    IMG_9323.jpeg   a photograph
    IMG_9323.MOV    an unrelated clip from the same camera, minutes apart

Eight folders in the author's own inbox look like that and seven of them are
phone camera folders. Pairing on the stem would have invented a fact about
somebody's family photographs — so the rule here is that **a shared name is the
reason to look, never the reason to believe**.

What changed is the metadata cache. `exiftool-image` holds capture time, camera
and Apple's content identifier against the exact bytes they were read from, so
the question can be answered from what the files record.

    live_photo    the two halves carry the SAME Apple content identifier.
                  That is proof: the device wrote one id into both when it made
                  them. No identifier, no pairing — there is no fallback, and
                  the near-miss rule below is deliberately not offered here.

    raw_render    same folder, same stem, one RAW and one rendered image, and
                  the metadata agrees: same camera, and capture times within
                  `SECONDS`. A camera writes both files in one shutter action,
                  so a two-second gap is generous; two unrelated files that
                  happen to share a stem will not also share a camera and a
                  moment.

Neither is a duplicate, and neither is a recommendation. A RAW and its JPEG are
usually both wanted — one to keep, one to send — and nothing here preselects
either for removal.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath

from librairy.live import live
from librairy.relationships import LIVE_PHOTO, RAW_RENDER, record

#  Formats a camera writes as its unprocessed original.
RAW_EXTS = frozenset({
    ".cr2", ".cr3", ".crw", ".nef", ".nrw", ".arw", ".srf", ".sr2", ".raf",
    ".orf", ".rw2", ".pef", ".dng", ".raw", ".3fr", ".erf", ".kdc", ".mrw",
    ".x3f", ".iiq",
})
#  What a camera or phone writes beside it, already developed.
RENDER_EXTS = frozenset({".jpg", ".jpeg", ".heic", ".heif", ".png", ".webp"})
#  The still half of a Live Photo, and the moving half.
STILL_EXTS = frozenset({".heic", ".heif", ".jpg", ".jpeg"})
MOTION_EXTS = frozenset({".mov", ".mp4"})

#  How far apart two halves of one shutter action may be recorded.
#
#  A camera writing a RAW and a JPEG timestamps them from the same exposure, so
#  in practice they are identical. Two seconds is slack for a body that stamps
#  each file as it finishes writing it, and it is still far tighter than the
#  gap between two deliberate photographs.
SECONDS = 2.0

#  How many pairs one pass will consider. The candidates come from a single
#  grouped query and the work per candidate is a dictionary lookup, but a
#  library reorganised in one go should still catch up over several audits
#  rather than in one that never finishes.
PER_RUN = 2000


@dataclass(frozen=True)
class Candidate:
    """One file, and what its bytes were measured to say."""

    item_id: int
    root: str
    relpath: str
    facts: dict

    @property
    def folder(self) -> str:
        #  Rooted, so an arriving JPEG and a filed RAW that happen to share a
        #  folder name inside their own roots are not read as one exposure.
        return f"{self.root}:{PurePosixPath(self.relpath).parent.as_posix()}"

    @property
    def stem(self) -> str:
        return PurePosixPath(self.relpath).stem.casefold()

    @property
    def suffix(self) -> str:
        return PurePosixPath(self.relpath).suffix.lower()

    @property
    def measured(self) -> bool:
        """Whether anything actually read this file. See `pair` for why."""
        return bool(self.facts)

    @property
    def content_id(self) -> str:
        return str(self.facts.get("content_id") or "").strip()

    @property
    def camera(self) -> str:
        return " ".join(str(self.facts.get("camera") or "").split()).casefold()

    @property
    def taken(self) -> datetime | None:
        return _moment(str(self.facts.get("taken") or ""))


#  How many files one pass will read metadata from.
#
#  One exiftool invocation for the batch, not one per file — the same bound
#  the photo grid's `Measure these photos` uses, for the same reason. A library
#  of forty thousand photographs catches up over several audits rather than in
#  one that never finishes, and the honest consequence is that a pair is not
#  known until both halves have been read.
MEASURE_PER_RUN = 300

#  Where pairing looks. Both, because a pair has to be establishable on either
#  side of filing — but each orchestration names its own, so the worker keeping
#  an arriving camera card current never spends its budget on the library.
ROOTS = ("library", "inbox")


def measure(
    conn: sqlite3.Connection,
    settings,  # noqa: ANN001
    *,
    limit: int = MEASURE_PER_RUN,
    roots: tuple[str, ...] = ROOTS,
) -> int:
    """Read capture metadata for images and clips that have none.

    Subprocess work, so it belongs to an explicit staged run or to the worker's
    own analysis phase, and never to a page. Recorded against the exact bytes
    it was read from, which is what makes a stale answer a miss rather than a
    wrong pairing.

    `roots` is how one implementation serves two orchestrations. The staged
    audit catches the library up; the worker keeps the *inbox* current, so a
    camera card knows its Live Photos before anybody is asked where to file
    them. Same evidence, same rules, different budget.
    """
    from librairy.photo_group import _payload as image_payload
    from librairy.planner import utc_now
    from librairy.tools.common import IMAGE_TOOL, set_cached_metadata
    from librairy.tools.exiftool import extract_many

    wanted = [
        candidate
        for candidate in _candidates(conn, limit=limit * 4, roots=roots)
        if not candidate.measured
    ][:limit]
    if not wanted:
        return 0
    prints = {
        int(row["id"]): str(row["fingerprint"] or "")
        for row in conn.execute(
            "SELECT id, fingerprint FROM items WHERE id IN "
            f"({','.join('?' * len(wanted))})",  # noqa: S608 - counted placeholders
            [candidate.item_id for candidate in wanted],
        )
    }
    roots = {"library": settings.library_dir, "inbox": settings.inbox_dir}
    paths = [roots[candidate.root] / candidate.relpath for candidate in wanted]
    try:
        measured = extract_many(paths, settings)
    except Exception:  # noqa: BLE001 - a missing binary means "no facts"
        return 0
    counted = 0
    for candidate, found in zip(wanted, measured, strict=False):
        fingerprint = prints.get(candidate.item_id, "")
        if not fingerprint or found is None:
            continue
        set_cached_metadata(
            conn, candidate.item_id, fingerprint, IMAGE_TOOL,
            image_payload(found), utc_now(),
        )
        counted += 1
    return counted


def pair(
    conn: sqlite3.Connection, *, limit: int = PER_RUN, roots: tuple[str, ...] = ROOTS
) -> int:
    """Record the photographic companions the measured metadata establishes.

    Reads the cache and nothing else. A file nobody has measured has no facts,
    and a file with no facts is not paired with anything — which is the honest
    outcome and the reason this can never be the thing that makes a page slow.
    """
    candidates = _candidates(conn, limit=limit, roots=roots)
    written = 0
    written += _live_photos(conn, candidates)
    written += _raw_renders(conn, candidates)
    return written


def _live_photos(conn: sqlite3.Connection, candidates: list[Candidate]) -> int:
    """Two halves the device itself said were one thing.

    Grouped by the identifier alone — not by folder and not by name — because
    the identifier *is* the claim. A Live Photo whose halves were filed into
    different folders is still a Live Photo.
    """
    by_id: dict[tuple[str, str], list[Candidate]] = {}
    for candidate in candidates:
        if not candidate.content_id:
            continue
        by_id.setdefault((candidate.root, candidate.content_id), []).append(candidate)
    written = 0
    for (_root, identifier), group in by_id.items():
        stills = [item for item in group if item.suffix in STILL_EXTS]
        motions = [item for item in group if item.suffix in MOTION_EXTS]
        if not stills or not motions:
            continue
        for still in stills:
            for motion in motions:
                record(
                    conn,
                    companion_item_id=motion.item_id,
                    subject_item_id=still.item_id,
                    kind=LIVE_PHOTO,
                    provenance=f"same Live Photo identifier {_short(identifier)}",
                )
                written += 1
    return written


def _raw_renders(conn: sqlite3.Connection, candidates: list[Candidate]) -> int:
    """A RAW and the developed image beside it, where the metadata agrees.

    The stem narrows the field; the metadata decides. Both halves have to have
    been measured — an unmeasured file cannot agree with anything, and treating
    silence as agreement is exactly the basename rule this replaces.
    """
    by_key: dict[tuple[str, str], list[Candidate]] = {}
    for candidate in candidates:
        by_key.setdefault((candidate.folder, candidate.stem), []).append(candidate)
    written = 0
    for group in by_key.values():
        raws = [item for item in group if item.suffix in RAW_EXTS]
        renders = [item for item in group if item.suffix in RENDER_EXTS]
        for raw in raws:
            for render in renders:
                why = _agrees(raw, render)
                if not why:
                    continue
                record(
                    conn,
                    companion_item_id=render.item_id,
                    subject_item_id=raw.item_id,
                    kind=RAW_RENDER,
                    provenance=why,
                )
                written += 1
    return written


def _agrees(raw: Candidate, render: Candidate) -> str:
    """Why these two are one exposure, or "" if the metadata does not say so.

    Returns the sentence rather than a boolean, because the sentence is what
    the relationship stores: a pairing that cannot say which rule matched is
    not evidence, it is an assertion.
    """
    if not raw.measured or not render.measured:
        return ""
    #  A camera that ties them explicitly settles it without the clock.
    shared = str(raw.facts.get("unique_id") or "").strip()
    if shared and shared == str(render.facts.get("unique_id") or "").strip():
        return f"same camera image id {_short(shared)}"
    left, right = raw.taken, render.taken
    if left is None or right is None:
        #  Measured, and the camera recorded no capture time. The stem is all
        #  that is left, and the stem is not evidence.
        return ""
    gap = abs((left - right).total_seconds())
    if gap > SECONDS:
        return ""
    if not raw.camera or raw.camera != render.camera:
        return ""
    when = "the same moment" if gap < 0.5 else f"{gap:.1f}s apart"
    return f"same camera and {when}"


def _candidates(
    conn: sqlite3.Connection, *, limit: int, roots: tuple[str, ...] = ROOTS
) -> list[Candidate]:
    """Library images and clips, with whatever has been measured about them.

    A LEFT JOIN on purpose: a file with no cache row is a real candidate that
    simply has nothing to say yet, and it has to appear so that "nobody
    measured this" stays distinguishable from "it was measured and disagreed".
    """
    from librairy.tools.common import IMAGE_TOOL

    suffixes = sorted(RAW_EXTS | RENDER_EXTS | MOTION_EXTS)
    #  SQLite's LIKE is case-insensitive for ASCII, which is exactly right
    #  here: a camera writes `.CR2` and a phone writes `.heic`, and both are
    #  the same extension.
    matches = " OR ".join("i.relpath LIKE ?" for _ in suffixes)
    places = ",".join("?" * len(roots))
    rows = conn.execute(
        f"""
        SELECT i.id, i.root, i.relpath, m.payload,
               m.fingerprint AS measured_fp, i.fingerprint
        FROM items i
        LEFT JOIN item_metadata m ON m.item_id = i.id AND m.tool = ?
        WHERE i.root IN ({places}) AND {live()} AND ({matches})
        ORDER BY i.relpath
        LIMIT ?
        """,  # noqa: S608 - the clause is built from a module constant
        (IMAGE_TOOL, *roots, *[f"%{suffix}" for suffix in suffixes], limit),
    ).fetchall()
    return [
        Candidate(
            item_id=int(row["id"]),
            root=str(row["root"]),
            relpath=str(row["relpath"]),
            #  Only facts read from *these* bytes. A cached payload whose
            #  fingerprint no longer matches describes a file that has been
            #  replaced, and pairing on it would carry an old exposure's
            #  metadata onto a new picture.
            facts=_payload(row)
            if row["measured_fp"] and row["measured_fp"] == row["fingerprint"]
            else {},
        )
        for row in rows
    ]


def _payload(row: sqlite3.Row) -> dict:
    try:
        found = json.loads(str(row["payload"] or "{}"))
    except (TypeError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


def _moment(value: str) -> datetime | None:
    """EXIF's own date format, and ISO for anything already normalised."""
    text = value.strip()
    if not text:
        return None
    for shape in ("%Y:%m:%d %H:%M:%S", "%Y:%m:%d %H:%M:%S%z", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text[: len(text)], shape)  # noqa: DTZ007
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _short(value: str) -> str:
    """An identifier a person can compare without reading thirty-six characters."""
    text = str(value)
    return text if len(text) <= 12 else f"{text[:8]}…{text[-4:]}"
