"""Commit as a workflow, not as three endpoints that happen to be adjacent.

The report that produced this file: committing a library correction landed on
a nearly empty page containing only

    Execution started
    approved
    pending: 1 / done: 0 / renamed: 0 / changed: 0 / missing: 0 / failed: 0

on the one screen in LibrAIry that moves files.

That was not a template bug. `POST /commit/execute/{plan}` returned
`partials/commit_progress.html` unconditionally. From the confirm screen that
is right — htmx swaps it into place. From "Commit this correction", which is an
ordinary `<form method="post">` with no htmx on it, the browser rendered that
fragment as the entire document: no header, no navigation, no way back.

Two routes had the same shape and the same fault, and one of them was Undo.
"""

from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from librairy.audit import Finding, record_findings
from librairy.config import Settings
from librairy.corrections import accept_correction
from librairy.db import connect
from librairy.models import EvidenceEntry
from librairy.proposals import upsert_proposal
from librairy.scanner import scan_root
from librairy.web.app import create_app

TRACK = "Music/Pop/Queen/05 - Song.flac"
DEST = "Music/Rock/Queen/A Night at the Opera/05 - Song.flac"


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


def scene(tmp_path: Path, *, correction: bool = False, inbox: bool = False):
    settings = settings_for(tmp_path)
    conn = connect(settings)
    path = settings.library_dir / TRACK
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("track bytes", encoding="utf-8")
    if inbox:
        (settings.inbox_dir / "new.txt").write_text("new", encoding="utf-8")
    scan_root(conn, "library", settings.library_dir, settings)
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    if inbox:
        row = conn.execute("SELECT id FROM items WHERE root='inbox'").fetchone()
        upsert_proposal(
            conn,
            item_id=row["id"],
            category="documents",
            clean_name="new.txt",
            dest_relpath="Documents/2026/new.txt",
            confidence=0.95,
            evidence=[EvidenceEntry("filesystem", "extension", ".txt", 0.9)],
        )
        conn.execute("UPDATE proposals SET status='approved'")
    if correction:
        item = conn.execute(
            "SELECT id, fingerprint FROM items WHERE relpath=?", (TRACK,)
        ).fetchone()
        record_findings(
            conn,
            [
                Finding(
                    relpath=TRACK,
                    kind="tag-path-mismatch",
                    severity="high",
                    summary="Tagged 'Queen' but filed under 'Pop'.",
                    dest_relpath=DEST,
                    item_id=item["id"],
                    fingerprint=item["fingerprint"],
                    evidence=[EvidenceEntry("tags", "artist", "Queen", 0.9)],
                )
            ],
        )
        finding_id = conn.execute("SELECT id FROM audit_findings").fetchone()["id"]
        accept_correction(conn, settings, finding_id)
    return TestClient(create_app(settings, conn)), conn, settings


def csrf(client: TestClient) -> dict[str, str]:
    client.get("/commit")
    return {"x-csrf-token": client.cookies["csrf_token"]}


def plan_of(conn) -> str:
    return conn.execute("SELECT plan_id FROM audit_findings").fetchone()["plan_id"]


