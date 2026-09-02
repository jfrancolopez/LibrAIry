"""Letting the settled decisions wait in front of Commit instead of in Review.

**Off by default.** `review.settled.auto_approve` turns it on, and with it off
nothing here runs and Review behaves exactly as it did — which is the point of
the setting rather than a concession to it. Approving on a person's behalf,
silently, on the first cycle after an upgrade, is not a thing to switch on for
somebody. See `docs/ROADMAP.md` M1-05.

What it does when it is on: a proposal whose *identity* was read off the file
or printed in it — an ISBN, a DOI, a catalog match on this recording — moves
from `proposed` to `approved` without being asked about. It has not moved and
nothing has been decided that Commit cannot show and Undo cannot take back:

    proposed     waiting for somebody to answer
    approved     answered, waiting for Commit          ← this is the change
    committed    the file actually moved               ← still needs a person

**Nothing here touches the safety model.** No filesystem operation happens
without Commit, Commit shows the exact list before it runs, and one press of
Undo puts the whole batch back — which is why the snapshot is taken before
anything changes, exactly as a person's own bulk approval takes one.

Two rules it must never break:

* **A learned habit can never reach here.** It is authority level 4,
  permanently: a statement about files that *resembled* this one. The tier rule
  in `confidence_tiers.py` is what enforces it; this module only asks.
* **What it does is never learned from.** `docs/architecture/decision-memory.md`
  says lessons come from explicit choices that completed, and lists "what the
  classifier produced on its own" among the things that are not lessons. An
  automatic approval that taught the learner would be the program citing itself
  as evidence, and the loop would tighten every cycle. So this deliberately
  does *not* call `remember_approvals`.
"""

from __future__ import annotations

import json
import sqlite3

from librairy.confidence_tiers import SETTLED, identity_of
from librairy.config import Settings
from librairy.lifecycle import transition_item
from librairy.planner import utc_now
from librairy.review_undo import record as record_undo
from librairy.review_undo import snapshot_proposals

SETTING = "review.settled.auto_approve"

#  A ceiling on one cycle, for the same reason the button has one: a batch
#  somebody may want to undo has to be a batch Undo can photograph. Whatever is
#  left is picked up next cycle.
BATCH = 200


def auto_approve_enabled(conn: sqlite3.Connection) -> bool:
    """Off unless the owner said otherwise."""
    row = conn.execute("SELECT value FROM settings WHERE key=?", (SETTING,)).fetchone()
    if row is None:
        return False
    try:
        return bool(json.loads(row["value"]))
    except (TypeError, ValueError):
        return False


def approve_settled(conn: sqlite3.Connection, settings: Settings) -> int:
    """Approve the waiting decisions nothing is in doubt about. Returns how many.

    The tier column narrows it to the rows whose evidence settled the question
    — one indexed read — and then each is checked against the things that were
    not knowable when the proposal was written. Those checks are the reason
    this is not a single `UPDATE`.
    """
    if not auto_approve_enabled(conn):
        return 0
    rows = conn.execute(
        """
        SELECT p.id AS id, p.item_id AS item_id, p.evidence AS evidence
        FROM proposals p
        JOIN items i ON i.id = p.item_id
        WHERE p.status = 'proposed' AND p.tier = ? AND i.missing_since IS NULL
        ORDER BY p.id
        LIMIT ?
        """,
        (SETTLED, BATCH),
    ).fetchall()
    chosen = [row for row in rows if not _in_question(conn, settings, row)]
    if not chosen:
        return 0
    ids = [int(row["id"]) for row in chosen]
    #  Photographed before anything changes, so one press of Undo puts the
    #  whole batch back — which matters more here than anywhere else, because
    #  nobody was watching when it happened.
    record_undo(conn, "approve", snapshot_proposals(conn, ids))
    now = utc_now()
    for row in chosen:
        transition_item(conn, int(row["item_id"]), "approved")
        conn.execute(
            "UPDATE proposals SET status='approved', updated_at=? WHERE id=?",
            (now, int(row["id"])),
        )
    #  Deliberately no `remember_approvals`. See the module docstring: a
    #  program that learns from its own automatic decisions is citing itself.
    return len(chosen)


def _in_question(conn: sqlite3.Connection, settings: Settings, row: sqlite3.Row) -> bool:
    """Is something about this file a different question than where to file it?

    Three of them, and each is a decision wearing a filing's clothes: this is a
    second copy of something already filed, this is another representation of
    something already filed, or a model looked at the picture and disagreed
    about what it is. None is knowable when the proposal is written, and any
    one of them means somebody should look.
    """
    from librairy.arrival_comparison import similar_arrival
    from librairy.classify.images import vision_disagrees, vision_for_items
    from librairy.inbox_duplicates import EVIDENCE_PREFIX

    item_id = int(row["item_id"])
    if EVIDENCE_PREFIX in (row["evidence"] or ""):
        return True
    if similar_arrival(conn, settings, item_id) is not None:
        return True
    looked = vision_for_items(conn, [item_id]).get(item_id)
    category = conn.execute(
        "SELECT category FROM proposals WHERE id=?", (int(row["id"]),)
    ).fetchone()
    return bool(looked and vision_disagrees(looked, category["category"] if category else ""))


def why(evidence: object) -> str:
    """What identified this file. The answer to "why am I here"."""
    return identity_of(evidence)
