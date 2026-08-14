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
        AUTH_REQUIRED=True,
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
    assert "Nothing needs you" in client.get("/dashboard").text

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


def test_the_dashboard_says_how_much_is_waiting_to_commit(tmp_path: Path) -> None:
    """Commit is a step *and* a place now.

    This asserted that the word did not appear at all while the queue was
    empty. That was right when the dashboard was a single hero: an idle Commit
    was noise. It is wrong for an operations overview, whose whole job is to
    say where the work is — and "Commit 0" is an answer to that question. The
    nav keeps Commit at zero too, decided in the pass before this one: a tab
    that vanishes when idle cannot be navigated to on purpose.
    """
    client, conn = client_for(tmp_path)
    client.post("/setup", data={"password": "correct horse battery"})

    quiet = client.get("/dashboard").text
    conn.execute(
        """
        INSERT INTO items(id, root, relpath, size, mtime_ns, fingerprint,
                          first_seen_at, last_seen_at)
        VALUES (1, 'inbox', 'a.mkv', 1, 1, 'fp', 'now', 'now')
        """
    )
    conn.execute(
        """
        INSERT INTO proposals(item_id, category, clean_name, dest_relpath, confidence,
                              status, evidence, created_at, updated_at)
        VALUES (1, 'movies', 'a.mkv', 'Movies/a.mkv', 0.9, 'approved', '[]', 'now', 'now')
        """
    )
    waiting = client.get("/dashboard").text

    assert '<span class="surface-count">0</span>' in quiet
    assert 'href="/commit"' in quiet
    assert 'href="/commit"' in waiting
    assert "nav-count" in waiting
    # And it is always reachable while you are on it, count or not.
    assert 'href="/commit"' in client.get("/commit").text


def test_the_rarely_used_pages_move_behind_one_more_menu(tmp_path: Path) -> None:
    """Nine equally-weighted tabs, five of them monthly at most."""
    client, _ = client_for(tmp_path)
    client.post("/setup", data={"password": "correct horse battery"})

    page = client.get("/dashboard").text

    assert "nav-more" in page
    for href in ("/quarantine", "/history", "/health", "/access"):
        assert f'href="{href}"' in page


def test_static_assets_are_revalidated_not_guessed(tmp_path: Path) -> None:
    """StaticFiles sends an ETag but no Cache-Control, which leaves the browser
    free to invent a lifetime. Pull a new image and a returning tab keeps the
    old stylesheet against the new HTML — an update that appears to do nothing
    and then fixes itself hours later, so it never gets reported."""
    client, _conn = client_for(tmp_path)

    response = client.get("/static/pipboy.css")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers.get("etag"), "revalidation needs a validator to be cheap"


def test_drawing_a_page_never_writes_to_the_database(tmp_path: Path) -> None:
    """The site header mirrored every AI provider into provider_status as a
    side effect of being rendered. A page view that collides with the worker
    holding the write lock is then a 500 on whatever page you were reading —
    seen live as "System Fault" on Review during a scan.
    """
    client, conn = client_for(tmp_path)
    client.post("/setup", data={"password": "correct horse battery"})
    writes: list[str] = []

    def watch(sql: str) -> None:
        if sql.lstrip()[:6].upper() in {"INSERT", "UPDATE", "DELETE"}:
            writes.append(" ".join(sql.split())[:90])

    conn.set_trace_callback(watch)
    try:
        for page in ("/dashboard", "/review", "/browse", "/health", "/quarantine"):
            assert client.get(page).status_code == 200, page
    finally:
        conn.set_trace_callback(None)

    # Touching the session is the one legitimate write on a GET.
    unexpected = [sql for sql in writes if "sessions" not in sql.lower()]
    assert unexpected == [], f"rendering wrote to the database: {unexpected}"
