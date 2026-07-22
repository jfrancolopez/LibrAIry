from __future__ import annotations

import re
import sqlite3
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree

from librairy.config import Settings
from librairy.paths import validate_relpath
from librairy.planner import utc_now

SCOPED_CATEGORIES = {"documents", "books", "projects"}
TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".rst", ".py", ".toml", ".json", ".yaml", ".yml"}
MAX_ATTEMPTS = 3


@dataclass(frozen=True)
class ExtractionResult:
    item_id: int
    outcome: str
    extractor: str | None = None
    chars: int = 0
    truncated: bool = False
    error: str | None = None


@dataclass(frozen=True)
class ExtractionSummary:
    extracted: int = 0
    skipped: int = 0
    failed: int = 0


def process_content_extractions(
    conn: sqlite3.Connection,
    settings: Settings,
    limit: int | None = None,
) -> ExtractionSummary:
    if not settings.content_search_enabled:
        return ExtractionSummary()
    extracted = skipped = failed = 0
    for row in _candidate_rows(conn, limit):
        result = extract_item(conn, settings, row)
        if result.outcome == "extracted":
            extracted += 1
        elif result.outcome == "failed":
            failed += 1
        else:
            skipped += 1
    return ExtractionSummary(extracted, skipped, failed)


def rebuild_content_index(conn: sqlite3.Connection, settings: Settings) -> int:
    conn.execute("DELETE FROM content_fts")
    conn.execute("DELETE FROM content_extractions")
    enabled_settings = settings.model_copy(update={"content_search_enabled": True})
    return process_content_extractions(conn, enabled_settings).extracted


def extract_item(
    conn: sqlite3.Connection,
    settings: Settings,
    row: sqlite3.Row,
) -> ExtractionResult:
    existing = conn.execute(
        "SELECT * FROM content_extractions WHERE item_id=?",
        (row["id"],),
    ).fetchone()
    if existing and existing["fingerprint"] == row["fingerprint"] and existing["error"] is None:
        return ExtractionResult(row["id"], "skipped")
    if (
        existing
        and existing["fingerprint"] == row["fingerprint"]
        and existing["attempts"] >= MAX_ATTEMPTS
    ):
        return ExtractionResult(row["id"], "skipped", error=existing["error"])

    path = validate_relpath(settings.library_dir, row["relpath"], kind="source")
    extractor = extractor_name(path)
    if extractor is None:
        return ExtractionResult(row["id"], "skipped")

    try:
        text = extract_text(path, extractor)
        text, truncated = cap_text(text, settings.content_extract_max_chars)
    except Exception as exc:
        attempts = (
            1
            if existing is None or existing["fingerprint"] != row["fingerprint"]
            else existing["attempts"] + 1
        )
        _record_failure(conn, row, extractor, attempts, str(exc))
        return ExtractionResult(row["id"], "failed", extractor=extractor, error=str(exc))

    conn.execute("DELETE FROM content_fts WHERE rowid=?", (row["id"],))
    conn.execute(
        "INSERT INTO content_fts(rowid, text, item_id) VALUES (?, ?, ?)",
        (row["id"], text, row["id"]),
    )
    conn.execute(
        """
        INSERT OR REPLACE INTO content_extractions(
          item_id, fingerprint, extractor, chars, truncated, attempts, extracted_at, error
        ) VALUES (?, ?, ?, ?, ?, 0, ?, NULL)
        """,
        (row["id"], row["fingerprint"], extractor, len(text), int(truncated), utc_now()),
    )
    return ExtractionResult(row["id"], "extracted", extractor, len(text), truncated)


def extract_text(path: Path, extractor: str) -> str:
    if extractor == "text":
        return path.read_text(encoding="utf-8", errors="replace")
    if extractor == "docx":
        return extract_docx(path)
    if extractor == "epub":
        return extract_epub(path)
    if extractor == "pdf":
        return extract_pdf(path)
    raise ValueError(f"unsupported extractor: {extractor}")


def extractor_name(path: Path) -> str | None:
    suffix = path.suffix.lower()
    if suffix in TEXT_SUFFIXES:
        return "text"
    if suffix == ".docx":
        return "docx"
    if suffix == ".epub":
        return "epub"
    if suffix == ".pdf":
        return "pdf"
    return None


def extract_docx(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        xml = archive.read("word/document.xml")
    root = ElementTree.fromstring(xml)
    return " ".join(text for text in root.itertext() if text.strip())


def extract_epub(path: Path) -> str:
    texts: list[str] = []
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            suffix = PurePosixPath(name).suffix.lower()
            if suffix not in {".html", ".xhtml", ".htm"}:
                continue
            raw = archive.read(name).decode("utf-8", errors="replace")
            texts.append(strip_tags(raw))
    return "\n".join(texts)


def extract_pdf(path: Path) -> str:
    with tempfile.NamedTemporaryFile(suffix=".txt") as output:
        result = subprocess.run(
            ["pdftotext", "-layout", "-q", str(path), output.name],
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "pdftotext failed")
        return Path(output.name).read_text(encoding="utf-8", errors="replace")


def strip_tags(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", value)).strip()


def cap_text(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


def _candidate_rows(conn: sqlite3.Connection, limit: int | None) -> list[sqlite3.Row]:
    sql = """
        SELECT i.id, i.relpath, i.fingerprint, COALESCE(p.category, '') AS category
        FROM items i
        LEFT JOIN proposals p ON p.item_id=i.id AND p.status != 'superseded'
        LEFT JOIN content_extractions c ON c.item_id=i.id
        WHERE i.root='library'
          AND i.missing_since IS NULL
          AND i.fingerprint IS NOT NULL
          AND (
            c.item_id IS NULL
            OR c.fingerprint != i.fingerprint
            OR (c.error IS NOT NULL AND c.attempts < ?)
          )
        ORDER BY i.id
    """
    params: list[object] = [MAX_ATTEMPTS]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return [row for row in conn.execute(sql, params) if _in_scope(row)]


def _in_scope(row: sqlite3.Row) -> bool:
    category = str(row["category"] or "")
    if category in SCOPED_CATEGORIES:
        return True
    top = PurePosixPath(row["relpath"]).parts[:1]
    return bool(top and top[0].lower() in {"documents", "books", "projects"})


def _record_failure(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    extractor: str,
    attempts: int,
    error: str,
) -> None:
    conn.execute("DELETE FROM content_fts WHERE rowid=?", (row["id"],))
    conn.execute(
        """
        INSERT OR REPLACE INTO content_extractions(
          item_id, fingerprint, extractor, chars, truncated, attempts, extracted_at, error
        ) VALUES (?, ?, ?, 0, 0, ?, ?, ?)
        """,
        (row["id"], row["fingerprint"], extractor, attempts, utc_now(), error[:500]),
    )
