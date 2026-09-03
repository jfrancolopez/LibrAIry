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


def test_browse_home_lists_the_folders_that_exist_and_drills_down(tmp_path: Path) -> None:
    """The root is a listing of the library, not a list of known categories.

    It used to render all eight classification categories whether or not the
    folders were there — five permanent zeroes — and, worse, could not show a
    folder the owner made themselves.
    """
    client, conn, settings = client_for(tmp_path)
    seed_item(conn, settings, "Music/Queen/Opera/Bohemian.flac", "music")
    seed_item(conn, settings, "Photos/2026/Italy/img.jpg", "photos")
    (settings.library_dir / "Archives").mkdir()

    home = client.get("/browse")
    music = client.get("/browse/Music")
    music_folder = client.get("/browse/Music?folder=Queen/Opera")

    assert "<strong>1</strong><span>Music</span>" in home.text
    assert "<strong>1</strong><span>Photos</span>" in home.text
    # A directory nobody classified anything into is still a directory.
    assert "<strong>0</strong><span>Archives</span>" in home.text
    for absent in ("Movies", "Shows", "Documents", "Books", "Misc"):
        assert f"<span>{absent}</span>" not in home.text
    assert "Queen" in music.text
    assert "Bohemian.flac" in music_folder.text


def test_a_browse_url_names_a_real_folder(tmp_path: Path) -> None:
    """Old lowercase links keep working; anything invented is a 404."""
    client, conn, settings = client_for(tmp_path)
    seed_item(conn, settings, "Music/Queen/Opera/Bohemian.flac", "music")

    assert client.get("/browse/Music").status_code == 200
    assert client.get("/browse/music").status_code == 200
    assert client.get("/browse/Movies").status_code == 404
    # `/browse/..` never reaches the app — httpx collapses it to /browse — so
    # the traversal that matters is the encoded one, which does arrive intact.
    assert client.get("/browse/%2e%2e").status_code == 404
    assert client.get("/browse/%2e%2e%2f%2e%2e").status_code == 404
    # Case-insensitive on the folder name, but the folder query is still a
    # path, and a traversal through it is not somewhere you can arrive.
    assert client.get("/browse/MUSIC").status_code == 200
    assert client.get("/browse/MUSIC", params={"folder": "../../etc"}).status_code == 404


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
    assert "Image" in response.text
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
    #  A refusal offers the page the decision was on first, when there is one,
    #  and the dashboard always. There is no referer on a typed-in URL, so this
    #  one falls back to the standing pair.
    assert 'href="/dashboard"' in response.text
    assert 'href="/history"' in response.text


