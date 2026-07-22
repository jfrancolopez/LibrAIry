from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from librairy.config import Settings
from librairy.db import connect
from librairy.models import EvidenceEntry
from librairy.proposals import upsert_proposal
from librairy.scanner import scan_root
from librairy.web import commit as commit_module
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


def test_commit_flow_executes_exact_approved_plan_and_hash(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    seed_approved(conn, settings, "a.txt", "Documents/a.txt")
    seed_proposal_only(conn, settings, "b.txt", "Documents/b.txt", status="proposed")

    confirm = client.post("/commit/create", headers=csrf(client))
    plan_id = conn.execute("SELECT id FROM plans ORDER BY created_at DESC LIMIT 1").fetchone()[0]
    plan_hash = conn.execute("SELECT plan_hash FROM plans WHERE id=?", (plan_id,)).fetchone()[0]
    ops = conn.execute("SELECT * FROM plan_ops WHERE plan_id=?", (plan_id,)).fetchall()

    assert confirm.status_code == 200
    assert plan_hash in confirm.text
    assert len(ops) == 1
    assert ops[0]["src_relpath"] == "a.txt"
    assert "b.txt" not in confirm.text

    execute = client.post(f"/commit/execute/{plan_id}", headers=csrf(client))
    wait_for_plan(conn, plan_id)
    progress = client.get(f"/commit/progress/{plan_id}")

    assert execute.status_code == 200
    assert (settings.library_dir / "Documents/a.txt").read_text(encoding="utf-8") == "a.txt"
    assert (
        conn.execute("SELECT plan_hash FROM plans WHERE id=?", (plan_id,)).fetchone()[0]
        == plan_hash
    )
    assert "done: 1" in progress.text
    assert (
        conn.execute("SELECT result FROM plan_ops WHERE plan_id=?", (plan_id,)).fetchone()[0]
        == "done"
    )


def test_commit_reports_changed_source_without_touching_file(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    seed_approved(conn, settings, "a.txt", "Documents/a.txt")
    client.post("/commit/create", headers=csrf(client))
    plan_id = conn.execute("SELECT id FROM plans ORDER BY created_at DESC LIMIT 1").fetchone()[0]
    (settings.inbox_dir / "a.txt").write_text("changed", encoding="utf-8")

    client.post(f"/commit/execute/{plan_id}", headers=csrf(client))
    wait_for_plan(conn, plan_id)
    progress = client.get(f"/commit/progress/{plan_id}")

    assert "changed: 1" in progress.text
    assert (
        conn.execute("SELECT result FROM plan_ops WHERE plan_id=?", (plan_id,)).fetchone()[0]
        == "skipped_changed"
    )
    assert (settings.inbox_dir / "a.txt").read_text(encoding="utf-8") == "changed"


def test_second_commit_attempt_is_blocked_with_friendly_message(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    seed_approved(conn, settings, "a.txt", "Documents/a.txt")
    client.post("/commit/create", headers=csrf(client))
    plan_id = conn.execute("SELECT id FROM plans ORDER BY created_at DESC LIMIT 1").fetchone()[0]
    client.app.state.commit_state.active_plan_id = "other-plan"

    response = client.post(f"/commit/execute/{plan_id}", headers=csrf(client))

    assert response.status_code == 200
    assert "execution started" not in response.text
    assert "pending: 1" in response.text


def test_progress_endpoint_responds_while_background_commit_runs(
    tmp_path: Path, monkeypatch
) -> None:
    client, conn, settings = client_for(tmp_path)
    seed_approved(conn, settings, "a.txt", "Documents/a.txt")
    client.post("/commit/create", headers=csrf(client))
    plan_id = conn.execute("SELECT id FROM plans ORDER BY created_at DESC LIMIT 1").fetchone()[0]
    original_execute = commit_module.execute_plan

    def slow_execute(conn, plan_id, settings):  # noqa: ANN001
        time.sleep(0.3)
        return original_execute(conn, plan_id, settings)

    monkeypatch.setattr(commit_module, "execute_plan", slow_execute)
    client.post(f"/commit/execute/{plan_id}", headers=csrf(client))

    response = client.get(f"/commit/progress/{plan_id}")

    assert response.status_code == 200
    assert "pending:" in response.text
    wait_for_plan(conn, plan_id)


def seed_approved(conn, settings: Settings, relpath: str, dest_relpath: str) -> int:
    proposal_id = seed_proposal_only(conn, settings, relpath, dest_relpath, status="approved")
    conn.execute(
        """
        UPDATE items SET state='approved'
        WHERE id=(SELECT item_id FROM proposals WHERE id=?)
        """,
        (proposal_id,),
    )
    return proposal_id


def seed_proposal_only(
    conn, settings: Settings, relpath: str, dest_relpath: str, status: str
) -> int:
    (settings.inbox_dir / relpath).write_text(relpath, encoding="utf-8")
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    item_id = conn.execute("SELECT id FROM items WHERE relpath=?", (relpath,)).fetchone()[0]
    proposal_id = upsert_proposal(
        conn,
        item_id=item_id,
        category="documents",
        clean_name=Path(relpath).name,
        dest_relpath=dest_relpath,
        confidence=0.9,
        evidence=[EvidenceEntry("heuristic", "category", "test", 0.9)],
    )
    conn.execute("UPDATE proposals SET status=? WHERE id=?", (status, proposal_id))
    return proposal_id


def csrf(client: TestClient) -> dict[str, str]:
    return {"x-csrf-token": client.cookies["csrf_token"]}


def wait_for_plan(conn, plan_id: str) -> None:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        status = conn.execute("SELECT status FROM plans WHERE id=?", (plan_id,)).fetchone()[0]
        if status in {"done", "failed"}:
            return
        time.sleep(0.02)
    raise AssertionError("plan did not finish")
