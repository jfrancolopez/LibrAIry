"""Restoring a whole decision, rather than eighteen files that resemble one.

The grouping is the thing under test. It comes from `quarantine_entries.plan_id`
— the plan that actually moved these files — and never from a shared date
folder, a common reason string or a filename prefix. Those group files that
look alike; this groups files that left together.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from librairy.config import Settings
from librairy.db import connect
from librairy.executor import execute_plan
from librairy.history import undo_plan
from librairy.planner import OperationSpec, approve_plan, create_plan
from librairy.quarantine_groups import (
    RestoreGroupError,
    cancel_restore,
    decision,
    decisions,
    preflight,
    request_restore,
)
from librairy.scanner import scan_root
from librairy.web.app import create_app


def settings_for(tmp_path: Path) -> Settings:
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        FILE_STABILITY_SECONDS=0,
        _env_file=None,
    )
    for root in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        root.mkdir(parents=True, exist_ok=True)
    return settings


def client_for(tmp_path: Path) -> tuple[TestClient, sqlite3.Connection, Settings]:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    client = TestClient(create_app(settings, conn))
    client.post("/setup", data={"password": "correct horse battery"})
    return client, conn, settings


def set_aside(
    conn: sqlite3.Connection,
    settings: Settings,
    names: list[str],
    *,
    day: str = "2026-08-20",
    folder: str = "Photos/Trip",
) -> str:
    """Commit one decision that sets several library files aside."""
    for name in names:
        path = settings.library_dir / folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name, encoding="utf-8")
    scan_root(conn, "library", settings.library_dir, settings)
    plan_id = create_plan(
        conn,
        [
            OperationSpec(
                op_type="quarantine",
                src_root="library",
                src_relpath=f"{folder}/{name}",
                dest_root="quarantine",
                dest_relpath=f"{day}/{folder}/{name}",
            )
            for name in names
        ],
        settings,
    )
    approve_plan(conn, plan_id, settings)
    execute_plan(conn, plan_id, settings)
    return plan_id


# 15 — several quarantine entries from one plan group together.
def test_entries_from_one_plan_form_one_decision(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    plan_id = set_aside(conn, settings, ["a.jpg", "b.jpg", "c.jpg"])

    found = decisions(conn)

    assert len(found) == 1
    assert (found[0].plan_id, found[0].total, found[0].held) == (plan_id, 3, 3)


# 16 — entries from different plans are never merged.
def test_two_decisions_on_the_same_day_stay_two_decisions(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    #  Same date folder, same reason, adjacent ids, same folder prefix. Every
    #  resemblance a heuristic could have grouped on, and they are still two
    #  answers somebody gave separately.
    first = set_aside(conn, settings, ["a.jpg", "b.jpg"])
    second = set_aside(conn, settings, ["c.jpg", "d.jpg"])

    found = {item.plan_id: item.total for item in decisions(conn)}

    assert found == {first: 2, second: 2}


# 17 — pressing Restore moves nothing.
def test_requesting_a_group_restore_moves_no_files(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    plan_id = set_aside(conn, settings, ["a.jpg", "b.jpg", "c.jpg"])

    restore = request_restore(conn, settings, plan_id)

    assert (settings.quarantine_dir / "2026-08-20/Photos/Trip/a.jpg").exists()
    assert not (settings.library_dir / "Photos/Trip/a.jpg").exists()
    status = conn.execute("SELECT status FROM plans WHERE id=?", (restore,)).fetchone()[0]
    assert status == "approved"


# 18 — the request is one Commit decision.
def test_the_restore_request_is_one_decision_in_commit(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    plan_id = set_aside(conn, settings, ["a.jpg", "b.jpg", "c.jpg"])
    request_restore(conn, settings, plan_id)

    page = client.get("/commit")

    assert "Put back 3 files" in page.text
    assert "RESTORE" in page.text
    #  Not a library correction: three files moving into the library is what
    #  every other rule here reads as one, and it is not what this is.
    assert "Library corrections" not in page.text


# 19 — the preflight validates every held member.
def test_preflight_reports_every_member(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    plan_id = set_aside(conn, settings, ["a.jpg", "b.jpg", "c.jpg"])

    checked = preflight(conn, settings, plan_id)

    assert len(checked.restorable) == 3
    assert checked.blocked == ""


# 20 — a changed held member blocks the whole coherent restore.
def test_a_changed_member_blocks_the_group(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    plan_id = set_aside(conn, settings, ["a.jpg", "b.jpg", "c.jpg"])
    held = settings.quarantine_dir / "2026-08-20/Photos/Trip/b.jpg"
    held.write_text("edited since", encoding="utf-8")

    checked = preflight(conn, settings, plan_id)

    assert [member.name for member in checked.changed] == ["b.jpg"]
    assert "not what it was" in checked.blocked
    with pytest.raises(RestoreGroupError):
        request_restore(conn, settings, plan_id)
    #  And nothing else was put back either. That is the point of coherent.
    assert not (settings.library_dir / "Photos/Trip/a.jpg").exists()


# 21 — a collision at an original path blocks the restore.
def test_something_already_at_an_original_path_blocks_the_group(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    plan_id = set_aside(conn, settings, ["a.jpg", "b.jpg"])
    (settings.library_dir / "Photos/Trip").mkdir(parents=True, exist_ok=True)
    (settings.library_dir / "Photos/Trip/a.jpg").write_text("someone else", encoding="utf-8")

    checked = preflight(conn, settings, plan_id)

    assert [member.name for member in checked.colliding] == ["a.jpg"]
    assert "already at one of the original paths" in checked.blocked


# 22 — an already-restored member is excluded, and the count says so.
def test_an_already_restored_member_is_excluded(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    plan_id = set_aside(conn, settings, ["a.jpg", "b.jpg", "c.jpg"])
    entry = conn.execute(
        "SELECT id FROM quarantine_entries ORDER BY id LIMIT 1"
    ).fetchone()[0]
    from librairy.quarantine_requests import request_restore as request_one

    one = request_one(conn, settings, entry)
    execute_plan(conn, one, settings)

    found = decision(conn, plan_id)
    checked = preflight(conn, settings, plan_id)

    assert found is not None
    assert (found.held, found.restored, found.partly_restored) == (2, 1, True)
    assert len(checked.restorable) == 2
    assert len(checked.already_restored) == 1


# 23 — a member that vanished from disk is reported, never invented.
def test_a_missing_held_member_is_reported(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    plan_id = set_aside(conn, settings, ["a.jpg", "b.jpg", "c.jpg"])
    (settings.quarantine_dir / "2026-08-20/Photos/Trip/c.jpg").unlink()

    checked = preflight(conn, settings, plan_id)

    assert [member.name for member in checked.gone] == ["c.jpg"]
    assert len(checked.restorable) == 2
    #  Not a blocker — the two that are there can still go back, and there is
    #  no copy of the third to put anywhere.
    assert checked.blocked == ""


# 24 — a successful restore puts every file at its exact original path.
def test_committing_the_group_restores_exact_paths(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    plan_id = set_aside(conn, settings, ["a.jpg", "b.jpg", "c.jpg"])
    restore = request_restore(conn, settings, plan_id)

    summary = execute_plan(conn, restore, settings)

    assert summary.done == 3
    for name in ("a.jpg", "b.jpg", "c.jpg"):
        assert (settings.library_dir / "Photos/Trip" / name).read_text(
            encoding="utf-8"
        ) == name
    held = conn.execute(
        "SELECT COUNT(*) FROM quarantine_entries WHERE restored_at IS NULL"
    ).fetchone()[0]
    assert held == 0


# 25 — History says it once.
def test_history_reads_as_one_restored_decision(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    plan_id = set_aside(conn, settings, ["a.jpg", "b.jpg", "c.jpg"])
    execute_plan(conn, request_restore(conn, settings, plan_id), settings)

    page = client.get("/history")

    assert "Restored 3 files from" in page.text
    assert "Filed 3 files" not in page.text


# 26 — undoing a group restore is exact.
def test_undoing_a_group_restore_puts_them_back_in_quarantine(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    plan_id = set_aside(conn, settings, ["a.jpg", "b.jpg"])
    restore = request_restore(conn, settings, plan_id)
    execute_plan(conn, restore, settings)

    results = undo_plan(conn, restore, settings)

    assert [result.outcome for result in results] == ["ok", "ok"]
    assert (settings.quarantine_dir / "2026-08-20/Photos/Trip/a.jpg").exists()
    assert not (settings.library_dir / "Photos/Trip/a.jpg").exists()
    #  And the entries are held again rather than sitting under "Put back"
    #  while the file is physically in Quarantine.
    held = conn.execute(
        "SELECT COUNT(*) FROM quarantine_entries WHERE restored_at IS NULL"
    ).fetchone()[0]
    assert held == 2


# 27 — the ordinary single-row restore is unchanged.
def test_one_file_restore_still_works_on_its_own(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    set_aside(conn, settings, ["a.jpg", "b.jpg"])
    entry = conn.execute("SELECT id FROM quarantine_entries ORDER BY id LIMIT 1").fetchone()[0]

    response = client.post(
        f"/quarantine/restore/{entry}",
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )

    assert response.status_code == 200
    assert "Restore requested" in response.text
    plan = conn.execute(
        "SELECT id FROM plans WHERE quarantine_entry_id=?", (entry,)
    ).fetchone()
    execute_plan(conn, plan["id"], settings)
    assert (settings.library_dir / "Photos/Trip/a.jpg").exists()


# 28 — nothing is recovered from a backup, ever.
def test_a_missing_member_is_never_recreated(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    plan_id = set_aside(conn, settings, ["a.jpg", "b.jpg"])
    (settings.quarantine_dir / "2026-08-20/Photos/Trip/b.jpg").unlink()

    execute_plan(conn, request_restore(conn, settings, plan_id), settings)

    assert (settings.library_dir / "Photos/Trip/a.jpg").exists()
    assert not (settings.library_dir / "Photos/Trip/b.jpg").exists()


def test_one_file_decisions_are_not_offered_as_a_group(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    plan_id = set_aside(conn, settings, ["only.jpg"])

    assert decisions(conn) == []
    assert decision(conn, plan_id) is None


def test_a_second_request_on_the_same_decision_is_refused(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    plan_id = set_aside(conn, settings, ["a.jpg", "b.jpg"])
    request_restore(conn, settings, plan_id)

    with pytest.raises(RestoreGroupError, match="already waiting"):
        request_restore(conn, settings, plan_id)


def test_a_member_with_its_own_pending_decision_refuses_the_group(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    plan_id = set_aside(conn, settings, ["a.jpg", "b.jpg"])
    entry = conn.execute("SELECT id FROM quarantine_entries ORDER BY id LIMIT 1").fetchone()[0]
    from librairy.quarantine_requests import request_delete_queue

    request_delete_queue(conn, settings, entry)

    with pytest.raises(RestoreGroupError, match="already waiting"):
        request_restore(conn, settings, plan_id)


def test_a_group_request_can_be_cancelled_before_commit(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    plan_id = set_aside(conn, settings, ["a.jpg", "b.jpg"])
    restore = request_restore(conn, settings, plan_id)

    cancel_restore(conn, plan_id)

    assert conn.execute("SELECT 1 FROM plans WHERE id=?", (restore,)).fetchone() is None
    assert decision(conn, plan_id).waiting is False


def test_the_quarantine_page_offers_the_whole_decision(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    plan_id = set_aside(conn, settings, ["a.jpg", "b.jpg", "c.jpg"])

    page = client.get("/quarantine")
    posted = client.post(
        f"/quarantine/decision/{plan_id}/restore",
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )

    assert "Whole decisions" in page.text
    assert "Restore all 3" in page.text
    assert "3 files set aside together" in " ".join(page.text.split())
    assert posted.status_code == 200
    assert "Restore requested for the whole decision" in posted.text
    #  Every member reads as waiting, not just one of them.
    assert conn.execute(
        "SELECT COUNT(*) FROM plan_ops o JOIN plans p ON p.id=o.plan_id"
        " WHERE p.restore_of_plan_id=?",
        (plan_id,),
    ).fetchone()[0] == 3
