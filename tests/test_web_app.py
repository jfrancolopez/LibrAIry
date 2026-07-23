from __future__ import annotations

from fastapi.testclient import TestClient

from librairy.config import Settings
from librairy.db import connect
from librairy.web.app import create_app


def client_for(tmp_path, *, auth_required: bool = True):
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata", AUTH_REQUIRED=auth_required, _env_file=None
    )
    return TestClient(create_app(settings, connect(settings)))


def test_root_redirects_and_static_assets_load(tmp_path) -> None:
    client = client_for(tmp_path)

    response = client.get("/", follow_redirects=False)
    css = client.get("/static/pipboy.css")
    htmx = client.get("/static/htmx.min.js")

    assert response.status_code == 302
    assert response.headers["location"] == "/setup"
    assert css.status_code == 200
    assert "--phosphor" in css.text
    assert htmx.status_code == 200
    # Guard against the P5-01 placeholder ever shipping again: it disabled every
    # htmx interaction in the browser while server-side tests stayed green.
    assert "placeholder" not in htmx.text
    assert len(htmx.content) > 20000
    assert "onLoad" in htmx.text


def test_setup_shell_has_theme_no_external_assets_and_status_idiom(tmp_path) -> None:
    client = client_for(tmp_path)

    response = client.get("/setup")

    assert response.status_code == 200
    assert "[OK]" in response.text
    assert "scanlines" in response.text
    assert "http://" not in response.text
    assert "https://" not in response.text


def test_autoescape_and_security_headers(tmp_path) -> None:
    client = client_for(tmp_path)
    client.post("/setup", data={"password": "correct horse battery"})

    response = client.get("/missing<script>")

    assert response.status_code == 404
    assert "<script>" not in response.text
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    csp = response.headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "style-src 'self' 'unsafe-inline'" in csp
    assert "script-src" not in csp  # scripts fall back to the strict default-src


def test_healthz_is_minimal_json(tmp_path) -> None:
    client = client_for(tmp_path)

    assert client.get("/healthz").json() == {"status": "ok"}
