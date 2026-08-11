"""Search answers "which files that are here match this?".

The bug these lock down: `missing_since` is set by every scan and checked by
Review, Commit, plan, dedup, duplicates, the catalog probe, content extraction,
backup, the indexer and companions. `search.py` did not mention the column
anywhere, so a file deleted in August still came back as a normal result —
same thumbnail slot, same size, same category badge, same destination — beside
a file that was really there.

The record is kept. It carries the decisions made about that file and History
still reaches it; only the query changed. Nothing here deletes anything.

Deterministic throughout: no AI provider, no catalog, no network.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from librairy.config import Settings
from librairy.db import connect
from librairy.models import EvidenceEntry
from librairy.proposals import upsert_proposal
from librairy.scanner import scan_root
from librairy.search import PAGE_SIZE, SearchFilters, search_items
from librairy.web.app import create_app


def settings_for(tmp_path: Path) -> Settings:
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        FILE_STABILITY_SECONDS=0,
        _env_file=None,
    )
    for root in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        root.mkdir(parents=True, exist_ok=True)
    return settings


def client_for(tmp_path: Path):
    settings = settings_for(tmp_path)
    conn = connect(settings)
    client = TestClient(create_app(settings, conn))
    client.post("/setup", data={"password": "correct horse battery"})
    return client, conn, settings


def write(settings: Settings, relpath: str, root: str = "library") -> Path:
    base = settings.library_dir if root == "library" else settings.inbox_dir
    path = base / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(relpath, encoding="utf-8")
    return path


def scan(conn, settings: Settings, root: str = "library") -> None:
    base = settings.library_dir if root == "library" else settings.inbox_dir
    scan_root(conn, root, base, settings)


def classify(conn, category: str = "photos", genre: str = "") -> None:
    """A proposal per item — deterministic, no classifier and no provider."""
    for row in conn.execute("SELECT id, relpath FROM items").fetchall():
        evidence = [EvidenceEntry("heuristic", "category", category, 0.9)]
        if genre:
            evidence.append(EvidenceEntry("heuristic", "genre", genre, 0.9))
        upsert_proposal(
            conn,
            item_id=row["id"],
            category=category,
            clean_name=Path(row["relpath"]).name,
            dest_relpath=row["relpath"],
            confidence=0.9,
            evidence=evidence,
        )


def names(rows) -> set[str]:
    return {Path(str(row["relpath"])).name for row in rows}


# --- the bug ----------------------------------------------------------------


def test_an_existing_file_is_found(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    write(settings, "Photos/real.jpg")
    scan(conn, settings)
    classify(conn)

    assert names(search_items(conn, "real")) == {"real.jpg"}


def test_a_deleted_file_is_not_a_result(tmp_path: Path) -> None:
    """The exact reported bug: scan, delete, rescan, still findable."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    write(settings, "Photos/real.jpg")
    write(settings, "Photos/later-deleted.jpg")
    scan(conn, settings)
    classify(conn)
    assert names(search_items(conn, "later")) == {"later-deleted.jpg"}

    (settings.library_dir / "Photos" / "later-deleted.jpg").unlink()
    scan(conn, settings)

    assert search_items(conn, "later") == []
    assert names(search_items(conn, "jpg")) == {"real.jpg"}


def test_the_record_survives_being_excluded(tmp_path: Path) -> None:
    """Excluded from Search is not deleted. The decisions made about that file
    are the reason the row is kept."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    write(settings, "Photos/gone.jpg")
    scan(conn, settings)
    classify(conn)
    item_id = conn.execute("SELECT id FROM items").fetchone()[0]

    (settings.library_dir / "Photos" / "gone.jpg").unlink()
    scan(conn, settings)

    assert conn.execute("SELECT COUNT(*) FROM items WHERE id=?", (item_id,)).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM proposals WHERE item_id=?", (item_id,)
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM search_fts WHERE item_id=?", (item_id,)
    ).fetchone()[0] == 1, "the entry stays, so a returning file needs no rebuild"
    assert conn.execute(
        "SELECT missing_since FROM items WHERE id=?", (item_id,)
    ).fetchone()[0] is not None


def test_a_returning_file_becomes_searchable_again(tmp_path: Path) -> None:
    """No manual repair: the scan that finds it again clears the flag."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    write(settings, "Photos/away.jpg")
    scan(conn, settings)
    classify(conn)
    (settings.library_dir / "Photos" / "away.jpg").unlink()
    scan(conn, settings)
    assert search_items(conn, "away") == []

    write(settings, "Photos/away.jpg")
    scan(conn, settings)

    assert names(search_items(conn, "away")) == {"away.jpg"}
    assert conn.execute(
        "SELECT missing_since FROM items WHERE relpath='Photos/away.jpg'"
    ).fetchone()[0] is None


