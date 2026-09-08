"""Which Projects matter right now — not a list of all of them.

A Project is a promoted tag: a **view** across files that stay exactly where
they are. It is not the `Projects/` folder, which is a filing destination like
any other, and nothing here can be created by a folder existing on disk. See
`docs/ui-vocabulary.md`.

## The question a card answers

    what is this          a name, how many files, what kinds
    has anything changed  what arrived recently, and when
    does it need me       unresolved decisions, approvals waiting, files
                          held for a provider, findings

Three lines, and the third is what decides the order. A Project that gained
forty photographs is *active*; a Project with one file nobody has answered for
is *asking*, and asking beats gaining every time.

**Gaining files is not a problem.** Nothing here colours ordinary activity, for
the same reason a drive in a drawer is not an error: a page that flags the
normal case teaches people that a flag means nothing.

## Ranking happens in SQL

A thousand Projects must not become a thousand Python objects that are sorted
afterwards, and a Project of forty thousand members must not be enumerated to
find out that it has forty thousand members. So:

    one statement       ranks every Project and returns the few that show
    one statement       breaks those few down by category
    one statement       says what backs those few up

Three, whatever the library holds and however many Projects exist. Each of the
three groups by tag *before* joining, so a file with two findings on it is one
file — a fan-out in the middle of an aggregate is how a count of members turns
into a count of rows.

## The order, and why the last term is a name

    anything asking for a person, first
    then how much is asking
    then what arrived recently
    then what changed most recently
    then the name

Four real signals before the alphabet, and the alphabet only to make ties
repeatable. A page that reshuffles between two renders is a page nobody can
learn.

## Backup coverage is a claim about policy, not about bytes

A Project spans categories — a house project is quotes, photographs and a video
walkthrough — and those categories can have different destinations, or none.
Reducing that to a green tick would undo the whole of M3-03, so the card says
what is *true*: how many of the kinds of file here are covered by a policy at
all, how many destinations, and how many of those last failed. Never *synced*,
*up to date* or *protected*, none of which this knows.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from librairy.humanize import human_ago, human_bytes

#  How many Projects the Dashboard shows. Small on purpose: this is a pointer
#  at the ones that matter, and "View all Projects" is one click away.
SHOWN = 6

#  How many the list page ranks at once. Bounded for the same reason every
#  other page is; the search box is how somebody reaches the rest.
PAGE = 50

#  What counts as recent. A week, because that is the unit somebody thinks in
#  when they ask "has anything happened".
RECENT_DAYS = 7

#  How many categories a card names before it says "and others". Three is
#  enough to recognise what a Project is made of.
KINDS = 3


@dataclass(frozen=True)
class Coverage:
    """What backs up the kinds of file in one Project, said carefully.

    Every field is a fact about *policy* — what is configured to go where —
    because that is all this can know without comparing. Whether the bytes are
    there is answered by comparing, which is what `librairy/transfer_plan.py`
    does and what no summary is allowed to imply.
    """

    categories: int = 0
    covered: int = 0
    destinations: int = 0
    failing: int = 0

    @property
    def any(self) -> bool:
        """Is there anything worth saying about backups here?

        A Project only gets a `Coverage` at all once *some* destination is
        configured. On a machine with no backups set up, every card would
        otherwise carry the same line about not being backed up — which is one
        conversation, and it belongs in Settings rather than on twenty cards.
        """
        return self.categories > 0

    @property
    def partial(self) -> bool:
        """Some kinds of file here go somewhere and some do not."""
        return bool(self.covered) and self.covered < self.categories

    @property
    def sentence(self) -> str:
        """Never a tick, and never a word this does not have evidence for."""
        if not self.categories:
            return ""
        if not self.destinations:
            return "No backup covers this yet"
        parts = [
            f"{self.destinations} destination{'' if self.destinations == 1 else 's'}"
        ]
        if self.partial:
            parts.append(f"covers {self.covered} of {self.categories} kinds of file here")
        if self.failing:
            parts.append(f"{self.failing} failed")
        return " · ".join(parts)


@dataclass(frozen=True)
class Card:
    """One Project, as much as a card should say about it."""

    id: int
    tag: str
    name: str
    files: int = 0
    bytes: int = 0
    added: int = 0
    unresolved: int = 0
    ready: int = 0
    waiting: int = 0
    findings: int = 0
    last_activity: str = ""
    kinds: tuple[str, ...] = ()
    other_kinds: int = 0
    coverage: Coverage = field(default_factory=Coverage)

    @property
    def href(self) -> str:
        return f"/projects/{self.id}"

    @property
    def asking(self) -> int:
        """How much of this is waiting for a person. Nought is the good case."""
        return self.unresolved + self.ready + self.waiting + self.findings

    @property
    def needs_attention(self) -> bool:
        return self.asking > 0

    @property
    def size(self) -> str:
        return human_bytes(self.bytes)

    @property
    def changed(self) -> str:
        return human_ago(self.last_activity) if self.last_activity else ""

    @property
    def made_of(self) -> str:
        """`Photos, Documents and 2 others`. What kind of thing this is."""
        named = ", ".join(self.kinds)
        if not named:
            return ""
        if self.other_kinds:
            return f"{named} and {self.other_kinds} other{'' if self.other_kinds == 1 else 's'}"
        return named

    @property
    def activity(self) -> str:
        """What arrived, in a sentence, or nothing at all.

        Deliberately not coloured and deliberately not counted as attention: a
        Project gaining files is a Project being used.
        """
        if not self.added:
            return ""
        return f"{self.added} file{'' if self.added == 1 else 's'} added this week"

    @property
    def asks(self) -> tuple[str, ...]:
        """Each thing waiting for a person, named separately.

        Separate because they are answered in different places, and a single
        number would send somebody looking for one page when they need three.
        """
        said = []
        if self.unresolved:
            said.append(f"{self.unresolved} waiting for your review")
        if self.ready:
            said.append(f"{self.ready} approved and waiting for Commit")
        if self.waiting:
            said.append(f"{self.waiting} waiting for a provider")
        if self.findings:
            said.append(f"{self.findings} audit finding{'' if self.findings == 1 else 's'}")
        return tuple(said)


#  Every Project, ranked, in one statement.
#
#  Each contribution is grouped by tag in its own subquery *before* being
#  joined, so nothing fans out: a file carrying two open findings is one file
#  in `files` and two in `findings`, which is what both numbers mean. Doing it
#  as one flat join would have made a count of members into a count of rows.
#  The ranking is an *outer* query over the aggregates rather than an ORDER BY
#  on the same SELECT, and that is not tidiness. Written the obvious way —
#  `ORDER BY (unresolved + ready + waiting + findings) > 0 DESC` on the select
#  that defines those aliases — SQLite resolves the names inside a compound
#  expression against the FROM clause rather than against the output aliases.
#  They exist there too, on the derived tables, and they are NULL for a Project
#  the LEFT JOIN found nothing for. `NULL + 0` is NULL, `NULL > 0` is NULL, and
#  NULL sorts *first* under DESC — so quiet Projects ranked above the ones
#  asking for a person, which is precisely backwards and entirely silent.
#  Out here the names are real columns of the subquery, already COALESCEd.
_RANKED = """
SELECT * FROM (
SELECT p.id, p.tag, p.name,
       COALESCE(m.files, 0)      AS files,
       COALESCE(m.bytes, 0)      AS bytes,
       COALESCE(m.added, 0)      AS added,
       COALESCE(m.waiting, 0)    AS waiting,
       COALESCE(m.last_seen, '') AS last_activity,
       COALESCE(r.unresolved, 0) AS unresolved,
       COALESCE(r.ready, 0)      AS ready,
       COALESCE(f.findings, 0)   AS findings
FROM projects p
LEFT JOIN (
  SELECT t.tag,
         COUNT(*) AS files,
         SUM(i.size) AS bytes,
         SUM(CASE WHEN i.first_seen_at >= :since THEN 1 ELSE 0 END) AS added,
         SUM(CASE WHEN i.state = 'waiting' THEN 1 ELSE 0 END) AS waiting,
         MAX(i.last_seen_at) AS last_seen
  FROM item_tags t
  JOIN items i ON i.id = t.item_id AND i.missing_since IS NULL
  GROUP BY t.tag
) m ON m.tag = p.tag
LEFT JOIN (
  SELECT t.tag,
         COUNT(DISTINCT CASE WHEN pr.status = 'proposed' THEN pr.item_id END) AS unresolved,
         COUNT(DISTINCT CASE WHEN pr.status = 'approved' THEN pr.item_id END) AS ready
  FROM item_tags t
  JOIN proposals pr ON pr.item_id = t.item_id
  JOIN items i ON i.id = t.item_id AND i.missing_since IS NULL
  WHERE pr.status IN ('proposed', 'approved')
  GROUP BY t.tag
) r ON r.tag = p.tag
LEFT JOIN (
  SELECT t.tag, COUNT(*) AS findings
  FROM item_tags t
  JOIN audit_findings af ON af.item_id = t.item_id AND af.status = 'open'
  GROUP BY t.tag
) f ON f.tag = p.tag
)
ORDER BY (unresolved + ready + waiting + findings) > 0 DESC,
         (unresolved + ready + waiting + findings) DESC,
         added DESC,
         last_activity DESC,
         name COLLATE NOCASE
