from __future__ import annotations

import time
from pathlib import Path

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
    assert "Edit in P6-03" in response.text


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


def seed_proposal(
    conn,
    relpath: str,
    category: str,
    dest_relpath: str | None,
    confidence: float,
    group_id: int | None,
) -> int:
    item = insert_item(conn, relpath)
    return upsert_proposal(
        conn,
        item_id=item,
        category=category,
        clean_name=Path(relpath).name,
        dest_relpath=dest_relpath,
        confidence=confidence,
        group_id=group_id,
        evidence=[EvidenceEntry("heuristic", "category", category, confidence)],
    )


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
