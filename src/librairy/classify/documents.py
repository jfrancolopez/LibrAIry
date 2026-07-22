from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from librairy.config import Settings
from librairy.models import Category, EvidenceEntry
from librairy.taxonomy import RenderResult, clean_name_from_title, render_destination

DOCUMENT_EXTS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".md", ".rtf"}
BOOK_EXTS = {".epub", ".mobi", ".azw", ".azw3", ".fb2"}
ARCHIVE_EXTS = {".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar"}
RELEASE_JUNK = re.compile(
    r"\b(1080p|720p|2160p|x264|x265|h264|h265|webrip|bluray|dvdrip|proper|repack)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ClassificationResult:
    category: Category
    clean_name: str
    dest_relpath: str | None
    confidence: float
    evidence: tuple[EvidenceEntry, ...]
    fields: dict[str, object]
    reason: str | None = None


def classify_document_like(
    relpath: str,
    *,
    settings: Settings,
) -> ClassificationResult:
    path = PurePosixPath(relpath)
    suffix = path.suffix.lower()
    title = clean_title(path.stem)
    year = _year_from_name(title) or 0
    evidence: list[EvidenceEntry] = []

    if _is_project_path(relpath):
        category: Category = "projects"
        confidence = 0.86
        project = clean_title(path.parts[0]) if path.parts else title
        clean_name = clean_name_from_title(project)
        fields: dict[str, object] = {"project": project, "clean_name": clean_name}
        evidence.append(EvidenceEntry("heuristic", "category", "project markers", 0.86))
    elif suffix in BOOK_EXTS or _booklike_pdf(suffix, title):
        category = "books"
        confidence = 0.78 if suffix == ".pdf" else 0.84
        clean_name = clean_name_from_title(title, suffix)
        fields = {
            "author": "Unknown Author",
            "title": title,
            "genre": "General",
            "clean_name": clean_name,
        }
        evidence.append(
            EvidenceEntry("heuristic", "category", "book-like extension/name", confidence)
        )
    elif suffix in DOCUMENT_EXTS:
        category = "documents"
        confidence = 0.45 if _ambiguous_document(title) else 0.72
        clean_name = clean_name_from_title(title, suffix)
        fields = {"year": year or "Unknown", "topic": title, "clean_name": clean_name}
        evidence.append(EvidenceEntry("heuristic", "category", "document extension", confidence))
    elif suffix in ARCHIVE_EXTS:
        category = "misc"
        confidence = 0.5
        clean_name = clean_name_from_title(title, suffix)
        fields = {"clean_name": clean_name}
        evidence.append(EvidenceEntry("heuristic", "category", "archive extension", confidence))
    else:
        category = "misc"
        confidence = 0.3
        clean_name = clean_name_from_title(title or path.name)
        fields = {"clean_name": clean_name}
        evidence.append(
            EvidenceEntry("heuristic", "category", "unknown extension fallback", confidence)
        )

    rendered = _render_if_confident(category, fields, confidence, settings)
    return ClassificationResult(
        category,
        clean_name,
        rendered.relpath,
        confidence,
        tuple(evidence),
        fields,
        rendered.reason,
    )


def clean_title(value: str) -> str:
    value = RELEASE_JUNK.sub("", value)
    value = re.sub(r"[._-]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value or "Untitled"


def _render_if_confident(
    category: Category,
    fields: dict[str, object],
    confidence: float,
    settings: Settings,
) -> RenderResult:
    if confidence < settings.confidence_threshold:
        return RenderResult(None, "below confidence threshold")
    return render_destination(category, fields, library_root=settings.library_dir)


def _year_from_name(value: str) -> int | None:
    match = re.search(r"\b(19\d{2}|20\d{2})\b", value)
    return int(match.group(1)) if match else None


def _ambiguous_document(title: str) -> bool:
    return bool(re.fullmatch(r"(?i)(scan|img|doc|document)\s*\d*", title.strip()))


def _booklike_pdf(suffix: str, title: str) -> bool:
    return suffix == ".pdf" and bool(re.search(r"(?i)\b(book|novel|edition|chapter|isbn)\b", title))


def _is_project_path(relpath: str) -> bool:
    parts = PurePosixPath(relpath).parts
    markers = {".git", "package.json", "pyproject.toml", "Cargo.toml", "go.mod"}
    return any(part in markers for part in parts)
