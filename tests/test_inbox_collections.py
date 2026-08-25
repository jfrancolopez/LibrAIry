"""What arrived together, and what deliberately does not count as together.

The negative tests matter more than the positive ones here. A collection that
groups by arrival time would be wrong in both directions at once — a slow card
copy scatters one import across an hour, and a fast one drops two unrelated
downloads into the same minute — so the rule is a folder somebody made, and
nothing else.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from fastapi.testclient import TestClient

from librairy.config import Settings
from librairy.db import connect
from librairy.inbox_collections import (
    PAGE_SIZE,
    folder_of,
    members,
    ready_proposal_ids,
    summaries,
    summary,
)
from librairy.models import EvidenceEntry
from librairy.proposals import upsert_proposal
from librairy.relationships import SUBTITLE, record
from librairy.web.app import create_app

EVIDENCE = [EvidenceEntry("heuristic", "category", "test", 0.9)]


def flat(text: str) -> str:
    """The page with its line wrapping taken out.

    Templates wrap for readability, so a sentence a person reads as one line
    arrives with a newline in the middle of it. Asserting on the wrapped form
    pins the indentation rather than the words.
    """
    return " ".join(text.split())


def settings_for(tmp_path: Path) -> Settings:
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
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


def add_item(
    conn: sqlite3.Connection,
    relpath: str,
    *,
    root: str = "inbox",
    state: str = "discovered",
    first_seen: str = "2026-08-24T10:00:00+00:00",
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO items(root, relpath, size, mtime_ns, fingerprint, state,
                          first_seen_at, last_seen_at)
        VALUES (?, ?, 10, 1, ?, ?, ?, ?)
        """,
        (root, relpath, f"{root}:{relpath}", state, first_seen, first_seen),
    )
    return int(cursor.lastrowid)


def propose(
    conn: sqlite3.Connection,
    item_id: int,
    *,
    category: str = "photos",
    dest_relpath: str | None = "Photos/2026/x.jpg",
    dest_root: str = "library",
    status: str = "proposed",
    confidence: float = 0.9,
) -> int:
    proposal_id = upsert_proposal(
        conn,
        item_id=item_id,
        category=category,
        clean_name="x.jpg",
        dest_relpath=dest_relpath,
        dest_root=dest_root,
        action="quarantine" if dest_root == "quarantine" else "move",
        confidence=confidence,
        evidence=EVIDENCE,
    )
    conn.execute("UPDATE proposals SET status=? WHERE id=?", (status, proposal_id))
    conn.execute(
        "UPDATE items SET state=? WHERE id=?",
        ("approved" if status == "approved" else "proposed", item_id),
    )
    return proposal_id


def seed_card(conn: sqlite3.Connection, folder: str = "CameraCard") -> dict[str, int]:
    """Twenty ready photographs, two choices, three unresolved."""
    ids: dict[str, int] = {}
    for index in range(20):
        item = add_item(conn, f"{folder}/IMG_{index:04d}.JPG")
        propose(conn, item, dest_relpath=f"Photos/2026/IMG_{index:04d}.jpg")
    for index in range(2):
        item = add_item(conn, f"{folder}/DUP_{index}.JPG")
        propose(conn, item, dest_root="quarantine", dest_relpath=f"2026/DUP_{index}.JPG")
        ids[f"choice{index}"] = item
    for index in range(3):
        ids[f"unknown{index}"] = add_item(conn, f"{folder}/weird_{index}.bin")
    return ids


# 1 — files in one Inbox subfolder form one collection.
def test_one_inbox_subfolder_is_one_collection(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path))
    for name in ("a.jpg", "b.jpg", "c.mov"):
        propose(conn, add_item(conn, f"CameraCard/{name}"))

    found, total = summaries(conn)

    assert total == 1
    assert [item.folder for item in found] == ["CameraCard"]
    assert found[0].total == 3


# 2 — root-level unrelated files are not grouped arbitrarily.
def test_loose_files_at_the_inbox_root_are_not_a_collection(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path))
    for name in ("holiday.jpg", "invoice.pdf", "song.flac"):
        propose(conn, add_item(conn, name))

    found, total = summaries(conn)

    assert (found, total) == ([], 0)
    assert folder_of("holiday.jpg") == ""
    assert folder_of("CameraCard/holiday.jpg") == "CameraCard"


# 3 — mixed categories are allowed.
def test_a_collection_may_hold_several_categories(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path))
    propose(conn, add_item(conn, "Old Drive/Photos/a.jpg"), category="photos")
    propose(conn, add_item(conn, "Old Drive/Music/b.flac"), category="music")
    propose(conn, add_item(conn, "Old Drive/Docs/c.pdf"), category="documents")

    found, _total = summaries(conn)

    #  Nested structure is the person's own arrangement, not a second
    #  collection: the import is the drive.
    assert found[0].folder == "Old Drive"
    assert dict(found[0].categories) == {"photos": 1, "music": 1, "documents": 1}


