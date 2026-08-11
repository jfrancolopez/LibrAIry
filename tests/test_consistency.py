"""Browse shows the disk, Search answers from the index. This is the gap.

Nothing rescans the library on a schedule — the worker watches the inbox only,
and library rows are written by the commit executor as it moves files in. So a
file copied straight into the library over SMB is browsable and unfindable, and
before this there was no way to tell except by searching for something you were
looking straight at.

These pin the two things that make the reading worth trusting: it counts the
same files Browse shows, and it never repairs anything.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

from fastapi.testclient import TestClient

from librairy.config import Settings
from librairy.consistency import consistency_view, library_consistency
from librairy.db import connect
from librairy.scanner import scan_root
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


def write(settings: Settings, relpath: str, body: str = "x") -> Path:
    path = settings.library_dir / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def scan(conn, settings: Settings) -> None:
    scan_root(conn, "library", settings.library_dir, settings)


def test_a_fully_scanned_library_reports_no_drift(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    write(settings, "Photos/2022/a.png")
    write(settings, "Music/Queen/b.flac")
    scan(conn, settings)

    state = library_consistency(conn, settings)

    assert state.matches
    assert state.physical_files == state.indexed_files == 2
    assert state.unindexed_files == state.missing_files == 0
    assert consistency_view(state)["summary"] == "2 files · index up to date"


def test_a_file_copied_in_after_the_scan_is_reported(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    write(settings, "Photos/2022/a.png")
    scan(conn, settings)
    write(settings, "Photos/2022/dropped-in.png")

    state = library_consistency(conn, settings)

    assert not state.matches
    assert state.physical_files == 2
    assert state.indexed_files == 1
    assert state.unindexed_files == 1
    assert state.unindexed_sample == ("Photos/2022/dropped-in.png",)
    assert consistency_view(state)["summary"] == "2 files · 1 not indexed"


def test_a_row_whose_file_is_gone_is_reported_separately(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    write(settings, "Photos/here.png")
    write(settings, "Photos/gone.png")
    scan(conn, settings)
    (settings.library_dir / "Photos" / "gone.png").unlink()

    state = library_consistency(conn, settings)

    assert state.physical_files == 1
    assert state.missing_files == 1
    assert state.missing_sample == ("Photos/gone.png",)
    assert state.unindexed_files == 0
    assert consistency_view(state)["summary"] == "1 file · 1 missing on disk"


def test_hidden_and_ignored_files_are_not_drift(tmp_path: Path) -> None:
    """They are invisible to both sides, so neither side is missing anything."""
    settings = settings_for(tmp_path)
    settings.ignore_patterns = ["*.tmp"]
    conn = connect(settings)
    write(settings, "Photos/a.png")
    scan(conn, settings)
    write(settings, "Photos/.DS_Store")
    write(settings, "Photos/scratch.tmp")

    state = library_consistency(conn, settings)

    assert state.matches
    assert state.physical_files == 1


def test_an_unsupported_file_type_is_indexed_like_anything_else(tmp_path: Path) -> None:
    """There is no visible-but-unindexable tier: the scanner has no extension
    filter, so "not indexed" always means "not scanned yet"."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    write(settings, "Projects/notes.xyz")
    write(settings, "Projects/archive.7z")
    scan(conn, settings)

    state = library_consistency(conn, settings)

    assert state.matches
    assert state.indexed_files == 2


def test_a_symlink_is_not_drift_on_either_side(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    write(settings, "Photos/a.png")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "secret.png").write_text("secret", encoding="utf-8")
    with contextlib.suppress(OSError):  # a platform without symlink permission
        (settings.library_dir / "Photos" / "linked.png").symlink_to(outside / "secret.png")
    scan(conn, settings)

    state = library_consistency(conn, settings)

    assert state.matches
    assert state.physical_files == 1


def test_awkward_filenames_do_not_invent_drift(tmp_path: Path) -> None:
    """Both sides must spell a path the same way, or the checker cries wolf.

    `items.relpath` is written by the scanner as a library-relative POSIX
    string; the walk builds its paths the same way from the same directory
    entries. Accents, spaces and separators are where that would come apart.
    """
    settings = settings_for(tmp_path)
    conn = connect(settings)
    for relpath in (
        "Music/Björk/Homogénic/05 Jóga.flac",
        "Music/AC⁄DC — Back in Black/01.flac",
        "Photos/2022/famille & amis/été (1).jpg",
    ):
        write(settings, relpath)
    scan(conn, settings)

    state = library_consistency(conn, settings)

    assert state.matches, (state.unindexed_sample, state.missing_sample)
    assert state.physical_files == 3


