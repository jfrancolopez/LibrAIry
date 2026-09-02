"""Browse must show what is on disk, not what the index happens to remember.

The bug these lock down: Browse built its folder list out of a *page of indexed
files*. `SELECT ... LIMIT 50`, take the second path component of each row, call
that the directory listing. On the author's library `Photos/` holds 89 indexed
files and the first 83 are inside `2022/`, so `Photos/Unknown/` — starting at
row 83 — did not exist as far as the UI was concerned, while navigating
straight to it worked fine. A folder you can reach but cannot see is worse than
a missing feature: it makes the whole view untrustworthy.

The invariant, and the reason for every test below:

    a normal file or directory that exists beneath the library root is not
    hidden merely because the index, classification, pagination or previous
    navigation does not know about it.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from librairy.config import Settings
from librairy.db import connect
from librairy.models import EvidenceEntry
from librairy.proposals import upsert_proposal
from librairy.scanner import scan_root
from librairy.web.app import create_app
from librairy.web.browse import PAGE_SIZE, browse_folder, library_roots


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


def write(settings: Settings, relpath: str, body: str = "x") -> Path:
    path = settings.library_dir / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def index_all(conn, settings: Settings, category: str = "photos") -> None:
    """Scan and classify everything on disk — the fully-indexed baseline.

    Records the library/index comparison too, the way an idle worker cycle
    does: Browse reports the last comparison rather than making one, because
    making one reads the whole library and the whole index.
    """
    from librairy.consistency import library_consistency, record_consistency

    scan_root(conn, "library", settings.library_dir, settings)
    for row in conn.execute("SELECT id, relpath FROM items WHERE root='library'").fetchall():
        upsert_proposal(
            conn,
            item_id=row["id"],
            category=category,
            clean_name=Path(row["relpath"]).name,
            dest_relpath=row["relpath"],
            confidence=0.9,
            evidence=[EvidenceEntry("heuristic", "category", category, 0.9)],
        )
    record_consistency(conn, library_consistency(conn, settings))
    conn.commit()


def children(conn, settings, category: str, folder: str = "") -> tuple[set[str], set[str]]:
    data = browse_folder(conn, settings, category, folder=folder)
    return (
        {entry["name"] for entry in data["folders"]},
        {item["name"] for item in data["items"]},
    )


def disk_children(settings: Settings, relpath: str) -> tuple[set[str], set[str]]:
    """The consistency checker, in four lines. Reused by every test here."""
    base = settings.library_dir / relpath if relpath else settings.library_dir
    entries = list(base.iterdir())
    return (
        {e.name for e in entries if e.is_dir() and not e.name.startswith(".")},
        {e.name for e in entries if e.is_file() and not e.name.startswith(".")},
    )


# --- directory completeness -------------------------------------------------


def test_a_sibling_folder_beyond_the_page_limit_still_appears(tmp_path: Path) -> None:
    """The exact reported bug, reproduced in miniature.

    `2022/` holds more files than one page, and `Unknown/` sorts after it. The
    old code took the first PAGE_SIZE rows — all of them inside 2022 — and
    derived the folder list from those, so Unknown vanished.
    """
    settings = settings_for(tmp_path)
    conn = connect(settings)
    for index in range(PAGE_SIZE + 20):
        write(settings, f"Photos/2022/shot-{index:03d}.png")
    write(settings, "Photos/Unknown/stray.png")
    write(settings, "Photos/Unsorted/other.png")
    index_all(conn, settings)

    folders, _ = children(conn, settings, "photos")

    assert folders == {"2022", "Unknown", "Unsorted"}


def test_the_folder_list_is_never_paginated(tmp_path: Path) -> None:
    """Whatever the file count, every folder is on the first page."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    for index in range(PAGE_SIZE * 3):
        write(settings, f"Photos/bulk-{index:03d}.png")
    for name in ("Alpha", "Middle", "Zulu"):
        write(settings, f"Photos/{name}/one.png")
    index_all(conn, settings)

    folders, files = children(conn, settings, "photos")

    assert folders == {"Alpha", "Middle", "Zulu"}
    assert len(files) == PAGE_SIZE, "files still page"


