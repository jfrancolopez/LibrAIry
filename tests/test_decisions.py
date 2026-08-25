"""What LibrAIry learns, what it refuses to learn, and what outranks it.

The refusals carry the weight here. A program that remembers everything you
did and repeats it confidently is worse than one that remembers nothing: one
accidental precedent becomes a rule, a divided history becomes a false
certainty, and a habit starts overruling a catalog that actually knows.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from librairy.config import Settings
from librairy.db import connect
from librairy.decision_cues import cues_for, outcome_for, outranked
from librairy.decisions import (
    ALLOWED,
    DESTINATION,
    MIN_SUPPORT,
    REPRESENTATION,
    Cue,
    generalize,
    learned,
    record,
    record_representation,
    render,
    restore,
    settle,
    suggest,
)
from librairy.executor import execute_plan
from librairy.history import undo_plan
from librairy.models import EvidenceEntry
from librairy.planner import OperationSpec, approve_plan, create_plan
from librairy.proposals import upsert_proposal
from librairy.scanner import scan_root
from librairy.web.app import create_app
from librairy.web.review import ReviewFilters, apply_review_action, learned_suggestions


def settings_for(tmp_path: Path) -> Settings:
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        FILE_STABILITY_SECONDS=0,
        _env_file=None,
    )
    for root in (
        settings.appdata_dir,
        settings.inbox_dir,
        settings.library_dir,
        settings.quarantine_dir,
    ):
        root.mkdir(parents=True, exist_ok=True)
    return settings


def client_for(tmp_path: Path) -> tuple[TestClient, sqlite3.Connection, Settings]:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    client = TestClient(create_app(settings, conn))
    client.post("/setup", data={"password": "correct horse battery"})
    return client, conn, settings


def manual_evidence(organization: str = "Honda Motor Co.") -> list[EvidenceEntry]:
    return [
        EvidenceEntry("heuristic", "category", "document extension", 0.88),
        EvidenceEntry("document", "type", "Manual", 0.85),
        EvidenceEntry("document", "organization", organization, 0.85),
    ]


def stage(
    conn: sqlite3.Connection,
    settings: Settings,
    name: str,
    dest: str,
    *,
    evidence: list[EvidenceEntry] | None = None,
    category: str = "documents",
) -> int:
    """One arrival, analysed and waiting for an answer."""
    (settings.inbox_dir / name).write_text(name, encoding="utf-8")
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    item = conn.execute(
        "SELECT id FROM items WHERE root='inbox' AND relpath=?", (name,)
    ).fetchone()["id"]
    proposal = upsert_proposal(
        conn,
        item_id=int(item),
        category=category,
        clean_name=Path(name).name,
        dest_relpath=dest,
        confidence=0.88,
        evidence=evidence if evidence is not None else manual_evidence(),
    )
    conn.execute("UPDATE items SET state='proposed' WHERE id=?", (item,))
    return proposal


def decide(
    conn: sqlite3.Connection,
    settings: Settings,
    name: str,
    dest: str,
    *,
    evidence: list[EvidenceEntry] | None = None,
    commit: bool = True,
    category: str = "documents",
) -> str:
    """Approve it, and — unless asked otherwise — actually commit it."""
    proposal = stage(conn, settings, name, dest, evidence=evidence, category=category)
    apply_review_action(conn, "approve", ReviewFilters(), proposal_ids=[proposal])
    if not commit:
        return ""
    plan = create_plan(conn, [OperationSpec("move", name, "library", dest)], settings)
    approve_plan(conn, plan, settings)
    execute_plan(conn, plan, settings)
    return plan


def row_for(conn: sqlite3.Connection, proposal_id: int) -> sqlite3.Row:
    return conn.execute(
        "SELECT p.*, i.relpath AS item_relpath FROM proposals p"
        " JOIN items i ON i.id = p.item_id WHERE p.id=?",
        (proposal_id,),
    ).fetchone()


def four_honda_manuals(conn: sqlite3.Connection, settings: Settings) -> None:
    for index in range(4):
        decide(
            conn,
            settings,
            f"manual-{index}.pdf",
            f"Documents/Manuals/Honda Motor Co/CR-V {index}.pdf",
        )


# 1 — one decision is a precedent, not a pattern.
def test_a_single_decision_creates_no_suggestion(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    decide(conn, settings, "one.pdf", "Documents/Manuals/Honda Motor Co/One.pdf")

    proposal = stage(conn, settings, "two.pdf", "Documents/2026/Two.pdf")

    assert learned_suggestions(conn, [row_for(conn, proposal)]) == {}


# 2 — two is still a coincidence.
def test_two_decisions_are_below_the_threshold(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    for index in range(2):
        decide(
            conn,
            settings,
            f"m{index}.pdf",
            f"Documents/Manuals/Honda Motor Co/M{index}.pdf",
        )

    proposal = stage(conn, settings, "third.pdf", "Documents/2026/Third.pdf")

    assert MIN_SUPPORT == 3
    assert learned_suggestions(conn, [row_for(conn, proposal)]) == {}


# 3 — three matching decisions make a suggestion.
def test_three_decisions_make_a_suggestion(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    for index in range(3):
        decide(
            conn,
            settings,
            f"m{index}.pdf",
            f"Documents/Manuals/Honda Motor Co/M{index}.pdf",
        )

    proposal = stage(conn, settings, "fourth.pdf", "Documents/2026/Fourth.pdf")
    found = learned_suggestions(conn, [row_for(conn, proposal)])[proposal]

    assert found["folder"] == "Documents/Manuals/Honda Motor Co"
    assert found["support"] == 3


# 4 — the count is the real count.
def test_the_support_count_is_truthful(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    four_honda_manuals(conn, settings)

    proposal = stage(conn, settings, "pilot.pdf", "Documents/2026/Pilot.pdf")
    found = learned_suggestions(conn, [row_for(conn, proposal)])[proposal]

    assert found["support"] == 4
    assert found["contradictions"] == 0
    assert "4 previous decisions" in found["explanation"]
    #  Decisions, never a percentage. A score cannot be checked; a count can.
    assert "%" not in found["explanation"]


# 5 — a divided history says nothing at all.
def test_conflicting_history_suppresses_the_suggestion(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    acme = manual_evidence("Acme")
    for index in range(3):
        decide(
            conn, settings, f"a{index}.pdf",
            f"Documents/Manuals/Acme/A{index}.pdf", evidence=acme,
        )
    for index in range(3):
        decide(
            conn, settings, f"b{index}.pdf",
            f"Documents/2026/B{index}.pdf", evidence=acme,
        )

    proposal = stage(conn, settings, "new.pdf", "Documents/Misc/New.pdf", evidence=acme)

    #  Three against three is two habits, not a preference. "You usually
    #  choose A" would be a claim the decisions do not support.
    assert learned_suggestions(conn, [row_for(conn, proposal)]) == {}


# 6 — the narrower pattern wins.
def test_a_narrower_pattern_beats_a_broader_one(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path))
    broad = Cue(DESTINATION, {"category": "documents", "document_type": "Manual"})
    narrow = Cue(
        DESTINATION,
        {"category": "documents", "document_type": "Manual", "organization": "Honda"},
    )
    for _ in range(4):
        record(conn, cue=broad, outcome="Documents/Manuals", settled=True)
        record(conn, cue=narrow, outcome="Documents/Manuals/{organization}", settled=True)

    answer = suggest(conn, [broad, narrow])

    assert answer is not None
    assert answer.outcome == "Documents/Manuals/{organization}"
    assert narrow.specificity > broad.specificity


# 8 — strong current evidence outranks a habit.
def test_a_catalog_identity_outranks_a_learned_destination(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    four_honda_manuals(conn, settings)

    identified = [
        *manual_evidence(),
        EvidenceEntry("openlibrary", "title", "A Manual", 0.95, status="matched"),
    ]
    proposal = stage(
        conn, settings, "known.pdf", "Documents/2026/Known.pdf", evidence=identified
    )

    assert outranked(row_for(conn, proposal))
    assert learned_suggestions(conn, [row_for(conn, proposal)]) == {}


def test_a_printed_identifier_outranks_a_learned_destination(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    four_honda_manuals(conn, settings)

    with_isbn = [*manual_evidence(), EvidenceEntry("document", "isbn", "978044", 0.95)]
    proposal = stage(
        conn, settings, "book.pdf", "Documents/2026/Book.pdf", evidence=with_isbn
    )

    assert "ISBN" in outranked(row_for(conn, proposal))
    assert learned_suggestions(conn, [row_for(conn, proposal)]) == {}


# 9 — a suggestion moves nothing and plans nothing.
def test_a_suggestion_creates_no_plan_and_moves_no_file(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    four_honda_manuals(conn, settings)
    proposal = stage(conn, settings, "pilot.pdf", "Documents/2026/Pilot.pdf")
    plans_before = conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0]

    assert learned_suggestions(conn, [row_for(conn, proposal)])

    assert conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == plans_before
    assert (settings.inbox_dir / "pilot.pdf").exists()
    assert not (settings.library_dir / "Documents/Manuals/Honda Motor Co/pilot.pdf").exists()
    status = conn.execute(
        "SELECT status FROM proposals WHERE id=?", (proposal,)
    ).fetchone()[0]
    assert status == "proposed"


# 10 — accepting a suggestion is the ordinary edit, still unapproved.
def test_accepting_a_suggestion_only_edits_the_proposal(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    four_honda_manuals(conn, settings)
    proposal = stage(conn, settings, "pilot.pdf", "Documents/2026/Pilot.pdf")

    response = client.post(
        f"/review/proposals/{proposal}/use-suggestion",
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )

    assert response.status_code == 200
    row = conn.execute(
        "SELECT dest_relpath, status FROM proposals WHERE id=?", (proposal,)
    ).fetchone()
    assert row["dest_relpath"] == "Documents/Manuals/Honda Motor Co/Pilot.pdf"
    assert row["status"] == "proposed"
    assert (settings.inbox_dir / "pilot.pdf").exists()


# 11/12 — a decision counts once it completes, and not before.
def test_only_committed_decisions_count(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    for index in range(4):
        decide(
            conn, settings, f"m{index}.pdf",
            f"Documents/Manuals/Honda Motor Co/M{index}.pdf", commit=False,
        )

    proposal = stage(conn, settings, "pilot.pdf", "Documents/2026/Pilot.pdf")

    #  Approved and never committed. Nothing moved, so nothing was decided in
    #  the only sense that matters to a file.
    assert learned_suggestions(conn, [row_for(conn, proposal)]) == {}
    events = conn.execute("SELECT COUNT(*) FROM decision_events").fetchone()[0]
    assert events > 0


def test_a_failed_commit_teaches_nothing(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    proposal = stage(conn, settings, "gone.pdf", "Documents/Manuals/Honda Motor Co/G.pdf")
    apply_review_action(conn, "approve", ReviewFilters(), proposal_ids=[proposal])
    plan = create_plan(
        conn,
        [OperationSpec("move", "gone.pdf", "library", "Documents/Manuals/Honda Motor Co/G.pdf")],
        settings,
    )
    approve_plan(conn, plan, settings)
    (settings.inbox_dir / "gone.pdf").unlink()

    execute_plan(conn, plan, settings)

    settled = conn.execute(
        "SELECT COUNT(*) FROM decision_events WHERE settled_at IS NOT NULL"
    ).fetchone()[0]
    assert settled == 0


# 13 — Undo takes the lesson back.
def test_undo_withdraws_the_evidence(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    four_honda_manuals(conn, settings)
    proposal = stage(conn, settings, "check.pdf", "Documents/2026/Check.pdf")
    assert learned_suggestions(conn, [row_for(conn, proposal)])

    #  Two of the four put back. Two remain, which is below the threshold.
    for index in range(2):
        plan = conn.execute(
            "SELECT plan_id FROM history WHERE src_relpath=? LIMIT 1",
            (f"manual-{index}.pdf",),
        ).fetchone()[0]
        undo_plan(conn, plan, settings)

    assert learned_suggestions(conn, [row_for(conn, proposal)]) == {}


# 14/15 — overriding records a departure, and enough of them retire the answer.
def test_overrides_accumulate_and_retire_a_suggestion(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    acme = manual_evidence("Acme")
    for index in range(4):
        decide(
            conn, settings, f"a{index}.pdf",
            f"Documents/Manuals/Acme/A{index}.pdf", evidence=acme,
        )
    proposal = stage(conn, settings, "probe.pdf", "Documents/Misc/P.pdf", evidence=acme)
    assert learned_suggestions(conn, [row_for(conn, proposal)])[proposal]["support"] == 4

    #  The owner starts filing them somewhere else instead.
    for index in range(2):
        decide(
            conn, settings, f"c{index}.pdf",
            f"Documents/Archive/Acme/C{index}.pdf", evidence=acme,
        )

    found = learned_suggestions(conn, [row_for(conn, proposal)])

    #  Four against two is no longer more than twice as many, so LibrAIry stops
    #  claiming there is a usual answer rather than clinging to the old one.
    assert found == {}


# 16 — a suppressed pattern stays quiet.
def test_a_suppressed_pattern_is_not_offered(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    four_honda_manuals(conn, settings)
    proposal = stage(conn, settings, "pilot.pdf", "Documents/2026/Pilot.pdf")
    found = learned_suggestions(conn, [row_for(conn, proposal)])[proposal]

    response = client.post(
        "/review/suggestions/suppress",
        data={"proposal_id": str(proposal)},
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )

    assert response.status_code == 200
    assert learned_suggestions(conn, [row_for(conn, proposal)]) == {}
    #  The decisions behind it are still what happened.
    assert conn.execute("SELECT COUNT(*) FROM decision_events").fetchone()[0] > 0
    restore(conn, found["signature"])
    assert learned_suggestions(conn, [row_for(conn, proposal)])


# 18 — a year in a destination is a placeholder, never a literal.
def test_a_dated_destination_is_learned_as_a_template(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    statements = [
        EvidenceEntry("heuristic", "category", "document extension", 0.88),
        EvidenceEntry("document", "type", "Financial", 0.85),
        EvidenceEntry("document", "organization", "Chase", 0.85),
    ]
    for index in range(4):
        decide(
            conn, settings, f"s{index}.pdf",
            f"Documents/Financial/2024/S{index}.pdf", evidence=statements,
        )

    proposal = stage(
        conn, settings, "new.pdf", "Documents/Financial/2026/New.pdf", evidence=statements
    )
    row = row_for(conn, proposal)
    stored = {
        str(event["outcome"])
        for event in conn.execute("SELECT DISTINCT outcome FROM decision_events")
    }

    assert stored == {"Documents/Financial/{year}"}
    #  And the 2026 statement is not offered the 2024 drawer.
    assert learned_suggestions(conn, [row]) == {}
    assert render("Documents/Financial/{year}", {"year": "2026"}) == "Documents/Financial/2026"


# 19 — nothing a document says is stored.
def test_no_document_text_reaches_a_pattern(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    private = [
        EvidenceEntry("heuristic", "category", "document extension", 0.88),
        EvidenceEntry("document", "type", "Financial", 0.85),
        EvidenceEntry("document", "organization", "Chase", 0.85),
        EvidenceEntry("document", "text", "Account 3324551 balance $18,204.11", 0.9),
    ]
    decide(conn, settings, "stmt.pdf", "Documents/Financial/2024/S.pdf", evidence=private)

    stored = " ".join(
        str(row["features"]) + str(row["outcome"])
        for row in conn.execute("SELECT features, outcome FROM decision_events")
    )

    assert "3324551" not in stored
    assert "18,204" not in stored
    assert "Account" not in stored
    assert "Chase" in stored


# 20 — no network, no model, anywhere in the path.
def test_learning_needs_no_network_or_model() -> None:
    """Structural, so adding a model later has to be a deliberate act.

    Read off the imports rather than the prose: the module *says* it uses no
    embeddings, and a test that greps the file finds that sentence and passes
    for the wrong reason.
    """
    import ast
    import inspect

    import librairy.decision_cues as cues_module
    import librairy.decisions as module

    imported: set[str] = set()
    for source in (inspect.getsource(module), inspect.getsource(cues_module)):
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])

    for banned in ("httpx", "requests", "urllib", "socket", "openai", "torch", "numpy"):
        assert banned not in imported
    assert imported <= {"json", "sqlite3", "collections", "dataclasses", "pathlib",
                        "librairy", "__future__"}


# 22 — an unrelated manual does not inherit somebody else's folder.
def test_a_different_manufacturer_does_not_inherit_the_pattern(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    four_honda_manuals(conn, settings)

    netgear = manual_evidence("Netgear")
    proposal = stage(
        conn, settings, "router.pdf", "Documents/2026/Router.pdf", evidence=netgear
    )
    found = learned_suggestions(conn, [row_for(conn, proposal)])

    #  The broad pattern generalised to `Documents/Manuals/{organization}`, so
    #  what carries over is the *shape*. Netgear's manual is never offered
    #  Honda's folder.
    assert found[proposal]["folder"] == "Documents/Manuals/Netgear"
    assert "Honda" not in found[proposal]["folder"]


# 25/26 — an explicit setting stays authoritative, and no shadow rule appears.
def test_a_configured_preference_is_not_duplicated_as_a_learned_rule(
    tmp_path: Path,
) -> None:
    conn = connect(settings_for(tmp_path))

    for _ in range(5):
        record_representation(
            conn, category="Music", formats=["flac", "mp3"], kept=["mp3"], settled=True
        )
    patterns = learned(conn)

    #  It is recorded as a representation decision and stays one. Nothing here
    #  writes `music.preferred_format`, and nothing reads this back as a
    #  second, competing preference.
    assert [item["kind"] for item in patterns] == [REPRESENTATION]
    from librairy.format_preference import preferred

    assert preferred(conn) == "mp3"


def test_keeping_both_is_its_own_kind_of_answer(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path))

    for _ in range(3):
        record_representation(
            conn, category="Books", formats=["epub", "pdf"], kept=["epub", "pdf"]
        )

    kinds = {row["kind"] for row in conn.execute("SELECT kind FROM decision_events")}
    assert kinds == {ALLOWED}
    #  "I want both" completes when it is said — there is no plan to wait for.
    settled = conn.execute(
        "SELECT COUNT(*) FROM decision_events WHERE settled_at IS NOT NULL"
    ).fetchone()[0]
    assert settled == 3


def test_a_photo_burst_teaches_nothing_about_format(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path))

    record_representation(
        conn, category="Photos", formats=["jpg"] * 25, kept=["jpg"] * 7, settled=True
    )

    assert conn.execute("SELECT COUNT(*) FROM decision_events").fetchone()[0] == 0


# 27 — the row explains itself.
def test_the_review_row_renders_the_explanation(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    four_honda_manuals(conn, settings)
    stage(conn, settings, "pilot.pdf", "Documents/2026/Pilot.pdf")

    page = client.get("/review")
    flat = " ".join(page.text.split())

    assert "FROM YOUR DECISIONS" in flat
    assert "Suggested from 4 previous decisions" in flat
    assert "Co.." not in flat
    assert "Documents/Manuals/Honda Motor Co/" in flat
    assert "Use this suggestion" in flat


def test_the_learned_page_lists_patterns_and_can_turn_one_off(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    four_honda_manuals(conn, settings)

    page = client.get("/review/learned")
    flat = " ".join(page.text.split())
    signature = [item["signature"] for item in learned(conn)][0]
    client.post(
        "/review/learned/suppress",
        data={"signature": signature},
        headers={"x-csrf-token": client.cookies["csrf_token"]},
        follow_redirects=True,
    )

    assert "What LibrAIry has learned" in flat
    assert "Documents/Manuals/{organization}" in flat
    assert "4 confirmations" in flat
    assert "not being suggested" in " ".join(client.get("/review/learned").text.split())


# 29 — a suggestion does not make an unresolved choice approvable.
def test_a_suggestion_does_not_change_what_may_be_approved(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    four_honda_manuals(conn, settings)
    proposal = stage(conn, settings, "pilot.pdf", "Documents/2026/Pilot.pdf")

    before = conn.execute(
        "SELECT status, confidence FROM proposals WHERE id=?", (proposal,)
    ).fetchone()
    learned_suggestions(conn, [row_for(conn, proposal)])
    after = conn.execute(
        "SELECT status, confidence FROM proposals WHERE id=?", (proposal,)
    ).fetchone()

    assert (before["status"], before["confidence"]) == (after["status"], after["confidence"])


def test_a_suggestion_agreeing_with_the_guess_is_not_shown(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    four_honda_manuals(conn, settings)

    #  LibrAIry already proposes the learned folder. Saying "you usually file
    #  these exactly where this is going" is furniture.
    proposal = stage(
        conn, settings, "pilot.pdf", "Documents/Manuals/Honda Motor Co/Pilot.pdf"
    )

    assert learned_suggestions(conn, [row_for(conn, proposal)]) == {}


def test_generalize_and_render_round_trip() -> None:
    template = generalize(
        "Documents/Manuals/Honda Motor Co", {"organization": "Honda Motor Co"}
    )

    assert template == "Documents/Manuals/{organization}"
    assert render(template, {"organization": "Netgear"}) == "Documents/Manuals/Netgear"
    #  A placeholder this file cannot fill has no answer, and half a path is
    #  not a destination.
    assert render(template, {}) == ""


def test_cues_and_outcome_are_read_from_recorded_evidence(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    proposal = stage(
        conn, settings, "m.pdf", "Documents/Manuals/Honda Motor Co/M.pdf"
    )
    row = row_for(conn, proposal)

    ladder = cues_for(row)

    assert [cue.specificity for cue in ladder] == sorted(
        [cue.specificity for cue in ladder], reverse=True
    )
    assert outcome_for(row) == "Documents/Manuals/{organization}"


def test_settle_marks_only_unsettled_events(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path))
    cue = Cue(DESTINATION, {"category": "documents"})
    conn.execute(
        "INSERT INTO items(root, relpath, size, mtime_ns, fingerprint, state,"
        " first_seen_at, last_seen_at) VALUES ('inbox','a.pdf',1,1,'f','proposed','n','n')"
    )
    record(conn, cue=cue, outcome="Documents", item_id=1)
    conn.execute("INSERT INTO plans(id, status, created_at) VALUES ('p','done','now')")

    settle(conn, 1, "p")
    settle(conn, 1, "other")

    rows = conn.execute("SELECT plan_id FROM decision_events").fetchall()
    assert [row["plan_id"] for row in rows] == ["p"]


# 31-34 — the lookup stays bounded as history grows.
@pytest.mark.parametrize("history", [1_000, 10_000])
def test_the_lookup_is_bounded_against_a_large_history(
    tmp_path: Path, history: int
) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    conn.executemany(
        "INSERT INTO decision_events(kind, signature, specificity, features,"
        " outcome, decided_at, settled_at)"
        " VALUES ('destination', ?, 2, '{}', ?, 'now', 'now')",
        [
            (f"destination|category=documents&document_type=t{index % 400}",
             f"Documents/T{index % 400}")
            for index in range(history)
        ],
    )
    rows = [
        row_for(conn, stage(conn, settings, f"f{index}.pdf", f"Documents/2026/F{index}.pdf"))
        for index in range(50)
    ]

    started = time.perf_counter()
    learned_suggestions(conn, rows)
    elapsed = time.perf_counter() - started

    assert elapsed < 2.0


def test_a_page_of_rows_asks_history_once(tmp_path: Path) -> None:
    """The structural half of the scale claim.

    A timing that looks fine on a laptop with a small journal is not evidence
    that the lookup is bounded; counting the statements is. Fifty rows must
    cost one question about decision history, not fifty.
    """
    settings = settings_for(tmp_path)
    conn = connect(settings)
    four_honda_manuals(conn, settings)
    rows = [
        row_for(conn, stage(conn, settings, f"p{index}.pdf", f"Documents/2026/P{index}.pdf"))
        for index in range(50)
    ]

    seen: list[str] = []
    conn.set_trace_callback(seen.append)
    try:
        found = learned_suggestions(conn, rows)
    finally:
        conn.set_trace_callback(None)

    assert len(found) == 50
    history = [sql for sql in seen if "decision_events" in sql]
    assert len(history) == 1, history