def test_a_returning_file_does_not_gain_a_second_search_entry(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    write(settings, "Photos/away.jpg")
    scan(conn, settings)
    for _ in range(2):
        (settings.library_dir / "Photos" / "away.jpg").unlink()
        scan(conn, settings)
        write(settings, "Photos/away.jpg")
        scan(conn, settings)

    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM search_fts").fetchone()[0] == 1
    assert len(search_items(conn, "away")) == 1


def test_a_rename_outside_librairy_leaves_one_healthy_result(tmp_path: Path) -> None:
    """Two rows, one file. LibrAIry does not detect the rename — but it must
    not present both as if they were both there."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    write(settings, "Photos/a.jpg")
    scan(conn, settings)
    classify(conn)

    (settings.library_dir / "Photos" / "a.jpg").rename(settings.library_dir / "Photos" / "b.jpg")
    scan(conn, settings)
    classify(conn)

    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 2
    assert names(search_items(conn, "jpg")) == {"b.jpg"}


# --- counting and paging ----------------------------------------------------


def test_missing_rows_are_excluded_before_the_page_is_cut(tmp_path: Path) -> None:
    """The Browse folder bug in another table: filter after LIMIT and a page
    silently shrinks, filter before it and the page stays full."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    for index in range(PAGE_SIZE + 10):
        write(settings, f"Photos/shot-{index:03d}.jpg")
    scan(conn, settings)
    classify(conn)
    for index in range(10):
        (settings.library_dir / "Photos" / f"shot-{index:03d}.jpg").unlink()
    scan(conn, settings)

    first = search_items(conn, "shot", SearchFilters(page=1))
    second = search_items(conn, "shot", SearchFilters(page=2))

    assert len(first) == PAGE_SIZE, "a full page of files that exist"
    assert len(second) == 0
    assert not (names(first) & {f"shot-{index:03d}.jpg" for index in range(10)})
    assert len(names(first) | names(second)) == PAGE_SIZE


def test_paging_does_not_skip_a_live_file(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    for index in range(PAGE_SIZE + 20):
        write(settings, f"Photos/shot-{index:03d}.jpg")
    scan(conn, settings)
    classify(conn)
    for index in range(0, PAGE_SIZE + 20, 2):
        (settings.library_dir / "Photos" / f"shot-{index:03d}.jpg").unlink()
    scan(conn, settings)

    seen: set[str] = set()
    for page in (1, 2, 3):
        seen |= names(search_items(conn, "shot", SearchFilters(page=page)))

    on_disk = {path.name for path in (settings.library_dir / "Photos").iterdir()}
    assert seen == on_disk


# --- no filter reintroduces them --------------------------------------------


def test_the_category_filter_does_not_bring_them_back(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    write(settings, "Photos/here.jpg")
    write(settings, "Photos/gone.jpg")
    scan(conn, settings)
    classify(conn, category="photos")
    (settings.library_dir / "Photos" / "gone.jpg").unlink()
    scan(conn, settings)

    assert names(search_items(conn, "", SearchFilters(category="photos"))) == {"here.jpg"}
    assert names(search_items(conn, "jpg", SearchFilters(category="photos"))) == {"here.jpg"}


def test_the_root_filter_does_not_bring_them_back(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    write(settings, "here.mkv", root="inbox")
    write(settings, "gone.mkv", root="inbox")
    scan(conn, settings, root="inbox")
    classify(conn, category="movies")
    (settings.inbox_dir / "gone.mkv").unlink()
    scan(conn, settings, root="inbox")

    for filters in (
        SearchFilters(root="inbox"),
        SearchFilters(root=None),
        SearchFilters(root="inbox", category="movies"),
    ):
        assert names(search_items(conn, "mkv", filters)) == {"here.mkv"}, filters


def test_the_genre_and_year_filters_do_not_bring_them_back(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    write(settings, "Music/here.flac")
    write(settings, "Music/gone.flac")
    scan(conn, settings)
    classify(conn, category="music", genre="Jazz")
    (settings.library_dir / "Music" / "gone.flac").unlink()
    scan(conn, settings)

    assert names(search_items(conn, "flac", SearchFilters(genre="Jazz"))) == {"here.flac"}
    assert names(search_items(conn, "", SearchFilters(genre="Jazz"))) == {"here.flac"}


def test_an_empty_query_does_not_bring_them_back(tmp_path: Path) -> None:
    """No text means "everything", which used to include the ghosts."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    write(settings, "Photos/here.jpg")
    write(settings, "Photos/gone.jpg")
    scan(conn, settings)
    classify(conn)
    (settings.library_dir / "Photos" / "gone.jpg").unlink()
    scan(conn, settings)

    assert names(search_items(conn, "")) == {"here.jpg"}


def test_a_content_match_does_not_bring_them_back(tmp_path: Path) -> None:
    """Text extracted from inside a document lives in its own index, joined to
    items separately — so it needed the same clause."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    write(settings, "Documents/gone.txt")
    scan(conn, settings)
    classify(conn, category="documents")
    item_id = conn.execute("SELECT id FROM items").fetchone()[0]
    conn.execute(
        "INSERT INTO content_fts(rowid, text, item_id) VALUES (?, ?, ?)",
        (item_id, "quarterly revenue projections", item_id),
    )
    assert search_items(conn, "quarterly", SearchFilters(content=True))

    (settings.library_dir / "Documents" / "gone.txt").unlink()
    scan(conn, settings)

    assert search_items(conn, "quarterly", SearchFilters(content=True)) == []


# --- the surfaces around it -------------------------------------------------


def test_the_page_does_not_render_a_ghost(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    write(settings, "Photos/here.jpg")
    write(settings, "Photos/gone.jpg")
    scan(conn, settings)
    classify(conn)
    (settings.library_dir / "Photos" / "gone.jpg").unlink()
    scan(conn, settings)

    page = client.get("/browse", params={"q": "jpg", "root": "library"}).text
    results = page.split('id="search-results"')[1]

    assert "here.jpg" in results
    assert "gone.jpg" not in results, "not a result"
    # The consistency line is the right place for it, and it is not a result:
    # it is the page saying the index and the disk disagree.
    assert "gone.jpg" in page
    assert "1 missing on disk" in page


def test_history_still_remembers_a_file_that_is_gone(tmp_path: Path) -> None:
    """Search says what is here; History says what happened. Excluding a file
    from one must not remove it from the other."""
    client, conn, settings = client_for(tmp_path)
    write(settings, "Photos/gone.jpg")
    scan(conn, settings)
    classify(conn)
    conn.execute(
        """
        INSERT INTO history(ts, plan_id, op_id, action, src_root, src_relpath,
                            dest_root, dest_relpath, fingerprint, outcome)
        VALUES ('2026-08-01T10:00:00+00:00', 'plan-ghost', 1, 'move', 'inbox',
                'gone.jpg', 'library', 'Photos/gone.jpg', 'abc', 'ok')
        """
    )
    (settings.library_dir / "Photos" / "gone.jpg").unlink()
    scan(conn, settings)

    assert search_items(conn, "gone") == []
    assert "gone.jpg" in client.get("/history").text


def test_browse_does_not_reconstruct_a_file_from_a_stale_row(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    write(settings, "Photos/gone.jpg")
    scan(conn, settings)
    classify(conn)
    (settings.library_dir / "Photos" / "gone.jpg").unlink()
    scan(conn, settings)

    from librairy.web.browse import browse_folder

    data = browse_folder(conn, settings, "Photos")

    assert data["items"] == []
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1


def test_a_preview_of_a_missing_file_fails_safely(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    write(settings, "Photos/gone.jpg")
    scan(conn, settings)
    classify(conn)
    item_id = conn.execute("SELECT id FROM items").fetchone()[0]
    (settings.library_dir / "Photos" / "gone.jpg").unlink()
    scan(conn, settings)

    card = client.get(f"/preview/items/{item_id}")
    thumb = client.get(f"/preview/items/{item_id}/thumb")

    assert card.status_code in (200, 404)
    assert thumb.status_code == 404
    for response in (card, thumb):
        assert str(settings.library_dir) not in response.text
        assert str(tmp_path) not in response.text


# --- item detail ------------------------------------------------------------


def test_item_detail_of_a_live_file_is_unchanged(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    write(settings, "Photos/here.jpg")
    scan(conn, settings)
    classify(conn)
    item_id = conn.execute("SELECT id FROM items").fetchone()[0]

    page = client.get(f"/items/{item_id}").text

    assert "Photos/here.jpg" in page
    assert "Not on disk" not in page
    assert "Need SMB/FTP/WebDAV access help?" in page


def test_item_detail_says_the_file_is_gone(tmp_path: Path) -> None:
    """Honest, not alarming: the record is fine, the file is not there."""
    client, conn, settings = client_for(tmp_path)
    write(settings, "Photos/gone.jpg")
    scan(conn, settings)
    classify(conn)
    item_id = conn.execute("SELECT id FROM items").fetchone()[0]
    (settings.library_dir / "Photos" / "gone.jpg").unlink()
    scan(conn, settings)

    page = client.get(f"/items/{item_id}")

    assert page.status_code == 200, "the record still opens — History points at it"
    assert "Not on disk" in page.text
    assert "library/Photos/gone.jpg" in page.text
    assert "LibrAIry does not" in page.text and "delete records" in page.text


def test_a_missing_item_offers_no_preview_to_open(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    write(settings, "Photos/gone.jpg")
    scan(conn, settings)
    classify(conn)
    item_id = conn.execute("SELECT id FROM items").fetchone()[0]
    (settings.library_dir / "Photos" / "gone.jpg").unlink()
    scan(conn, settings)

    page = client.get(f"/items/{item_id}").text
    panel = client.get(f"/browse/items/{item_id}/panel").text

    for body in (page, panel):
        assert "preview-expand" not in body, "no control that opens nothing"
        assert "Preview unavailable" not in body, "not an error — the file is simply gone"
    assert "not on disk" in panel


def test_no_absolute_path_reaches_a_missing_item_page(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    write(settings, "Photos/gone.jpg")
    scan(conn, settings)
    classify(conn)
    item_id = conn.execute("SELECT id FROM items").fetchone()[0]
    (settings.library_dir / "Photos" / "gone.jpg").unlink()
    scan(conn, settings)

    page = client.get(f"/items/{item_id}").text
    panel = client.get(f"/browse/items/{item_id}/panel").text

    for body in (page, panel):
        assert str(settings.library_dir) not in body
        assert str(tmp_path) not in body
