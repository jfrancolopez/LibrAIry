"""Reading an organization off a document, without a list of organizations.

The roadmap carried this from M2-06: `document_set` groups "the same kind of
document from the same organization", and nothing read an organization off a
financial document, so the example the roadmap itself used — a year of bank
statements — did not group.

What was happening was worse than not grouping. The first line of a statement is
the bank's name, `docmeta` takes the first heading as the document's title, and
so twelve statements were all called `NORTHCREST BANK, N.A.pdf` and all wanted
one path. The heading was read correctly and understood as the wrong thing.

**No institution is named in `librairy/issuer.py`, and these fixtures are
invented on purpose.** A test suite that proved LibrAIry could recognise the
five banks somebody thought of would prove nothing about the sixth. What is
general is how an organization writes its own name on its own paperwork: a legal
form, a web address, a metadata field. Every name below is made up, and two of
the three institutions are shaped differently from each other.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from librairy import issuer
from librairy.classify import analyze_items
from librairy.config import Settings
from librairy.db import connect
from librairy.docmeta import FINANCIAL, MANUAL, facts_for
from librairy.scanner import scan_root
from tests.support.documents import build_pdf

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
    for directory in (
        settings.appdata_dir, settings.inbox_dir,
        settings.library_dir, settings.quarantine_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    return settings


#  Three issuers, deliberately unlike one another: one that names itself on a
#  letterhead and repeats itself in a web address, one whose identity is in the
#  PDF metadata as well, and one that is not a bank at all.
NORTHCREST = (
    "NORTHCREST BANK, N.A.",
    "PO Box 1184, Springfield",
    "www.northcrestbank.com",
    "",
    "Account Statement",
)
DELTA = (
    "Banco Delta S.A.",
    "Avenida Central 100",
    "bancodelta.example",
    "",
    "Account Statement",
)
STUDIO = (
    "Rivet & Stone Studio LLC",
    "hello@rivetstone.example",
    "",
    "Tax Invoice",
    "Invoice number 2026-118",
)


def statement(period: str, letterhead=NORTHCREST, **kwargs) -> bytes:  # noqa: ANN001, ANN003
    return build_pdf(
        pages=1,
        lines=(*letterhead, f"Statement period: {period}", "Opening balance 2,104.55"),
        **kwargs,
    )


# --- the extractor, on its own --------------------------------------------------------


def test_two_agreeing_sources_are_worth_more_than_either_alone() -> None:
    """A name off a letterhead, corroborated by a web address that contains a
    word from it. This is the only combination that reaches the threshold which
    files anything, and that is the whole confidence model."""
    found = issuer.read("\n".join(NORTHCREST))

    assert found.name == "Northcrest Bank, N.A."
    assert found.confidence == issuer.CORROBORATED
    assert found.sources == ("its letterhead", "its web address")
    assert found.contested is False


def test_a_letterhead_alone_is_a_suggestion() -> None:
    """Below what files a document, and said with one source rather than two."""
    found = issuer.read("Kestrel Mutual Limited\n\nAccount Statement\n")

    assert found.name == "Kestrel Mutual Limited"
    assert found.confidence == issuer.NAMED
    assert found.sources == ("its letterhead",)


def test_a_web_address_alone_is_weaker_still() -> None:
    """It is genuinely evidence — printed on the document by whoever sent it —
    and genuinely weak, because `brightwater.co.uk` gives a label rather than a
    name anybody writes. It informs a person; it does not choose a folder."""
    found = issuer.read("Statement\nQueries: www.brightwater.co.uk\n")

    assert found.name == "Brightwater"
    assert found.confidence == issuer.DOMAIN_ONLY
    assert found.confidence < 0.8


def test_two_sources_naming_different_organizations_is_a_question() -> None:
    """Never resolved by preferring one. A statement whose letterhead and whose
    metadata disagree about who wrote it is exactly the document a person should
    look at, and picking the letterhead would be right often enough to file
    somebody's statements under their accountant's name the time it was not."""
    found = issuer.read(
        "Northcrest Bank, N.A.\nAccount Statement\n",
        author="Hollis & Pike Accountants LLP",
    )

    assert found.contested is True
    assert found.sources == ("its letterhead", "the document's metadata")


def test_the_software_that_made_the_file_is_not_the_issuer() -> None:
    """A producer's own domain on a page says who rendered it, not who sent it."""
    found = issuer.read("Account Statement\nGenerated with www.adobe.com\n")

    assert found.name == ""


