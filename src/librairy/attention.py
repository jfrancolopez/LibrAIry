"""What needs attention now — derived, never stored.

Health used to answer *is the machinery working*: are the helper binaries
installed, does the AI endpoint answer, is there disk space, did the backup
run. All of that is worth knowing and none of it is what somebody opens a
health page to find out once a program has been running for a while. The
question by then is:

    is anything waiting on me, and is anything quietly wrong?

Both halves are already in the database. An approval whose source has moved on
is a row in `plan_ops` beside a row in `items`. A queued file somebody deleted
outside LibrAIry is a `missing_since`. An audit that stopped half way through
is an `audit_runs` row in `cancelled`. Nothing here is measured, probed or
discovered — it is read, and it is read from the same tables the workflow that
owns it reads.

Three rules shape the module.

**No table.** A `health_events` table would be a second account of facts that
already exist, free to disagree with them, and needing to be written by
everything that changes anything. Deriving costs a handful of aggregate
queries and cannot go stale. The one thing that *is* stored — the Format Policy
impact snapshot — is stored because measuring it walks the whole index, and
this module reports its age rather than refreshing it.

**No work.** `report()` opens no file, runs no subprocess, calls no provider
and writes nothing. A page that repaired what it found would destroy the
evidence of the bug that produced it, and a page that measured what it reported
would get slower the more somebody owns. Every concern links to the workflow
that owns the fix.

**Three levels, and they have to mean something.** `Critical / High / Medium /
Low` is severity vocabulary borrowed from incident response, and applying it to
a file that has not been measured yet turns an ordinary backlog into a wall of
warnings nobody reads twice. So:

    ACTION      something is wrong and a person has to decide what to do
    ATTENTION   worth knowing before it becomes the first kind
    INFORMATION operational state that is not a problem at all

A blocked Undo is the clearest case of the third. It means the safeguard built
last pass is working exactly as designed, and colouring it red would teach
people that red means nothing.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from librairy.humanize import human_ago, human_bytes

#  The three levels, most urgent first. Deliberately short: a fourth would need
#  a rule for telling it from its neighbours, and there is no such rule.
ACTION = "action"
ATTENTION = "attention"
INFORMATION = "information"
LEVELS = (ACTION, ATTENTION, INFORMATION)

LEVEL_LABEL = {
    ACTION: "Needs a decision",
    ATTENTION: "Worth knowing",
    INFORMATION: "Information",
}

#  What each level actually promises the reader, said on the page. A heading
#  called "Needs a decision" with no explanation is a heading somebody has to
#  learn by watching what appears under it.
LEVEL_NOTE = {
    ACTION: "Something here can no longer do what it says it will. "
            "Each one is answered on the page that owns it.",
    ATTENTION: "Nothing is broken. These are the things worth knowing about "
               "before they turn into the ones above.",
    INFORMATION: "Current operational state. Nothing here needs doing.",
}

#  How many named examples one concern may carry. Three is enough to recognise
#  what a count is about and few enough that a concern stays one paragraph —
#  the owning page is where the full list lives.
SHOWN = 3


@dataclass(frozen=True)
class Example:
    """One named instance under a concern. Text, and optionally where it is."""

    text: str
    detail: str = ""


@dataclass(frozen=True)
class Concern:
    """One thing Health has to say, and where it is answered."""

    code: str
    level: str
    headline: str
    detail: str = ""
    examples: tuple[Example, ...] = ()
    more: int = 0
    href: str = ""
    #  The words on the link. Always the name the destination already uses for
    #  itself — a page that invents "Review Commit" for the thing every other
    #  page calls "Commit" has made one destination look like two.
    action: str = ""
    count: int = 0

    @property
    def actionable(self) -> bool:
        return self.level == ACTION


@dataclass(frozen=True)
class Report:
    """Everything Health has to say, grouped by how much it matters."""

    concerns: tuple[Concern, ...] = ()

    def at(self, level: str) -> list[Concern]:
        return [concern for concern in self.concerns if concern.level == level]

    @property
    def action(self) -> list[Concern]:
        return self.at(ACTION)

    @property
    def attention(self) -> list[Concern]:
        return self.at(ATTENTION)

    @property
    def information(self) -> list[Concern]:
        return self.at(INFORMATION)

    @property
    def needing(self) -> int:
        """How many things a person is being asked to look at.

        Information is excluded on purpose. A summary that counts "last audit
        completed yesterday" as something needing attention is a summary that
        can never reach zero, and a number that never reaches zero stops being
        read.
        """
        return len(self.action) + len(self.attention)

    @property
    def settled(self) -> bool:
        return self.needing == 0


def report(conn: sqlite3.Connection, settings=None) -> Report:  # noqa: ANN001
    """Everything currently worth saying, in one pass of aggregate queries.

    Each probe is independent and each is allowed to find nothing — a quiet
    installation produces a report with information in it and no concerns,
    which is the state this page exists to make legible.
    """
    probes = (
        _stale_approvals,
        _conflicting_plans,
        _delete_queue,
        _audit,
        _search,
        _photos,
        _format_impact,
        _blocked_undo,
        _policy,
        _learned,
    )
    found: list[Concern] = []
    for probe in probes:
        found.extend(probe(conn, settings))
    order = {level: rank for rank, level in enumerate(LEVELS)}
    found.sort(key=lambda concern: order.get(concern.level, len(LEVELS)))
    return Report(concerns=tuple(found))


# --- approvals that can no longer run -----------------------------------------

#  Why an approval stopped describing what would happen, worst first. The rank
#  is what makes one plan count once: a decision whose source is gone *and*
#  whose destination is now occupied is one outdated approval, not two.
DRIFT_REASONS = (
    (1, "missing", "the file is no longer where it was"),
    (2, "changed", "the file changed after it was approved"),
    (3, "protected", "the original is now protected by your Format Policy"),
    (4, "occupied", "another file now occupies the destination"),
    (5, "related", "a related file changed"),
)
DRIFT_LABEL = {code: text for _, code, text in DRIFT_REASONS}
DRIFT_RANK = {rank: code for rank, code, _ in DRIFT_REASONS}

#  Every unexecuted operation of every approved plan, with what the index says
#  about it now.
#
#  Index-only, and that is the whole design. `correction_state.plan_drift`
#  answers the same question by hashing every source, which is right for one
#  card and wrong for a page that summarises the whole queue — it would open
#  every file in the queue on every Health load. So this reads what the scanner
#  already recorded. It can therefore *under*-report: a file changed since the
#  last scan looks unchanged here, and the Commit card and then the executor
#  both catch it. Under-reporting is the safe direction for a page whose job is
#  to point at the workflow that decides.
_APPROVED_OPS = """
  SELECT p.id AS plan_id,
         CASE
           WHEN i.id IS NULL OR i.missing_since IS NOT NULL THEN 1
           WHEN i.fingerprint IS NOT NULL AND o.src_fingerprint <> ''
                AND i.fingerprint <> o.src_fingerprint THEN 2
           WHEN o.dest_root='quarantine' AND o.src_root='library'
                AND EXISTS (SELECT 1 FROM format_policy_scopes s
                             WHERE s.preserve_originals=1 AND s.scope_kind='folder'
                               AND o.src_relpath LIKE s.scope_value || '/%') THEN 3
           WHEN EXISTS (SELECT 1 FROM items d
                         WHERE d.root = o.dest_root AND d.relpath = o.dest_relpath
                           AND d.missing_since IS NULL
                           AND d.id IS NOT COALESCE(i.id, -1)) THEN 4
           ELSE 0
         END AS rank
  FROM plans p
  JOIN plan_ops o ON o.plan_id = p.id AND o.executed_at IS NULL
  LEFT JOIN items i ON i.root = o.src_root AND i.relpath = o.src_relpath
  WHERE p.status = 'approved'
