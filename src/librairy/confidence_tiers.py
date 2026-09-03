"""How much attention one proposal is worth, stated as a rule about evidence.

Every decision in Review costs the same amount of attention regardless of how
certain it is. A file whose ISBN was read off its own title page and a file
whose destination was guessed from a year in its filename arrive as the same
row, with the same buttons, in the same list.

Three tiers, and **the rule is about evidence, not about the score**:

    uncertain    nothing is sure enough to preselect, or two things disagree
    suggested    good evidence, or a habit — preselected, and labelled why
    settled      identity, read off this file or printed in it

The distinction that matters is between a *guess with a high number on it* and
an *identity*. `0.92` from a filename heuristic and `0.92` from an AcoustID
match are the same number and are not the same claim, so the number is not what
decides the tier. `decision_cues.STRONG_SOURCES` and `STRONG_FIELDS` already
name the difference — they are what keeps a learned habit quiet — and this
reads the same two sets rather than growing a third opinion about them.

## What may never be settled

A learned pattern. It is authority level 4, permanently, and the reason is in
`docs/architecture/decision-memory.md`: a habit is a statement about files that
*resembled* this one. It may preselect, it may explain itself, and it may never
put a file in front of Commit on its own.

Neither may a weak heuristic, an AI cue or a model's opinion about a picture.

## What settled means, and what it does not

It means the answer is not in doubt, so the decision is worth *one* look
instead of one each. It does **not** mean anything moves: nothing in LibrAIry
moves without Commit, and Commit shows the exact list before it touches a file.
This changes where a decision waits, never whether it is taken.

See `docs/ROADMAP.md` M1-05.
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from typing import Any

from librairy.decision_cues import STRONG_FIELDS, STRONG_SOURCES

UNCERTAIN = "uncertain"
SUGGESTED = "suggested"
SETTLED = "settled"
TIERS = (UNCERTAIN, SUGGESTED, SETTLED)

#  The same threshold Review has always used for "confident", and it still only
#  ever reaches `suggested`. A high score is a good guess, and a good guess is
#  worth preselecting and not worth skipping.
CONFIDENT = 0.85

TIER_LABEL = {
    UNCERTAIN: "Needs you",
    SUGGESTED: "Suggested",
    SETTLED: "Settled by identity",
}

TIER_NOTE = {
    UNCERTAIN: "Nothing here is sure enough to answer for you.",
    SUGGESTED: "A good guess, filled in for you. Check it and approve.",
    SETTLED: "Identified from the file itself. Still yours to commit.",
}

#  Evidence that is a claim about *this* file rather than about files like it.
#  Read from `decision_cues` so the tier and the authority order cannot drift:
#  the sources that outrank a habit are exactly the sources that can settle a
#  decision, because they are the same fact.
IDENTIFIED_SOURCES = STRONG_SOURCES
IDENTIFIED_FIELDS = STRONG_FIELDS

#  A catalog that was asked and found nothing has not identified anything. The
#  entry is still written — "we looked" is worth recording — and it must not be
#  read as an answer.
NO_MATCH = "no-match"

#  The evidence field `classify/documents.py` writes when the sources that
#  named a document do not agree. A contested document may carry an ISBN and
#  must still not be settled by it: the identifier is real, and which work it
#  identifies is precisely what is in question. See
#  `librairy/document_identity.py`.
CONTESTED = "conflict"


def contested(evidence: object) -> bool:
    """Did the analysis record that its sources disagreed about this file?"""
    return any(
        str(entry.get("field") or "") == CONTESTED for entry in entries(evidence)
    )


def entries(evidence: object) -> list[dict[str, Any]]:
    """The evidence of one proposal, however the caller is holding it.

    Three shapes, because three callers hold it three ways and all of them ask
    the same question: `EvidenceEntry` objects on the way in from the
    classifier, a JSON string on the way out of SQLite, and dicts in between.
    """
    if isinstance(evidence, list):
        return [
            entry if isinstance(entry, dict) else asdict(entry)
            for entry in evidence
            if isinstance(entry, dict) or is_dataclass(entry)
        ]
    try:
        payload = json.loads(str(evidence or "[]"))
    except (TypeError, ValueError):
        return []
    return [entry for entry in payload if isinstance(entry, dict)]


def identity_of(evidence: object) -> str:
    """What identified this file, in words, or "".

    The answer to "why am I here" for anything that reaches `settled`, and it
    is derived from the evidence rather than stored beside it — two records of
    why can disagree, and one cannot.
    """
    for entry in entries(evidence):
        source = str(entry.get("source") or "")
        field = str(entry.get("field") or "")
        detail = str(entry.get("detail") or "")
        if source in IDENTIFIED_SOURCES and str(entry.get("status") or "") != NO_MATCH:
            named = f" — {detail}" if detail else ""
            return f"{source} identified this file{named}"
        if field in IDENTIFIED_FIELDS:
            printed = f" {detail}" if detail else ""
            return f"the {field.upper()}{printed} is printed in this file"
    return ""


def tier_for(
    evidence: object, confidence: float | None, dest_relpath: str | None
) -> str:
    """Which tier one proposal belongs to, from its own evidence.

    A pure function of what the analysis wrote, so it can be decided once when
    a proposal is made and recomputed only when the evidence changes. It knows
    nothing about the situation *around* the file — an arrival that turns out
    to be a second copy of something, a comparison somebody has not answered —
    because those are not facts about this proposal and they arrive later. The
    callers that act on a tier check them; see `settled_now`.
    """
    #  A decision with nowhere to go is the definition of one that needs
    #  somebody. It is asked first because a strong identity with no
    #  destination is still a question — knowing what a file *is* is not
    #  knowing where its owner keeps it.
    if not dest_relpath:
        return UNCERTAIN
    #  Before the identity, and that is the whole point. A document whose ISBN
    #  is printed on a page whose title disagrees with its metadata has an
    #  identifier and an open question, and answering it automatically would
    #  file the book the argument was about.
    if contested(evidence):
        return UNCERTAIN
    if identity_of(evidence):
        return SETTLED
    if (confidence or 0.0) >= CONFIDENT:
        return SUGGESTED
    return UNCERTAIN


#  Facts about the *situation* rather than about the proposal, each of which
#  turns a settled filing back into a question. All three are a different
#  decision wearing a filing's clothes: this file is a copy of one you have,
#  this file is another version of one you have, the picture is not what the
#  category says. None of them is knowable when the proposal is written.
def settled_now(row: dict[str, Any]) -> bool:
    """Is this row settled *and* is nothing else about it in question?

    What a batch action and any automatic approval must ask, as opposed to what
    the column says. The column is a fact about the evidence; this is a fact
    about right now.
    """
    if str(row.get("tier") or "") != SETTLED:
        return False
    return not (
        row.get("duplicate_of")
        or row.get("similar_to")
        or row.get("vision_disagrees")
        or row.get("suggestion")
    )


def why_settled(row: dict[str, Any]) -> str:
    """"Why am I here", for one row that reached the settled tier."""
    return identity_of(row.get("evidence"))
