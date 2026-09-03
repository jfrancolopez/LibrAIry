"""Documents whose sources do not agree about what they are called.

The case this whole feature exists for is one real file:

    programming_rust_2e.pdf     embedded title `CRACKING`
                                first page `Programming Rust`

LibrAIry filed that as `CRACKING.pdf`. Not through a bug — through a rule that
reads perfectly well until you meet this file, *the document's own metadata
outranks its filename*, applied by stopping at the first source that answered.

Each test here pins three things rather than one, because a proposed title on
its own does not say whether the program understood anything:

    what it decided        the title, and whether a destination was filled in
    what it compared       every source that named the document, on the row
    why                    the sentence that says which was taken and what
                           disagreed with it

Real PDFs throughout — `tests/support/documents.py` builds valid ones — because
a mocked `pdfinfo` would prove the mock. The tests that need poppler skip
without it, which is the same rule the rest of the document tests follow.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from librairy.classify.documents import classify_document_like
from librairy.config import Settings
from librairy.db import connect
from librairy.docmeta import facts_for
from librairy.document_identity import (
    CATALOG,
    CONTENT,
    EMBEDDED,
    FILENAME,
    OCR,
    Candidate,
    agree,
    meaningless_filename,
    producer_default,
    resolve,
)
from librairy.proposals import encode_evidence
from librairy.tools.openlibrary import BookMatch
from tests.support.documents import build_pdf, write_epub

poppler = pytest.mark.skipif(
    shutil.which("pdfinfo") is None, reason="poppler is not installed"
)


def settings_for(tmp_path: Path) -> Settings:
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        FILE_STABILITY_SECONDS=0,
        AUTH_REQUIRED=False,
        OLLAMA_HOST="",
        _env_file=None,
    )
    for directory in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        directory.mkdir(parents=True, exist_ok=True)
    return settings


def a_pdf(tmp_path: Path, name: str, **kwargs: object):
    settings = settings_for(tmp_path)
    path = settings.inbox_dir / name
    path.write_bytes(build_pdf(**kwargs))  # type: ignore[arg-type]
    return path, settings


def classified(tmp_path: Path, name: str, *, book_lookup=None, **kwargs: object):  # noqa: ANN001
    path, settings = a_pdf(tmp_path, name, **kwargs)
    return classify_document_like(
        name,
        settings=settings,
        book_lookup=book_lookup,
        facts=facts_for(path, settings),
    )


def row_for(result):  # noqa: ANN001
    """The document block as Review draws it, through the real encoder."""
    from librairy.web.review import _document_row

    return _document_row({"evidence": encode_evidence(list(result.evidence))})


def titles(result) -> dict[str, str]:
    return {
        entry.field.removeprefix("title/"): entry.detail
        for entry in result.evidence
        if entry.field.startswith("title/")
    }


# --- the case it was built for -------------------------------------------------


@poppler
def test_a_garbage_embedded_title_no_longer_becomes_the_filename(
    tmp_path: Path,
) -> None:
    """The `CRACKING` case, end to end.

    Three sources name a version of one work and one names something else,
    which is a fact about the *set* — no ordering of the sources could have
    found it, because the embedded title outranks the first page under every
    ordering anybody would write down.
    """
    result = classified(
        tmp_path,
        "programming_rust_2e.pdf",
        title="CRACKING",
        author="Jim Blandy",
        lines=("Programming Rust", "Fast, Safe Systems Development"),
        pages=2,
    )

    assert "CRACKING" not in str(result.clean_name)
    assert result.clean_name == "Programming Rust.pdf"
    #  What was compared, and what each said.
    assert titles(result) == {
        "embedded": "CRACKING",
        "content": "Programming Rust",
        "filename": "programming rust 2e",
    }
    #  Why. Named sources, not a score.
    why = next(entry for entry in result.evidence if entry.field == "conflict")
    assert "Embedded title disagrees" in why.detail
    assert "First page and Filename name the same work" in why.detail


@poppler
def test_a_contested_document_is_asked_about_rather_than_taken_on_trust(
    tmp_path: Path,
) -> None:
    """Contested is not weak. There is an answer and a reason for it, so the
    destination is filled in — it is simply not an answer worth approving in
    bulk, so it stays under the threshold that sweeps rows up."""
    from librairy.confidence_tiers import CONFIDENT, UNCERTAIN, tier_for

    result = classified(
        tmp_path,
        "programming_rust_2e.pdf",
        title="CRACKING",
        lines=("Programming Rust",),
        pages=2,
    )

    assert result.dest_relpath, "there is a recommendation, and it is on the row"
    assert result.confidence < CONFIDENT, "and it is not swept up by a bulk approve"
    assert result.ask is True
    assert tier_for(result.evidence, result.confidence, result.dest_relpath) == UNCERTAIN


@poppler
def test_an_identifier_does_not_settle_a_document_that_is_being_argued_about(
    tmp_path: Path,
) -> None:
    """An ISBN is the strongest thing a document can carry, and *which work it
    identifies* is exactly what is in question here. Settling on it would file
    the book the argument was about."""
    from librairy.confidence_tiers import SETTLED, tier_for

    result = classified(
        tmp_path,
        "programming_rust_2e.pdf",
        title="CRACKING",
        lines=("Programming Rust", "ISBN 978-1-4920-5259-3"),
        pages=2,
    )

    assert any(entry.field == "isbn" for entry in result.evidence)
    assert tier_for(result.evidence, result.confidence, result.dest_relpath) != SETTLED


# --- agreement, and what it earns ----------------------------------------------


@poppler
def test_a_document_whose_sources_agree_is_answered_without_a_question(
    tmp_path: Path,
) -> None:
    result = classified(
        tmp_path,
        "scan-0473.pdf",
        title="2024 CR-V Owner's Manual",
        author="Honda Motor Co.",
        lines=("2024 CR-V Owner's Manual", "American Honda Motor Co., Inc."),
        pages=3,
    )

    assert result.ask is False
    assert not any(entry.field == "conflict" for entry in result.evidence)
    assert result.confidence >= 0.88
    row = row_for(result)
    assert row is not None
    assert row["conflict"] == ""
    assert [named["chosen"] for named in row["sources"]].count(True) == 1


def test_a_title_that_says_more_is_the_same_title() -> None:
    """Three spellings of one work. Comparing them means comparing what they
    name, so editions and subtitles come off before the comparison — and stay
    on the wording that gets used."""
    assert agree("Programming Rust, 2nd Edition", "Programming Rust")
    assert agree("programming_rust_2e", "Programming Rust")
    assert agree("Dune: A Novel", "Dune")
    assert not agree("CRACKING", "Programming Rust")
    assert not agree("", "Programming Rust"), "nothing agrees with nothing"


def test_the_strongest_source_in_the_winning_group_supplies_the_wording() -> None:
    """Agreement decides *which* answer; authority decides how it is spelled.
    `Programming Rust, 2nd Edition` is a better filename than `Programming
    Rust` because the edition is part of what the file is."""
    identity = resolve(
        [
            Candidate(FILENAME, "programming rust 2e"),
            Candidate(CONTENT, "Programming Rust"),
            Candidate(CATALOG, "Programming Rust, 2nd Edition"),
        ]
    )

    assert identity.title == "Programming Rust, 2nd Edition"
    assert identity.source == CATALOG
    assert not identity.contested


def test_a_model_may_agree_and_may_never_decide() -> None:
    """Authority level four, permanently. A model's opinion can corroborate
    something and can never be the reason a title is chosen."""
    from librairy.document_identity import MODEL

    identity = resolve([Candidate(MODEL, "Invoices 2019"), Candidate(CONTENT, "Widgets Ltd")])

    assert identity.source == CONTENT
    assert MODEL not in identity.conflicts, "a model's dissent is not a question"


# --- sources that name nothing --------------------------------------------------


def test_a_name_with_no_word_in_it_is_not_a_name() -> None:
    """An arXiv identifier is a perfectly good identifier and says nothing
    about what the paper is called. Counting it made the arXiv PDF in the
    fixture disagree with its own metadata."""
    assert meaningless_filename("1706.03762v5")
    assert meaningless_filename("scan-0473")
    assert meaningless_filename("IMG_20240612_0001")
    assert not meaningless_filename("programming_rust_2e")
    assert not meaningless_filename("2024 CR-V Owner's Manual")


def test_a_producing_applications_default_is_not_a_claim_about_the_document() -> None:
    assert producer_default("Microsoft Word - report.docx")
    assert producer_default("Untitled")
    assert not producer_default("CRACKING"), (
        "deciding a title *looks* wrong is the judgement this refuses to make"
    )


@poppler
def test_an_old_filename_beside_two_agreeing_sources_is_not_a_disagreement(
    tmp_path: Path,
) -> None:
    """Renaming a file is the most ordinary thing anybody does to one. When the
    document's own metadata and its own title page agree, the filename losing
    is not worth an afternoon."""
    result = classified(
        tmp_path,
        "old-copy-final.pdf",
        title="Widgets Quarterly",
        lines=("Widgets Quarterly", "Volume 12"),
        pages=2,
    )

    assert result.ask is False
    assert "filename" not in titles(result) or not any(
        entry.note == "disagrees" and entry.field == "title/filename"
        for entry in result.evidence
    )


@poppler
def test_a_filename_still_disagrees_when_nothing_corroborates_the_metadata(
    tmp_path: Path,
) -> None:
    """The other half of the same rule. One internal source against one
    filename is exactly the shape of the bug, at a smaller scale."""
    result = classified(
        tmp_path, "invoice-2019.pdf", title="CRACKING", lines=(), pages=1
    )

    assert result.ask is True
    why = next(entry for entry in result.evidence if entry.field == "conflict")
    assert "Filename" in why.detail


# --- catalogs -------------------------------------------------------------------


@poppler
def test_a_catalog_that_agrees_is_what_settles_a_disagreement(tmp_path: Path) -> None:
    """A third opinion is worth a round trip precisely when the sources that
    already answered do not agree — which is the one case the catalog used not
    to be asked in."""
    asked: list[str] = []

    def lookup(title: str) -> BookMatch:
        asked.append(title)
        return BookMatch(title="Programming Rust, 2nd Edition", author="Jim Blandy", year=2021)

    result = classified(
        tmp_path,
        "programming_rust_2e.pdf",
        book_lookup=lookup,
        title="CRACKING",
        lines=("Programming Rust", "ISBN 978-1-4920-5259-3"),
        pages=2,
    )

    assert asked, "the disagreement is what made the lookup worth making"
    assert result.fields["title"] == "Programming Rust, 2nd Edition"
    assert titles(result)["catalog"] == "Programming Rust, 2nd Edition"


@poppler
def test_a_catalog_is_still_not_asked_about_a_book_that_named_itself(
    tmp_path: Path,
) -> None:
    """Unchanged, and worth keeping: a catalog that renames a book whose own
    metadata already named it is a round trip spent overwriting better
    evidence."""
    asked: list[str] = []

    def lookup(title: str):  # noqa: ANN202
        asked.append(title)
        return None

    settings = settings_for(tmp_path)
    path = settings.inbox_dir / "dune.epub"
    write_epub(path, title="Dune", author="Frank Herbert", identifier="urn:isbn:9780441013593")
    classify_document_like(
        "dune.epub", settings=settings, book_lookup=lookup, facts=facts_for(path, settings)
    )

    assert asked == []


@poppler
def test_a_catalog_that_disagrees_with_the_document_asks_rather_than_renames(
    tmp_path: Path,
) -> None:
    def lookup(title: str) -> BookMatch:  # noqa: ARG001
        return BookMatch(title="An Entirely Different Book", author=None, year=None)

    result = classified(
        tmp_path, "notes.pdf", book_lookup=lookup, lines=("The Rust Programming Language",)
    )

    assert result.ask is True
    why = next(entry for entry in result.evidence if entry.field == "conflict")
    assert "Catalog" in why.detail or "First page" in why.detail


# --- scans, and when pixels are worth reading -----------------------------------


@poppler
def test_a_readable_pdf_is_never_handed_to_ocr(tmp_path: Path) -> None:
    """The most important thing this feature does not do. A document that
    already has a text layer is one OCR would tell us nothing new about."""
    read: list[Path] = []
    path, settings = a_pdf(
        tmp_path,
        "manual.pdf",
        title="Widgets",
        #  Enough text that `docmeta` calls this a text layer rather than a
        #  scan. Under `TEXT_FLOOR` a PDF is a scan by definition, which is the
        #  right rule and makes a two-word fixture the wrong test of it.
        lines=(
            "Widgets",
            "A manual for the maintenance and operation of widgets",
            "Published by the Widget Company, second edition",
        ),
        pages=2,
    )

    facts = facts_for(path, settings, ocr=lambda page: read.append(page) or "")

    assert read == []
    assert facts.ocr_wanted is False
    assert facts.scanned is False


@poppler
def test_an_image_only_pdf_is_read_when_ocr_is_switched_on(tmp_path: Path) -> None:
    path, settings = a_pdf(tmp_path, "scan.pdf", pages=2)

    facts = facts_for(
        path, settings, ocr=lambda page: "Honda CR-V Owner's Manual\nSection 1"  # noqa: ARG005
    )

    assert facts.ocr_read is True
    assert facts.ocr_title == "Honda CR-V Owner's Manual"
    assert facts.title == "Honda CR-V Owner's Manual"


@poppler
def test_a_scan_nobody_read_says_so_rather_than_guessing(tmp_path: Path) -> None:
    result = classified(tmp_path, "IMG_20240612_0001.pdf", pages=2)

    row = row_for(result)
    assert row is not None
    assert row["scanned"] is True
    assert row["ocr"] is False
    assert row["title"] == "", "nothing named it, so nothing is claimed"


@poppler
def test_ocr_text_is_a_candidate_and_never_an_answer_on_its_own(
    tmp_path: Path,
) -> None:
    """One vote. A title read off a blurry cover competes with everything else
    and wins only where nothing contradicts it."""
    identity = resolve(
        [Candidate(OCR, "Widgets Quarterly"), Candidate(EMBEDDED, "Annual Report 2019")]
    )

    assert identity.source == EMBEDDED, "the embedded title outranks scanned text"
    assert identity.contested
    assert OCR in identity.conflicts


# --- OCR is bounded by the processing mode --------------------------------------


def test_ocr_is_off_until_somebody_switches_it_on(tmp_path: Path) -> None:
    from librairy import ocr
    from librairy.resources import BALANCED, PROCESSING_MODES

    conn = connect(settings_for(tmp_path))
    mode = PROCESSING_MODES[BALANCED]

    assert ocr.enabled(conn) is False
    assert ocr.reader(conn, mode, ocr.budget_for(mode)) is None

    ocr.set_enabled(conn, True)
    assert ocr.enabled(conn) is True


def test_the_ai_mode_does_not_govern_ocr(tmp_path: Path) -> None:
    """Tesseract turns pixels into characters and makes no judgement. Switching
    Local AI off must not stop a scanner's output being readable."""
    from librairy import ocr
    from librairy.resources import AI_OFF, BALANCED, PROCESSING_MODES, set_ai_mode

    conn = connect(settings_for(tmp_path))
    ocr.set_enabled(conn, True)
    set_ai_mode(conn, AI_OFF)
    mode = PROCESSING_MODES[BALANCED]

    #  `available()` is what decides on this machine; the point is that the AI
    #  mode is not consulted at all.
    assert mode.ocr is True
    assert PROCESSING_MODES[BALANCED].ocr_per_cycle == 10


