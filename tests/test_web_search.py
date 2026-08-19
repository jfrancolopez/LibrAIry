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
    # Preview expands in place; "View details" is the only navigation. The old
    # Detail link pointed straight at a fragment endpoint, which replaced the
    # whole page with an unstyled bare card.
    assert 'data-preview-target="search-preview-' in page.text
    assert "View details" in page.text
    assert 'href="/items/' in page.text


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
    landing = client.get("/browse?q=queen opera").text
    # The old Search URL still works — bookmarks and links outlive a redesign.
    redirect = client.get("/search?q=queen opera", follow_redirects=False)

    assert 'class="search-hero"' in dashboard
    assert 'action="/browse"' in dashboard
    assert 'name="q"' in dashboard
    # Landing on /browse?q=... renders results server-side (no extra click).
    assert str(queen) in landing
    assert "Bohemian.flac" in landing
    assert redirect.status_code == 302
    assert redirect.headers["location"] == "/browse?q=queen%20opera"


def test_results_carry_the_facts_that_identify_a_file(tmp_path: Path) -> None:
    """A result used to be a name, a path and a category — not enough to tell
    two similarly named files apart without opening both."""
    client, conn, settings = client_for(tmp_path)
    seed_library(conn, settings, "Photos/2026/holiday.jpg", "photos")

    page = client.get("/search?q=holiday").text

    assert "row-thumb" in page, "an image result should show its thumbnail"
    assert "/preview/items/" in page and "/thumb" in page
    assert "result-facts" in page
    assert "jpg" in page


def test_non_previewable_results_do_not_request_a_thumbnail(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    seed_library(conn, settings, "Documents/notes.txt", "documents")

    page = client.get("/search?q=notes").text

    assert "row-thumb is-placeholder" in page
    assert "/thumb" not in page


def test_search_size_formatting() -> None:
    from librairy.search import human_size

    assert human_size(None) == ""
    assert human_size(0) == ""
    assert human_size(900) == "900 B"
    assert human_size(1536) == "1.5 KB"


def test_destination_is_hidden_once_the_file_is_already_there(tmp_path: Path) -> None:
    """A committed item would otherwise show "goes to" pointing at its own path."""
    client, conn, settings = client_for(tmp_path)
    seed_library(conn, settings, "Music/song.flac", "music")

    page = client.get("/search?q=song").text

    assert "goes to" not in page


def test_destination_is_shown_while_a_file_is_still_in_the_inbox(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    (settings.inbox_dir / "loose.flac").write_text("x", encoding="utf-8")
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    item_id = conn.execute("SELECT id FROM items WHERE root='inbox'").fetchone()[0]
    upsert_proposal(
        conn,
        item_id=item_id,
        category="music",
        clean_name="loose.flac",
        dest_relpath="Music/loose.flac",
        confidence=0.9,
        evidence=[EvidenceEntry("heuristic", "category", "music", 0.9)],
    )

    # Searching defaults to the library, so an unfiled file needs saying so.
    default_scope = client.get("/browse?q=loose").text
    page = client.get("/browse?q=loose&root=inbox").text

    assert "loose.flac" not in default_scope
    assert "goes to" in page
    assert "library/Music/loose.flac" in page


def test_search_defaults_to_the_library_not_every_root(tmp_path: Path) -> None:
    """Searching every root at once mixed three unrelated things, and the
    unfiled ones dominate because that is where the volume is. Typing a word
    in Browse and getting inbox files back is the confusing half of that."""
    client, conn, settings = client_for(tmp_path)
    seed_library(conn, settings, "Music/Queen/Bohemian.flac", "music")
    (settings.inbox_dir / "Bohemian-copy.flac").write_text("x", encoding="utf-8")
    scan_root(conn, "inbox", settings.inbox_dir, settings)

    library_only = client.get("/browse?q=bohemian").text
    everywhere = client.get("/browse?q=bohemian&root=all").text

    assert "Music/Queen/Bohemian.flac" in library_only
    assert "Bohemian-copy.flac" not in library_only
    assert "Bohemian-copy.flac" in everywhere


def test_browse_shows_categories_until_you_actually_search(tmp_path: Path) -> None:
    """Tiles or results, never both: a live search must not leave a grid of
    categories stranded above the matches."""
    client, conn, settings = client_for(tmp_path)
    seed_library(conn, settings, "Music/Queen/Bohemian.flac", "music")

    idle = client.get("/browse").text
    searching = client.get("/browse?q=bohemian").text

    assert 'href="/browse/Music"' in idle
    assert "Back to categories" not in idle
    assert 'href="/browse/Music"' not in searching
    assert "Back to categories" in searching
