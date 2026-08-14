"""Library Audit in Review, and on the command line.

The load-bearing test here is the last one in the first section: an inbox bulk
action must not be able to reach a library finding. It is guaranteed by
structure — findings live in their own table, and the section renders outside
the inbox form — and this asserts both halves, because a guarantee nobody
checks is a comment.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from librairy.audit import audit_library
from librairy.config import Settings
from librairy.db import connect
from librairy.models import EvidenceEntry
from librairy.proposals import upsert_proposal
from librairy.scanner import scan_root
from librairy.web.app import create_app


def env_for(tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "APPDATA_DIR": str(tmp_path / "appdata"),
            "INBOX_DIR": str(tmp_path / "inbox"),
            "LIBRARY_DIR": str(tmp_path / "library"),
            "QUARANTINE_DIR": str(tmp_path / "quarantine"),
            "FILE_STABILITY_SECONDS": "0",
            "AI_ENABLED": "false",
        }
    )
    return env


def run_cli(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "librairy", *args],
        env=env_for(tmp_path),
        text=True,
        capture_output=True,
        check=False,
    )


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


def post(client, path: str, **data):
    """Every write goes through CSRF, the audit routes included."""
    client.get("/review")
    token = client.cookies["csrf_token"]
    return client.post(
        path,
        data={**data, "csrf_token": token},
        headers={"x-csrf-token": token},
        follow_redirects=False,
    )


def scene(tmp_path: Path):
    """One inbox proposal waiting, and one library file with a problem."""
    settings = settings_for(tmp_path)
    conn = connect(settings)

    (settings.inbox_dir / "new-arrival.mkv").write_text("arriving", encoding="utf-8")
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    item_id = conn.execute("SELECT id FROM items WHERE root='inbox'").fetchone()["id"]
    upsert_proposal(
        conn,
        item_id=item_id,
        category="shows",
        clean_name="new-arrival.mkv",
        dest_relpath="Shows/new-arrival.mkv",
        confidence=0.95,
        evidence=[EvidenceEntry("tvmaze", "show", "Best Shot", 0.95)],
    )
    # So the approve-all below really approves something: the point is that a
    # successful inbox action still leaves every library finding alone.
    conn.execute("UPDATE items SET state='proposed' WHERE id=?", (item_id,))

    for relpath in (
        "Music/Rock/Queen/A Night at the Opera/01 - Bohemian Rhapsody.mp3",
        "Music/Rock/Queen/A Night at the Opera/cover.jpg",
        "Music/Rock/Queen/tax-return.pdf",
    ):
        path = settings.library_dir / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relpath, encoding="utf-8")
    scan_root(conn, "library", settings.library_dir, settings)
    audit_library(conn, settings, read_tags=False)

    client = TestClient(create_app(settings, conn))
    return client, conn, settings


# --- the Review page ----------------------------------------------------------


def test_the_audit_section_renders_separately_from_the_inbox_queue(tmp_path: Path) -> None:
    client, _, _ = scene(tmp_path)

    html = client.get("/review").text

    assert 'id="library-audit"' in html
    assert "Library Review" in html
    assert "new-arrival.mkv" in html, "the inbox queue is still there"


def test_every_audit_row_says_so_in_words(tmp_path: Path) -> None:
    """Colour reinforces; it never carries the meaning alone."""
    client, _, _ = scene(tmp_path)

    html = client.get("/review").text
    rows = re.findall(r'class="row-shell audit-row[^"]*">(.*?)</article>', html, flags=re.S)

    assert rows
    for row in rows:
        assert "EXISTING LIBRARY" in row


def test_current_and_suggested_are_distinguishable(tmp_path: Path) -> None:
    client, _, _ = scene(tmp_path)

    html = client.get("/review").text

    assert "row-dest" in html and "dest-arrow" in html
    assert "Music/Rock/Queen/tax-return.pdf" in html


def test_an_observation_does_not_pretend_to_have_a_destination(tmp_path: Path) -> None:
    """`tax-return.pdf` has nowhere to go, so no Suggested line at all."""
    client, _, _ = scene(tmp_path)

    html = client.get("/review").text
    section = html.split('id="library-audit"', 1)[1]

    assert "<dt>Suggested</dt>" not in section


def test_inbox_bulk_actions_cannot_reach_a_library_finding(tmp_path: Path) -> None:
    """The one that matters. "Approve all confident" acts on checkboxes inside
    the inbox form; the audit section is outside it and its rows carry no
    proposal checkbox at all."""
    client, conn, _ = scene(tmp_path)

    html = client.get("/review").text
    audit_section = html.split('id="library-audit"', 1)[1]
    assert 'name="proposal_ids"' not in audit_section
    assert "review/action" not in audit_section, "no route into the inbox toolbar"

    before = [dict(row) for row in conn.execute("SELECT * FROM audit_findings")]
    response = post(
        client, "/review/action", action="approve", all_matching="true", state="proposed"
    )

    assert response.status_code in (200, 303)
    after = [dict(row) for row in conn.execute("SELECT * FROM audit_findings")]
    assert after == before, "a library finding survived an inbox approve-all untouched"


def test_approving_every_inbox_proposal_does_not_plan_a_library_move(tmp_path: Path) -> None:
    client, conn, _ = scene(tmp_path)

    post(client, "/review/action", action="approve", all_matching="true", state="proposed")

    planned = conn.execute(
        "SELECT COUNT(*) c FROM proposals WHERE status='approved' AND dest_relpath LIKE 'Music/%'"
    ).fetchone()["c"]
    assert planned == 0


def test_keep_as_is_removes_it_from_the_open_list(tmp_path: Path) -> None:
    client, conn, _ = scene(tmp_path)
    finding_id = conn.execute("SELECT id FROM audit_findings").fetchone()["id"]

    response = post(client, f"/review/audit/{finding_id}/keep")

    # Rendered, not redirected: a dismissal that produces no visible trace is
    # indistinguishable from a deletion, and people press it accordingly.
    assert response.status_code == 200
    assert "Suggestion dismissed" in response.text
    assert "can be restored" in response.text
    row = conn.execute(
        "SELECT status FROM audit_findings WHERE id=?", (finding_id,)
    ).fetchone()
    assert row["status"] == "kept"


def test_a_healthy_library_shows_no_audit_section_at_all(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    client = TestClient(create_app(settings, conn))

    body = client.get("/review").text
    # The section stays, as one compact line. A feature that vanishes when it
    # has nothing to say is a feature nobody knows they have.
    #
    # Before any audit has run the line says so rather than claiming a clean
    # bill of health: "no issues" and "nobody has looked" are different, and
    # only one of them is reassuring.
    assert "No audit has run yet" in body
    assert "audit-list" not in body
    assert "audit-toolbar" not in body


def test_the_browse_trigger_audits_and_changes_nothing(tmp_path: Path) -> None:
    """The button asks; the worker answers.

    It used to run the whole reconciliation inside the request. Now it writes
    a row and redirects, so the assertion moves from "findings exist" to
    "findings exist once the worker has had its slices" — and the part that
    has not changed, and must not, is that the library is untouched either
    way.
    """
    from librairy.audit_job import advance

    client, conn, settings = scene(tmp_path)
    conn.execute("DELETE FROM audit_findings")
    before = sorted(
        (path.relative_to(settings.library_dir).as_posix(), path.stat().st_size)
        for path in settings.library_dir.rglob("*")
        if path.is_file()
    )

    response = post(client, "/browse/audit", scope="Music")

    assert response.status_code == 303
    assert conn.execute("SELECT COUNT(*) c FROM audit_runs").fetchone()["c"] == 1
    for _ in range(20):
        if advance(conn, settings).finished:
            break
    assert conn.execute("SELECT COUNT(*) c FROM audit_findings").fetchone()["c"] >= 1
    after = sorted(
        (path.relative_to(settings.library_dir).as_posix(), path.stat().st_size)
        for path in settings.library_dir.rglob("*")
        if path.is_file()
    )
    assert after == before


def test_the_browse_trigger_refuses_to_escape_the_library(tmp_path: Path) -> None:
    client, conn, _ = scene(tmp_path)
    conn.execute("DELETE FROM audit_findings")

    response = post(client, "/browse/audit", scope="../../etc")

    assert response.status_code == 303
    assert response.headers["location"] == "/browse"
    assert conn.execute("SELECT COUNT(*) c FROM audit_findings").fetchone()["c"] == 0


# --- the command line ---------------------------------------------------------


def test_audit_run_reports_what_it_found_and_what_it_moved(tmp_path: Path) -> None:
    scene(tmp_path)

    result = run_cli(tmp_path, "--json", "audit", "run", "--no-tags")

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["files_seen"] == 3
    assert payload["findings"] >= 1
    assert payload["files_moved"] == 0
    assert payload["files_deleted"] == 0


def test_audit_run_takes_a_scope(tmp_path: Path) -> None:
    scene(tmp_path)

    payload = json.loads(
        run_cli(tmp_path, "--json", "audit", "run", "--scope", "Music/Rock", "--no-tags").stdout
    )

    assert payload["scope"] == "Music/Rock"
    assert payload["files_seen"] == 3


def test_audit_list_shows_open_findings(tmp_path: Path) -> None:
    scene(tmp_path)
    run_cli(tmp_path, "audit", "run", "--no-tags")

    payload = json.loads(run_cli(tmp_path, "--json", "audit", "list").stdout)

    assert payload["open"] >= 1
    kinds = {finding["kind"] for finding in payload["findings"]}
    assert "unexpected-file-type" in kinds


def test_audit_is_not_called_scan(tmp_path: Path) -> None:
    """Two different verbs for two different jobs: scan indexes what is there,
    audit asks whether it is in the right place."""
    help_text = run_cli(tmp_path, "audit", "--help").stdout

    assert "librairy audit" in help_text
    assert "run" in help_text and "list" in help_text
