"""What a scan does to the index, pinned so the contract stops being folklore.

The last audit reported one item with no `search_fts` row and wondered whether
FTS was optional for DVD structural files. It is not: `sync_search_item` has no
extension filter and every item gets an entry. The 242-of-243 reading came from
copying a live database that had been killed mid-write — `sync_search_item` is
a DELETE followed by an INSERT, and a torn copy lands between them.

Rather than take that on trust, these assert the contract directly.

Deterministic: no AI provider, no catalog, no network. `index_library` is the
supported reconciliation path and reaches none of them.
"""

from __future__ import annotations

from pathlib import Path

from librairy.config import Settings
from librairy.db import connect
from librairy.indexer import index_library
from librairy.scanner import scan_root
from librairy.search import SearchFilters, rebuild_search_index, search_items

DISC = "Queen - 1979-12-26 - The Queen Special on TV - DVD5/VIDEO_TS"
DISC_FILES = (
    "VIDEO_TS.IFO",
    "VIDEO_TS.BUP",
    "VIDEO_TS.VOB",
    "VTS_01_0.IFO",
    "VTS_01_0.BUP",
    "VTS_01_1.VOB",
)


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


def write(settings: Settings, relpath: str) -> Path:
    path = settings.library_dir / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(relpath, encoding="utf-8")
    return path


def scan(conn, settings: Settings) -> None:
    scan_root(conn, "library", settings.library_dir, settings)


def counts(conn) -> tuple[int, int]:
    return (
        conn.execute("SELECT COUNT(*) FROM items").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM search_fts").fetchone()[0],
    )


# --- the contract -----------------------------------------------------------


def test_every_scanned_file_gets_exactly_one_search_entry(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    for relpath in ("Photos/a.png", "Projects/notes.xyz", "Music/song.flac", "loose.pdf"):
        write(settings, relpath)
    scan(conn, settings)

    assert counts(conn) == (4, 4)
    assert conn.execute(
        "SELECT COUNT(*) FROM items i LEFT JOIN search_fts s ON s.item_id=i.id "
        "WHERE s.item_id IS NULL"
    ).fetchone()[0] == 0


def test_the_scanner_has_no_extension_filter(tmp_path: Path) -> None:
    """"Not indexed" therefore always means "not scanned yet", which is what
    the Browse consistency line tells the owner."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    for name in ("thing.xyz", "no-extension", "archive.7z", "disc.IFO", "sheet.cue"):
        write(settings, f"Misc/{name}")
    scan(conn, settings)

    assert counts(conn) == (5, 5)


def test_a_dvd_structure_is_indexed_file_by_file_with_names_intact(tmp_path: Path) -> None:
    """The exact case the last audit flagged. `.IFO` is not special, the names
    are preserved verbatim, and each file gets its own entry."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    for name in DISC_FILES:
        write(settings, f"{DISC}/{name}")
    scan(conn, settings)

    assert counts(conn) == (len(DISC_FILES), len(DISC_FILES))
    indexed = {
        row["relpath"].rsplit("/", 1)[1]
        for row in conn.execute(
            "SELECT i.relpath FROM search_fts s JOIN items i ON i.id=s.item_id"
        )
    }
    assert indexed == set(DISC_FILES)


def test_the_ifo_file_is_findable_by_name(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    for name in DISC_FILES:
        write(settings, f"{DISC}/{name}")
    scan(conn, settings)

    rows = search_items(conn, "VIDEO_TS.IFO", SearchFilters(root="library"))

    found = [Path(str(row["relpath"])).name for row in rows]
    # The name is tokenised, so `IFO` also matches its sibling — that is FTS
    # working, not a leak. What matters is that the structural file is there
    # and ranks first, and that the disc's real filenames were preserved.
    assert found[0] == "VIDEO_TS.IFO"
    assert set(found) <= set(DISC_FILES)


def test_rescanning_does_not_duplicate_anything(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    for name in DISC_FILES:
        write(settings, f"{DISC}/{name}")
    for _ in range(3):
        scan(conn, settings)

    assert counts(conn) == (len(DISC_FILES), len(DISC_FILES))


def test_rebuilding_the_search_index_reproduces_it_exactly(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    for relpath in ("Photos/a.png", f"{DISC}/VIDEO_TS.IFO", "loose.pdf"):
        write(settings, relpath)
    scan(conn, settings)
    before = {
        tuple(row)
        for row in conn.execute("SELECT item_id, name, category, root FROM search_fts")
    }

    rebuilt = rebuild_search_index(conn)

    assert rebuilt == 3
    assert counts(conn) == (3, 3)
    assert {
        tuple(row)
        for row in conn.execute("SELECT item_id, name, category, root FROM search_fts")
    } == before


def test_a_missing_item_keeps_its_entry_through_a_rebuild(tmp_path: Path) -> None:
    """Rebuild refills from `items`, which is why filtering at query time is
    the smaller model: a rebuild cannot disagree with it."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    write(settings, "Photos/gone.png")
    scan(conn, settings)
    (settings.library_dir / "Photos" / "gone.png").unlink()
    scan(conn, settings)

    rebuild_search_index(conn)

    assert counts(conn) == (1, 1)
    assert search_items(conn, "gone") == []
    assert conn.execute(
        "SELECT missing_since FROM items"
    ).fetchone()[0] is not None


# --- what the supported scan actually does ----------------------------------


def test_a_library_scan_marks_clears_and_never_removes(tmp_path: Path) -> None:
    """`librairy scan --root library`, end to end, in one test — because the
    documentation now describes this and must not overpromise.

    It marks what is gone, clears what came back, indexes what is new, and
    deletes nothing. It does not classify: no proposal appears here, and the
    module reaches no provider or catalog.
    """
    settings = settings_for(tmp_path)
    conn = connect(settings)
    write(settings, "Photos/stays.png")
    write(settings, "Photos/leaves.png")
    index_library(conn, settings)
    assert counts(conn) == (2, 2)
    assert conn.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 0

    (settings.library_dir / "Photos" / "leaves.png").unlink()
    write(settings, "Photos/arrives.png")
    index_library(conn, settings)

    state = {
        row["relpath"]: row["missing_since"]
        for row in conn.execute("SELECT relpath, missing_since FROM items")
    }
    assert state["Photos/stays.png"] is None
    assert state["Photos/arrives.png"] is None
    assert state["Photos/leaves.png"] is not None
    assert counts(conn) == (3, 3), "nothing was removed, one was added"

    write(settings, "Photos/leaves.png")
    index_library(conn, settings)

    assert conn.execute(
        "SELECT missing_since FROM items WHERE relpath='Photos/leaves.png'"
    ).fetchone()[0] is None
    assert counts(conn) == (3, 3), "and still nothing duplicated"


def test_a_scan_only_reconciles_the_root_it_scanned(tmp_path: Path) -> None:
    """Which is why inbox records can sit missing while the library is clean —
    the worker only ever scans the inbox, and this only scans the library."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    (settings.inbox_dir / "dropped.mkv").write_text("x", encoding="utf-8")
    write(settings, "Photos/a.png")
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    index_library(conn, settings)

    (settings.inbox_dir / "dropped.mkv").unlink()
    index_library(conn, settings)

    assert conn.execute(
        "SELECT missing_since FROM items WHERE root='inbox'"
    ).fetchone()[0] is None, "a library scan cannot speak for the inbox"

    scan_root(conn, "inbox", settings.inbox_dir, settings)

    assert conn.execute(
        "SELECT missing_since FROM items WHERE root='inbox'"
    ).fetchone()[0] is not None
