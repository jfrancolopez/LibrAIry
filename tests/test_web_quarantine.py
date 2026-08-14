from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from librairy.config import Settings
from librairy.db import connect
from librairy.executor import execute_plan
from librairy.models import EvidenceEntry
from librairy.planner import approve_plan, create_plan
from librairy.proposals import upsert_proposal
from librairy.quarantine import quarantine_operation
from librairy.scanner import scan_root
from librairy.web.app import create_app


def client_for(tmp_path: Path) -> tuple[TestClient, object, Settings]:
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        FILE_STABILITY_SECONDS=0,
        _env_file=None,
    )
    settings.inbox_dir.mkdir()
    settings.library_dir.mkdir()
    settings.quarantine_dir.mkdir()
    conn = connect(settings)
    client = TestClient(create_app(settings, conn))
    client.post("/setup", data={"password": "correct horse battery"})
    return client, conn, settings


def test_quarantine_restore_round_trips_file_and_journals(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    entry_id = seed_executed_quarantine(conn, settings)

    response = client.post(
        f"/quarantine/restore/{entry_id}",
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )

    assert response.status_code == 200
    assert "Done</span>" in response.text and "dupe.txt" in response.text
    assert (settings.inbox_dir / "dupe.txt").read_text(encoding="utf-8") == "dupe"
    assert not (settings.quarantine_dir / "2026-07-22/dupe.txt").exists()
    assert (
        conn.execute("SELECT action FROM history ORDER BY id DESC LIMIT 1").fetchone()[0]
        == "restore_quarantine"
    )


def test_quarantine_screen_lists_staged_and_similar_flags_without_delete(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    seed_staged_quarantine(conn)
    seed_similar_flag(conn, settings)
    seed_executed_quarantine(conn, settings)

    response = client.get("/quarantine")

    assert response.status_code == 200
    assert "LibrAIry never deletes anything" in response.text
    assert "copy.txt" in response.text
    assert "inbox:dupe.txt" in response.text or "dupe.txt" in response.text
    assert "your call" in response.text.lower()
    assert "similarity 0.91" in response.text
    # The page now tells you where to go and delete things yourself, so the
    # invariant is "no control that deletes", not "the word never appears".
    for control in ("hx-delete", 'action="/quarantine/delete', "/quarantine/purge"):
        assert control not in response.text


def test_staged_quarantine_approve_and_unstage_actions(tmp_path: Path) -> None:
    client, conn, _ = client_for(tmp_path)
    proposal_id = seed_staged_quarantine(conn)

    approve = client.post(
        f"/quarantine/staged/{proposal_id}/approve",
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )
    assert approve.status_code == 200
    assert "Approved. It moves out on the next commit" in approve.text
    assert proposal_status(conn, proposal_id) == "approved"

    conn.execute("UPDATE proposals SET status='proposed' WHERE id=?", (proposal_id,))
    conn.execute(
        """
        UPDATE items SET state='quarantine-proposed'
        WHERE id=(SELECT item_id FROM proposals WHERE id=?)
        """,
        (proposal_id,),
    )
    unstage = client.post(
        f"/quarantine/staged/{proposal_id}/unstage",
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )

    assert unstage.status_code == 200
    assert "Kept — it will be filed normally" in unstage.text
    row = conn.execute(
        "SELECT action, dest_root FROM proposals WHERE id=?", (proposal_id,)
    ).fetchone()
    assert dict(row) == {"action": "move", "dest_root": "library"}


def seed_executed_quarantine(conn, settings: Settings) -> int:
    (settings.inbox_dir / "dupe.txt").write_text("dupe", encoding="utf-8")
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    plan_id = create_plan(conn, [quarantine_operation("dupe.txt", date="2026-07-22")], settings)
    approve_plan(conn, plan_id, settings)
    execute_plan(conn, plan_id, settings)
    return int(conn.execute("SELECT id FROM quarantine_entries").fetchone()[0])


def seed_staged_quarantine(conn) -> int:
    item_id = insert_item(conn, "copy.txt", "quarantine-proposed")
    return upsert_proposal(
        conn,
        item_id=item_id,
        category="documents",
        clean_name="copy.txt",
        dest_relpath="2026-07-22/copy.txt",
        confidence=0.99,
        evidence=[EvidenceEntry("heuristic", "duplicate", "same fingerprint", 0.99)],
        action="quarantine",
        dest_root="quarantine",
    )


def seed_similar_flag(conn, settings: Settings) -> None:
    (settings.inbox_dir / "left.jpg").write_text("left", encoding="utf-8")
    (settings.inbox_dir / "right.jpg").write_text("right", encoding="utf-8")
    left = insert_item(conn, "left.jpg", "proposed", size=4)
    right = insert_item(conn, "right.jpg", "proposed", size=5)
    conn.execute(
        """
        INSERT INTO similar_media_flags(item_id, similar_item_id, kind, score, created_at)
        VALUES (?, ?, 'image', 0.91, 'now')
        """,
        (left, right),
    )


def insert_item(conn, relpath: str, state: str, size: int = 1) -> int:
    cursor = conn.execute(
        """
        INSERT INTO items(
          root, relpath, size, mtime_ns, fingerprint, state, first_seen_at, last_seen_at
        )
        VALUES ('inbox', ?, ?, 1, ?, ?, 'now', 'now')
        """,
        (relpath, size, relpath, state),
    )
    return int(cursor.lastrowid)


def proposal_status(conn, proposal_id: int) -> str:
    return conn.execute("SELECT status FROM proposals WHERE id=?", (proposal_id,)).fetchone()[0]


def test_quarantine_staged_rows_show_from_to_and_why(tmp_path: Path) -> None:
    client, conn, _ = client_for(tmp_path)
    seed_staged_quarantine(conn)

    page = client.get("/quarantine").text

    assert 'class="from-to"' in page
    assert "Why?" in page
    # Humanized, not a bracket code.
    assert "duplicate: same fingerprint" in page


def test_marking_a_held_file_for_deletion_gathers_it_without_deleting_it(
    tmp_path: Path,
) -> None:
    """The out-tray had one answer — "put it back" — so being finished with a
    file meant going through quarantine by hand in a file manager. This is the
    other answer: one folder to empty deliberately, yourself.
    """
    client, conn, settings = client_for(tmp_path)
    entry_id = seed_executed_quarantine(conn, settings)

    response = client.post(
        f"/quarantine/mark-delete/{entry_id}",
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )

    assert response.status_code == 200
    moved = settings.quarantine_dir / "_to-delete/2026-07-22/dupe.txt"
    assert moved.read_text(encoding="utf-8") == "dupe"
    assert not (settings.quarantine_dir / "2026-07-22/dupe.txt").exists()
    assert conn.execute("SELECT relpath FROM items WHERE root='quarantine'").fetchone()[0] == (
        "_to-delete/2026-07-22/dupe.txt"
    )
    assert (
        conn.execute("SELECT action FROM history ORDER BY id DESC LIMIT 1").fetchone()[0]
        == "mark_for_deletion"
    )
    assert "marked for deletion" in client.get("/quarantine").text


def test_a_file_in_the_delete_pile_can_still_be_put_back(tmp_path: Path) -> None:
    """Marked is not deleted, and the entry remembers where the file came from
    rather than where it is sitting now."""
    client, conn, settings = client_for(tmp_path)
    entry_id = seed_executed_quarantine(conn, settings)
    csrf = client.cookies["csrf_token"]

    client.post(f"/quarantine/mark-delete/{entry_id}", headers={"x-csrf-token": csrf})
    restored = client.post(f"/quarantine/restore/{entry_id}", headers={"x-csrf-token": csrf})

    assert restored.status_code == 200
    assert (settings.inbox_dir / "dupe.txt").read_text(encoding="utf-8") == "dupe"
    assert not (settings.quarantine_dir / "_to-delete/2026-07-22/dupe.txt").exists()


def test_a_staged_duplicate_can_go_straight_to_the_delete_pile(tmp_path: Path) -> None:
    """Otherwise being finished with a duplicate that has not moved yet costs
    two commits: one into quarantine, another to move it along."""
    client, conn, _ = client_for(tmp_path)
    proposal_id = seed_staged_quarantine(conn)

    response = client.post(
        f"/quarantine/staged/{proposal_id}/mark-delete",
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )

    assert response.status_code == 200
    row = conn.execute(
        "SELECT status, action, dest_root, dest_relpath FROM proposals WHERE id=?", (proposal_id,)
    ).fetchone()
    assert row["status"] == "approved"
    assert row["action"] == "quarantine"
    assert row["dest_relpath"].startswith("_to-delete/")
    assert "still not deleted" in response.text


# --- what a quarantine row says, and why -----------------------------------


def test_a_hand_quarantined_file_is_not_called_a_duplicate(tmp_path: Path) -> None:
    """Every entry recorded `exact_duplicate` regardless of why the file was
    set aside, so a file you sent here yourself from Review was described on
    this page as a byte-for-byte copy of something you already have."""
    client, conn, settings = client_for(tmp_path)
    (settings.inbox_dir / "unwanted.txt").write_text("mine", encoding="utf-8")
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    item_id = int(
        conn.execute("SELECT id FROM items WHERE relpath='unwanted.txt'").fetchone()[0]
    )
    # No duplicate evidence anywhere: this file is here because you said so.
    upsert_proposal(
        conn,
        item_id=item_id,
        category="documents",
        clean_name="unwanted.txt",
        dest_relpath="unwanted.txt",
        confidence=0.9,
        evidence=[EvidenceEntry("heuristic", "category", "you sent it here", 0.9)],
        action="quarantine",
        dest_root="quarantine",
    )
    plan_id = create_plan(conn, [quarantine_operation("unwanted.txt")], settings)
    approve_plan(conn, plan_id, settings)
    execute_plan(conn, plan_id, settings)

    reason = conn.execute("SELECT reason FROM quarantine_entries").fetchone()[0]
    body = client.get("/quarantine").text

    assert reason == "user"
    assert "byte-for-byte copy" not in body
    assert "you said you did not want it" in body
    assert "no reason recorded" not in body


def test_a_duplicate_is_still_recorded_as_one(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    (settings.inbox_dir / "dupe.txt").write_text("dupe", encoding="utf-8")
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    item_id = int(conn.execute("SELECT id FROM items WHERE relpath='dupe.txt'").fetchone()[0])
    upsert_proposal(
        conn,
        item_id=item_id,
        category="documents",
        clean_name="dupe.txt",
        dest_relpath="dupe.txt",
        confidence=0.99,
        evidence=[EvidenceEntry("heuristic", "duplicate", "exact duplicate of library:x", 0.99)],
        action="quarantine",
        dest_root="quarantine",
    )
    plan_id = create_plan(conn, [quarantine_operation("dupe.txt")], settings)
    approve_plan(conn, plan_id, settings)
    execute_plan(conn, plan_id, settings)

    assert conn.execute("SELECT reason FROM quarantine_entries").fetchone()[0] == "exact_duplicate"
    assert "byte-for-byte copy" in client.get("/quarantine").text


def test_a_moved_out_row_leads_with_the_name_and_hides_the_rest(tmp_path: Path) -> None:
    """A table of four columns could not reflow: the full relpath wrapped to
    four lines on a phone and pushed the actions off the screen."""
    client, conn, settings = client_for(tmp_path)
    seed_executed_quarantine(conn, settings)

    body = client.get("/quarantine").text

    assert "<table" not in body.split("Already moved out")[1].split("</section>")[0]
    assert 'class="qrow' in body
    # The name identifies the row; the path and the plan are behind Details.
    assert "qrow-name" in body
    assert "<summary>Details</summary>" in body
    assert "came from" in body


def test_quarantine_actions_and_delete_pile_semantics_are_unchanged(tmp_path: Path) -> None:
    """The rework is presentation. Nothing about what the buttons do moved."""
    client, conn, settings = client_for(tmp_path)
    entry_id = seed_executed_quarantine(conn, settings)

    body = client.get("/quarantine").text

    assert f"/quarantine/restore/{entry_id}" in body
    assert f"/quarantine/mark-delete/{entry_id}" in body
    assert "LibrAIry never deletes anything." in body
    assert "delete them yourself" in body
    # Marking is still a staging move into one folder, never a deletion.
    assert "It is still not deleted" in body


def test_quarantine_empty_state_says_how_files_get_here(tmp_path: Path) -> None:
    client, _conn, _settings = client_for(tmp_path)

    body = client.get("/quarantine").text

    assert "Nothing is being held." in body
    assert "Nothing has been moved out yet." in body
    assert "never on their own" in body


def test_quarantine_rows_can_show_what_the_file_actually_is(tmp_path: Path) -> None:
    """Deciding whether a file goes back is a question about what it is, and a
    UUID filename does not answer it."""
    client, conn, settings = client_for(tmp_path)
    entry_id = seed_executed_quarantine(conn, settings)
    item_id = int(
        conn.execute("SELECT item_id FROM quarantine_entries WHERE id=?", (entry_id,)).fetchone()[0]
    )

    body = client.get("/quarantine").text

    assert f'data-preview-url="/preview/items/{item_id}"' in body
    assert f'id="qpreview-{entry_id}"' in body
    # The preview card carries an expand control, so the page must carry the
    # viewer it opens — otherwise Quarantine grows the dead button Browse had.
    assert 'id="lightbox"' in body
    assert "/static/lightbox.js" in body


def test_a_staged_quarantine_can_be_previewed_before_you_decide(tmp_path: Path) -> None:
    client, conn, _settings = client_for(tmp_path)
    proposal_id = seed_staged_quarantine(conn)
    item_id = int(
        conn.execute("SELECT item_id FROM proposals WHERE id=?", (proposal_id,)).fetchone()[0]
    )

    body = client.get("/quarantine").text

    assert f'data-preview-url="/preview/items/{item_id}"' in body
    assert f'id="spreview-{proposal_id}"' in body
