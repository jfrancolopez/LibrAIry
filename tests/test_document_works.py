"""One work, two files, and the three different things that can mean.

    Books/Frank Herbert/Dune/Dune.epub     EPUB · 1.2 MB
    Books/Frank Herbert/Dune/Dune.pdf      PDF  · 4.8 MB

Neither existing workflow could see this pair: the bytes differ so no
fingerprint matches them, and czkawka compares pictures and sound, not
documents. They are the same book and LibrAIry had nothing to say.

Most of these tests are refusals, because the useful part of this feature is
what it will *not* group. A second edition, a 2024 manual beside the 2023 one,
twelve monthly statements from one bank sharing a title and a template — every
one of those is protected by the same single rule: a group needs the *same
identifier*, and nothing here ever compares titles.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from librairy.audit import record_findings
from librairy.config import Settings
from librairy.corrections import CorrectionRefused, undo_correction
from librairy.db import connect
from librairy.docmeta import facts_for_item
from librairy.document_works import KIND, compare, detect, resolve
from librairy.executor import execute_plan
from librairy.scanner import scan_root
from tests.support.documents import build_pdf, write_epub

poppler = pytest.mark.skipif(
    shutil.which("pdfinfo") is None, reason="poppler-utils is not installed"
)

DUNE_ISBN = "urn:isbn:9780441013593"


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


def library(tmp_path: Path):
    settings = settings_for(tmp_path)
    return connect(settings), settings


def analysed(conn, settings: Settings) -> None:
    """Read every filed document once, the way analysis does."""
    scan_root(conn, "library", settings.library_dir, settings)
    for row in conn.execute(
        "SELECT id, relpath FROM items WHERE root='library'"
    ).fetchall():
        relpath = str(row["relpath"])
        if relpath.lower().endswith((".pdf", ".epub")):
            facts_for_item(
                conn, settings, int(row["id"]), settings.library_dir / relpath
            )


def write(settings: Settings, relpath: str, body: bytes) -> None:
    path = settings.library_dir / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)


def epub(settings: Settings, relpath: str, **kwargs) -> None:
    path = settings.library_dir / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    write_epub(path, **kwargs)


def finding_row(conn):
    record_findings(conn, detect(conn))
    return conn.execute(
        "SELECT * FROM audit_findings WHERE kind=?", (KIND,)
    ).fetchone()


def dune(tmp_path: Path):
    """One book, two containers, one ISBN."""
    conn, settings = library(tmp_path)
    epub(settings, "Books/Frank Herbert/Dune/Dune.epub",
         title="Dune", author="Frank Herbert", identifier=DUNE_ISBN)
    write(settings, "Books/Frank Herbert/Dune/Dune.pdf",
          build_pdf(title="Dune", author="Frank Herbert",
                    lines=("Dune", "ISBN 978-0-441-01359-3"), pages=412))
    analysed(conn, settings)
    return conn, settings


# --- 16-17, 21: the same work in two formats -----------------------------------


@poppler
def test_one_isbn_in_two_containers_is_one_work(tmp_path: Path) -> None:
    conn, settings = dune(tmp_path)

    findings = detect(conn)

    assert len(findings) == 1
    assert findings[0].kind == KIND
    assert findings[0].summary == "Dune is filed in 2 formats: EPUB, PDF."


@poppler
def test_the_row_shows_facts_and_prefers_neither_format(tmp_path: Path) -> None:
    """The music preference is about music. Nobody has said whether they would
    rather keep an EPUB or a PDF, so nothing here says it for them."""
    conn, settings = dune(tmp_path)
    view = compare(conn, settings, finding_row(conn))

    assert view is not None
    assert [member.format for member in view.members] == ["EPUB", "PDF"]
    printed = " ".join(
        f"{label} {value}" for member in view.members for label, value in member.facts
    )
    for word in ("preferred", "better", "recommended", "best"):
        assert word not in printed.lower()
    #  And the facts are facts.
    assert "Pages 412" in printed
    assert "Author Frank Herbert" in printed


@poppler
def test_it_is_one_decision_however_many_formats(tmp_path: Path) -> None:
    conn, settings = dune(tmp_path)
    row = finding_row(conn)

    plan_id = resolve(
        conn, settings, int(row["id"]), ["Books/Frank Herbert/Dune/Dune.epub"]
    )

    ops = conn.execute(
        "SELECT op_type, src_relpath FROM plan_ops WHERE plan_id=?", (plan_id,)
    ).fetchall()
    assert [op["op_type"] for op in ops] == ["quarantine"]
    assert ops[0]["src_relpath"] == "Books/Frank Herbert/Dune/Dune.pdf"


# --- 18-20: keeping one, or both -----------------------------------------------


@poppler
@pytest.mark.parametrize("keep", ["Dune.epub", "Dune.pdf"])
def test_either_format_can_be_the_one_that_stays(tmp_path: Path, keep: str) -> None:
    conn, settings = dune(tmp_path)
    row = finding_row(conn)
    kept = f"Books/Frank Herbert/Dune/{keep}"

    plan_id = resolve(conn, settings, int(row["id"]), [kept])
    execute_plan(conn, plan_id, settings)

    assert (settings.library_dir / kept).is_file()
    gone = [
        path
        for path in (settings.library_dir / "Books/Frank Herbert/Dune").iterdir()
    ]
    assert [path.name for path in gone] == [keep]


@poppler
def test_keeping_both_formats_makes_no_plan(tmp_path: Path) -> None:
    """There is no filesystem work in leaving things as they are."""
    conn, settings = dune(tmp_path)
    row = finding_row(conn)
    view = compare(conn, settings, row)

    plan_id = resolve(
        conn, settings, int(row["id"]), [m.relpath for m in view.members]
    )

    assert plan_id == ""
    assert conn.execute("SELECT COUNT(*) c FROM plans").fetchone()["c"] == 0
    for member in view.members:
        assert (settings.library_dir / member.relpath).is_file()


@poppler
def test_setting_aside_every_format_is_refused(tmp_path: Path) -> None:
    conn, settings = dune(tmp_path)
    row = finding_row(conn)

    with pytest.raises(CorrectionRefused):
        resolve(conn, settings, int(row["id"]), [])


@poppler
def test_undo_puts_the_other_format_back(tmp_path: Path) -> None:
    conn, settings = dune(tmp_path)
    row = finding_row(conn)
    plan_id = resolve(
        conn, settings, int(row["id"]), ["Books/Frank Herbert/Dune/Dune.epub"]
    )
    execute_plan(conn, plan_id, settings)

    undo_correction(conn, settings, plan_id)

    assert (settings.library_dir / "Books/Frank Herbert/Dune/Dune.pdf").is_file()
    assert (settings.library_dir / "Books/Frank Herbert/Dune/Dune.epub").is_file()


# --- 22-27: everything this must NOT group -------------------------------------


@poppler
def test_two_different_isbns_are_two_works(tmp_path: Path) -> None:
    """A second edition carries a second ISBN, which is the point of ISBNs."""
    conn, settings = library(tmp_path)
    epub(settings, "Books/A/One/One.epub", title="One", author="A",
         identifier="urn:isbn:9780441013593")
    epub(settings, "Books/A/Two/Two.epub", title="One", author="A",
         identifier="urn:isbn:9780316769488")
    analysed(conn, settings)

    assert detect(conn) == []


@poppler
def test_two_dois_are_two_papers(tmp_path: Path) -> None:
    conn, settings = library(tmp_path)
    write(settings, "Documents/Papers/A/preprint.pdf",
          build_pdf(title="A Result", lines=("Abstract", "doi:10.1000/preprint1")))
    write(settings, "Documents/Papers/A/published.pdf",
          build_pdf(title="A Result", lines=("Abstract", "doi:10.1000/published9")))
    analysed(conn, settings)

    assert detect(conn) == []


@poppler
def test_one_doi_in_two_containers_is_one_paper(tmp_path: Path) -> None:
    conn, settings = library(tmp_path)
    write(settings, "Documents/Papers/A/paper.pdf",
          build_pdf(title="A Result", lines=("Abstract", "doi:10.1000/xyz123")))
    epub(settings, "Documents/Papers/A/paper.epub", title="A Result", author="A",
         identifier="doi:10.1000/xyz123")
    analysed(conn, settings)

    findings = detect(conn)

    assert len(findings) == 1
    assert "10.1000/xyz123" in " ".join(
        str(entry.detail) for entry in findings[0].evidence
    )


@poppler
def test_the_2023_and_2024_manuals_stay_separate(tmp_path: Path) -> None:
    """Related documents, not duplicates. Neither has an identifier, so nothing
    groups them — and nothing here compares titles, which is what would."""
    conn, settings = library(tmp_path)
    for year in (2023, 2024):
        write(
            settings,
            f"Documents/Manuals/Honda/{year} CR-V Owner's Manual.pdf",
            build_pdf(
                title=f"{year} CR-V Owner's Manual",
                author="Honda Motor Co.",
                lines=(f"{year} CR-V Owner's Manual", "Read before operating."),
            ),
        )
    analysed(conn, settings)

    assert detect(conn) == []


@poppler
def test_monthly_statements_are_never_collapsed(tmp_path: Path) -> None:
    """One bank, one template, twelve documents. The identical half is the
    part that is not the document."""
    conn, settings = library(tmp_path)
    for month in ("March", "April", "May"):
        write(
            settings,
            f"Documents/Financial/2024/Account Statement {month} 2024.pdf",
            build_pdf(
                title=f"Account Statement {month} 2024",
                lines=("Account statement", f"Statement period: 1 {month} to 30 {month}"),
            ),
        )
    analysed(conn, settings)

    assert detect(conn) == []


@poppler
def test_two_copies_of_one_format_are_not_a_format_question(tmp_path: Path) -> None:
    """Two PDFs of one ISBN is a re-download or a second scan, and which of
    those to keep is the duplicate and similar workflows' question."""
    conn, settings = library(tmp_path)
    for name in ("Dune.pdf", "Dune (1).pdf"):
        write(settings, f"Books/Frank Herbert/Dune/{name}",
              build_pdf(title="Dune", lines=("Dune", "ISBN 978-0-441-01359-3"),
                        pages=1 if name == "Dune.pdf" else 2))
    analysed(conn, settings)

    assert detect(conn) == []


