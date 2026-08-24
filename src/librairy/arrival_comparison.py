"""An arrival that is not the same bytes as what you have, but is the same thing.

    Inbox/Death on Two Legs.flac                                   38 MB, lossless
    Library/Music/Rock/Queen/A Night at the Opera/01 - …​.mp3        7 MB, 320 kbps

`inbox_duplicates.py` handles the arrival whose bytes already exist: it is
redundant, and the only question is whether to set it aside. This is the other
cross-root case, and it is not that question at all. Nothing is redundant. The
person has two representations of one recording, one filed and one arriving,
and **which of them they want is a preference about their library.**

Three answers, and the asymmetry with the library-to-library comparison is the
whole reason this is a separate module rather than a flag on that one. There,
either representation may be kept and both are already filed. Here one is filed
and one is arriving, which changes what each answer *means*:

    keep the library copy    the arrival goes to Quarantine, filed copy
                             untouched — the shape `inbox_duplicates` already
                             has, with different provenance
    use the arriving copy    the filed copy goes to Quarantine **first**, and
                             the arrival takes its place. Never an overwrite:
                             LibrAIry has no operation that loses bytes
    keep both                the filed copy stays and the arrival is still an
                             arrival. It goes back through ordinary Review to
                             be filed, because "keep both" that left the second
                             one sitting in the inbox forever would be a
                             promise the software did not keep

The second one is the interesting one and it is the reason `plans.coherent`
exists. It is two operations that are one decision: if the move cannot happen,
the quarantine must not happen either, or somebody ends up with their only copy
of a recording in Quarantine and nothing in the library. That is not data loss —
nothing is deleted and it can be restored — but it is emphatically not what they
approved.

The destination is taken from the filed copy rather than from classification.
The comparison has already established that these are the same recording, and
the filed copy is where this library has decided that recording lives; sending
the arrival back through a fresh guess would be throwing away the better
evidence. Its own extension is kept, because a FLAC is not an MP3.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import PurePosixPath

from librairy.config import Settings
from librairy.fingerprint import blake2b_file
from librairy.paths import PathValidationError, validate_dest, validate_relpath
from librairy.planner import OperationSpec, approve_plan, create_plan, utc_now
from librairy.quarantine import quarantine_operation

#  Written into the arrival's proposal evidence when it is set aside after a
#  comparison, and read back by `quarantine.quarantine_reason`. Deliberately a
#  different sentence from `inbox_duplicates.EVIDENCE_PREFIX`: these files do
#  not have the same bytes, and saying they do is the one claim this workflow
#  exists to avoid making.
EVIDENCE_PREFIX = "similar to"

KEEP_LIBRARY = "keep-library"
USE_ARRIVAL = "use-arrival"
KEEP_BOTH = "keep-both"
CHOICES = (KEEP_LIBRARY, USE_ARRIVAL, KEEP_BOTH)

CHOICE_NOTE = {
    KEEP_LIBRARY: "The arriving file goes to Quarantine. Your filed copy is "
    "untouched, and nothing is deleted.",
    USE_ARRIVAL: "Your filed copy goes to Quarantine and this one takes its "
    "place. Nothing is overwritten and nothing is deleted.",
    KEEP_BOTH: "Both are kept. This one carries on through Review to be filed.",
}


@dataclass(frozen=True)
class Twin:
    """The filed representation an arrival resembles."""

    item_id: int
    relpath: str
    size: int
    fingerprint: str

    @property
    def name(self) -> str:
        return PurePosixPath(self.relpath).name


@dataclass(frozen=True)
class Arrival:
    """One inbox file, the filed copy it resembles, and where it would land."""

    item_id: int
    relpath: str
    size: int
    twin: Twin
    dest_relpath: str

    @property
    def name(self) -> str:
        return PurePosixPath(self.relpath).name

    @property
    def replaces_in_place(self) -> bool:
        """Would using this arrival land it exactly where the filed copy is?"""
        return self.dest_relpath == self.twin.relpath


def similar_arrival(
    conn: sqlite3.Connection, settings: Settings, item_id: int
) -> Arrival | None:
    """The filed copy this arrival resembles, or None.

    Read from czkawka's own pairs and from nowhere else. Byte-identical pairs
    are excluded here exactly as they are in `similar_media.py`: those are the
    exact-duplicate workflow's question, which knows what rmlint said and can
    say the bytes match.
    """
    from librairy.similar_media import SIMILAR_KINDS, active_clause

    row = conn.execute(
        "SELECT id, relpath, size, fingerprint, root, missing_since FROM items"
        " WHERE id=?",
        (item_id,),
    ).fetchone()
    if row is None or row["root"] != "inbox" or row["missing_since"] is not None:
        return None
    found = conn.execute(
        f"""
        SELECT b.id AS id, b.relpath AS relpath, b.size AS size,
               b.fingerprint AS fingerprint
        FROM similar_media_flags f
        JOIN items i ON i.id = ?
        JOIN items b ON b.id = CASE WHEN f.item_id=i.id THEN f.similar_item_id ELSE f.item_id END
        WHERE {active_clause("i", "b")} AND f.kind IN ({",".join("?" * len(SIMILAR_KINDS))})
          AND (f.item_id=? OR f.similar_item_id=?)
          AND b.root='library' AND b.missing_since IS NULL
        ORDER BY b.relpath LIMIT 1
        """,  # noqa: S608 - the placeholders are a module constant
        (item_id, *SIMILAR_KINDS, item_id, item_id),
    ).fetchone()
    if found is None:
        return None
    if row["fingerprint"] and row["fingerprint"] == found["fingerprint"]:
        return None
    twin = Twin(
        item_id=int(found["id"]),
        relpath=str(found["relpath"]),
        size=int(found["size"] or 0),
        fingerprint=str(found["fingerprint"] or ""),
    )
    return Arrival(
        item_id=int(row["id"]),
        relpath=str(row["relpath"]),
        size=int(row["size"] or 0),
        twin=twin,
        dest_relpath=_destination(twin.relpath, str(row["relpath"])),
    )


def _destination(twin_relpath: str, arrival_relpath: str) -> str:
    """Where the filed copy lives, under the arrival's own extension.

    The comparison has established that these are the same recording, and the
    filed copy is where this library has decided that recording lives. Sending
    the arrival back through a fresh guess would discard the better evidence.
    The extension is the arrival's own, because a FLAC is not an MP3.
    """
    twin = PurePosixPath(twin_relpath)
    suffix = PurePosixPath(arrival_relpath).suffix
    return str(twin.with_name(twin.stem + suffix))


def describe(
    conn: sqlite3.Connection, settings: Settings, item_id: int
) -> dict[str, object] | None:
    """What the Review row says about this arrival, without measuring anything.

    The technical table is fetched when somebody opens it, from the same
    endpoint the library-to-library comparison uses. This is the sentence above
    it and the three buttons under it.
    """
    from librairy.format_preference import label_for, prefer_among, sentence
    from librairy.humanize import human_bytes

    arrival = similar_arrival(conn, settings, item_id)
    if arrival is None:
        return None
    #  The workflow has already decided these two are representations of one
    #  thing — that is what the row is — so the only question left is which
    #  representation, which is exactly what the preference is about. It
    #  preselects a button and moves nothing.
    wanted = prefer_among(conn, [arrival.relpath, arrival.twin.relpath])
    return {
        "item_id": arrival.item_id,
        "twin_item_id": arrival.twin.item_id,
        "match": arrival.twin.relpath,
        "arrival_size": human_bytes(arrival.size),
        "twin_size": human_bytes(arrival.twin.size),
        "arrival_format": label_for(conn, arrival.relpath),
        "twin_format": label_for(conn, arrival.twin.relpath),
        "destination": arrival.dest_relpath,
        "in_place": arrival.replaces_in_place,
        "notes": CHOICE_NOTE,
        #  Which of the three buttons the owner's stated preference points at,
        #  or "". `keep-both` is never preferred by a format preference: it is
        #  an answer about how many copies to have, not about which format.
        "preferred": (
            USE_ARRIVAL
            if wanted and wanted == arrival.relpath
            else KEEP_LIBRARY
            if wanted and wanted == arrival.twin.relpath
            else ""
        ),
        "preference": sentence(conn) if wanted else "",
    }


# --- the decision -------------------------------------------------------------------


def resolve(
    conn: sqlite3.Connection, settings: Settings, item_id: int, choice: str
) -> str:
    """Answer one cross-root comparison. Returns the plan id, or "" for none.

    `keep both` makes no plan on purpose: nothing moves, the filed copy stays,
    and the arrival is still an arrival with a Review row of its own. What it
    does do is stop the comparison being asked again, so the row goes back to
    being an ordinary "where should this be filed" question rather than
    re-offering a decision that was already made.
    """
    from librairy.corrections import CorrectionRefused

    if choice not in CHOICES:
        raise CorrectionRefused("that is not one of the choices")
    arrival = similar_arrival(conn, settings, item_id)
    if arrival is None:
        raise CorrectionRefused("there is nothing to compare this with any more")
    if _already_claimed(conn, arrival):
        raise CorrectionRefused("one of these files is already waiting for Commit")
    _assert_current(conn, settings, "inbox", arrival.relpath)
    _assert_current(conn, settings, "library", arrival.twin.relpath)
    if choice == KEEP_BOTH:
        return _keep_both(conn, arrival)
    if choice == KEEP_LIBRARY:
        return _keep_library(conn, settings, arrival)
    return _use_arrival(conn, settings, arrival)


def _keep_both(conn: sqlite3.Connection, arrival: Arrival) -> str:
    """Nothing moves, and the arrival is not stranded.

    The comparison is dismissed rather than the file — the person said they
    want both, so the arrival keeps its ordinary Review row and gets filed like
    anything else. Dismissing the *flag* is what stops the next audit asking
    again; a finding-level dismissal would not hold, because re-running the
    audit rewrites a finding's status to `open`.
    """
    from librairy.similar_media import dismiss_between

    dismiss_between(conn, [arrival.item_id, arrival.twin.item_id])
    return ""


def _keep_library(
    conn: sqlite3.Connection, settings: Settings, arrival: Arrival
) -> str:
    """The arrival goes to Quarantine; the filed copy is not touched at all."""
    spec = quarantine_operation(arrival.relpath)
    plan_id = _approved_plan(conn, settings, [spec])
    _mark_arrival(conn, arrival)
    return plan_id


def _use_arrival(
    conn: sqlite3.Connection, settings: Settings, arrival: Arrival
) -> str:
    """The filed copy is preserved first, then the arrival takes its place.

    Two operations in one plan, in this order and only this order, and the plan
    is marked coherent so that neither happens without the other. An overwrite
    would be one operation and would lose the filed copy's bytes; LibrAIry does
    not have an operation that loses bytes.
    """
    from librairy.corrections import CorrectionRefused

    try:
        destination = validate_dest(settings.library_dir, arrival.dest_relpath)
    except (PathValidationError, ValueError) as exc:
        raise CorrectionRefused(
            f"{arrival.name} has no safe destination: {exc}"
        ) from exc
    if destination.exists() and not arrival.replaces_in_place:
        #  Something other than the filed copy is already standing where this
        #  would land. Renumbering it would invent a name nobody approved.
        raise CorrectionRefused(
            f"{PurePosixPath(arrival.dest_relpath).name} already exists and is not "
            f"the copy you are replacing"
        )
    from librairy.replacement import swap_specs

    specs = swap_specs(
        preserve=arrival.twin.relpath,
        source_root="inbox",
        source_relpath=arrival.relpath,
        dest_relpath=arrival.dest_relpath,
    )
    plan_id = _approved_plan(conn, settings, specs, coherent=True)
    _close_proposal(conn, arrival.item_id)
    return plan_id


def _approved_plan(
    conn: sqlite3.Connection,
    settings: Settings,
    specs: list[OperationSpec],
    *,
    coherent: bool = False,
) -> str:
    from librairy.corrections import CorrectionRefused

    plan_id = create_plan(conn, specs, settings)
    if coherent:
        conn.execute("UPDATE plans SET coherent=1 WHERE id=?", (plan_id,))
    try:
        approve_plan(conn, plan_id, settings)
    except sqlite3.IntegrityError as exc:
        conn.execute("DELETE FROM plan_ops WHERE plan_id=?", (plan_id,))
        conn.execute("DELETE FROM plans WHERE id=?", (plan_id,))
        raise CorrectionRefused(
            "this comparison was answered by something else a moment ago"
        ) from exc
    return plan_id


def _mark_arrival(conn: sqlite3.Connection, arrival: Arrival) -> None:
    """Point the arrival's proposal at Quarantine, with truthful evidence.

    The same shape the exact-duplicate staging uses, and a different sentence:
    `similar to` rather than `exact duplicate of`, so every page downstream
    that reads this evidence back describes what actually happened.
    """
    conn.execute(
        "UPDATE proposals SET action='quarantine', dest_root='quarantine',"
        " dest_relpath=?, status='approved', evidence=?, updated_at=?"
        " WHERE item_id=? AND status != 'superseded'",
        (
            quarantine_operation(arrival.relpath).dest_relpath,
            f'[{{"source": "czkawka", "field": "similar", "detail": '
            f'"{EVIDENCE_PREFIX} library:{arrival.twin.relpath}", "weight": 0.8}}]',
            utc_now(),
            arrival.item_id,
        ),
    )


def _close_proposal(conn: sqlite3.Connection, item_id: int) -> None:
    """The arrival is being filed by this decision, not by the inbox queue.

    Leaving its proposal `proposed` would put the same file in two places at
    once: waiting for Commit as part of this comparison, and sitting in Review
    as an undecided arrival with a destination of its own.
    """
    conn.execute(
        "UPDATE proposals SET status='superseded', updated_at=? WHERE item_id=?"
        " AND status NOT IN ('committed', 'superseded')",
        (utc_now(), item_id),
    )


def _already_claimed(conn: sqlite3.Connection, arrival: Arrival) -> bool:
    from librairy.correction_state import ACTIVE_PLAN_STATUSES

    statuses = ",".join("?" * len(ACTIVE_PLAN_STATUSES))
    return (
        conn.execute(
            f"SELECT 1 FROM plan_ops o JOIN plans p ON p.id = o.plan_id"  # noqa: S608
            f" WHERE o.src_relpath IN (?, ?) AND p.status IN ({statuses}) LIMIT 1",
            (arrival.relpath, arrival.twin.relpath, *ACTIVE_PLAN_STATUSES),
        ).fetchone()
        is not None
    )


def _assert_current(
    conn: sqlite3.Connection, settings: Settings, root: str, relpath: str
) -> None:
    from librairy.corrections import CorrectionRefused

    row = conn.execute(
        "SELECT fingerprint FROM items WHERE root=? AND relpath=?", (root, relpath)
    ).fetchone()
    if row is None or not row["fingerprint"]:
        raise CorrectionRefused(f"{PurePosixPath(relpath).name} has not been indexed")
    base = settings.inbox_dir if root == "inbox" else settings.library_dir
    try:
        path = validate_relpath(base, relpath, kind=root)
    except PathValidationError as exc:
        raise CorrectionRefused(f"{PurePosixPath(relpath).name} is not a {root} path") from exc
    if not path.is_file() or blake2b_file(path) != row["fingerprint"]:
        raise CorrectionRefused(
            f"{PurePosixPath(relpath).name} changed since these were compared"
        )


# --- what the rest of the program asks ----------------------------------------------


def is_similar_proposal(conn: sqlite3.Connection, item_id: int) -> bool:
    """Was this arrival set aside after a comparison, rather than as a copy?"""
    row = conn.execute(
        "SELECT evidence FROM proposals WHERE item_id=? AND status != 'superseded'"
        " ORDER BY id DESC LIMIT 1",
        (item_id,),
    ).fetchone()
    return bool(row) and EVIDENCE_PREFIX in (row["evidence"] or "")


def compared_with(conn: sqlite3.Connection, item_id: int) -> int | None:
    """The library item this arrival was compared with, by its recorded path."""
    row = conn.execute(
        "SELECT evidence FROM proposals WHERE item_id=? AND status != 'superseded'"
        " ORDER BY id DESC LIMIT 1",
        (item_id,),
    ).fetchone()
    evidence = (row["evidence"] if row else "") or ""
    marker = f"{EVIDENCE_PREFIX} library:"
    if marker not in evidence:
        return None
    relpath = evidence.split(marker, 1)[1].split('"', 1)[0]
    found = conn.execute(
        "SELECT id FROM items WHERE root='library' AND relpath=?", (relpath,)
    ).fetchone()
    return int(found["id"]) if found else None


def withdraw(conn: sqlite3.Connection, settings: Settings, plan_id: str) -> None:
    """Take back a comparison decision that has not run, and restore the row.

    Deliberately not called Undo. Undo reverses files that moved; this reverses
    a decision about files that did not. The arrival goes back to being an
    ordinary Review row with its own destination, and the comparison is a live
    question again — withdrawing the answer must not also suppress the
    question, or the row would come back with nothing to decide.
    """
    from librairy.corrections import CorrectionRefused

    plan = conn.execute(
        "SELECT id, status, coherent FROM plans WHERE id=?", (plan_id,)
    ).fetchone()
    if plan is None or not plan["coherent"]:
        raise CorrectionRefused("that is not a comparison decision")
    if plan["status"] != "approved":
        raise CorrectionRefused("this decision is already running or finished")
    items = [
        int(row["item_id"])
        for row in conn.execute(
            "SELECT item_id FROM plan_ops WHERE plan_id=? AND item_id IS NOT NULL",
            (plan_id,),
        )
    ]
    conn.execute("DELETE FROM plan_ops WHERE plan_id=?", (plan_id,))
    conn.execute("DELETE FROM plans WHERE id=?", (plan_id,))
    for item in items:
        conn.execute(
            "UPDATE proposals SET status='proposed', action='move', dest_root='library',"
            " updated_at=? WHERE item_id=? AND status IN ('approved', 'superseded')",
            (utc_now(), item),
        )
    if len(items) >= 2:
        conn.execute(
            "UPDATE similar_media_flags SET status='review', dismissed_fingerprints=NULL"
            " WHERE item_id IN (?, ?) AND similar_item_id IN (?, ?)",
            (items[0], items[1], items[0], items[1]),
        )
