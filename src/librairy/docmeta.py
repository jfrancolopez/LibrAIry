"""What a document actually is, from the document rather than from its name.

    Inbox/scan-0473.pdf
      title      2024 CR-V Owner's Manual
      author     Honda Motor Co.
      pages      643
      type       manual

Every other medium in LibrAIry is identified from the file. Music reads tags
and can ask a catalog about the audio itself; photographs carry EXIF; films
have a year in the container. Documents were the exception: `classify_document`
read the **filename** and nothing else, so `scan-0473.pdf` became
`Documents/Unknown/scan-0473.pdf` and a book became `Books/Unknown Author/`.

The pieces to do better were already here and unused for this. `poppler-utils`
is in the image for text extraction, and `pdfinfo` comes with it. An EPUB is a
zip with an XML manifest in it, which needs nothing at all. So this reads what
the document says about itself, and only falls back to the name when the
document says nothing.

**The ladder, strongest first**, and every rung is something *recorded* rather
than inferred:

    1  embedded metadata      the PDF's Info dictionary, the EPUB's OPF
    2  an identifier          an ISBN or a DOI printed in the front matter
    3  front-matter text      the first page, where a title page is
    4  the filename           somebody typed it, possibly wrongly

    --- the line ---

       a model's suggestion   plausible is not the same as true

Nothing below the line is read here at all: this module runs no model and
makes no request. Where a model does contribute elsewhere, **deterministic
metadata outranks it** — a title read out of the file is not overwritten by a
title something guessed from the file.

**Scanned documents are said to be scanned.** A PDF with pages and no
extractable text is an image of a document, and LibrAIry has no OCR — there is
no tesseract in the image and this pass does not add one. So it reports what
is true, `no text layer`, and classifies from metadata and the filename
without pretending anything was read. The alternative is a document that looks
identified and is not.

**Nothing here runs on GET.** `pdfinfo` and `pdftotext` are subprocesses, and a
Review page drawing forty rows must not spawn eighty of them. Facts are read
during analysis, when the file is already being opened, and what a page renders
afterwards is the stored answer.
"""

from __future__ import annotations

import re
import subprocess
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree

from librairy.config import Settings

#  How far into a document to look for a title page, an ISBN or an abstract.
#  Front matter is at the front; reading a 643-page manual to find out it is a
#  manual is work nobody asked for.
FRONT_PAGES = 2
FRONT_CHARS = 4000

#  Under this many characters on the first pages, a PDF that has pages has no
#  text layer worth the name. Not zero: a scan often carries a stray ligature
#  or a page number from the scanner's own software.
TEXT_FLOOR = 40

SECONDS = 20

#  Broad types, and only ones that change what a person does with the row. A
#  taxonomy of fifty document kinds is a taxonomy nobody maintains.
BOOK = "book"
MANUAL = "manual"
PAPER = "paper"
FINANCIAL = "financial"
UNKNOWN = "document"

TYPE_LABEL = {
    BOOK: "Book",
    MANUAL: "Manual",
    PAPER: "Paper",
    FINANCIAL: "Financial document",
    UNKNOWN: "Document",
}

_ISBN = re.compile(r"\bISBN(?:-1[03])?:?\s*((?:97[89][- ]?)?(?:\d[- ]?){9}[\dXx])\b")
_DOI = re.compile(r"\b(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)\b")
_YEAR = re.compile(r"\b(19\d{2}|20\d{2})\b")

_MANUAL_WORDS = re.compile(
    r"(?i)\b(owner'?s manual|user'?s? (?:manual|guide)|instruction manual|"
    r"service manual|installation guide|quick start guide|reference manual)\b"
)
_PAPER_WORDS = re.compile(r"(?i)\b(abstract|doi:|arxiv|proceedings of|journal of)\b")
#  Deliberately narrow. "Balance" and "total" appear in a physics paper; these
#  phrases do not appear anywhere else.
_FINANCIAL_WORDS = re.compile(
    r"(?i)\b(account statement|statement period|invoice number|invoice no\.?|"
    r"tax invoice|payment receipt|amount due|billing period)\b"
)


