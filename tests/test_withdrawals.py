"""Decisions taken back, and the account of them.

Withdrawing an approval has always been possible and has never been visible.
That mattered less while it was rare; now that two waiting decisions can be
found to contradict each other, the program actively asks people to send one
back — so "what did I withdraw, and why" is a question it has to be able to
answer.

Two things it must keep straight. A withdrawal is not a move, so it does not go
in the journal and there is nothing to undo. And a withdrawal is not a completed
decision, so it teaches Decision Memory nothing — making them visible must not
quietly turn them into evidence.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from librairy import withdrawals
from librairy.config import Settings
from librairy.db import connect
from librairy.planner import OperationSpec, approve_plan, create_plan, utc_now
from librairy.scanner import scan_root
from librairy.web.app import create_app


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


def flat(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def library_file(settings: Settings, relpath: str, body: bytes = b"a file") -> Path:
    path = settings.library_dir / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


def approved(
    conn: sqlite3.Connection, settings: Settings, src: str, dest: str
) -> str:
    plan_id = create_plan(
        conn,
        [
            OperationSpec(
                op_type="move",
                src_root="library",
                src_relpath=src,
                dest_root="library",
                dest_relpath=dest,
            )
        ],
        settings,
    )
    conn.execute("UPDATE plans SET coherent=1 WHERE id=?", (plan_id,))
    approve_plan(conn, plan_id, settings)
    return plan_id


def withdraw(conn: sqlite3.Connection, plan_id: str, *, source: str) -> None:
    withdrawals.record(conn, plan_id, source=source)
    conn.execute("DELETE FROM plan_ops WHERE plan_id=?", (plan_id,))
    conn.execute("DELETE FROM plans WHERE id=?", (plan_id,))


# --------------------------------------------------------------------------
# 21-23: it is visible, and it is not the journal
# --------------------------------------------------------------------------


def test_a_withdrawn_decision_is_visible(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    library_file(settings, "Music/song.flac")
    scan_root(conn, "library", settings.library_dir, settings)
    withdraw(
        conn,
        approved(conn, settings, "Music/song.flac", "Music/Queen/song.flac"),
        source=withdrawals.SENT_BACK,
    )

    page = flat(client.get("/history?view=withdrawn").text)

    assert "song.flac" in page
    assert "Sent back to Review" in page
    assert withdrawals.total(conn) == 1


def test_the_journal_of_completed_moves_stays_separate(tmp_path: Path) -> None:
    """A withdrawal moved nothing, so putting it in the journal would claim a
    move that never happened and put a row in front of Undo that Undo cannot
    reverse."""
    client, conn, settings = client_for(tmp_path)
    library_file(settings, "Music/song.flac")
    scan_root(conn, "library", settings.library_dir, settings)
    withdraw(
        conn,
        approved(conn, settings, "Music/song.flac", "Music/Queen/song.flac"),
        source=withdrawals.SENT_BACK,
    )

    response = client.get("/history")
    completed = re.sub(r"<[^>]+>", " ", response.text)
    completed = re.sub(r"\s+", " ", completed)

    assert conn.execute("SELECT COUNT(*) FROM history").fetchone()[0] == 0
    assert "song.flac" not in completed
    #  Counted on the tab, and nowhere in the journal itself.
    assert "Withdrawn 1" in completed
    assert 'href="/history?view=withdrawn"' in response.text


def test_every_withdrawal_says_that_nothing_moved(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    library_file(settings, "Music/song.flac")
    scan_root(conn, "library", settings.library_dir, settings)
    withdraw(
        conn,
        approved(conn, settings, "Music/song.flac", "Music/Queen/song.flac"),
        source=withdrawals.SENT_BACK,
    )
    before = sorted(
        path.name for path in settings.library_dir.rglob("*") if path.is_file()
    )

    page = flat(client.get("/history?view=withdrawn").text)

    assert "No files moved." in page
    assert (
        sorted(path.name for path in settings.library_dir.rglob("*") if path.is_file())
        == before
    )


# --------------------------------------------------------------------------
# 24-27: what is recorded, and what is not invented
# --------------------------------------------------------------------------


def test_sending_back_and_cancelling_are_told_apart(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    library_file(settings, "Music/one.flac", b"one")
    library_file(settings, "Music/two.flac", b"two")
    scan_root(conn, "library", settings.library_dir, settings)
    withdraw(
        conn,
        approved(conn, settings, "Music/one.flac", "Music/A/one.flac"),
        source=withdrawals.SENT_BACK,
    )
    withdraw(
        conn,
        approved(conn, settings, "Music/two.flac", "Music/B/two.flac"),
        source=withdrawals.CANCELLED,
    )

    actions = {row.action for row in withdrawals.listing(conn)}
    page = flat(client.get("/history?view=withdrawn").text)

    assert actions == {"Sent back to Review", "Request cancelled"}
    assert "Request cancelled" in page


def test_a_withdrawal_that_recorded_no_reason_does_not_get_one_invented(
    tmp_path: Path,
) -> None:
    """Working out a motive afterwards from the current state is
    reconstruction, and a page that says "withdrawn to resolve a conflict"
    about one that had nothing to do with a conflict is worse than silence."""
    client, conn, settings = client_for(tmp_path)
    library_file(settings, "Music/song.flac")
    scan_root(conn, "library", settings.library_dir, settings)
    plan_id = approved(conn, settings, "Music/song.flac", "Music/Queen/song.flac")
    #  A database written before withdrawals recorded anything about why.
    conn.execute(
        "INSERT INTO plan_withdrawals(plan_id, relpath, op_count, withdrawn_at)"
        " VALUES (?, 'Music/song.flac', 1, ?)",
        (plan_id, utc_now()),
    )

    row = withdrawals.listing(conn)[0]
    page = flat(client.get("/history?view=withdrawn").text)

    assert row.action == "Withdrawn"
    assert row.reason == ""
    assert row.conflicted_with == ""
    assert "conflicted with" not in page


def test_an_outdated_approval_is_not_recorded_as_a_withdrawal(
    tmp_path: Path,
) -> None:
    """A decision that went stale is still waiting. Nobody withdrew it, and
    calling that a user cancellation would be putting words in their mouth."""
    from librairy.correction_state import plan_drift

    _, conn, settings = client_for(tmp_path)
    library_file(settings, "Music/song.flac", b"the approved bytes")
    scan_root(conn, "library", settings.library_dir, settings)
    plan_id = approved(conn, settings, "Music/song.flac", "Music/Queen/song.flac")
    library_file(settings, "Music/song.flac", b"somebody else wrote this")
    scan_root(conn, "library", settings.library_dir, settings)

    assert plan_drift(conn, settings, plan_id) != ""
    assert withdrawals.total(conn) == 0
    assert (
        conn.execute("SELECT status FROM plans WHERE id=?", (plan_id,)).fetchone()[
            "status"
        ]
        == "approved"
    )


def test_a_withdrawal_that_resolved_a_conflict_names_it(tmp_path: Path) -> None:
    """Captured while it is still true. Once the plan is gone the collision
    goes with it, and saying afterwards what it resolved would be
    reconstruction."""
    from librairy.lifecycle import transition_item
    from librairy.proposals import upsert_proposal
    from librairy.web.review import ReviewFilters, apply_review_action

    client, conn, settings = client_for(tmp_path)
    library_file(settings, "Music/loose.flac", b"a recording")
    (settings.inbox_dir / "arriving.flac").write_bytes(b"a different recording")
    scan_root(conn, "library", settings.library_dir, settings)
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    destination = "Music/Queen/song.flac"
    plan_id = approved(conn, settings, "Music/loose.flac", destination)

    arriving = int(
        conn.execute(
            "SELECT id FROM items WHERE root='inbox' AND relpath='arriving.flac'"
        ).fetchone()["id"]
    )
    proposal = upsert_proposal(
        conn,
        item_id=arriving,
        category="music",
        clean_name="song.flac",
        dest_relpath=destination,
        confidence=0.9,
        evidence=[],
    )
    transition_item(conn, arriving, "proposed")
    apply_review_action(conn, "approve", ReviewFilters(), proposal_ids=[proposal])

    withdraw(conn, plan_id, source=withdrawals.SENT_BACK)

    row = withdrawals.listing(conn)[0]
    page = flat(client.get("/history?view=withdrawn").text)
    assert "conflicted with filing arriving.flac" in row.reason
    assert "conflicted with filing arriving.flac" in page


# --------------------------------------------------------------------------
# 28-30: what it must not become
# --------------------------------------------------------------------------


def test_a_withdrawn_decision_teaches_nothing(tmp_path: Path) -> None:
    """Decision Memory learns from decisions that completed. A withdrawal is
    the opposite of one, and making it visible must not turn it into
    evidence."""
    from librairy.decisions import learned, tally

    _, conn, settings = client_for(tmp_path)
    library_file(settings, "Music/song.flac")
    scan_root(conn, "library", settings.library_dir, settings)
    for index in range(4):
        library_file(settings, f"Music/t{index}.flac", f"body {index}".encode())
    scan_root(conn, "library", settings.library_dir, settings)
    for index in range(4):
        plan_id = approved(
            conn, settings, f"Music/t{index}.flac", f"Music/Queen/t{index}.flac"
        )
        conn.execute(
            "INSERT INTO decision_events(kind, signature, specificity, features,"
            " outcome, item_id, plan_id, dest_relpath, decided_at) VALUES"
            " ('destination', 'music|flac', 1, '{}', 'Music/Queen', NULL, ?, ?, ?)",
            (plan_id, f"Music/Queen/t{index}.flac", utc_now()),
        )
        withdraw(conn, plan_id, source=withdrawals.SENT_BACK)

    assert withdrawals.total(conn) == 4
    #  Four of them, which is over the threshold — and none of them completed.
    assert tally(conn, ["music|flac"]) == {}
    assert learned(conn) == []


def test_withdrawing_a_decision_that_recorded_a_choice_does_not_crash(
    tmp_path: Path,
) -> None:
    """A comparison records its representation choice against the plan, and
    `decision_events.plan_id` is a foreign key — so deleting the withdrawn plan
    raised `FOREIGN KEY constraint failed` and the whole withdrawal failed on a
    table that has nothing to do with withdrawing.

    The event is kept and the link is dropped. The person really did make that
    choice; what stopped being true is that a plan would carry it out.
    """
    _, conn, settings = client_for(tmp_path)
    library_file(settings, "Music/song.flac")
    scan_root(conn, "library", settings.library_dir, settings)
    plan_id = approved(conn, settings, "Music/song.flac", "Music/Queen/song.flac")
    conn.execute(
        "INSERT INTO decision_events(kind, signature, specificity, features,"
        " outcome, plan_id, dest_relpath, decided_at) VALUES"
        " ('representation', 'sig', 1, '{}', 'keep', ?, ?, ?)",
        (plan_id, "Music/Queen/song.flac", utc_now()),
    )

    withdraw(conn, plan_id, source=withdrawals.SENT_BACK)

    row = conn.execute("SELECT plan_id, settled_at FROM decision_events").fetchone()
    assert row["plan_id"] is None
    assert row["settled_at"] is None
    assert withdrawals.total(conn) == 1


def test_the_view_is_bounded(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    for index in range(120):
        conn.execute(
            "INSERT INTO plan_withdrawals(plan_id, relpath, op_count, withdrawn_at,"
            " source) VALUES (?, ?, 1, ?, ?)",
            (f"plan-{index}", f"Music/t{index}.flac", utc_now(), withdrawals.SENT_BACK),
        )

    rows = withdrawals.listing(conn)
    page = client.get("/history?view=withdrawn")

    assert len(rows) == withdrawals.PAGE_SIZE
    assert withdrawals.total(conn) == 120
    assert "Next" in page.text


def test_nothing_offers_to_reopen_a_withdrawn_decision(tmp_path: Path) -> None:
    """A decision withdrawn a month ago may name files that have since moved.
    Reinstating it would be approving something nobody has looked at."""
    client, conn, settings = client_for(tmp_path)
    library_file(settings, "Music/song.flac")
    scan_root(conn, "library", settings.library_dir, settings)
    withdraw(
        conn,
        approved(conn, settings, "Music/song.flac", "Music/Queen/song.flac"),
        source=withdrawals.SENT_BACK,
    )

    page = flat(client.get("/history?view=withdrawn").text).lower()

    for banned in ("re-open", "reopen", "reinstate", "restore this decision", "approve again"):
        assert banned not in page
    #  No control at all on a withdrawal row. The only form on the page is the
    #  site header's log out.
    body = client.get("/history?view=withdrawn").text
    assert body.count("<form") == 1
    assert "/logout" in body
