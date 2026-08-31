"""After a restore: which of the things LibrAIry believes are still true?

A backup tool's job is to put bytes and a database back. It is not the same job
as making sure they still describe each other, and no amount of teaching the
backup tool about individual tables would make it so — the database and the
filesystem are restored from two snapshots, possibly of two different moments,
possibly onto a machine where somebody has since moved things about.

So the responsibility is split, and this module owns the second half:

    what do I currently have?
        ↓
    which of my persisted facts are still true?
        ↓
    which derived facts can I safely rebuild?
        ↓
    which ambiguities require a person?

**Not all persisted state is the same kind of thing.** Three kinds, and
treating them alike is how a restore either destroys a decision somebody made
or trusts a cache that describes bytes that are gone:

    AUTHORITATIVE   History, Format Policy, Decision Memory, suppressions,
                    withdrawals, committed plan provenance. Nobody can
                    regenerate these — they are a record of what a person
                    chose. They survive a restore untouched, always.

    DERIVED         the search index, the metadata cache, catalog identity,
                    relationship discovery, audit and similarity findings.
                    All rebuildable from the files and the authoritative
                    state. A stale one is an inconvenience, not a loss.

    FINGERPRINT-BOUND  the subset of derived state that was measured from
                    *specific bytes* and records which: `item_metadata`,
                    `track_identity`, `content_extractions`, `vision_results`.
                    These are the dangerous ones. Silently attaching last
                    month's identity to this month's bytes is worse than
                    having no identity at all, so they are checked against the
                    fingerprint they were measured from and reported stale
                    when it no longer matches.

**Validation writes nothing and moves nothing.** It is a read of the index
against itself and against a walk of the filesystem. Reconciliation — deciding
that a file found elsewhere really is the one the index lost — is a separate,
explicit act, in `librairy/reconcile.py`.

**A path mismatch is not data loss.** If the index expects
`Music/Queen/Album/song.mp3` and the exact same bytes are sitting at
`Music/Album/song.mp3`, nothing has been lost; the file has moved. Reporting
that as missing is how a restore looks like a catastrophe when it was a
success. Fingerprint identity is what tells the two apart, and it is the only
thing allowed to: a matching filename, a matching size or a similar title
prove nothing.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from librairy.config import Settings
from librairy.live import dormant_optimization_result

#  How many named examples any one finding carries. The owning page lists the
#  rest; a validation report that prints ten thousand paths is a directory
#  listing wearing a verdict.
SHOWN = 5

#  What can be true of one indexed file after a restore.
MATCHED = "matched"
MOVED = "moved"
AMBIGUOUS = "ambiguous"
MISSING = "missing"
CHANGED = "changed"

#  How much a finding matters. The same three levels Health uses, because a
#  person reading both should not have to learn two vocabularies.
BLOCKING = "blocking"
ATTENTION = "attention"
REBUILDABLE = "rebuildable"
SETTLED = "settled"


@dataclass(frozen=True)
class Finding:
    """One thing the validation has to say about the restored state."""

    code: str
    level: str
    count: int
    headline: str
    detail: str = ""
    examples: tuple[str, ...] = ()
    more: int = 0


@dataclass(frozen=True)
class Report:
    """Everything the validation found, and what it could not see."""

    findings: tuple[Finding, ...] = ()
    #  Counts of files by what the index can currently prove about them.
    counts: dict[str, int] = field(default_factory=dict)
    #  True when the roots have not been walked since whatever happened. The
    #  answers below are still true about the index; they are not yet true
    #  about the disk.
    scan_needed: bool = False
    unscanned: int = 0

    def at(self, level: str) -> list[Finding]:
        return [item for item in self.findings if item.level == level]

    @property
    def blocking(self) -> list[Finding]:
        return self.at(BLOCKING)

    @property
    def attention(self) -> list[Finding]:
        return self.at(ATTENTION)

    @property
    def rebuildable(self) -> list[Finding]:
        return self.at(REBUILDABLE)

    @property
    def settled(self) -> bool:
        return not self.blocking and not self.attention and not self.rebuildable


def validate(conn: sqlite3.Connection, settings: Settings | None = None) -> Report:
    """Read the restored state and say what still holds. Writes nothing.

    Index-first on purpose. Hashing the whole library on demand would take
    hours on the libraries this matters most for, and the scanner already
    re-hashes anything whose size or modification time moved — so the honest
    order is *scan, then validate*, and the report says so when the two
    disagree about which files even exist.
    """
    findings: list[Finding] = []
    counts = _identity_counts(conn)
    unscanned = _unscanned(conn, settings)
    findings.extend(_identity_findings(conn, counts))
    findings.extend(_stale_measurements(conn))
    findings.extend(_search(conn))
    findings.extend(_quarantine(conn))
    findings.extend(_pending(conn))
    findings.extend(_preserved(conn))
    order = {BLOCKING: 0, ATTENTION: 1, REBUILDABLE: 2, SETTLED: 3}
    findings.sort(key=lambda item: order.get(item.level, 4))
    return Report(
        findings=tuple(findings),
        counts=counts,
        scan_needed=bool(unscanned),
        unscanned=unscanned,
    )


# --- where the files went -----------------------------------------------------

#  Every indexed file the index has lost, paired with live rows holding exactly
#  its bytes.
#
#  Fingerprint and nothing else. A filename match, a size match or a similar
#  title prove nothing at all, and a reconciliation built on any of them would
#  attach one file's history to another file. `idx_items_fingerprint` makes
#  this an index lookup per lost row rather than a comparison against every
#  other row, which is the difference between a report and an afternoon.
#
#  Same root, deliberately. A library file whose bytes turn up in the inbox has
#  not moved — that is an arrival that happens to be a copy, and the duplicate
#  workflow is where it belongs.
_LOST = f"""
  SELECT gone.id AS gone_id, gone.root AS root, gone.relpath AS gone_relpath,
         gone.fingerprint AS fingerprint,
         (SELECT COUNT(*) FROM items found
           WHERE found.fingerprint = gone.fingerprint
             AND found.missing_since IS NULL
             AND found.root = gone.root
             AND found.id <> gone.id) AS candidates
  FROM items gone
  WHERE gone.missing_since IS NOT NULL
    AND gone.fingerprint IS NOT NULL AND gone.fingerprint <> ''
    AND NOT ({dormant_optimization_result("gone")})
