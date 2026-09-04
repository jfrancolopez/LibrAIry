"""Documents that are one decision, and the many more that are not.

M1-03 built a group face for every medium and found that documents had no
groups to put in it. The face was never the hard part. The hard part is that a
heading is trusted: eleven unrelated PDFs under one of them, with one Approve
button underneath, is the most expensive mistake this program could make — and
document grouping is the easiest place in it to make that mistake.

So most of these tests are about **not** grouping. The rule earns its keep by
what it refuses: an arrival, a category, a shared tag over unrelated things, a
document whose own sources disagree about what it is.

Real files throughout — `tests/support/documents.py` builds valid PDFs and
EPUBs — and a real analysis pass, because every interesting case here was
visible only end to end. The first version of this rule put a boiler manual and
a novel under one heading, and it did that through a fixture that looked
perfectly reasonable in isolation.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from librairy import document_groups
from librairy.classify import analyze_items
from librairy.config import Settings
from librairy.db import connect
from librairy.document_groups import SERIES, SET, TAGGED, candidate, split_volume
from librairy.models import EvidenceEntry
from librairy.scanner import scan_root
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


def analysed(tmp_path: Path, build) -> tuple[sqlite3.Connection, Settings]:  # noqa: ANN001
    """Files somebody built, through the real analysis pass."""
    settings = settings_for(tmp_path)
    build(settings)
    conn = connect(settings)
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    analyze_items(conn, settings)
    return conn, settings


def manual(settings: Settings, name: str, title: str, maker: str) -> None:
    path = settings.inbox_dir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        build_pdf(
            title=title,
            author=maker,
            #  The title on the cover, as a real manual has it. A first page
            #  whose first line is the company name makes the *content* title
            #  the company, which reads as a disagreement — a fixture artifact
            #  that cost an hour the first time.
            lines=(title, maker, "Read this before operating."),
            pages=2,
        )
    )


def groups(conn: sqlite3.Connection) -> list[tuple[str, str, int]]:
    return [
        (str(row["kind"]), str(row["label"]), int(row["members"]))
        for row in conn.execute(
            """
            SELECT g.kind, g.label, COUNT(p.id) AS members
            FROM groups g JOIN proposals p ON p.group_id = g.id
            GROUP BY g.id ORDER BY g.label
            """
        )
    ]


def group_of(conn: sqlite3.Connection, relpath: str) -> int | None:
    row = conn.execute(
        "SELECT p.group_id FROM proposals p JOIN items i ON i.id = p.item_id"
        " WHERE i.relpath = ?",
        (relpath,),
    ).fetchone()
    return None if row is None or row["group_id"] is None else int(row["group_id"])


# --- what earns a group ------------------------------------------------------------


def test_two_editions_of_one_title_are_one_decision(tmp_path: Path) -> None:
    """The case the roadmap named. An explicit edition marker over one stem."""

    def build(settings: Settings) -> None:
        write_epub(
            settings.inbox_dir / "books/pr1.epub",
            title="Programming Rust, 1st Edition",
            author="Jim Blandy",
        )
        write_epub(
            settings.inbox_dir / "books/pr2.epub",
            title="Programming Rust, 2nd Edition",
            author="Jim Blandy",
        )

    conn, _ = analysed(tmp_path, build)

    assert groups(conn) == [(SERIES, "Programming Rust", 2)]
    assert group_of(conn, "books/pr1.epub") == group_of(conn, "books/pr2.epub")


def test_a_series_does_not_swallow_an_unrelated_book(tmp_path: Path) -> None:
    def build(settings: Settings) -> None:
        write_epub(
            settings.inbox_dir / "books/e1.epub",
            title="Earthsea Book 1",
            author="Ursula K. Le Guin",
        )
        write_epub(
            settings.inbox_dir / "books/e2.epub",
            title="Earthsea Book 2",
            author="Ursula K. Le Guin",
        )
        write_epub(
            settings.inbox_dir / "books/dune.epub",
            title="Dune",
            author="Frank Herbert",
        )

    conn, _ = analysed(tmp_path, build)

    assert groups(conn) == [(SERIES, "Earthsea", 2)]
    assert group_of(conn, "books/dune.epub") is None


@poppler
def test_two_manuals_from_one_manufacturer_are_one_decision(tmp_path: Path) -> None:
    def build(settings: Settings) -> None:
        manual(settings, "scans/cb500.pdf", "Honda CB500 Owner's Manual", "Honda Motor Co.")
        manual(settings, "scans/cb750.pdf", "Honda CB750 Owner's Manual", "Honda Motor Co.")

    conn, _ = analysed(tmp_path, build)

    assert groups(conn) == [(SET, "Manuals from Honda Motor Co.", 2)]


@poppler
def test_one_manual_from_each_of_two_makers_is_two_rows(tmp_path: Path) -> None:
    """The "200 unrelated invoices" case, in the shape this fixture can build.

    Both are manuals. Neither has anybody else to be a set with, and "documents
    of the same type" is a category, not a relationship.
    """

    def build(settings: Settings) -> None:
        manual(settings, "scans/cb500.pdf", "Honda CB500 Owner's Manual", "Honda Motor Co.")
        manual(settings, "scans/r7000.pdf", "Netgear R7000 User Manual", "NETGEAR Inc.")

    conn, _ = analysed(tmp_path, build)

    assert groups(conn) == []


# --- what does not ------------------------------------------------------------------


def test_arriving_in_one_folder_is_not_a_relationship(tmp_path: Path) -> None:
    """Every PDF imported on Tuesday. The tempting one, and the wrong one.

    Files dropped in together are related more often than not, and "more often
    than not" is the standard that writes a wrong heading.
    """

    def build(settings: Settings) -> None:
        for name, title in (
            ("notes.epub", "Field Notes"),
            ("atlas.epub", "Road Atlas"),
            ("recipes.epub", "家常菜"),
        ):
            write_epub(settings.inbox_dir / "Tuesday" / name, title=title, author="Nobody")

    conn, _ = analysed(tmp_path, build)

    assert groups(conn) == []


def test_one_tag_over_unrelated_kinds_of_document_is_not_one_group(
    tmp_path: Path,
) -> None:
    """A Project holds invoices, photographs, manuals and permits.

    That is what a project *is*, and grouping on the tag alone would put all
    four under one heading with one Approve button — which M2-05's own brief
    named as the thing not to do. The type has to agree as well, and it has to
    say more than the category already did: two epubs are both "Book" whatever
    is in them, so `Books tagged #ProjectHouse` is not a set.
    """

    def build(settings: Settings) -> None:
        write_epub(
            settings.inbox_dir / "#ProjectHouse/boiler.epub",
            title="Boiler Owners Manual",
            author="Vaillant",
        )
        write_epub(
            settings.inbox_dir / "#ProjectHouse/novel.epub",
            title="A Novel",
            author="Someone Else",
        )

    conn, _ = analysed(tmp_path, build)

    assert groups(conn) == []


def test_a_document_whose_sources_disagree_is_not_folded_into_a_set() -> None:
    """Conflicted identity prevents grouping rather than disappearing inside one.

    What this document *is* is the open question in front of the reader, and a
    group heading is where a question goes to stop being noticed.
    """
    contested = [
        EvidenceEntry("document", "title/embedded", "CRACKING", 0.9, note="disagrees"),
        EvidenceEntry("document", "title/content", "Programming Rust", 0.9, note="chosen"),
        EvidenceEntry("document", "type", "Manual", 0.85),
        EvidenceEntry("document", "organization", "No Starch Press", 0.85),
    ]

    assert candidate("documents", contested) is None


def test_only_the_filename_disagreeing_still_groups() -> None:
    """And the other half of that, which is what makes the rule usable.

    `pr2.epub` whose metadata says `Programming Rust, 2nd Edition` is contested
    — the filename dissents, and M2-02 is right to ask about it, because that
    is how `CRACKING.pdf` was caught. But an abbreviated filename is the
    ordinary case for an ebook, and treating it as a real conflict made every
    set of them ungroupable for the reason that says least about the files.
    """
    abbreviated = [
        EvidenceEntry("document", "title/embedded", "Dune Book 2", 0.9, note="chosen"),
        EvidenceEntry("document", "title/filename", "dune2", 0.6, note="disagrees"),
        EvidenceEntry("document", "type", "Book", 0.85),
    ]

    found = candidate("books", abbreviated)

    assert found is not None
    assert found.kind == SERIES
    assert found.label == "Dune"


def test_a_shared_identifier_is_never_a_set(tmp_path: Path) -> None:
    """One work in two containers is a comparison, not a decision group.

    `document_works` already owns that question, and it owns it with a better
    answer: keeping both an EPUB and a PDF is a first-class outcome there.
    Turning the pair into a "group" would quietly reframe a keep-both question
    as an approve-once one.
    """

    def build(settings: Settings) -> None:
        write_epub(
            settings.inbox_dir / "books/dune.epub",
            title="Dune",
            author="Frank Herbert",
            identifier="urn:isbn:9780441013593",
        )
        (settings.inbox_dir / "books/dune.pdf").write_bytes(
            build_pdf(
                title="Dune",
                author="Frank Herbert",
                lines=("Dune", "Frank Herbert", "ISBN 978-0-441-01359-3"),
            )
        )

    conn, _ = analysed(tmp_path, build)

    assert groups(conn) == []


def test_no_key_is_built_from_an_identifier() -> None:
    """The rule above, where it is enforced rather than only where it shows.

    Structural rather than a special case: no branch of `candidate` reads an
    ISBN or a DOI, so two files carrying the same one have nothing to share
    here however much else they agree about.
    """
    same_book = [
        EvidenceEntry("document", "title/embedded", "Dune", 0.9, note="chosen"),
        EvidenceEntry("document", "isbn", "9780441013593", 0.95),
        EvidenceEntry("document", "type", "Book", 0.85),
    ]

    assert candidate("books", same_book) is None


@poppler
def test_coherent_documents_under_one_tag_are_one_decision(tmp_path: Path) -> None:
    """The half of the tag rule that says yes.

    Two invoices for the renovation, both tagged, both the same kind of thing.
    The tag is the owner saying these belong together and the type is what
    keeps it from meaning "everything in the project".
    """

    def build(settings: Settings) -> None:
        for name, who in (("roof.pdf", "Roofing Ltd"), ("wiring.pdf", "Sparks Ltd")):
            path = settings.inbox_dir / "#ProjectHouse" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(
                build_pdf(
                    title=f"{who} Invoice",
                    author=who,
                    lines=(f"{who} Invoice", "Invoice number 4471", "Amount due 1,200"),
                )
            )

    conn, _ = analysed(tmp_path, build)

    assert groups(conn) == [(TAGGED, "Financial documents tagged #ProjectHouse", 2)]


def test_nothing_outside_documents_and_books_is_looked_at() -> None:
    """Every other medium already has a grouping rule that works."""
    facts = [
        EvidenceEntry("document", "title/embedded", "Greatest Hits Vol. 2", 0.9, note="chosen"),
        EvidenceEntry("document", "type", "Book", 0.85),
    ]

    assert candidate("music", facts) is None
    assert candidate("photos", facts) is None


# --- the stem rule ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "stem"),
    [
        ("Programming Rust, 2nd Edition", "Programming Rust"),
        ("Dune Book 2", "Dune"),
        ("The Art of Computer Programming Volume 3", "The Art of Computer Programming"),
        ("Foundation, Part Two", "Foundation"),
        #  A marker over a word that names nothing. Two files called Manual are
        #  two manuals, and the stem would gather the whole shelf.
        ("Manual Vol. 2", ""),
        ("Volume 4", ""),
        #  No explicit marker at all. `Report 2024` and `Report 2025` are a set
        #  at best, and a trailing number is not a volume.
        ("Annual Report 2024", ""),
        ("Attention Is All You Need", ""),
    ],
)
def test_a_volume_marker_is_explicit_or_there_is_no_series(title: str, stem: str) -> None:
    assert split_volume(title)[0] == stem


# --- the group, as a decision -------------------------------------------------------


@poppler
def test_the_member_going_somewhere_else_is_split_out(tmp_path: Path) -> None:
    """M1-04's outlier machinery, reused rather than reimplemented.

    `groups.dest_base` is the folder most of the set is going to, so a member
    whose destination is not under it is not a doubt about that file — it is a
    statement that it belongs somewhere else. That the base is a *majority* and
    not a unanimity is the whole of why this works: letting one dissenting
    member erase the base would switch off the split in the one case it is for.
    """

    def build(settings: Settings) -> None:
        for name, title in (
            ("scans/cb500.pdf", "Honda CB500 Owner's Manual"),
            ("scans/cb750.pdf", "Honda CB750 Owner's Manual"),
            ("scans/crv.pdf", "Honda CR-V Owner's Manual"),
        ):
            manual(settings, name, title, "Honda Motor Co.")

    conn, _ = analysed(tmp_path, build)
    group_id = group_of(conn, "scans/cb500.pdf")
    assert group_id is not None

    base = conn.execute(
        "SELECT dest_base FROM groups WHERE id=?", (group_id,)
    ).fetchone()["dest_base"]
    #  Two of the three keep the group's folder; one is sent elsewhere by hand,
    #  which is what a person disagreeing with a proposal looks like.
    conn.execute(
        "UPDATE proposals SET dest_relpath='Documents/Elsewhere/odd.pdf'"
        " WHERE item_id = (SELECT id FROM items WHERE relpath='scans/crv.pdf')"
    )

    from librairy.web.review import ReviewFilters, grouped_page

    sections, _ = grouped_page(conn, ReviewFilters(category="documents"))
    odd = [section for section in sections if section["outlier"]]

    assert base, "the group needs a base for a split to mean anything"
    assert len(odd) == 1, "the member going elsewhere was not split out"
    assert odd[0]["total"] == 1


def test_a_group_says_what_makes_it_one_decision(tmp_path: Path) -> None:
    """The acceptance, in one assertion: every document group can say why.

    Not a kind a reader has to decode — a sentence, written when the reason was
    known, and shown under the heading with the Approve button beneath it.
    """

    def build(settings: Settings) -> None:
        for name, title in (
            ("books/e1.epub", "Earthsea Book 1"),
            ("books/e2.epub", "Earthsea Book 2"),
        ):
            write_epub(settings.inbox_dir / name, title=title, author="Ursula K. Le Guin")

    conn, _ = analysed(tmp_path, build)

    reason = conn.execute("SELECT reason FROM groups").fetchone()["reason"]

    assert reason.startswith("one title in several parts or editions")
    #  Arrival is corroboration on a reason that already stood up, never a
    #  reason of its own — `test_arriving_in_one_folder_is_not_a_relationship`
    #  is the other half of this.
    assert "they all arrived in books" in reason


def test_arrival_can_strengthen_a_reason_and_never_write_one(tmp_path: Path) -> None:
    def build(settings: Settings) -> None:
        write_epub(
            settings.inbox_dir / "one/e1.epub", title="Earthsea Book 1", author="Le Guin"
        )
        write_epub(
            settings.inbox_dir / "two/e2.epub", title="Earthsea Book 2", author="Le Guin"
        )

    conn, _ = analysed(tmp_path, build)

    reason = conn.execute("SELECT reason FROM groups").fetchone()["reason"]

    assert groups(conn) == [(SERIES, "Earthsea", 2)], "the series is still a series"
    assert "arrived" not in reason, "two folders is not one arrival"


def test_a_settled_document_does_not_make_a_group_out_of_a_new_one(
    tmp_path: Path,
) -> None:
    """A group is work somebody still has to do.

    One live document joining eight already-filed ones is not a group of nine.
    It is one document, and a heading over a set of files that are already
    where they belong is furniture.
    """

    def build(settings: Settings) -> None:
        write_epub(
            settings.inbox_dir / "books/e1.epub", title="Earthsea Book 1", author="Le Guin"
        )
        write_epub(
            settings.inbox_dir / "books/e2.epub", title="Earthsea Book 2", author="Le Guin"
        )

    conn, _ = analysed(tmp_path, build)
    conn.execute("UPDATE proposals SET group_id = NULL")
    conn.execute(
        "UPDATE proposals SET status='committed' WHERE item_id ="
        " (SELECT id FROM items WHERE relpath='books/e1.epub')"
    )

    made = document_groups.group_documents(
        conn, [int(row["id"]) for row in conn.execute("SELECT id FROM items")]
    )

    assert made == 0
    assert group_of(conn, "books/e2.epub") is None


def test_a_large_set_renders_bounded(tmp_path: Path) -> None:
    """Ten thousand documents in one set is one bounded page, like everything else.

    The members come from the same paging every other group uses, so this is
    checking that documents did not arrive with an exception to it.
    """

    def build(settings: Settings) -> None:
        write_epub(
            settings.inbox_dir / "books/e1.epub", title="Earthsea Book 1", author="Le Guin"
        )
        write_epub(
            settings.inbox_dir / "books/e2.epub", title="Earthsea Book 2", author="Le Guin"
        )

    conn, _ = analysed(tmp_path, build)
    group_id = group_of(conn, "books/e1.epub")
    item_id = int(conn.execute("SELECT MAX(id) FROM items").fetchone()[0])
    for extra in range(1, 400):
        conn.execute(
            "INSERT INTO items(id, root, relpath, size, mtime_ns, state, first_seen_at,"
            " last_seen_at) VALUES (?, 'inbox', ?, 10, 0, 'proposed', 'now', 'now')",
            (item_id + extra, f"books/volume-{extra}.epub"),
        )
        conn.execute(
            "INSERT INTO proposals(item_id, category, clean_name, dest_relpath,"
            " confidence, action, dest_root, group_id, status, evidence, tier,"
            " created_at, updated_at) VALUES (?, 'books', ?, ?, 0.9, 'move',"
            " 'library', ?, 'proposed', '[]', 'confident', 'now', 'now')",
            (
                item_id + extra,
                f"volume-{extra}.epub",
                f"Books/Le Guin/Earthsea/volume-{extra}.epub",
                group_id,
            ),
        )

    from librairy.web.review import ReviewFilters, grouped_page

    sections, _ = grouped_page(conn, ReviewFilters(category="books"))
    section = next(part for part in sections if int(part["unit"][1:]) == group_id)

    assert section["total"] == 401
    assert len(section["rows"]) <= 25, "a 401-file set drew itself member by member"


def test_the_document_group_face_is_reachable(tmp_path: Path) -> None:
    """The whole of M1-03's PARTIAL, in one assertion.

    The grid was built a milestone ago and no workflow could reach it, which is
    why M1-03 has said PARTIAL ever since. A real group now renders in it.
    """
    from librairy.web.review import layout_for

    def build(settings: Settings) -> None:
        for name, title in (
            ("books/e1.epub", "Earthsea Book 1"),
            ("books/e2.epub", "Earthsea Book 2"),
        ):
            write_epub(settings.inbox_dir / name, title=title, author="Le Guin")

    conn, _ = analysed(tmp_path, build)

    from librairy.web.review import ReviewFilters, grouped_page

    sections, _ = grouped_page(conn, ReviewFilters(category="books"))

    assert layout_for("books") == "documents"
    assert [section["layout"] for section in sections] == ["documents"]
    assert Path(
        "src/librairy/web/templates/partials/members/documents.html"
    ).exists()


def test_turning_the_rule_off_leaves_the_rows_exactly_as_they_were(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other acceptance. Nothing here is load-bearing for a document row.

    A rule that could only be judged by its results would be a rule nobody
    could back out of. With `candidate` answering None the documents are the
    rows they have always been — no group, no heading, no key.
    """
    monkeypatch.setattr(document_groups, "candidate", lambda *_: None)

    def build(settings: Settings) -> None:
        for name, title in (
            ("books/e1.epub", "Earthsea Book 1"),
            ("books/e2.epub", "Earthsea Book 2"),
        ):
            write_epub(settings.inbox_dir / name, title=title, author="Le Guin")

    conn, _ = analysed(tmp_path, build)

    assert groups(conn) == []
    assert [
        row["group_key"] for row in conn.execute("SELECT group_key FROM proposals")
    ] == [None, None]