# 4 — the counts are right.
def test_collection_counts_split_ready_choice_and_unresolved(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path))
    seed_card(conn)

    found = summary(conn, "CameraCard")

    assert found is not None
    assert (found.total, found.ready, found.choice, found.unresolved) == (25, 20, 2, 3)
    assert found.settled is False


# 5 — the Review row is bounded: counts, never member rows.
def test_review_shows_a_summary_and_not_a_card_per_file(tmp_path: Path) -> None:
    client, conn, _settings = client_for(tmp_path)
    seed_card(conn)

    page = client.get("/review")

    assert "Arrived together" in page.text
    assert "CameraCard" in page.text
    assert "20</span> ready to file" in flat(page.text)
    assert "2</span> need your choice" in flat(page.text)
    #  The proof that this is a summary: the twentieth photograph is nowhere in
    #  the block, whatever the file list further down happens to show.
    head = page.text.split('id="review-list"')[0]
    assert "IMG_0019.JPG" not in head


# 6 — the collection page is paginated.
def test_collection_page_is_paginated(tmp_path: Path) -> None:
    client, conn, _settings = client_for(tmp_path)
    for index in range(PAGE_SIZE + 10):
        propose(conn, add_item(conn, f"Big/IMG_{index:04d}.JPG"))

    page = client.get("/review/collection/Big")

    assert page.status_code == 200
    assert page.text.count("collection-member") == PAGE_SIZE + 1  # the <ul> class too
    assert "page 1 of 2" in flat(page.text)
    assert "IMG_0059.JPG" not in page.text
    assert "IMG_0059.JPG" in client.get("/review/collection/Big?page=2").text


# 7 — a thousand members stay bounded.
def test_a_thousand_members_render_one_bounded_page(tmp_path: Path) -> None:
    client, conn, _settings = client_for(tmp_path)
    for index in range(1000):
        propose(conn, add_item(conn, f"Thousand/IMG_{index:05d}.JPG"))

    started = time.perf_counter()
    page = client.get("/review/collection/Thousand")
    elapsed = time.perf_counter() - started

    assert page.status_code == 200
    assert "1000 files that arrived together" in flat(page.text)
    assert page.text.count("collection-member") == PAGE_SIZE + 1
    assert elapsed < 5.0


# 8 — ten thousand rows in the database, still one bounded page.
def test_ten_thousand_members_stay_bounded(tmp_path: Path) -> None:
    client, conn, _settings = client_for(tmp_path)
    conn.executemany(
        """
        INSERT INTO items(root, relpath, size, mtime_ns, fingerprint, state,
                          first_seen_at, last_seen_at)
        VALUES ('inbox', ?, 10, 1, ?, 'discovered', 'now', 'now')
        """,
        [(f"Huge/file_{index:05d}.bin", f"f{index}") for index in range(10_000)],
    )

    started = time.perf_counter()
    review = client.get("/review")
    page = client.get("/review/collection/Huge")
    elapsed = time.perf_counter() - started

    assert "10000" in flat(review.text)
    assert page.text.count("collection-member") == PAGE_SIZE + 1
    assert elapsed < 20.0


# 9 — approving the ready subset chooses nothing new.
def test_approving_the_ready_subset_applies_answers_and_chooses_nothing(
    tmp_path: Path,
) -> None:
    client, conn, _settings = client_for(tmp_path)
    ids = seed_card(conn)

    response = client.post(
        "/review/collection/CameraCard/approve",
        data={"csrf_token": client.cookies.get("csrf") or ""},
        follow_redirects=False,
        headers={"x-csrf-token": _csrf(client)},
    )

    assert response.status_code == 303
    approved = conn.execute(
        "SELECT COUNT(*) FROM proposals WHERE status='approved'"
    ).fetchone()[0]
    assert approved == 20
    #  The two choices are untouched, and so is everything with no destination.
    for key in ("choice0", "choice1"):
        status = conn.execute(
            "SELECT status FROM proposals WHERE item_id=?", (ids[key],)
        ).fetchone()[0]
        assert status == "proposed"


# 10 — unresolved members stay unresolved.
def test_bulk_approval_leaves_unresolved_members_alone(tmp_path: Path) -> None:
    client, conn, _settings = client_for(tmp_path)
    seed_card(conn)

    client.post(
        "/review/collection/CameraCard/approve",
        headers={"x-csrf-token": _csrf(client)},
        follow_redirects=False,
    )
    found = summary(conn, "CameraCard")

    assert found is not None
    assert (found.ready, found.waiting, found.unresolved) == (0, 20, 3)


