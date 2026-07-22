from __future__ import annotations

from pathlib import Path

from librairy.config import Settings
from librairy.db import connect
from librairy.indexer import apply_library_pattern, find_pattern, index_library, rebuild_pattern_map


def settings_for(tmp_path: Path) -> Settings:
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        LIBRARY_DIR=tmp_path / "library",
        FILE_STABILITY_SECONDS=0,
        _env_file=None,
    )
    settings.library_dir.mkdir()
    return settings


def test_indexing_library_creates_items_and_pattern_map_without_writes(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    track = settings.library_dir / "Music/Queen/News/track.mp3"
    track.parent.mkdir(parents=True)
    track.write_text("audio", encoding="utf-8")
    before = track.stat().st_mtime_ns
    conn = connect(settings)

    summary = index_library(conn, settings)

    assert summary.discovered == 1
    assert track.stat().st_mtime_ns == before
    assert conn.execute("SELECT COUNT(*) FROM items WHERE root='library'").fetchone()[0] == 1
    pattern = find_pattern(conn, "artist", "Queen")
    assert pattern is not None
    assert pattern.dest_base == "Music/Queen"


def test_existing_artist_pattern_overrides_genre_guess(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    track = settings.library_dir / "Music/Queen/News/track.mp3"
    track.parent.mkdir(parents=True)
    track.write_text("audio", encoding="utf-8")
    conn = connect(settings)
    index_library(conn, settings)

    dest, evidence = apply_library_pattern(
        conn,
        kind="artist",
        key="Queen",
        clean_name="new.mp3",
    )

    assert dest == "Music/Queen/new.mp3"
    assert evidence is not None
    assert evidence.source == "library-pattern"


def test_pattern_map_reflects_new_committed_item_without_full_rescan(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    conn.execute(
        """
        INSERT INTO items(root, relpath, size, mtime_ns, fingerprint, first_seen_at, last_seen_at)
        VALUES ('library', 'Shows/Example Show/Season 01/S01E01.mkv', 1, 1, 'abc', 'now', 'now')
        """
    )

    rebuild_pattern_map(conn)

    pattern = find_pattern(conn, "show", "Example Show")
    assert pattern is not None
    assert pattern.dest_base == "Shows/Example Show"