def wait_for_plan(conn, plan_id: str, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = conn.execute("SELECT status FROM plans WHERE id=?", (plan_id,)).fetchone()
        if row and row["status"] in {"done", "failed"}:
            return
        time.sleep(0.02)
    raise AssertionError("plan never finished")


# --- the raw page -----------------------------------------------------------------


def test_a_plain_form_post_gets_a_page_and_not_a_fragment(tmp_path: Path) -> None:
    """The bug itself, from the exact control that produced it."""
    client, conn, _ = scene(tmp_path, correction=True)
    plan_id = plan_of(conn)

    response = client.post(f"/commit/execute/{plan_id}", headers=csrf(client))

    assert response.status_code == 200
    assert "<!doctype html>" in response.text
    assert 'class="app-nav"' in response.text, "the normal shell, with a way back"


def test_htmx_still_gets_the_fragment_it_swaps(tmp_path: Path) -> None:
    """The confirm screen relies on this, and must not have been broken in
    the course of fixing the other caller."""
    client, conn, _ = scene(tmp_path, correction=True)
    plan_id = plan_of(conn)

    response = client.post(
        f"/commit/execute/{plan_id}",
        headers={**csrf(client), "HX-Request": "true"},
    )

    assert "<!doctype html>" not in response.text
    assert response.text.lstrip().startswith('<div id="commit-progress"')


def test_the_progress_page_survives_a_reload(tmp_path: Path) -> None:
    """Reachable by refresh, by bookmark, and by anyone who lost the tab."""
    client, conn, _ = scene(tmp_path, correction=True)
    plan_id = plan_of(conn)

    response = client.get(f"/commit/progress/{plan_id}")

    assert 'class="app-nav"' in response.text


def test_undo_from_a_plain_form_gets_a_page_too(tmp_path: Path) -> None:
    """The same fault, on the route that had just moved somebody's files back."""
    client, conn, _ = scene(tmp_path, correction=True)
    plan_id = plan_of(conn)
    client.post(f"/commit/execute/{plan_id}", headers=csrf(client))
    wait_for_plan(conn, plan_id)

    response = client.post(f"/history/plans/{plan_id}/undo", headers=csrf(client))

    assert "<!doctype html>" in response.text
    assert 'class="app-nav"' in response.text


def test_the_execution_page_names_what_is_moving_not_a_plan_id(tmp_path: Path) -> None:
    """A UUID is the one thing on this screen nobody deciding anything needs."""
    client, conn, _ = scene(tmp_path, correction=True)
    plan_id = plan_of(conn)

    response = client.post(f"/commit/execute/{plan_id}", headers=csrf(client))

    assert "Applying correction" in response.text
    assert "05 - Song.flac" in response.text
    assert plan_id not in response.text.split("</header>", 1)[0]


# --- what the progress actually says -------------------------------------------------


def test_a_finished_commit_reads_as_a_result(tmp_path: Path) -> None:
    client, conn, settings = scene(tmp_path, correction=True)
    plan_id = plan_of(conn)
    client.post(f"/commit/execute/{plan_id}", headers=csrf(client))
    wait_for_plan(conn, plan_id)

    body = client.get(f"/commit/progress/{plan_id}").text

    assert "1 file moved" in body
    assert "Nothing failed" in body
    assert (settings.library_dir / DEST).is_file()


def test_outcomes_that_did_not_happen_are_not_listed(tmp_path: Path) -> None:
    """Four permanent zeroes taught people to read past the line, including
    the times it was not zero."""
    client, conn, _ = scene(tmp_path, correction=True)
    plan_id = plan_of(conn)
    client.post(f"/commit/execute/{plan_id}", headers=csrf(client))
    wait_for_plan(conn, plan_id)

    body = client.get(f"/commit/progress/{plan_id}").text

    for absent in ("renamed:", "changed:", "missing:", "failed:", "pending:"):
        assert absent not in body


def test_a_finished_plan_stops_polling_itself(tmp_path: Path) -> None:
    client, conn, _ = scene(tmp_path, correction=True)
    plan_id = plan_of(conn)
    client.post(f"/commit/execute/{plan_id}", headers=csrf(client))
    wait_for_plan(conn, plan_id)

    body = client.get(f"/commit/progress/{plan_id}").text

    assert "hx-trigger" not in body.split('id="commit-progress"', 1)[1].split(">", 1)[0]


def test_a_completed_commit_offers_history_browse_and_undo(tmp_path: Path) -> None:
    client, conn, _ = scene(tmp_path, correction=True)
    plan_id = plan_of(conn)
    client.post(f"/commit/execute/{plan_id}", headers=csrf(client))
    wait_for_plan(conn, plan_id)

    body = client.get(f"/commit/progress/{plan_id}").text

    assert f'href="/history/plans/{plan_id}"' in body
    assert 'href="/browse"' in body
    # The one undo, the one History uses. There is not a second reversal.
    assert f'action="/history/plans/{plan_id}/undo"' in body


# --- the Commit page itself ------------------------------------------------------------


def test_commit_stays_in_the_navigation_with_nothing_waiting(tmp_path: Path) -> None:
    """It used to appear only when something was approved, so committing the
    last item made the destination vanish underneath the person using it."""
    client, _, _ = scene(tmp_path)

    body = client.get("/review").text

    assert 'href="/commit"' in body


def test_the_navigation_count_includes_library_corrections(tmp_path: Path) -> None:
    """It counted inbox proposals only, so a correction approved on its own
    left no badge and no tab — the same "nothing happened", elsewhere."""
    client, _, _ = scene(tmp_path, correction=True)

    body = client.get("/review").text
    nav = body.split('class="app-nav"', 1)[1].split("</nav>", 1)[0]

    assert 'class="nav-count">1<' in nav


def test_the_empty_state_says_what_would_appear_here(tmp_path: Path) -> None:
    client, _, _ = scene(tmp_path)

    body = client.get("/commit").text

    assert "Nothing waiting to commit" in body
    assert "Approved changes appear here before LibrAIry moves anything" in body
    assert 'href="/review#review-list"' in body
    assert 'href="/review#library-audit"' in body


def test_new_files_and_library_corrections_are_never_one_list(tmp_path: Path) -> None:
    """Different promises. One of these moves something the owner already
    had."""
    client, _, _ = scene(tmp_path, correction=True, inbox=True)

    body = client.get("/commit").text

    assert body.index(">New files<") < body.index(">Library corrections<")


def test_a_correction_shows_current_and_proposed_before_the_button(
    tmp_path: Path,
) -> None:
    client, _, _ = scene(tmp_path, correction=True)

    body = client.get("/commit").text

    assert "Current" in body
    assert "After Commit" in body
    assert TRACK in body
    assert DEST in body
    assert "Affects" in body


def test_a_correction_can_be_sent_back_before_it_runs(tmp_path: Path) -> None:
    client, conn, settings = scene(tmp_path, correction=True)
    finding_id = conn.execute("SELECT id FROM audit_findings").fetchone()["id"]

    assert "Send back to Review" in client.get("/commit").text

    client.post(f"/review/audit/{finding_id}/unapprove", headers=csrf(client))

    assert (
        conn.execute("SELECT status FROM audit_findings").fetchone()["status"] == "open"
    )
    assert (settings.library_dir / TRACK).is_file(), "nothing moved"


def test_inbox_approvals_can_be_sent_back_before_they_run(tmp_path: Path) -> None:
    client, conn, settings = scene(tmp_path, inbox=True)

    assert "Send all back to Review" in client.get("/commit").text

    client.post("/commit/unapprove", headers=csrf(client))

    assert conn.execute("SELECT status FROM proposals").fetchone()["status"] == "proposed"
    assert (settings.inbox_dir / "new.txt").is_file(), "nothing moved"


def test_the_pre_commit_reversal_is_never_called_undo(tmp_path: Path) -> None:
    """Before execution and after execution are different actions. One word
    for both is how somebody comes to believe Undo will rescue a commit they
    never made — or hesitates to press the one that would."""
    client, _, _ = scene(tmp_path, correction=True, inbox=True)

    body = client.get("/commit").text
    waiting = body.split(">Library corrections<", 1)[1].split("commit-last", 1)[0]

    assert "Send back to Review" in waiting
    assert ">Undo<" not in waiting


def test_the_last_result_stays_visible_after_the_queue_empties(tmp_path: Path) -> None:
    """Committing the last item replaced the page with "nothing approved",
    which is true and reads as though it never happened."""
    client, conn, _ = scene(tmp_path, correction=True)
    plan_id = plan_of(conn)
    client.post(f"/commit/execute/{plan_id}", headers=csrf(client))
    wait_for_plan(conn, plan_id)

    body = client.get("/commit").text

    assert "Last completed" in body
    assert "1 file moved" in body
    assert f'href="/history/plans/{plan_id}"' in body


def test_the_last_result_offers_the_existing_undo(tmp_path: Path) -> None:
    client, conn, _ = scene(tmp_path, correction=True)
    plan_id = plan_of(conn)
    client.post(f"/commit/execute/{plan_id}", headers=csrf(client))
    wait_for_plan(conn, plan_id)

    body = client.get("/commit").text

    assert f'action="/history/plans/{plan_id}/undo"' in body


def test_no_route_answers_a_browser_with_a_bare_fragment() -> None:
    """The invariant, rather than three separate fixes.

    Any route that renders a `partials/` template has to be reachable only by
    htmx, or has to choose. These three are reachable from ordinary forms.
    """
    source = Path("src/librairy/web/app.py").read_text(encoding="utf-8")

    for route in ("/commit/execute/", "/commit/progress/", "/history/plans/{plan_id}/undo"):
        block = source.split(f'"{route}', 1)[1].split("\n    @app.", 1)[0]
        assert "_is_htmx(request)" in block, route
