from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from librairy.config import Settings
from librairy.db import connect
from librairy.lifecycle import forget_vanished, vanished_count
from librairy.proposals import upsert_proposal
from librairy.web.app import create_app


def client_for(tmp_path: Path) -> tuple[TestClient, object, Settings]:
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        _env_file=None,
    )
    for root in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        root.mkdir(parents=True)
    conn = connect(settings)
    client = TestClient(create_app(settings, conn))
    client.post("/setup", data={"password": "correct horse battery"})
    return client, conn, settings


def seed(conn, relpath: str, *, status: str, gone: bool) -> int:
    cursor = conn.execute(
        """
        INSERT INTO items(root, relpath, size, mtime_ns, fingerprint, state,
                          first_seen_at, last_seen_at, missing_since)
        VALUES ('inbox', ?, 10, 1, ?, ?, 'now', 'now', ?)
        """,
        (relpath, relpath, "approved" if status == "approved" else "proposed",
         "2026-01-01T00:00:00+00:00" if gone else None),
    )
    item_id = int(cursor.lastrowid)
    upsert_proposal(
        conn,
        item_id=item_id,
        category="shows",
        clean_name=Path(relpath).name,
        dest_root="library",
        dest_relpath=f"Shows/Test/Season-01/{Path(relpath).name}",
        confidence=0.9,
        evidence=[],
    )
    conn.execute("UPDATE proposals SET status=? WHERE item_id=?", (status, item_id))
    return item_id


def test_a_file_deleted_outside_librairy_leaves_review(tmp_path: Path) -> None:
    """Approving one produces a commit operation that cannot run.

    The scanner already records missing_since; nothing was asking it, so the
    proposal sat in Review offering a decision nobody could act on.
    """
    client, conn, _ = client_for(tmp_path)
    seed(conn, "here.mkv", status="proposed", gone=False)
    seed(conn, "gone.mkv", status="proposed", gone=True)

    page = client.get("/review").text

    assert "here.mkv" in page
    assert "gone.mkv" not in page
    assert "1 file moved or deleted outside LibrAIry" in page


def test_commit_will_not_build_a_plan_around_a_missing_file(tmp_path: Path) -> None:
    """"See exactly what will move" answered with a raw JSON "source not ready"."""
    client, conn, _ = client_for(tmp_path)
    seed(conn, "gone.mkv", status="approved", gone=True)

    page = client.get("/commit").text
    created = client.post(
        "/commit/create",
        data={"csrf_token": client.cookies["csrf_token"]},
        headers={"x-csrf-token": client.cookies["csrf_token"]},
        follow_redirects=False,
    )

    assert "Nothing is approved yet" in page
    assert created.status_code != 422


def test_forget_clears_the_rows_and_never_touches_a_file(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    seed(conn, "here.mkv", status="proposed", gone=False)
    seed(conn, "gone.mkv", status="approved", gone=True)
    survivor = settings.inbox_dir / "here.mkv"
    survivor.write_text("still here", encoding="utf-8")

    assert vanished_count(conn) == 1
    assert forget_vanished(conn) == 1

    assert vanished_count(conn) == 0
    assert survivor.exists()
    assert conn.execute(
        "SELECT COUNT(*) FROM proposals WHERE status='proposed'"
    ).fetchone()[0] == 1


def test_an_unmountable_disk_is_not_cleared_on_its_own(tmp_path: Path) -> None:
    """Clearing automatically would throw away a whole volume's decisions."""
    client, conn, _ = client_for(tmp_path)
    seed(conn, "gone.mkv", status="proposed", gone=True)

    client.get("/review")
    client.get("/commit")

    assert vanished_count(conn) == 1, "looking at a page must not decide anything"


def test_a_refused_action_in_a_browser_is_a_page_not_json(tmp_path: Path) -> None:
    client, _, _ = client_for(tmp_path)

    csrf = client.cookies["csrf_token"]
    path = "/settings/providers/order/openai/sideways"
    as_browser = client.post(path, headers={"accept": "text/html", "x-csrf-token": csrf})
    as_api = client.post(path, headers={"accept": "application/json", "x-csrf-token": csrf})

    assert as_browser.status_code == 422
    assert "<html" in as_browser.text
    assert "direction must be up or down" in as_browser.text
    assert as_api.json()["detail"] == "direction must be up or down"
