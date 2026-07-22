from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from librairy.config import Settings
from librairy.db import connect
from librairy.web.app import create_app
from librairy.web.auth import SESSION_COOKIE, hash_password, verify_password


def client_for(tmp_path: Path) -> tuple[TestClient, object]:
    settings = Settings(APPDATA_DIR=tmp_path / "appdata", _env_file=None)
    conn = connect(settings)
    return TestClient(create_app(settings, conn)), conn


def test_fresh_db_redirects_to_setup_then_setup_creates_session(tmp_path: Path) -> None:
    client, conn = client_for(tmp_path)

    assert client.get("/", follow_redirects=False).headers["location"] == "/setup"
    response = client.post(
        "/setup", data={"password": "correct horse battery"}, follow_redirects=False
    )

    assert response.status_code == 302
    assert response.headers["location"] == "/dashboard"
    assert SESSION_COOKIE in client.cookies
    row = conn.execute("SELECT value FROM settings WHERE key='auth.admin_password'").fetchone()
    assert row is not None
    assert "correct horse battery" not in row["value"]
    assert client.get("/setup", follow_redirects=False).headers["location"] == "/login"


def test_login_failure_rate_limit_and_success_cookie_is_hashed(tmp_path: Path) -> None:
    client, conn = client_for(tmp_path)
    client.post("/setup", data={"password": "correct horse battery"})
    client.post("/logout", headers={"x-csrf-token": client.cookies["csrf_token"]})

    for _ in range(5):
        assert client.post("/login", data={"password": "wrong"}).status_code == 200
    assert client.post("/login", data={"password": "wrong"}).status_code == 429

    client, conn = client_for(tmp_path / "fresh")
    client.post("/setup", data={"password": "correct horse battery"})
    client.post("/logout", headers={"x-csrf-token": client.cookies["csrf_token"]})
    response = client.post(
        "/login", data={"password": "correct horse battery"}, follow_redirects=False
    )

    assert response.status_code == 302
    token = client.cookies[SESSION_COOKIE]
    row = conn.execute(
        "SELECT token_hash FROM sessions ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    assert row["token_hash"] != token


def test_protected_routes_redirect_and_csrf_required(tmp_path: Path) -> None:
    client, _ = client_for(tmp_path)

    assert client.get("/dashboard", follow_redirects=False).headers["location"] == "/login"
    client.post("/setup", data={"password": "correct horse battery"})

    assert client.get("/dashboard").status_code == 200
    assert client.post("/csrf-check").status_code == 403
    assert client.post(
        "/csrf-check", headers={"x-csrf-token": client.cookies["csrf_token"]}
    ).json() == {"status": "ok"}


def test_logout_invalidates_session_and_expiry_is_honored(tmp_path: Path) -> None:
    client, conn = client_for(tmp_path)
    client.post("/setup", data={"password": "correct horse battery"})
    csrf = client.cookies["csrf_token"]

    assert (
        client.post("/logout", headers={"x-csrf-token": csrf}, follow_redirects=False).status_code
        == 302
    )
    assert client.get("/dashboard", follow_redirects=False).headers["location"] == "/login"

    client.post("/login", data={"password": "correct horse battery"})
    conn.execute("UPDATE sessions SET expires_at='0'")
    assert client.get("/dashboard", follow_redirects=False).headers["location"] == "/login"


def test_scrypt_params_are_stored_and_verifiable_after_parameter_change() -> None:
    stored = hash_password("secret phrase", n=2**14, r=8, p=1)
    stored["n"] = json.loads(json.dumps(stored))["n"]

    assert stored["algorithm"] == "scrypt"
    assert verify_password("secret phrase", stored) is True
    assert verify_password("wrong", stored) is False
