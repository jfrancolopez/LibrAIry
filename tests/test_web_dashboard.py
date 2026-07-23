from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from librairy.config import Settings
from librairy.db import connect
from librairy.lifecycle import transition_item
from librairy.scanner import scan_root
from librairy.web.app import create_app


def setup_client(tmp_path: Path) -> tuple[TestClient, object, Settings]:
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


def test_dashboard_counts_update_via_partial(tmp_path: Path) -> None:
    client, conn, settings = setup_client(tmp_path)

    first = client.get("/dashboard/stats")
    (settings.inbox_dir / "a.txt").write_text("a", encoding="utf-8")
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    item_id = conn.execute("SELECT id FROM items").fetchone()[0]
    transition_item(conn, item_id, "pending")
    second = client.get("/dashboard/stats")

    assert "inbox clear" in first.text
    assert "pending: 1" in second.text


def test_dashboard_reads_existing_tables_without_engine_mutation(tmp_path: Path) -> None:
    client, conn, _ = setup_client(tmp_path)
    before = _counts(conn)

    response = client.get("/dashboard")

    after = _counts(conn)

    assert response.status_code == 200
    assert "hx-get=\"/dashboard/stats\"" in response.text
    assert before == after


def test_dashboard_empty_state_and_disk_rows_render(tmp_path: Path) -> None:
    client, _, _ = setup_client(tmp_path)

    response = client.get("/dashboard")

    assert "drop files to begin" in response.text
    assert "inbox:" in response.text
    assert "appdata:" in response.text


def test_dashboard_surfaces_backup_queue_counts(tmp_path: Path) -> None:
    client, conn, _ = setup_client(tmp_path)
    conn.execute(
        """
        INSERT INTO items(
          id, root, relpath, size, mtime_ns, fingerprint, first_seen_at, last_seen_at
        )
        VALUES (1, 'library', 'Documents/a.txt', 1, 1, 'fp', 'now', 'now')
        """
    )
    conn.execute(
        """
        INSERT INTO backup_queue(item_id, relpath, fingerprint, state, created_at, updated_at)
        VALUES (1, 'Documents/a.txt', 'fp', 'queued', 'now', 'now')
        """
    )

    response = client.get("/dashboard/stats")

    assert "Backup" in response.text
    assert "queued: 1" in response.text


def _counts(conn) -> dict[str, int]:
    tables = ["items", "proposals", "history", "worker_state", "provider_status"]
    return {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables}
