from __future__ import annotations

from pathlib import Path

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from librairy.config import Settings
from librairy.db import connect
from librairy.web.app import EXEMPT_PATHS, create_app
from librairy.web.auth import SESSION_COOKIE


def client_for(tmp_path: Path) -> tuple[TestClient, object]:
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        _env_file=None,
    )
    conn = connect(settings)
    return TestClient(create_app(settings, conn)), conn


def test_route_protection_sweep_auto_discovers_protected_routes(tmp_path: Path) -> None:
    client, _ = client_for(tmp_path)
    public_routes = {"/", "/setup", "/login", "/healthz"}
    assert public_routes == EXEMPT_PATHS

    for route in client.app.routes:
        if not isinstance(route, APIRoute) or route.path in public_routes:
            continue
        path = _sample_path(route.path)
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            response = client.request(method, path, follow_redirects=False)
            assert response.status_code == 302, f"{method} {route.path} is not protected"
            assert response.headers["location"] == "/login"


def test_full_browser_flow_setup_login_dashboard_logout_blocked(tmp_path: Path) -> None:
    client, conn = client_for(tmp_path)

    assert client.get("/", follow_redirects=False).headers["location"] == "/setup"
    setup = client.post(
        "/setup", data={"password": "correct horse battery"}, follow_redirects=False
    )
    assert setup.headers["location"] == "/dashboard"
    assert SESSION_COOKIE in client.cookies
    assert "proposals staged" in client.get("/dashboard").text

    csrf = client.cookies["csrf_token"]
    logout = client.post("/logout", headers={"x-csrf-token": csrf}, follow_redirects=False)
    assert logout.headers["location"] == "/login"
    assert client.get("/dashboard", follow_redirects=False).headers["location"] == "/login"

    login = client.post(
        "/login", data={"password": "correct horse battery"}, follow_redirects=False
    )
    assert login.headers["location"] == "/dashboard"
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1


def _sample_path(path: str) -> str:
    return path.replace("{name}", "missing-provider")
