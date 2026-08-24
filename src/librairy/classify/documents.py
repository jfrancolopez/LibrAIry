from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath

from librairy.config import Settings
from librairy.models import Category, EvidenceEntry
from librairy.taxonomy import (
    RenderResult,
    clean_name_from_title,
    document_name,
    document_template,
    render_destination,
    render_template,
)
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
        #  Not `clean_title`: that repairs a *filename stem*, turning dots,
        #  dashes and underscores into spaces so `my_report-v2` reads. A title
        #  the document itself carries has already been written by a person,
        #  and running it through the same pass cost `CR-V` its hyphen.
        title = " ".join(facts.title.split())
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
        clean_name = document_name(title, suffix)
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
            fields["clean_name"] = document_name(facts.title, suffix)
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
            fields["clean_name"] = document_name(match.title, suffix)
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
        clean_name = document_name(title, suffix)
        fields = {"year": year or "Unknown", "topic": title, "clean_name": clean_name}
        evidence.append(EvidenceEntry("heuristic", "category", "document extension", confidence))
        if facts is not None and facts.read:
            fields["document_type"] = facts.kind
            evidence.append(EvidenceEntry("document", "type", facts.label, 0.85))
        branch = _document_branch(facts, fields, evidence)
        if branch:
            #  A deliberate branch of the Documents hierarchy, chosen from what
            #  the document established about itself. Rendered through the same
            #  sanitizer and the same `validate_dest` as every other
            #  destination — see `taxonomy.render_template`.
            rendered = render_template(
                branch, category, fields, library_root=settings.library_dir
            )
            if rendered.relpath and confidence >= settings.confidence_threshold:
                return ClassificationResult(
                    category, clean_name, rendered.relpath, confidence,
                    tuple(evidence), fields, None,
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


#  Names a PDF producer writes into the Author field when nobody filled one
#  in. Treating `Acrobat Distiller` as a manufacturer would create a folder
#  named after the software that made the file.
_NOT_AN_ORGANIZATION = re.compile(
    r"(?i)^(acrobat|adobe|microsoft|word|writer|libreoffice|openoffice|pdf|"
    r"scanner|canon|epson scan|hp scan|unknown|user|admin|owner|none|n/?a)\b"
)


def _document_branch(facts, fields: dict[str, object], evidence: list) -> str:  # noqa: ANN001
    """Which branch of the Documents hierarchy this document has earned.

    Only the broad types the classifier can actually support, and only from
    deterministic identity. A document with no type and no title keeps the
    generic dated branch it has always had.
    """
    from librairy.docmeta import FINANCIAL, MANUAL, PAPER

    if facts is None or not facts.read or not facts.identified:
        return ""
    if facts.kind == MANUAL:
        organization = _organization(facts)
        if organization:
            fields["organization"] = organization
            evidence.append(EvidenceEntry("document", "organization", organization, 0.85))
    elif facts.kind == PAPER:
        author = _primary_author(facts)
        if author:
            fields["author"] = author
            evidence.append(EvidenceEntry("document", "author", author, 0.85))
        #  The document's own year, never the year it was imported. "I filed
        #  this in 2026" is a fact about the import and says nothing about the
        #  paper — and `Unknown`, which the generic branch uses as a literal
        #  placeholder, is not a year either.
        if facts.year:
            fields["year"] = facts.year
        else:
            fields.pop("year", None)
    elif facts.kind == FINANCIAL:
        if facts.year:
            fields["year"] = facts.year
        else:
            #  No trustworthy date, so no dated folder. `Financial/2026/` for a
            #  statement whose date nobody read would be a filing cabinet
            #  drawer labelled with the wrong year.
            fields.pop("year", None)
    return document_template(facts.kind, fields)


def _organization(facts) -> str:  # noqa: ANN001
    """The manufacturer, when the Author field is plausibly one."""
    value = " ".join(str(getattr(facts, "author", "") or "").split())
    if len(value) < 2 or _NOT_AN_ORGANIZATION.match(value):
        return ""
    return value


def _primary_author(facts) -> str:  # noqa: ANN001
    """The first author named, for a paper with several."""
    value = _organization(facts)
    if not value:
        return ""
    #  Semicolons and `and` separate *authors*; a comma usually separates a
    #  surname from initials, so splitting on it would file `Einstein, A.`
    #  under `Einstein` and lose the half that tells two Einsteins apart.
    for separator in (";", " and ", " & "):
        if separator in value:
            return value.split(separator, 1)[0].strip()
    return value


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
