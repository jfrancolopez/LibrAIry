"""What a person can actually do with one Library Review row.

This exists because actionability used to be *inferred* rather than stated.
The template asked `finding.executable` in three places and drew a button in
one of them; the checkbox beside every row was always enabled; and the toolbar
disabled its own button when nothing eligible was selected. Put together, a
folder-naming observation looked exactly like a correction right up to the
moment you pressed a button that could not be pressed — and pressing a disabled
button produces no request, no message and no change. "I approved it and
nothing happened" was a completely accurate description of the software.

So the row now carries one value that says what it is, and every control on it
is derived from that value:

    READY           a correction LibrAIry can execute, right now
    BLOCKED         a correction in principle, refused today, and why
    CHOICE          something can be done, and only the owner can say which
    OBSERVATION     nothing to execute — no move answers this
    NEEDS_ANALYSIS  the file changed after the audit
    NOT_ON_DISK     the file is gone
    WAITING         approved, waiting for Commit
    OUTDATED        approved, but the file changed since — cannot be committed
    APPLYING        the approved plan is running now
    CORRECTED       approved and executed
    DISMISSED       the owner decided against it; reversible

Only READY is approvable. That is asserted here, once, rather than being a
property of which template branch happened to render — the same request can
arrive from a page left open since yesterday, from a second tab, or from curl,
and `accept_correction` refuses all three. This module is what stops the UI
from *offering* it.
"""

from __future__ import annotations

import sqlite3

from librairy.corrections import MISSING, STALE

READY = "ready"
BLOCKED = "blocked"
CHOICE = "choice"
OBSERVATION = "observation"
NEEDS_ANALYSIS = "needs-analysis"
NOT_ON_DISK = "not-on-disk"
WAITING = "waiting"
OUTDATED = "approval-outdated"
APPLYING = "applying"
CORRECTED = "corrected"
DISMISSED = "dismissed"

# The single source of "may this be approved". One member, and `CHOICE` is the
# reason the set exists rather than an equality test: it is the first state that
# has a real action and still must not be approvable. Two byte-identical files
# have no measurable difference to choose between, so "approve all confident"
# has no answer to "which one" — the row carries its own controls and bulk
# cannot reach it.
APPROVABLE = frozenset({READY})

# The chip on the row. Never the stored status value: `open`, `accepted` and
# `kept` are database states, and "Waiting for Commit" is what a person is
# actually looking at.
LABEL = {
    READY: "Ready to approve",
    BLOCKED: "Cannot be applied",
    CHOICE: "Your choice",
    OBSERVATION: "Observation",
    NEEDS_ANALYSIS: "Needs analysis again",
    NOT_ON_DISK: "Not on disk",
    WAITING: "Waiting for Commit",
    OUTDATED: "Approval is outdated",
    APPLYING: "Applying",
    CORRECTED: "Corrected",
    DISMISSED: "Dismissed",
}

# Said on the row itself, not hidden behind the absence of a button. A row that
# simply lacks an Approve control leaves the reader to work out whether that is
# a rule or a bug.
EXPLANATION = {
    READY: "",
    BLOCKED: "",
    CHOICE: "Both files are identical, so only you can say which one to keep.",
    OBSERVATION: "No automatic correction is available for this.",
    NEEDS_ANALYSIS: "The file changed after this was found, so the suggestion "
    "no longer describes it.",
    NOT_ON_DISK: "The file is no longer where it was found.",
    WAITING: "Approved. Nothing has moved yet.",
    OUTDATED: "The file changed after you approved this correction, so it can "
    "no longer be applied as approved.",
    APPLYING: "This correction is running now.",
    CORRECTED: "This was approved and applied.",
    DISMISSED: "You decided against this. It can be restored.",
}

# How a bulk result names each outcome, in the order a summary reads best:
# what happened first, then what did not, then why not.
BULK_ORDER = (READY, WAITING, OUTDATED, APPLYING, CORRECTED, DISMISSED, BLOCKED,
              NEEDS_ANALYSIS, NOT_ON_DISK, CHOICE, OBSERVATION)


def actionability(
    row: sqlite3.Row,
    state: str,
    *,
    executable: bool,
    blocked: str = "",
    plan: object | None = None,
    choices: bool = False,
) -> str:
    """The one value every control on the row is derived from.

    Order matters, three times.

    **An active plan outranks everything, including the row's own status.** If
    an approved, unexecuted plan names this finding then approval has already
    happened, whatever `audit_findings.status` says — and on the live database
    it said `open`, which is how a correction that was already approved came to
    render an Approve button that would have built a second plan. The reasoning
    is in `librairy/correction_state.py`; the consequence is here, first,
    because every later branch can offer approval and this one never can.

    Then a decision the owner already made outranks anything the filesystem has
    to say: telling someone their dismissed suggestion "needs analysis again"
    invites them to re-open a question they closed.

    And what a finding *is* outranks what has happened to the file since. A
    kind that can never produce a move is an observation whether or not its
    folder is still there — "not on disk" would suggest that finding the file
    again would make it approvable, and it would not. The row still says the
    file is gone; it just does not say it instead of the truth.
    """
    if plan is not None:
        if plan.applying:
            return APPLYING
        return OUTDATED if plan.stale else WAITING
    status = row["status"]
    if status == "accepted":
        return WAITING
    if status == "corrected":
        return CORRECTED
    if status == "kept":
        return DISMISSED
    if choices:
        # Before `_corrigible`, and deliberately: a duplicate is not an
        # executable *kind* and never will be, because the thing to do about it
        # is a quarantine rather than a move — and it still has something a
        # person can do. Calling it an observation would be the old lie in a
        # new place.
        return CHOICE
    if not _corrigible(row):
        return OBSERVATION
    if state == MISSING:
        return NOT_ON_DISK
    if blocked:
        return BLOCKED
    if executable:
        return READY
    # Staleness is only worth reporting where it changes the answer. "Needs
    # analysis again" on an observation that is perfectly accurate is a warning
    # about nothing, followed by a button that re-finds the same thing.
    if state == STALE:
        return NEEDS_ANALYSIS
    return OBSERVATION


def _corrigible(row: sqlite3.Row) -> bool:
    """Could this kind of finding ever produce a move, staleness aside?"""
    from librairy.audit import EXECUTABLE_KINDS

    return row["kind"] in EXECUTABLE_KINDS and bool(row["dest_relpath"])


def can_approve(value: str) -> bool:
    return value in APPROVABLE


def summarize(counts: dict[str, int], selected: int) -> str:
    """What a bulk action actually did, per outcome.

    "2 item(s) updated" is the sentence this replaces. It is true and useless:
    on a page that moves files somebody already owns, the two that were skipped
    are the interesting half.
    """
    lines = [f"Selected: {selected}"]
    for key in BULK_ORDER:
        count = counts.get(key, 0)
        if count:
            lines.append(f"{OUTCOME_TEXT[key]}: {count}")
    return " · ".join(lines)


# What a row *became*, or the reason it could not. Phrased as results rather
# than states, because this sentence is read straight after pressing a button.
OUTCOME_TEXT = {
    READY: "Approved",
    WAITING: "Already waiting for Commit",
    OUTDATED: "Approved earlier, now outdated",
    APPLYING: "Already running",
    CORRECTED: "Already applied",
    DISMISSED: "Dismissed",
    BLOCKED: "Cannot be applied",
    CHOICE: "Waiting for you to choose",
    NEEDS_ANALYSIS: "Changed since the audit",
    NOT_ON_DISK: "No longer on disk",
    OBSERVATION: "Observation only",
}
