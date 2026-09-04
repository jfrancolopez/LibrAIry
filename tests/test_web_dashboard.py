from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from librairy.config import Settings
from librairy.db import connect
from librairy.lifecycle import transition_item
from librairy.proposals import EvidenceEntry, upsert_proposal
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

    assert "Inbox clear" in first.text
    # Plain language, not the database's word for it.
    assert "needs more information" in second.text
    assert "pending" not in second.text


def test_dashboard_reads_existing_tables_without_engine_mutation(tmp_path: Path) -> None:
    client, conn, _ = setup_client(tmp_path)
    before = _counts(conn)

    response = client.get("/dashboard")

    after = _counts(conn)

    assert response.status_code == 200
    assert 'hx-get="/dashboard/stats?days=' in response.text
    assert before == after


def test_dashboard_empty_state_and_disk_rows_render(tmp_path: Path) -> None:
    client, _, _ = setup_client(tmp_path)

    response = client.get("/dashboard")

    assert "drop files at" in response.text
    # All four roots are one volume on a test machine, so they are named
    # together on one row rather than reported as four separate disks.
    assert "inbox + library + quarantine + appdata" in response.text
    assert "GB free" in response.text


def test_dashboard_keeps_polling_after_the_first_swap(tmp_path: Path) -> None:
    """hx-swap="outerHTML" replaces the element the attributes are on.

    With the polling attributes on a wrapper, the first response replaced that
    wrapper with markup that had no hx-trigger, and the dashboard went quiet
    for good after five seconds.
    """
    client, _, _ = setup_client(tmp_path)

    page = client.get("/dashboard")
    swapped = client.get("/dashboard/stats")

    assert 'hx-trigger="every 5s"' in swapped.text
    assert 'hx-get="/dashboard/stats?days=' in swapped.text
    # And exactly one element claims the id, on the full page too.
    assert page.text.count('id="dashboard-stats"') == 1


def test_the_chosen_history_range_survives_the_poll(tmp_path: Path) -> None:
    """Otherwise the five-second refresh puts the range back to the default
    every time, which reads as the page arguing with you."""
    client, _, _ = setup_client(tmp_path)

    page = client.get("/dashboard?days=90")
    swapped = client.get("/dashboard/stats?days=90")

    assert 'hx-get="/dashboard/stats?days=90"' in page.text
    assert 'hx-get="/dashboard/stats?days=90"' in swapped.text
    #  And a range nobody offers falls back to the default rather than being
    #  honoured — a query string is somebody's typing, not an API.
    assert 'hx-get="/dashboard/stats?days=30"' in client.get("/dashboard?days=4000").text


def test_dashboard_leads_with_the_thing_that_wants_you(tmp_path: Path) -> None:
    client, conn, _ = setup_client(tmp_path)

    calm = client.get("/dashboard/stats")
    conn.execute(
        """
        INSERT INTO items(
          id, root, relpath, size, mtime_ns, fingerprint, first_seen_at, last_seen_at
        )
        VALUES (1, 'inbox', 'a.mkv', 1, 1, 'fp', 'now', 'now')
        """
    )
    upsert_proposal(
        conn,
        item_id=1,
        category="movies",
        clean_name="a.mkv",
        dest_relpath="Movies/A.mkv",
        confidence=0.9,
        evidence=[EvidenceEntry("heuristic", "category", "extension", 0.9)],
    )
    busy = client.get("/dashboard/stats")

    assert "Nothing needs you" in calm.text
    assert "waiting for your review" in busy.text
    assert 'href="/review"' in busy.text
    assert "Nothing needs you" not in busy.text


def test_dashboard_offers_commit_when_the_queue_is_already_approved(tmp_path: Path) -> None:
    """Approved-and-uncommitted is its own state: nothing to review, but the
    files are still sitting in the inbox until you commit."""
    client, conn, _ = setup_client(tmp_path)
    conn.execute(
        """
        INSERT INTO items(
          id, root, relpath, size, mtime_ns, fingerprint, first_seen_at, last_seen_at
        )
        VALUES (1, 'inbox', 'a.mkv', 1, 1, 'fp', 'now', 'now')
        """
    )
    upsert_proposal(
        conn,
        item_id=1,
        category="movies",
        clean_name="a.mkv",
        dest_relpath="Movies/A.mkv",
        confidence=0.9,
        evidence=[EvidenceEntry("heuristic", "category", "extension", 0.9)],
    )
    conn.execute("UPDATE proposals SET status='approved'")

    response = client.get("/dashboard/stats")

    assert "ready to commit" in response.text
    assert 'href="/commit"' in response.text


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
    assert "<span>queued</span><strong>1</strong>" in response.text


def _counts(conn) -> dict[str, int]:
    tables = ["items", "proposals", "history", "worker_state", "provider_status"]
    return {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables}
