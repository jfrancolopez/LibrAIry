"""Two identical files, and the one decision that answers it.

The audit has been able to see this since the first release and could never do
anything about it. The reason in `EXECUTABLE_KINDS` was right the whole time —
setting a copy aside is a quarantine, not a move, and quarantine has its own
safety semantics — but the consequence was that the finding with the clearest
answer in the whole audit was the one with no button.

Two things hold this together, and the first is not about code at all.

**LibrAIry does not choose which copy you keep.** The bytes are identical, so
there is nothing measurable to appeal to; the difference is what the folders
mean to you. Every deterministic rule anyone could write — keep the deeper one,
keep the first alphabetically — is a preference wearing a rule's clothes, and it
would be applied to a whole library. So the row lists the copies and the person
picks, which is why this can never be an executable kind and why bulk Approve
must never reach it.

**And the last copy is never set aside.** That is the check every test here
circles: on disk, in the index, across two tabs, and through the endpoint
directly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from librairy.audit import audit_library
from librairy.audit_duplicates import copies, set_aside
from librairy.config import Settings
from librairy.corrections import CorrectionRefused, undo_correction
from librairy.db import connect
from librairy.executor import execute_plan
from librairy.scanner import scan_root
from librairy.web.actionability import CHOICE, can_approve

HERE = "Music/Pop/Queen/05 - Song.flac"
THERE = "Music/Unsorted/05 - Song.flac"


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


def library(tmp_path: Path, *relpaths: str, body: str = "the same bytes"):
    settings = settings_for(tmp_path)
    conn = connect(settings)
    for relpath in relpaths:
        path = settings.library_dir / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    scan_root(conn, "library", settings.library_dir, settings)
    return conn, settings


def duplicate(conn, settings):
    audit_library(conn, settings, read_tags=False, use_catalogs=False)
    return conn.execute("SELECT * FROM audit_findings WHERE kind='duplicate'").fetchone()


def tree(root: Path) -> list[str]:
    return sorted(
        path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()
    )


# --- what the row offers ---------------------------------------------------------


def test_both_copies_are_offered_and_neither_is_recommended(tmp_path: Path) -> None:
    conn, settings = library(tmp_path, HERE, THERE)
    row = duplicate(conn, settings)

    found = copies(conn, settings, row)

    assert [copy.relpath for copy in found] == [HERE, THERE]
    assert all(copy.removable for copy in found)


def test_a_duplicate_is_a_choice_and_never_approvable_in_bulk(tmp_path: Path) -> None:
    from librairy.web.review import _audit_row

    conn, settings = library(tmp_path, HERE, THERE)
    row = _audit_row(conn, settings, duplicate(conn, settings))

    assert row["status_kind"] == CHOICE
    assert can_approve(CHOICE) is False
    assert row["can_approve"] is False
    assert len(row["copies"]) == 2


def test_the_page_says_why_rather_than_dropping_a_control(tmp_path: Path) -> None:
    from librairy.protected import set_protected_roots

    conn, settings = library(tmp_path, HERE, THERE)
    row = duplicate(conn, settings)
    set_protected_roots(conn, ["Music/Pop"])

    found = {copy.relpath: copy for copy in copies(conn, settings, row)}

    assert found[HERE].removable is False
    assert "protected" in found[HERE].reason
    assert found[THERE].removable is True


def test_one_copy_left_offers_nothing(tmp_path: Path) -> None:
    """The finding is a statement about a moment. If somebody deleted the other
    copy by hand since, there is no duplicate any more."""
    conn, settings = library(tmp_path, HERE, THERE)
    row = duplicate(conn, settings)
    (settings.library_dir / THERE).unlink()
    scan_root(conn, "library", settings.library_dir, settings)

    found = copies(conn, settings, row)

    assert [copy.removable for copy in found] == [False]
    assert "only one copy" in found[0].reason


def test_a_copy_already_waiting_for_commit_is_not_offered_again(
    tmp_path: Path,
) -> None:
    conn, settings = library(tmp_path, HERE, THERE)
    row = duplicate(conn, settings)
    set_aside(conn, settings, row["id"], THERE)

    found = {copy.relpath: copy for copy in copies(conn, settings, row)}

    assert found[THERE].removable is False
    assert "waiting for Commit" in found[THERE].reason


# --- setting one aside -------------------------------------------------------------


def test_setting_a_copy_aside_builds_one_approved_quarantine_plan(
    tmp_path: Path,
) -> None:
    conn, settings = library(tmp_path, HERE, THERE)
    row = duplicate(conn, settings)

    plan_id = set_aside(conn, settings, row["id"], THERE)

    ops = conn.execute(
        "SELECT * FROM plan_ops WHERE plan_id=?", (plan_id,)
    ).fetchall()
    assert len(ops) == 1
    assert ops[0]["op_type"] == "quarantine"
    assert ops[0]["src_root"] == "library"
    assert ops[0]["src_relpath"] == THERE
    assert ops[0]["dest_root"] == "quarantine"
    plan = conn.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
    assert plan["status"] == "approved"
    assert plan["audit_finding_id"] == row["id"]


def test_nothing_moves_until_commit(tmp_path: Path) -> None:
    conn, settings = library(tmp_path, HERE, THERE)
    row = duplicate(conn, settings)

    set_aside(conn, settings, row["id"], THERE)

    assert tree(settings.library_dir) == sorted([HERE, THERE])
    assert tree(settings.quarantine_dir) == []


def test_commit_moves_the_chosen_copy_and_leaves_the_other(tmp_path: Path) -> None:
    conn, settings = library(tmp_path, HERE, THERE)
    row = duplicate(conn, settings)
    plan_id = set_aside(conn, settings, row["id"], THERE)

    summary = execute_plan(conn, plan_id, settings)

    assert summary.done == 1
    assert tree(settings.library_dir) == [HERE]
    assert len(tree(settings.quarantine_dir)) == 1
    assert tree(settings.quarantine_dir)[0].endswith(THERE)


def test_nothing_is_deleted(tmp_path: Path) -> None:
    """It moves to quarantine, which is a folder you can look in."""
    conn, settings = library(tmp_path, HERE, THERE)
    row = duplicate(conn, settings)
    original = (settings.library_dir / THERE).read_bytes()

    execute_plan(conn, set_aside(conn, settings, row["id"], THERE), settings)

    held = settings.quarantine_dir / tree(settings.quarantine_dir)[0]
    assert held.read_bytes() == original


def test_it_appears_in_quarantine_with_where_it_came_from(tmp_path: Path) -> None:
    conn, settings = library(tmp_path, HERE, THERE)
    row = duplicate(conn, settings)

    execute_plan(conn, set_aside(conn, settings, row["id"], THERE), settings)

    entry = conn.execute("SELECT * FROM quarantine_entries").fetchone()
    assert entry["original_root"] == "library"
    assert entry["original_relpath"] == THERE


def test_undo_puts_the_copy_back(tmp_path: Path) -> None:
    conn, settings = library(tmp_path, HERE, THERE)
    row = duplicate(conn, settings)
    before = tree(settings.library_dir)
    plan_id = set_aside(conn, settings, row["id"], THERE)
    execute_plan(conn, plan_id, settings)

    results = undo_correction(conn, settings, plan_id)

    assert [result.outcome for result in results] == ["ok"]
    assert tree(settings.library_dir) == before
    assert tree(settings.quarantine_dir) == []


# --- the refusals ---------------------------------------------------------------------


def test_the_last_copy_cannot_be_set_aside(tmp_path: Path) -> None:
    """The check that matters most. Two tabs, one file each, and quarantine
    would hold both halves of a pair the library no longer has."""
    conn, settings = library(tmp_path, HERE, THERE)
    row = duplicate(conn, settings)
    execute_plan(conn, set_aside(conn, settings, row["id"], THERE), settings)
    conn.execute("UPDATE audit_findings SET status='open', plan_id=NULL WHERE id=?", (row["id"],))

    with pytest.raises(CorrectionRefused, match="only one copy"):
        set_aside(conn, settings, row["id"], HERE)

    assert tree(settings.library_dir) == [HERE]


def test_a_second_copy_cannot_be_set_aside_while_the_first_waits(
    tmp_path: Path,
) -> None:
    conn, settings = library(tmp_path, HERE, THERE)
    row = duplicate(conn, settings)
    set_aside(conn, settings, row["id"], THERE)

    with pytest.raises(CorrectionRefused, match="already waiting for Commit"):
        set_aside(conn, settings, row["id"], HERE)


def test_a_file_that_is_not_one_of_these_copies_is_refused(tmp_path: Path) -> None:
    other = "Music/Pop/Queen/06 - Other.flac"
    conn, settings = library(tmp_path, HERE, THERE)
    (settings.library_dir / other).write_text("different bytes", encoding="utf-8")
    scan_root(conn, "library", settings.library_dir, settings)
    row = duplicate(conn, settings)

    with pytest.raises(CorrectionRefused, match="not one of these copies"):
        set_aside(conn, settings, row["id"], other)


def test_a_copy_that_changed_since_the_scan_is_refused(tmp_path: Path) -> None:
    conn, settings = library(tmp_path, HERE, THERE)
    row = duplicate(conn, settings)
    (settings.library_dir / THERE).write_text("different now", encoding="utf-8")

    with pytest.raises(CorrectionRefused, match="changed since"):
        set_aside(conn, settings, row["id"], THERE)


def test_a_protected_copy_is_refused_at_the_door(tmp_path: Path) -> None:
    from librairy.protected import set_protected_roots

    conn, settings = library(tmp_path, HERE, THERE)
    row = duplicate(conn, settings)
    set_protected_roots(conn, ["Music/Pop"])

    with pytest.raises(CorrectionRefused, match="protected"):
        set_aside(conn, settings, row["id"], HERE)


def test_another_kind_of_finding_cannot_be_set_aside(tmp_path: Path) -> None:
    from librairy.audit import Finding, record_findings

    conn, settings = library(tmp_path, HERE, THERE)
    record_findings(
        conn,
        [
            Finding(
                relpath=HERE,
                kind="tag-path-mismatch",
                severity="high",
                summary="Tagged one way, filed another.",
                dest_relpath="Music/Rock/Queen/05 - Song.flac",
            )
        ],
    )
    other = conn.execute(
        "SELECT id FROM audit_findings WHERE kind='tag-path-mismatch'"
    ).fetchone()

    with pytest.raises(CorrectionRefused, match="not a duplicate"):
        set_aside(conn, settings, other["id"], HERE)


def test_the_route_refuses_the_same_things_the_row_does(tmp_path: Path) -> None:
    """A page left open since yesterday, a second tab, and curl."""
    from fastapi.testclient import TestClient

    from librairy.web.app import create_app

    conn, settings = library(tmp_path, HERE, THERE)
    row = duplicate(conn, settings)
    client = TestClient(create_app(settings, conn))
    client.get("/review")
    token = client.cookies.get("csrf_token", "")

    ok = client.post(
        f"/review/audit/{row['id']}/set-aside",
        data={"relpath": THERE, "csrf_token": token},
        headers={"x-csrf-token": token},
        follow_redirects=False,
    )
    again = client.post(
        f"/review/audit/{row['id']}/set-aside",
        data={"relpath": HERE, "csrf_token": token},
        headers={"x-csrf-token": token},
        follow_redirects=False,
    )

    assert ok.status_code == 303
    assert again.status_code == 409
