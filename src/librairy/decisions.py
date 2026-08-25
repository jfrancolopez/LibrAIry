"""What the owner keeps choosing, and what LibrAIry may do about it.

Review is where a person teaches this program things. Until now it forgot every
lesson the moment the file moved: file the fourth Honda manual under
`Documents/Manuals/Honda Motor Co/` and the fifth arrives knowing nothing.

So repeated decisions are remembered. The rule is narrow on purpose:

    learn what was explicitly chosen and actually completed
    → recognise the same cues later
    → *suggest* the answer, with the evidence for it
    → the person confirms or overrides

**It accelerates Review. It does not bypass Review.** Nothing here approves
anything, creates a plan, or moves a file. A suggestion is a sentence and a
button, and the button leads to the ordinary edit the person could have made
themselves.

Four authorities, in order, and a learned pattern is the weakest of them:

    1. safety invariants     never overwrite, never delete, revalidate hashes
    2. explicit user policy  `music.preferred_format = mp3`
    3. strong current evidence   a catalog identity, an ISBN, a DOI
    4. learned suggestion    "you filed six Honda manuals here"

A learned pattern may never override 1 or 2, and must stay quiet when 3 has a
better answer about *this* file. Six Queen tracks filed under one release is a
habit; MusicBrainz saying this recording is from another release is a fact.

Nothing about this is a model. There is no training, no embedding, no score,
and no network: it is a count of decisions that completed, grouped by the cues
they were made under, and a threshold. That is also why every suggestion can
say exactly why it is there.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field

from librairy.planner import utc_now

#  What sort of question was answered. Kept apart because the answers are not
#  comparable: a destination and "keep both of these" are not two values of one
#  thing, and averaging them would be the beginning of a score.
DESTINATION = "destination"
REPRESENTATION = "representation"
ALLOWED = "allowed"

KINDS = (DESTINATION, REPRESENTATION, ALLOWED)

#  How many completed decisions make a pattern.
#
#  One is a precedent. Two is a coincidence. Three is the first number at which
#  "you keep doing this" is a fair thing to say to somebody — and getting it
#  wrong in the generous direction is how a program starts confidently
#  repeating an accident.
MIN_SUPPORT = 3

#  When a history is too divided to have a usual answer.
#
#  A suggestion needs **more than twice as many confirmations as departures**.
#  Five against four is not a preference, it is two habits, and "you usually
#  choose A" would be a sentence the evidence does not support. Stated as a
#  ratio rather than a percentage because it is a count of real decisions and
#  should read like one.
def _dominant(top: int, others: int) -> bool:
    return top >= MIN_SUPPORT and top > 2 * others


@dataclass(frozen=True)
class Cue:
    """The cues a decision was made under, in the order of narrowing."""

    kind: str
    features: dict[str, str] = field(default_factory=dict)

    @property
    def signature(self) -> str:
        """The canonical form, which is what a lookup matches on."""
        parts = "&".join(
            f"{name}={_normal(value)}"
            for name, value in sorted(self.features.items())
            if _normal(value)
        )
        return f"{self.kind}|{parts}"

    @property
    def specificity(self) -> int:
        """How many cues had to agree. The whole of "narrower wins"."""
        return sum(1 for value in self.features.values() if _normal(value))

    @property
    def described(self) -> str:
        """The cues in words, for the sentence under a suggestion."""
        return ", ".join(
            f"{name.replace('_', ' ')} {value}"
            for name, value in sorted(self.features.items())
            if _normal(value)
        )


@dataclass(frozen=True)
class Suggestion:
    """A learned answer, and the evidence for it."""

    kind: str
    signature: str
    outcome: str
    support: int
    contradictions: int
    cue: Cue
    #  The outcome with this file's own values in it. Empty when the template
    #  needs a cue this file does not have, which is the same as having no
    #  suggestion — see `render`.
    rendered: str = ""

    @property
    def explanation(self) -> str:
        """Why this is being suggested, in decisions rather than in a score."""
        times = "decision" if self.support == 1 else "decisions"
        note = f"Suggested from {self.support} previous {times}"
        if self.cue.described:
            note = f"{note} about {self.cue.described}"
        if self.contradictions:
            other = "time" if self.contradictions == 1 else "times"
            note = f"{note}; you chose differently {self.contradictions} {other}"
        #  The cue values are somebody else's punctuation — `Honda Motor Co.`
        #  already ends in a stop, and the sentence adding another produced
        #  "Honda Motor Co..".
        return note if note.endswith(".") else f"{note}."


def record(
    conn: sqlite3.Connection,
    *,
    cue: Cue,
    outcome: str,
    item_id: int | None = None,
    plan_id: str | None = None,
    dest_relpath: str | None = None,
    settled: bool = False,
) -> int:
    """Write down one explicit decision, as it was made.

    Written at the moment of the choice, because that is the only moment the
    cues are known: re-deriving them later would classify the file with today's
    rules rather than the ones the person was looking at.

    `settled` is for the decisions that have no plan because the answer was to
    leave everything alone — keeping both formats of a book, keeping every
    photograph in a group. Those complete when they are made. Everything else
    completes when a file actually moves, which is stamped by `settle`.
    """
    if cue.kind not in KINDS:
        raise ValueError(f"unknown decision kind: {cue.kind}")
    if not outcome:
        raise ValueError("a decision with no outcome is not a decision")
    now = utc_now()
    cursor = conn.execute(
        """
        INSERT INTO decision_events
          (kind, signature, specificity, features, outcome, item_id, plan_id,
           dest_relpath, decided_at, settled_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            cue.kind,
            cue.signature,
            cue.specificity,
            json.dumps(cue.features, sort_keys=True),
            outcome,
            item_id,
            plan_id,
            dest_relpath,
            now,
            now if settled else None,
        ),
    )
    return int(cursor.lastrowid)


