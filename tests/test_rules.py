"""Turning a habit into a policy, and the line between the two.

The whole of M2-04 is one distinction and every test here is about it:

    eighteen decisions        a fact about what somebody has done
    a rule                    a statement about what they want

Repetition earns the **offer**. It never earns the rule. A program that turns
the first into the second on a count has taken a decision that was not its to
take, and the tests that matter here are the ones proving it cannot.

Histories are built by actually approving and committing files, through the
same `apply_review_action` and `execute_plan` a person drives, because the
thing under test is what LibrAIry learns from real decisions. A hand-inserted
`decision_events` row would prove the insert.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from librairy import rules
from librairy.decision_cues import CATEGORY
from librairy.decisions import Cue, learned
from librairy.models import EvidenceEntry
from librairy.web.review import learned_suggestions
from tests.test_decisions import (  # noqa: PLC2701 - the shared history builders
    client_for,
    decide,
    manual_evidence,
    row_for,
    settings_for,
    stage,
)


def book_evidence(author: str = "Steve Klabnik") -> list[EvidenceEntry]:
    return [
        EvidenceEntry("heuristic", "category", "book-like extension/name", 0.84),
        EvidenceEntry("document", "type", "Book", 0.85),
        EvidenceEntry("document", "author", author, 0.9),
    ]


def a_settled_habit(
    conn: sqlite3.Connection,
    settings,  # noqa: ANN001
    times: int = 9,
    dest: str = "Documents/Manuals/Honda Motor Co",
) -> None:
    """Enough Honda manuals, all filed the same way, to be worth writing down."""
    for index in range(times):
        decide(conn, settings, f"manual-{index}.pdf", f"{dest}/manual-{index}.pdf")


def the_pattern(conn: sqlite3.Connection) -> dict:
    """The manuals lesson, as the page shows it.

    `learned` collapses the ladder to its broadest rung — "manuals go here" is
    how "Honda manuals go here" was learned, not a second belief — so the
    description names the type rather than the manufacturer.
    """
    found = [
        pattern
        for pattern in learned(conn, limit=50)
        if "Manual" in str(pattern["described"])
    ]
    assert found, "the history should have produced a pattern"
    return found[0]


# --- what repetition may and may not do -----------------------------------------


def test_repetition_earns_an_offer_and_never_a_rule(tmp_path: Path) -> None:
    """The line the whole feature is drawn along. Nine identical decisions
    produce an offer on the page and nothing in the rules table."""
    _, conn, settings = client_for(tmp_path)
    a_settled_habit(conn, settings)

    pattern = the_pattern(conn)

    assert rules.promotable(pattern) is True
    assert "9 times" in rules.offer(pattern)
    assert rules.all_rules(conn) == [], "nothing but a person creates a rule"


def test_a_habit_that_is_not_settled_yet_is_not_offered(tmp_path: Path) -> None:
    """Three is enough to *suggest*, and deliberately not enough to write down.
    A suggestion says "you keep doing this"; a rule says "this is how it
    works", and the second claim needs more than the first."""
    _, conn, settings = client_for(tmp_path)
    a_settled_habit(conn, settings, times=4)

    pattern = the_pattern(conn)

    assert pattern["support"] == 4, "enough to be suggested"
    assert rules.promotable(pattern) is False, "and not enough to be written down"


def test_a_divided_history_is_never_offered_however_long_it_is(
    tmp_path: Path,
) -> None:
    """An offer needs the evidence to be settled rather than merely popular:
    five times as many confirmations as departures, against the two the
    ordinary suggestion needs."""
    _, conn, settings = client_for(tmp_path)
    a_settled_habit(conn, settings, times=9)
    for index in range(3):
        decide(
            conn,
            settings,
            f"elsewhere-{index}.pdf",
            f"Documents/2024/elsewhere-{index}.pdf",
        )

    pattern = the_pattern(conn)

    assert pattern["support"] == 9
    assert pattern["contradictions"] == 3
    assert rules.promotable(pattern) is False