def test_quiet_rations_ocr_rather_than_refusing_it() -> None:
    """Refusing outright would make Quiet answer a scanned document
    *differently* rather than later, which is the one thing a mode may not do."""
    from librairy.resources import BALANCED, FULL, PROCESSING_MODES, QUIET

    assert PROCESSING_MODES[QUIET].ocr is True
    assert PROCESSING_MODES[QUIET].ocr_per_cycle == 2
    assert PROCESSING_MODES[BALANCED].ocr_per_cycle == 10
    assert PROCESSING_MODES[FULL].ocr_per_cycle is None


def test_a_document_whose_turn_did_not_come_is_left_for_the_next_cycle() -> None:
    from librairy.ocr import Budget

    budget = Budget(limit=2)
    for item_id in (1, 2, 3, 4):
        budget.item = item_id
        budget.take()

    assert budget.spent == 2
    assert budget.deferred == [3, 4], "left alone, not answered badly"


def test_the_budget_bounds_the_cycle_and_not_the_document(tmp_path: Path) -> None:
    """Two subprocesses over two pages, whatever the document. A 643-page
    manual costs the same as a two-page receipt."""
    from librairy import ocr

    calls: list[list[str]] = []

    class Done:
        returncode = 0
        stdout = ""

    def fake_run(argv, **kwargs):  # noqa: ANN001, ANN202, ARG001
        calls.append(list(argv))
        return Done()

    ocr.read_pages(tmp_path / "nothing.pdf", run=fake_run)

    assert calls, "poppler was asked to render the front matter"
    assert calls[0][0] == "pdftoppm"
    assert "-l" in calls[0] and str(ocr.PAGES) in calls[0]


