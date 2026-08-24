"""Documents, read from the document rather than from its name.

    Inbox/scan-0473.pdf   ->   2024 CR-V Owner's Manual · Honda · 643 pages

Every other medium in LibrAIry is identified from the file: music reads tags
and can ask a catalog about the audio, photographs carry EXIF, films have a
year in the container. Documents were the exception — `classify_document_like`
read the filename and nothing else, so a scan became `Documents/Unknown/` and
a book became `Books/Unknown Author/`.

The documents in these tests are **real files**: valid PDFs with real Info
dictionaries, and real EPUB zips with real OPF manifests. A test that mocked
`pdfinfo` would be a test of the mock, and the scanned case in particular has
to be a PDF that genuinely has pages and no text in them.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from librairy.classify.documents import classify_document_like
from librairy.config import Settings
from librairy.db import connect
from librairy.docmeta import BOOK, FINANCIAL, MANUAL, PAPER, UNKNOWN, facts_for, readable
from librairy.scanner import scan_root
from tests.support.documents import build_pdf, write_epub

MANUAL_LINES = (
    "2024 CR-V Owner's Manual",
    "American Honda Motor Co., Inc.",
    "Read this manual before operating the vehicle.",
)

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


def pdf_at(tmp_path: Path, name: str, **kwargs) -> tuple[Path, Settings]:
    settings = settings_for(tmp_path)
    path = settings.inbox_dir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(build_pdf(**kwargs))
    return path, settings


# --- 17-20: what the document says about itself --------------------------------


@poppler
def test_a_pdfs_own_title_and_author_are_read(tmp_path: Path) -> None:
    path, settings = pdf_at(
        tmp_path,
        "scan-0473.pdf",
        title="2024 CR-V Owner's Manual",
        author="Honda Motor Co.",
        lines=MANUAL_LINES,
        pages=3,
    )

    facts = facts_for(path, settings)

    assert facts.title == "2024 CR-V Owner's Manual"
    assert facts.author == "Honda Motor Co."
    assert facts.pages == 3
    assert facts.kind == MANUAL
    assert facts.scanned is False
    assert ("PDF title metadata", "2024 CR-V Owner's Manual") in facts.sources


@poppler
def test_a_pdf_with_no_metadata_falls_back_to_its_first_page(tmp_path: Path) -> None:
    """Used only where the file said nothing — never to correct metadata."""
    path, settings = pdf_at(
        tmp_path, "doc.pdf", lines=("Quarterly Safety Report 2024", "Section one")
    )

    facts = facts_for(path, settings)

    assert facts.title == "Quarterly Safety Report 2024"
    assert ("first page", "Quarterly Safety Report 2024") in facts.sources


@poppler
def test_a_scanned_pdf_is_reported_as_a_scan_and_nothing_is_invented(
    tmp_path: Path,
) -> None:
    """There is no OCR in this program, so a document it could not read says so."""
    path, settings = pdf_at(tmp_path, "scan.pdf", pages=4)

    facts = facts_for(path, settings)

    assert facts.scanned is True
    assert facts.pages == 4
    assert facts.title == ""
    assert facts.author == ""
    assert facts.identified is False
    assert ("text", "no text layer — this is a scan") in facts.sources


def test_an_epubs_metadata_is_read_without_any_binary(tmp_path: Path) -> None:
    """A zip and an XML manifest, so this works wherever Python does."""
    settings = settings_for(tmp_path)
    path = settings.inbox_dir / "book.epub"
    write_epub(
        path,
        title="Dune",
        author="Frank Herbert",
        identifier="urn:isbn:9780441013593",
        date="1965-08-01",
    )

    facts = facts_for(path, settings)

    assert facts.title == "Dune"
    assert facts.author == "Frank Herbert"
    assert facts.isbn == "9780441013593"
    assert facts.year == 1965
    assert facts.kind == BOOK


def test_an_unreadable_document_is_no_facts_rather_than_an_error(
    tmp_path: Path,
) -> None:
    settings = settings_for(tmp_path)
    path = settings.inbox_dir / "broken.epub"
    path.write_bytes(b"not a zip at all")

    facts = facts_for(path, settings)

    assert facts.identified is False
    assert facts.kind == BOOK  # an .epub is a book by construction


def test_only_formats_with_a_reader_are_opened(tmp_path: Path) -> None:
    assert readable("a/b.pdf") is True
    assert readable("a/b.epub") is True
    assert readable("a/b.docx") is False
    assert readable("a/b.jpg") is False


# --- 21-22: the evidence ladder ------------------------------------------------


@poppler
def test_the_documents_own_title_outranks_the_filename(tmp_path: Path) -> None:
    """`scan-0473` and `2024 CR-V Owner's Manual` are not two candidate titles."""
    path, settings = pdf_at(
        tmp_path,
        "scan-0473.pdf",
        title="2024 CR-V Owner's Manual",
        author="Honda Motor Co.",
        lines=MANUAL_LINES,
        pages=3,
    )

    result = classify_document_like(
        "scan-0473.pdf", settings=settings, facts=facts_for(path, settings)
    )

    assert "CR-V" in result.clean_name
    assert "0473" not in result.clean_name
    assert result.confidence >= 0.85


@poppler
def test_a_catalog_is_not_asked_about_a_book_that_named_itself(
    tmp_path: Path,
) -> None:
    """A round trip spent overwriting better evidence is a round trip too many."""
    settings = settings_for(tmp_path)
    path = settings.inbox_dir / "dune.epub"
    write_epub(path, title="Dune", author="Frank Herbert", identifier="urn:isbn:9780441013593")
    asked: list[str] = []

    def lookup(title: str):  # noqa: ANN202
        asked.append(title)
        return None

    result = classify_document_like(
        "dune.epub",
        settings=settings,
        book_lookup=lookup,
        facts=facts_for(path, settings),
    )

    assert asked == []
    assert result.fields["author"] == "Frank Herbert"
    assert result.fields["title"] == "Dune"


def test_a_weak_filename_does_not_invent_an_author(tmp_path: Path) -> None:
    """No facts, so nothing is claimed — exactly as before this existed."""
    settings = settings_for(tmp_path)

    result = classify_document_like("scan 0473.pdf", settings=settings, facts=None)

    assert result.category == "documents"
    assert "author" not in result.fields


# --- 23-26: what kind of document ----------------------------------------------


@poppler
@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        (("2024 CR-V Owner's Manual", "Read before operating."), MANUAL),
        (("Router XR500 User Guide", "Setup"), MANUAL),
        (("Abstract", "We show that... doi:10.1000/xyz123"), PAPER),
        (("Account statement", "Statement period: 1 March to 31 March"), FINANCIAL),
        (("Notes from the meeting", "We talked about things."), UNKNOWN),
    ],
)
def test_broad_document_types_come_from_deterministic_words(
    tmp_path: Path, lines: tuple[str, ...], expected: str
) -> None:
    path, settings = pdf_at(tmp_path, "doc.pdf", lines=lines, pages=2)

    assert facts_for(path, settings).kind == expected


@poppler
def test_a_doi_makes_it_a_paper_whatever_it_is_called(tmp_path: Path) -> None:
    path, settings = pdf_at(
        tmp_path,
        "notes.pdf",
        title="Some notes",
        lines=("Introduction", "See 10.1038/nphys1170 for the method."),
    )

    facts = facts_for(path, settings)

    assert facts.kind == PAPER
    assert facts.doi == "10.1038/nphys1170"


@poppler
def test_an_isbn_makes_it_a_book_and_it_is_filed_as_one(tmp_path: Path) -> None:
    path, settings = pdf_at(
        tmp_path,
        "download.pdf",
        title="The Pragmatic Programmer",
        author="Hunt and Thomas",
        lines=("The Pragmatic Programmer", "ISBN 978-0-13-595705-9"),
    )

    result = classify_document_like(
        "download.pdf", settings=settings, facts=facts_for(path, settings)
    )

    assert result.category == "books"
    assert result.fields["author"] == "Hunt and Thomas"
    assert str(result.dest_relpath).startswith("Books/Hunt and Thomas/")


@poppler
def test_an_unidentified_document_stays_an_honest_document(tmp_path: Path) -> None:
    """A scan LibrAIry could not read is not upgraded by having been looked at."""
    path, settings = pdf_at(tmp_path, "scan.pdf", pages=4)

    result = classify_document_like(
        "scan.pdf", settings=settings, facts=facts_for(path, settings)
    )

    assert result.category == "documents"
    assert result.confidence < 0.8
    assert result.dest_relpath is None
    assert any("scan" in str(entry.detail) for entry in result.evidence)


# --- 34: privacy ---------------------------------------------------------------


def test_what_a_document_said_about_itself_never_reaches_a_provider() -> None:
    """A title read out of a PDF can *be* the private part of the file.

    "Account statement, March 2024" is not a filing hint. It is also evidence a
    model is not needed for, since a document that named itself is already
    classified deterministically — so it is dropped at the redaction boundary
    whatever provider chain is configured.
    """
    from librairy.ai.redact import build_view
    from librairy.models import EvidenceEntry, Item

    item = Item(
        id=1,
        root="inbox",
        relpath="statement.pdf",
        size=1024,
        mtime_ns=0,
        fingerprint="abc",
        state="analyzed",
        first_seen_at="2026-08-24T00:00:00Z",
        last_seen_at="2026-08-24T00:00:00Z",
        missing_since=None,
    )
    evidence = (
        EvidenceEntry("document", "pdf title metadata", "Account statement March 2024", 0.9),
        EvidenceEntry("document", "type", "Financial document", 0.85),
        EvidenceEntry("heuristic", "category", "document extension", 0.72),
    )

    view = build_view(item, {}, evidence)

    printed = " ".join(view.evidence_summaries)
    assert "Account statement" not in printed
    assert "March 2024" not in printed
    assert "document extension" in printed


@poppler
def test_reading_a_document_asks_nothing_of_the_network(
    tmp_path: Path, monkeypatch
) -> None:
    import urllib.request

    def forbidden(*_args, **_kwargs):
        raise AssertionError("reading a document must not reach the network")

    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    path, settings = pdf_at(
        tmp_path, "m.pdf", title="A Manual", lines=MANUAL_LINES, pages=2
    )

    assert facts_for(path, settings).title == "A Manual"


# --- 27-33: the whole workflow -------------------------------------------------


def analysed(tmp_path: Path, name: str, **kwargs):
    """Inbox → analyse, the way the worker does it."""
    from librairy.classify import classify_item

    path, settings = pdf_at(tmp_path, name, **kwargs)
    conn = connect(settings)
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    result = classify_item(path, name, settings)
    return conn, settings, result


@poppler
def test_the_review_row_shows_the_identity_rather_than_the_extension(
    tmp_path: Path,
) -> None:
    from librairy.web.review import _document_row

    path, settings = pdf_at(
        tmp_path,
        "scan-0473.pdf",
        title="2024 CR-V Owner's Manual",
        author="Honda Motor Co.",
        lines=MANUAL_LINES,
        pages=3,
    )
    result = classify_document_like(
        "scan-0473.pdf", settings=settings, facts=facts_for(path, settings)
    )
    row = {"evidence": _encode(result.evidence)}

    found = _document_row(row)

    assert found is not None
    assert found["title"] == "2024 CR-V Owner's Manual"
    assert found["author"] == "Honda Motor Co."
    assert found["scanned"] is False


@poppler
def test_the_review_row_says_when_a_document_is_a_scan(tmp_path: Path) -> None:
    from librairy.web.review import _document_row

    path, settings = pdf_at(tmp_path, "scan.pdf", pages=4)
    result = classify_document_like(
        "scan.pdf", settings=settings, facts=facts_for(path, settings)
    )

    found = _document_row({"evidence": _encode(result.evidence)})

    assert found is not None
    assert found["scanned"] is True
    assert found["title"] == ""


def test_a_row_with_no_document_evidence_has_no_identity_block(tmp_path: Path) -> None:
    from librairy.web.review import _document_row

    assert _document_row({"evidence": None}) is None


def _encode(evidence) -> str:  # noqa: ANN001
    import json

    return json.dumps(
        [
            {
                "source": entry.source,
                "field": entry.field,
                "detail": entry.detail,
                "weight": entry.weight,
            }
            for entry in evidence
        ]
    )


@poppler
def test_a_document_is_filed_and_undone_through_the_ordinary_workflow(
    tmp_path: Path,
) -> None:
    """No document planner and no document executor: the same plan as everything."""
    from librairy.corrections import undo_correction
    from librairy.executor import execute_plan
    from librairy.planner import OperationSpec, approve_plan, create_plan

    conn, settings, result = analysed(
        tmp_path,
        "scan-0473.pdf",
        title="2024 CR-V Owner's Manual",
        author="Honda Motor Co.",
        lines=MANUAL_LINES,
        pages=3,
    )

    assert result.dest_relpath is not None
    plan_id = create_plan(
        conn,
        [
            OperationSpec(
                op_type="move",
                src_root="inbox",
                src_relpath="scan-0473.pdf",
                dest_root="library",
                dest_relpath=str(result.dest_relpath),
            )
        ],
        settings,
    )
    approve_plan(conn, plan_id, settings)

    assert (settings.inbox_dir / "scan-0473.pdf").is_file(), "approval moves nothing"

    execute_plan(conn, plan_id, settings)

    assert (settings.library_dir / str(result.dest_relpath)).is_file()
    assert not (settings.inbox_dir / "scan-0473.pdf").exists()

    undo_correction(conn, settings, plan_id)

    assert (settings.inbox_dir / "scan-0473.pdf").is_file()
    assert not (settings.library_dir / str(result.dest_relpath)).exists()


@poppler
def test_browse_and_search_see_the_filed_document(tmp_path: Path) -> None:
    from librairy.search import search_items

    conn, settings, result = analysed(
        tmp_path,
        "scan-0473.pdf",
        title="2024 CR-V Owner's Manual",
        author="Honda Motor Co.",
        lines=MANUAL_LINES,
        pages=3,
    )
    target = settings.library_dir / str(result.dest_relpath)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes((settings.inbox_dir / "scan-0473.pdf").read_bytes())
    scan_root(conn, "library", settings.library_dir, settings)

    found = search_items(conn, "CR-V")

    assert any("CR-V" in str(row["relpath"]) for row in found)


def test_the_name_only_pass_does_not_get_to_answer_for_a_document(
    tmp_path: Path,
) -> None:
    """The fast heuristic was confidently wrong and nothing ever opened the file.

    `dune.epub` answered `Books/Unknown-Author/dune/` at 0.85 — above the
    confidence threshold, so classification stopped there and the `Frank
    Herbert` written inside the file was never read.
    """
    from librairy.classify import classify_item, classify_path

    settings = settings_for(tmp_path)
    path = settings.inbox_dir / "dune.epub"
    write_epub(path, title="Dune", author="Frank Herbert", identifier="urn:isbn:9780441013593")

    guessed = classify_path(path, settings)
    result = classify_item(path, "dune.epub", settings)

    assert guessed.fields["author"] == "Unknown Author"
    assert result.fields["author"] == "Frank Herbert"
    assert str(result.dest_relpath) == "Books/Frank Herbert/Dune/Dune.epub"


@poppler
def test_a_document_that_says_nothing_keeps_the_answer_it_had(tmp_path: Path) -> None:
    """Only a document that actually spoke displaces the name-only pass."""
    from librairy.classify import classify_item, classify_path

    path, settings = pdf_at(tmp_path, "holiday-notes.pdf", pages=2)

    guessed = classify_path(path, settings)
    result = classify_item(path, "holiday-notes.pdf", settings)

    assert (guessed.category if guessed else result.category) == result.category
    assert result.category == "documents"


# --- 1-14: the Documents hierarchy ---------------------------------------------


@poppler
def test_a_manual_with_a_manufacturer_is_filed_under_it(tmp_path: Path) -> None:
    path, settings = pdf_at(
        tmp_path,
        "scan-0473.pdf",
        title="2024 CR-V Owner's Manual",
        author="Honda Motor Co.",
        lines=MANUAL_LINES,
        pages=3,
    )

    result = classify_document_like(
        "scan-0473.pdf", settings=settings, facts=facts_for(path, settings)
    )

    #  The trailing dot of `Co.` goes, because a component ending in a dot is
    #  a component Windows silently drops. Everything else survives.
    assert result.dest_relpath == (
        "Documents/Manuals/Honda Motor Co/2024 CR-V Owner's Manual.pdf"
    )


@poppler
def test_a_manual_with_no_trustworthy_maker_is_filed_one_level_up(
    tmp_path: Path,
) -> None:
    """Absence of evidence makes *less* structure, never invented structure."""
    path, settings = pdf_at(
        tmp_path,
        "guide.pdf",
        title="Router XR500 User Guide",
        author="Acrobat Distiller 11.0",
        lines=("Router XR500 User Guide", "Setup"),
    )

    result = classify_document_like(
        "guide.pdf", settings=settings, facts=facts_for(path, settings)
    )

    assert result.dest_relpath == "Documents/Manuals/Router XR500 User Guide.pdf"
    assert "Unknown" not in str(result.dest_relpath)
    assert "General" not in str(result.dest_relpath)


@poppler
def test_a_financial_document_is_filed_by_its_own_year(tmp_path: Path) -> None:
    path, settings = pdf_at(
        tmp_path,
        "doc.pdf",
        title="Account Statement March 2024",
        lines=("Account statement", "Statement period: 1 March to 31 March"),
    )

    result = classify_document_like(
        "doc.pdf", settings=settings, facts=facts_for(path, settings)
    )

    assert result.dest_relpath == (
        "Documents/Financial/2024/Account Statement March 2024.pdf"
    )


@poppler
def test_the_import_year_is_never_substituted_for_a_missing_document_year(
    tmp_path: Path,
) -> None:
    """"I filed this in 2026" is a fact about the import, not about the file."""
    import datetime

    path, settings = pdf_at(
        tmp_path,
        "doc.pdf",
        title="Account Statement",
        lines=("Account statement", "Amount due on receipt"),
    )

    result = classify_document_like(
        "doc.pdf", settings=settings, facts=facts_for(path, settings)
    )

    assert result.dest_relpath == "Documents/Financial/Account Statement.pdf"
    assert str(datetime.date.today().year) not in str(result.dest_relpath)


@poppler
def test_a_paper_with_an_author_is_filed_under_the_first_one(tmp_path: Path) -> None:
    path, settings = pdf_at(
        tmp_path,
        "preprint.pdf",
        title="On the Electrodynamics of Moving Bodies",
        author="Einstein, A.; Grossmann, M.",
        lines=("Abstract", "We show that... doi:10.1000/xyz123"),
    )

    result = classify_document_like(
        "preprint.pdf", settings=settings, facts=facts_for(path, settings)
    )

    #  `Einstein, A` and not `Einstein`: the comma separates a surname from
    #  initials, and dropping them is dropping the half that tells two
    #  Einsteins apart. The semicolon is what separates authors.
    assert result.dest_relpath == (
        "Documents/Papers/Einstein, A/On the Electrodynamics of Moving Bodies.pdf"
    )


@poppler
def test_a_paper_with_no_author_falls_back_to_its_year(tmp_path: Path) -> None:
    path, settings = pdf_at(
        tmp_path,
        "paper.pdf",
        title="A Study of Something 2019",
        lines=("Abstract", "Proceedings of the conference"),
    )

    result = classify_document_like(
        "paper.pdf", settings=settings, facts=facts_for(path, settings)
    )

    assert result.dest_relpath == (
        "Documents/Papers/2019/A Study of Something 2019.pdf"
    )


@poppler
def test_a_paper_with_neither_keeps_the_shallowest_branch(tmp_path: Path) -> None:
    path, settings = pdf_at(
        tmp_path,
        "paper.pdf",
        title="Notes on a Method",
        lines=("Abstract", "The method is described below."),
    )

    result = classify_document_like(
        "paper.pdf", settings=settings, facts=facts_for(path, settings)
    )

    assert result.dest_relpath == "Documents/Papers/Notes on a Method.pdf"


@poppler
def test_a_generic_document_keeps_the_dated_branch_it_always_had(
    tmp_path: Path,
) -> None:
    """Backward conceptual compatibility: nothing about the old shape changed."""
    path, settings = pdf_at(
        tmp_path,
        "notes.pdf",
        title="Meeting Notes 2024",
        lines=("Meeting Notes 2024", "We talked about things."),
    )

    result = classify_document_like(
        "notes.pdf", settings=settings, facts=facts_for(path, settings)
    )

    assert result.dest_relpath == "Documents/2024/Meeting Notes 2024.pdf"


def test_the_book_hierarchy_is_unchanged(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    path = settings.inbox_dir / "dune.epub"
    write_epub(path, title="Dune", author="Frank Herbert", identifier="urn:isbn:9780441013593")

    result = classify_document_like(
        "dune.epub", settings=settings, facts=facts_for(path, settings)
    )

    assert result.dest_relpath == "Books/Frank Herbert/Dune/Dune.epub"


@poppler
def test_document_filenames_keep_the_punctuation_the_title_had(
    tmp_path: Path,
) -> None:
    """Readable, and filesystem-safe in that order. A slash is still a
    separator; an apostrophe is not."""
    path, settings = pdf_at(
        tmp_path,
        "x.pdf",
        title="Rock & Roll: What's It All About? (2nd ed.)",
        lines=("Rock & Roll", "Chapter one"),
    )

    result = classify_document_like(
        "x.pdf", settings=settings, facts=facts_for(path, settings)
    )

    #  Whatever the existing sanitizer does with `:` and `?` is what it does —
    #  the point is that the ampersand, the apostrophe and the brackets survive.
    assert "&" in result.clean_name
    assert "What's" in result.clean_name
    assert "(2nd ed.)" in result.clean_name
    assert result.clean_name.endswith(".pdf")


@poppler
def test_a_scan_with_no_identity_gets_no_hierarchy_at_all(tmp_path: Path) -> None:
    path, settings = pdf_at(tmp_path, "IMG_20240612_0001.pdf", pages=4)

    result = classify_document_like(
        "IMG_20240612_0001.pdf", settings=settings, facts=facts_for(path, settings)
    )

    assert result.dest_relpath is None
    assert result.category == "documents"


def test_the_new_taxonomy_does_not_report_documents_already_filed(
    tmp_path: Path,
) -> None:
    """Like Music naming: the policy applies to new filing, not to what is
    already on the shelf. An audit that suddenly reported every document for
    being in the folder LibrAIry itself chose last year is house style wearing
    a defect's clothes."""
    from librairy.audit import audit_library

    settings = settings_for(tmp_path)
    conn = connect(settings)
    filed = settings.library_dir / "Documents/2024/2024 CR-V Owner's Manual.pdf"
    filed.parent.mkdir(parents=True, exist_ok=True)
    filed.write_bytes(build_pdf(title="2024 CR-V Owner's Manual", author="Honda Motor Co.",
                               lines=MANUAL_LINES, pages=3))
    scan_root(conn, "library", settings.library_dir, settings)

    audit_library(conn, settings)

    findings = conn.execute(
        "SELECT kind, relpath FROM audit_findings WHERE relpath LIKE 'Documents/%'"
    ).fetchall()
    assert [row["kind"] for row in findings] == []
