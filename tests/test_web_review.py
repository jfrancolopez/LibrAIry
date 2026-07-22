from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from librairy.config import Settings
from librairy.db import connect
from librairy.models import EvidenceEntry
from librairy.proposals import upsert_proposal
from librairy.web.app import create_app


def client_for(tmp_path: Path) -> tuple[TestClient, object]:
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        _env_file=None,
    )
    conn = connect(settings)
    client = TestClient(create_app(settings, conn))
    client.post("/setup", data={"password": "correct horse battery"})
    return client, conn


def test_review_renders_groups_filters_and_htmx_pagination(tmp_path: Path) -> None:
    client, conn = client_for(tmp_path)
    album = insert_group(conn, "album", "Kind of Blue")
    event = insert_group(conn, "photo_event", "Italy")
    seed_proposal(conn, "music/a.flac", "music", "Music/A.flac", 0.95, album)
    seed_proposal(conn, "photos/a.jpg", "photos", "Photos/A.jpg", 0.75, event)
    seed_proposal(conn, "docs/a.txt", "documents", None, 0.4, None)

    page = client.get("/review")
    filtered = client.get("/review/list?category=music")

    assert "album / Kind of Blue / 1 shown" in page.text
    assert "photo_event / Italy / 1 shown" in page.text
    assert "hx-get=\"/review/list\"" in page.text
    assert "music/a.flac" in filtered.text
    assert "photos/a.jpg" not in filtered.text


def test_review_evidence_labels_cloud_marker_and_pending_edit(tmp_path: Path) -> None:
    client, conn = client_for(tmp_path)
    item = insert_item(conn, "pending.bin")
    upsert_proposal(
        conn,
        item_id=item,
        category="misc",
        clean_name="pending.bin",
        dest_relpath=None,
        confidence=0.3,
        evidence=[
            EvidenceEntry("heuristic", "category", "unknown item fallback", 0.2),
            EvidenceEntry("ai", "category", "openai/gpt-4o-mini/cloud: guessed", 0.7),
        ],
    )

    response = client.get("/review")

    assert "[HEURISTIC] category unknown item fallback 0.20" in response.text
    assert "[CLOUD AI:openai/gpt-4o-mini/cloud] category" in response.text
    assert "[WARN] pending destination" in response.text
    assert "Destination" in response.text


def test_review_large_seed_is_paginated_and_fast(tmp_path: Path) -> None:
    client, conn = client_for(tmp_path)
    evidence = [EvidenceEntry("heuristic", "category", "bulk", 0.5)]
    for index in range(5000):
        item = insert_item(conn, f"bulk/{index}.txt")
        upsert_proposal(
            conn,
            item_id=item,
            category="documents",
            clean_name=f"{index}.txt",
            dest_relpath=f"Documents/{index}.txt",
            confidence=0.5,
            evidence=evidence,
        )
    started = time.perf_counter()

    response = client.get("/review/list")
    elapsed = time.perf_counter() - started

    assert response.status_code == 200
    assert "5000 ITEMS MATCH" in response.text
    assert response.text.count("type=\"checkbox\"") == 50
    assert "Next" in response.text
    assert elapsed < 1.0


def test_batch_approve_filtered_set_only_updates_matches(tmp_path: Path) -> None:
    client, conn = client_for(tmp_path)
    music_id = seed_proposal(conn, "music/a.flac", "music", "Music/A.flac", 0.9, None)
    doc_id = seed_proposal(conn, "docs/a.txt", "documents", "Documents/A.txt", 0.9, None)

    response = client.post(
        "/review/action",
        data={"action": "approve", "all_matching": "true", "category": "music"},
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )

    assert response.status_code == 200
    assert proposal_status(conn, music_id) == "approved"
    assert item_state(conn, music_id) == "approved"
    assert proposal_status(conn, doc_id) == "proposed"


def test_reject_and_postpone_transitions_leave_default_queue(tmp_path: Path) -> None:
    client, conn = client_for(tmp_path)
    rejected_id = seed_proposal(conn, "reject.txt", "documents", "Documents/reject.txt", 0.8, None)
    postponed_id = seed_proposal(conn, "later.txt", "documents", "Documents/later.txt", 0.8, None)

    reject = client.post(
        "/review/action",
        data={"action": "reject", "proposal_id": str(rejected_id)},
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )
    postpone = client.post(
        "/review/action",
        data={"action": "postpone", "proposal_id": str(postponed_id)},
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )
    queue = client.get("/review/list")

    assert reject.status_code == 200
    assert postpone.status_code == 200
    assert proposal_status(conn, rejected_id) == "rejected"
    assert item_state(conn, rejected_id) == "pending"
    assert proposal_status(conn, postponed_id) == "postponed"
    assert item_state(conn, postponed_id) == "postponed"
    assert "reject.txt" not in queue.text
    assert "later.txt" not in queue.text