def test_a_shouted_letterhead_is_not_a_folder_name() -> None:
    """Letterheads are often set in capitals, and that is the document's
    typography rather than the organization's name — but `N.A.` and `LLC` are
    initials and stay that way, and `eBay` is somebody's actual styling."""
    assert issuer._tidy("NORTHCREST BANK, N.A.") == "Northcrest Bank, N.A."
    assert issuer._tidy("RIVET & STONE STUDIO LLC") == "Rivet & Stone Studio LLC"
    assert issuer._tidy("eBay Inc.") == "eBay Inc."


def test_nothing_is_read_from_a_document_that_says_nothing() -> None:
    assert issuer.read("Account Statement\nStatement period: 1 March 2026\n").name == ""


# --- through the document reader ------------------------------------------------------


@poppler
def test_a_statement_says_who_issued_it_and_what_kind_it_is(tmp_path: Path) -> None:
    """The two facts the roadmap asked for, off a document with neither in its
    filename and neither in its metadata."""
    settings = settings_for(tmp_path)
    path = tmp_path / "scan-0473.pdf"
    path.write_bytes(statement("1 March 2026 to 31 March 2026"))

    facts = facts_for(path, settings)

    assert facts.kind == FINANCIAL
    assert facts.organization == "Northcrest Bank, N.A."
    assert facts.label == "Bank statement"
    assert ("who issued it", "Northcrest Bank, N.A.") in facts.sources


@poppler
def test_the_issuers_name_is_not_taken_as_the_documents_title(tmp_path: Path) -> None:
    """The defect that made this worth doing.

    A statement's first heading is its bank. Read as a title it becomes the
    filename, so every statement from one bank collides on one path — and the
    fix is not to read the heading differently but to know what it is.
    """
    settings = settings_for(tmp_path)
    path = tmp_path / "scan-0473.pdf"
    path.write_bytes(statement("1 March 2026 to 31 March 2026"))

    facts = facts_for(path, settings)

    assert facts.title == ""
    assert facts.content_title == ""
    assert ("first page", "NORTHCREST BANK, N.A.") not in facts.sources


@poppler
def test_a_manual_is_unaffected(tmp_path: Path) -> None:
    """The control. A manual's manufacturer already came off the Author field,
    its title is a real title, and nothing here may touch either."""
    settings = settings_for(tmp_path)
    path = tmp_path / "ax3000.pdf"
    path.write_bytes(
        build_pdf(
            pages=1,
            title="AX3000 Owner's Manual",
            author="Vellum Networks Ltd",
            lines=("Vellum Networks Ltd", "AX3000 Owner's Manual", "Installation"),
        )
    )

    facts = facts_for(path, settings)

    assert facts.kind == MANUAL
    assert facts.title == "AX3000 Owner's Manual"
    assert facts.organization == ""


@poppler
def test_an_ordinary_document_is_not_given_an_issuer(tmp_path: Path) -> None:
    """The other control. A letter mentioning a company is not a statement from
    it, and the extractor only ever runs where the kind says it is the question."""
    settings = settings_for(tmp_path)
    path = tmp_path / "letter.pdf"
    path.write_bytes(
        build_pdf(
            pages=1,
            title="A note about the garden",
            lines=(
                "Dear Marta",
                "I finally called Rivet & Stone Studio LLC about the wall.",
                "They were very helpful.",
            ),
        )
    )

    facts = facts_for(path, settings)

    assert facts.kind != FINANCIAL
    assert facts.organization == ""


