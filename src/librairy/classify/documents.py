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
    #  "This needs a person, and I have something to show them." Set when the
    #  sources that named a document disagree. It is what keeps a contested
    #  document out of the held list: `waiting` exists for files nothing could
    #  say anything about, and a disagreement is the opposite of one.
    ask: bool = False


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
    #  Everything that named this document, compared rather than ranked. The
    #  rule used to be "the document's own title beats the filename, always",
    #  which is right until the document's own title is `CRACKING` and its
    #  title page says `Programming Rust`. See `librairy/document_identity.py`.
    identity = document_identity(relpath, facts)
    if identity.title:
        #  Not `clean_title`: that repairs a *filename stem*, turning dots,
        #  dashes and underscores into spaces so `my_report-v2` reads. A title
        #  a document carries has already been written by a person, and running
        #  it through the same pass cost `CR-V` its hyphen.
        title = " ".join(identity.title.split())
        year = (facts.year if facts is not None else 0) or year
    if facts is not None and facts.scanned and not facts.ocr_read:
        #  Said out loud rather than worked around: nothing read the pixels,
        #  and a document that looks identified and is not is worse than one
        #  that admits it was only read by its name.
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
        if identity.title:
            fields["title"] = title
            fields["clean_name"] = document_name(title, suffix)
            clean_name = str(fields["clean_name"])
            if _named_itself(identity):
                confidence = max(confidence, 0.9)
        if facts is not None and facts.isbn:
            #  An identifier makes the openlibrary lookup below unnecessary
            #  rather than merely optional. The evidence line for it is written
            #  once, at the exit — see `_identity_evidence`.
            confidence = max(confidence, 0.92)
        #  Asked when the file could not answer for itself — a catalog that
        #  renames a book whose own metadata already named it is a network
        #  round trip spent overwriting better evidence — **or** when the
        #  sources that did answer disagree, which is exactly the case where a
        #  third opinion is worth a round trip.
        match = (
            book_lookup(title)
            if book_lookup and (not _named_itself(identity) or identity.contested)
            else None
        )
        if match is not None:
            #  Compared, not accepted. A catalog is the strongest source here
            #  and it is still a source: if it agrees with what the file said,
            #  that agreement is what earns the preselection, and if it
            #  disagrees with everything the document is contested and asks.
            identity = document_identity(relpath, facts, catalog_title=match.title)
            title = " ".join(identity.title.split()) if identity.title else title
            fields["title"] = title
            if match.author:
                fields["author"] = match.author
            if match.year:
                fields["year"] = match.year
            fields["clean_name"] = document_name(title, suffix)
            clean_name = str(fields["clean_name"])
            confidence = max(confidence, 0.92)
            detail = f"{match.title}" + (f" — {match.author}" if match.author else "")
            evidence.append(EvidenceEntry("openlibrary", "title", detail, 0.92))
    elif suffix in DOCUMENT_EXTS:
        category = "documents"
        #  A document that named itself is not an ambiguous document, whatever
        #  the scanner called the file. This is the whole point of reading it:
        #  `scan-0473.pdf` used to score 0.45 and sit in Review forever.
        if _named_itself(identity):
            confidence = 0.88
        else:
            confidence = 0.45 if _ambiguous_document(title) else 0.72
        clean_name = document_name(title, suffix)
        fields = {"year": year or "Unknown", "topic": title, "clean_name": clean_name}
        evidence.append(EvidenceEntry("heuristic", "category", "document extension", confidence))
        if facts is not None and facts.read:
            fields["document_type"] = facts.kind
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
                #  The same comparison and the same cap as the common exit
                #  below. A branch that returns early is a branch a reader has
                #  to check separately, so it says so rather than being trusted.
                evidence.extend(_identity_evidence(identity, facts))
                if identity.contested:
                    confidence = min(confidence, CONTESTED_CONFIDENCE)
                    if confidence < settings.confidence_threshold:
                        return ClassificationResult(
                            category, clean_name, None, confidence,
                            tuple(evidence), fields,
                            "below confidence threshold", ask=True,
                        )
                return ClassificationResult(
                    category, clean_name, rendered.relpath, confidence,
                    tuple(evidence), fields, None, ask=identity.contested,
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

    #  Every source that named this document, on the row, in one place — and
    #  the one exit that both document branches pass through, so a comparison
    #  cannot be shown on one and not the other.
    evidence.extend(_identity_evidence(identity, facts))
    if identity.contested:
        #  Contested, not weak. There is an answer and a reason for it, so the
        #  destination is still rendered; it is simply not an answer worth
        #  taking on trust, so it stays under `review.CONFIDENT` and out of the
        #  settled tier. See `librairy/confidence_tiers.py`.
        confidence = min(confidence, CONTESTED_CONFIDENCE)
    rendered = _render_if_confident(category, fields, confidence, settings)
    return ClassificationResult(
        category,
        clean_name,
        rendered.relpath,
        confidence,
        tuple(evidence),
        fields,
        rendered.reason,
        #  A disagreement is a question with something to show. It must never
        #  be held for want of an AI provider — `waiting` is for files nothing
        #  could say anything about, and this is the opposite of one.
        ask=identity.contested,
    )


#  What a contested document scores. Above the threshold, so a destination is
#  rendered and the row shows a real proposal rather than a shrug — and below
#  `review.CONFIDENT`, so it is never swept up by "approve all confident" and
#  never reaches the settled tier. The disagreement is the reason a person is
#  asked; it is not a reason to have no answer ready for them.
CONTESTED_CONFIDENCE = 0.82


def document_identity(relpath: str, facts, catalog_title: str = ""):  # noqa: ANN001, ANN201
    """Everything that named this document, compared.

    The filename is a candidate like any other and is *counted* unless it is a
    scanner's serial number; an embedded title is counted unless it is a
    producing application's default. Both exclusions are narrow and both leave
    the source visible on the row — "LibrAIry ignored the filename" is
    information, and silently dropping a source is how a comparison becomes
    the opaque score it was built to replace.
    """
    from librairy.document_identity import (
        CATALOG,
        CONTENT,
        EMBEDDED,
        FILENAME,
        OCR,
        Candidate,
        meaningless_filename,
        producer_default,
        resolve,
    )

    stem = PurePosixPath(relpath).stem
    candidates = []
    named = clean_title(stem)
    if named and named != "Untitled":
        ignorable = meaningless_filename(stem)
        candidates.append(
            Candidate(
                FILENAME,
                named,
                counted=not ignorable,
                note="a scanner's counter, not a name" if ignorable else "",
            )
        )
    if facts is not None:
        if facts.embedded_title:
            default = producer_default(facts.embedded_title)
            candidates.append(
                Candidate(
                    EMBEDDED,
                    facts.embedded_title,
                    counted=not default,
                    note="the producing application's default" if default else "",
                )
            )
        if facts.content_title:
            candidates.append(Candidate(CONTENT, facts.content_title))
        if facts.ocr_title:
            candidates.append(Candidate(OCR, facts.ocr_title))
    if catalog_title:
        candidates.append(Candidate(CATALOG, catalog_title))
    return resolve(candidates)


def _identity_evidence(identity, facts=None) -> list[EvidenceEntry]:  # noqa: ANN001
    """The comparison, as evidence the row can print and History can keep.

    One entry per source that said something, plus — when they disagree — one
    that says so and names the recommendation. That last entry is what
    `confidence_tiers` reads to keep a contested document out of the settled
    tier, so it is written whether or not any page is looking.

    The facts that are not titles — author, ISBN, DOI, what kind of document it
    is — come out here too rather than from whichever branch happened to run.
    A document row that shows an ISBN for a book and not for a paper is a row
    whose contents depend on a code path, which is not a thing a reader can
    know.
    """
    from librairy.document_identity import FILENAME, SOURCE_LABEL

    shown = identity.shown()
    #  Nothing to compare is nothing to show. A file whose only candidate is
    #  its own filename has no comparison — printing "Filename: dune opaque
    #  name" on the row tells a reader what the row already says — so the
    #  document block stays empty and the row is exactly what it was before
    #  any of this existed.
    if len(shown) < 2 and all(entry["source"] == FILENAME for entry in shown):  # noqa: PLR2004
        shown = ()

    found = [
        EvidenceEntry(
            "document",
            f"title/{named['source']}",
            named["title"],
            0.9 if named["chosen"] else 0.6,
            #  `note` and not `status`: `status` is the catalog vocabulary —
            #  matched, no-match, not-checked — and this is not a catalog
            #  source. One word, so the row can mark the line and History can
            #  keep it without a second table.
            note="disagrees" if named["conflict"] else ("chosen" if named["chosen"] else ""),
        )
        for named in shown
    ]
    if identity.contested:
        disagreeing = ", ".join(
            SOURCE_LABEL.get(name, name) for name in identity.conflicts
        )
        found.append(
            EvidenceEntry(
                "document",
                "conflict",
                f"{disagreeing} disagrees. {identity.why}",
                0.9,
            )
        )
    if facts is None:
        return found
    for field, detail, weight in (
        ("author", facts.author, 0.9),
        #  An identifier, not a resemblance. Nothing else a document carries is
        #  as strong, which is why it is also what lifts a book's confidence.
        ("isbn", facts.isbn, 0.95),
        ("doi", facts.doi, 0.95),
    ):
        if detail:
            found.append(EvidenceEntry("document", field, detail, weight))
    if facts.read:
        #  The same type line the row prints. Without it a book read out of its
        #  own metadata was labelled `Document` on the row while being filed
        #  under `Books/` — the page disagreeing with the decision under it.
        found.append(EvidenceEntry("document", "type", facts.label, 0.85))
    if facts.ocr_read:
        found.append(
            EvidenceEntry("document", "text", "read by OCR — this was a scan", 0.85)
        )
    return found


def _named_itself(identity) -> bool:  # noqa: ANN001
    """Did something *inside* the document name it?

    The filename is a candidate and it is not the document speaking. A `.txt`
    called `notes` has a title in the same sense every file has a title, and
    treating that as an identity would give every file in the inbox the
    confidence of one that was actually read.
    """
    from librairy.document_identity import FILENAME

    return bool(identity.title) and identity.source != FILENAME


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
