"""Two versions already filed, and one of them becoming *the* version.

    Music/Rock/Queen/A Night at the Opera/01 - Song.mp3
    Music/Rock/Queen/A Night at the Opera/alternate/01 - Song.flac

`similar_media` has always been able to answer this one way: keep the FLAC, and
the MP3 goes to Quarantine. That is a **set-aside** — one file leaves, and the
survivor stays exactly where it was, which for the pair above means the good
version is still in a folder called `alternate`.

The other answer is a **replacement**: the FLAC takes the MP3's slot and the
MP3 is preserved. Same three files at the end, entirely different library. The
two are not shades of one control, so they do not share a button: `Keep X`
means one thing and `Use X as the active version` means another, and
overloading either would be the software saying two things with one word.

**Why this is not offered on every similar pair.** czkawka's similarity says
two files look or sound alike; it says nothing about whether they are the same
release of the same recording, and moving a file into another's path on that
basis would be reorganising a library on the strength of a perceptual hash. So
replacement requires the two files to carry the **same MusicBrainz recording
identity**, established by the fingerprint lookup somebody asked for and stored
against the exact bytes of each file. Everything weaker keeps the answer it
already had: set aside, or keep both.

**The slot is not guessed.** It is the path of the version being displaced —
directory and stem — with the chosen file's own extension, because a FLAC is
not an MP3. Nothing is derived from which path is shorter, newer or first
alphabetically; those are properties of a string, not decisions about a
library. Three members mean two possible slots and no way to know which, so a
group that is not a pair is set-aside only.

What it builds is the coherent swap `replacement.py` spells for all three
directions: preserve first, admit second, both or neither, no overwrite. The
displaced version lands in Quarantine as a replaced representation, with the
comparison provenance that lets it be swapped back from there.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import PurePosixPath

from librairy.config import Settings
from librairy.fingerprint import blake2b_file
from librairy.paths import PathValidationError, validate_dest, validate_relpath


@dataclass(frozen=True)
class Side:
    """One of the two filed versions."""

    item_id: int
    relpath: str
    size: int
    recording_id: str

    @property
    def name(self) -> str:
        return PurePosixPath(self.relpath).name

    @property
    def folder(self) -> str:
        return str(PurePosixPath(self.relpath).parent)


@dataclass(frozen=True)
class Swap:
    """Making one filed version the active one, and what that displaces."""

    chosen: Side
    displaced: Side
    dest_relpath: str
    #  Where the owner's declared music format preference points. These two are
    #  already known to be the same MusicBrainz recording — that is what makes
    #  this control exist at all — so a preference about containers is applying
    #  what somebody said rather than deciding what something is.
    preferred: bool = False

    @property
    def same_path(self) -> bool:
        """Would the chosen file land exactly where the displaced one is?"""
        return self.dest_relpath == self.displaced.relpath

    @property
    def pointless(self) -> bool:
        """Is the chosen file already where this would put it?

        Two versions in one folder differing only by extension: making the
        FLAC active moves it onto its own path, which is not a decision and
        must not become a plan that says it was one.
        """
        return self.dest_relpath == self.chosen.relpath


def swaps_for(
    conn: sqlite3.Connection, settings: Settings, row: sqlite3.Row
) -> tuple[Swap, ...]:
    """Both directions this comparison could be resolved by replacement.

    Empty for every group that is not an eligible pair, which is most of them —
    and empty is the honest answer there rather than a button that refuses when
    pressed. The row keeps `Keep X` and `Keep all of them` either way.
    """
    from librairy.similar_media import compare

    found = compare(conn, settings, row, measure=False)
    if found is None or len(found.members) != 2:
        return ()
    sides = [_side(conn, settings, member.relpath) for member in found.members]
    if any(side is None for side in sides):
        return ()
    first, second = sides  # type: ignore[misc]
    if not _same_recording(first, second):
        return ()
    from librairy.format_preference import prefer_among

    wanted = prefer_among(conn, [first.relpath, second.relpath])
    swaps = []
    for chosen, displaced in ((first, second), (second, first)):
        swap = Swap(
            chosen=chosen,
            displaced=displaced,
            dest_relpath=_slot(displaced.relpath, chosen.relpath),
            preferred=bool(wanted) and chosen.relpath == wanted,
        )
        if not swap.pointless:
            swaps.append(swap)
    return tuple(swaps)


def _same_recording(first: Side, second: Side) -> bool:
    """The one piece of evidence strong enough to move a file into a path.

    Both sides identified, by the audio rather than by resemblance, to the same
    MusicBrainz recording. A shared perceptual-hash flag is not this: it says
    these sound alike, which is true of a song and its live version.
    """
    return bool(first.recording_id) and first.recording_id == second.recording_id


def _slot(displaced_relpath: str, chosen_relpath: str) -> str:
    """Where the displaced version lives, with the chosen one's extension."""
    from librairy.arrival_comparison import _destination

    return _destination(displaced_relpath, chosen_relpath)


