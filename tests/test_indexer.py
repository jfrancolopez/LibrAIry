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
    """Only the part above the artist is replaced; the album stays.

    This used to return `f"{dest_base}/{clean_name}"`, so a genre-first render
    of `Music/Rock/Queen/A-Night/01.mp3` came back as `Music/Queen/01.mp3` —
    the album flattened away. Nothing called it, so nobody found out.
    """
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
        relpath="Music/Rock/Queen/A-Night-at-the-Opera/01-bohemian.mp3",
    )

    assert dest == "Music/Queen/A-Night-at-the-Opera/01-bohemian.mp3"
    assert evidence is not None
    assert evidence.source == "library-pattern"


def test_a_genre_first_library_does_not_register_its_genres_as_artists(
    tmp_path: Path,
) -> None:
    """The old map always read parts[1], so `Music/Rock/Queen/…` recorded an
    artist called "Rock" and never one called "Queen". Both depths are
    recorded now and the lookup by real name picks the right one."""
    settings = settings_for(tmp_path)
    track = settings.library_dir / "Music/Rock/Queen/News/track.mp3"
    track.parent.mkdir(parents=True)
    track.write_text("audio", encoding="utf-8")
    conn = connect(settings)

    index_library(conn, settings)

    assert find_pattern(conn, "artist", "Queen").dest_base == "Music/Rock/Queen"


def test_a_season_folder_is_not_the_name_of_a_show(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    episode = settings.library_dir / "Shows/Breaking Bad/Season 01/s01e01.mkv"
    episode.parent.mkdir(parents=True)
    episode.write_text("video", encoding="utf-8")
    conn = connect(settings)

    index_library(conn, settings)

    assert find_pattern(conn, "show", "Breaking Bad").dest_base == "Shows/Breaking Bad"
    assert find_pattern(conn, "show", "Season 01") is None


def test_no_matching_folder_leaves_the_template_alone(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    settings.library_dir.mkdir(parents=True, exist_ok=True)
    conn = connect(settings)
    index_library(conn, settings)

    dest, evidence = apply_library_pattern(
        conn, kind="artist", key="Nobody", relpath="Music/Rock/Nobody/x.mp3"
    )

    assert dest is None
    assert evidence is None


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


def test_a_new_track_lands_in_the_artist_folder_you_already_have(
    tmp_path: Path, monkeypatch
) -> None:
    """The whole promise of "fits your existing layout", end to end.

    Both halves of this were written in phase 2 and neither was ever called,
    so it had never once happened. With a genre-first template and an existing
    Music/Queen/ in the library, a new Queen track must join it rather than
    starting a second Music/Rock/Queen/ beside it.
    """
    from librairy.classify import analyze_items
    from librairy.scanner import scan_root
    from librairy.taxonomy import set_template_style

    # Its own Settings: the shared helper leaves INBOX_DIR at the container
    # default, and this is the one test here that puts anything in an inbox.
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        LIBRARY_DIR=tmp_path / "library",
        INBOX_DIR=tmp_path / "inbox",
        QUARANTINE_DIR=tmp_path / "quarantine",
        FILE_STABILITY_SECONDS=0,
        OLLAMA_HOST="",
        _env_file=None,
    )
    existing = settings.library_dir / "Music/Queen/A Night at the Opera/01 track.mp3"
    existing.parent.mkdir(parents=True)
    existing.write_text("audio", encoding="utf-8")
    settings.inbox_dir.mkdir(parents=True, exist_ok=True)
    conn = connect(settings)
    set_template_style(conn, "music", "genre-first")
    index_library(conn, settings)

    # Standing in for ffprobe: embedded tags are the only thing that yields a
    # real artist name, and a genuinely tagged MP3 is not worth carrying in
    # the repository to prove a path-rewriting rule. Everything downstream —
    # classify, render, the pattern lookup, upsert — is the real thing.
    monkeypatch.setattr(
        "librairy.classify._audio_tags",
        lambda path, settings: {
            "artist": "Queen",
            "album": "News of the World",
            "title": path.stem,
            "genre": "Rock",
        },
    )
    album = settings.inbox_dir / "Queen - News of the World"
    album.mkdir(parents=True)
    for track in ("01 - We Will Rock You.mp3", "02 - We Are The Champions.mp3",
                  "03 - Sheer Heart Attack.mp3"):
        (album / track).write_text("audio", encoding="utf-8")
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    analyze_items(conn, settings)

    row = conn.execute(
        """
        SELECT p.dest_relpath, p.evidence FROM proposals p JOIN items i ON i.id = p.item_id
        WHERE i.root='inbox' AND p.dest_relpath IS NOT NULL LIMIT 1
        """
    ).fetchone()
    assert row is not None, "the album should have been proposed"
    assert row["dest_relpath"].startswith("Music/Queen/"), row["dest_relpath"]
    assert "library-pattern" in row["evidence"]