# --- all the way through --------------------------------------------------------------


@poppler
def test_a_year_of_statements_files_under_its_bank_and_groups(tmp_path: Path) -> None:
    """The roadmap's own example, end to end and through the real pipeline.

    Two institutions, because one would prove only that a fixture matched
    itself. The statements from each land under that institution, keep their own
    filenames, and the two from one bank become one decision through the
    grouping machinery that already existed.
    """
    settings = settings_for(tmp_path)
    for month, period in (
        ("03", "1 March 2026 to 31 March 2026"),
        ("04", "1 April 2026 to 30 April 2026"),
    ):
        (settings.inbox_dir / f"northcrest-2026-{month}.pdf").write_bytes(
            statement(period)
        )
    (settings.inbox_dir / "delta-q1.pdf").write_bytes(
        statement("1 January 2026 to 31 March 2026", DELTA, author="Banco Delta S.A.")
    )
    (settings.inbox_dir / "studio-118.pdf").write_bytes(
        build_pdf(pages=1, lines=(*STUDIO, "Amount due: 1,240.00"))
    )
    conn = connect(settings)
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    analyze_items(conn, settings)

    filed = {
        str(row["relpath"]): str(row["dest_relpath"] or "")
        for row in conn.execute(
            "SELECT i.relpath, p.dest_relpath FROM proposals p"
            " JOIN items i ON i.id = p.item_id"
        )
    }
    assert filed["northcrest-2026-03.pdf"].startswith(
        "Documents/Financial/Northcrest Bank, N.A"
    )
    assert filed["northcrest-2026-04.pdf"].startswith(
        "Documents/Financial/Northcrest Bank, N.A"
    )
    assert filed["delta-q1.pdf"].startswith("Documents/Financial/Banco Delta S.A")
    assert filed["studio-118.pdf"].startswith(
        "Documents/Financial/Rivet & Stone Studio LLC"
    )
    #  Two statements from one bank, one decision. Two banks, two decisions —
    #  never one heading over both.
    assert filed["northcrest-2026-03.pdf"] != filed["northcrest-2026-04.pdf"]
    labels = sorted(str(row["label"]) for row in conn.execute("SELECT label FROM groups"))
    assert labels == ["Bank statements from Northcrest Bank, N.A."]


@poppler
def test_a_weakly_read_issuer_does_not_choose_a_folder(tmp_path: Path) -> None:
    """Weak extraction does not become fact.

    A web address and nothing else identifies the sender well enough to be worth
    showing and not well enough to file on. The document goes to Review with the
    evidence on it rather than into a folder named after a domain label.
    """
    settings = settings_for(tmp_path)
    (settings.inbox_dir / "statement.pdf").write_bytes(
        build_pdf(
            pages=1,
            lines=(
                "Account Statement",
                "Statement period: 1 March 2026 to 31 March 2026",
                "Questions? www.brightwater.example",
            ),
        )
    )
    conn = connect(settings)
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    analyze_items(conn, settings)

    row = conn.execute(
        "SELECT dest_relpath, confidence FROM proposals"
    ).fetchone()
    assert "Brightwater" not in str(row["dest_relpath"] or "")


@poppler
def test_the_evidence_says_what_read_the_organization(tmp_path: Path) -> None:
    """Provenance, because an extracted name is only reviewable if a person can
    see what said so."""
    settings = settings_for(tmp_path)
    (settings.inbox_dir / "scan-0473.pdf").write_bytes(
        statement("1 March 2026 to 31 March 2026")
    )
    conn = connect(settings)
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    analyze_items(conn, settings)

    evidence = str(conn.execute("SELECT evidence FROM proposals").fetchone()["evidence"])
    assert "Northcrest Bank, N.A." in evidence
    assert "its letterhead" in evidence
    assert "its web address" in evidence
    assert "Bank statement" in evidence
