from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from librairy.config import Settings
from librairy.db import connect
from librairy.web.app import create_app


def client_for(tmp_path: Path, *, auth_required: bool = True) -> TestClient:
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        HOST_INBOX_DIR=Path("/mnt/user/dropbox/inbox"),
        AUTH_REQUIRED=auth_required,
        _env_file=None,
    )
    for path in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        path.mkdir(parents=True)
    client = TestClient(create_app(settings, connect(settings)))
    return client


def test_setup_screen_explains_first_run(tmp_path: Path) -> None:
    response = client_for(tmp_path).get("/setup")

    assert response.status_code == 200
    assert "protects the LAN web portal" in response.text
    assert "Initialize Secure Portal" in response.text


def test_setup_screen_offers_to_skip_when_password_is_optional(tmp_path: Path) -> None:
    response = client_for(tmp_path, auth_required=False).get("/setup")

    assert response.status_code == 200
    assert "A password is optional" in response.text
    assert "Skip — continue without a password" in response.text


def test_first_visit_banner_dismissal_survives_logout_login(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    client.post("/setup", data={"password": "correct horse battery"})

    first = client.get("/dashboard")
    dismissed = client.post(
        "/welcome/dismiss",
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )
    second = client.get("/dashboard")
    client.post("/logout", headers={"x-csrf-token": client.cookies["csrf_token"]})
    client.post("/login", data={"password": "correct horse battery"})
    after_login = client.get("/dashboard")

    assert 'id="welcome-banner"' in first.text
    assert dismissed.status_code == 200
    assert 'id="welcome-banner"' not in second.text
    assert 'id="welcome-banner"' not in after_login.text


def test_fresh_install_empty_states_are_purposeful(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    client.post("/setup", data={"password": "correct horse battery"})
    headers = {"x-csrf-token": client.cookies["csrf_token"]}
    client.post("/welcome/dismiss", headers=headers)

    dashboard = client.get("/dashboard")
    review = client.get("/review")
    search = client.get("/search")
    browse = client.get("/browse")
    quarantine = client.get("/quarantine")
    history = client.get("/history")

    assert "/mnt/user/dropbox/inbox" in dashboard.text
    assert "drop files into the inbox" in review.text
    assert "Search is ready" in search.text
    assert "browse is empty" in browse.text
    assert "Quarantine is reversible" in quarantine.text
    assert "Every committed filesystem operation" in history.text


def test_logout_control_renders_in_authenticated_header(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    client.post("/setup", data={"password": "correct horse battery"})

    dashboard = client.get("/dashboard")
    settings = client.get("/settings")
    logout = client.post(
        "/logout",
        headers={"x-csrf-token": client.cookies["csrf_token"]},
        follow_redirects=False,
    )

    assert dashboard.text.count('action="/logout"') == 1
    assert settings.text.count('action="/logout"') == 1
    assert "Log out" in dashboard.text
    assert "<button type=\"submit\">Logout</button>" not in dashboard.text
    assert logout.status_code == 302
    assert logout.headers["location"] == "/login"
