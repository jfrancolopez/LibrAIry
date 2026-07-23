from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from librairy.config import Settings
from librairy.db import connect
from librairy.web.app import create_app
from librairy.web.auth import SESSION_COOKIE, hash_password, verify_password


def client_for(tmp_path: Path, *, auth_required: bool = True) -> tuple[TestClient, object]:
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata", AUTH_REQUIRED=auth_required, _env_file=None
    )
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


def test_plain_logout_form_posts_csrf_field(tmp_path: Path) -> None:
    client, _ = client_for(tmp_path)
    client.post("/setup", data={"password": "correct horse battery"})

    page = client.get("/dashboard")
    response = client.post(
        "/logout",
        data={"csrf_token": client.cookies["csrf_token"]},
        follow_redirects=False,
    )

    assert 'name="csrf_token"' in page.text
    assert response.status_code == 302
    assert response.headers["location"] == "/login"


def test_open_portal_serves_pages_without_a_password(tmp_path: Path) -> None:
    client, conn = client_for(tmp_path, auth_required=False)

    assert client.get("/", follow_redirects=False).headers["location"] == "/dashboard"
    dashboard = client.get("/dashboard", follow_redirects=False)

    assert dashboard.status_code == 200
    assert SESSION_COOKIE in client.cookies
    assert client.get("/login", follow_redirects=False).headers["location"] == "/dashboard"
    assert "[OK] LOGOUT" not in dashboard.text
    assert (
        conn.execute("SELECT 1 FROM settings WHERE key='auth.admin_password'").fetchone() is None
    )


def test_open_portal_still_rejects_cross_site_posts(tmp_path: Path) -> None:
    client, _ = client_for(tmp_path, auth_required=False)
    client.get("/dashboard")

    assert client.post("/csrf-check").status_code == 403
    assert client.post(
        "/csrf-check", headers={"x-csrf-token": client.cookies["csrf_token"]}
    ).json() == {"status": "ok"}


def test_password_set_from_settings_locks_the_portal_and_removal_reopens_it(
    tmp_path: Path,
) -> None:
    client, conn = client_for(tmp_path, auth_required=False)
    client.get("/dashboard")
    csrf = client.cookies["csrf_token"]

    set_response = client.post(
        "/settings/password",
        data={
            "csrf_token": csrf,
            "new_password": "correct horse battery",
            "confirm_password": "correct horse battery",
        },
        follow_redirects=False,
    )
    client.cookies.delete(SESSION_COOKIE)

    assert set_response.status_code == 302
    assert client.get("/dashboard", follow_redirects=False).headers["location"] == "/login"

    client.post("/login", data={"password": "correct horse battery"})
    removed = client.post(
        "/settings/password/remove",
        data={"csrf_token": client.cookies["csrf_token"], "current_password": "wrong"},
    )

    assert removed.status_code == 422
    assert "current password is incorrect" in removed.text

    client.post(
        "/settings/password/remove",
        data={
            "csrf_token": client.cookies["csrf_token"],
            "current_password": "correct horse battery",
        },
    )
    client.cookies.delete(SESSION_COOKIE)

    assert (
        conn.execute("SELECT 1 FROM settings WHERE key='auth.admin_password'").fetchone() is None
    )
    assert client.get("/dashboard", follow_redirects=False).status_code == 200


def test_auth_required_refuses_password_removal(tmp_path: Path) -> None:
    client, _ = client_for(tmp_path, auth_required=True)
    client.post("/setup", data={"password": "correct horse battery"})

    response = client.post(
        "/settings/password/remove",
        data={
            "csrf_token": client.cookies["csrf_token"],
            "current_password": "correct horse battery",
        },
    )

    assert response.status_code == 422
    assert "cannot be removed" in response.text
    assert client.get("/dashboard").status_code == 200


def test_password_change_requires_current_password_and_confirmation(tmp_path: Path) -> None:
    client, _ = client_for(tmp_path, auth_required=False)
    client.post("/setup", data={"password": "correct horse battery"})
    csrf = client.cookies["csrf_token"]

    wrong_current = client.post(
        "/settings/password",
        data={
            "csrf_token": csrf,
            "current_password": "nope",
            "new_password": "another good one",
            "confirm_password": "another good one",
        },
    )
    mismatch = client.post(
        "/settings/password",
        data={
            "csrf_token": csrf,
            "current_password": "correct horse battery",
            "new_password": "another good one",
            "confirm_password": "typo here too",
        },
    )
    changed = client.post(
        "/settings/password",
        data={
            "csrf_token": csrf,
            "current_password": "correct horse battery",
            "new_password": "another good one",
            "confirm_password": "another good one",
        },
        follow_redirects=False,
    )
    client.cookies.delete(SESSION_COOKIE)

    assert wrong_current.status_code == 422
    assert mismatch.status_code == 422
    assert "do not match" in mismatch.text
    assert changed.status_code == 302
    assert (
        client.post(
            "/login", data={"password": "another good one"}, follow_redirects=False
        ).headers["location"]
        == "/dashboard"
    )


def test_scrypt_params_are_stored_and_verifiable_after_parameter_change() -> None:
    stored = hash_password("secret phrase", n=2**14, r=8, p=1)
    stored["n"] = json.loads(json.dumps(stored))["n"]

    assert stored["algorithm"] == "scrypt"
    assert verify_password("secret phrase", stored) is True
    assert verify_password("wrong", stored) is False