# --- 15, 28-29: the boundaries with the other two workflows --------------------


@poppler
def test_byte_identical_files_stay_with_the_exact_workflow(tmp_path: Path) -> None:
    conn, settings = library(tmp_path)
    body = build_pdf(title="Dune", lines=("Dune", "ISBN 978-0-441-01359-3"))
    write(settings, "Books/Frank Herbert/Dune/Dune.pdf", body)
    write(settings, "Books/Frank Herbert/Dune/copy.pdf", body)
    analysed(conn, settings)

    assert detect(conn) == []


@poppler
def test_the_quarantine_sentence_never_says_duplicate(tmp_path: Path) -> None:
    from librairy.document_works import describe

    conn, settings = dune(tmp_path)
    item = conn.execute(
        "SELECT id FROM items WHERE relpath='Books/Frank Herbert/Dune/Dune.pdf'"
    ).fetchone()

    said = describe(conn, int(item["id"]))

    assert said == "Same ISBN, different file format."
    assert "duplicate" not in said.lower()


@poppler
def test_a_changed_file_blocks_the_decision(tmp_path: Path) -> None:
    conn, settings = dune(tmp_path)
    row = finding_row(conn)
    plan_id = resolve(
        conn, settings, int(row["id"]), ["Books/Frank Herbert/Dune/Dune.epub"]
    )

    (settings.library_dir / "Books/Frank Herbert/Dune/Dune.pdf").write_bytes(b"redownloaded")
    summary = execute_plan(conn, plan_id, settings)

    assert summary.done == 0
    assert (settings.library_dir / "Books/Frank Herbert/Dune/Dune.pdf").is_file()


