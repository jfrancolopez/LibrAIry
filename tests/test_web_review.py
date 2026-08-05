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

    assert "Kind of Blue" in page.text
    assert "1 shown" in page.text
    assert "Italy" in page.text
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

    # Evidence renders as plain-language "why" lines, not bracket codes.
    assert "Looks like unknown item fallback" in response.text
    assert "AI · openai" in response.text
    assert "Why?" in response.text
    assert "pending destination" in response.text
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
    assert "of <strong>5000</strong>" in response.text
    # Row checkboxes only — the header select-all is a checkbox too.
    assert response.text.count('name="proposal_id" type="checkbox"') == 50
    assert "Page <strong>1</strong> of <strong>100</strong>" in response.text
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
    assert "value=\"approve\"" in page.text
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


def test_review_rows_carry_confidence_bands_and_row_actions(tmp_path: Path) -> None:
    client, conn = client_for(tmp_path)
    high = insert_item(conn, "sure.mp3")
    low = insert_item(conn, "unsure.bin")
    upsert_proposal(
        conn, item_id=high, category="music", clean_name="sure.mp3",
        dest_relpath="Music/A/B/sure.mp3", confidence=0.95,
        evidence=[EvidenceEntry("tags", "metadata", "embedded audio tags", 0.95)],
    )
    upsert_proposal(
        conn, item_id=low, category="misc", clean_name="unsure.bin",
        dest_relpath=None, confidence=0.3,
        evidence=[EvidenceEntry("heuristic", "category", "unknown", 0.3)],
    )

    page = client.get("/review").text

    # Colour band is paired with a text label, never colour alone.
    assert "conf-high" in page
    assert "confident" in page
    assert "conf-low" in page
    assert "needs a look" in page
    # Per-row decisions, CSP-safe (htmx attributes, no inline handlers).
    assert 'hx-vals=\'{"action": "approve"' in page
    assert "onclick=" not in page


def test_quick_approve_confident_only_touches_high_confidence(tmp_path: Path) -> None:
    client, conn = client_for(tmp_path)
    sure = seed_proposal(conn, "sure.flac", "music", "Music/sure.flac", 0.95, None)
    unsure = seed_proposal(conn, "unsure.bin", "misc", "Misc/unsure.bin", 0.4, None)

    page = client.get("/review").text
    response = client.post(
        "/review/action",
        data={
            "action": "approve",
            "all_matching": "true",
            "state": "proposed",
            "min_confidence": "0.85",
        },
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )

    assert "Approve all confident" in page
    assert response.status_code == 200
    assert proposal_status(conn, sure) == "approved"
    assert proposal_status(conn, unsure) == "proposed"


def test_wallet_files_are_flagged_in_the_queue(tmp_path: Path) -> None:
    """A wallet must not vanish into a bulk approve unnoticed."""
    client, conn = client_for(tmp_path)
    seed_proposal(conn, "backup/wallet.dat", "misc", "Misc/wallet.dat", 0.9, None)

    page = client.get("/review").text

    assert "possible wallet" in page
    assert "flag-crypto" in page
    assert "not a backed-up file" in page


def test_hidden_files_offer_a_rename_that_unhides_them(tmp_path: Path) -> None:
    client, conn = client_for(tmp_path)
    seed_proposal(conn, ".env", "documents", "Documents/env", 0.9, None)

    page = client.get("/review").text

    assert "flag-hidden" in page
    assert "Rename to env" in page


def test_possible_adult_content_is_marked_as_a_guess(tmp_path: Path) -> None:
    client, conn = client_for(tmp_path)
    seed_proposal(conn, "downloads/clip.XXX.1080p.mp4", "movies", "Movies/clip.mp4", 0.9, None)

    page = client.get("/review").text

    assert "possibly adult" in page
    assert "guess from the filename" in page


def test_ordinary_files_carry_no_flag_markup(tmp_path: Path) -> None:
    client, conn = client_for(tmp_path)
    seed_proposal(conn, "Music/song.flac", "music", "Music/song.flac", 0.9, None)

    page = client.get("/review").text

    assert "flag-list" not in page


def test_pager_says_how_much_there_is_not_just_the_page_number(tmp_path: Path) -> None:
    """"page 3" alone tells you nothing about how much is left to get through."""
    client, conn = client_for(tmp_path)
    for index in range(120):
        seed_proposal(conn, f"inbox/file-{index}.mkv", "movies", f"Movies/f{index}.mkv", 0.9, None)

    first = client.get("/review/list").text
    middle = client.get("/review/list?page=2").text

    assert "Showing <strong>1–50</strong> of <strong>120</strong>" in first
    assert "Page <strong>1</strong> of <strong>3</strong>" in first
    assert "First" not in first  # already there
    assert "Showing <strong>51–100</strong> of <strong>120</strong>" in middle
    assert "« First" in middle
    assert "Last »" in middle


def test_pager_is_quiet_when_everything_fits_on_one_page(tmp_path: Path) -> None:
    client, conn = client_for(tmp_path)
    seed_proposal(conn, "inbox/only.mkv", "movies", "Movies/only.mkv", 0.9, None)

    body = client.get("/review/list").text

    assert "Showing <strong>1–1</strong> of <strong>1</strong>" in body
    assert "Page <strong>1</strong> of" not in body


def test_each_group_offers_a_select_all_box(tmp_path: Path) -> None:
    """Bulk actions used to need every row ticked by hand; the only shortcut
    was "every match", which is a far bigger commitment than "these ones"."""
    client, conn = client_for(tmp_path)
    for index in range(3):
        seed_proposal(conn, f"inbox/file-{index}.mkv", "movies", f"Movies/f{index}.mkv", 0.9, None)

    body = client.get("/review/list").text

    assert 'class="select-all"' in body
