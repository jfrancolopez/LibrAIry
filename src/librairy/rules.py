"""A habit the owner promoted into a policy.

`decisions.py` notices that the same choice keeps being made and offers the
answer. That is a **suggestion**: it is built from counting, it goes quiet on
its own the moment the history stops agreeing with itself, and it says how many
decisions are behind it. A **rule** is what somebody says when they have seen
that pattern and want it kept — *yes, that is my filing policy* — and from then
on it is theirs rather than an observation about them.

    18 completed decisions        →  LibrAIry offers to make a rule
    the owner presses the button  →  there is a rule

**Repetition earns the offer. It never earns the rule.** That distinction is
the whole of this module and it is worth being blunt about: "you have done this
eighteen times" is not the same claim as "you have decided this is how it
works", and a program that quietly turns the first into the second has taken a
decision that was not its to take. Nothing here is created by a worker, a
threshold or a cycle.

## Same machinery, same authority

A rule is not a second learning engine. It stores the *signature* the pattern
was learned under, verbatim, so a rule and the decisions behind it can never
come to describe different things; it is consulted on the same code path, in
the same function, and produces the same shape of answer with a different
sentence attached.

And it is the same **authority** — level four, permanently, exactly as a
learned suggestion is:

    1  safety invariants        never overwrite, never delete, revalidate
    2  explicit user policy     `music.preferred_format = mp3`
    3  strong current evidence  a catalog identity, an ISBN, a DOI
    4  a habit, or a rule       "you file Honda manuals here"

A rule may **fill an answer in**. It may not approve, commit, settle, or move
anything, and it must stay quiet when level 3 has a better answer about *this*
file. Promoting a pattern does not move it up the ladder; it makes it durable,
listable and yours. See `docs/architecture/decision-memory.md`.

## Scope, and why widening is a separate act

A rule is created at exactly the width it was learned at, which always includes
the category — so a habit about books cannot begin answering questions about
music because somebody promoted it. Making one apply across categories is a
second, deliberate press, and it says so on the page.

That asymmetry is the point. Narrowing a rule costs somebody nothing; widening
one silently is how a filing policy about invoices ends up renaming a
photograph.

## Overriding a rule

Counted, shown, and never acted on. A learned suggestion weakens itself — the
history divides and `decisions._dominant` stops finding an answer — because a
suggestion is a claim *about* the owner's behaviour and their behaviour
changed. A rule is a claim the owner made, so LibrAIry says "you have filed
four of these somewhere else since" and leaves the decision where it belongs.

Turning off a policy somebody wrote down, on a count, would be the same
overreach as creating one on a count.

See `docs/ROADMAP.md` M2-04.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from librairy.decisions import KINDS, MIN_SUPPORT, Cue
from librairy.planner import utc_now

#  How many completed decisions make an *offer* worth putting on screen.
#
#  Not a second opinion about what a pattern is — `decisions.MIN_SUPPORT` still
#  decides whether LibrAIry has learned anything at all. This is the point at
#  which a learned answer has been right often enough that writing it down is a
#  service rather than a guess, and it is deliberately well above the point at
#  which suggesting one becomes fair.
PROMOTE_SUPPORT = 2 * MIN_SUPPORT + 2  # 8

#  And it has to be *settled*, not merely popular. A suggestion needs more than
#  twice as many confirmations as departures; an offer to write the habit down
#  permanently needs five times as many. Same shape of test as
#  `decisions._dominant`, one rung stricter, so there is one idea here and not
#  two.
PROMOTE_DOMINANCE = 5

#  How many overrides before the page says something. A number for a sentence,
#  never for an action: see the module docstring.
OVERRIDES_WORTH_MENTIONING = 3

CATEGORY_SCOPE = "category"
GLOBAL_SCOPE = "global"
SCOPES = (CATEGORY_SCOPE, GLOBAL_SCOPE)


@dataclass(frozen=True)
class Rule:
    """One promoted pattern, as the pages read it."""

    id: int
    kind: str
    signature: str
    scope: str
    features: dict[str, str]
    outcome: str
    name: str
    enabled: bool
    support: int
    overrides: int
    created_at: str

    @property
    def described(self) -> str:
        return Cue(self.kind, self.features).described

    @property
    def is_global(self) -> bool:
        return self.scope == GLOBAL_SCOPE

    @property
    def domain(self) -> str:
        """The category this rule belongs to, or "" once it was widened."""
        from librairy.decision_cues import CATEGORY

        return "" if self.is_global else self.features.get(CATEGORY, "")

    @property
    def why(self) -> str:
        """Why LibrAIry offered to make this, in the words it offered them in."""
        return (
            f"Offered after {self.support} decision"
            f"{'' if self.support == 1 else 's'} that all went the same way."
        )

    @property
    def worth_mentioning(self) -> bool:
        return self.overrides >= OVERRIDES_WORTH_MENTIONING


def promotable(pattern: dict[str, object]) -> bool:
    """Is this learned pattern settled enough to offer writing down?

    Takes a row from `decisions.learned` rather than querying again, so the
    page cannot offer to promote something it is not also showing.
    """
    support = int(pattern.get("support") or 0)
    others = int(pattern.get("contradictions") or 0)
    if pattern.get("suppressed"):
        return False
    return support >= PROMOTE_SUPPORT and support > PROMOTE_DOMINANCE * others


def offer(pattern: dict[str, object]) -> str:
    """The sentence that offers it, with both numbers in it.

    Both, because "you have chosen this 18 times" and "and twice you did not"
    are the same fact and only one of them flatters the suggestion.

    It does *not* repeat the pattern's own description. "You have filed category
    documents, document type Manual this way 8 times" is a sentence written in
    the column names, and the row above already says what the pattern matches
    in the place a reader is looking for it.
    """
    support = int(pattern.get("support") or 0)
    others = int(pattern.get("contradictions") or 0)
    times = f"{support} time{'' if support == 1 else 's'}"
    if not others:
        return f"You have filed these this way {times}, and never anywhere else."
    return (
        f"You have filed these this way {times}, and "
        f"{others} time{'' if others == 1 else 's'} you did not."
    )


def suggested_name(pattern: dict[str, object]) -> str:
    """A name somebody can recognise on a list, from the cues themselves.

    Generated rather than demanded: a form that will not submit without a name
    is a form that turns a one-press decision into a writing exercise. It is
    editable, and this is what it starts as.

    Built from the feature *values* rather than from the sentence describing
    them. Parsing "document type Manual" back apart guesses where the cue name
    ends, and guessed wrong: it produced `Documents · Type Manual`.
    """
    features = pattern.get("features")
    if isinstance(features, dict) and features:
        values = [str(value).strip() for _, value in sorted(features.items())]
        return " · ".join(value.title() for value in values if value)
    return "Everything else"


def promote(
    conn: sqlite3.Connection,
    *,
    signature: str,
    kind: str,
    features: dict[str, str],
    outcome: str,
    name: str = "",
    support: int = 0,
) -> int:
    """Write a rule down. Only ever called by somebody pressing a button.

    Idempotent on the signature: promoting the same pattern twice is one rule,
    re-enabled and renamed, rather than two that would both fire and disagree
    about which of them is the reason.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown decision kind: {kind}")
    if not signature or not outcome:
        raise ValueError("a rule needs a pattern and an answer")
    now = utc_now()
    conn.execute(
        """
        INSERT INTO decision_rules
          (kind, signature, scope, features, outcome, name, enabled, support,
           overrides, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, 1, ?, 0, ?, ?)
        ON CONFLICT(signature) DO UPDATE SET
          enabled=1,
          name=excluded.name,
          outcome=excluded.outcome,
          support=excluded.support,
          updated_at=excluded.updated_at
        """,
        (
            kind,
            signature,
            CATEGORY_SCOPE,
            json.dumps(features, sort_keys=True),
            outcome,
            name.strip() or suggested_name({"described": Cue(kind, features).described}),
            support,
            now,
            now,
        ),
    )
    row = conn.execute(
        "SELECT id FROM decision_rules WHERE signature=?", (signature,)
    ).fetchone()
    return int(row["id"])


