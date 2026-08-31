"""One metadata cache, three tools, and a fingerprint gate on every read.

`item_metadata` was keyed by `item_id` alone, because for a long time one
reader wrote it. A test failed deliberately if a second writer appeared — with
that key a document reader and a media reader would have taken turns
overwriting each other, and a row saying `tool='exiftool-image'` where a caller
expected ffprobe is worse than no cache at all.

There are three real readers now, describing different things about one file:
technical media facts, document identity, and image metadata. Each owns a row.
These tests are about the two properties that makes safe — tools cannot clobber
one another, and a cached answer about bytes the file no longer has is a miss —
and about the one property that makes it useful: measuring happens during
analysis, never while a page is being drawn.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from librairy.config import Settings
from librairy.db import SCHEMA_VERSION, connect, migrate
from librairy.planner import utc_now
from librairy.scanner import scan_root
from librairy.tools.common import (
    DOCUMENT_TOOL,
    IMAGE_TOOL,
    MEDIA_TOOL,
    get_cached_metadata,
    set_cached_metadata,
)
from tests.support.documents import build_pdf, write_epub

poppler = pytest.mark.skipif(
    shutil.which("pdfinfo") is None, reason="poppler-utils is not installed"
)


def settings_for(tmp_path: Path) -> Settings:
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        FILE_STABILITY_SECONDS=0,
        AUTH_REQUIRED=False,
        _env_file=None,
    )
    for root in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        root.mkdir(parents=True, exist_ok=True)
    return settings


def an_item(conn, settings: Settings, relpath: str, body: bytes) -> tuple[int, str]:
    path = settings.library_dir / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    scan_root(conn, "library", settings.library_dir, settings)
    row = conn.execute(
        "SELECT id, fingerprint FROM items WHERE root='library' AND relpath=?", (relpath,)
    ).fetchone()
    return int(row["id"]), str(row["fingerprint"])


# --- 31: the migration ---------------------------------------------------------


def test_the_migration_keeps_every_ffprobe_row(tmp_path: Path) -> None:
    """Technical metadata is not invalidated because the schema changed.

    Built at the old shape by hand — `item_id INTEGER PRIMARY KEY`, which is
    what a database written before migration 037 really holds — and then
    migrated, because a test that starts from the new shape proves nothing
    about the upgrade.
    """
    settings = settings_for(tmp_path)
    settings.appdata_dir.mkdir(parents=True, exist_ok=True)
    database = settings.appdata_dir / "librairy.db"
    old = sqlite3.connect(database)
    old.row_factory = sqlite3.Row
    old.executescript(
        """
        PRAGMA user_version=36;
        CREATE TABLE items (id INTEGER PRIMARY KEY, root TEXT, relpath TEXT,
          size INTEGER, mtime_ns INTEGER, fingerprint TEXT, state TEXT,
          first_seen_at TEXT, last_seen_at TEXT, missing_since TEXT);
        INSERT INTO items(id, root, relpath, size, mtime_ns, fingerprint, state,
          first_seen_at, last_seen_at)
          VALUES (1, 'library', 'a.mkv', 10, 0, 'the-bytes', 'committed', 'x', 'x');
        -- None of the tables below are what this test is about, and a real
        -- database from before 037 has every one of them. Later migrations
        -- alter them and — since 046 — rebuild the search index from them, so
        -- a fixture that omitted them was asserting that the upgrade works on
        -- a database nobody has.
        CREATE TABLE plans (id TEXT PRIMARY KEY, status TEXT NOT NULL,
          plan_hash TEXT, created_at TEXT NOT NULL, approved_at TEXT,
          finished_at TEXT);
        -- Same reasoning: `settings` has existed since migration 002, and
        -- migration 044 reads the music format preference out of it on its way
        -- into the central Format Policy.
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        -- Migration 045 indexes both of these; a database from before 037 has
        -- had them since the first release.
        CREATE TABLE plan_ops (id INTEGER PRIMARY KEY, plan_id TEXT NOT NULL,
          seq INTEGER NOT NULL, op_type TEXT NOT NULL, item_id INTEGER,
          src_root TEXT NOT NULL, src_relpath TEXT NOT NULL,
          src_fingerprint TEXT NOT NULL, dest_root TEXT NOT NULL,
          dest_relpath TEXT NOT NULL, result TEXT, final_relpath TEXT,
          executed_at TEXT);
        -- Migration 046 recreates the search index, and rebuilding it reads
        -- these. A database from before 037 has all of them.
        CREATE TABLE proposals (id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL,
          category TEXT NOT NULL, clean_name TEXT NOT NULL, dest_relpath TEXT,
          confidence REAL NOT NULL, group_id INTEGER, status TEXT NOT NULL
          DEFAULT 'proposed', evidence TEXT NOT NULL, created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL);
        CREATE TABLE groups (id INTEGER PRIMARY KEY, kind TEXT NOT NULL,
          key TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE worker_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE track_identity (item_id INTEGER PRIMARY KEY,
          fingerprint TEXT NOT NULL DEFAULT '', provider TEXT NOT NULL,
          recording_id TEXT NOT NULL DEFAULT '', artist TEXT NOT NULL DEFAULT '',
          artist_id TEXT NOT NULL DEFAULT '', title TEXT NOT NULL DEFAULT '',
          score REAL, releases TEXT NOT NULL DEFAULT '[]',
          looked_up_at TEXT NOT NULL);
        CREATE TABLE catalog_identity (id INTEGER PRIMARY KEY,
          scope_kind TEXT NOT NULL, scope_key TEXT NOT NULL,
          provider TEXT NOT NULL, entity TEXT NOT NULL,
          catalog_id TEXT NOT NULL DEFAULT '',
          canonical_title TEXT NOT NULL DEFAULT '',
          canonical_artist TEXT NOT NULL DEFAULT '',
          artist_id TEXT NOT NULL DEFAULT '', looked_up_at TEXT NOT NULL);
        CREATE TABLE vision_results (item_id INTEGER PRIMARY KEY,
          fingerprint TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
          category TEXT, caption TEXT, subjects TEXT NOT NULL DEFAULT '[]',
          tags TEXT NOT NULL DEFAULT '[]', name_tokens TEXT NOT NULL DEFAULT '[]',
          visible_text TEXT, looked_at TEXT);
        CREATE TABLE history (id INTEGER PRIMARY KEY, ts TEXT NOT NULL,
          plan_id TEXT, op_id INTEGER, action TEXT NOT NULL,
          src_root TEXT NOT NULL, src_relpath TEXT NOT NULL,
          dest_root TEXT NOT NULL, dest_relpath TEXT NOT NULL,
          fingerprint TEXT, outcome TEXT NOT NULL);
        CREATE TABLE item_metadata (
          item_id INTEGER PRIMARY KEY REFERENCES items(id),
          fingerprint TEXT NOT NULL, tool TEXT NOT NULL,
          payload TEXT NOT NULL, updated_at TEXT NOT NULL);
        -- Migration 047 adds columns to this, so it has to be here for the
        -- replay to reach 037 at all.
        CREATE TABLE plan_withdrawals (
          id INTEGER PRIMARY KEY, plan_id TEXT NOT NULL, plan_hash TEXT,
          audit_finding_id INTEGER, relpath TEXT NOT NULL, dest_relpath TEXT,
          op_count INTEGER NOT NULL DEFAULT 0, approved_at TEXT,
          withdrawn_at TEXT NOT NULL);
        INSERT INTO item_metadata(item_id, fingerprint, tool, payload, updated_at)
          VALUES (1, 'the-bytes', 'ffprobe-media', '{"duration": 5820}', 'then');
        """
    )
    old.commit()

    migrate(old)

    assert old.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert get_cached_metadata(old, 1, "the-bytes", MEDIA_TOOL) == {"duration": 5820}
    row = old.execute("SELECT * FROM item_metadata WHERE item_id=1").fetchone()
    assert row["tool"] == MEDIA_TOOL
    assert row["updated_at"] == "then"


# --- 32-36: coexistence and the fingerprint gate -------------------------------


def test_three_tools_describe_one_file_without_touching_each_other(
    tmp_path: Path,
) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    item_id, fingerprint = an_item(conn, settings, "a.pdf", b"%PDF-1.4 x")

    set_cached_metadata(conn, item_id, fingerprint, MEDIA_TOOL, {"duration": 1}, utc_now())
    set_cached_metadata(conn, item_id, fingerprint, DOCUMENT_TOOL, {"pages": 643}, utc_now())
    set_cached_metadata(conn, item_id, fingerprint, IMAGE_TOOL, {"width": 4032}, utc_now())

    assert get_cached_metadata(conn, item_id, fingerprint, MEDIA_TOOL) == {"duration": 1}
    assert get_cached_metadata(conn, item_id, fingerprint, DOCUMENT_TOOL) == {"pages": 643}
    assert get_cached_metadata(conn, item_id, fingerprint, IMAGE_TOOL) == {"width": 4032}


def test_a_cached_answer_about_older_bytes_is_never_shown(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    item_id, fingerprint = an_item(conn, settings, "a.pdf", b"%PDF-1.4 x")
    set_cached_metadata(conn, item_id, "an-older-rip", DOCUMENT_TOOL, {"pages": 12}, utc_now())

    assert get_cached_metadata(conn, item_id, fingerprint, DOCUMENT_TOOL) is None


# --- 37-40: documents ----------------------------------------------------------


@poppler
def test_a_document_is_measured_once_and_read_from_the_cache_after(
    tmp_path: Path, monkeypatch
) -> None:
    from librairy import docmeta

    settings = settings_for(tmp_path)
    conn = connect(settings)
    body = build_pdf(title="A Manual", author="Acme", lines=("A Manual",), pages=7)
    item_id, _ = an_item(conn, settings, "a.pdf", body)
    runs: list[str] = []
    real = docmeta.facts_for

    def counted(path, settings, **kwargs):  # noqa: ANN001, ANN003
        runs.append(str(path))
        return real(path, settings, **kwargs)

    monkeypatch.setattr(docmeta, "facts_for", counted)

    first = docmeta.facts_for_item(conn, settings, item_id, settings.library_dir / "a.pdf")
    second = docmeta.facts_for_item(conn, settings, item_id, settings.library_dir / "a.pdf")

    assert first.title == "A Manual"
    assert second.title == "A Manual"
    assert second.pages == 7
    assert len(runs) == 1, "the second read came from the cache"


def test_epub_identity_caches_too(tmp_path: Path) -> None:
    from librairy import docmeta

    settings = settings_for(tmp_path)
    conn = connect(settings)
    path = settings.library_dir / "b.epub"
    write_epub(path, title="Dune", author="Frank Herbert", identifier="urn:isbn:9780441013593")
    item_id, fingerprint = an_item(conn, settings, "b.epub", path.read_bytes())

    docmeta.facts_for_item(conn, settings, item_id, path)

    cached = get_cached_metadata(conn, item_id, fingerprint, DOCUMENT_TOOL)
    assert cached is not None
    assert cached["isbn"] == "9780441013593"


@poppler
def test_a_page_render_never_measures_a_document(tmp_path: Path, monkeypatch) -> None:
    """Analysis owns measurement. A GET reads what is cached, or says nothing."""
    from librairy import docmeta

    def forbidden(*_args, **_kwargs):
        raise AssertionError("a GET must not run pdfinfo")

    settings = settings_for(tmp_path)
    conn = connect(settings)
    item_id, _ = an_item(conn, settings, "a.pdf", build_pdf(title="A Manual", pages=2))
    monkeypatch.setattr(docmeta, "facts_for", forbidden)

    found = docmeta.cached_facts(conn, item_id, settings.library_dir / "a.pdf")

    assert found is None


@poppler
def test_a_re_ripped_document_is_measured_again(tmp_path: Path) -> None:
    from librairy import docmeta

    settings = settings_for(tmp_path)
    conn = connect(settings)
    path = settings.library_dir / "a.pdf"
    item_id, _ = an_item(conn, settings, "a.pdf", build_pdf(title="First", pages=1))
    docmeta.facts_for_item(conn, settings, item_id, path)

    path.write_bytes(build_pdf(title="Second", pages=1))
    scan_root(conn, "library", settings.library_dir, settings)

    assert docmeta.facts_for_item(conn, settings, item_id, path).title == "Second"
    rows = conn.execute(
        "SELECT COUNT(*) c FROM item_metadata WHERE item_id=? AND tool=?",
        (item_id, DOCUMENT_TOOL),
    ).fetchone()
    assert rows["c"] == 1, "one row per item per tool, holding the newest bytes"