def test_review_actions_are_csrf_protected_and_keyboard_controls_render(tmp_path: Path) -> None:
    client, conn = client_for(tmp_path)
    proposal_id = seed_proposal(conn, "a.txt", "documents", "Documents/a.txt", 0.9, None)

    blocked = client.post(
        "/review/action", data={"action": "approve", "proposal_id": str(proposal_id)}
    )
    page = client.get("/review")

    assert blocked.status_code == 403
    assert proposal_status(conn, proposal_id) == "proposed"
    assert "Approve Selected" in page.text
    assert "aria-label=\"select a.txt\"" in page.text


@pytest.mark.parametrize(
    "dest_relpath",
    ["../../etc/x", "/tmp/x", "Documents\\x.txt", "Documents/bad\x00.txt", "Documents/{token}.txt"],
)
def test_edit_rejects_hostile_destinations_without_saving(
    tmp_path: Path, dest_relpath: str
) -> None:
    client, conn = client_for(tmp_path)
    proposal_id = seed_proposal(conn, "a.txt", "documents", "Documents/a.txt", 0.9, None)

    response = client.post(
        f"/review/proposals/{proposal_id}/edit",
        data={
            "category": "documents",
            "clean_name": "safe.txt",
            "dest_relpath": dest_relpath,
        },
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )

    row = conn.execute(
        "SELECT clean_name, dest_relpath FROM proposals WHERE id=?", (proposal_id,)
    ).fetchone()
    assert response.status_code == 422
    assert row["clean_name"] == "a.txt"
    assert row["dest_relpath"] == "Documents/a.txt"


def test_edit_suffixes_existing_file_and_live_proposal_collisions(tmp_path: Path) -> None:
    client, conn = client_for(tmp_path)
    settings = client.app.state.settings
    (settings.library_dir / "Documents").mkdir(parents=True)
    (settings.library_dir / "Documents/a.txt").write_text("existing", encoding="utf-8")
    first = seed_proposal(conn, "a.txt", "documents", "Documents/old.txt", 0.9, None)
    second = seed_proposal(conn, "b.txt", "documents", "Documents/b.txt", 0.9, None)

    first_response = client.post(
        f"/review/proposals/{first}/edit",
        data={"category": "documents", "clean_name": "a.txt", "dest_relpath": "Documents/a.txt"},
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )
    second_response = client.post(
        f"/review/proposals/{second}/edit",
        data={"category": "documents", "clean_name": "a.txt", "dest_relpath": "Documents/a.txt"},
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )

    assert "collision suffix applied" in first_response.text
    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert proposal_dest(conn, first) == "Documents/a (2).txt"
    assert proposal_dest(conn, second) == "Documents/a (3).txt"


def test_edit_route_does_not_mutate_filesystem(tmp_path: Path) -> None:
    client, conn = client_for(tmp_path)
    settings = client.app.state.settings
    settings.library_dir.mkdir(parents=True, exist_ok=True)
    before = sorted(
        path.relative_to(settings.library_dir) for path in settings.library_dir.rglob("*")
    )
    proposal_id = seed_proposal(conn, "a.txt", "documents", "Documents/a.txt", 0.9, None)

    response = client.post(
        f"/review/proposals/{proposal_id}/edit",
        data={"category": "documents", "clean_name": "b.txt", "dest_relpath": "Documents/b.txt"},
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )
    after = sorted(
        path.relative_to(settings.library_dir) for path in settings.library_dir.rglob("*")
    )

    assert response.status_code == 200
    assert before == after


def seed_proposal(
    conn,
    relpath: str,
    category: str,
    dest_relpath: str | None,
    confidence: float,
    group_id: int | None,
) -> int:
    item = insert_item(conn, relpath)
    proposal_id = upsert_proposal(
        conn,
        item_id=item,
        category=category,
        clean_name=Path(relpath).name,
        dest_relpath=dest_relpath,
        confidence=confidence,
        group_id=group_id,
        evidence=[EvidenceEntry("heuristic", "category", category, confidence)],
    )
    conn.execute("UPDATE items SET state='proposed' WHERE id=?", (item,))
    return proposal_id


def insert_item(conn, relpath: str) -> int:
    cursor = conn.execute(
        """
        INSERT INTO items(root, relpath, size, mtime_ns, fingerprint, first_seen_at, last_seen_at)
        VALUES ('inbox', ?, 1, 1, ?, 'now', 'now')
        """,
        (relpath, relpath),
    )
    return int(cursor.lastrowid)


def insert_group(conn, kind: str, label: str) -> int:
    cursor = conn.execute(
        "INSERT INTO groups(kind, label, created_at) VALUES (?, ?, 'now')",
        (kind, label),
    )
    return int(cursor.lastrowid)


def proposal_status(conn, proposal_id: int) -> str:
    return conn.execute("SELECT status FROM proposals WHERE id=?", (proposal_id,)).fetchone()[0]


def proposal_dest(conn, proposal_id: int) -> str:
    return conn.execute(
        "SELECT dest_relpath FROM proposals WHERE id=?", (proposal_id,)
    ).fetchone()[0]


def item_state(conn, proposal_id: int) -> str:
    return conn.execute(
        """
        SELECT i.state
        FROM proposals p
        JOIN items i ON i.id = p.item_id
        WHERE p.id=?
        """,
        (proposal_id,),
    ).fetchone()[0]