def set_enabled(conn: sqlite3.Connection, rule_id: int, enabled: bool) -> None:
    """Switch one off, or back on. The rule and its history are untouched."""
    conn.execute(
        "UPDATE decision_rules SET enabled=?, updated_at=? WHERE id=?",
        (int(enabled), utc_now(), rule_id),
    )


def rename(conn: sqlite3.Connection, rule_id: int, name: str) -> None:
    conn.execute(
        "UPDATE decision_rules SET name=?, updated_at=? WHERE id=?",
        (name.strip(), utc_now(), rule_id),
    )


def widen(conn: sqlite3.Connection, rule_id: int) -> None:
    """Make one rule apply outside the category it was learned in.

    Its own function and its own button, because it is the one thing about a
    rule that cannot be undone by looking at it: a filing policy learned from
    invoices, applied to everything, will rename a photograph the first time it
    matches one. Nothing automatic reaches this.
    """
    conn.execute(
        "UPDATE decision_rules SET scope=?, updated_at=? WHERE id=?",
        (GLOBAL_SCOPE, utc_now(), rule_id),
    )


def narrow(conn: sqlite3.Connection, rule_id: int) -> None:
    """Put a widened rule back where it was learned."""
    conn.execute(
        "UPDATE decision_rules SET scope=?, updated_at=? WHERE id=?",
        (CATEGORY_SCOPE, utc_now(), rule_id),
    )