# --- waiting for AI, unchanged --------------------------------------------------


@poppler
def test_a_document_that_disagrees_is_never_held_for_want_of_a_provider(
    tmp_path: Path,
) -> None:
    """`waiting` exists for files nothing could say anything about. A
    disagreement is the opposite of one: there is an answer, a reason, and
    something to show — so it goes to Review, not to the held list."""
    from librairy import waiting
    from librairy.classify import analyze_items
    from librairy.scanner import scan_root

    settings = settings_for(tmp_path)
    (settings.inbox_dir / "programming_rust_2e.pdf").write_bytes(
        build_pdf(title="CRACKING", lines=("Programming Rust",), pages=2)
    )
    conn = connect(settings)
    scan_root(conn, "inbox", settings.inbox_dir, settings)

    summary = analyze_items(conn, settings)

    assert summary.held == 0
    assert waiting.total(conn) == 0
    assert conn.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 1


@poppler
def test_a_scan_nothing_could_read_still_waits_for_ai_when_there_is_none(
    tmp_path: Path,
) -> None:
    """The other side of the same boundary, and M2-01's rule unchanged: a file
    nothing named, with no provider to ask, is held rather than guessed at."""
    from librairy import waiting
    from librairy.classify import analyze_items
    from librairy.scanner import scan_root

    settings = settings_for(tmp_path)
    (settings.inbox_dir / "IMG_20240612_0001.pdf").write_bytes(build_pdf(pages=2))
    conn = connect(settings)
    scan_root(conn, "inbox", settings.inbox_dir, settings)

    analyze_items(conn, settings)

    assert waiting.counts(conn) == {waiting.UNAVAILABLE: 1}


# --- the row --------------------------------------------------------------------


@poppler
def test_the_row_shows_what_was_compared_and_which_was_taken(tmp_path: Path) -> None:
    result = classified(
        tmp_path,
        "programming_rust_2e.pdf",
        title="CRACKING",
        author="Jim Blandy",
        lines=("Programming Rust",),
        pages=2,
    )

    row = row_for(result)

    assert row is not None
    assert row["title"] == "Programming Rust"
    assert row["author"] == "Jim Blandy"
    assert [(named["label"], named["conflict"]) for named in row["sources"]] == [
        ("Embedded title", True),
        ("First page", False),
        ("Filename", False),
    ]
    assert "Embedded title disagrees" in str(row["conflict"])


@poppler
def test_a_document_only_one_thing_named_gets_no_comparison_table(
    tmp_path: Path,
) -> None:
    """Nothing to compare is nothing to show. Printing "Filename: notes" on the
    row tells a reader what the row already says."""
    settings = settings_for(tmp_path)
    result = classify_document_like("notes.txt", settings=settings)

    assert row_for(result) is None