def settle_plan(conn: sqlite3.Connection, plan_id: str) -> None:
    """A decision recorded against a whole plan completes when the plan does.

    The comparison shape: "keep the MP3" is one answer about several files, so
    it is attached to the plan rather than to one of them. A plan that failed
    or was refused teaches nothing — the person's choice never happened.
    """
    plan = conn.execute(
        "SELECT status FROM plans WHERE id=?", (plan_id,)
    ).fetchone()
    if plan is None or plan["status"] != "done":
        return
    conn.execute(
        "UPDATE decision_events SET settled_at=? WHERE plan_id=? AND settled_at IS NULL",
        (utc_now(), plan_id),
    )


def settle(conn: sqlite3.Connection, item_id: int | None, plan_id: str) -> None:
    """The file moved, so the decision about it actually happened.

    Called from the executor, at the same point the proposal is marked
    committed — after the bytes are copied and verified, per operation. A plan
    that half ran teaches only the half that ran.

    Whether it was later *undone* is not stored. That is in the journal, under
    this plan id, and reading it there is what keeps a reversal from needing a
    second record that could disagree with the first.
    """
    if item_id is None:
        return
    conn.execute(
        """
        UPDATE decision_events SET plan_id=?, settled_at=?
        WHERE item_id=? AND settled_at IS NULL AND plan_id IS NULL
        """,
        (plan_id, utc_now(), int(item_id)),
    )


#  A decision counts when it completed and was not put back.
#
#  Both halves are read from the journal rather than stored: `undo_move` and
#  `undo_quarantine` rows carry the destination the file was taken *back* from,
#  which is the destination this decision chose. Choosing A, committing,
#  undoing and then choosing B must not leave A remembered as something that
#  worked.
_COMPLETED = """
  e.settled_at IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM history h
    WHERE h.plan_id = e.plan_id AND h.outcome = 'ok'
      AND h.action IN ('undo_move', 'undo_quarantine')
      AND (e.dest_relpath IS NULL OR h.src_relpath = e.dest_relpath)
  )
"""


def tally(
    conn: sqlite3.Connection, signatures: list[str]
) -> dict[str, dict[str, int]]:
    """How many completed, un-reversed decisions each signature has, by outcome.

    One query for a whole page of Review, never one per row: a history scan per
    row is the shape that stops working at fifty rows, let alone five hundred.
    Suppressed patterns are left out here rather than filtered afterwards, so a
    suppressed signature costs nothing to have.
    """
    if not signatures:
        return {}
    placeholders = ",".join("?" * len(signatures))
    rows = conn.execute(
        f"""
        SELECT e.signature, e.outcome, COUNT(*) AS n
        FROM decision_events e
        WHERE e.signature IN ({placeholders})
          AND {_COMPLETED}
          AND NOT EXISTS (
            SELECT 1 FROM decision_suppressions s WHERE s.signature = e.signature
          )
        GROUP BY e.signature, e.outcome
        """,  # noqa: S608 - placeholders are counted; the rest are constants
        signatures,
    ).fetchall()
    found: dict[str, dict[str, int]] = defaultdict(dict)
    for row in rows:
        found[str(row["signature"])][str(row["outcome"])] = int(row["n"])
    return dict(found)


