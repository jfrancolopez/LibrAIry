from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from librairy.config import Settings
from librairy.db import connect
from librairy.web.app import create_app


def client_for(tmp_path: Path) -> tuple[TestClient, object, Settings]:
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        HOST_LIBRARY_DIR=Path("/mnt/user/media/library"),
        _env_file=None,
    )
    for path in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        path.mkdir(parents=True)
    conn = connect(settings)
    client = TestClient(create_app(settings, conn))
    client.post("/setup", data={"password": "correct horse battery"})
    return client, conn, settings


def test_access_page_substitutes_host_path_and_disclaims_protocols(tmp_path: Path) -> None:
    client, _, _ = client_for(tmp_path)

    response = client.get("/access")

    assert response.status_code == 200
    assert "/mnt/user/media/library" in response.text
    assert "LibrAIry does not serve SMB, FTP, WebDAV" in response.text
    assert "\\\\TOWER\\library" in response.text
    assert "smb://TOWER/library" in response.text


def test_access_page_is_linked_from_browse(tmp_path: Path) -> None:
    client, _, _ = client_for(tmp_path)

    response = client.get("/browse")

    assert response.status_code == 200
    assert 'href="/access"' in response.text


def test_access_page_is_linked_from_item_detail(tmp_path: Path) -> None:
    client, conn, _ = client_for(tmp_path)
    item_id = conn.execute(
        """
        INSERT INTO items(
          root, relpath, size, mtime_ns, fingerprint, state, first_seen_at, last_seen_at
        )
        VALUES ('library', 'Documents/a.txt', 1, 1, 'fp', 'discovered', 'now', 'now')
        """
    ).lastrowid

    response = client.get(f"/items/{item_id}")

    assert response.status_code == 200
    assert 'href="/access"' in response.text