def test_a_suppressed_pattern_is_not_offered_for_promotion(tmp_path: Path) -> None:
    """"Stop offering me this" and "would you like to make it permanent" are
    not two things to say about one pattern."""
    from librairy.decisions import suppress

    _, conn, settings = client_for(tmp_path)
    a_settled_habit(conn, settings)
    pattern = the_pattern(conn)
    for signature in pattern.get("signatures") or [pattern["signature"]]:
        suppress(conn, signature)

    again = next(
        (item for item in learned(conn, limit=50) if "Manual" in str(item["described"])),
        None,
    )

    assert again is not None
    assert rules.promotable(again) is False


# --- domain scoping --------------------------------------------------------------


def test_a_habit_learned_from_books_says_nothing_about_music(tmp_path: Path) -> None:
    """Every cue carries the category, so a signature learned in one domain
    cannot match a row in another. This is not a filter applied afterwards —
    the patterns are different strings."""
    _, conn, settings = client_for(tmp_path)
    for index in range(5):
        decide(
            conn,
            settings,
            f"book-{index}.epub",
            f"Books/Programming/book-{index}.epub",
            evidence=book_evidence(),
            category="books",
        )

    music = stage(
        conn,
        settings,
        "track.flac",
        "Music/Unsorted/track.flac",
        evidence=[EvidenceEntry("heuristic", "category", "audio extension", 0.8)],
        category="music",
    )

    assert learned_suggestions(conn, [row_for(conn, music)]) == {}


def test_every_learned_pattern_names_the_domain_it_belongs_to(
    tmp_path: Path,
) -> None:
    _, conn, settings = client_for(tmp_path)
    a_settled_habit(conn, settings, times=4)

    for pattern in learned(conn, limit=50):
        assert CATEGORY in pattern["features"], (
            "a pattern with no category could match anything"
        )


# --- promotion ------------------------------------------------------------------


def promoted(conn: sqlite3.Connection, pattern: dict, name: str = "Honda manuals") -> int:
    return rules.promote(
        conn,
        signature=str(pattern["signature"]),
        kind=str(pattern["kind"]),
        features=dict(pattern["features"]),
        outcome=str(pattern["outcome"]),
        name=name,
        support=int(pattern["support"]),
    )


def test_a_promoted_rule_says_what_it_matches_and_why_it_exists(
    tmp_path: Path,
) -> None:
    _, conn, settings = client_for(tmp_path)
    a_settled_habit(conn, settings)
    promoted(conn, the_pattern(conn))

    rule = rules.all_rules(conn)[0]

    assert rule.name == "Honda manuals"
    assert "Manual" in rule.described, "what it matches, in the cues it matched on"
    assert rule.outcome.startswith("Documents/Manuals")
    assert rule.domain == "documents", "the context it belongs to"
    assert "9 decision" in rule.why
    assert rule.enabled is True


def test_a_rule_survives_a_restart(tmp_path: Path) -> None:
    """It is a row in the database, not a thing held in a process."""
    from librairy.db import connect

    _, conn, settings = client_for(tmp_path)
    a_settled_habit(conn, settings)
    promoted(conn, the_pattern(conn))
    conn.commit()
    conn.close()

    reopened = connect(settings_for(tmp_path))

    assert [rule.name for rule in rules.all_rules(reopened)] == ["Honda manuals"]


def test_promoting_the_same_pattern_twice_is_one_rule(tmp_path: Path) -> None:
    """Two rules matching the same files would both fire and disagree about
    which of them is the reason."""
    _, conn, settings = client_for(tmp_path)
    a_settled_habit(conn, settings)
    pattern = the_pattern(conn)

    promoted(conn, pattern, name="First")
    promoted(conn, pattern, name="Second")

    assert [rule.name for rule in rules.all_rules(conn)] == ["Second"]