"""


def _identity_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """How many indexed files are matched, moved, ambiguous or gone."""
    rows = conn.execute(
        f"""
        SELECT CASE WHEN candidates = 0 THEN 'missing'
                    WHEN candidates = 1 THEN 'moved'
                    ELSE 'ambiguous' END AS state,
               COUNT(*) AS files
          FROM ({_LOST}) GROUP BY state
        """  # noqa: S608 - `_LOST` is a module constant
    ).fetchall()
    counts = {str(row["state"]): int(row["files"]) for row in rows}
    counts[MATCHED] = int(
        conn.execute(
            "SELECT COUNT(*) FROM items WHERE missing_since IS NULL"
        ).fetchone()[0]
    )
    counts[CHANGED] = _changed(conn)
    return counts


def _changed(conn: sqlite3.Connection) -> int:
    """Files still at their path whose bytes are not the ones we measured.

    Read from the measurement cache rather than by hashing: every row in
    `item_metadata` records the fingerprint it was read from, so a row whose
    fingerprint no longer matches its item is proof the bytes changed under it.
    """
    return int(
        conn.execute(
            """
            SELECT COUNT(DISTINCT m.item_id) FROM item_metadata m
              JOIN items i ON i.id = m.item_id
             WHERE i.missing_since IS NULL AND i.fingerprint IS NOT NULL
               AND m.fingerprint <> i.fingerprint
            """
        ).fetchone()[0]
    )


def _identity_findings(
    conn: sqlite3.Connection, counts: dict[str, int]
) -> list[Finding]:
    findings: list[Finding] = []
    if counts.get(MOVED):
        found = counts[MOVED]
        findings.append(
            Finding(
                code="moved",
                level=ATTENTION,
                count=found,
                headline=(
                    f"{found} file{'' if found == 1 else 's'} "
                    f"{'is' if found == 1 else 'are'} somewhere else"
                ),
                detail=(
                    "The exact same bytes are on disk at a different path. "
                    "Nothing has been lost — LibrAIry's record of where they "
                    "live is out of date, and recognising the new location "
                    "moves no files."
                ),
                examples=_examples(conn, 1),
                more=max(0, found - SHOWN),
            )
        )
    if counts.get(AMBIGUOUS):
        found = counts[AMBIGUOUS]
        findings.append(
            Finding(
                code="ambiguous",
                level=ATTENTION,
                count=found,
                headline=(
                    f"{found} file{'' if found == 1 else 's'} could be in more "
                    f"than one place"
                ),
                detail=(
                    "Identical bytes exist at several paths, so which one is "
                    "the file LibrAIry lost cannot be established from the "
                    "bytes alone. These are left for you to decide."
                ),
                examples=_examples(conn, 2),
                more=max(0, found - SHOWN),
            )
        )
    if counts.get(MISSING):
        found = counts[MISSING]
        findings.append(
            Finding(
                code="missing",
                level=BLOCKING,
                count=found,
                headline=(
                    f"{found} indexed file{'' if found == 1 else 's'} "
                    f"{'is' if found == 1 else 'are'} not on disk anywhere"
                ),
                detail=(
                    "Nothing on disk holds these bytes. Their records are kept "
                    "— an unmounted share looks exactly like this, and "
                    "everything comes back the moment it returns."
                ),
                examples=_examples(conn, 0),
                more=max(0, found - SHOWN),
            )
        )
    return findings


def _examples(conn: sqlite3.Connection, candidates: int) -> tuple[str, ...]:
    """A few of the paths behind one count, bounded."""
    comparison = ">= 2" if candidates > 1 else f"= {int(candidates)}"
    rows = conn.execute(
        f"SELECT root, gone_relpath FROM ({_LOST})"  # noqa: S608 - `_LOST` is a constant
        f" WHERE candidates {comparison} ORDER BY gone_relpath LIMIT ?",
        (SHOWN,),
    ).fetchall()
    return tuple(f"{row['root']}/{row['gone_relpath']}" for row in rows)


def _unscanned(conn: sqlite3.Connection, settings: Settings | None) -> int:
    """Files on disk the index has never seen. Means: scan before believing this.

    Uses the walk Browse already does and no hashing. After a restore this is
    usually the first thing that is true, and every other answer in the report
    is provisional until it is zero.
    """
    if settings is None:
        return 0
    from librairy.consistency import library_consistency

    return library_consistency(conn, settings).unindexed_files


# --- measurements taken from bytes that have since changed --------------------

#  Every cache that records which bytes it was read from, and the column that
#  says so. These are the tables a restore can make dangerous rather than
#  merely stale: the row is *about* a specific set of bytes, and the bytes at
#  that path may now be different ones.
FINGERPRINT_BOUND = (
    ("item_metadata", "item_id", "fingerprint", "measured metadata"),
    ("track_identity", "item_id", "fingerprint", "recording identity"),
    ("content_extractions", "item_id", "fingerprint", "extracted text"),
    ("vision_results", "item_id", "fingerprint", "what a model saw"),
)


def _stale_measurements(conn: sqlite3.Connection) -> list[Finding]:
    """Cached facts whose bytes have moved on.

    Reported, never silently reused and never silently deleted. Every reader of
    these tables already gates on the fingerprint matching — that is what makes
    a stale row a miss rather than a wrong answer — so this is telling somebody
    what a rebuild would recover, not warning them about a live risk.
    """
    stale: list[tuple[str, int]] = []
    for table, key, column, label in FINGERPRINT_BOUND:
        found = int(
            conn.execute(
                f"""
                SELECT COUNT(*) FROM {table} c JOIN items i ON i.id = c.{key}
                 WHERE i.fingerprint IS NOT NULL AND c.{column} <> ''
                   AND c.{column} <> i.fingerprint
                """  # noqa: S608 - table and column come from a module constant
            ).fetchone()[0]
        )
        if found:
            stale.append((label, found))
    if not stale:
        return []
    total = sum(count for _, count in stale)
    return [
        Finding(
            code="stale-measurements",
            level=REBUILDABLE,
            count=total,
            headline=(
                f"{total} cached measurement{'' if total == 1 else 's'} "
                f"describe{'s' if total == 1 else ''} bytes that have changed"
            ),
            detail=(
                "None of it is being used: every reader checks the fingerprint "
                "first, so a measurement of bytes that are gone is a miss "
                "rather than a wrong answer. Re-measuring recovers it."
            ),
            examples=tuple(f"{count} × {label}" for label, count in stale),
        )
    ]


# --- the derived index --------------------------------------------------------


def _search(conn: sqlite3.Connection) -> list[Finding]:
    """The search index is rebuildable, and a mismatch is not corruption."""
    from librairy.search_health import recorded_health, unindexed

    findings: list[Finding] = []
    missing = unindexed(conn)
    if missing:
        findings.append(
            Finding(
                code="search-stale",
                level=REBUILDABLE,
                count=missing,
                headline=(
                    f"{missing} file{'' if missing == 1 else 's'} "
                    f"{'is' if missing == 1 else 'are'} not in the search index"
                ),
                detail="Rebuilt from the index and the files. Nothing is asked "
                "of any catalog or provider to do it.",
            )
        )
    health = recorded_health(conn)
    if not health.ok:
        findings.append(
            Finding(
                code="search-damaged",
                level=REBUILDABLE,
                count=0,
                headline="The search index needs rebuilding",
                detail="Browse is unaffected — it walks the filesystem.",
            )
        )
    return findings


# --- held files ---------------------------------------------------------------


def _quarantine(conn: sqlite3.Connection) -> list[Finding]:
    """Whether the files quarantine says it is holding are the ones it holds."""
    row = conn.execute(
        """
        SELECT
          SUM(CASE WHEN i.missing_since IS NOT NULL THEN 1 ELSE 0 END) AS gone,
          SUM(CASE WHEN i.missing_since IS NULL AND o.src_fingerprint IS NOT NULL
                    AND i.fingerprint IS NOT NULL
                    AND o.src_fingerprint <> i.fingerprint THEN 1 ELSE 0 END) AS changed
        FROM quarantine_entries qe
        JOIN items i ON i.id = qe.item_id
        LEFT JOIN plan_ops o ON o.id = (
          SELECT p.id FROM plan_ops p
           WHERE p.plan_id = qe.plan_id AND p.item_id = qe.item_id
           ORDER BY p.seq LIMIT 1
        )
        WHERE qe.restored_at IS NULL
        """
    ).fetchone()
    gone = int((row["gone"] if row else 0) or 0)
    changed = int((row["changed"] if row else 0) or 0)
    if not gone and not changed:
        return []
    parts = []
    if gone:
        parts.append(f"{gone} not on disk")
    if changed:
        parts.append(f"{changed} holding different bytes")
    total = gone + changed
    return [
        Finding(
            code="quarantine",
            level=BLOCKING,
            count=total,
            headline=(
                f"{total} held file{'' if total == 1 else 's'} "
                f"{'is' if total == 1 else 'are'} not what quarantine recorded"
            ),
            detail=(
                ", ".join(parts).capitalize()
                + ". Restoring one of these would not put back what the "
                "decision was about, so it is not offered."
            ),
        )
    ]


# --- decisions that had not run yet -------------------------------------------


def _pending(conn: sqlite3.Connection) -> list[Finding]:
    """Approvals that were waiting when the snapshot was taken.

    Never executed and never cancelled by a restore. Both would be the program
    deciding something on somebody's behalf about files it has just been told
    it may not understand. They are classified and left alone; Commit is where
    they are answered.
    """
    from librairy.attention import _stale_approvals
    from librairy.plan_conflicts import count as conflicts

    waiting = int(
        conn.execute(
            "SELECT COUNT(*) FROM plans WHERE status='approved'"
        ).fetchone()[0]
    )
    if not waiting:
        return []
    outdated = sum(item.count for item in _stale_approvals(conn))
    conflicting = conflicts(conn)
    detail = "Nothing was executed and nothing was cancelled."
    if outdated:
        detail = (
            f"{outdated} of them no longer describe what would happen. "
            f"{detail}"
        )
    if conflicting:
        detail = f"{detail} {conflicting} are in conflict with each other."
    return [
        Finding(
            code="pending-plans",
            level=ATTENTION if (outdated or conflicting) else SETTLED,
            count=waiting,
            headline=(
                f"{waiting} decision{'' if waiting == 1 else 's'} "
                f"{'was' if waiting == 1 else 'were'} waiting for Commit"
            ),
            detail=detail,
        )
    ]


# --- what a restore must never touch ------------------------------------------

#  The state nobody can regenerate. Counted and shown so that a restore can be
#  seen to have kept it, which is the reassurance somebody actually wants after
#  putting a database back.
AUTHORITATIVE = (
    ("history", "committed operations"),
    ("format_policy_scopes", "Format Policy scopes"),
    ("decision_events", "remembered decisions"),
    ("decision_suppressions", "suppressed suggestions"),
    ("plan_withdrawals", "withdrawn decisions"),
    ("reconciliations", "recognised moves"),
)


def preserved(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    """What survived, by name. Never rebuilt, never cleared by a rebuild."""
    return [
        (
            label,
            int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]),  # noqa: S608
        )
        for table, label in AUTHORITATIVE
    ]


def _preserved(conn: sqlite3.Connection) -> list[Finding]:
    rows = [(label, count) for label, count in preserved(conn) if count]
    if not rows:
        return []
    total = sum(count for _, count in rows)
    return [
        Finding(
            code="preserved",
            level=SETTLED,
            count=total,
            headline="Your decisions are untouched",
            detail=(
                "None of this is derived from the files, so nothing a rebuild "
                "does can remove it. A stale index is not the same thing as a "
                "decision you never made."
            ),
            examples=tuple(f"{count} {label}" for label, count in rows),
        )
    ]
