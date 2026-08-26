"""Whether reversing an old decision would undo a newer one nobody asked about.

Undo has always been plan-scoped, and for a long time that was the whole truth:
a plan filed some files, and reversing it put them back. LibrAIry now produces
*sequences*. A file is filed, then a correction renames its album, then a
better encode replaces it. Eighteen photographs are set aside, then restored,
then one of them is corrected. Each of those is an explicit decision somebody
took, and blind reversal of the first quietly invalidates the ones after it.

That is a safety problem rather than a papercut, and it is the last place in
the program where an explicit choice can be overwritten without anybody being
asked.

**Derived, not stored.** What a plan did is already recorded — immutably — in
`plan_ops` and `history`, and every operation carries the `item_id` of the file
it moved. That identity is the thing that survives a move, which is exactly
what a dependency question needs and exactly what a path comparison cannot
give: `Photos/2024/foo.jpg` is a string two plans might share by coincidence,
and item 4,127 is the same photograph. A dependency table alongside those
records would be a second account of the same events, free to disagree with the
first.

**What counts as a dependency.** A later *committed filesystem decision* that
moved a file this plan moved, and that has not itself been reversed. Nothing
else does:

    an audit that read the file          no
    a metadata measurement               no
    a relationship discovered            no
    a search index update                no
    a decision-memory event              no

None of those are operations, so none of them can appear here — which is a
property of the derivation rather than a list somebody has to maintain.

**It does not cascade.** Reversing one decision to make room for reversing
another crosses two explicit choices, and this refuses to do that on somebody's
behalf. It names the later decision and stops. Undo that one and the earlier
becomes available on its own; the chain unwinds in reverse order because that
is the only order in which each step is a decision somebody actually took.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

#  Nothing later depends on this plan. It may be reversed, subject to the
#  ordinary preflight — see `history.undo_preflight`.
CLEAR = "clear"
#  One or more later committed decisions moved a file this plan moved.
BLOCKED = "blocked"
#  Every forward operation has already been reversed.
UNDONE = "undone"
#  The journal cannot be linked to the operations that produced it, so identity
#  cannot be established. Refused rather than guessed at: "probably
#  independent" is not a thing to say before moving somebody's files.
UNKNOWN = "unknown"
#  The files are no longer what the journal says. Only filled in when a caller
#  supplies `Settings`, because answering it means hashing.
DRIFTED = "drifted"

STATES = (CLEAR, BLOCKED, UNDONE, UNKNOWN, DRIFTED)

#  The journal actions that reverse a forward operation. An `ok` row for an
#  operation means that operation no longer stands, which is what lets a chain
#  become undoable in reverse order without anything being recalculated.
UNDO_ACTIONS = ("undo_move", "undo_quarantine")

#  How many later decisions one answer names before it starts counting. A plan
#  whose files were all touched again is blocked by the first one either way,
#  and a list of forty is not an instruction.
SHOWN = 3


@dataclass(frozen=True)
class Blocker:
    """One later decision that consumed state this plan created."""

    plan_id: str
    shared: int
    when: str = ""
    summary: str = ""


@dataclass(frozen=True)
class Sequence:
    """Where one plan sits in the order of decisions taken since."""

    plan_id: str
    state: str
    #  How many of this plan's operations are still standing, and how many of
    #  those a later decision has moved on from. Both, because "1 of 18" is the
    #  sentence that stops somebody assuming the other seventeen make it safe.
    operations: int = 0
    affected: int = 0
    blockers: list[Blocker] = field(default_factory=list)
    blockers_more: int = 0

    @property
    def undoable(self) -> bool:
        return self.state == CLEAR

    @property
    def explanation(self) -> str:
        """Why this cannot be reversed yet, in the words a person needs.

        Never a disabled button with nothing beside it. The whole value of
        knowing about the dependency is being told which decision to reverse
        first.
        """
        if self.state == CLEAR:
            return ""
        if self.state == UNDONE:
            return "This decision has already been reversed."
        if self.state == UNKNOWN:
            return (
                "LibrAIry cannot tell which later decisions depend on this one, "
                "so it will not reverse it."
            )
        if self.state == DRIFTED:
            return "These files are no longer what this decision left behind."
        first = self.blockers[0] if self.blockers else None
        scope = (
            f"{self.affected} of {self.operations} files"
            if self.operations > 1
            else "this file"
        )
        later = f" — {first.summary}" if first and first.summary else ""
        return (
            f"A later decision changed {scope} from this one{later}. "
            f"Reverse that one first."
        )


def sequence(
    conn: sqlite3.Connection, plan_id: str, *, settings=None  # noqa: ANN001
) -> Sequence:
    """Where one plan stands. One plan, one call — see `sequences` for a page."""
    found = sequences(conn, [plan_id]).get(plan_id)
    if found is None:
        return Sequence(plan_id=plan_id, state=UNKNOWN)
    if settings is None or found.state != CLEAR:
        return found
    from librairy.history import undo_preflight

    #  The ordinary preflight, asked alongside rather than instead. Sequence
    #  answers "would this reverse somebody else's decision"; preflight answers
    #  "are these files still what we left". Both have to be yes, and only this
    #  one hashes — which is why it happens behind a button and not on a page.
    if undo_preflight(conn, settings, plan_id):
        return Sequence(**{**found.__dict__, "state": DRIFTED})
    return found


def sequences(
    conn: sqlite3.Connection, plan_ids: list[str]
) -> dict[str, Sequence]:
    """Where each of these plans stands — three queries for the whole page.

    History draws fifty plan cards. A dependency query per card, each of them
    scanning every operation ever executed, is the shape that works on a
    fixture and stops working on a real journal.
    """
    if not plan_ids:
        return {}
    wanted = list(dict.fromkeys(plan_ids))
    standing = _standing(conn, wanted)
    unlinked = _unlinked(conn, wanted)
    later = _later_decisions(conn, wanted)
    labels = _labels(conn, {row.plan_id for rows in later.values() for row in rows})

    found: dict[str, Sequence] = {}
    for plan_id in wanted:
        total, live = standing.get(plan_id, (0, 0))
        if plan_id in unlinked:
            found[plan_id] = Sequence(plan_id, UNKNOWN, operations=total)
            continue
        if total == 0:
            #  No forward operations at all: a plan that never ran, or one
            #  whose every operation has been reversed already.
            found[plan_id] = Sequence(plan_id, UNDONE if live == 0 else CLEAR)
            continue
        if live == 0:
            found[plan_id] = Sequence(plan_id, UNDONE, operations=total)
            continue
        blockers = later.get(plan_id, [])
        if not blockers:
            found[plan_id] = Sequence(plan_id, CLEAR, operations=live)
            continue
        named = [
            Blocker(
                plan_id=row.plan_id,
                shared=row.shared,
                when=labels.get(row.plan_id, ("", ""))[0],
                summary=labels.get(row.plan_id, ("", ""))[1],
            )
            for row in blockers[:SHOWN]
        ]
        found[plan_id] = Sequence(
            plan_id,
            BLOCKED,
            operations=live,
            affected=max(row.shared for row in blockers),
            blockers=named,
            blockers_more=max(0, len(blockers) - SHOWN),
        )
    return found


@dataclass(frozen=True)
class _Row:
    plan_id: str
    shared: int


#  The forward journal actions. A `history` row carrying one of these with an
#  `ok` outcome is the record that an operation actually happened.
FORWARD_ACTIONS = ("move", "quarantine")

#  Every operation that ran and has not been reversed.
#
#  Driven from `history` rather than from `plan_ops`, because the journal is
#  what Undo reverses — `undo_plan` reads exactly these rows. `plan_ops` is
#  joined in for one thing only: the `item_id`, which is the identity a file
#  keeps across every move and the thing a path cannot give.
#
#  The ordinal is the journal row's own id, not its timestamp. `utc_now()` has
#  one-second resolution, and two decisions committed in the same second are
#  ordinary rather than exotic — a filing and the correction made immediately
#  after it. Comparing timestamps called that pair simultaneous and found no
#  dependency at all, which is the failure this module exists to prevent.
#  `history.id` increases with every operation carried out, across every plan,
#  so it *is* the execution order.
#
#  The reversal check pairs an undo row with its forward row by `op_id` where
#  there is one, and by the path it came back from where there is not.
_EXECUTED = """
  SELECT h.id AS ordinal, h.plan_id AS plan_id, h.op_id AS op_id,
         o.item_id AS item_id,
         h.src_root AS src_root, h.src_relpath AS src_relpath,
         h.dest_root AS dest_root, h.dest_relpath AS dest_relpath
  FROM history h
  LEFT JOIN plan_ops o ON o.id = h.op_id
  WHERE h.outcome='ok' AND h.action IN ({forward}) {only}
    AND NOT EXISTS (
      SELECT 1 FROM history u
      WHERE u.outcome='ok' AND u.action IN ({undone}) AND u.id > h.id
        AND ((u.op_id IS NOT NULL AND u.op_id = h.op_id)
             OR (u.op_id IS NULL AND h.op_id IS NULL
                 AND u.plan_id IS h.plan_id
                 AND u.src_root = h.dest_root
                 AND u.src_relpath = h.dest_relpath))
    )