def test_the_sample_is_capped_but_the_counts_are_not(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    write(settings, "Photos/scanned.png")
    scan(conn, settings)
    for index in range(12):
        write(settings, f"Photos/new-{index:02d}.png")

    view = consistency_view(library_consistency(conn, settings))

    assert "12 not indexed" in view["summary"]
    assert len(view["examples"]) == 5
    assert view["more"] == 7


def test_the_remedy_named_is_the_one_that_would_work(tmp_path: Path) -> None:
    """index rebuild rebuilds FTS from rows that already exist — it discovers
    nothing, so naming it here would waste the owner's time."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    write(settings, "Photos/never-scanned.png")

    view = consistency_view(library_consistency(conn, settings))

    assert [note["remedy"] for note in view["notes"]] == ["librairy scan --root library"]
    assert "not scanned yet" in view["notes"][0]["text"]


def test_a_stale_record_is_offered_no_command_that_would_not_work(tmp_path: Path) -> None:
    """A scan sets missing_since and keeps the row, and Search still returns
    it. Naming a command that does not clear this would be a lie; deleting the
    row would be an unasked-for repair. So it explains and offers neither."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    write(settings, "Photos/gone.png")
    scan(conn, settings)
    (settings.library_dir / "Photos" / "gone.png").unlink()
    scan(conn, settings)

    view = consistency_view(library_consistency(conn, settings))

    assert [note["remedy"] for note in view["notes"]] == [None]
    assert "Search can still return them" in view["notes"][0]["text"]
    assert conn.execute("SELECT COUNT(*) FROM items WHERE root='library'").fetchone()[0] == 1


def test_a_scan_closes_the_gap_it_reported(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    write(settings, "Photos/a.png")
    scan(conn, settings)
    write(settings, "Photos/b.png")
    write(settings, "Projects/notes.xyz")
    assert library_consistency(conn, settings).unindexed_files == 2

    scan(conn, settings)

    assert library_consistency(conn, settings).matches


# --- what it must not do ----------------------------------------------------


def test_reading_the_status_changes_nothing(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    write(settings, "Photos/scanned.png")
    scan(conn, settings)
    write(settings, "Photos/unscanned.png")
    write(settings, "Photos/gone.png")
    scan(conn, settings)
    (settings.library_dir / "Photos" / "gone.png").unlink()

    before = _snapshot(conn, settings)
    for _ in range(3):
        library_consistency(conn, settings)

    assert _snapshot(conn, settings) == before


def test_opening_browse_does_not_index_or_repair_anything(tmp_path: Path) -> None:
    """Observation only. Looking at a folder is not consent to touch it."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    write(settings, "Photos/scanned.png")
    write(settings, "Photos/stale.png")
    scan(conn, settings)
    (settings.library_dir / "Photos" / "stale.png").unlink()
    write(settings, "Photos/dropped-in.png")
    client = TestClient(create_app(settings, conn))
    client.post("/setup", data={"password": "correct horse battery"})

    before = _snapshot(conn, settings)
    home = client.get("/browse")
    client.get("/browse/Photos")

    assert home.status_code == 200
    assert "1 not indexed" in home.text
    assert "1 missing on disk" in home.text
    assert "librairy scan --root library" in home.text
    assert _snapshot(conn, settings) == before


def test_the_status_line_is_quiet_when_there_is_nothing_to_say(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    write(settings, "Photos/a.png")
    scan(conn, settings)
    client = TestClient(create_app(settings, conn))
    client.post("/setup", data={"password": "correct horse battery"})

    home = client.get("/browse").text

    assert "1 file · index up to date" in home
    assert "not indexed" not in home
    assert "sync-line is-drift" not in home, "nothing to expand when the two agree"


def test_no_absolute_path_reaches_the_status_line(tmp_path: Path) -> None:
    client_settings = settings_for(tmp_path)
    conn = connect(client_settings)
    write(client_settings, "Photos/unscanned.png")
    client = TestClient(create_app(client_settings, conn))
    client.post("/setup", data={"password": "correct horse battery"})

    home = client.get("/browse").text

    assert "Photos/unscanned.png" in home
    assert str(client_settings.library_dir) not in home
    assert str(tmp_path) not in home


def _snapshot(conn, settings: Settings) -> tuple:
    return (
        [tuple(row) for row in conn.execute("SELECT id, root, relpath, state FROM items")],
        conn.execute("SELECT COUNT(*) FROM search_fts").fetchone()[0],
        sorted(path.name for path in settings.library_dir.rglob("*")),
    )