def test_a_rule_keeps_offering_when_the_habit_behind_it_has_gone_quiet(
    tmp_path: Path,
) -> None:
    """The one thing a rule does that a suggestion does not. A learned pattern
    falls silent when the history divides — that is right, because it is a
    claim about behaviour and the behaviour changed. A rule is a claim the
    owner made, and it stands until they say otherwise."""
    _, conn, settings = client_for(tmp_path)
    a_settled_habit(conn, settings)
    promoted(conn, the_pattern(conn))
    #  Enough filed elsewhere that the counting no longer finds an answer.
    for index in range(8):
        decide(conn, settings, f"other-{index}.pdf", f"Documents/2024/other-{index}.pdf")

    waiting = stage(conn, settings, "manual-new.pdf", "Documents/Unsorted/manual-new.pdf")
    found = learned_suggestions(conn, [row_for(conn, waiting)])

    assert found, "the rule still answers"
    assert found[int(waiting)]["rule"] == "Honda manuals"
    assert found[int(waiting)]["explanation"] == "Rule: Honda manuals"


# --- what a rule may never do ----------------------------------------------------


def test_a_rule_preselects_and_never_approves(tmp_path: Path) -> None:
    """Level four, permanently. Promoting a habit makes it durable; it does not
    move it up the authority ladder."""
    from librairy.confidence_tiers import settled_now

    _, conn, settings = client_for(tmp_path)
    a_settled_habit(conn, settings)
    promoted(conn, the_pattern(conn))

    waiting = stage(conn, settings, "manual-new.pdf", "Documents/Unsorted/manual-new.pdf")
    row = row_for(conn, waiting)
    found = learned_suggestions(conn, [row])

    assert found, "it fills an answer in"
    assert conn.execute(
        "SELECT status FROM proposals WHERE id=?", (waiting,)
    ).fetchone()["status"] == "proposed", "and changes nothing else"
    #  The tier machinery from M1-05, on the same row: a proposal carrying a
    #  suggestion is never settled and so never reaches Ready for Commit on
    #  its own, whatever its score.
    assert settled_now({"tier": "settled", "suggestion": found[int(waiting)]}) is False


def test_a_rule_loses_to_a_catalog_identity_about_this_file(tmp_path: Path) -> None:
    """Level three beats level four, and a rule is level four. Six manuals
    filed under one folder is a habit; an ISBN printed in this file is a fact
    about this file."""
    _, conn, settings = client_for(tmp_path)
    a_settled_habit(conn, settings)
    promoted(conn, the_pattern(conn))

    identified = stage(
        conn,
        settings,
        "manual-isbn.pdf",
        "Books/Some Author/manual-isbn.pdf",
        evidence=[*manual_evidence(), EvidenceEntry("document", "isbn", "9780441013593", 0.95)],
    )

    assert learned_suggestions(conn, [row_for(conn, identified)]) == {}


def test_only_a_request_can_create_a_rule() -> None:
    """A statement about the code rather than about one run.

    Every other automation in LibrAIry has a worker step: settled approvals,
    consistency checks, held-file recovery. This one deliberately has none, and
    the way to keep it that way is to notice the day something outside the web
    layer learns how to call `promote`.
    """
    import re
    from pathlib import Path as P

    #  A *call*, not the word: "promoted", "promotion" and a comment about
    #  promoting all appear in modules that have nothing to do with this.
    calls = re.compile(r"\bpromote\s*\(")
    source = P("src/librairy")
    callers = sorted(
        str(path.relative_to(source))
        for path in source.rglob("*.py")
        if path.name != "rules.py" and calls.search(path.read_text(encoding="utf-8"))
    )

    assert callers == ["web/app.py"], (
        "a rule is created by somebody pressing a button, and by nothing else"
    )


# --- overriding ------------------------------------------------------------------


def test_overriding_a_learned_suggestion_enough_times_silences_it(
    tmp_path: Path,
) -> None:
    """No new machinery: a suggestion is a claim about what somebody usually
    does, and doing something else is recorded as the decision it is. The
    history divides and the claim stops being supportable."""
    _, conn, settings = client_for(tmp_path)
    a_settled_habit(conn, settings, times=5)
    waiting = stage(conn, settings, "check.pdf", "Documents/Unsorted/check.pdf")
    assert learned_suggestions(conn, [row_for(conn, waiting)]), "it fires at first"

    for index in range(5):
        decide(conn, settings, f"instead-{index}.pdf", f"Documents/2025/instead-{index}.pdf")

    again = stage(conn, settings, "check-2.pdf", "Documents/Unsorted/check-2.pdf")

    assert learned_suggestions(conn, [row_for(conn, again)]) == {}


