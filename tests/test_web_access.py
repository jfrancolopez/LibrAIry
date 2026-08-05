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
    assert "serves no file-sharing protocol of its own" in response.text
    # Per-OS instructions with the real path already filled in, so nobody has
    # to retype it from a screenshot.
    for system in ("UNRAID", "Linux (Samba)", "macOS", "Windows"):
        assert system in response.text
    assert "smb://" in response.text
    assert "net use" in response.text


def test_access_distinguishes_host_paths_from_container_paths(tmp_path: Path) -> None:
    """Pointing Samba at /data/library is the most common setup mistake."""
    client, _, settings = client_for(tmp_path)

    page = client.get("/access").text

    assert "/mnt/user/media/library" in page
    assert str(settings.library_dir) in page
    assert "Host path (use this)" in page
    assert "only exists inside LibrAIry" in page


def test_access_reports_contents_and_which_roots_to_share(tmp_path: Path) -> None:
    client, conn, _ = client_for(tmp_path)
    conn.execute(
        """
        INSERT INTO items(root, relpath, size, mtime_ns, fingerprint, state,
                          first_seen_at, last_seen_at)
        VALUES ('library', 'a.txt', 2048, 1, 'fp', 'discovered', 'now', 'now')
        """
    )

    page = client.get("/access").text

    assert "1 file(s)" in page
    assert "2.0 KB" in page
    assert "shareable" in page
    # appdata holds the database; sharing it is a bad idea.
    assert "keep private" in page
    assert "do not share it" in page


def test_access_reports_the_portal_security_state(tmp_path: Path) -> None:
    client, _, _ = client_for(tmp_path)

    page = client.get("/access").text

    # client_for sets a password during setup.
    assert "Portal password" in page
    assert "PUID:PGID" in page


def test_access_warns_when_the_portal_is_open(tmp_path: Path) -> None:
    from librairy.web.auth import clear_admin_password

    client, conn, _ = client_for(tmp_path)
    clear_admin_password(conn)

    page = client.get("/access").text

    assert "anyone on this network can use the portal" in page


def test_access_size_formatting() -> None:
    from librairy.web.access import human_bytes

    assert human_bytes(0) == "0 B"
    assert human_bytes(900) == "900 B"
    assert human_bytes(1536) == "1.5 KB"


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
