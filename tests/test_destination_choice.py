"""One artist, two folders, and no fact that says which one is right.

    Music/Rock/Prince/     3 albums, 12 files
    Music/Pop/Prince/      5 albums, 27 files

Nothing is colliding and nothing is broken. The only problem is that there are
two of them, and which one is correct is a fact about how somebody wants their
library arranged. `audit_music` has always reported this and always refused to
propose a destination, which left it an observation nobody could act on.

So the row asks. This file is about the two promises that make asking honest:
the candidates are folders the library really has, and none of them is
recommended. And about the second half — that once the direction is chosen this
is an ordinary merge, planned by the ordinary merge planner, with the ordinary
collision questions, plan, Commit and Undo.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from librairy.audit import Finding, record_findings
from librairy.config import Settings
from librairy.corrections import CorrectionRefused, accept_correction, undo_correction
from librairy.db import connect
from librairy.destination_choice import (
    candidates,
    choose,
    chosen,
    plan_for,
    selected,
    subject,
)
from librairy.executor import execute_plan
from librairy.merge import KEEP_EXISTING, USE_INCOMING, record_choice
from librairy.models import EvidenceEntry
from librairy.scanner import scan_root
from librairy.web.actionability import APPROVABLE, CHOICE

ROCK = "Music/Rock/Prince"
POP = "Music/Pop/Prince"


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


def library(tmp_path: Path, files: dict[str, str]):
    settings = settings_for(tmp_path)
    conn = connect(settings)
    write(settings, files)
    scan_root(conn, "library", settings.library_dir, settings)
    return conn, settings


def write(settings: Settings, files: dict[str, str]) -> None:
    for relpath, body in files.items():
        path = settings.library_dir / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")


def rescan(conn, settings) -> None:
    scan_root(conn, "library", settings.library_dir, settings)


def split_finding(conn, *, relpath: str = f"{ROCK}/Dirty Mind"):
    """The finding as `audit_music` really writes it: no destination at all."""
    record_findings(
        conn,
        [
            Finding(
                relpath=relpath,
                kind="artist-split",
                severity="review",
                summary="'Prince' has folders under 2 different sections.",
                evidence=[
                    EvidenceEntry("filesystem", "artist", "Prince", 0.9),
                    EvidenceEntry("library-pattern", "mostly under", "Music/Pop", 0.85),
                    EvidenceEntry("filesystem", "also under", "Music/Rock", 0.9),
                ],
            )
        ],
    )
    return conn.execute(
        "SELECT * FROM audit_findings WHERE kind='artist-split'"
    ).fetchone()


def two_sections(tmp_path: Path):
    """Prince in two places, with nothing colliding in either direction."""
    return library(
        tmp_path,
        {
            f"{ROCK}/Dirty Mind/01 - Dirty Mind.flac": "rock one",
            f"{POP}/Purple Rain/01 - Let's Go Crazy.flac": "pop one",
            f"{POP}/1999/01 - 1999.flac": "pop two",
        },
    )


def colliding(tmp_path: Path):
    """The same album folder on both sides, each with its own cover."""
    return library(
        tmp_path,
        {
            f"{ROCK}/Dirty Mind/01 - Dirty Mind.flac": "rock one",
            f"{ROCK}/Purple Rain/cover.jpg": "the sleeve filed under Rock",
            f"{POP}/Purple Rain/01 - Let's Go Crazy.flac": "pop one",
            f"{POP}/Purple Rain/cover.jpg": "a different scan",
        },
    )


def tree(root: Path) -> list[str]:
    return sorted(
        path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()
    )


def row_for(conn, settings, finding):
    from librairy.web.review import _audit_row

    return _audit_row(conn, settings, finding)


def reload(conn, finding):
    return conn.execute(
        "SELECT * FROM audit_findings WHERE id=?", (finding["id"],)
    ).fetchone()


# --- the question -----------------------------------------------------------------


def test_an_artist_in_two_sections_is_a_choice(tmp_path: Path) -> None:
    conn, settings = two_sections(tmp_path)
    finding = split_finding(conn)

    assert row_for(conn, settings, finding)["status_kind"] == CHOICE


def test_both_folders_are_offered_as_candidates(tmp_path: Path) -> None:
    conn, settings = two_sections(tmp_path)
    finding = split_finding(conn)

    assert [c.relpath for c in candidates(conn, finding)] == [POP, ROCK]


def test_the_candidates_are_folders_the_library_really_has(tmp_path: Path) -> None:
    """Never an invented third home, however tidy it would be.

    `Music/Other/Prince` is exactly the kind of destination a rule would
    produce and nobody asked for. Every candidate comes off the index.
    """
    conn, settings = two_sections(tmp_path)
    finding = split_finding(conn)

    for candidate in candidates(conn, finding):
        assert (settings.library_dir / candidate.relpath).is_dir()


def test_each_candidate_says_what_is_in_it(tmp_path: Path) -> None:
    conn, settings = two_sections(tmp_path)
    finding = split_finding(conn)

    by_path = {c.relpath: c for c in candidates(conn, finding)}

    assert (by_path[POP].files, by_path[POP].albums) == (2, 2)
    assert (by_path[ROCK].files, by_path[ROCK].albums) == (1, 1)
    assert by_path[POP].bytes > 0


def test_no_candidate_is_recommended(tmp_path: Path) -> None:
    """The larger folder is a fact, not an answer.

    Sorting by size, starring one, or calling one "recommended" would all be
    LibrAIry choosing while appearing to ask. The candidates come back in path
    order and carry no rank of any kind.
    """
    conn, settings = two_sections(tmp_path)
    finding = split_finding(conn)

    row = row_for(conn, settings, finding)["destination"]

    assert [c["relpath"] for c in row["candidates"]] == [POP, ROCK]
    assert not any(c["chosen"] for c in row["candidates"])
    fields = set(row["candidates"][0])
    assert fields == {"relpath", "section", "name", "files", "albums", "size", "chosen"}


def test_the_row_names_the_artist(tmp_path: Path) -> None:
    conn, settings = two_sections(tmp_path)
    finding = split_finding(conn)

    assert subject(finding) == "Prince"


def test_an_artist_left_in_one_folder_is_no_longer_a_choice(tmp_path: Path) -> None:
    conn, settings = two_sections(tmp_path)
    finding = split_finding(conn)
    for path in (settings.library_dir / ROCK).rglob("*.flac"):
        path.unlink()
    rescan(conn, settings)

    assert row_for(conn, settings, reload(conn, finding))["destination"] is None


# --- the answer -------------------------------------------------------------------


def test_choosing_a_destination_persists(tmp_path: Path) -> None:
    conn, settings = two_sections(tmp_path)
    finding = split_finding(conn)

    choose(conn, settings, finding["id"], POP)

    assert chosen(conn, finding["id"]) == POP


def test_a_folder_that_is_not_a_candidate_is_refused(tmp_path: Path) -> None:
    conn, settings = two_sections(tmp_path)
    finding = split_finding(conn)

    with pytest.raises(CorrectionRefused):
        choose(conn, settings, finding["id"], "Music/Jazz/Prince")


def test_choosing_sets_the_direction_of_an_ordinary_merge(tmp_path: Path) -> None:
    conn, settings = two_sections(tmp_path)
    finding = split_finding(conn)

    choose(conn, settings, finding["id"], POP)
    view = plan_for(conn, settings, finding, verify=True)

    assert view.target == POP
    assert view.sources == (ROCK,)
    assert [member.relpath for member in view.members] == [
        f"{ROCK}/Dirty Mind/01 - Dirty Mind.flac"
    ]


def test_the_other_direction_moves_the_other_files(tmp_path: Path) -> None:
    conn, settings = two_sections(tmp_path)
    finding = split_finding(conn)

    choose(conn, settings, finding["id"], ROCK)
    view = plan_for(conn, settings, finding, verify=True)

    assert view.target == ROCK
    assert sorted(member.relpath for member in view.members) == [
        f"{POP}/1999/01 - 1999.flac",
        f"{POP}/Purple Rain/01 - Let's Go Crazy.flac",
    ]


def test_a_clean_direction_can_be_approved(tmp_path: Path) -> None:
    conn, settings = two_sections(tmp_path)
    finding = split_finding(conn)

    choose(conn, settings, finding["id"], POP)
    row = row_for(conn, settings, reload(conn, finding))

    assert row["approve_choice"] is True
    assert row["status_kind"] == CHOICE


def test_a_direction_with_collisions_is_still_unanswered(tmp_path: Path) -> None:
    conn, settings = colliding(tmp_path)
    finding = split_finding(conn)

    choose(conn, settings, finding["id"], POP)
    row = row_for(conn, settings, reload(conn, finding))

    assert row["approve_choice"] is False
    assert row["merge"]["unresolved"] == 1


def test_answering_every_collision_enables_approve(tmp_path: Path) -> None:
    conn, settings = colliding(tmp_path)
    finding = split_finding(conn)
    choose(conn, settings, finding["id"], POP)

    record_choice(conn, finding["id"], f"{ROCK}/Purple Rain/cover.jpg", KEEP_EXISTING)
    row = row_for(conn, settings, reload(conn, finding))

    assert row["approve_choice"] is True


def test_a_resolved_choice_is_never_bulk_approvable(tmp_path: Path) -> None:
    """The person answered it. That is not the same as `approve all confident`.

    A destination choice stays `CHOICE` even when it is completely settled, so
    the bulk control cannot reach it however the selection was made. The row
    has its own Approve, pressed by whoever answered the question.
    """
    conn, settings = two_sections(tmp_path)
    finding = split_finding(conn)
    choose(conn, settings, finding["id"], POP)

    row = row_for(conn, settings, reload(conn, finding))

    assert row["status_kind"] not in APPROVABLE
    assert row["can_approve"] is False


# --- changing your mind -----------------------------------------------------------


def test_switching_direction_recomputes_the_merge(tmp_path: Path) -> None:
    conn, settings = two_sections(tmp_path)
    finding = split_finding(conn)

    choose(conn, settings, finding["id"], POP)
    choose(conn, settings, finding["id"], ROCK)
    view = plan_for(conn, settings, finding, verify=True)

    assert view.target == ROCK


def test_switching_direction_clears_the_collision_answers(tmp_path: Path) -> None:
    """`Keep existing` names a different file once the folders swap roles.

    An answer that survived the switch would be the person's word applied to a
    question nobody asked them. So the questions start again.
    """
    conn, settings = colliding(tmp_path)
    finding = split_finding(conn)
    choose(conn, settings, finding["id"], POP)
    record_choice(conn, finding["id"], f"{ROCK}/Purple Rain/cover.jpg", USE_INCOMING)

    choose(conn, settings, finding["id"], ROCK)

    assert conn.execute("SELECT COUNT(*) FROM merge_choices").fetchone()[0] == 0
    assert plan_for(conn, settings, finding, verify=True).unresolved


def test_choosing_the_same_folder_again_keeps_the_answers(tmp_path: Path) -> None:
    conn, settings = colliding(tmp_path)
    finding = split_finding(conn)
    choose(conn, settings, finding["id"], POP)
    record_choice(conn, finding["id"], f"{ROCK}/Purple Rain/cover.jpg", KEEP_EXISTING)

    choose(conn, settings, finding["id"], POP)

    assert plan_for(conn, settings, finding, verify=True).settled


# --- approval and after -----------------------------------------------------------


def test_approval_creates_an_ordinary_correction_plan(tmp_path: Path) -> None:
    conn, settings = two_sections(tmp_path)
    finding = split_finding(conn)
    choose(conn, settings, finding["id"], POP)

    plan_id = accept_correction(conn, settings, finding["id"])

    plan = conn.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
    assert plan["status"] == "approved"
    assert plan["audit_finding_id"] == finding["id"]


def test_it_introduces_no_new_operation_type(tmp_path: Path) -> None:
    """Moves and quarantines, which is everything the executor already had."""
    conn, settings = colliding(tmp_path)
    finding = split_finding(conn)
    choose(conn, settings, finding["id"], POP)
    record_choice(conn, finding["id"], f"{ROCK}/Purple Rain/cover.jpg", USE_INCOMING)

    plan_id = accept_correction(conn, settings, finding["id"])

    kinds = {
        row["op_type"]
        for row in conn.execute("SELECT op_type FROM plan_ops WHERE plan_id=?", (plan_id,))
    }
    assert kinds <= {"move", "quarantine"}


def test_commit_shows_one_decision(tmp_path: Path) -> None:
    from librairy.web.commit_queue import queue_rows

    conn, settings = two_sections(tmp_path)
    finding = split_finding(conn)
    choose(conn, settings, finding["id"], POP)
    accept_correction(conn, settings, finding["id"])

    rows = queue_rows(conn, settings, kind="correction")

    assert len(rows) == 1
    assert rows[0]["subject"] == "Bring Prince together"
    assert rows[0]["is_file"] is False
    assert rows[0]["after"].endswith(POP)
    #  The folder that moves, not the album the finding is anchored at — which
    #  after this choice sits inside the destination and is going nowhere.
    assert rows[0]["current"].endswith(ROCK)


def test_committing_merges_into_the_chosen_folder(tmp_path: Path) -> None:
    conn, settings = two_sections(tmp_path)
    finding = split_finding(conn)
    choose(conn, settings, finding["id"], POP)
    plan_id = accept_correction(conn, settings, finding["id"])

    summary = execute_plan(conn, plan_id, settings)

    assert summary.done == 1
    assert tree(settings.library_dir) == [
        f"{POP}/1999/01 - 1999.flac",
        f"{POP}/Dirty Mind/01 - Dirty Mind.flac",
        f"{POP}/Purple Rain/01 - Let's Go Crazy.flac",
    ]


def test_a_displaced_cover_goes_to_quarantine_not_away(tmp_path: Path) -> None:
    conn, settings = colliding(tmp_path)
    finding = split_finding(conn)
    choose(conn, settings, finding["id"], POP)
    record_choice(conn, finding["id"], f"{ROCK}/Purple Rain/cover.jpg", USE_INCOMING)
    plan_id = accept_correction(conn, settings, finding["id"])

    execute_plan(conn, plan_id, settings)

    assert (settings.library_dir / POP / "Purple Rain/cover.jpg").read_text() == (
        "the sleeve filed under Rock"
    )
    assert any(
        path.name == "cover.jpg" for path in settings.quarantine_dir.rglob("cover.jpg")
    )


def test_history_says_the_artist_was_brought_together(tmp_path: Path) -> None:
    from librairy.web.commit_queue import queue_rows

    conn, settings = two_sections(tmp_path)
    finding = split_finding(conn)
    choose(conn, settings, finding["id"], POP)
    plan_id = accept_correction(conn, settings, finding["id"])

    subject_line = queue_rows(conn, settings, kind="correction")[0]["subject"]
    execute_plan(conn, plan_id, settings)

    assert "Prince" in subject_line


def test_undo_puts_both_folders_back(tmp_path: Path) -> None:
    conn, settings = colliding(tmp_path)
    before = tree(settings.library_dir)
    finding = split_finding(conn)
    choose(conn, settings, finding["id"], POP)
    record_choice(conn, finding["id"], f"{ROCK}/Purple Rain/cover.jpg", USE_INCOMING)
    plan_id = accept_correction(conn, settings, finding["id"])
    execute_plan(conn, plan_id, settings)

    undo_correction(conn, settings, plan_id)

    assert tree(settings.library_dir) == before
    assert tree(settings.quarantine_dir) == []


# --- the tree moving underneath ---------------------------------------------------


def test_a_destination_that_vanished_returns_to_the_question(tmp_path: Path) -> None:
    conn, settings = two_sections(tmp_path)
    finding = split_finding(conn)
    choose(conn, settings, finding["id"], POP)
    for path in (settings.library_dir / POP).rglob("*.flac"):
        path.unlink()
    rescan(conn, settings)

    assert selected(conn, reload(conn, finding)) == ""


def test_approval_is_refused_once_the_destination_is_gone(tmp_path: Path) -> None:
    conn, settings = two_sections(tmp_path)
    finding = split_finding(conn)
    choose(conn, settings, finding["id"], POP)
    for path in (settings.library_dir / POP).rglob("*.flac"):
        path.unlink()
    rescan(conn, settings)

    with pytest.raises(CorrectionRefused):
        accept_correction(conn, settings, finding["id"])


def test_a_source_file_changed_since_the_choice_refuses_approval(tmp_path: Path) -> None:
    conn, settings = two_sections(tmp_path)
    finding = split_finding(conn)
    choose(conn, settings, finding["id"], POP)
    (settings.library_dir / ROCK / "Dirty Mind/01 - Dirty Mind.flac").write_text("edited")

    with pytest.raises(CorrectionRefused):
        accept_correction(conn, settings, finding["id"])


def test_a_collision_that_appeared_after_the_choice_refuses_commit(tmp_path: Path) -> None:
    """The destinations were examined when this was approved. That one was not.

    Renumbering it would invent a name nobody approved, so the whole merge
    stops before its first operation.
    """
    conn, settings = two_sections(tmp_path)
    finding = split_finding(conn)
    choose(conn, settings, finding["id"], POP)
    plan_id = accept_correction(conn, settings, finding["id"])
    write(settings, {f"{POP}/Dirty Mind/01 - Dirty Mind.flac": "somebody else's"})

    summary = execute_plan(conn, plan_id, settings)

    assert summary.done == 0
    assert summary.refused_collision == 1


def test_an_unanswered_choice_cannot_be_approved_by_any_route(tmp_path: Path) -> None:
    """Not by a button, not by a stale page, not by curl."""
    conn, settings = two_sections(tmp_path)
    finding = split_finding(conn)

    with pytest.raises(CorrectionRefused):
        accept_correction(conn, settings, finding["id"])


def test_a_half_answered_choice_cannot_be_approved(tmp_path: Path) -> None:
    conn, settings = colliding(tmp_path)
    finding = split_finding(conn)
    choose(conn, settings, finding["id"], POP)

    with pytest.raises(CorrectionRefused):
        accept_correction(conn, settings, finding["id"])