@dataclass(frozen=True)
class DocumentFacts:
    """What the document said about itself, and where each part came from."""

    title: str = ""
    author: str = ""
    subject: str = ""
    pages: int = 0
    year: int = 0
    isbn: str = ""
    doi: str = ""
    kind: str = UNKNOWN
    #  A PDF with pages and no text layer. Reported, never worked around:
    #  LibrAIry has no OCR and does not pretend the text was read.
    scanned: bool = False
    #  Whether anything could be read at all. False for a format with no
    #  reader, or a tool that is not installed — which is a normal outcome and
    #  never an error.
    read: bool = False
    sources: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    @property
    def identified(self) -> bool:
        """Is there a title from the document itself?"""
        return bool(self.title)

    @property
    def label(self) -> str:
        return TYPE_LABEL.get(self.kind, TYPE_LABEL[UNKNOWN])

    @property
    def facts(self) -> tuple[tuple[str, str], ...]:
        """The identity as label/value pairs, for a row to print plainly."""
        found: list[tuple[str, str]] = [("Type", self.label)]
        for label, value in (
            ("Title", self.title),
            ("Author", self.author),
            ("ISBN", self.isbn),
            ("DOI", self.doi),
        ):
            if value:
                found.append((label, value))
        if self.pages:
            found.append(("Pages", str(self.pages)))
        if self.year:
            found.append(("Year", str(self.year)))
        if self.scanned:
            found.append(("Text", "no text layer — this is a scan"))
        return tuple(found)


def readable(relpath: str) -> bool:
    """Whether this is a document whose insides LibrAIry knows how to read."""
    return PurePosixPath(relpath).suffix.lower() in {".pdf", ".epub"}


def facts_for_item(
    conn, settings: Settings, item_id: int, path: Path  # noqa: ANN001
) -> DocumentFacts:
    """This document's identity, measured once and remembered against its bytes.

    Analysis calls this; a page render calls `cached_facts` and takes no for an
    answer. The difference is the whole rule: `pdfinfo` and `pdftotext` are
    subprocesses, and a Review page of forty rows must not spawn eighty of
    them. Re-reading the same 643-page manual on every screen that mentions it
    is the same waste at a smaller scale.
    """
    from librairy.planner import utc_now
    from librairy.tools.common import DOCUMENT_TOOL, set_cached_metadata

    cached = cached_facts(conn, item_id, path)
    if cached is not None:
        return cached
    facts = facts_for(path, settings)
    fingerprint = _fingerprint(conn, item_id)
    if fingerprint:
        set_cached_metadata(
            conn, item_id, fingerprint, DOCUMENT_TOOL, _as_payload(facts), utc_now()
        )
    return facts


def cached_facts(conn, item_id: int, path: Path) -> DocumentFacts | None:  # noqa: ANN001
    """What was measured about *these bytes*, or None. Never measures anything.

    None means "nobody has looked at this file yet", and a page that gets it
    says so rather than opening the document to find out. An answer recorded
    against a previous version of the file is also None: a page count from
    before somebody replaced the scan is a fact about a file that is gone.
    """
    from librairy.tools.common import DOCUMENT_TOOL, get_cached_metadata

    if conn is None or not readable(path.name):
        return None
    fingerprint = _fingerprint(conn, item_id)
    if not fingerprint:
        return None
    payload = get_cached_metadata(conn, item_id, fingerprint, DOCUMENT_TOOL)
    return _from_payload(payload) if payload else None


def _fingerprint(conn, item_id: int) -> str:  # noqa: ANN001
    row = conn.execute(
        "SELECT fingerprint FROM items WHERE id=?", (item_id,)
    ).fetchone()
    return str(row["fingerprint"] or "") if row else ""


def _as_payload(facts: DocumentFacts) -> dict:
    """The normalised fields, not the raw metadata dump.

    A cache holding everything a tool can say is a cache nobody can read and a
    row that grows with the tool's verbosity. These are the fields something
    consumes.
    """
    return {
        "title": facts.title,
        "author": facts.author,
        "subject": facts.subject,
        "pages": facts.pages,
        "year": facts.year,
        "isbn": facts.isbn,
        "doi": facts.doi,
        "kind": facts.kind,
        "scanned": facts.scanned,
        "read": facts.read,
        "sources": [list(pair) for pair in facts.sources],
    }