def test_overriding_a_rule_is_counted_and_never_acted_on(tmp_path: Path) -> None:
    """Switching off a policy somebody wrote down, on a count, is the same
    overreach as creating one on a count. So it is said, and left to them."""
    _, conn, settings = client_for(tmp_path)
    a_settled_habit(conn, settings)
    promoted(conn, the_pattern(conn))

    for index in range(4):
        decide(conn, settings, f"nope-{index}.pdf", f"Documents/2025/nope-{index}.pdf")

    rule = rules.all_rules(conn)[0]

    assert rule.overrides == 4
    assert rule.enabled is True, "still theirs"
    assert rule.worth_mentioning is True, "and the page says so"


def test_approving_what_a_rule_recommended_is_not_an_override(
    tmp_path: Path,
) -> None:
    _, conn, settings = client_for(tmp_path)
    a_settled_habit(conn, settings)
    promoted(conn, the_pattern(conn))
    before = rules.all_rules(conn)[0].overrides

    decide(
        conn,
        settings,
        "manual-again.pdf",
        "Documents/Manuals/Honda Motor Co/manual-again.pdf",
    )

    assert rules.all_rules(conn)[0].overrides == before


# --- scope -----------------------------------------------------------------------


def test_a_rule_is_created_at_the_width_it_was_learned_at(tmp_path: Path) -> None:
    _, conn, settings = client_for(tmp_path)
    a_settled_habit(conn, settings)
    promoted(conn, the_pattern(conn))

    rule = rules.all_rules(conn)[0]

    assert rule.scope == rules.CATEGORY_SCOPE
    assert rule.is_global is False
    assert CATEGORY in rule.features


def test_widening_a_rule_is_a_separate_deliberate_act(tmp_path: Path) -> None:
    """A filing policy learned from one kind of file, applied to everything,
    renames a photograph the first time it matches one. Nothing automatic
    reaches this."""
    _, conn, settings = client_for(tmp_path)
    a_settled_habit(conn, settings)
    rule_id = promoted(conn, the_pattern(conn))

    assert rules.all_rules(conn)[0].is_global is False

    rules.widen(conn, rule_id)
    assert rules.all_rules(conn)[0].is_global is True
    assert rules.all_rules(conn)[0].domain == "", "it belongs to no one category now"

    rules.narrow(conn, rule_id)
    assert rules.all_rules(conn)[0].is_global is False


def test_a_narrow_rule_does_not_match_another_category(tmp_path: Path) -> None:
    facts = [Cue("destination", {CATEGORY: "books", "document_type": "Manual"})]
    rule = rules.Rule(
        id=1,
        kind="destination",
        signature="destination|category=documents&document_type=manual",
        scope=rules.CATEGORY_SCOPE,
        features={CATEGORY: "documents", "document_type": "Manual"},
        outcome="Documents/Manuals",
        name="Manuals",
        enabled=True,
        support=9,
        overrides=0,
        created_at="now",
    )

    assert rules.matching([rule], facts) is None

    widened = rules.Rule(**{**rule.__dict__, "scope": rules.GLOBAL_SCOPE})
    assert rules.matching([widened], facts) is widened


def test_the_narrowest_matching_rule_wins(tmp_path: Path) -> None:  # noqa: ARG001
    """The same relation the ladder uses: how many features had to agree, and
    nothing else. There is no score here either."""
    facts = [
        Cue(
            "destination",
            {CATEGORY: "documents", "document_type": "Manual", "organization": "Honda"},
        )
    ]
    broad = rules.Rule(
        1, "destination", "a", rules.CATEGORY_SCOPE,
        {CATEGORY: "documents", "document_type": "Manual"},
        "Documents/Manuals", "Manuals", True, 9, 0, "now",
    )
    narrow = rules.Rule(
        2, "destination", "b", rules.CATEGORY_SCOPE,
        {CATEGORY: "documents", "document_type": "Manual", "organization": "Honda"},
        "Documents/Manuals/Honda", "Honda", True, 9, 0, "now",
    )

    assert rules.matching([broad, narrow], facts) is narrow


