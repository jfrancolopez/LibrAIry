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

#  How many recent failed commits a concern is derived from. Bounded because the
#  set only ever grows: a plan that failed in March is still a failed plan, and a
#  page that reads all of them gets slower every month.
RECENT_PLANS = 50


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


def report(conn: sqlite3.Connection, settings=None, counts=None) -> Report:  # noqa: ANN001
    """Everything currently worth saying, in one pass of aggregate queries.

    Each probe is independent and each is allowed to find nothing — a quiet
    installation produces a report with information in it and no concerns,
    which is the state this page exists to make legible.

    `counts` is what the search index holds, when the caller has already
    counted it. Health draws this report *and* the index panel, and counting
    an FTS5 table means reading it: every probe takes the argument so the loop
    stays one line, and only `_search` has any use for it.
    """
    probes = (
        _stale_approvals,
        _conflicting_plans,
        _moved_files,
        _delete_queue,
        _audit,
        _search,
        _waiting,
        _photos,
        _format_impact,
        _blocked_undo,
        _unfinished_commit,
        _failed_commit,
        _storage,
        _policy,
        _learned,
        _transfers,
    )
    found: list[Concern] = []
    for probe in probes:
        found.extend(probe(conn, settings, counts))
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


def _stale_approvals(conn: sqlite3.Connection, settings=None, counts=None) -> list[Concern]:  # noqa: ANN001, ARG001
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


def _conflicting_plans(conn: sqlite3.Connection, settings=None, counts=None) -> list[Concern]:  # noqa: ANN001, ARG001
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


# --- files that turned up somewhere else --------------------------------------


def _moved_files(conn: sqlite3.Connection, settings=None, counts=None) -> list[Concern]:  # noqa: ANN001, ARG001
    """Indexed files whose bytes are on disk at a path LibrAIry does not expect.

    One count and a link. Health is frozen at the shape it reached: it says
    what needs a person and points at the page that owns the answer, and the
    answer here — which of these moves to agree to — is Reconcile's.
    """
    from librairy.reconcile import total as moved

    found = moved(conn)
    if not found:
        return []
    return [
        Concern(
            code="moved-files",
            level=ATTENTION,
            headline=(
                f"{found} file{'' if found == 1 else 's'} "
                f"{'is' if found == 1 else 'are'} not where LibrAIry expects"
            ),
            detail=(
                "The exact same bytes are on disk at a different path, so "
                "nothing has been lost — somebody moved them, or a restore put "
                "them back somewhere else. Agreeing to the new location moves "
                "no files."
            ),
            href="/reconcile",
            action="Reconcile",
            count=found,
        )
    ]


# --- the delete queue ---------------------------------------------------------


def _delete_queue(conn: sqlite3.Connection, settings=None, counts=None) -> list[Concern]:  # noqa: ANN001, ARG001
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


def _audit(conn: sqlite3.Connection, settings=None, counts=None) -> list[Concern]:  # noqa: ANN001, ARG001
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


def _search(conn: sqlite3.Connection, settings=None, counts=None) -> list[Concern]:  # noqa: ANN001, ARG001
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
    found = unindexed(conn, counts)
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


# --- files nothing could answer -----------------------------------------------