def _from_payload(payload: dict) -> DocumentFacts:
    return DocumentFacts(
        title=str(payload.get("title") or ""),
        author=str(payload.get("author") or ""),
        subject=str(payload.get("subject") or ""),
        pages=int(payload.get("pages") or 0),
        year=int(payload.get("year") or 0),
        isbn=str(payload.get("isbn") or ""),
        doi=str(payload.get("doi") or ""),
        kind=str(payload.get("kind") or UNKNOWN),
        scanned=bool(payload.get("scanned")),
        read=bool(payload.get("read")),
        sources=tuple(
            (str(pair[0]), str(pair[1]))
            for pair in payload.get("sources") or []
            if isinstance(pair, list | tuple) and len(pair) == 2
        ),
    )


def facts_for(
    path: Path,
    settings: Settings,
    *,
    run=None,  # noqa: ANN001
) -> DocumentFacts:
    """Read one document's identity. Analysis-time work, never a page render.

    `run` is the seam the tests drive in place of `subprocess.run`. Every
    failure — a missing binary, an encrypted file, a timeout, a PDF poppler
    cannot parse — comes back as "nothing was read" rather than an exception:
    a document nobody could identify is a normal outcome and must not cost a
    scan.
    """
    suffix = path.suffix.lower()
    if suffix == ".epub":
        return _epub(path)
    if suffix == ".pdf":
        return _pdf(path, settings, run=run or subprocess.run)
    return DocumentFacts()


# --- PDF ------------------------------------------------------------------------------


def _pdf(path: Path, settings: Settings, *, run) -> DocumentFacts:  # noqa: ANN001
    info = _pdfinfo(path, run=run)
    text = _front_text(path, run=run)
    title = _clean(info.get("Title", ""))
    author = _clean(info.get("Author", ""))
    pages = _int(info.get("Pages", ""))
    sources: list[tuple[str, str]] = []
    if title:
        sources.append(("PDF title metadata", title))
    if author:
        sources.append(("PDF author metadata", author))
    #  The title page, used only when the file's own metadata said nothing.
    #  Never to *correct* metadata: a running header is not more authoritative
    #  than what the producing application wrote down.
    if not title and text:
        heading = _heading(text)
        if heading:
            title = heading
            sources.append(("first page", heading))
    isbn = _first(_ISBN, text)
    doi = _first(_DOI, text)
    if isbn:
        sources.append(("ISBN in the text", isbn))
    if doi:
        sources.append(("DOI in the text", doi))
    scanned = bool(pages) and len(text.strip()) < TEXT_FLOOR
    if scanned:
        sources.append(("text", "no text layer — this is a scan"))
    year = _year(info.get("CreationDate", "")) or _year(title)
    return DocumentFacts(
        title=title,
        author=author,
        subject=_clean(info.get("Subject", "")),
        pages=pages,
        year=year,
        isbn=isbn,
        doi=doi,
        kind=classify(title=title, text=text, isbn=isbn, doi=doi, suffix=".pdf"),
        scanned=scanned,
        read=bool(info or text),
        sources=tuple(sources),
    )


def _pdfinfo(path: Path, *, run) -> dict[str, str]:  # noqa: ANN001
    """The Info dictionary, via poppler — which the image already installs."""
    try:
        result = run(
            ["pdfinfo", str(path)],
            capture_output=True,
            text=True,
            timeout=SECONDS,
            check=False,
        )
    except Exception:  # noqa: BLE001 - a missing binary means "no facts"
        return {}
    if getattr(result, "returncode", 1) != 0:
        return {}
    found: dict[str, str] = {}
    for line in (result.stdout or "").splitlines():
        key, _, value = line.partition(":")
        if key and value:
            found[key.strip()] = value.strip()
    return found


def _front_text(path: Path, *, run) -> str:  # noqa: ANN001
    """The first pages only. A title page is at the front or it is nowhere."""
    try:
        result = run(
            ["pdftotext", "-l", str(FRONT_PAGES), "-q", str(path), "-"],
            capture_output=True,
            text=True,
            timeout=SECONDS,
            check=False,
        )
    except Exception:  # noqa: BLE001
        return ""
    if getattr(result, "returncode", 1) != 0:
        return ""
    return (result.stdout or "")[:FRONT_CHARS]


# --- EPUB -----------------------------------------------------------------------------


