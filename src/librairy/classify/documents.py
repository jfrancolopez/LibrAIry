from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath

from librairy.config import Settings
from librairy.models import Category, EvidenceEntry
from librairy.taxonomy import RenderResult, clean_name_from_title, render_destination
from librairy.tools.openlibrary import BookMatch

BookLookup = Callable[[str], "BookMatch | None"]

DOCUMENT_EXTS = {
    ".csv",
    ".doc",
    ".docx",
    ".md",
    ".odp",
    ".ods",
    ".odt",
    ".pdf",
    ".ppt",
    ".pptx",
    ".rtf",
    ".tsv",
    ".txt",
    ".xls",
    ".xlsx",
}
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
    book_lookup: BookLookup | None = None,
    facts=None,  # noqa: ANN001 - librairy.docmeta.DocumentFacts, or None
) -> ClassificationResult:
    """What this file is and where it goes.

    `facts` is what the document said about itself — read by `docmeta` at
    analysis time, and the reason this stopped being a filename parser. The
    ladder is the same one every other medium here uses: what the file
    records outranks what its name suggests, and both outrank a guess.

    A document with no readable identity behaves exactly as it always did, so
    nothing that worked before this depends on poppler being installed.
    """
    path = PurePosixPath(relpath)
    suffix = path.suffix.lower()
    title = clean_title(path.stem)
    year = _year_from_name(title) or 0
    evidence: list[EvidenceEntry] = []
    #  The document's own title beats the filename, always. `scan-0473.pdf`
    #  and `2024 CR-V Owner's Manual` are not two candidate titles to weigh —
    #  one of them is a title and the other is what a scanner called a file.
    if facts is not None and facts.identified:
        title = clean_title(facts.title)
        year = facts.year or year
        evidence.extend(
            EvidenceEntry("document", label.lower(), detail, 0.92)
            for label, detail in facts.sources
        )
    elif facts is not None and facts.scanned:
        #  Said out loud rather than worked around: there is no OCR here, and a
        #  document that looks identified and is not is worse than one that
        #  admits it was only read by its name.
        evidence.append(
            EvidenceEntry("document", "text", "no text layer — this is a scan", 0.9)
        )

    if _is_project_path(relpath):
        category: Category = "projects"
        confidence = 0.86
        project = clean_title(path.parts[0]) if path.parts else title
        clean_name = clean_name_from_title(project)
        fields: dict[str, object] = {"project": project, "clean_name": clean_name}
        evidence.append(EvidenceEntry("heuristic", "category", "project markers", 0.86))
    elif suffix in BOOK_EXTS or _is_book(facts) or _booklike_pdf(suffix, title):
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
        #  A real author out of the file, where there is one. `Books/Unknown
        #  Author/` was not a destination, it was a shrug with a path in it.
        if facts is not None and facts.author:
            fields["author"] = facts.author
            confidence = max(confidence, 0.9)
        if facts is not None and facts.identified:
            fields["title"] = facts.title
            fields["clean_name"] = clean_name_from_title(facts.title, suffix)
            clean_name = str(fields["clean_name"])
            confidence = max(confidence, 0.9)
        if facts is not None and facts.isbn:
            #  An identifier, not a resemblance. Nothing else in this file is
            #  as strong, and it is what makes the openlibrary lookup below
            #  unnecessary rather than merely optional.
            evidence.append(EvidenceEntry("document", "isbn", facts.isbn, 0.95))
            confidence = max(confidence, 0.92)
        if facts is not None and facts.read:
            #  The same type line a document row gets. Without it a book read
            #  out of its own metadata was labelled `Document` on the row while
            #  being filed under `Books/`, which is the page disagreeing with
            #  the decision underneath it.
            evidence.append(EvidenceEntry("document", "type", facts.label, 0.85))
        #  Asked only when the file could not answer for itself. A catalog
        #  that renames a book whose own metadata already named it is a
        #  network round trip spent overwriting better evidence.
        match = (
            book_lookup(title)
            if book_lookup and not (facts is not None and facts.identified)
            else None
        )
        if match is not None:
            fields["title"] = match.title
            if match.author:
                fields["author"] = match.author
            if match.year:
                fields["year"] = match.year
            fields["clean_name"] = clean_name_from_title(match.title, suffix)
            clean_name = str(fields["clean_name"])
            confidence = max(confidence, 0.92)
            detail = f"{match.title}" + (f" — {match.author}" if match.author else "")
            evidence.append(EvidenceEntry("openlibrary", "title", detail, 0.92))
    elif suffix in DOCUMENT_EXTS:
        category = "documents"
        #  A document that named itself is not an ambiguous document, whatever
        #  the scanner called the file. This is the whole point of reading it:
        #  `scan-0473.pdf` used to score 0.45 and sit in Review forever.
        if facts is not None and facts.identified:
            confidence = 0.88
        else:
            confidence = 0.45 if _ambiguous_document(title) else 0.72
        clean_name = clean_name_from_title(title, suffix)
        fields = {"year": year or "Unknown", "topic": title, "clean_name": clean_name}
        evidence.append(EvidenceEntry("heuristic", "category", "document extension", confidence))
        if facts is not None and facts.read:
            #  The broad type, where deterministic evidence supports one. It
            #  does not change the destination — the Documents hierarchy is
            #  what it is — but it is most of what a person needs to recognise
            #  the row.
            fields["document_type"] = facts.kind
            evidence.append(
                EvidenceEntry("document", "type", facts.label, 0.85)
            )
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


def _is_book(facts) -> bool:  # noqa: ANN001
    """A document that carries an ISBN is a book, whatever it is called."""
    from librairy.docmeta import BOOK

    return facts is not None and facts.kind == BOOK


def _booklike_pdf(suffix: str, title: str) -> bool:
    return suffix == ".pdf" and bool(re.search(r"(?i)\b(book|novel|edition|chapter|isbn)\b", title))


def _is_project_path(relpath: str) -> bool:
    parts = PurePosixPath(relpath).parts
    markers = {".git", "package.json", "pyproject.toml", "Cargo.toml", "go.mod"}
    return any(part in markers for part in parts)