def forget(conn: sqlite3.Connection, rule_id: int) -> None:
    """Remove a rule. The decisions it was promoted from are untouched.

    They are what happened, they are in History, and the pattern may well be
    offered again — which is right: forgetting the rule is saying "this is not
    my policy", not "I never did this".
    """
    conn.execute("DELETE FROM decision_rules WHERE id=?", (rule_id,))


def all_rules(conn: sqlite3.Connection) -> list[Rule]:
    return [_rule(row) for row in conn.execute(
        "SELECT * FROM decision_rules ORDER BY enabled DESC, name COLLATE NOCASE"
    )]


def active(conn: sqlite3.Connection, kind: str = "") -> list[Rule]:
    """The rules that may fill an answer in, for one page of Review.

    One query for the page rather than one per row: there are never many of
    these — a rule is something a person deliberately made — and matching them
    against a row's cues is set comparison in Python.
    """
    where = "enabled = 1" + (" AND kind = ?" if kind else "")
    params = (kind,) if kind else ()
    return [_rule(row) for row in conn.execute(
        f"SELECT * FROM decision_rules WHERE {where}", params  # noqa: S608
    )]


def matching(rules: list[Rule], cues: list[Cue]) -> Rule | None:
    """The narrowest enabled rule this file satisfies, or None.

    A rule matches when every feature it names is one this file actually has,
    with the same value — a subset test, which is the same relation
    `decisions.suggest` uses when it walks the ladder from narrow to broad. A
    global rule is matched with its category ignored on both sides, which is
    the only thing being global changes.

    Narrowest wins, decided by how many features had to agree and by nothing
    else. There is no score here either.
    """
    from librairy.decision_cues import CATEGORY

    if not rules or not cues:
        return None
    facts: dict[str, str] = {}
    for cue in sorted(cues, key=lambda item: cue_width(item)):
        facts.update(cue.features)
    found: list[tuple[int, Rule]] = []
    for rule in rules:
        wanted = dict(rule.features)
        against = dict(facts)
        if rule.is_global:
            wanted.pop(CATEGORY, None)
            against.pop(CATEGORY, None)
        if not wanted:
            continue
        if all(against.get(name) == value for name, value in wanted.items()):
            found.append((len(wanted), rule))
    if not found:
        return None
    return max(found, key=lambda pair: pair[0])[1]


def cue_width(cue: Cue) -> int:
    return cue.specificity


def note_override(conn: sqlite3.Connection, signatures: list[str]) -> None:
    """Somebody filed a matching file somewhere else. Counted, never acted on.

    Called with the signatures of what was actually chosen, so a rule only
    counts an override when the file it matched went elsewhere — approving the
    rule's own answer is not an override of it.
    """
    if not signatures:
        return
    placeholders = ",".join("?" for _ in signatures)
    conn.execute(
        f"UPDATE decision_rules SET overrides = overrides + 1, updated_at = ? "  # noqa: S608
        f"WHERE enabled = 1 AND signature IN ({placeholders})",
        (utc_now(), *signatures),
    )


def _rule(row: sqlite3.Row) -> Rule:
    try:
        features = json.loads(row["features"])
    except (TypeError, ValueError):  # pragma: no cover - a hand-edited row
        features = {}
    return Rule(
        id=int(row["id"]),
        kind=str(row["kind"]),
        signature=str(row["signature"]),
        scope=str(row["scope"] or CATEGORY_SCOPE),
        features={str(k): str(v) for k, v in features.items()},
        outcome=str(row["outcome"]),
        name=str(row["name"]),
        enabled=bool(row["enabled"]),
        support=int(row["support"] or 0),
        overrides=int(row["overrides"] or 0),
        created_at=str(row["created_at"] or ""),
    )
