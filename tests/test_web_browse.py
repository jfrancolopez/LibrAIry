from __future__ import annotations

import os
import re
from pathlib import Path

from fastapi.testclient import TestClient

from librairy.config import Settings
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
        _env_file=None,
    )
    settings.inbox_dir.mkdir()
    settings.library_dir.mkdir()
    settings.quarantine_dir.mkdir()
    conn = connect(settings)
    client = TestClient(create_app(settings, conn))
    client.post("/setup", data={"password": "correct horse battery"})
    return client, conn, settings


def test_browse_all_categories_counts_and_folder_drilldown(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    seed_item(conn, settings, "Music/Queen/Opera/Bohemian.flac", "music")
    seed_item(conn, settings, "Photos/2026/Italy/img.jpg", "photos")

    home = client.get("/browse")
    music = client.get("/browse/music")
    music_folder = client.get("/browse/music?folder=Queen/Opera")

    categories = ["music", "movies", "shows", "photos", "documents", "books", "projects", "misc"]
    for category in categories:
        assert f"/browse/{category}" in home.text
    assert "<strong>1</strong><span>music</span>" in home.text
    assert "Queen" in music.text
    assert "Bohemian.flac" in music_folder.text


def test_item_detail_shows_preview_metadata_evidence_siblings_and_history(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    first = seed_item(conn, settings, "Photos/2026/Italy/a.jpg", "photos")
    second = seed_item(conn, settings, "Photos/2026/Italy/b.jpg", "photos")
    conn.execute(
        """
        INSERT INTO history(ts, plan_id, op_id, action, src_root, src_relpath, dest_root,
                            dest_relpath, fingerprint, outcome)
        VALUES ('now', 'plan-1', 1, 'move', 'inbox', 'a.jpg', 'library',
                'Photos/2026/Italy/a.jpg', 'fp', 'ok')
        """
    )

    response = client.get(f"/items/{first}")

    assert response.status_code == 200
    assert "Image preview" in response.text
    assert "category: photos" in response.text
    assert "category photos 0.90" in response.text
    assert f"/items/{second}" in response.text
    assert "/history/plans/plan-1" in response.text
    assert "/mnt/user/library/Photos/2026/Italy/a.jpg" in response.text


def test_item_detail_degrades_when_preview_generation_fails(tmp_path: Path, monkeypatch) -> None:
    client, conn, settings = client_for(tmp_path)
    item_id = seed_item(conn, settings, "Photos/2026/Italy/a.jpg", "photos")

    def broken_preview(*args, **kwargs):  # noqa: ANN002, ANN003
        raise OSError("cache unavailable")

    monkeypatch.setattr("librairy.web.browse.preview_for_item", broken_preview)

    response = client.get(f"/items/{item_id}")

    assert response.status_code == 200
    assert "Preview unavailable" in response.text
    assert "cache unavailable" in response.text


def test_item_detail_degrades_when_evidence_decode_fails(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    item_id = seed_item(conn, settings, "Documents/a.txt", "documents")
    conn.execute("UPDATE proposals SET evidence='not-json' WHERE item_id=?", (item_id,))

    response = client.get(f"/items/{item_id}")

    assert response.status_code == 200
    assert "Evidence unavailable" in response.text


def test_error_page_identifies_itself(tmp_path: Path) -> None:
    client, _, _ = client_for(tmp_path)

    response = client.get("/missing-route")

    assert response.status_code == 404
    assert "[ERROR 404]" in response.text
    assert "Back to dashboard" in response.text


def test_browse_templates_have_no_mutating_affordances(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    item_id = seed_item(conn, settings, "Documents/a.txt", "documents")

    html = client.get("/browse").text + client.get("/browse/documents").text + client.get(
        f"/items/{item_id}"
    ).text

    # The shared app header (logout form) is chrome, not a browse affordance.
    html = re.sub(r"<header class=\"app-header\".*?</header>", "", html, flags=re.S)

    assert "<form" not in html
    assert "hx-post" not in html
    assert "<button" not in html


def test_browse_requests_do_not_walk_filesystem(tmp_path: Path, monkeypatch) -> None:
    client, conn, settings = client_for(tmp_path)
    seed_item(conn, settings, "Documents/a.txt", "documents")

    def forbidden_walk(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("browse must use the index, not os.walk")

    monkeypatch.setattr(os, "walk", forbidden_walk)

    assert client.get("/browse").status_code == 200
    assert client.get("/browse/documents").status_code == 200


def seed_item(conn, settings: Settings, relpath: str, category: str) -> int:
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


def test_browse_breadcrumbs_and_parent_link(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    seed_item(conn, settings, "Photos/2026/Italy/a.jpg", "photos")

    page = client.get("/browse/photos?folder=2026/Italy").text

    assert 'class="crumbs"' in page
    assert 'href="/browse"' in page
    assert 'href="/browse/photos"' in page
    assert 'href="/browse/photos?folder=2026"' in page
    # ".." row goes up one level
    assert 'data-parent="/browse/photos?folder=2026"' in page


def test_browse_detail_panel_reuses_item_detail(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    item_id = seed_item(conn, settings, "Photos/2026/Italy/a.jpg", "photos")

    listing = client.get("/browse/photos?folder=2026/Italy").text
    panel = client.get(f"/browse/items/{item_id}/panel")

    assert f'hx-get="/browse/items/{item_id}/panel"' in listing
    assert panel.status_code == 200
    assert 'id="browse-panel"' in panel.text
    assert "a.jpg" in panel.text
    # Humanized evidence, not raw codes.
    assert "Looks like photos" in panel.text
    assert f'href="/items/{item_id}"' in panel.text