def suggest(
    conn: sqlite3.Connection, cues: list[Cue], *, counts: dict | None = None
) -> Suggestion | None:
    """The narrowest cue with a settled habit behind it, or None.

    `cues` come in whatever order the caller likes; this sorts them so the most
    specific is asked first. Honda manuals beat manuals, and manuals beat
    documents — decided by how many cues had to agree and by nothing else,
    which is why there is no score to explain.

    A divided history produces no suggestion at all rather than a majority.
    Five against four is two habits, and saying "you usually choose A" about it
    would be a claim the decisions do not support.
    """
    ordered = sorted(cues, key=lambda cue: -cue.specificity)
    tallies = counts if counts is not None else tally(
        conn, [cue.signature for cue in ordered]
    )
    for cue in ordered:
        outcomes = tallies.get(cue.signature) or {}
        if not outcomes:
            continue
        outcome, top = max(outcomes.items(), key=lambda pair: (pair[1], pair[0]))
        others = sum(count for name, count in outcomes.items() if name != outcome)
        if not _dominant(top, others):
            #  A real split at this specificity. Falling through to a broader
            #  cue would answer a narrower question with a vaguer habit, which
            #  is worse than saying nothing.
            return None
        return Suggestion(
            kind=cue.kind,
            signature=cue.signature,
            outcome=outcome,
            support=top,
            contradictions=others,
            cue=cue,
        )
    return None


def formats_cue(category: str, formats: list[str]) -> Cue | None:
    """The cue for "which of these representations do you keep".

    None unless the members genuinely differ in *format*. Eighteen JPEGs set
    aside from a burst of twenty-five is a decision about photographs, not
    about JPEG, and recording it would fill decision memory with a pattern
    whose answer is always the format it started with.
    """
    kinds = sorted({fmt.lower().lstrip(".") for fmt in formats if fmt})
    if len(kinds) < 2:
        return None
    return Cue(REPRESENTATION, {"category": category, "formats": "+".join(kinds)})


def record_representation(
    conn: sqlite3.Connection,
    *,
    category: str,
    formats: list[str],
    kept: list[str],
    plan_id: str | None = None,
    settled: bool = False,
) -> None:
    """Remember which format survived a comparison, when format was the question.

    Recorded for both shapes of answer: setting the others aside, and keeping
    everything — which is a real decision ("I want both of these") and is
    stored as its own kind so it can never be mistaken for a preference for one
    of them.
    """
    cue = formats_cue(category, formats)
    if cue is None:
        return
    survivors = sorted({fmt.lower().lstrip(".") for fmt in kept if fmt})
    if not survivors:
        return
    outcome = "+".join(survivors)
    keeping_all = len(survivors) == len(cue.features["formats"].split("+"))
    record(
        conn,
        cue=Cue(ALLOWED if keeping_all else REPRESENTATION, dict(cue.features)),
        outcome=outcome,
        plan_id=plan_id,
        settled=settled or keeping_all,
    )


def suppress(conn: sqlite3.Connection, signature: str) -> None:
    """"Stop offering me this."

    The decisions behind it are still what happened and still appear in
    History. This turns off the conclusion, not the record — deleting the
    events would rewrite the past to change a suggestion.
    """
    conn.execute(
        "INSERT OR IGNORE INTO decision_suppressions(signature, created_at)"
        " VALUES (?, ?)",
        (signature, utc_now()),
    )


def suppressed(conn: sqlite3.Connection, signature: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM decision_suppressions WHERE signature=?", (signature,)
        ).fetchone()
        is not None
    )


def restore(conn: sqlite3.Connection, signature: str) -> None:
    """Undo a suppression. The pattern is offered again from its old evidence."""
    conn.execute("DELETE FROM decision_suppressions WHERE signature=?", (signature,))


def generalize(destination: str, features: dict[str, str]) -> str:
    """A destination with this file's own values put back as placeholders.

    `Documents/Financial/2024` learned from four 2024 statements would file a
    2026 statement under 2024 — the literal is a fact about those four files,
    not about the policy that produced them. Substituting the cue values back
    keeps what the person chose (`Documents/Financial/{year}`) and drops what
    was only true that time.

    Longest value first, so a folder that happens to contain a short cue value
    inside a longer one is not half-replaced.
    """
    result = destination
    for name, value in sorted(
        features.items(), key=lambda pair: -len(str(pair[1] or ""))
    ):
        text = str(value or "")
        if len(text) >= 2 and text in result:
            result = result.replace(text, "{" + name + "}")
    return result