def test_a_folder_of_entirely_unindexed_files_still_appears(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    write(settings, "Photos/2022/known.png")
    index_all(conn, settings)
    # Written after the scan: on disk, absent from the index.
    write(settings, "Photos/Unknown/ghost.png")

    folders, _ = children(conn, settings, "photos")

    assert "Unknown" in folders


def test_an_empty_directory_appears(tmp_path: Path) -> None:
    """It exists beneath the library root, so Browse says so. The old model
    could not represent one at all — no files meant no folder."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    write(settings, "Photos/2022/a.png")
    (settings.library_dir / "Photos" / "EmptyAlbum").mkdir(parents=True)
    index_all(conn, settings)

    folders, _ = children(conn, settings, "photos")

    assert "EmptyAlbum" in folders


def test_an_unsupported_file_type_does_not_hide_its_folder(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    write(settings, "Photos/Archives/backup.7z")
    write(settings, "Photos/2022/a.png")
    index_all(conn, settings)

    folders, _ = children(conn, settings, "photos")

    assert "Archives" in folders


def test_companion_files_are_all_visible_in_browse(tmp_path: Path) -> None:
    """The sidecar work deliberately keeps some companions out of search. That
    must never mean out of Browse — the folder is what is on disk."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    for name in ("The Matrix (1999).mkv", "The Matrix (1999).en.srt", "movie.nfo"):
        write(settings, f"Movies/The Matrix (1999)/{name}")
    index_all(conn, settings, category="movies")

    _, files = children(conn, settings, "movies", folder="The Matrix (1999)")

    assert files == {"The Matrix (1999).mkv", "The Matrix (1999).en.srt", "movie.nfo"}


def test_hidden_and_ignored_entries_stay_hidden(tmp_path: Path) -> None:
    """Junk stays out — but hiding junk must not hide a real folder."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    write(settings, "Photos/Unknown/real.png")
    write(settings, "Photos/.DS_Store")
    (settings.library_dir / "Photos" / ".hidden").mkdir(parents=True)
    index_all(conn, settings)

    folders, files = children(conn, settings, "photos")

    assert ".hidden" not in folders
    assert ".DS_Store" not in files
    assert "Unknown" in folders


# --- indexed vs unindexed vs stale -----------------------------------------


def test_an_indexed_file_carries_its_metadata(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    write(settings, "Photos/2022/a.png", "hello")
    index_all(conn, settings)

    data = browse_folder(conn, settings, "photos", folder="2022")
    entry = data["items"][0]

    assert entry["item_id"] is not None
    assert entry["indexed"] is True


def test_an_unindexed_file_is_listed_and_says_so(tmp_path: Path) -> None:
    """Metadata unavailable is not the same claim as file does not exist."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    write(settings, "Photos/2022/indexed.png")
    index_all(conn, settings)
    write(settings, "Photos/2022/later.png", "written after the scan")

    data = browse_folder(conn, settings, "photos", folder="2022")
    by_name = {item["name"]: item for item in data["items"]}

    assert set(by_name) == {"indexed.png", "later.png"}
    assert by_name["later.png"]["indexed"] is False
    assert by_name["later.png"]["item_id"] is None
    # It still gets a real size, read off the disk.
    assert by_name["later.png"]["size"]


def test_an_index_row_whose_file_is_gone_is_not_presented_as_healthy(
    tmp_path: Path,
) -> None:
    """The reverse consistency problem. Browse lists what is there, so a stale
    row simply is not there — it must never render as a normal file."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    write(settings, "Photos/2022/deleted.png")
    write(settings, "Photos/2022/kept.png")
    index_all(conn, settings)
    (settings.library_dir / "Photos" / "2022" / "deleted.png").unlink()

    _, files = children(conn, settings, "photos", folder="2022")

    assert files == {"kept.png"}


# --- navigation stability ---------------------------------------------------


def test_navigating_into_a_child_does_not_change_the_parent(tmp_path: Path) -> None:
    """Photos → 2022 → Photos yields the same Photos."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    for index in range(PAGE_SIZE + 5):
        write(settings, f"Photos/2022/f-{index:03d}.png")
    write(settings, "Photos/Unknown/a.png")
    index_all(conn, settings)

    first = children(conn, settings, "photos")
    children(conn, settings, "photos", folder="2022")
    again = children(conn, settings, "photos")

    assert first == again
    assert first[0] == {"2022", "Unknown"}


def test_the_same_url_always_returns_the_same_children(tmp_path: Path) -> None:
    """Reload, back, and a fresh tab are all just this GET again."""
    client, conn, settings = client_for(tmp_path)
    for index in range(PAGE_SIZE + 5):
        write(settings, f"Photos/2022/f-{index:03d}.png")
    write(settings, "Photos/Unknown/a.png")
    index_all(conn, settings)

    first = client.get("/browse/photos").text
    client.get("/browse/photos?folder=2022")
    reloaded = client.get("/browse/photos").text
    fresh = TestClient(create_app(settings, conn))
    fresh.post("/login", data={"password": "correct horse battery"})
    other_tab = fresh.get("/browse/photos").text

    assert "Unknown" in first
    for body in (reloaded, other_tab):
        assert _folder_names(body) == _folder_names(first)


def _folder_names(html: str) -> list[str]:
    import re

    return re.findall(r'class="browse-row is-folder"[^>]*>\s*<span>([^<]*)</span>', html)


def test_the_partial_and_the_full_page_agree(tmp_path: Path) -> None:
    """The explorer swaps fragments; a fragment must not know less than a page."""
    client, conn, settings = client_for(tmp_path)
    for index in range(PAGE_SIZE + 5):
        write(settings, f"Photos/2022/f-{index:03d}.png")
    write(settings, "Photos/Unknown/a.png")
    index_all(conn, settings)

    full = client.get("/browse/photos").text
    partial = client.get("/browse/photos/files?folder=&page=1").text

    assert "Unknown" in full
    # The files fragment pages exactly like the page it came from.
    assert partial.count('class="browse-row is-item') == PAGE_SIZE or "No files" in partial


def test_breadcrumbs_lead_back_to_the_same_listing(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    write(settings, "Photos/2022/Deep/a.png")
    write(settings, "Photos/Unknown/b.png")
    index_all(conn, settings)

    deep = client.get("/browse/photos?folder=2022/Deep").text
    assert 'href="/browse/Photos"' in deep, "a crumb back to the category"

    up = client.get("/browse/photos").text
    assert _folder_names(up) == ["2022", "Unknown"]


def test_ordering_is_deterministic_and_not_insertion_order(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    for name in ("Zulu", "alpha", "2023", "2022", "Unknown"):
        write(settings, f"Photos/{name}/a.png")
    index_all(conn, settings)

    data = browse_folder(conn, settings, "photos")
    names = [entry["name"] for entry in data["folders"]]

    assert names == sorted(names, key=str.lower)
    assert browse_folder(conn, settings, "photos")["folders"] == data["folders"]


# --- security ---------------------------------------------------------------


def test_traversal_cannot_escape_the_library_root(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    write(settings, "Photos/2022/a.png")
    index_all(conn, settings)
    (tmp_path / "outside.txt").write_text("secret", encoding="utf-8")

    for attack in ("../..", "../../..", "2022/../../..", "/etc", "..%2F..", "....//"):
        with pytest.raises(ValueError, match="no such folder"):
            browse_folder(conn, settings, "photos", folder=attack)


def test_traversal_through_the_route_is_refused(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    write(settings, "Photos/2022/a.png")
    index_all(conn, settings)
    (tmp_path / "outside.txt").write_text("secret", encoding="utf-8")

    for attack in ("../../outside.txt", "%2e%2e%2f%2e%2e", "..%2F.."):
        response = client.get(f"/browse/photos?folder={attack}")
        assert response.status_code in (200, 404)
        assert "secret" not in response.text
        assert str(tmp_path) not in response.text


def test_a_symlinked_directory_is_not_followed(tmp_path: Path) -> None:
    """The scanner skips symlinks; Browse uses the same predicate, so the two
    cannot disagree about what the library contains."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    write(settings, "Photos/2022/a.png")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "secret.png").write_text("secret", encoding="utf-8")
    try:
        (settings.library_dir / "Photos" / "linked").symlink_to(outside)
    except OSError:  # pragma: no cover - platform without symlink permission
        return
    index_all(conn, settings)

    folders, _ = children(conn, settings, "photos")

    assert "linked" not in folders


def test_no_absolute_host_path_reaches_the_page(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    write(settings, "Photos/Unknown/a.png")
    index_all(conn, settings)

    body = client.get("/browse/photos?folder=Unknown").text

    assert str(settings.library_dir) not in body
    assert str(tmp_path) not in body


# --- the checker itself -----------------------------------------------------


def test_browse_matches_the_filesystem_across_a_whole_tree(tmp_path: Path) -> None:
    """The consistency check, run over a tree with every awkward case in it:
    a folder past the page limit, an unindexed folder, an empty one, hidden
    junk, and a companion file."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    for index in range(PAGE_SIZE + 30):
        write(settings, f"Photos/2022/f-{index:03d}.png")
    write(settings, "Photos/Unknown/Unsorted/a.png")
    write(settings, "Photos/Unknown/b.png")
    write(settings, "Photos/.DS_Store")
    (settings.library_dir / "Photos" / "Empty").mkdir(parents=True)
    index_all(conn, settings)
    write(settings, "Photos/Unknown/unindexed.png")

    for folder in ("", "2022", "Unknown", "Unknown/Unsorted", "Empty"):
        disk_dirs, disk_files = disk_children(settings, f"Photos/{folder}".rstrip("/"))
        browse_dirs, browse_files = children(conn, settings, "photos", folder=folder)
        assert disk_dirs == browse_dirs, f"folders differ at Photos/{folder}"
        # Files page; compare only when the folder fits on one page.
        if len(disk_files) <= PAGE_SIZE:
            assert disk_files == browse_files, f"files differ at Photos/{folder}"


# --- the root screen --------------------------------------------------------
#
# The same invariant one level up. The root used to be a hard-coded tuple of
# eight classification categories counted with a GROUP BY on
# `search_fts.category`, so it could not show a folder the owner made and it
# counted rows rather than files.


def roots(settings: Settings) -> dict[str, int]:
    return {entry["name"]: entry["count"] for entry in library_roots(settings)}


def test_a_top_level_folder_appears_with_nothing_indexed_in_it(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    write(settings, "Photos/a.png")
    index_all(conn, settings)
    write(settings, "Archives/scan.pdf")

    assert roots(settings) == {"Archives": 1, "Photos": 1}


def test_an_empty_top_level_folder_appears(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    (settings.library_dir / "Archives").mkdir()

    assert roots(settings) == {"Archives": 0}


def test_the_count_is_every_file_underneath_at_any_depth(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    for relpath in ("Photos/a.png", "Photos/2022/b.png", "Photos/2022/Deep/c.png"):
        write(settings, relpath)

    # Three files, not one direct child and not two subfolders.
    assert roots(settings) == {"Photos": 3}


def test_an_unindexed_file_counts_towards_the_tile(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    write(settings, "Photos/indexed.png")
    index_all(conn, settings)
    write(settings, "Photos/later.png")

    assert roots(settings) == {"Photos": 2}


def test_a_stale_row_does_not_count_towards_the_tile(tmp_path: Path) -> None:
    """A row whose file is gone used to keep inflating the number."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    write(settings, "Photos/here.png")
    write(settings, "Photos/gone.png")
    index_all(conn, settings)
    (settings.library_dir / "Photos" / "gone.png").unlink()

    assert roots(settings) == {"Photos": 1}
    assert conn.execute("SELECT COUNT(*) FROM items WHERE root='library'").fetchone()[0] == 2


def test_hidden_and_ignored_files_do_not_inflate_the_tile(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    settings.ignore_patterns = ["*.tmp"]
    write(settings, "Photos/a.png")
    write(settings, "Photos/.DS_Store")
    write(settings, "Photos/2022/.hidden.png")
    write(settings, "Photos/2022/scratch.tmp")

    assert roots(settings) == {"Photos": 1}


def test_a_hidden_or_symlinked_top_level_folder_is_not_a_root(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    write(settings, "Photos/a.png")
    (settings.library_dir / ".cache").mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "secret.png").write_text("secret", encoding="utf-8")
    with contextlib.suppress(OSError):  # a platform without symlink permission
        (settings.library_dir / "linked").symlink_to(outside)

    assert roots(settings) == {"Photos": 1}


def test_a_loose_file_at_the_library_root_is_not_a_root(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    write(settings, "Photos/a.png")
    (settings.library_dir / "stray.txt").write_text("x", encoding="utf-8")

    assert roots(settings) == {"Photos": 1}


def test_root_order_is_deterministic_and_not_creation_order(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    for name in ("Zulu", "archives", "Photos", "2019"):
        write(settings, f"{name}/a.png")

    names = [entry["name"] for entry in library_roots(settings)]

    assert names == ["2019", "archives", "Photos", "Zulu"]
    assert names == [entry["name"] for entry in library_roots(settings)]


def test_paging_below_the_root_cannot_change_the_root(tmp_path: Path) -> None:
    """The old count came off the same table the old folder list was truncated
    from; this pins that the two can no longer interact at all."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    for index in range(PAGE_SIZE + 30):
        write(settings, f"Photos/2022/f-{index:03d}.png")
    write(settings, "Photos/Unknown/a.png")
    index_all(conn, settings)

    before = roots(settings)
    for page in (1, 2, 3):
        assert browse_folder(conn, settings, "photos", page=page)

    assert before == {"Photos": PAGE_SIZE + 31} == roots(settings)


def test_the_tile_count_equals_what_the_explorer_can_reach(tmp_path: Path) -> None:
    """Walk the explorer the way a person would and count what it shows."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    write(settings, "Photos/loose.png")
    write(settings, "Photos/2022/a.png")
    write(settings, "Photos/Unknown/Unsorted/b.png")
    (settings.library_dir / "Photos" / "Empty").mkdir(parents=True)
    index_all(conn, settings)

    def walk(folder: str = "") -> int:
        data = browse_folder(conn, settings, "Photos", folder=folder)
        total = len(data["items"])
        for entry in data["folders"]:
            child = f"{folder}/{entry['name']}" if folder else entry["name"]
            total += walk(child)
        return total

    assert walk() == roots(settings)["Photos"] == 3


def test_a_root_tile_opens_the_folder_it_names(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    write(settings, "Movies & TV/a.mkv")
    index_all(conn, settings)

    entry = library_roots(settings)[0]
    opened = client.get(entry["href"])

    assert entry["name"] == "Movies & TV"
    assert entry["href"] == "/browse/Movies%20%26%20TV"
    assert opened.status_code == 200
    assert "a.mkv" in opened.text


def test_going_in_and_back_out_leaves_the_root_identical(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    write(settings, "Photos/2022/a.png")
    write(settings, "Music/song.flac")
    index_all(conn, settings)

    first = client.get("/browse").text
    client.get("/browse/Photos?folder=2022")
    client.get("/browse/Music")

    assert client.get("/browse").text == first
    assert client.get("/browse").text == first, "and again on refresh"


def test_a_folder_count_means_the_same_thing_as_a_tile(tmp_path: Path) -> None:
    """A bare number has to mean one thing. It used to count direct entries in
    the folder pane — subfolders included — and files-at-any-depth on a tile."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    write(settings, "Photos/2022/a.png")
    write(settings, "Photos/2022/Deep/b.png")
    write(settings, "Photos/2022/Deep/Deeper/c.png")
    write(settings, "Photos/loose.png")
    (settings.library_dir / "Photos" / "Empty").mkdir(parents=True)
    index_all(conn, settings)

    data = browse_folder(conn, settings, "Photos")
    counts = {entry["name"]: entry["count"] for entry in data["folders"]}

    assert counts == {"2022": 3, "Empty": 0}
    assert roots(settings)["Photos"] == 4, "and the tile counts the loose file too"


def test_a_folder_count_ignores_the_same_junk_the_tile_does(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    settings.ignore_patterns = ["*.tmp"]
    conn = connect(settings)
    write(settings, "Photos/2022/a.png")
    write(settings, "Photos/2022/.DS_Store")
    write(settings, "Photos/2022/scratch.tmp")
    index_all(conn, settings)

    data = browse_folder(conn, settings, "Photos")

    assert data["folders"][0]["count"] == 1


# --- files lying directly in the library root -------------------------------
#
# Indexed, searchable, with a detail page — and nowhere in Browse they could
# appear, because the root screen lists directories and the explorer only opens
# one. Nothing should have to be put in a folder before Browse admits it exists.


def test_a_file_in_the_library_root_is_shown(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    write(settings, "Photos/a.png")
    (settings.library_dir / "loose-root.pdf").write_text("x", encoding="utf-8")
    index_all(conn, settings)

    home = client.get("/browse").text

    assert "loose-root.pdf" in home
    assert "In the library root" in home
    assert "<span>Photos</span>" in home, "and the folder tiles are still there"


def test_the_section_is_absent_when_the_root_holds_only_folders(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    write(settings, "Photos/a.png")
    index_all(conn, settings)

    assert "In the library root" not in client.get("/browse").text


def test_an_unindexed_root_file_is_shown_and_says_so(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    write(settings, "Photos/a.png")
    index_all(conn, settings)
    (settings.library_dir / "dropped-in.pdf").write_text("x", encoding="utf-8")

    home = client.get("/browse").text

    assert "dropped-in.pdf" in home
    assert "not indexed" in home
    assert conn.execute(
        "SELECT COUNT(*) FROM items WHERE relpath='dropped-in.pdf'"
    ).fetchone()[0] == 0, "looking at it does not index it"


def test_root_hidden_junk_and_symlinks_stay_out(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    settings.ignore_patterns = ["*.tmp"]
    write(settings, "Photos/a.png")
    (settings.library_dir / "real.pdf").write_text("x", encoding="utf-8")
    (settings.library_dir / ".DS_Store").write_text("x", encoding="utf-8")
    (settings.library_dir / "scratch.tmp").write_text("x", encoding="utf-8")
    outside = tmp_path / "elsewhere.pdf"
    outside.write_text("secret", encoding="utf-8")
    with contextlib.suppress(OSError):  # a platform without symlink permission
        (settings.library_dir / "linked.pdf").symlink_to(outside)
    index_all(conn, settings)

    home = client.get("/browse").text

    assert "real.pdf" in home
    for hidden in (".DS_Store", "scratch.tmp", "linked.pdf"):
        assert hidden not in home


def test_root_files_page_but_folders_never_do(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    for name in ("Music", "Photos", "Zulu"):
        write(settings, f"{name}/a.png")
    for index in range(PAGE_SIZE + 12):
        (settings.library_dir / f"loose-{index:03d}.pdf").write_text("x", encoding="utf-8")
    index_all(conn, settings)

    home = client.get("/browse").text
    more = client.get("/browse/root-files", params={"page": 2}).text

    assert home.count('class="browse-row is-item') == PAGE_SIZE
    for folder in ("Music", "Photos", "Zulu"):
        assert f"<span>{folder}</span>" in home, "a folder cannot be paged away"
    assert "Load more" in home
    assert '/browse/root-files?folder=&page=2' in home
    assert more.count('class="browse-row is-item') == 12
    assert "<html" not in more, "a fragment, not a whole page"
    assert "Load more" not in more


def test_the_root_files_route_wins_over_a_folder_of_that_name(tmp_path: Path) -> None:
    """`/browse/root-files` is registered before `/browse/{top}`."""
    client, conn, settings = client_for(tmp_path)
    (settings.library_dir / "root-files").mkdir()
    write(settings, "root-files/decoy.png")
    (settings.library_dir / "loose.pdf").write_text("x", encoding="utf-8")
    index_all(conn, settings)

    response = client.get("/browse/root-files")

    assert "loose.pdf" in response.text
    assert "decoy.png" not in response.text


def test_a_root_file_keeps_its_preview_and_detail_page(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    write(settings, "Photos/a.png")
    (settings.library_dir / "snap.png").write_text("x", encoding="utf-8")
    index_all(conn, settings)
    item_id = conn.execute(
        "SELECT id FROM items WHERE relpath='snap.png'"
    ).fetchone()[0]

    home = client.get("/browse").text

    assert f'href="/items/{item_id}"' in home
    assert f'/preview/items/{item_id}/thumb' in home
    assert client.get(f"/items/{item_id}").status_code == 200
    assert 'id="lightbox"' in home, "the viewer ships with the page that shows previews"


def test_a_root_file_counts_towards_consistency_but_no_tile(tmp_path: Path) -> None:
    """It is in the library, so the total counts it. It is in no folder, so no
    tile claims it — and the two numbers are allowed to differ by exactly that."""
    client, conn, settings = client_for(tmp_path)
    write(settings, "Photos/a.png")
    (settings.library_dir / "loose.pdf").write_text("x", encoding="utf-8")
    index_all(conn, settings)

    home = client.get("/browse").text

    assert roots(settings) == {"Photos": 1}
    assert "2 files · index up to date" in home