@poppler
def test_a_document_re_read_since_it_was_cached_drops_out(tmp_path: Path) -> None:
    """Cached identity describes the bytes it was read from. New bytes are a
    document nobody has read, and it takes part in nothing until they are."""
    conn, settings = dune(tmp_path)
    assert len(detect(conn)) == 1

    (settings.library_dir / "Books/Frank Herbert/Dune/Dune.pdf").write_bytes(
        build_pdf(title="Dune", lines=("Dune",), pages=9)
    )
    scan_root(conn, "library", settings.library_dir, settings)

    assert detect(conn) == []


@poppler
def test_a_work_comparison_is_a_choice_and_never_bulk_approved(tmp_path: Path) -> None:
    from librairy.web.actionability import CHOICE
    from librairy.web.review import audit_view

    conn, settings = dune(tmp_path)
    finding_row(conn)

    groups = audit_view(conn, settings)
    found = next(
        row
        for group in groups["audit_groups"]
        for row in group["findings"]
        if row["kind"] == KIND
    )

    assert found["status_kind"] == CHOICE
    assert found["can_approve"] is False


@poppler
def test_keeping_both_stops_the_audit_asking_again(tmp_path: Path) -> None:
    conn, settings = dune(tmp_path)
    row = finding_row(conn)
    view = compare(conn, settings, row)

    resolve(conn, settings, int(row["id"]), [m.relpath for m in view.members])

    assert detect(conn) == []


