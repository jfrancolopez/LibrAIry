"""Who may start an audit, and with what scope.

Both audit buttons returned 403 in a real browser while every test passed,
because the tests sent an `x-csrf-token` header the browser has no way to
send. So the rule here is: submit the form the way a browser submits it —
read the fields out of the rendered HTML, post those fields, send no header.
A test that supplies the token by a route the UI does not use is testing the
middleware, not the button.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from librairy.config import Settings
from librairy.db import connect
from librairy.scanner import scan_root
from librairy.web.app import create_app

FORM = re.compile(r'<form[^>]*action="/browse/audit".*?</form>', re.S)
FIELD = re.compile(r'name="([^"]+)"\s+value="([^"]*)"')


def settings_for(tmp_path: Path) -> Settings:
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        FILE_STABILITY_SECONDS=0,
        AUTH_REQUIRED=False,
        _env_file=None,
    )
    for root in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        root.mkdir(parents=True, exist_ok=True)
    return settings


def scene(tmp_path: Path):
    settings = settings_for(tmp_path)
    track = settings.library_dir / "Music" / "Rock" / "Queen" / "01 - Bohemian Rhapsody.mp3"
    track.parent.mkdir(parents=True, exist_ok=True)
    track.write_text("audio", encoding="utf-8")
    conn = connect(settings)
    scan_root(conn, "library", settings.library_dir, settings)
    return TestClient(create_app(settings, conn)), conn, settings


def submit_form_on(client, page: str):
    """Press the audit button on a page, exactly as a browser would."""
    html = client.get(page).text
    match = FORM.search(html)
    assert match is not None, f"no audit form on {page}"
    fields = dict(FIELD.findall(match.group(0)))
    assert fields.get("csrf_token"), f"the form on {page} carries no CSRF token"
    return client.post("/browse/audit", data=fields, follow_redirects=False)


def token(client) -> str:
    client.get("/browse")
    return client.cookies["csrf_token"]


def tree(root: Path) -> set[tuple[str, int]]:
    return {
        (str(path.relative_to(root)), path.stat().st_size)
        for path in root.rglob("*")
        if path.is_file()
    }


# --- the button works ---------------------------------------------------------


def test_audit_the_whole_library_is_not_forbidden(tmp_path: Path) -> None:
    client, conn, _ = scene(tmp_path)

    response = submit_form_on(client, "/browse")

    assert response.status_code == 303, response.text
    assert response.headers["location"] == "/review#library-audit"
    assert conn.execute("SELECT count(*) FROM audit_findings").fetchone()[0] >= 0


def test_audit_this_folder_is_not_forbidden(tmp_path: Path) -> None:
    client, _, _ = scene(tmp_path)

    response = submit_form_on(client, "/browse/Music")

    assert response.status_code == 303, response.text


def test_both_buttons_post_the_same_place_with_the_same_semantics(tmp_path: Path) -> None:
    """One route, one guard. The scope is the only thing that differs."""
    client, _, _ = scene(tmp_path)

    whole = FIELD.findall(FORM.search(client.get("/browse").text).group(0))
    folder = FIELD.findall(FORM.search(client.get("/browse/Music").text).group(0))

    assert dict(whole)["scope"] == ""
    assert dict(folder)["scope"] == "Music"
    assert [name for name, _ in whole] == [name for name, _ in folder]


def test_the_posted_scope_actually_reaches_the_handler(tmp_path: Path) -> None:
    """The second bug the first one was hiding.

    The CSRF middleware parsed the body to find the token, which consumed the
    receive stream, and Starlette then replayed an *empty* body to the route.
    So "Audit this folder" arrived with no scope at all and audited the whole
    library — the wrong work, silently, with a success redirect.
    """
    import librairy.web.app as app_module

    settings = settings_for(tmp_path)
    track = settings.library_dir / "Music" / "Rock" / "a.mp3"
    track.parent.mkdir(parents=True, exist_ok=True)
    track.write_text("audio", encoding="utf-8")
    conn = connect(settings)
    scan_root(conn, "library", settings.library_dir, settings)

    seen: list[str] = []
    real = app_module.audit_library

    def spy(conn, settings, *, scope="", read_tags=True):  # noqa: ANN001, ANN202
        seen.append(scope)
        return real(conn, settings, scope=scope, read_tags=read_tags)

    app_module.audit_library = spy
    try:
        client = TestClient(create_app(settings, conn))
        submit_form_on(client, "/browse/Music")
    finally:
        app_module.audit_library = real

    assert seen == ["Music"], "the folder button audited something else"


def test_every_form_on_browse_carries_a_usable_token(tmp_path: Path) -> None:
    """The bug was a hidden field rendering empty, which looks like nothing.

    Any POST form whose token is blank is a button that cannot be pressed, so
    check the whole page rather than the one form that happened to be found.
    """
    client, _, _ = scene(tmp_path)

    for page in ("/browse", "/browse/Music", "/review", "/commit"):
        html = client.get(page).text
        for form in re.findall(r"<form[^>]*method=\"post\"[^>]*>.*?</form>", html, re.S):
            match = re.search(r'name="csrf_token"\s+value="([^"]*)"', form)
            if match is None:
                continue
            assert match.group(1), f"empty CSRF token in a form on {page}"


# --- the scope is still contained ---------------------------------------------


def test_a_traversing_scope_is_refused(tmp_path: Path) -> None:
    client, conn, _ = scene(tmp_path)

    response = client.post(
        "/browse/audit",
        data={"csrf_token": token(client), "scope": "../../etc"},
        follow_redirects=False,
    )

    assert response.headers["location"] == "/browse"
    assert conn.execute("SELECT count(*) FROM audit_findings").fetchone()[0] == 0


def test_an_encoded_traversing_scope_cannot_escape(tmp_path: Path) -> None:
    """Percent-encoding is decoded once, by the form parser, before the scope
    is validated. `%2e%2e` therefore arrives either as `..`, which is refused,
    or as a literal folder name that does not exist. Neither reads outside the
    library, and asserting *that* survives a change of redirect.
    """
    client, conn, _ = scene(tmp_path)
    outside = tmp_path / "secret.txt"
    outside.write_text("not yours", encoding="utf-8")

    for scope in ("%2e%2e/%2e%2e", "..%2F..%2Fetc", "%2E%2E", "Music/%2e%2e/%2e%2e"):
        response = client.post(
            "/browse/audit",
            data={"csrf_token": token(client), "scope": scope},
            follow_redirects=False,
        )
        assert response.status_code == 303, scope

    for row in conn.execute("SELECT relpath FROM audit_findings"):
        assert "secret.txt" not in row["relpath"]
        assert not row["relpath"].startswith("..")


def test_an_absolute_host_path_is_refused(tmp_path: Path) -> None:
    client, conn, _ = scene(tmp_path)

    for scope in ("/etc", "/", "//", str(tmp_path)):
        response = client.post(
            "/browse/audit",
            data={"csrf_token": token(client), "scope": scope},
            follow_redirects=False,
        )
        assert response.headers["location"] in {"/browse", "/review#library-audit"}, scope
    # "/" strips to the empty scope, which is the library — never the host root.
    for row in conn.execute("SELECT relpath FROM audit_findings"):
        assert not row["relpath"].startswith("/")


def test_the_empty_scope_means_the_library_not_the_filesystem_root(tmp_path: Path) -> None:
    from librairy.audit import sanitize_scope

    settings = settings_for(tmp_path)

    assert sanitize_scope("", settings.library_dir) == ""
    assert sanitize_scope("/", settings.library_dir) == ""
    assert sanitize_scope("///", settings.library_dir) == ""


# --- and it is still only reading ---------------------------------------------


def test_the_whole_library_audit_moves_nothing(tmp_path: Path) -> None:
    client, _, settings = scene(tmp_path)
    before = tree(settings.library_dir)

    submit_form_on(client, "/browse")

    assert tree(settings.library_dir) == before


def test_the_whole_library_audit_writes_findings_and_nothing_else(tmp_path: Path) -> None:
    """An audit is analysis. It may not queue work, approve it or commit it."""
    client, conn, settings = scene(tmp_path)
    junk = settings.library_dir / "Music" / "loose.txt"
    junk.write_text("loose", encoding="utf-8")

    submit_form_on(client, "/browse")

    assert conn.execute("SELECT count(*) FROM plans").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM plan_ops").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM history").fetchone()[0] == 0
    assert {row["status"] for row in conn.execute("SELECT status FROM audit_findings")} <= {"open"}


def test_the_audit_route_does_not_loosen_the_rest_of_browse(tmp_path: Path) -> None:
    """Browse stays read-only. The audit POST is the one exception, and it
    is an exception about analysis, not about writing."""
    client, _, _ = scene(tmp_path)
    csrf = token(client)

    for path in ("/browse", "/browse/Music", "/browse/root-files"):
        response = client.post(path, data={"csrf_token": csrf}, follow_redirects=False)
        assert response.status_code == 405, path


def test_csrf_is_still_enforced_on_the_audit_route(tmp_path: Path) -> None:
    client, conn, _ = scene(tmp_path)
    client.get("/browse")

    response = client.post("/browse/audit", data={"scope": ""}, follow_redirects=False)

    assert response.status_code == 403
    assert conn.execute("SELECT count(*) FROM audit_findings").fetchone()[0] == 0


def test_the_audit_route_still_needs_a_login_when_one_is_required(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    settings.auth_required = True
    conn = connect(settings)
    client = TestClient(create_app(settings, conn))

    response = client.post("/browse/audit", data={"scope": ""}, follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == "/login"
    assert conn.execute("SELECT count(*) FROM audit_findings").fetchone()[0] == 0
