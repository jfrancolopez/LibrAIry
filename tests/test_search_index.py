from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from librairy.config import Settings
from librairy.content.extract import process_content_extractions, rebuild_content_index
from librairy.db import (
    MIGRATION_001,
    MIGRATION_002,
    MIGRATION_003,
    MIGRATION_004,
    MIGRATION_005,
    MIGRATION_006,
    MIGRATION_007,
    connect,
)
from librairy.executor import execute_plan
from librairy.models import EvidenceEntry
from librairy.planner import approve_plan, create_plan
from librairy.proposals import upsert_proposal
from librairy.quarantine import quarantine_operation
from librairy.scanner import scan_root
from librairy.search import SearchFilters, rebuild_search_index, search_checksum, search_items


def settings_for(tmp_path: Path) -> Settings:
    return Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        FILE_STABILITY_SECONDS=0,
        CONTENT_SEARCH_ENABLED=True,
        _env_file=None,
    )


def test_migration_backfills_existing_items(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    settings.appdata_dir.mkdir()
    db_path = settings.appdata_dir / "librairy.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        f"""
        BEGIN;
        {MIGRATION_001}
        {MIGRATION_002}
        {MIGRATION_003}
        {MIGRATION_004}
        {MIGRATION_005}
        {MIGRATION_006}
        {MIGRATION_007}
        INSERT INTO items(
          root, relpath, size, mtime_ns, fingerprint, first_seen_at, last_seen_at
        ) VALUES (
          'library', 'Music/Queen/A Night at the Opera/Bohemian.flac', 1, 1,
          'q', 'now', 'now'
        );
        PRAGMA user_version=7;
        COMMIT;
        """
    )
    conn.close()

    migrated = connect(settings)

    rows = search_items(migrated, "queen opera")
    assert len(rows) == 1
    assert rows[0]["root"] == "library"


def test_new_proposal_commit_and_quarantine_sync_fts(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    settings.inbox_dir.mkdir()
    settings.library_dir.mkdir()
    settings.quarantine_dir.mkdir()
    (settings.inbox_dir / "song.flac").write_text("song", encoding="utf-8")
    (settings.inbox_dir / "dupe.txt").write_text("dupe", encoding="utf-8")
    conn = connect(settings)
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    song_id = conn.execute("SELECT id FROM items WHERE relpath='song.flac'").fetchone()[0]
    upsert_proposal(
        conn,
        item_id=song_id,
        category="music",
        clean_name="Bohemian Rhapsody.flac",
        dest_relpath="Music/Queen/A Night at the Opera/Bohemian Rhapsody.flac",
        confidence=0.9,
        evidence=[EvidenceEntry("hashtag", "tag", "queen", 0.7)],
    )
    assert search_items(conn, "bohemian queen")[0]["item_id"] == song_id

    plan_id = create_plan(
        conn,
        [quarantine_operation("dupe.txt", date="2026-07-22")],
        settings,
    )
    approve_plan(conn, plan_id, settings)
    execute_plan(conn, plan_id, settings)

    quarantined = search_items(conn, "dupe")
    assert quarantined[0]["root"] == "quarantine"


def test_index_rebuild_reproduces_search_results(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    settings.inbox_dir.mkdir()
    conn = connect(settings)
    for name in ["alpha.txt", "beta.txt"]:
        (settings.inbox_dir / name).write_text(name, encoding="utf-8")
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    before = search_checksum(conn, "alpha")

    conn.execute("DELETE FROM search_fts")
    indexed = rebuild_search_index(conn)
    after = search_checksum(conn, "alpha")

    assert indexed == 2
    assert before == after


def test_index_rebuild_cli(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "librairy", "--json", "index", "rebuild"],
        env={"APPDATA_DIR": str(tmp_path / "appdata")},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert json.loads(result.stdout) == {"indexed": 0}


def test_content_search_facet_finds_inner_text_only_when_enabled(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    settings.library_dir.mkdir()
    conn = connect(settings)
    path = settings.library_dir / "Documents/doc_0042.txt"
    path.parent.mkdir(parents=True)
    path.write_text("this file talks about coding", encoding="utf-8")
    conn.execute(
        """
        INSERT INTO items(root, relpath, size, mtime_ns, fingerprint, first_seen_at, last_seen_at)
        VALUES ('library', 'Documents/doc_0042.txt', ?, ?, 'fp-content', 'now', 'now')
        """,
        (path.stat().st_size, path.stat().st_mtime_ns),
    )
    rebuild_search_index(conn)
    process_content_extractions(conn, settings)

    assert search_items(conn, "coding", SearchFilters(content=False)) == []
    rows = search_items(conn, "coding", SearchFilters(content=True))

    assert rows[0]["relpath"] == "Documents/doc_0042.txt"
    assert rows[0]["source"] == "content"
    assert "<mark>coding</mark>" in str(rows[0]["snippet"])


def test_content_index_rebuild_reproduces_results(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    settings.library_dir.mkdir()
    conn = connect(settings)
    path = settings.library_dir / "Documents/doc_0042.txt"
    path.parent.mkdir(parents=True)
    path.write_text("coding", encoding="utf-8")
    conn.execute(
        """
        INSERT INTO items(root, relpath, size, mtime_ns, fingerprint, first_seen_at, last_seen_at)
        VALUES ('library', 'Documents/doc_0042.txt', ?, ?, 'fp-content', 'now', 'now')
        """,
        (path.stat().st_size, path.stat().st_mtime_ns),
    )
    before_count = rebuild_content_index(conn, settings)
    before = search_items(conn, "coding", SearchFilters(content=True))

    conn.execute("DELETE FROM content_fts")
    after_count = rebuild_content_index(conn, settings)
    after = search_items(conn, "coding", SearchFilters(content=True))

    assert before_count == after_count == 1
    assert [row["item_id"] for row in before] == [row["item_id"] for row in after]


@pytest.mark.parametrize("query", ['"', "AND OR", "*", "(unbalanced", "queen 🐍"])
def test_hostile_queries_never_500(tmp_path: Path, query: str) -> None:
    settings = settings_for(tmp_path)
    settings.inbox_dir.mkdir()
    conn = connect(settings)
    (settings.inbox_dir / "queen.txt").write_text("q", encoding="utf-8")
    scan_root(conn, "inbox", settings.inbox_dir, settings)

    rows = search_items(conn, query)

    assert isinstance(rows, list)


def test_search_10k_items_perf_under_ci_budget(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    settings.inbox_dir.mkdir()
    conn = connect(settings)
    for index in range(10_000):
        conn.execute(
            """
            INSERT INTO items(
              root, relpath, size, mtime_ns, fingerprint, first_seen_at, last_seen_at
            )
            VALUES ('library', ?, 1, 1, ?, 'now', 'now')
            """,
            (f"Documents/bulk-{index}.txt", f"fp-{index}"),
        )
    rebuild_search_index(conn)

    import time

    started = time.perf_counter()
    rows = search_items(conn, "bulk-9999")
    elapsed = time.perf_counter() - started

    assert rows
    assert elapsed < 0.5