def test_browse_templates_have_no_mutating_affordances(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    item_id = seed_item(conn, settings, "Documents/a.txt", "documents")

    html = client.get("/browse").text + client.get("/browse/documents").text + client.get(
        f"/items/{item_id}"
    ).text

    # The shared app header (logout form) is chrome, not a browse affordance.
    html = re.sub(r"<header class=\"app-header\".*?</header>", "", html, flags=re.S)

    # The exceptions, named rather than waved through. Each writes a *record*
    # and none of them can move, rename, delete or queue a file:
    #
    #   /browse/audit          writes findings; the audit module is read-only
    #                          against the library by construction
    #   /items/{id}/tags       writes what the owner says this file is about.
    #                          The only way to give explicit context used to be
    #                          renaming the file, which is a filesystem change
    #                          to avoid a form — the invariant standing on its
    #                          head. See `librairy/tags.py`.
    #   /items/{id}/identify   asks a catalog what an audio file is and records
    #                          the answer. Named here because it was passing
    #                          only by fixture: this test seeds a document, and
    #                          that form renders for audio.
    #
    # Every other write verb stays banned, including a second POST anywhere
    # else. What this test defends is that Browse cannot change the library —
    # not that it has no controls.
    allowed = {
        "/browse/audit",
        f"/items/{item_id}/tags",
        f"/items/{item_id}/identify",
    }
    posted = re.findall(
        r"<form[^>]*method=\"post\"[^>]*action=\"([^\"]+)\"[^>]*>", html, flags=re.I
    )
    assert set(posted) <= allowed, f"unexpected write form: {set(posted) - allowed}"
    for action in allowed:
        html = re.sub(
            rf"<form[^>]*method=\"post\"[^>]*action=\"{re.escape(action)}\".*?</form>",
            "",
            html,
            flags=re.S,
        )

    # The invariant is that Browse cannot change anything, not that it has no
    # controls at all: it now carries the search box that used to be its own
    # tab, and a GET form reads. Anything that writes is still forbidden.
    for verb in ("hx-post", "hx-put", "hx-patch", "hx-delete", 'method="post"'):
        assert verb not in html.lower()
    forms = re.findall(r"<form[^>]*>", html)
    assert all('method="get"' in form for form in forms), forms
    # Counting buttons used to stand in for "Browse changes nothing", which
    # stopped working the moment Browse gained the same read-only preview
    # controls Review has. Naming them keeps the invariant instead: a button
    # here is the search submit or a view-only control, and a new one that
    # writes matches neither and fails.
    # `popovertarget` joins the list because the file-type `?` became a
    # popover button. It opens a panel of reference text and cannot write.
    view_only = (
        "data-lightbox",
        "data-preview-all",
        "data-preview-toggle",
        "preview-expand",
        "popovertarget=",
    )
    buttons = re.findall(r"<button\b[^>]*>", html)
    unexplained = [
        button
        for button in buttons
        if "submit" not in button and not any(marker in button for marker in view_only)
    ]
    assert not unexplained, unexplained
    assert sum("submit" in button for button in buttons) == len(forms)


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
    assert 'href="/browse/Photos"' in page
    assert 'href="/browse/Photos?folder=2026"' in page
    # ".." row goes up one level
    assert 'data-parent="/browse/Photos?folder=2026"' in page


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


def test_browse_counts_only_committed_library_files(tmp_path: Path) -> None:
    """Inbox items are searchable but not browsable — they must not be counted."""
    client, conn, settings = client_for(tmp_path)
    # A committed library file...
    seed_item(conn, settings, "Music/Queen/Opera/song.flac", "music")
    # ...and an inbox item that is indexed but not yet committed.
    inbox_file = settings.inbox_dir / "loose.flac"
    inbox_file.write_text("x", encoding="utf-8")
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    item_id = conn.execute("SELECT id FROM items WHERE root='inbox'").fetchone()[0]
    upsert_proposal(
        conn, item_id=item_id, category="music", clean_name="loose.flac",
        dest_relpath="Music/loose.flac", confidence=0.9,
        evidence=[EvidenceEntry("heuristic", "category", "music", 0.9)],
    )

    home = client.get("/browse").text

    # One browsable music file, not two.
    assert "<strong>1</strong><span>Music</span>" in home


def test_explorer_renders_four_panes(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    seed_item(conn, settings, "Photos/2026/Italy/a.jpg", "photos")
    seed_item(conn, settings, "Music/song.flac", "music")

    page = client.get("/browse/photos").text

    assert 'id="explorer"' in page
    for pane in ("0", "1", "2"):
        assert f'data-pane="{pane}"' in page
    assert 'id="browse-panel"' in page
    # Category pane lets you switch library sections without leaving the page.
    assert 'href="/browse/Music"' in page
    assert "/static/browse.js" in page


def test_file_rows_carry_a_thumbnail_and_size(tmp_path: Path) -> None:
    """A photo library listed as bare filenames is not browsable."""
    client, conn, settings = client_for(tmp_path)
    seed_item(conn, settings, "Photos/2026/Italy/a.jpg", "photos")

    page = client.get("/browse/photos?folder=2026/Italy").text

    assert 'class="row-thumb"' in page
    assert "/preview/items/" in page and "/thumb" in page
    assert 'class="row-size muted"' in page
    assert 'loading="lazy"' in page, "50 thumbnails must not all load at once"


def test_rows_without_a_thumbnail_get_a_placeholder(tmp_path: Path) -> None:
    """Otherwise names jump left and right depending on the file type."""
    client, conn, settings = client_for(tmp_path)
    seed_item(conn, settings, "Documents/notes.txt", "documents")

    page = client.get("/browse/documents").text

    assert "row-thumb is-placeholder" in page
    assert "/thumb" not in page, "a text file has no thumbnail to request"


def test_details_pane_loads_the_first_file_on_arrival(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    item_id = seed_item(conn, settings, "Photos/2026/Italy/a.jpg", "photos")

    page = client.get("/browse/photos?folder=2026/Italy").text

    assert f'hx-get="/browse/items/{item_id}/panel"' in page
    assert 'hx-trigger="load"' in page
    assert "Select a file to see it here." not in page


def test_empty_folder_pane_collapses_at_the_top_level(tmp_path: Path) -> None:
    """A category with no subfolders should not reserve a third of the screen."""
    client, conn, settings = client_for(tmp_path)
    seed_item(conn, settings, "Documents/notes.txt", "documents")

    page = client.get("/browse/documents").text

    assert 'class="explorer-pane is-empty" data-pane="1"' in page


def test_folder_pane_stays_when_it_holds_the_parent_link(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    seed_item(conn, settings, "Photos/2026/Italy/a.jpg", "photos")

    page = client.get("/browse/photos?folder=2026/Italy").text

    assert 'class="explorer-pane" data-pane="1"' in page
    assert 'data-parent="/browse/Photos?folder=2026"' in page


def test_human_size_formatting() -> None:
    from librairy.web.browse import human_size

    assert human_size(0) == ""
    assert human_size(None) == ""
    assert human_size(900) == "900 B"
    assert human_size(1536) == "1.5 KB"
    assert human_size(25_904_964) == "24.7 MB"


def test_load_more_appends_instead_of_paging(tmp_path: Path) -> None:
    """Prev/Next threw away everything you had already scrolled past."""
    client, conn, settings = client_for(tmp_path)
    for index in range(55):
        seed_item(conn, settings, f"Documents/file-{index:03d}.txt", "documents")

    page = client.get("/browse/documents").text

    assert "Load more" in page
    assert "Prev" not in page and "page 1" not in page
    assert 'hx-target="this"' in page
    assert "/browse/Documents/files?folder=&page=2" in page


def test_load_more_returns_only_rows_and_the_next_button(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    for index in range(55):
        seed_item(conn, settings, f"Documents/file-{index:03d}.txt", "documents")

    batch = client.get("/browse/documents/files?page=2")

    assert batch.status_code == 200
    assert "<html" not in batch.text, "a fragment, not a whole page"
    assert "file-050.txt" in batch.text
    # Last batch, so nothing more to offer.
    assert "Load more" not in batch.text


def test_load_more_is_absent_when_everything_fits(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    seed_item(conn, settings, "Documents/only.txt", "documents")

    assert "Load more" not in client.get("/browse/documents").text


def test_empty_message_only_shows_on_the_first_page(tmp_path: Path) -> None:
    """Appending past the end must not stamp "No files" under the list."""
    client, conn, settings = client_for(tmp_path)
    seed_item(conn, settings, "Documents/only.txt", "documents")

    beyond = client.get("/browse/documents/files?page=9")

    assert "No files at this level." not in beyond.text


def test_folder_with_an_ampersand_is_reachable(tmp_path: Path) -> None:
    """"R&B" turned ?folder=R&B into folder=R plus a stray B parameter.

    The pane came up empty with nothing on screen to explain why, and every
    folder name comes from the filesystem, so & is only the first character
    that would have done this.
    """
    client, conn, settings = client_for(tmp_path)
    seed_item(conn, settings, "Music/R&B Soul/track.mp3", "music")
    seed_item(conn, settings, "Music/R&B Soul/Alicia/live.mp3", "music")

    top = client.get("/browse/music").text
    assert "folder=R%26B+Soul" in top, "the folder link must escape the ampersand"

    inside = client.get("/browse/music", params={"folder": "R&B Soul"})
    assert "track.mp3" in inside.text
    assert 'data-parent="/browse/Music"' in inside.text
    assert "folder=R%26B+Soul%2FAlicia" in inside.text, "and so must the link one level down"


def test_blank_filter_fields_do_not_break_search(tmp_path: Path) -> None:
    """An untouched Year box submits year=, which used to 422.

    htmx does not swap error responses, so the search box simply stopped
    responding — no results, no message, nothing in the page to react to.
    """
    client, conn, settings = client_for(tmp_path)
    seed_item(conn, settings, "Music/Queen/Bohemian.flac", "music")

    blanks = {"q": "bohemian", "root": "library", "category": "", "year": "", "genre": ""}
    page = client.get("/browse", params=blanks)
    body = client.get("/browse/body", params=blanks)

    assert page.status_code == 200
    assert body.status_code == 200
    assert "Bohemian.flac" in body.text