def _side(
    conn: sqlite3.Connection, settings: Settings, relpath: str
) -> Side | None:
    from librairy.track_identity import recall

    row = conn.execute(
        "SELECT id, size, fingerprint FROM items"
        " WHERE root='library' AND relpath=? AND missing_since IS NULL",
        (relpath,),
    ).fetchone()
    if row is None:
        return None
    identity = recall(conn, int(row["id"]), fingerprint=str(row["fingerprint"] or ""))
    return Side(
        item_id=int(row["id"]),
        relpath=relpath,
        size=int(row["size"] or 0),
        recording_id=identity.recording_id if identity and identity.matched else "",
    )


# --- the decision --------------------------------------------------------------------


def make_active(
    conn: sqlite3.Connection, settings: Settings, finding_id: int, relpath: str
) -> str:
    """Make one filed version the active one. Returns the plan id.

    Every refusal here is a refusal to act on something that has changed since
    the question was asked: a file re-encoded, a file moved, a third file
    standing in the slot. The comparison in front of somebody has to be the
    comparison the plan is about.
    """
    from librairy.corrections import CorrectionRefused, load_finding
    from librairy.replacement import approve_coherent, swap_specs

    row = load_finding(conn, finding_id)
    swap = next(
        (swap for swap in swaps_for(conn, settings, row) if swap.chosen.relpath == relpath),
        None,
    )
    if swap is None:
        raise CorrectionRefused(
            "these two are not known to be the same recording, so one cannot "
            "take the other's place"
        )
    if _claimed(conn, swap):
        raise CorrectionRefused("one of these files is already waiting for Commit")
    _assert_current(conn, settings, swap.chosen.relpath)
    _assert_current(conn, settings, swap.displaced.relpath)
    try:
        destination = validate_dest(settings.library_dir, swap.dest_relpath)
    except (PathValidationError, ValueError) as exc:
        raise CorrectionRefused(
            f"{swap.chosen.name} has no safe destination: {exc}"
        ) from exc
    if destination.exists() and not swap.same_path:
        #  Something that is neither of these two is standing where this would
        #  land. Renumbering it would invent a name nobody approved.
        raise CorrectionRefused(
            f"{PurePosixPath(swap.dest_relpath).name} already exists and is not "
            f"the version you are replacing"
        )
    plan_id = approve_coherent(
        conn,
        settings,
        swap_specs(
            preserve=swap.displaced.relpath,
            source_root="library",
            source_relpath=swap.chosen.relpath,
            dest_relpath=swap.dest_relpath,
        ),
        error=CorrectionRefused,
    )
    conn.execute(
        "UPDATE plans SET audit_finding_id=? WHERE id=?", (finding_id, plan_id)
    )
    return plan_id


def _claimed(conn: sqlite3.Connection, swap: Swap) -> bool:
    from librairy.correction_state import ACTIVE_PLAN_STATUSES

    statuses = ",".join("?" * len(ACTIVE_PLAN_STATUSES))
    return (
        conn.execute(
            f"SELECT 1 FROM plan_ops o JOIN plans p ON p.id = o.plan_id"  # noqa: S608
            f" WHERE o.src_relpath IN (?, ?) AND p.status IN ({statuses}) LIMIT 1",
            (swap.chosen.relpath, swap.displaced.relpath, *ACTIVE_PLAN_STATUSES),
        ).fetchone()
        is not None
    )


def _assert_current(
    conn: sqlite3.Connection, settings: Settings, relpath: str
) -> None:
    from librairy.corrections import CorrectionRefused

    row = conn.execute(
        "SELECT fingerprint FROM items WHERE root='library' AND relpath=?", (relpath,)
    ).fetchone()
    if row is None or not row["fingerprint"]:
        raise CorrectionRefused(f"{PurePosixPath(relpath).name} has not been indexed")
    try:
        path = validate_relpath(settings.library_dir, relpath, kind="library")
    except PathValidationError as exc:
        raise CorrectionRefused(
            f"{PurePosixPath(relpath).name} is not a path inside the library"
        ) from exc
    if not path.is_file() or blake2b_file(path) != row["fingerprint"]:
        raise CorrectionRefused(
            f"{PurePosixPath(relpath).name} changed since these were compared"
        )