def _epub(path: Path) -> DocumentFacts:
    """Title, author and ISBN out of the OPF. Stdlib only, so it always works."""
    try:
        with zipfile.ZipFile(path) as archive:
            names = [
                name for name in archive.namelist() if name.lower().endswith(".opf")
            ]
            if not names:
                return DocumentFacts(kind=BOOK)
            root = ElementTree.fromstring(archive.read(names[0]))
    except Exception:  # noqa: BLE001 - an unreadable epub simply has no facts
        return DocumentFacts(kind=BOOK)
    found: dict[str, list[str]] = {}
    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1].lower()
        if element.text and element.text.strip():
            found.setdefault(tag, []).append(element.text.strip())
    title = _clean(next(iter(found.get("title", [])), ""))
    author = _clean(next(iter(found.get("creator", [])), ""))
    identifiers = " ".join(found.get("identifier", []))
    isbn = _first(_ISBN, identifiers) or _bare_isbn(identifiers)
    #  An EPUB's `dc:identifier` is whatever the publisher put there — an ISBN
    #  most often, a DOI for a paper distributed as one, a random UUID for a
    #  self-published file. Read what is there and claim nothing else.
    doi = _first(_DOI, identifiers)
    sources: list[tuple[str, str]] = []
    if title:
        sources.append(("EPUB metadata", title))
    if author:
        sources.append(("EPUB author", author))
    if isbn:
        sources.append(("EPUB identifier", isbn))
    return DocumentFacts(
        title=title,
        author=author,
        year=_year(" ".join(found.get("date", []))),
        isbn=isbn,
        doi=doi,
        #  An EPUB is a book by construction. Nothing else is published in one.
        kind=BOOK,
        read=True,
        sources=tuple(sources),
    )


# --- what kind of document ------------------------------------------------------------


def classify(*, title: str, text: str, isbn: str, doi: str, suffix: str) -> str:
    """A broad type, from deterministic evidence only.

    Ordered by how much the evidence proves rather than by how common the type
    is. A DOI is a published paper; an ISBN is a book; the words "owner's
    manual" on a title page are a manual. Everything with none of those is
    called a document, which is what it is.
    """
    if doi:
        return PAPER
    if isbn or suffix in {".epub", ".mobi", ".azw3"}:
        return BOOK
    #  Typographic quotes, because `pdftotext` renders them as they were set:
    #  `Owner’s Manual` is what comes back off a real title page, and a
    #  pattern written with a straight apostrophe matches none of them.
    head = _plain(f"{title}\n{text[:FRONT_CHARS]}")
    if _MANUAL_WORDS.search(head):
        return MANUAL
    if _FINANCIAL_WORDS.search(head):
        return FINANCIAL
    if _PAPER_WORDS.search(head):
        return PAPER
    return UNKNOWN


# --- small readers --------------------------------------------------------------------


def _plain(value: str) -> str:
    """Curly quotes and dashes as their typewriter equivalents, for matching."""
    return (
        str(value or "")
        .replace("’", "'")
        .replace("‘", "'")
        .replace("“", '"')
        .replace("”", '"')
        .replace("–", "-")
        .replace("—", "-")
    )


def _heading(text: str) -> str:
    """The first line of the first page that looks like a title.

    Deliberately dull: the first non-empty line of reasonable length. Nothing
    here scores candidate lines or picks the largest font — a heuristic that
    ranks lines is a heuristic that confidently returns a page number.
    """
    for line in text.splitlines():
        clean = " ".join(line.split())
        if 4 <= len(clean) <= 120 and not clean.isdigit():
            return clean
    return ""


def _first(pattern: re.Pattern[str], text: str) -> str:
    match = pattern.search(text or "")
    return re.sub(r"[- ]", "", match.group(1)) if match and pattern is _ISBN else (
        match.group(1) if match else ""
    )


def _bare_isbn(value: str) -> str:
    """`urn:isbn:9780306406157`, which carries no `ISBN` word to match on."""
    match = re.search(r"(?i)isbn[:\s]*((?:97[89])?\d{9}[\dXx])", value or "")
    return match.group(1) if match else ""


def _clean(value: str) -> str:
    return " ".join(str(value or "").split()).strip()


def _int(value: str) -> int:
    text = str(value or "").strip()
    return int(text) if text.isdigit() else 0


def _year(value: str) -> int:
    match = _YEAR.search(str(value or ""))
    return int(match.group(1)) if match else 0