def _waiting(conn: sqlite3.Connection, settings=None, counts=None) -> list[Concern]:  # noqa: ANN001, ARG001
    """Files held because the evidence ran out and AI could not settle it.

    Two concerns rather than one, because they are answered on different days
    by different people. A provider that is down or broken is machinery, and it
    is `ATTENTION`: nothing is lost, the files resume by themselves, and the
    person who can fix it is the person who set the provider up. Files waiting
    on *evidence* resume for nobody, so they are the ones that will still be
    here next month unless somebody decides them — and that is a decision, not
    a fault, which is why neither of these is `ACTION`. Nothing here is broken.

    Counted, never listed. A provider that was off overnight can hold tens of
    thousands of files and this stays two aggregate queries either way; the
    list itself is a bounded section of Review.
    """
    from librairy import waiting

    counted = waiting.counts(conn)
    concerns: list[Concern] = []
    machinery = sum(counted.get(reason, 0) for reason in waiting.RESUMABLE)
    if machinery:
        reason = (
            waiting.FAILED if counted.get(waiting.FAILED) else waiting.UNAVAILABLE
        )
        concerns.append(
            Concern(
                code="waiting-provider",
                level=ATTENTION,
                headline=(
                    f"{machinery} file{'' if machinery == 1 else 's'} "
                    f"{'is' if machinery == 1 else 'are'} waiting for AI"
                ),
                detail="Nothing was guessed about them and nothing is lost. They "
                       "return to the queue on their own when a provider answers "
                       "again, and you can decide any of them by hand meanwhile.",
                examples=tuple(
                    Example(name) for name in waiting.examples(conn, reason)
                ),
                more=max(0, machinery - SHOWN),
                href="/review#review-waiting",
                action="Review",
                count=machinery,
            )
        )
    stalled = counted.get(waiting.EVIDENCE, 0)
    if stalled:
        concerns.append(
            Concern(
                code="waiting-evidence",
                level=ATTENTION,
                headline=(
                    f"{stalled} file{'' if stalled == 1 else 's'} "
                    f"{'needs' if stalled == 1 else 'need'} more than anything "
                    "here can tell"
                ),
                detail="Everything that could be asked was asked and answered, and "
                       "it was still not enough. Nothing is wrong, so nothing will "
                       "change on its own — these wait for you.",
                examples=tuple(
                    Example(name) for name in waiting.examples(conn, waiting.EVIDENCE)
                ),
                more=max(0, stalled - SHOWN),
                href="/review#review-waiting",
                action="Review",
                count=stalled,
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


def _photos(conn: sqlite3.Connection, settings=None, counts=None) -> list[Concern]:  # noqa: ANN001, ARG001
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


def _format_impact(conn: sqlite3.Connection, settings=None, counts=None) -> list[Concern]:  # noqa: ANN001, ARG001
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


def _blocked_undo(conn: sqlite3.Connection, settings=None, counts=None) -> list[Concern]:  # noqa: ANN001, ARG001
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


def _unfinished_commit(conn: sqlite3.Connection, settings=None, counts=None) -> list[Concern]:  # noqa: ANN001, ARG001
    """A commit whose process stopped before it finished.

    ACTION rather than ATTENTION, and the distinction is the whole point of the
    concern: files somebody approved are still sitting where they were, and
    nothing will move them until a person asks again. Nothing is lost and
    nothing is at risk — every operation that ran recorded its own result, and
    committing the plan again picks up exactly where the dead run stopped,
    including the file whose bytes had moved a moment before the process died.

    Silent while anything holds the lock, which is what keeps a commit that is
    genuinely running from being described as a dead one.
    """
    from librairy.commit_state import unfinished

    if settings is None:
        return []
    stopped = unfinished(conn, settings)
    if not stopped:
        return []
    return [
        Concern(
            code="commit-interrupted",
            level=ACTION,
            headline=f"A commit was interrupted"
                     f"{'' if len(stopped) == 1 else f' — {len(stopped)} of them'}",
            detail="The process stopped part way through. Nothing was lost and "
                   "nothing is half-moved: each file was verified before it was "
                   "recorded, and committing again carries on from where it "
                   "stopped.",
            examples=tuple(
                Example(text=stopped_one.sentence) for stopped_one in stopped[:SHOWN]
            ),
            more=max(0, len(stopped) - SHOWN),
            href="/commit",
            action="View in Commit",
            count=len(stopped),
        )
    ]


def _storage(conn: sqlite3.Connection, settings=None, counts=None) -> list[Concern]:  # noqa: ANN001, ARG001
    """A root LibrAIry cannot recognise, or cannot find.

    One concern for all of them rather than one per root, because a NAS that
    went away took the inbox, the library and the quarantine with it in the same
    instant and three identical red cards is three times the alarm and none of
    the information. See `librairy/roots.py` for what "recognise" means here.

    ACTION, and it is the most literal use of that level in the module: nothing
    LibrAIry can do will fix it, and until somebody mounts the storage every
    commit and every reversal will refuse.
    """
    from librairy import roots

    if settings is None:
        return []
    #  This one stats three directories, which the module's "no work" rule says
    #  it should not. The rule is about not *measuring* what can be derived, and
    #  whether a mount is present cannot be derived from any table — the database
    #  is the thing that would be wrong about it. `_missing_rclone` bends it the
    #  same way and much more expensively, for the same reason.
    unknown = [row for row in roots.state(conn, settings) if not row.recognised]
    if not unknown:
        return []
    #  Headed by what is actually wrong. "Library storage is not available" over
    #  an example reading "a different filesystem is mounted as your library" is
    #  two accounts of one thing, and the vaguer one is on top.
    names = ", ".join(row.label for row in unknown)
    first = unknown[0].failure
    what = first.what if first and len(unknown) == 1 else f"{names} storage is not available."
    return [
        Concern(
            code="storage-unavailable",
            level=ACTION,
            headline=what.rstrip("."),
            detail="Nothing will be moved into it, and nothing has been: a "
                   "commit or a reversal refuses before it touches a file when "
                   "the storage is not the one LibrAIry started against. "
                   + (first.next if first else "Reconnect it, then try again."),
            examples=tuple(
                Example(text=f"{row.label} — {row.detail}", detail=str(row.path))
                for row in unknown[:SHOWN]
            )
            if len(unknown) > 1
            else (),
            more=max(0, len(unknown) - SHOWN) if len(unknown) > 1 else 0,
            href="/health#machinery",
            action="View in Health",
            count=len(unknown),
        )
    ]


def _failed_commit(conn: sqlite3.Connection, settings=None, counts=None) -> list[Concern]:  # noqa: ANN001, ARG001
    """Files somebody committed that did not move, and the reason they did not.

    Not the same thing as `_unfinished_commit`, which is a *process* that
    stopped: this is a run that finished and reported failures. It had no
    concern at all — a Library remounted read-only produced "3 failed" on one
    screen, and Health went on saying nothing needed attention.

    Grouped by reason. Four policies that failed because rclone is missing is
    one missing binary, and the same is true here: forty files that would not
    fit on a full disk are one full disk.
    """
    from librairy.failures import from_outcome

    #  Anchored on `plans`, which has one row per commit, and never on a scan of
    #  the journal — `outcome LIKE 'failed %'` across a million-row history is
    #  one of the shapes that made this page take two seconds.
    failed = [
        str(row["id"])
        for row in conn.execute(
            "SELECT id FROM plans WHERE status='failed'"
            " ORDER BY finished_at DESC LIMIT ?",
            (RECENT_PLANS,),
        )
    ]
    if not failed:
        return []
    #  And joined to the file, which is what makes this concern go away by
    #  itself. A retry that succeeds moves the item to its destination, so the
    #  row no longer matches its own source address and the count drops. Without
    #  it, "3 files did not move" stayed on Health after the three files moved —
    #  a red card about a problem somebody had already fixed, which is the
    #  fastest way to teach a person that the red cards are furniture.
    rows = conn.execute(
        f"""
        SELECT h.outcome AS outcome, COUNT(*) AS count
        FROM history h
        JOIN items i
          ON i.root = h.src_root AND i.relpath = h.src_relpath
         AND i.missing_since IS NULL
        WHERE h.plan_id IN ({",".join("?" * len(failed))})
          AND h.outcome LIKE 'failed %'
        GROUP BY h.outcome
        """,  # noqa: S608 - placeholders only
        failed,
    ).fetchall()
    by_code: dict[str, tuple[object, int]] = {}
    for row in rows:
        failure = from_outcome(str(row["outcome"]))
        seen = by_code.get(failure.code)
        by_code[failure.code] = (failure, int(row["count"]) + (seen[1] if seen else 0))
    concerns = []
    for failure, count in sorted(by_code.values(), key=lambda pair: -pair[1]):
        concerns.append(
            Concern(
                code=f"commit-failed-{failure.code}",
                #  A failure nobody can act on is still worth knowing, and one
                #  that waits on a person is not the same kind of thing.
                level=ACTION if failure.needs_you else ATTENTION,
                headline=f"{count} file{'' if count == 1 else 's'} did not move",
                detail=f"{failure.what} Their originals are where they were, and "
                       f"nothing was overwritten. {failure.next}",
                href="/commit",
                action="View in Commit",
                count=count,
            )
        )
    return concerns


def _policy(conn: sqlite3.Connection, settings=None, counts=None) -> list[Concern]:  # noqa: ANN001, ARG001
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


def _learned(conn: sqlite3.Connection, settings=None, counts=None) -> list[Concern]:  # noqa: ANN001, ARG001
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


# --- backups, mirrors and the drive in the drawer ------------------------------

#  How many failures in a row stop being bad luck. One is a network hiccup; the
#  third in a row is a thing that is not going to fix itself.
REPEATED = 3


def _transfers(conn: sqlite3.Connection, settings=None, counts=None) -> list[Concern]:  # noqa: ANN001, ARG001
    """What is wrong with a backup, what is worth knowing, and what is normal.

    The three levels do real work here, and the hardest one to get right is the
    third: **a registered drive in a drawer is where a backup drive lives.** It
    is not late, not missing and not unavailable, and colouring it would teach
    somebody that a warning about their backups means nothing. What it gets is
    a line under Information with a date on it.

    What does need a decision is a destination that is configured, switched on,
    and cannot do its job: a NAS that has stopped answering, a run failing over
    and over, rclone gone from a machine that needs it, and — the sharp one — a
    *different drive* mounted where the registered one should be, which is a
    refusal that will keep happening until somebody looks.
    """
    from librairy.transfer_status import destination_views

    if settings is None:  # pragma: no cover - every caller passes settings
        return []
    try:
        views = destination_views(conn, settings)
    except Exception:  # noqa: BLE001 - Health must render without a backup drive
        return []
    if not views:
        return []
    found: list[Concern] = []
    found.extend(_wrong_drives(views))
    found.extend(_failing(conn, views))
    found.extend(_missing_rclone(settings, views))
    found.extend(_interrupted(views))
    found.extend(_disconnected(views))
    found.extend(_only_at_destination(views))
    found.extend(_marker_only(views))
    return found


def _wrong_drives(views: list) -> list[Concern]:
    wrong = [view for view in views if view.wrong_drive]
    if not wrong:
        return []
    return [
        Concern(
            code="backup-wrong-drive",
            level=ACTION,
            headline=(
                f"A different drive is mounted where {_names(wrong)} should be"
            ),
            detail="Nothing has been copied to it, and nothing will be until the "
                   "registered drive is back. This is the check working: a drive "
                   "that carries our marker on a different filesystem is a copy, "
                   "not the one that was registered.",
            examples=tuple(Example(view.name, view.presence_note) for view in wrong[:SHOWN]),
            more=max(0, len(wrong) - SHOWN),
            href="/settings#destinations",
            action="Settings",
            count=len(wrong),
        )
    ]


def _failing(conn: sqlite3.Connection, views: list) -> list[Concern]:
    from librairy import backup_runs

    failing = [view for view in views if view.enabled and view.last_failed]
    if not failing:
        return []
    repeated = [
        view
        for view in failing
        if sum(
            1
            for run in backup_runs.recent(conn, view.destination.id, limit=REPEATED)
            if run.state == backup_runs.FAILED
        )
        >= REPEATED
    ]
    level = ACTION if repeated else ATTENTION
    return [
        Concern(
            code="backup-failing",
            level=level,
            headline=(
                f"{_names(repeated or failing)} "
                f"{'has been failing' if repeated else 'last failed'}"
            ),
            detail=(
                "Three runs in a row have failed, which is no longer bad luck."
                if repeated
                else "One run failed. The next comparison finds whatever is still "
                     "missing and copies it, so this often clears itself."
            ),
            examples=tuple(
                Example(view.name, view.last_attempted_ago) for view in failing[:SHOWN]
            ),
            more=max(0, len(failing) - SHOWN),
            href="/backups",
            action="Backups",
            count=len(failing),
        )
    ]


def _missing_rclone(settings, views: list) -> list[Concern]:  # noqa: ANN001
    """rclone gone from a machine with an enabled remote policy.

    Only when something actually needs it. A library backed up to a drive in a
    drawer does not care whether rclone is installed, and telling somebody
    their backups are broken because a tool they do not use is absent is how a
    Health page gets ignored.
    """
    from librairy.tools import rclone

    wanting = [
        view
        for view in views
        if view.enabled and view.kind != "local" and view.policies
    ]
    if not wanting:
        return []
    status = rclone.rclone_status(settings.appdata_dir / "rclone" / "rclone.conf")
    if status.available:
        return []
    return [
        Concern(
            code="backup-no-rclone",
            level=ACTION,
            headline="rclone is not available, and a remote destination needs it",
            detail=f"{_names(wanting)} cannot be reached until it is installed. "
                   "Local and offline destinations are unaffected.",
            href="/health#tools",
            action="Tools",
            count=len(wanting),
        )
    ]


def _interrupted(views: list) -> list[Concern]:
    left = [view for view in views if view.interrupted]
    if not left:
        return []
    return [
        Concern(
            code="backup-interrupted",
            level=ATTENTION,
            headline=f"A run to {_names(left)} was interrupted",
            detail="The process stopped before the run could record how it ended, "
                   "so the outcome is genuinely unknown rather than assumed. "
                   "Nothing was lost: the next comparison finds whatever is still "
                   "missing and copies it.",
            href="/backups",
            action="Backups",
            count=len(left),
        )
    ]


def _disconnected(views: list) -> list[Concern]:
    """Information, and never anything else.

    A registered drive is *supposed* to be in a drawer most of the time. The
    useful thing to say about one is when it was last here.
    """
    away = [
        view
        for view in views
        if view.offline and view.enabled and not view.wrong_drive
        and view.presence != "present"
    ]
    if not away:
        return []
    return [
        Concern(
            code="backup-drive-away",
            level=INFORMATION,
            headline=(
                f"{len(away)} offline backup drive{'' if len(away) == 1 else 's'} "
                "not connected"
            ),
            detail="Normal. Each one updates when you plug it in.",
            examples=tuple(Example(view.name, view.presence_note) for view in away[:SHOWN]),
            more=max(0, len(away) - SHOWN),
            href="/settings#destinations",
            action="Settings",
            count=len(away),
        )
    ]


def _only_at_destination(views: list) -> list[Concern]:
    """Information that is worth acting on, and never a queue of things to remove."""
    holding = [view for view in views if view.only_here]
    if not holding:
        return []
    total = sum(view.only_here for view in holding)
    return [
        Concern(
            code="backup-only-at-destination",
            level=INFORMATION,
            headline=f"{total:,} files are only at a destination",
            detail="Files your library no longer has, still held where they were "
                   "copied. Nothing removes them and nothing here suggests you "
                   "should — this is what a backup is for.",
            examples=tuple(
                Example(view.name, view.only_here_sentence) for view in holding[:SHOWN]
            ),
            more=max(0, len(holding) - SHOWN),
            href=f"/backups/{holding[0].destination.id}/only-here",
            action="See which",
            count=total,
        )
    ]


def _marker_only(views: list) -> list[Concern]:
    """Reduced verification, said rather than hidden.

    A drive registered with a volume id and later checked where none can be
    read is still allowed — that fallback is deliberate — but it is *less*
    checking than happened at registration, and showing it identically to a
    full check would be hiding a reduction rather than making a decision.
    """
    reduced = [view for view in views if view.reduced]
    if not reduced:
        return []
    return [
        Concern(
            code="backup-marker-only",
            level=INFORMATION,
            headline=f"{_names(reduced)} was identified by its marker file only",
            detail="Your system could not say which filesystem it is, so only "
                   "half the identity check ran. Backups still work; a cloned "
                   "drive would not be caught.",
            href="/settings#destinations",
            action="Settings",
            count=len(reduced),
        )
    ]


def _names(views: list) -> str:
    names = [view.name for view in views]
    if len(names) == 1:
        return names[0]
    if len(names) == 2:  # noqa: PLR2004
        return f"{names[0]} and {names[1]}"
    return f"{names[0]} and {len(names) - 1} others"