"""

_FORWARD_IN = ",".join("?" * len(FORWARD_ACTIONS))
_UNDONE_IN = ",".join("?" * len(UNDO_ACTIONS))
_EXECUTED_SQL = _EXECUTED.format(forward=_FORWARD_IN, undone=_UNDONE_IN, only="")
_EXECUTED_PARAMS = (*FORWARD_ACTIONS, *UNDO_ACTIONS)


def _executed_after(bound: str) -> str:
    """The view, restricted to operations after a point in the journal.

    Nothing before the oldest operation on the page can possibly be *later*
    than it, so the whole journal up to that point is excluded before the
    dependency join starts. History shows the newest decisions first, which
    means that bound usually excludes almost everything.
    """
    return _EXECUTED.format(forward=_FORWARD_IN, undone=_UNDONE_IN, only=bound)


def _executed_for(plan_ids: list[str]) -> str:
    """The same view, narrowed to these plans before anything else happens.

    The filter belongs *inside* the subquery. Left outside it, the dependency
    self-join materialises every operation ever executed twice and then throws
    almost all of it away — 197 ms for a History page on a ten-thousand-plan
    journal, growing with the journal rather than with the page.
    """
    return _EXECUTED.format(
        forward=_FORWARD_IN,
        undone=_UNDONE_IN,
        only=f"AND h.plan_id IN ({','.join('?' * len(plan_ids))})",
    )


def _standing(
    conn: sqlite3.Connection, plan_ids: list[str]
) -> dict[str, tuple[int, int]]:
    """How many operations each plan carried out, and how many still stand."""
    placeholders = ",".join("?" * len(plan_ids))
    rows = conn.execute(
        f"""
        SELECT h.plan_id AS plan_id, COUNT(*) AS total,
               SUM(CASE WHEN EXISTS (
                     SELECT 1 FROM ({_EXECUTED_SQL}) live WHERE live.ordinal = h.id
                   ) THEN 1 ELSE 0 END) AS live
        FROM history h
        WHERE h.plan_id IN ({placeholders}) AND h.outcome='ok'
          AND h.action IN ({_FORWARD_IN})
        GROUP BY h.plan_id
        """,  # noqa: S608 - placeholders are counted; the rest is a constant
        (*_EXECUTED_PARAMS, *plan_ids, *FORWARD_ACTIONS),
    ).fetchall()
    return {
        str(row["plan_id"]): (int(row["total"]), int(row["live"] or 0)) for row in rows
    }


def _unlinked(conn: sqlite3.Connection, plan_ids: list[str]) -> set[str]:
    """Plans whose journal and operations disagree about what happened.

    The one genuinely ambiguous case: a plan that has operations recorded, and
    journal rows that cannot be tied to any of them. There is then no way to
    know which files those rows were about, and "probably independent" is not a
    sentence to say before moving somebody's files.

    A plan with journal rows and no operations at all is *not* ambiguous — it
    is simply older than `plan_ops`, and its dependencies are still derivable
    from the paths the journal records. Refusing those would block every
    historical Undo to no purpose.
    """
    placeholders = ",".join("?" * len(plan_ids))
    rows = conn.execute(
        f"""
        SELECT live.plan_id AS plan_id,
               SUM(CASE WHEN live.op_id IS NULL THEN 1 ELSE 0 END) AS loose,
               (SELECT COUNT(*) FROM plan_ops o
                 WHERE o.plan_id = live.plan_id AND o.result='done') AS ops
        FROM ({_EXECUTED_SQL}) live
        WHERE live.plan_id IN ({placeholders})
        GROUP BY live.plan_id
        """,  # noqa: S608 - placeholders are counted; the rest is a constant
        (*_EXECUTED_PARAMS, *plan_ids),
    ).fetchall()
    return {
        str(row["plan_id"])
        for row in rows
        if int(row["loose"] or 0) and int(row["ops"] or 0)
    }


def _later_decisions(
    conn: sqlite3.Connection, plan_ids: list[str]
) -> dict[str, list[_Row]]:
    """Committed decisions that consumed state one of these plans created.

    Two ways a later operation can be shown to depend on an earlier one, and
    the order matters.

    *Identity* — the later operation moved the same `items` row. This is the
    strong form and the one that survives renaming: `Photos/2024/foo.jpg` is a
    string two plans could share by coincidence, and item 4,127 is the same
    photograph however often it has moved.

    *Continuation* — the later operation took the file **from where this one
    put it**. Used where identity is unavailable, which is what an old journal
    written before `plan_ops` carried item ids looks like. It is deliberately
    not a general path comparison: two plans naming the same string prove
    nothing, while one plan reading exactly where another wrote is a handover.

    Then *standing*: a later decision that has itself been reversed is no
    longer in the way, which is what lets a chain unwind in reverse order
    without anything being recomputed or stored.
    """
    #  The oldest operation on this page. Everything before it is by
    #  definition not later than anything here, and on a History page — newest
    #  first — that excludes nearly the whole journal before the self-join
    #  begins. Measured at ten thousand plans: 186 ms to 2 ms.
    floor = conn.execute(
        f"SELECT MIN(mine.ordinal) AS floor FROM ({_executed_for(plan_ids)}) mine",  # noqa: S608
        (*FORWARD_ACTIONS, *plan_ids, *UNDO_ACTIONS),
    ).fetchone()
    if floor is None or floor["floor"] is None:
        return {}
    rows = conn.execute(
        f"""
        SELECT mine.plan_id AS plan_id, later.plan_id AS blocker,
               COUNT(DISTINCT COALESCE(mine.item_id, mine.dest_relpath)) AS shared,
               MIN(later.ordinal) AS first_at
        FROM ({_executed_for(plan_ids)}) mine
        JOIN ({_executed_after("AND h.id > ?")}) later
          ON later.plan_id <> mine.plan_id
         AND later.ordinal > mine.ordinal
         AND ((later.item_id IS NOT NULL AND mine.item_id IS NOT NULL
               AND later.item_id = mine.item_id)
              OR (later.src_root = mine.dest_root
                  AND later.src_relpath = mine.dest_relpath))
        GROUP BY mine.plan_id, later.plan_id
        ORDER BY mine.plan_id, first_at, later.plan_id
        """,  # noqa: S608 - placeholders are counted; the rest is a constant
        (
            *FORWARD_ACTIONS, *plan_ids, *UNDO_ACTIONS,
            *FORWARD_ACTIONS, int(floor["floor"]), *UNDO_ACTIONS,
        ),
    ).fetchall()
    found: dict[str, list[_Row]] = {}
    for row in rows:
        found.setdefault(str(row["plan_id"]), []).append(
            _Row(plan_id=str(row["blocker"]), shared=int(row["shared"]))
        )
    return found


def _labels(
    conn: sqlite3.Connection, plan_ids: set[str]
) -> dict[str, tuple[str, str]]:
    """A date and a short description for each blocking plan, in one query.

    `planner.summarise` is the implementation. It used to live here, and then
    `plan_conflicts` needed the same sentence about the same plans — at which
    point two modules describing one decision differently was a matter of time.
    """
    from librairy.planner import summarise

    return summarise(conn, sorted(plan_ids))


def assert_clear(conn: sqlite3.Connection, plan_id: str) -> None:
    """Refuse to reverse a decision another decision has since built on.

    The gate itself, at the one function every reversal goes through. A page
    that explains the dependency and a route that reverses anyway would be a
    warning rather than a safeguard — and the whole reason this exists is that
    a blind reversal silently discards a choice somebody made afterwards.

    `undone` passes: reversing an already-reversed plan is the existing no-op,
    and turning it into an error here would change behaviour that has nothing
    to do with sequencing.
    """
    from librairy.history import UndoError

    found = sequence(conn, plan_id)
    if found.state in (CLEAR, UNDONE):
        return
    raise UndoError(found.explanation)


#  How far back the summary looks.
#
#  A count over the whole journal is a self-join over every operation ever
#  carried out — 186 ms at ten thousand plans, growing with the journal rather
#  than with anything a person is looking at. History itself is paginated
#  newest-first for the same reason, and a decision from two years ago that
#  nobody can see the Undo button for is not something a health page needs to
#  count. So the summary is over the recent journal and says so in as many
#  words; the per-plan answer, which is the one that actually gates a reversal,
#  is never bounded.
WINDOW = 200


@dataclass(frozen=True)
class Blocked:
    """How many recent decisions a later decision has built on."""

    count: int = 0
    window: int = WINDOW
    examples: tuple[tuple[str, str], ...] = ()


def blocked(conn: sqlite3.Connection, *, window: int = WINDOW) -> Blocked:
    """A bounded count of decisions that cannot currently be reversed.

    For a summary only. Nothing decides anything from this — `assert_clear` is
    the gate, and it asks about one plan with no window at all.
    """
    recent = [
        str(row["plan_id"])
        for row in conn.execute(
            #  Newest by *journal position*, never by plan id: a plan id is
            #  a UUID, and sorting those descending returns two hundred
            #  arbitrary decisions rather than the two hundred most recent.
            "SELECT h.plan_id AS plan_id, MAX(h.id) AS latest FROM history h"
            " WHERE h.plan_id IS NOT NULL AND h.outcome='ok'"
            f" AND h.action IN ({_FORWARD_IN})"
            " GROUP BY h.plan_id ORDER BY latest DESC LIMIT ?",  # noqa: S608 - counted
            (*FORWARD_ACTIONS, max(1, window)),
        )
    ]
    if not recent:
        return Blocked(window=window)
    found = sequences(conn, recent)
    stuck = [item for item in found.values() if item.state == BLOCKED]
    labels = _labels(conn, {item.plan_id for item in stuck[:SHOWN]})
    return Blocked(
        count=len(stuck),
        window=window,
        examples=tuple(
            (
                labels.get(item.plan_id, ("", ""))[1].capitalize()
                or "A decision",
                item.explanation,
            )
            for item in stuck[:SHOWN]
        ),
    )