@poppler
def test_replacing_one_format_makes_the_question_live_again(tmp_path: Path) -> None:
    """The answer was about those two files. A better scan is a comparison
    nobody has been asked about — suppressing by title would have hidden it."""
    conn, settings = dune(tmp_path)
    row = finding_row(conn)
    view = compare(conn, settings, row)
    resolve(conn, settings, int(row["id"]), [m.relpath for m in view.members])
    assert detect(conn) == []

    write(settings, "Books/Frank Herbert/Dune/Dune.pdf",
          build_pdf(title="Dune", author="Frank Herbert",
                    lines=("Dune", "ISBN 978-0-441-01359-3"), pages=500))
    analysed(conn, settings)

    assert len(detect(conn)) == 1


@poppler
def test_the_quarantine_row_does_not_say_you_did_not_want_it(tmp_path: Path) -> None:
    """It said exactly that, over a book whose format somebody chose. The
    stored reason column cannot hold this case, so it is read off the plan's
    finding — the same way a preserved original is."""
    from librairy.web.quarantine import quarantine_data

    conn, settings = dune(tmp_path)
    row = finding_row(conn)
    plan_id = resolve(
        conn, settings, int(row["id"]), ["Books/Frank Herbert/Dune/Dune.epub"]
    )
    execute_plan(conn, plan_id, settings)

    entries = quarantine_data(conn, settings)["entries"]
    entry = next(row for row in entries if row["display_name"] == "Dune.pdf")

    assert entry["display_name"] == "Dune.pdf"
    assert entry["reason_text"] == "another format of a document you kept"
    assert entry["reason_tag"] == "other format"
    assert entry["work_note"] == "Same ISBN, different file format."
    assert "did not want" not in entry["reason_text"]
    assert "duplicate" not in entry["reason_text"]


@poppler
def test_the_documents_audit_stage_reads_and_groups(tmp_path: Path) -> None:
    """Filing a document creates a new item row, so a book identified in the
    inbox arrives in the library unmeasured. The audit is the pass that reads
    what is filed — without it two formats sat side by side and nothing could
    say so."""
    from librairy.audit_stages import Context, run_stage

    conn, settings = library(tmp_path)
    epub(settings, "Books/Frank Herbert/Dune/Dune.epub",
         title="Dune", author="Frank Herbert", identifier=DUNE_ISBN)
    write(settings, "Books/Frank Herbert/Dune/Dune.pdf",
          build_pdf(title="Dune", lines=("Dune", "ISBN 978-0-441-01359-3"), pages=412))
    scan_root(conn, "library", settings.library_dir, settings)

    assert detect(conn) == [], "nothing has been read yet"

    from librairy.audit_job import Counters

    context = Context(
        conn=conn, settings=settings, scope="", counters=Counters(),
        deadline=float("inf"), now=lambda: 0.0, cancelled=lambda: False,
    )
    assert run_stage("documents", context) is True

    assert len(context.findings) == 1
    assert context.findings[0].kind == KIND
