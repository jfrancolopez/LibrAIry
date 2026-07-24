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
    assert "ok</span> → dupe.txt" in response.text
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
    assert "Quarantine is reversible" in response.text
    assert "copy.txt" in response.text
    assert "original inbox:dupe.txt" in response.text
    assert "needs human judgment" in response.text
    assert "score 0.91" in response.text
    assert "delete" not in response.text.lower()


def test_staged_quarantine_approve_and_unstage_actions(tmp_path: Path) -> None:
    client, conn, _ = client_for(tmp_path)
    proposal_id = seed_staged_quarantine(conn)

    approve = client.post(
        f"/quarantine/staged/{proposal_id}/approve",
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )
    assert approve.status_code == 200
    assert "approved" in approve.text
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
    assert "unstaged" in unstage.text
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