def test_a_set_that_grows_stays_one_group(tmp_path: Path) -> None:
    """Found by the M2 integration gate, and only visible across two passes.

    A document set's base is a *majority*, so it can move when the set grows.
    Finding the group by (kind, label, dest_base) — which is how every other
    kind of group is found — meant a set of two that became a set of five filed
    somewhere else created a **second** group under an identical heading. The
    eleven-PDFs problem wearing the opposite hat: two headings for one thing.
    """

    def build(settings: Settings) -> None:
        for index in (1, 2):
            write_epub(
                settings.inbox_dir / f"books/e{index}.epub",
                title=f"Earthsea Book {index}",
                author="Le Guin",
            )

    conn, settings = analysed(tmp_path, build)
    #  The first two are going one place; three more arrive going somewhere
    #  else, which moves the majority.
    conn.execute("UPDATE proposals SET dest_relpath='Books/Old/x.epub'")
    for index in (3, 4, 5):
        write_epub(
            settings.inbox_dir / f"books/e{index}.epub",
            title=f"Earthsea Book {index}",
            author="Le Guin",
        )
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    analyze_items(conn, settings)

    headings = groups(conn)

    assert len(headings) == 1, f"one set produced two headings: {headings}"
    assert headings[0][2] == 5


def test_a_held_file_is_not_a_member_of_anything(tmp_path: Path) -> None:
    """M2-01 and M2-06 meeting. A file nothing could answer has no answer to
    approve, so it belongs in the waiting list and not under a heading with an
    Approve button over it."""
    from librairy import waiting

    def build(settings: Settings) -> None:
        for index in (1, 2):
            write_epub(
                settings.inbox_dir / f"books/e{index}.epub",
                title=f"Earthsea Book {index}",
                author="Le Guin",
            )
        #  A PDF of filler bytes: nothing can read it, no provider is
        #  configured, so it is held rather than guessed at.
        (settings.inbox_dir / "books/mystery.pdf").write_bytes(
            b"%PDF-1.4\n" + b"x" * 2048
        )

    conn, _ = analysed(tmp_path, build)

    held = {int(row["item_id"]) for row in conn.execute("SELECT item_id FROM processing_waits")}

    assert held, "the fixture has to hold something for this to mean anything"
    assert waiting.reason_for is not None
    assert group_of(conn, "books/mystery.pdf") is None
    assert groups(conn) == [(SERIES, "Earthsea", 2)]


# --- the line against `document_works` ----------------------------------------------


def test_the_two_document_relationships_stay_two_things() -> None:
    """One work in several containers, and several works in one set.

    Written as a test rather than only a docstring because merging them is the
    plausible refactor: both are "documents that belong together", and they are
    different questions with different answers — keep both, versus file these
    once. See `librairy/document_works.py`.
    """
    import inspect

    from librairy import document_works

    assert document_works.KIND == "document-formats"
    assert document_works.KIND not in document_groups.KINDS
    source = inspect.getsource(document_groups)
    assert "isbn" not in source.split('"""', 2)[2].lower(), (
        "a shared identifier became a grouping key; that is document_works' question"
    )
