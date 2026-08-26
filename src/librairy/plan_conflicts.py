"""Two waiting decisions that cannot both still be right.

Last pass gave Undo a sense of order: a decision that a later decision has
built on cannot be reversed blind, because reversing it would silently discard
the later choice. This is the same problem in the other direction.

Two decisions can each be approved on their own and reach *Waiting for Commit*
while describing incompatible futures — a correction that renames a track and a
replacement that expects that track at its old name, two arrivals filed to the
same destination, a photograph one decision sets aside while another decision's
approval was explained by it staying. Both cards look fine. Commit runs the
first, the executor's preflight refuses the second, and the person finds out
about the collision from a failure.

That is safe. It is also unnecessarily late, and it is late in the one place
where lateness costs something: the decision that fails is the one you have
already agreed to, and the reason it failed is a choice you also made.

So the collision is found while both are still decisions.

    committed dependency   a later decision has been carried out on top of an
                           earlier one. Affects **Undo**. See `undo_sequence`.

    pending conflict       two decisions that have not run cannot both remain
                           valid against the current state. Affects
                           **approval and Commit**. This module.

They are related and they are not the same thing, and nothing here creates the
other: a plan that has not executed has moved no files, so it cannot be
depended on by anything.

**Derived, never stored.** A `plan_conflicts` table would be a second account
of facts that already exist in `plan_ops`, `proposals` and `plan_relationships`,
free to disagree with them and needing to be rewritten every time anything is
approved or withdrawn. Resolving a conflict here means withdrawing one of the
decisions, at which point it simply stops being computed.

**It never resolves anything.** Which of two decisions to keep is the person's
question — the program has no basis for preferring the older, the larger or the
more confident, and picking one on any of those grounds would be cancelling a
decision somebody made. So a conflict is reported, both cards stop offering
Commit, and the existing *Send back* on either one clears it.

**The executor stays authoritative.** This runs against the database; between
here and the move there is still a filesystem other programs can write to. The
preflight that hashes every source has not moved and has not weakened.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import PurePosixPath

#  The two kinds of thing a waiting decision can be. A plan is an approved,
#  hashed set of operations; an approved inbox proposal is a decision that has
#  not been turned into operations yet — the filing plan is built when Commit
#  is pressed. Both are things somebody agreed to and both can collide, so both
#  are decisions here.
PLAN = "plan"
PROPOSAL = "proposal"

#  What is being contested. A *file* is a specific set of bytes with an
#  identity; a *place* is a path that can hold one thing.
FILE = "file"
PLACE = "place"

#  How a conflict reads to a person. Kept to three: every additional category
#  needs a rule for telling it from its neighbours, and a page that shows four
#  words for one situation has explained nothing.
SAME_FILE = "same-file"
SAME_PLACE = "same-place"
RELATED_FILE = "related-file"

#  How many conflicting decisions one card names before it stops listing them.
SHOWN = 3

#  A cap on how much of a contested library the whole-database summary will
#  walk. Reached only by a database with thousands of simultaneous collisions,
#  which is a state with its own problem.
LIMIT = 500


@dataclass(frozen=True)
class Party:
    """One decision holding a claim on the contested file or place."""

    kind: str
    ref: str
    summary: str = ""
    #  True when this decision *operates* on the subject; false when it merely
    #  depends on it staying as it is. The distinction is what keeps two
    #  decisions that both mention the same relationship from being called a
    #  conflict — neither of them is changing it.
    operates: bool = True

    @property
    def href(self) -> str:
        return "/commit"


@dataclass(frozen=True)
class Conflict:
    """One contested file or place, and every decision claiming it."""

    scope: str
    subject: str
    parties: tuple[Party, ...] = ()

    @property
    def kind(self) -> str:
        if self.scope == PLACE:
            return SAME_PLACE
        if any(not party.operates for party in self.parties):
            return RELATED_FILE
        return SAME_FILE

    @property
    def name(self) -> str:
        return PurePosixPath(self.subject).name or self.subject

    @property
    def headline(self) -> str:
        count = len(self.parties)
        many = "Two" if count == 2 else str(count)
        if self.kind == SAME_PLACE:
            return f"{many} decisions expect to put a file at the same path"
        if self.kind == RELATED_FILE:
            return "Another decision would move a file this one was explained by"
        return f"{many} decisions expect to change the same file"

    @property
    def detail(self) -> str:
        if self.kind == RELATED_FILE:
            return (
                f"{self.name} is not part of both decisions, but one of them "
                f"would move it and the other was approved on the basis that it "
                f"stays where it is."
            )
        return (
            "Only one of them can still be right in the current state. "
            "Send one back and the other becomes valid again."
        )

    def without(self, kind: str, ref: str) -> tuple[Party, ...]:
        """The other side, for a card that already knows which one it is."""
        return tuple(
            party
            for party in self.parties
            if not (party.kind == kind and party.ref == str(ref))
        )


#  Every claim every waiting decision makes, as rows.
#
#  Grouped by the thing claimed rather than joined decision-against-decision.
#  A self-join over waiting operations is O(n²) in the size of the queue —
#  five thousand waiting operations is twenty-five million comparisons to find
#  the handful of pairs that collide. Grouping by a key is one sort.
#
#  Each operation makes two claims and they are not the same claim: it consumes
#  a file, and it occupies a place. Two decisions moving the same file collide;
#  two decisions writing to the same path collide; a decision writing to a path
#  another decision is *vacating* does not, and keeping the two scopes apart is
#  what stops that being reported as a collision.
_CLAIMS = """
  SELECT 'plan' AS kind, o.plan_id AS ref, 'file' AS scope,
         'file:#' || o.item_id AS key, 1 AS operates, o.src_relpath AS label
    FROM plan_ops o JOIN plans p ON p.id = o.plan_id
   WHERE p.status='approved' AND o.executed_at IS NULL AND o.item_id IS NOT NULL
  UNION ALL
  SELECT 'plan', o.plan_id, 'file',
         'file:' || o.src_root || ':' || o.src_relpath, 1, o.src_relpath
    FROM plan_ops o JOIN plans p ON p.id = o.plan_id
   WHERE p.status='approved' AND o.executed_at IS NULL
  UNION ALL
  SELECT 'plan', o.plan_id, 'place',
         'place:' || o.dest_root || ':' || o.dest_relpath, 1, o.dest_relpath
    FROM plan_ops o JOIN plans p ON p.id = o.plan_id
   WHERE p.status='approved' AND o.executed_at IS NULL
  UNION ALL
  --  A file the decision does not touch and was explained in terms of. It
  --  claims nothing — `operates` is 0 — so two decisions that both merely
  --  mention it are not in conflict. Only a decision that would *move* it
  --  collides with one that was approved on the basis of it staying.
  SELECT 'plan', r.plan_id, 'file', 'file:#' || r.outside_item_id, 0,
         COALESCE(i.relpath, '')
    FROM plan_relationships r JOIN plans p ON p.id = r.plan_id
    LEFT JOIN items i ON i.id = r.outside_item_id
   WHERE p.status='approved' AND r.outside_item_id IS NOT NULL
  UNION ALL
  --  An approved arrival. It has no operations yet — the filing plan is built
  --  when Commit is pressed — but it is a decision somebody made about a
  --  specific file and a specific destination, and it can collide with both.
  SELECT 'proposal', CAST(pr.id AS TEXT), 'file', 'file:#' || pr.item_id, 1, i.relpath
    FROM proposals pr JOIN items i ON i.id = pr.item_id
   WHERE pr.status='approved' AND pr.dest_relpath IS NOT NULL
     AND i.missing_since IS NULL AND NOT {carried_out}
  UNION ALL
  SELECT 'proposal', CAST(pr.id AS TEXT), 'place',
         'place:' || pr.dest_root || ':' || pr.dest_relpath, 1, pr.dest_relpath
    FROM proposals pr JOIN items i ON i.id = pr.item_id
   WHERE pr.status='approved' AND pr.dest_relpath IS NOT NULL
     AND i.missing_since IS NULL AND NOT {carried_out}