LIMIT :limit
"""

#  What the chosen few are made of, for all of them at once.
_KINDS = """
SELECT t.tag, COALESCE(pr.category, '') AS category, COUNT(*) AS files
FROM item_tags t
JOIN items i ON i.id = t.item_id AND i.missing_since IS NULL
LEFT JOIN proposals pr ON pr.item_id = i.id AND pr.status != 'superseded'
WHERE t.tag IN ({placeholders})
GROUP BY t.tag, category
ORDER BY t.tag, files DESC, category
"""


def cards(conn: sqlite3.Connection, limit: int = SHOWN) -> list[Card]:
    """The Projects worth showing, most in need of a person first.

    Three statements whatever the library holds: rank, break down, and ask what
    backs them up. `tests/test_project_status.py` counts them.
    """
    ranked = list(
        conn.execute(
            _RANKED,
            {"since": _week_ago(), "limit": max(1, min(int(limit), PAGE))},
        )
    )
    if not ranked:
        return []
    tags = [str(row["tag"]) for row in ranked]
    kinds = _kinds_for(conn, tags)
    coverage = _coverage_for(conn, kinds)
    return [
        Card(
            id=int(row["id"]),
            tag=str(row["tag"]),
            name=str(row["name"]),
            files=int(row["files"] or 0),
            bytes=int(row["bytes"] or 0),
            added=int(row["added"] or 0),
            unresolved=int(row["unresolved"] or 0),
            ready=int(row["ready"] or 0),
            waiting=int(row["waiting"] or 0),
            findings=int(row["findings"] or 0),
            last_activity=str(row["last_activity"] or ""),
            kinds=tuple(_named(kinds.get(str(row["tag"]), []))[:KINDS]),
            other_kinds=max(0, len(_named(kinds.get(str(row["tag"]), []))) - KINDS),
            coverage=coverage.get(str(row["tag"]), Coverage()),
        )
        for row in ranked
    ]


def listed(
    conn: sqlite3.Connection, query: str = "", limit: int = PAGE
) -> list[Card]:
    """The list page: the same ranking, filtered, and still bounded.

    Filtered in SQL rather than by building every Project and dropping most of
    them — a search that has to construct a thousand cards to show two is the
    thing this module exists not to do.
    """
    if not query.strip():
        return cards(conn, limit)
    wanted = f"%{query.strip().lower()}%"
    matching = [
        str(row["tag"])
        for row in conn.execute(
            "SELECT tag FROM projects"
            " WHERE lower(name) LIKE ? OR lower(tag) LIKE ?"
            " ORDER BY name COLLATE NOCASE LIMIT ?",
            (wanted, wanted, max(1, min(int(limit), PAGE))),
        )
    ]
    return [card for card in cards(conn, PAGE) if card.tag in matching]


def counted(conn: sqlite3.Connection) -> int:
    """How many Projects there are at all, for "View all"."""
    return int(conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] or 0)


def _kinds_for(
    conn: sqlite3.Connection, tags: list[str]
) -> dict[str, list[tuple[str, int]]]:
    """Category counts for every chosen Project, in one statement."""
    sql = _KINDS.format(placeholders=", ".join("?" * len(tags)))
    found: dict[str, list[tuple[str, int]]] = {}
    for row in conn.execute(sql, tags):  # noqa: S608 - placeholders are bound
        found.setdefault(str(row["tag"]), []).append(
            (str(row["category"] or ""), int(row["files"] or 0))
        )
    return found


def _coverage_for(
    conn: sqlite3.Connection, kinds: dict[str, list[tuple[str, int]]]
) -> dict[str, Coverage]:
    """Which of each Project's categories a policy covers, and how those are going.

    One statement for the policies and one pass in Python over a handful of
    rows. A Project spanning three categories where two have destinations is
    exactly the case a green tick would lie about.
    """
    from librairy import backup_runs, destinations

    policies = [
        (policy, destination)
        for policy, destination in destinations.active(conn)
    ]
    if not policies:
        return {}
    #  One run lookup per destination, not per project: a handful either way,
    #  and this keeps it a handful when there are fifty Projects on screen.
    failing = {
        destination.id: bool(
            (found := backup_runs.last_finished(conn, destination.id))
            and found.state == backup_runs.FAILED
        )
        for _policy, destination in policies
    }
    by_category: dict[str, set[int]] = {}
    for policy, destination in policies:
        by_category.setdefault(policy.category, set()).add(destination.id)

    coverage: dict[str, Coverage] = {}
    for tag, rows in kinds.items():
        categories = {category for category, files in rows if category and files}
        reached = {
            destination
            for category in categories
            for destination in by_category.get(category, ())
        }
        coverage[tag] = Coverage(
            categories=len(categories),
            covered=len([category for category in categories if by_category.get(category)]),
            destinations=len(reached),
            failing=len([destination for destination in reached if failing.get(destination)]),
        )
    return coverage


def _named(rows: list[tuple[str, int]]) -> list[str]:
    """The categories a Project actually spans, in the taxonomy's own words.

    Files with no category yet are not a kind of file — they are files nothing
    has decided about — so they are left out rather than shown as a category
    called nothing.
    """
    from librairy.transfer_plan import _folder  # noqa: PLC2701

    return [_folder(category) for category, files in rows if category and files]


def _week_ago() -> str:
    return (datetime.now(UTC) - timedelta(days=RECENT_DAYS)).isoformat(timespec="seconds")
