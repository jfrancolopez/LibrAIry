from __future__ import annotations

import zipfile
from pathlib import Path

from librairy.config import Settings
from librairy.content import extract as extract_module
from librairy.content.extract import MAX_ATTEMPTS, process_content_extractions
from librairy.db import SCHEMA_VERSION, connect, user_version


def settings_for(tmp_path: Path, **overrides) -> Settings:
    values = {
        "APPDATA_DIR": tmp_path / "appdata",
        "INBOX_DIR": tmp_path / "inbox",
        "LIBRARY_DIR": tmp_path / "library",
        "QUARANTINE_DIR": tmp_path / "quarantine",
        "CONTENT_SEARCH_ENABLED": True,
        "_env_file": None,
    }
    values.update(overrides)
    settings = Settings(**values)
    for path in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        path.mkdir(parents=True)
    return settings


def add_library_item(conn, settings: Settings, relpath: str, content: str = "body") -> int:
    path = settings.library_dir / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return conn.execute(
        """
        INSERT INTO items(root, relpath, size, mtime_ns, fingerprint, first_seen_at, last_seen_at)
        VALUES ('library', ?, ?, ?, ?, 'now', 'now')
        """,
        (relpath, path.stat().st_size, path.stat().st_mtime_ns, f"fp-{relpath}"),
    ).lastrowid


def test_schema_adds_content_tables(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path))

    assert user_version(conn) == SCHEMA_VERSION
    assert SCHEMA_VERSION >= 9
    conn.execute("SELECT * FROM content_extractions")
    conn.execute("SELECT * FROM content_fts")


def test_disabled_setting_does_no_extraction(tmp_path: Path) -> None:
    settings = settings_for(tmp_path, CONTENT_SEARCH_ENABLED=False)
    conn = connect(settings)
    add_library_item(conn, settings, "Documents/note.txt", "coding words")

    summary = process_content_extractions(conn, settings)

    assert summary.extracted == 0
    assert conn.execute("SELECT COUNT(*) FROM content_extractions").fetchone()[0] == 0


def test_extracts_text_and_skips_unchanged_fingerprint(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    item_id = add_library_item(conn, settings, "Documents/note.txt", "coding words")

    first = process_content_extractions(conn, settings)
    second = process_content_extractions(conn, settings)

    row = conn.execute("SELECT text FROM content_fts WHERE rowid=?", (item_id,)).fetchone()
    assert first.extracted == 1
    assert second.extracted == 0
    assert "coding words" in row["text"]


def test_changed_fingerprint_reextracts(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    item_id = add_library_item(conn, settings, "Documents/note.txt", "old words")
    process_content_extractions(conn, settings)
    (settings.library_dir / "Documents/note.txt").write_text("new words", encoding="utf-8")
    conn.execute("UPDATE items SET fingerprint='changed' WHERE id=?", (item_id,))

    summary = process_content_extractions(conn, settings)

    row = conn.execute("SELECT text FROM content_fts WHERE rowid=?", (item_id,)).fetchone()
    assert summary.extracted == 1
    assert "new words" in row["text"]


def test_cap_and_truncation_flag(tmp_path: Path) -> None:
    settings = settings_for(tmp_path, CONTENT_EXTRACT_MAX_CHARS=1024)
    conn = connect(settings)
    item_id = add_library_item(conn, settings, "Documents/big.txt", "x" * 2000)

    process_content_extractions(conn, settings)

    row = conn.execute(
        "SELECT chars, truncated FROM content_extractions WHERE item_id=?",
        (item_id,),
    ).fetchone()
    assert row["chars"] == 1024
    assert row["truncated"] == 1


def test_docx_and_epub_extractors(tmp_path: Path) -> None:
    docx = tmp_path / "sample.docx"
    with zipfile.ZipFile(docx, "w") as archive:
        archive.writestr(
            "word/document.xml",
            "<w:document xmlns:w='w'><w:body><w:t>docx coding</w:t></w:body></w:document>",
        )
    epub = tmp_path / "sample.epub"
    with zipfile.ZipFile(epub, "w") as archive:
        archive.writestr("chapter.xhtml", "<html><body><p>epub coding</p></body></html>")

    assert "docx coding" in extract_module.extract_docx(docx)
    assert "epub coding" in extract_module.extract_epub(epub)


def test_failures_stop_retrying_after_three_attempts(tmp_path: Path, monkeypatch) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    add_library_item(conn, settings, "Documents/bad.pdf", "%PDF-bad")
    calls = 0

    def fail(path: Path) -> str:  # noqa: ARG001
        nonlocal calls
        calls += 1
        raise RuntimeError("broken pdf")

    monkeypatch.setattr(extract_module, "extract_pdf", fail)

    for _ in range(MAX_ATTEMPTS + 2):
        process_content_extractions(conn, settings)

    row = conn.execute("SELECT attempts, error FROM content_extractions").fetchone()
    assert calls == MAX_ATTEMPTS
    assert row["attempts"] == MAX_ATTEMPTS
    assert "broken pdf" in row["error"]


def test_only_library_scoped_categories_are_extracted(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    add_library_item(conn, settings, "Music/song.txt", "lyrics")
    add_library_item(conn, settings, "Documents/note.txt", "coding")

    summary = process_content_extractions(conn, settings)

    assert summary.extracted == 1
    assert conn.execute("SELECT COUNT(*) FROM content_fts").fetchone()[0] == 1


def test_extraction_does_not_touch_mtime(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    add_library_item(conn, settings, "Documents/note.txt", "coding")
    path = settings.library_dir / "Documents/note.txt"
    before = path.stat().st_mtime_ns

    process_content_extractions(conn, settings)

    assert path.stat().st_mtime_ns == before
