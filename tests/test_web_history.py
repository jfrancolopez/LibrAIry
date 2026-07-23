from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from librairy.config import Settings
from librairy.db import connect
from librairy.executor import execute_plan
from librairy.planner import OperationSpec, approve_plan, create_plan
from librairy.scanner import scan_root
from librairy.web.app import create_app


def client_for(tmp_path: Path) -> tuple[TestClient, object, Settings]:
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        FILE_STABILITY_SECONDS=0,
        _env_file=None,
    )
    settings.inbox_dir.mkdir()
    settings.library_dir.mkdir()
    settings.quarantine_dir.mkdir()
    conn = connect(settings)
    client = TestClient(create_app(settings, conn))
    client.post("/setup", data={"password": "correct horse battery"})
    return client, conn, settings


def test_history_lists_commit_plan_detail_and_single_op_undo(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    plan_id = seed_committed_plan(settings, conn, ["a.txt"])
    history_id = conn.execute("SELECT id FROM history WHERE action='move'").fetchone()[0]
    plan_hash = conn.execute("SELECT plan_hash FROM plans WHERE id=?", (plan_id,)).fetchone()[0]

    history_page = client.get("/history")
    detail = client.get(f"/history/plans/{plan_id}")
    undo = client.post(f"/history/undo/{history_id}", headers=csrf(client))

    assert plan_id in history_page.text
    assert plan_hash in detail.text
    assert "Documents/a.txt" in detail.text
    assert undo.status_code == 200
    assert ">undo</span>" in undo.text
    assert (settings.inbox_dir / "a.txt").read_text(encoding="utf-8") == "a.txt"
    assert not (settings.library_dir / "Documents/a.txt").exists()


def test_whole_plan_undo_restores_pre_commit_tree_and_journals(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    plan_id = seed_committed_plan(settings, conn, ["a.txt", "b.txt"])

    response = client.post(f"/history/plans/{plan_id}/undo", headers=csrf(client))

    actions = [
        row["action"]
        for row in conn.execute(
            "SELECT action FROM history WHERE plan_id=? ORDER BY id", (plan_id,)
        )
    ]
    assert response.status_code == 200
    assert response.text.count(">undo</span>") == 2
    assert (settings.inbox_dir / "a.txt").exists()
    assert (settings.inbox_dir / "b.txt").exists()
    assert not (settings.library_dir / "Documents/a.txt").exists()
    assert actions == ["move", "move", "undo_move", "undo_move"]


def test_fingerprint_mismatch_refusal_renders_expected_and_actual(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    seed_committed_plan(settings, conn, ["a.txt"])
    history_id = conn.execute("SELECT id FROM history WHERE action='move'").fetchone()[0]
    expected = conn.execute(
        "SELECT fingerprint FROM history WHERE id=?", (history_id,)
    ).fetchone()[0]
    (settings.library_dir / "Documents/a.txt").write_text("changed", encoding="utf-8")

    response = client.post(f"/history/undo/{history_id}", headers=csrf(client))

    assert response.status_code == 200
    assert "undo_refused_changed" in response.text
    assert f"expected={expected}" in response.text
    assert "actual=" in response.text
    assert (settings.library_dir / "Documents/a.txt").read_text(encoding="utf-8") == "changed"


def test_journal_rows_are_read_only_without_edit_affordances(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    seed_committed_plan(settings, conn, ["a.txt"])

    response = client.get("/history")

    assert response.status_code == 200
    assert "edit" not in response.text.lower()
    assert "contenteditable" not in response.text.lower()
    assert "Undo" in response.text


def seed_committed_plan(settings: Settings, conn, names: list[str]) -> str:
    for name in names:
        (settings.inbox_dir / name).write_text(name, encoding="utf-8")
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    plan_id = create_plan(
        conn,
        [OperationSpec("move", name, "library", f"Documents/{name}") for name in names],
        settings,
    )
    approve_plan(conn, plan_id, settings)
    execute_plan(conn, plan_id, settings)
    return plan_id


def csrf(client: TestClient) -> dict[str, str]:
    return {"x-csrf-token": client.cookies["csrf_token"]}


def test_history_timeline_groups_by_plan_and_deep_links_to_browse(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    plan_id = seed_committed_plan(settings, conn, ["a.txt", "b.txt"])

    page = client.get("/history").text

    # Grouped git-log style, with an undo-plan control and the plan link.
    assert "timeline-plan" in page
    assert f"/history/plans/{plan_id}" in page
    assert "Undo plan" in page
    assert "2 file(s)" in page
    # Committed destinations deep-link into Browse at the containing folder.
    assert '/browse/documents' in page
