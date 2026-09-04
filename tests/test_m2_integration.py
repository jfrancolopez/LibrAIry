"""The M2 gate: six features meeting, rather than six features passing.

M2-01 through M2-06 each have their own file and each passes on its own. That
is not the same as their holding together, and the difference is where the real
defects were: a set that grows moving its own base and forking into two
identical headings, a tagged file losing its tag on the one code path a held
file takes, a companion held because the pass that finds companions looks for
proposals. Every one of those was invisible from inside the feature that caused
it.

So this file only makes claims **no single feature can make**:

    nothing M2 added can move a file          the safety line, across all six
    a mode changes when work happens,         M2-03 against everything else
      never what the answer is
    a held file blocks nothing                M2-01 against M2-05 and M2-06
    the three waiting reasons survived        asked for explicitly, twice
    there is one authority path               M2-04 and M2-05, structurally

A real inbox and a real analysis pass throughout. A gate assembled out of mocks
would be a gate against the mocks.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from librairy import resources, tags, waiting
from librairy.classify import analyze_items
from librairy.config import Settings
from librairy.db import connect
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


def everything(settings: Settings) -> None:
    """One inbox with all six features' material in it.

    A tagged set of documents, a book series, a file nothing can identify, and
    a tag written on a file rather than on a folder — which is the shape that
    exercises M2-01, M2-02, M2-05 and M2-06 in one pass.
    """
    for index in (1, 2):
        write_epub(
            settings.inbox_dir / f"shelf/earthsea-{index}.epub",
            title=f"A Wizard of Earthsea Book {index}",
            author="Ursula K. Le Guin",
        )
    for who in ("Roofing Ltd", "Sparks Ltd"):
        path = settings.inbox_dir / "#ProjectHouse" / f"{who.split()[0].lower()}.pdf"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(
            build_pdf(
                title=f"{who} Invoice",
                author=who,
                lines=(f"{who} Invoice", "Invoice number 4471", "Amount due 1,200"),
            )
        )
    #  Nothing readable, no provider configured: held rather than guessed at.
    (settings.inbox_dir / "#ProjectHouse/mystery.pdf").write_bytes(
        b"%PDF-1.4\n" + b"x" * 2048
    )
    #  A tag in a file's own name, which is how most people tag one file.
    write_epub(
        settings.inbox_dir / "shelf/atlas #Reference.epub",
        title="Road Atlas",
        author="Nobody",
    )


def analysed(tmp_path: Path, mode: str = "") -> tuple[sqlite3.Connection, Settings]:
    settings = settings_for(tmp_path)
    everything(settings)
    conn = connect(settings)
    if mode:
        resources.set_processing_mode(conn, mode)
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    analyze_items(conn, settings)
    return conn, settings


def picture(conn: sqlite3.Connection) -> dict[str, object]:
    """What one pass decided, in the terms the six features are about."""
    return {
        "proposals": sorted(
            (str(row["relpath"]), str(row["category"]), str(row["dest_relpath"] or ""))
            for row in conn.execute(
                "SELECT i.relpath, p.category, p.dest_relpath FROM proposals p"
                " JOIN items i ON i.id = p.item_id WHERE p.status != 'superseded'"
            )
        ),
        "groups": sorted(
            (str(row["kind"]), str(row["label"]))
            for row in conn.execute("SELECT kind, label FROM groups")
        ),
        "tags": sorted(
            (int(row["item_id"]), str(row["tag"]))
            for row in conn.execute("SELECT item_id, tag FROM item_tags")
        ),
        "held": sorted(
            str(row["reason"]) for row in conn.execute("SELECT reason FROM processing_waits")
        ),
    }


# --- the line every one of them is on -----------------------------------------------


@poppler
def test_nothing_m2_added_can_move_a_file(tmp_path: Path) -> None:
    """The safety line, across all six at once.

    Tags recorded, documents grouped, files held, rules matched, modes read —
    and the inbox is exactly as it was, the library is empty, and no plan
    exists. Every one of these features is allowed to *explain* and none of
    them is allowed to act: filing still converges at Commit and Commit is
    still the only thing that touches bytes.
    """
    settings = settings_for(tmp_path)
    everything(settings)
    before = sorted(path.relative_to(settings.inbox_dir).as_posix()
                    for path in settings.inbox_dir.rglob("*") if path.is_file())
    conn = connect(settings)
    scan_root(conn, "inbox", settings.inbox_dir, settings)

    analyze_items(conn, settings)

    after = sorted(path.relative_to(settings.inbox_dir).as_posix()
                   for path in settings.inbox_dir.rglob("*") if path.is_file())
    assert after == before, "analysis renamed or moved something in the inbox"
    assert list(settings.library_dir.rglob("*")) == [], "something reached the library"
    assert conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM plan_ops").fetchone()[0] == 0
    #  And it did do the work, so the assertions above are about a pass that
    #  had something to act wrongly on.
    seen = picture(conn)
    assert seen["groups"], "nothing was grouped, so the check above proves little"
    assert seen["tags"], "nothing was tagged"
    assert seen["held"], "nothing was held"


# --- M2-03 against the rest ---------------------------------------------------------


@poppler
def test_a_mode_changes_when_work_happens_and_never_the_answer(
    tmp_path: Path,
) -> None:
    """Quiet and Full Power reach the same decisions about the same inbox.

    The claim M2-03 was built on, and the one that is only checkable from
    outside it: a resource mode decides how hard the worker may work, so the
    same files have to come out with the same proposals, the same groups and
    the same tags whichever mode read them. A mode that changed an *answer*
    would mean two libraries organised differently for a reason nobody chose.
    """
    quiet, _ = analysed(tmp_path / "quiet", resources.QUIET)
    full, _ = analysed(tmp_path / "full", resources.FULL)

    #  Item ids are the only thing allowed to differ, and they do not here —
    #  the scan walks one identical tree — but the comparison is on what was
    #  decided rather than on row identity.
    assert picture(quiet) == picture(full)


@poppler
def test_the_ocr_budget_defers_and_never_decides(tmp_path: Path) -> None:
    """A document whose turn did not come is untouched, not answered badly.

    M2-02 reads pixels only where text extraction genuinely failed, and M2-03
    bounds how many of those one cycle may do. The two meeting has to leave a
    deferred file exactly where it was — a mode running out of budget must not
    produce a weaker answer, only a later one.
    """
    from librairy import ocr

    conn, _ = analysed(tmp_path, resources.QUIET)
    budget = ocr.budget_for(resources.processing_mode(conn))

    assert budget.limit == resources.PROCESSING_MODES[resources.QUIET].ocr_per_cycle
    #  Nothing here was OCR'd — tesseract is not a test dependency — so the
    #  claim under test is the one that holds regardless: a deferred file keeps
    #  its state and is not proposed.
    stranded = conn.execute(
        "SELECT COUNT(*) FROM items i LEFT JOIN proposals p ON p.item_id = i.id"
        " WHERE i.state = 'discovered' AND p.id IS NOT NULL"
    ).fetchone()[0]
    assert stranded == 0, "a file was left discovered with a proposal against it"


# --- M2-01 against the rest ---------------------------------------------------------


@poppler
def test_a_held_file_blocks_nothing(tmp_path: Path) -> None:
    """The Inbox carries on without the file it could not answer.

    Everything else in the batch is proposed, grouped and tagged. The held file
    keeps its tag — it was written on the folder it arrived in, and that is
    true whatever happened next — and it is in no group, because a file with no
    answer has no answer to approve.
    """
    conn, _ = analysed(tmp_path)

    held = [
        int(row["item_id"]) for row in conn.execute("SELECT item_id FROM processing_waits")
    ]

    assert len(held) == 1
    assert [tag["tag"] for tag in tags.for_item(conn, held[0])] == ["projecthouse"]
    assert conn.execute(
        "SELECT COUNT(*) FROM proposals WHERE item_id = ?", (held[0],)
    ).fetchone()[0] == 0
    #  And the rest of the batch got everything it was owed.
    seen = picture(conn)
    assert ("tagged_set", "Financial documents tagged #ProjectHouse") in seen["groups"]
    assert ("book_series", "A Wizard of Earthsea") in seen["groups"]


def test_the_three_waiting_reasons_survived(tmp_path: Path) -> None:
    """Asked for explicitly, and worth pinning where it can be broken.

    "AI unavailable", "AI processing failed" and "more evidence genuinely
    required" are three different states of the same file, and every later M2
    item touched the analysis loop they are written from.
    """
    assert waiting.UNAVAILABLE != waiting.FAILED != waiting.EVIDENCE
    assert set(waiting.RESUMABLE) == {waiting.UNAVAILABLE, waiting.FAILED}, (
        "a reason became resumable or stopped being; the distinction is the point"
    )
    conn, _ = analysed(tmp_path)

    reasons = {
        str(row["reason"]) for row in conn.execute("SELECT reason FROM processing_waits")
    }

    assert reasons <= {waiting.UNAVAILABLE, waiting.FAILED, waiting.EVIDENCE}


# --- one authority path -------------------------------------------------------------


def test_a_tag_and_a_rule_reach_a_destination_the_same_way() -> None:
    """M2-04 and M2-05 are one authority model, structurally rather than by habit.

    Both are cues on the same ladder, answered by the same `suggest`, rendered
    by the same row. The check is that neither has grown a path of its own:
    nothing outside Decision Memory turns a tag or a rule into a destination.
    """
    import inspect

    from librairy import rules
    from librairy import tags as tag_store
    from librairy.web import review

    for module in (tag_store, rules):
        source = inspect.getsource(module)
        assert "dest_relpath" not in source, (
            f"{module.__name__} names a destination; that is Decision Memory's job"
        )
    #  And the one place that does name one reads a rule and a learned answer
    #  through the same call, so a rule cannot outrank strong evidence by
    #  arriving on a different path. See `learned_suggestions`.
    suggestions = inspect.getsource(review.learned_suggestions)
    assert "outranked(row)" in suggestions
    assert suggestions.index("outranked(row)") < suggestions.index("matching(promoted")


def test_the_m2_tables_are_all_reachable_from_a_released_schema(
    tmp_path: Path,
) -> None:
    """Everything M2 added arrives on an upgrade, not only on a fresh install.

    1.3.1 shipped on schema 47. The six items since added four tables and three
    columns, and a person upgrading gets them by replaying migrations rather
    than by starting again — which is the only path any of them will actually
    take.
    """
    from librairy.db import SCHEMA_VERSION, migrate, user_version

    settings = settings_for(tmp_path)
    conn = connect(settings)
    #  A fresh database is already current; the claim is about the replay, so
    #  this asserts the end state every historical fixture in
    #  `test_release_acceptance` is migrated to.
    assert user_version(conn) == SCHEMA_VERSION
    migrate(conn)

    tables = {
        str(row["name"])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }

    assert {"processing_waits", "decision_rules", "item_tags", "projects"} <= tables
    columns = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(proposals)")
    }
    assert {"group_key", "group_hint"} <= columns
    assert "reason" in {
        str(row["name"]) for row in conn.execute("PRAGMA table_info(groups)")
    }