def test_a_switched_off_rule_answers_nothing(tmp_path: Path) -> None:
    _, conn, settings = client_for(tmp_path)
    a_settled_habit(conn, settings)
    rule_id = promoted(conn, the_pattern(conn))
    rules.set_enabled(conn, rule_id, False)

    assert rules.active(conn) == []

    rules.forget(conn, rule_id)
    assert rules.all_rules(conn) == []
    assert learned(conn, limit=50), "the decisions it was made from are untouched"


# --- through the pages ------------------------------------------------------------


def test_the_page_offers_promotion_and_only_a_press_takes_it(
    tmp_path: Path,
) -> None:
    client, conn, settings = client_for(tmp_path)
    a_settled_habit(conn, settings)

    page = client.get("/review/learned").text
    assert "Save as a rule" in page
    assert "You have filed" in page
    assert rules.all_rules(conn) == []

    signature = str(the_pattern(conn)["signature"])
    client.post(
        "/review/learned/promote",
        data={"signature": signature, "name": "Honda manuals"},
        headers={"x-csrf-token": client.cookies["csrf_token"]},
        follow_redirects=False,
    )

    assert [rule.name for rule in rules.all_rules(conn)] == ["Honda manuals"]
    after = client.get("/review/learned").text
    assert "Your rules" in after
    assert "Honda manuals" in after


def test_the_page_can_switch_a_rule_off_widen_it_and_remove_it(
    tmp_path: Path,
) -> None:
    client, conn, settings = client_for(tmp_path)
    a_settled_habit(conn, settings)
    rule_id = promoted(conn, the_pattern(conn))
    headers = {"x-csrf-token": client.cookies["csrf_token"]}

    def act(action: str) -> None:
        client.post(
            f"/review/rules/{rule_id}",
            data={"action": action},
            headers=headers,
            follow_redirects=False,
        )

    act("disable")
    assert rules.all_rules(conn)[0].enabled is False
    act("enable")
    act("widen")
    assert rules.all_rules(conn)[0].is_global is True
    act("forget")
    assert rules.all_rules(conn) == []


def test_an_unknown_rule_action_is_refused(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    a_settled_habit(conn, settings)
    rule_id = promoted(conn, the_pattern(conn))

    response = client.post(
        f"/review/rules/{rule_id}",
        data={"action": "approve everything"},
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )

    assert response.status_code == 422


def test_review_says_which_kind_of_authority_filled_the_answer_in(
    tmp_path: Path,
) -> None:
    """One authority model, and a reader has to be able to tell the two
    sentences apart: a rule is something they wrote, a habit is something
    LibrAIry noticed."""
    _, conn, settings = client_for(tmp_path)
    a_settled_habit(conn, settings, times=5)
    waiting = stage(conn, settings, "check.pdf", "Documents/Unsorted/check.pdf")

    habit = learned_suggestions(conn, [row_for(conn, waiting)])[int(waiting)]
    assert habit["rule"] == ""
    assert "previous decision" in str(habit["explanation"])

    promoted(conn, the_pattern(conn), name="Honda manuals")
    again = learned_suggestions(conn, [row_for(conn, waiting)])[int(waiting)]

    assert again["explanation"] == "Rule: Honda manuals"


@pytest.mark.parametrize(
    ("features", "expected"),
    [
        ({"category": "documents", "document_type": "Manual"}, "Documents · Manual"),
        ({}, "Everything else"),
    ],
)
def test_a_rule_starts_with_a_name_somebody_can_recognise(
    features: dict, expected: str
) -> None:
    """Generated rather than demanded. A form that will not submit without a
    name turns a one-press decision into a writing exercise — and it is built
    from the cue values, because parsing the sentence back apart guessed where
    the cue name ended and produced `Documents · Type Manual`."""
    assert rules.suggested_name({"features": features}) == expected