def render(template: str, features: dict[str, str]) -> str:
    """The template with *this* file's values in it, or "" if it cannot be.

    A pattern that needs a year this document does not carry has no answer for
    it, and an unfilled placeholder in a path is worse than no suggestion.
    """
    result = template
    while "{" in result:
        start = result.index("{")
        end = result.find("}", start)
        if end < 0:
            return ""
        name = result[start + 1 : end]
        value = str(features.get(name) or "")
        if not value:
            return ""
        result = result[:start] + value + result[end + 1 :]
    return result.strip("/")


def learned(conn: sqlite3.Connection, *, limit: int = 50) -> list[dict[str, object]]:
    """Every pattern with enough behind it to be offered, for a read-only list.

    Built from the same counts a Review row uses, so the page cannot show a
    pattern that Review would not offer, or miss one it would.
    """
    from librairy.format_policy import answers as policy_answer

    rows = conn.execute(
        f"""
        SELECT e.signature, e.kind, e.outcome, e.features, e.specificity,
               COUNT(*) AS n, MAX(e.decided_at) AS last
        FROM decision_events e
        WHERE {_COMPLETED}
        GROUP BY e.signature, e.outcome
        ORDER BY n DESC, last DESC
        """,  # noqa: S608 - `_COMPLETED` is a module constant
    ).fetchall()
    by_signature: dict[str, list] = defaultdict(list)
    for row in rows:
        by_signature[str(row["signature"])].append(row)
    stopped = {
        str(row["signature"])
        for row in conn.execute("SELECT signature FROM decision_suppressions")
    }
    found: list[dict[str, object]] = []
    for signature, group in by_signature.items():
        top = group[0]
        support = int(top["n"])
        others = sum(int(row["n"]) for row in group[1:])
        if not _dominant(support, others):
            continue
        features = json.loads(top["features"])
        found.append(
            {
                "signature": signature,
                "kind": str(top["kind"]),
                "outcome": str(top["outcome"]),
                "support": support,
                "contradictions": others,
                "described": Cue(str(top["kind"]), features).described,
                #  Non-empty when explicit policy already answers this. A
                #  learned pattern is the weakest kind of evidence in the
                #  program and must never be presented as competing with an
                #  instruction the owner actually gave — see
                #  `format_policy.answers`.
                "policy": policy_answer(conn, str(top["kind"]), features),
                "suppressed": signature in stopped,
                "last": str(top["last"] or ""),
            }
        )
    found.sort(key=lambda item: (-int(item["support"]), str(item["described"])))
    return _collapse(found)[:limit]


def _collapse(patterns: list[dict[str, object]]) -> list[dict[str, object]]:
    """One row per lesson, not one per rung of the ladder that reaches it.

    "Manuals go under `Documents/Manuals/{organization}`" and "Honda manuals go
    under `Documents/Manuals/{organization}`" are the same lesson written at
    two widths — the second is how the first was learned, not a second thing
    the owner believes. Listing both reads as a duplicate, and turning one off
    would leave the other still firing.

    The broadest is kept, because it is the one that covers the most, and it
    carries every signature that agrees with it so that switching it off
    switches off the whole conclusion.
    """
    by_outcome: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    for pattern in patterns:
        by_outcome[(str(pattern["kind"]), str(pattern["outcome"]))].append(pattern)
    kept: list[dict[str, object]] = []
    for group in by_outcome.values():
        broadest = min(group, key=lambda item: len(str(item["described"])))
        kept.append(
            {
                **broadest,
                "signatures": [str(item["signature"]) for item in group],
                #  Named so the page can say what the lesson was learned from
                #  without pretending they are separate beliefs.
                "narrower": [
                    str(item["described"])
                    for item in group
                    if item is not broadest and item["described"]
                ],
            }
        )
    kept.sort(key=lambda item: (-int(item["support"]), str(item["described"])))
    return kept


def _normal(value: object) -> str:
    """One spelling of a cue value, so `Honda Motor Co.` matches itself."""
    return " ".join(str(value or "").casefold().split()).strip(" .")