# 11 — companions are reflected in the count.
def test_companions_are_counted_as_companions(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path))
    video = add_item(conn, "Movie/film.mkv")
    subtitle = add_item(conn, "Movie/film.en.srt")
    propose(conn, video, category="movies", dest_relpath="Movies/Film.mkv")
    propose(conn, subtitle, category="movies", dest_relpath="Movies/Film.en.srt")
    record(
        conn,
        companion_item_id=subtitle,
        subject_item_id=video,
        kind=SUBTITLE,
        provenance="names the same file",
    )

    found = summary(conn, "Movie")

    assert found is not None
    assert found.companions == 1


# 12 — files leaving through Commit update the collection.
def test_committed_files_leave_the_collection(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path))
    for name in ("a.jpg", "b.jpg", "c.jpg"):
        propose(conn, add_item(conn, f"Trip/{name}"))
    gone = conn.execute(
        "SELECT id FROM items WHERE relpath='Trip/c.jpg'"
    ).fetchone()[0]
    assert summary(conn, "Trip").total == 3

    #  What Commit does to the row: the item moves root, in place.
    conn.execute("UPDATE items SET root='library', relpath='Photos/c.jpg' WHERE id=?", (gone,))

    assert summary(conn, "Trip").total == 2


# 13 — a completed collection disappears rather than lingering.
def test_a_finished_collection_disappears(tmp_path: Path) -> None:
    client, conn, _settings = client_for(tmp_path)
    items = [add_item(conn, f"Trip/{name}") for name in ("a.jpg", "b.jpg")]
    for item in items:
        propose(conn, item)
    assert "Trip" in client.get("/review").text

    for index, item in enumerate(items):
        conn.execute(
            "UPDATE items SET root='library', relpath=? WHERE id=?",
            (f"Photos/{index}.jpg", item),
        )

    assert summary(conn, "Trip") is None
    assert summaries(conn) == ([], 0)
    assert client.get("/review/collection/Trip").status_code == 404
    assert "Arrived together" not in client.get("/review").text


# 14 — nothing is grouped by when it turned up.
def test_arrival_time_never_forms_a_collection(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path))
    #  Three unrelated downloads that landed in the same second, and two halves
    #  of one folder copied twenty minutes apart. Timestamps say the opposite of
    #  the truth in both cases.
    for name in ("one.jpg", "two.flac", "three.pdf"):
        propose(conn, add_item(conn, name, first_seen="2026-08-24T10:00:00+00:00"))
    propose(conn, add_item(conn, "Card/a.jpg", first_seen="2026-08-24T10:00:00+00:00"))
    propose(conn, add_item(conn, "Card/b.jpg", first_seen="2026-08-24T10:20:00+00:00"))
    #  A single loose file in a folder of its own is not an import either.
    propose(conn, add_item(conn, "Alone/only.jpg"))

    found, total = summaries(conn)

    assert total == 1
    assert (found[0].folder, found[0].total) == ("Card", 2)


def test_sections_are_addressable_and_bounded(tmp_path: Path) -> None:
    client, conn, _settings = client_for(tmp_path)
    seed_card(conn)

    unresolved = client.get("/review/collection/CameraCard?section=unresolved")

    assert "weird_0.bin" in unresolved.text
    assert "IMG_0000.JPG" not in unresolved.text


def test_a_missing_member_is_not_part_of_the_collection(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path))
    for name in ("a.jpg", "b.jpg", "c.jpg"):
        propose(conn, add_item(conn, f"Card/{name}"))
    away = conn.execute("SELECT id FROM items WHERE relpath='Card/c.jpg'").fetchone()[0]
    conn.execute("UPDATE items SET missing_since='now' WHERE id=?", (away,))

    assert summary(conn, "Card").total == 2


def test_ready_ids_are_resolved_from_the_database_not_the_page(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path))
    ready = add_item(conn, "Card/a.jpg")
    propose(conn, ready)
    assert ready_proposal_ids(conn, "Card")

    #  A duplicate report arrives after the page was drawn. The file is a
    #  choice now, and bulk must not be able to reach it.
    conn.execute(
        "INSERT INTO duplicate_reports(item_id, other_id, payload, created_at)"
        " VALUES (?, ?, '{}', 'now')",
        (ready, ready),
    )

    assert ready_proposal_ids(conn, "Card") == []


def test_members_of_an_unknown_folder_is_empty(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path))
    propose(conn, add_item(conn, "Card/a.jpg"))
    propose(conn, add_item(conn, "Card/b.jpg"))

    assert members(conn, "Nope") == []
    assert summary(conn, "Card/nested") is None


def test_a_folder_holding_one_file_is_not_an_import(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path))
    propose(conn, add_item(conn, "Manuals/router.pdf"))

    assert summaries(conn) == ([], 0)
    assert summary(conn, "Manuals") is None


def _csrf(client: TestClient) -> str:
    page = client.get("/review")
    marker = 'name="csrf_token" value="'
    return page.text.split(marker, 1)[1].split('"', 1)[0]