"""

#  The other half: a file this decision was *explained in terms of* and does not
#  touch. `plan_relationships` recorded what the person was shown; if that file
#  has gone or is different bytes, the sentence on the card is no longer true.
#  Same question `relationship_impact.drift` asks per plan, asked here for the
#  whole queue at once.
_APPROVED_RELATED = """
  SELECT p.id AS plan_id, 5 AS rank
  FROM plans p
  JOIN plan_relationships r ON r.plan_id = p.id
  JOIN items i ON i.id = r.outside_item_id
  WHERE p.status = 'approved' AND r.outside_item_id IS NOT NULL
    AND (i.missing_since IS NOT NULL
         OR (r.outside_fingerprint IS NOT NULL
             AND i.fingerprint IS NOT COALESCE(r.outside_fingerprint, i.fingerprint)))
"""

_WORST_PER_PLAN = f"""
  SELECT plan_id, MIN(rank) AS rank FROM (
    SELECT plan_id, rank FROM ({_APPROVED_OPS}) WHERE rank > 0
    UNION ALL
    SELECT plan_id, rank FROM ({_APPROVED_RELATED})
  ) GROUP BY plan_id
"""


def _stale_approvals(conn: sqlite3.Connection, settings=None) -> list[Concern]:  # noqa: ANN001, ARG001
    """Waiting decisions that can no longer do what they say.

    One aggregate query for the counts and one bounded query for the examples.
    Neither builds a row per plan in Python, which is what keeps this the same
    cost at ten waiting decisions and at ten thousand.
    """
    counts = {
        DRIFT_RANK.get(int(row["rank"]), ""): int(row["plans"])
        for row in conn.execute(
            f"SELECT rank, COUNT(*) AS plans FROM ({_WORST_PER_PLAN}) GROUP BY rank"  # noqa: S608
        )
    }
    total = sum(counts.values())
    if not total:
        return []
    lines = tuple(
        Example(f"{count} {DRIFT_LABEL[code]}")
        for _, code, _ in DRIFT_REASONS
        if (count := counts.get(code, 0))
    )
    return [
        Concern(
            code="stale-approvals",
            level=ACTION,
            headline=(
                f"{total} waiting decision{'' if total == 1 else 's'} "
                f"need{'s' if total == 1 else ''} another look"
            ),
            detail=(
                "Each was approved against files that have since moved on. "
                "Commit re-checks every one before it runs, so none of these "
                "can go wrong quietly — but none of them can run either."
            ),
            examples=lines,
            href="/commit",
            action="View in Commit",
            count=total,
        )
    ]


# --- conflicts between waiting decisions --------------------------------------


def _conflicting_plans(conn: sqlite3.Connection, settings=None) -> list[Concern]:  # noqa: ANN001, ARG001
    """Waiting decisions that cannot both remain valid.

    Reported, never resolved. Which of two decisions to keep is exactly the
    kind of question a health page must not answer on somebody's behalf.
    """
    from librairy.plan_conflicts import count as conflict_count

    found = conflict_count(conn)
    if not found:
        return []
    return [
        Concern(
            code="plan-conflicts",
            level=ACTION,
            headline=(
                f"{found} waiting decision{'' if found == 1 else 's'} "
                f"{'is' if found == 1 else 'are'} in conflict"
            ),
            detail=(
                "Two approved decisions expect to change the same file, or to "
                "put two different files in the same place. Only one of each "
                "pair can still be right. Send one back and the other becomes "
                "valid again."
            ),
            href="/commit",
            action="View in Commit",
            count=found,
        )
    ]


# --- the delete queue ---------------------------------------------------------


def _delete_queue(conn: sqlite3.Connection, settings=None) -> list[Concern]:  # noqa: ANN001, ARG001
    """What is waiting, and anything about it that is no longer true.

    Two concerns from one query, at two different levels, because they are two
    different facts. A queue with files in it is not a problem — nothing is
    ever removed without somebody doing it — and a queued file that changed
    since is, because restoring it would put different bytes back.
    """
    from librairy.delete_queue import health as queue_health

    found = queue_health(conn)
    concerns: list[Concern] = []
    wrong = int(found["changed"]) + int(found["gone"])
    if wrong:
        parts = []
        if found["changed"]:
            parts.append(
                f"{found['changed']} changed since "
                f"{'it was' if found['changed'] == 1 else 'they were'} queued"
            )
        if found["gone"]:
            parts.append(
                f"{found['gone']} no longer on disk"
            )
        concerns.append(
            Concern(
                code="delete-queue-drift",
                level=ACTION,
                headline=(
                    f"{wrong} queued file{'' if wrong == 1 else 's'} "
                    f"{'is' if wrong == 1 else 'are'} not what was queued"
                ),
                detail=(
                    "Restore is not offered for these — putting them back "
                    "would not put back what the decision was about. "
                    + ", ".join(parts).capitalize() + "."
                ),
                href="/delete-queue",
                action="Delete queue",
                count=wrong,
            )
        )
    if found["files"]:
        concerns.append(
            Concern(
                code="delete-queue",
                level=INFORMATION,
                headline=(
                    f"{found['files']} file{'' if found['files'] == 1 else 's'} "
                    f"waiting in the delete queue"
                ),
                detail=(
                    f"{human_bytes(int(found['bytes']))} still on disk"
                    + (f", oldest queued {found['oldest']}" if found["oldest"] else "")
                    + ". Nothing is removed until you do it yourself."
                ),
                href="/delete-queue",
                action="Delete queue",
                count=int(found["files"]),
            )
        )
    return concerns


# --- the staged audit ---------------------------------------------------------


def _audit(conn: sqlite3.Connection, settings=None) -> list[Concern]:  # noqa: ANN001, ARG001
    """Where the library audit got to — the live one, and the last one to end.

    Two questions, deliberately, because a run starting now does not undo the
    fact that the previous one stopped in the middle of Similar media. Reading
    only the newest row answered the second question with the first one's
    state and hid the failure the moment somebody pressed Audit again.

    **Never "overdue".** There is no configured audit cadence in LibrAIry — an
    audit is something a person starts — so there is nothing to be late for.
    Inventing a threshold ("more than a day old") would manufacture a problem
    out of a working installation, and a page that does that once is a page
    people learn to scroll past.
    """
    from librairy.audit_job import (
        CANCELLED,
        COMPLETE,
        FAILED,
        LIVE_STATES,
        RUNNING,
        STAGE_LABEL,
    )

    concerns: list[Concern] = []
    placeholders = ",".join("?" * len(LIVE_STATES))
    live = conn.execute(
        f"SELECT state, stage FROM audit_runs WHERE state IN ({placeholders})"  # noqa: S608
        " ORDER BY id DESC LIMIT 1",
        LIVE_STATES,
    ).fetchone()
    ended = conn.execute(
        f"SELECT state, stage, error, finished_at FROM audit_runs"  # noqa: S608
        f" WHERE state NOT IN ({placeholders}) ORDER BY id DESC LIMIT 1",
        LIVE_STATES,
    ).fetchone()
    if live is not None:
        stage = STAGE_LABEL.get(str(live["stage"]), str(live["stage"]))
        concerns.append(
            Concern(
                code="audit-running",
                level=INFORMATION,
                headline=(
                    f"An audit is running — {stage}"
                    if str(live["state"]) == RUNNING
                    else "An audit is queued"
                ),
                detail="It reads the library and never moves anything.",
                href="/review",
                action="Review",
            )
        )
    if ended is None:
        if live is None:
            concerns.append(
                Concern(
                    code="audit-never",
                    level=INFORMATION,
                    headline="No library audit has run yet",
                    detail="An audit reads the library and records what it finds. "
                    "It never moves anything.",
                    href="/review",
                    action="Review",
                )
            )
        return concerns
    stage = STAGE_LABEL.get(str(ended["stage"]), str(ended["stage"]))
    state = str(ended["state"])
    if state == FAILED:
        concerns.append(
            Concern(
                code="audit-failed",
                level=ACTION,
                headline=f"The last audit failed during {stage}",
                detail=(str(ended["error"] or "").strip() or "No reason was recorded.")
                + " Everything it had already concluded was kept.",
                href="/review",
                action="Review",
            )
        )
    elif state == CANCELLED:
        concerns.append(
            Concern(
                code="audit-stopped",
                level=ATTENTION,
                headline=f"The last audit run stopped during {stage}",
                detail=(
                    "Stages after that one did not run, so anything only they "
                    "would have found has not been looked for. Starting an "
                    "audit again picks up from the beginning."
                ),
                href="/review",
                action="Review",
            )
        )
    elif state == COMPLETE:
        when = human_ago(str(ended["finished_at"] or "")) if ended["finished_at"] else ""
        concerns.append(
            Concern(
                code="audit-complete",
                level=INFORMATION,
                headline=f"Last audit completed {when}" if when else "Last audit completed",
                href="/review",
                action="Review",
            )
        )
    return concerns


# --- the search index ---------------------------------------------------------


def _search(conn: sqlite3.Connection, settings=None) -> list[Concern]:  # noqa: ANN001, ARG001
    """Only the two things the index can prove about itself cheaply.

    A present file with no index row is a real gap and one `NOT EXISTS` finds
    it. A recorded integrity failure is a verdict something else already
    reached. Everything beyond that — is each indexed row's *content* current —
    would mean re-deriving every row's text on a page load, which is a search
    project rather than a health check, so it is not asked here.
    """
    from librairy.search_health import recorded_health, unindexed

    concerns: list[Concern] = []
    health = recorded_health(conn)
    if not health.ok:
        concerns.append(
            Concern(
                code="search-damaged",
                level=ACTION,
                headline="The search index needs rebuilding",
                detail="Searches may be returning fewer results than they should. "
                       "Browse is unaffected — it walks the filesystem. The "
                       "Search index panel below rebuilds it.",
            )
        )
    found = unindexed(conn)
    if found:
        concerns.append(
            Concern(
                code="search-unindexed",
                level=ATTENTION,
                headline=(
                    f"{found} file{'' if found == 1 else 's'} on disk "
                    f"{'is' if found == 1 else 'are'} not in the search index"
                ),
                detail="They are in your library and Browse finds them; Search does "
                       "not. The Search index panel below has the numbers.",
                count=found,
            )
        )
    return concerns


# --- photographs nobody has measured ------------------------------------------

#  What a picture or a clip can be, by extension. The same sets photo pairing
#  uses — imported rather than restated, so a format added there is measured
#  here too.
def _image_suffixes() -> list[str]:
    from librairy.photo_pairs import MOTION_EXTS, RAW_EXTS, RENDER_EXTS

    return sorted(RAW_EXTS | RENDER_EXTS | MOTION_EXTS)


def _photos(conn: sqlite3.Connection, settings=None) -> list[Concern]:  # noqa: ANN001, ARG001
    """Arriving pictures whose companions cannot be established yet.

    **Deliberately not "every image with no cache row".** A library of sixty
    thousand filed JPEGs that nobody has run exiftool over is not unhealthy —
    they are filed, they are findable, and measuring them would change nothing
    about them. What matters is the picture that is *about to be filed*, where
    an unread capture time is the difference between a RAW and its JPEG being
    recognised as one exposure and being filed as two unrelated arrivals.

    So the question is narrowed to exactly that: images waiting for a decision
    in the inbox, with no capture metadata read from the bytes they have now.
    """
    from librairy.tools.common import IMAGE_TOOL

    suffixes = _image_suffixes()
    matches = " OR ".join("i.relpath LIKE ?" for _ in suffixes)
    found = int(
        conn.execute(
            f"""
            SELECT COUNT(*) FROM items i
            JOIN proposals pr ON pr.item_id = i.id
            LEFT JOIN item_metadata m ON m.item_id = i.id AND m.tool = ?
            WHERE i.root='inbox' AND i.missing_since IS NULL
              AND pr.status IN ('proposed','pending','postponed')
              AND ({matches})
              AND (m.fingerprint IS NULL OR m.fingerprint IS NOT i.fingerprint)
            """,  # noqa: S608 - the clause is built from a module constant
            (IMAGE_TOOL, *[f"%{suffix}" for suffix in suffixes]),
        ).fetchone()[0]
    )
    if not found:
        return []
    return [
        Concern(
            code="photos-unmeasured",
            level=ATTENTION,
            headline=(
                f"{found} arriving photo{'' if found == 1 else 's'} "
                f"{'has' if found == 1 else 'have'} not been measured yet"
            ),
            detail=(
                "Until a picture's capture metadata is read, LibrAIry cannot "
                "tell whether it is half of a Live Photo or the JPEG beside a "
                "RAW — so it files them as unrelated arrivals. The audit's "
                "photo stage reads them."
            ),
            href="/review",
            action="Review",
            count=found,
        )
    ]


# --- the format policy snapshot -----------------------------------------------


def _format_impact(conn: sqlite3.Connection, settings=None) -> list[Concern]:  # noqa: ANN001, ARG001
    """Whether the measured policy impact still describes the library.

    Reported, never refreshed. Measuring walks every indexed library row, and a
    page that did that while drawing itself would be the slowest page in the
    program and would get slower the more somebody owns.
    """
    from librairy.format_impact import is_stale, last

    report_ = last(conn)
    if report_ is None:
        return []
    if not is_stale(conn, report_):
        when = human_ago(str(report_.get("measured_at") or ""))
        return [
            Concern(
                code="format-impact",
                level=INFORMATION,
                headline=f"Format Policy impact measured {when}" if when
                else "Format Policy impact measured",
                href="/settings/format-policy",
                action="Format Policy",
            )
        ]
    when = human_ago(str(report_.get("measured_at") or ""))
    return [
        Concern(
            code="format-impact-stale",
            level=ATTENTION,
            headline="The Format Policy impact figures are out of date",
            detail=(
                f"Measured {when}, and the library has changed since. "
                if when
                else "The library has changed since it was measured. "
            )
            + "Nothing acts on those figures — they are there to be read before "
            "you change a policy.",
            href="/settings/format-policy",
            action="Format Policy",
        )
    ]


# --- decisions a later decision has built on ----------------------------------


def _blocked_undo(conn: sqlite3.Connection, settings=None) -> list[Concern]:  # noqa: ANN001, ARG001
    """Executed decisions that cannot currently be reversed.

    **Information, not a warning.** This is the safeguard working: a later
    decision built on what an earlier one did, so reversing the earlier one
    blind would silently discard the later choice. Nothing is broken, nothing
    is lost, and there is nothing to do about it — the later decision can be
    reversed first if somebody wants the earlier one back.
    """
    from librairy.undo_sequence import blocked

    found = blocked(conn)
    if not found.count:
        return []
    return [
        Concern(
            code="undo-blocked",
            level=INFORMATION,
            headline=(
                f"{found.count} earlier decision{'' if found.count == 1 else 's'} "
                f"cannot be undone yet"
            ),
            detail=(
                "A later decision depends on each of them. Reverse the later "
                "one first and the earlier one becomes available. Counted over "
                f"the {found.window} most recent decisions."
            ),
            examples=tuple(
                Example(text, detail) for text, detail in found.examples[:SHOWN]
            ),
            more=max(0, found.count - SHOWN),
            href="/history",
            action="History",
            count=found.count,
        )
    ]


# --- what the owner has configured --------------------------------------------


def _policy(conn: sqlite3.Connection, settings=None) -> list[Concern]:  # noqa: ANN001, ARG001
    """Protected scopes, counted. Never a problem — somebody asked for these."""
    found = int(
        conn.execute(
            "SELECT COUNT(*) FROM format_policy_scopes WHERE preserve_originals=1"
        ).fetchone()[0]
    )
    if not found:
        return []
    return [
        Concern(
            code="protected-scopes",
            level=INFORMATION,
            headline=(
                f"{found} folder{'' if found == 1 else 's'} "
                f"{'is' if found == 1 else 'are'} set to preserve originals"
            ),
            detail="No representation preference or optimization may trade "
                   "those originals away. Filing and renaming are unaffected.",
            href="/settings/format-policy",
            action="Format Policy",
            count=found,
        )
    ]


def _learned(conn: sqlite3.Connection, settings=None) -> list[Concern]:  # noqa: ANN001, ARG001
    """How many patterns have enough behind them to be offered as suggestions.

    A count, and nothing else. These are suggestions a person confirms one at a
    time; none of them acts, so none of them is a state to be concerned about.
    """
    from librairy.decisions import _COMPLETED, MIN_SUPPORT

    found = int(
        conn.execute(
            f"""
            SELECT COUNT(*) FROM (
              SELECT e.signature FROM decision_events e
              WHERE {_COMPLETED}
              GROUP BY e.signature, e.outcome
              HAVING COUNT(*) >= ?
            )
            """,  # noqa: S608 - `_COMPLETED` is a module constant
            (MIN_SUPPORT,),
        ).fetchone()[0]
    )
    if not found:
        return []
    return [
        Concern(
            code="learned",
            level=INFORMATION,
            headline=(
                f"{found} learned pattern{'' if found == 1 else 's'} "
                f"{'is' if found == 1 else 'are'} being offered as suggestions"
            ),
            detail="Each is shown with what it was learned from and applied "
                   "only when you press it.",
            href="/review/learned",
            action="Learned patterns",
            count=found,
        )
    ]
