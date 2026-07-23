from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from librairy.config import Settings
from librairy.content.extract import process_content_extractions
from librairy.db import connect
from librairy.models import EvidenceEntry
from librairy.proposals import upsert_proposal
from librairy.scanner import scan_root
from librairy.web.app import create_app


def client_for(tmp_path: Path) -> tuple[TestClient, object, Settings]:
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        HOST_LIBRARY_DIR=Path("/mnt/user/library"),
        FILE_STABILITY_SECONDS=0,
        CONTENT_SEARCH_ENABLED=True,
        _env_file=None,
    )
    settings.inbox_dir.mkdir()
    settings.library_dir.mkdir()
    settings.quarantine_dir.mkdir()
    conn = connect(settings)
    client = TestClient(create_app(settings, conn))
    client.post("/setup", data={"password": "correct horse battery"})
    return client, conn, settings


def test_search_text_facets_and_combinations_return_fixtures(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    queen = seed_library(conn, settings, "Music/Queen/Night Opera/Bohemian.flac", "music")
    seed_library(conn, settings, "Photos/2026/Italy/img.jpg", "photos")

    assert str(queen) in client.get("/search/results?q=queen opera").text
    assert "Bohemian.flac" in client.get("/search/results?category=music").text
    assert "img.jpg" not in client.get("/search/results?category=music").text
    assert "img.jpg" in client.get("/search/results?root=library&q=2026").text


def test_search_highlight_pagination_host_path_and_actions(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    for index in range(55):
        seed_library(conn, settings, f"Documents/Queen-{index}.txt", "documents")

    page = client.get("/search?q=queen")
    next_page = client.get("/search/results?q=queen&page=2")

    assert "<mark>Queen</mark>" in page.text
    assert "hx-get=\"/search/results" in page.text
    assert "Next" in page.text
    assert next_page.text.count("/preview/items/") == 5
    assert "/mnt/user/library/Documents/Queen-54.txt" in next_page.text
    assert "/data/library" not in page.text
    assert "Detail" in page.text
    assert "History" in page.text


def test_search_first_visit_and_empty_state_are_keyboard_operable(tmp_path: Path) -> None:
    client, _, _ = client_for(tmp_path)

    first = client.get("/search")
    empty = client.get("/search/results?q=missing")

    assert "placeholder=\"queen night opera\"" in first.text
    assert "<button type=\"submit\">Search</button>" in first.text
    assert "No matching indexed items" in empty.text


def test_search_content_facet_renders_marker_and_snippet(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    seed_library(conn, settings, "Documents/doc_0042.txt", "documents")
    (settings.library_dir / "Documents/doc_0042.txt").write_text(
        "inside text mentions coding",
        encoding="utf-8",
    )
    conn.execute("UPDATE items SET fingerprint='changed' WHERE relpath='Documents/doc_0042.txt'")
    process_content_extractions(conn, settings)

    without_content = client.get("/search/results?q=coding")
    with_content = client.get("/search/results?q=coding&content=true")

    assert "doc_0042.txt" not in without_content.text
    assert "text match" in with_content.text
    assert "<mark>coding</mark>" in with_content.text


def seed_library(conn, settings: Settings, relpath: str, category: str) -> int:
    path = settings.library_dir / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(relpath, encoding="utf-8")
    scan_root(conn, "library", settings.library_dir, settings)
    item_id = conn.execute("SELECT id FROM items WHERE relpath=?", (relpath,)).fetchone()[0]
    upsert_proposal(
        conn,
        item_id=item_id,
        category=category,
        clean_name=Path(relpath).name,
        dest_relpath=relpath,
        confidence=0.9,
        evidence=[EvidenceEntry("heuristic", "category", category, 0.9)],
    )
    return item_id


def test_dashboard_search_box_lands_on_results(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    queen = seed_library(conn, settings, "Music/Queen/Night Opera/Bohemian.flac", "music")

    dashboard = client.get("/dashboard").text
    landing = client.get("/search?q=queen opera").text

    # Dashboard has a prominent search box that GETs /search.
    assert 'class="search-hero"' in dashboard
    assert 'action="/search"' in dashboard
    assert 'name="q"' in dashboard
    # Landing on /search?q=... renders results server-side (no extra click).
    assert str(queen) in landing
    assert "Bohemian.flac" in landing