"""

#  An arrival whose decision has already been turned into operations.
#
#  A comparison answered in Review builds the plan and *leaves the proposal
#  approved*, pointing at the same file and the same destination — one decision
#  wearing two rows, which is how the Commit page can show an arrival being set
#  aside while a plan carries the move out. Counting both as decisions makes
#  every one of those collide with itself, which is not a conflict but a
#  spelling.
_CARRIED_OUT = """EXISTS (
      SELECT 1 FROM plan_ops o2 JOIN plans p2 ON p2.id = o2.plan_id
       WHERE p2.status='approved' AND o2.executed_at IS NULL
         AND o2.item_id = pr.item_id
    )"""

_CLAIMS = _CLAIMS.format(carried_out=_CARRIED_OUT)

#  A key is contested when more than one decision claims it and at least one of
#  them would actually change it.
_CONTESTED = f"""
  SELECT key, MIN(label) AS label FROM ({_CLAIMS})
   GROUP BY key
  HAVING COUNT(DISTINCT kind || ':' || ref) > 1 AND SUM(operates) > 0
"""


def count(conn: sqlite3.Connection) -> int:
    """How many waiting decisions are in at least one conflict.

    Decisions, not pairs. "Two waiting decisions conflict" is what somebody
    needs to know; whether that is one collision or three is a detail for the
    page that shows them.
    """
    return int(
        conn.execute(
            f"""
            SELECT COUNT(*) FROM (
              SELECT DISTINCT c.kind, c.ref FROM ({_CLAIMS}) c
               WHERE c.key IN (SELECT key FROM ({_CONTESTED}))
            )
            """  # noqa: S608 - both fragments are module constants
        ).fetchone()[0]
    )


def contested(conn: sqlite3.Connection, *, limit: int = LIMIT) -> list[Conflict]:
    """Every current conflict, bounded. For a page that lists them."""
    keys = [
        (str(row["key"]), str(row["label"] or ""))
        for row in conn.execute(
            f"SELECT key, label FROM ({_CONTESTED}) ORDER BY key LIMIT ?",  # noqa: S608
            (limit,),
        )
    ]
    return _build(conn, keys)


def for_decisions(
    conn: sqlite3.Connection, decisions: list[tuple[str, str]]
) -> dict[tuple[str, str], list[Conflict]]:
    """The conflicts touching each of these decisions — two queries for a page.

    Asked for the fifty cards a Commit page is drawing, never for the whole
    database. The keys those decisions claim come first, and only then the
    other claimants of those keys, which is how the far side of a collision is
    found without walking every waiting decision.
    """
    if not decisions:
        return {}
    refs = [f"{kind}:{ref}" for kind, ref in decisions]
    placeholders = ",".join("?" * len(refs))
    mine = [
        str(row["key"])
        for row in conn.execute(
            f"""
            SELECT DISTINCT c.key AS key FROM ({_CLAIMS}) c
             WHERE c.kind || ':' || c.ref IN ({placeholders})
            """,  # noqa: S608 - placeholders are counted from the caller's list
            refs,
        )
    ]
    if not mine:
        return {}
    holders = ",".join("?" * len(mine))
    rows = conn.execute(
        f"""
        SELECT c.key AS key, c.scope AS scope, c.kind AS kind, c.ref AS ref,
               MAX(c.operates) AS operates, MIN(c.label) AS label
          FROM ({_CLAIMS}) c
         WHERE c.key IN ({holders})
         GROUP BY c.key, c.scope, c.kind, c.ref
        """,  # noqa: S608 - placeholders are counted from the query above
        mine,
    ).fetchall()
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(str(row["key"]), []).append(row)
    keys = [
        (key, str(min(str(row["label"] or "") for row in claims)))
        for key, claims in grouped.items()
        if len(claims) > 1 and any(int(row["operates"]) for row in claims)
    ]
    conflicts = _build(conn, sorted(keys), claims=grouped)
    found: dict[tuple[str, str], list[Conflict]] = {}
    for conflict in conflicts:
        for party in conflict.parties:
            key = (party.kind, party.ref)
            if key in {(kind, str(ref)) for kind, ref in decisions}:
                found.setdefault(key, []).append(conflict)
    return found


def check(conn: sqlite3.Connection, plan_id: str) -> list[Conflict]:
    """Every conflict this one plan is in — asked of a plan of any status.

    Approval is the moment this matters most, and at that moment the plan is
    still a draft: it is not in the waiting queue, so it cannot be found by
    looking through the waiting queue. So its claims are read from its own
    operations and matched against everything already waiting.

    The same function answers for an approved plan, which is what makes the
    sentence on the Commit card and the sentence at approval the same sentence.
    """
    claims = _own_claims(conn, plan_id)
    if not claims:
        return []
    keys = sorted({key for key, _ in claims})
    #  The arrivals this plan is the execution of.
    #
    #  An approved proposal and the plan that carries it out are one decision:
    #  Review approves "file this here", Commit builds the operations, and for
    #  the moment in between both rows exist and name the same file and the
    #  same destination. Every single filing looks like a collision without
    #  this, which is how a safeguard becomes a thing people switch off.
    mine_items = _own_items(conn, plan_id)
    excluded = ",".join("?" * len(mine_items)) if mine_items else "NULL"
    placeholders = ",".join("?" * len(keys))
    rows = conn.execute(
        f"""
        SELECT c.key AS key, c.scope AS scope, c.kind AS kind, c.ref AS ref,
               MAX(c.operates) AS operates, MIN(c.label) AS label
          FROM ({_CLAIMS}) c
         WHERE c.key IN ({placeholders})
           AND NOT (c.kind = 'plan' AND c.ref = ?)
           AND NOT (c.kind = 'proposal' AND CAST(c.ref AS INTEGER) IN (
                 SELECT pr.id FROM proposals pr WHERE pr.item_id IN ({excluded})))
         GROUP BY c.key, c.scope, c.kind, c.ref
        """,  # noqa: S608 - placeholders are counted from this plan's own keys
        (*keys, plan_id, *mine_items),
    ).fetchall()
    if not rows:
        return []
    labels = dict(claims)
    others: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        others.setdefault(str(row["key"]), []).append(row)
    from librairy.planner import summarise

    plans = summarise(
        conn,
        [str(row["ref"]) for rows_ in others.values() for row in rows_
         if str(row["kind"]) == PLAN],
    )
    arrivals = _arrivals(
        conn,
        [int(row["ref"]) for rows_ in others.values() for row in rows_
         if str(row["kind"]) == PROPOSAL and str(row["ref"]).isdigit()],
    )
    mine = summarise(conn, [plan_id]).get(plan_id, ("", ""))[1]
    found = []
    for key in keys:
        claimants = others.get(key, [])
        if not claimants:
            continue
        parties = (
            Party(kind=PLAN, ref=plan_id, summary=mine, operates=True),
            *(
                Party(
                    kind=str(row["kind"]),
                    ref=str(row["ref"]),
                    summary=(
                        plans.get(str(row["ref"]), ("", ""))[1]
                        if str(row["kind"]) == PLAN
                        else arrivals.get(str(row["ref"]), "")
                    ),
                    operates=bool(int(row["operates"])),
                )
                for row in claimants
            ),
        )
        found.append(
            Conflict(
                scope=str(claimants[0]["scope"]),
                subject=labels.get(key, ""),
                parties=parties,
            )
        )
    return found


def _own_items(conn: sqlite3.Connection, plan_id: str) -> list[int]:
    """The items this plan operates on, by identity."""
    return [
        int(row["item_id"])
        for row in conn.execute(
            "SELECT DISTINCT item_id FROM plan_ops"
            " WHERE plan_id=? AND item_id IS NOT NULL",
            (plan_id,),
        )
    ]


def _own_claims(conn: sqlite3.Connection, plan_id: str) -> list[tuple[str, str]]:
    """What one plan claims, read from its operations rather than the queue."""
    claims: list[tuple[str, str]] = []
    for row in conn.execute(
        "SELECT item_id, src_root, src_relpath, dest_root, dest_relpath"
        " FROM plan_ops WHERE plan_id=? AND executed_at IS NULL",
        (plan_id,),
    ):
        source = str(row["src_relpath"])
        if row["item_id"] is not None:
            claims.append((f"file:#{int(row['item_id'])}", source))
        claims.append((f"file:{row['src_root']}:{source}", source))
        claims.append(
            (f"place:{row['dest_root']}:{row['dest_relpath']}", str(row["dest_relpath"]))
        )
    return claims


def _build(
    conn: sqlite3.Connection,
    keys: list[tuple[str, str]],
    *,
    claims: dict[str, list[sqlite3.Row]] | None = None,
) -> list[Conflict]:
    """Turn contested keys into conflicts, naming every decision once."""
    if not keys:
        return []
    if claims is None:
        wanted = [key for key, _ in keys]
        placeholders = ",".join("?" * len(wanted))
        claims = {}
        for row in conn.execute(
            f"""
            SELECT c.key AS key, c.scope AS scope, c.kind AS kind, c.ref AS ref,
                   MAX(c.operates) AS operates, MIN(c.label) AS label
              FROM ({_CLAIMS}) c
             WHERE c.key IN ({placeholders})
             GROUP BY c.key, c.scope, c.kind, c.ref
            """,  # noqa: S608 - placeholders are counted from the key list
            wanted,
        ):
            claims.setdefault(str(row["key"]), []).append(row)
    plan_ids = {
        str(row["ref"])
        for rows in claims.values()
        for row in rows
        if str(row["kind"]) == PLAN
    }
    proposal_ids = {
        int(row["ref"])
        for rows in claims.values()
        for row in rows
        if str(row["kind"]) == PROPOSAL and str(row["ref"]).isdigit()
    }
    from librairy.planner import summarise

    plans = summarise(conn, sorted(plan_ids))
    arrivals = _arrivals(conn, sorted(proposal_ids))
    found = []
    for key, label in keys:
        rows = claims.get(key, [])
        if len(rows) < 2:
            continue
        parties = tuple(
            Party(
                kind=str(row["kind"]),
                ref=str(row["ref"]),
                summary=(
                    plans.get(str(row["ref"]), ("", ""))[1]
                    if str(row["kind"]) == PLAN
                    else arrivals.get(str(row["ref"]), "")
                ),
                operates=bool(int(row["operates"])),
            )
            for row in sorted(rows, key=lambda row: (str(row["kind"]), str(row["ref"])))
        )
        found.append(
            Conflict(
                scope=str(rows[0]["scope"]),
                subject=label,
                parties=parties,
            )
        )
    return found


def _arrivals(conn: sqlite3.Connection, proposal_ids: list[int]) -> dict[str, str]:
    """One line naming each arriving file, from the row that proposes it."""
    if not proposal_ids:
        return {}
    placeholders = ",".join("?" * len(proposal_ids))
    return {
        str(row["id"]): (
            f"filing {PurePosixPath(str(row['relpath'])).name}"
            if row["relpath"]
            else "an arriving file"
        )
        for row in conn.execute(
            f"""
            SELECT pr.id AS id, i.relpath AS relpath FROM proposals pr
              JOIN items i ON i.id = pr.item_id WHERE pr.id IN ({placeholders})
            """,  # noqa: S608 - placeholders are counted from the id list
            proposal_ids,
        )
    }


def arrivals_in_conflict(conn: sqlite3.Connection) -> dict[str, int]:
    """Approved arrivals in a conflict, split by where they are going.

    Every approved arrival is filed by one plan built when Commit is pressed,
    so one collision among them stops all of them: the plan would name two
    files into a single destination and approval refuses exactly that. The
    group's button has to know, and it cannot learn it from the fifty rows it
    happens to be drawing.
    """
    rows = conn.execute(
        f"""
        SELECT CASE WHEN pr.dest_root='quarantine' THEN 'set-aside'
                    ELSE 'new-file' END AS kind, COUNT(*) AS decisions
          FROM proposals pr
         WHERE pr.status='approved'
           AND CAST(pr.id AS TEXT) IN (
                 SELECT c.ref FROM ({_CLAIMS}) c
                  WHERE c.kind='proposal'
                    AND c.key IN (SELECT key FROM ({_CONTESTED}))
               )
         GROUP BY kind
        """  # noqa: S608 - both fragments are module constants
    ).fetchall()
    return {str(row["kind"]): int(row["decisions"]) for row in rows}
