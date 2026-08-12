"""What Review, Commit and History show once a correction can execute.

The rule the pages have to hold up: nothing about a correction is a surprise.
Every file that will move is listed before it moves, an accepted correction
says what it is waiting for, and a file the audit no longer describes offers
re-analysis instead of a button.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from librairy.audit import Finding, record_findings
from librairy.config import Settings
from librairy.corrections import accept_correction
from librairy.db import connect
from librairy.executor import execute_plan
from librairy.scanner import scan_root
from librairy.web.app import create_app

TRACK = "Music/Pop/Queen/05 - Song.flac"
LYRICS = "Music/Pop/Queen/05 - Song.lrc"
DEST = "Music/Rock/Queen/Album/05 - Song.flac"


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


def scene(tmp_path: Path, *relpaths: str, kind: str = "tag-path-mismatch"):
    settings = settings_for(tmp_path)
    conn = connect(settings)
    for relpath in relpaths or (TRACK, LYRICS):
        path = settings.library_dir / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"bytes of {relpath}", encoding="utf-8")
    scan_root(conn, "library", settings.library_dir, settings)
    row = conn.execute("SELECT id, fingerprint FROM items WHERE relpath=?", (TRACK,)).fetchone()
    record_findings(
        conn,
        [
            Finding(
                relpath=TRACK,
                kind=kind,
                severity="high",
                summary="Tagged 'Queen' but filed under Pop.",
                dest_relpath=DEST if kind == "tag-path-mismatch" else None,
                item_id=row["id"],
                fingerprint=row["fingerprint"],
            )
        ],
    )
    finding = conn.execute("SELECT * FROM audit_findings").fetchone()
    return TestClient(create_app(settings, conn)), conn, settings, finding


def post(client, path: str, **data):
    client.get("/review")
    token = client.cookies["csrf_token"]
    return client.post(
        path,
        data={**data, "csrf_token": token},
        headers={"x-csrf-token": token},
        follow_redirects=False,
    )


# --- Review -------------------------------------------------------------------


def test_an_executable_finding_offers_to_accept_the_correction(tmp_path: Path) -> None:
    client, *_ = scene(tmp_path)

    body = client.get("/review").text

    assert "Accept correction" in body
    assert "LIBRARY AUDIT" in body
    assert "Keep as it is" in body


def test_the_wording_is_never_the_inbox_wording(tmp_path: Path) -> None:
    """"Approve" admits a new file. "Accept correction" changes something you
    already own. The two must not read the same."""
    client, *_ = scene(tmp_path)

    body = client.get("/review").text
    audit_section = body.split('id="library-audit"', 1)[1]

    assert "Accept correction" in audit_section
    assert "Approve all confident" not in audit_section


def test_an_observation_offers_no_correction(tmp_path: Path) -> None:
    client, *_ = scene(tmp_path, TRACK, kind="missing-artwork")

    body = client.get("/review").text

    assert "LIBRARY AUDIT" in body
    assert "Accept correction" not in body
    assert "Keep as it is" in body


def test_every_affected_file_is_listed_before_commit(tmp_path: Path) -> None:
    client, *_ = scene(tmp_path)

    body = client.get("/review").text

    assert "2 files will move" in body
    assert "05 - Song.lrc" in body
    assert "companion" in body


def test_a_one_file_correction_does_not_shout_about_a_group(tmp_path: Path) -> None:
    client, *_ = scene(tmp_path, TRACK)

    body = client.get("/review").text

    assert "files will move" not in body
    assert "Accept correction" in body


def test_a_stale_finding_offers_re_analysis_and_not_a_correction(tmp_path: Path) -> None:
    client, conn, settings, finding = scene(tmp_path)
    (settings.library_dir / TRACK).write_text("re-tagged by hand", encoding="utf-8")

    body = client.get("/review").text

    assert "NEEDS RE-ANALYSIS" in body
    assert "The file changed after this audit was created." in body
    assert "Re-audit" in body
    assert "Accept correction" not in body


def test_a_finding_whose_file_is_gone_says_so_plainly(tmp_path: Path) -> None:
    client, conn, settings, finding = scene(tmp_path)
    (settings.library_dir / TRACK).unlink()

    body = client.get("/review").text

    assert "NOT ON DISK" in body
    assert "Accept correction" not in body
    # Nothing to re-analyse either: the file is not there to look at.
    assert "Re-audit" not in body


def test_the_stale_wording_is_not_alarming(tmp_path: Path) -> None:
    client, conn, settings, finding = scene(tmp_path)
    (settings.library_dir / TRACK).write_text("changed", encoding="utf-8")

    body = client.get("/review").text

    for word in ("error", "corrupt", "danger", "failed", "warning"):
        assert word not in body.split('id="library-audit"', 1)[1].lower()


def test_accepting_marks_the_finding_as_waiting_for_commit(tmp_path: Path) -> None:
    client, conn, settings, finding = scene(tmp_path)

    response = post(client, f"/review/audit/{finding['id']}/accept")

    assert response.status_code == 303
    body = client.get("/review").text
    assert "waiting for" in body
    assert "Accept correction" not in body


def test_accepting_a_stale_finding_over_http_is_refused(tmp_path: Path) -> None:
    """A page left open since yesterday still posts yesterday's opinion."""
    client, conn, settings, finding = scene(tmp_path)
    (settings.library_dir / TRACK).write_text("changed", encoding="utf-8")

    response = post(client, f"/review/audit/{finding['id']}/accept")

    assert response.status_code == 409
    assert conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 0


def test_accepting_an_observation_over_http_is_refused(tmp_path: Path) -> None:
    client, conn, settings, finding = scene(tmp_path, TRACK, kind="missing-artwork")

    response = post(client, f"/review/audit/{finding['id']}/accept")

    assert response.status_code == 409


def test_re_auditing_refreshes_the_finding(tmp_path: Path) -> None:
    client, conn, settings, finding = scene(tmp_path)
    (settings.library_dir / TRACK).write_text("changed", encoding="utf-8")

    response = post(client, f"/review/audit/{finding['id']}/reaudit")

    assert response.status_code == 303
    # The stale statement is gone. Whether a new one replaces it is the
    # audit's business, not the button's.
    stale = conn.execute(
        "SELECT * FROM audit_findings WHERE id=? AND fingerprint=?",
        (finding["id"], finding["fingerprint"]),
    ).fetchone()
    assert stale is None


def test_rendering_review_with_findings_writes_nothing(tmp_path: Path) -> None:
    """The concurrency contract, kept. Staleness is computed from the
    filesystem and the row already loaded."""
    client, conn, settings, finding = scene(tmp_path)
    writes: list[str] = []

    def trace(statement: str) -> None:
        if statement.strip().split(" ")[0].upper() in {"INSERT", "UPDATE", "DELETE"}:
            writes.append(statement)

    client.get("/review")  # warm the session so the cold-visit INSERT is done
    conn.set_trace_callback(trace)
    try:
        assert client.get("/review").status_code == 200
    finally:
        conn.set_trace_callback(None)

    assert writes == []


def test_the_extension_control_still_works_on_companions(tmp_path: Path) -> None:
    """The `?` is most useful exactly here: `.lrc` is the sort of extension
    nobody recognises, and it is about to move because LibrAIry says so."""
    client, *_ = scene(tmp_path)

    body = client.get("/review").text

    assert "ext-info" in body
    assert "Lyrics" in body or "lyric" in body.lower()


def test_the_audit_section_is_still_outside_the_inbox_form(tmp_path: Path) -> None:
    """The structural guarantee the accept button must not have broken."""
    client, *_ = scene(tmp_path)

    body = client.get("/review").text
    before_audit = body.split('id="library-audit"', 1)[0]

    assert before_audit.count("<form") == before_audit.count("</form>")


# --- Commit -------------------------------------------------------------------


def test_commit_separates_new_files_from_library_corrections(tmp_path: Path) -> None:
    client, conn, settings, finding = scene(tmp_path)
    accept_correction(conn, settings, finding["id"])

    body = client.get("/commit").text

    assert "Library corrections" in body
    assert "LIBRARY AUDIT" in body
    assert "library correction" in body
    assert "you already own" in body


def test_commit_lists_every_operation_in_a_correction(tmp_path: Path) -> None:
    client, conn, settings, finding = scene(tmp_path)
    accept_correction(conn, settings, finding["id"])

    body = client.get("/commit").text

    assert "05 - Song.lrc" in body
    assert "Music/Rock/Queen/Album/05 - Song.lrc" in body
    assert "Commit this correction" in body


def test_a_correction_is_not_listed_as_a_forgotten_plan(tmp_path: Path) -> None:
    """It is approved deliberately, not abandoned halfway."""
    client, conn, settings, finding = scene(tmp_path)
    accept_correction(conn, settings, finding["id"])

    body = client.get("/commit").text

    assert "Started but never run" not in body


def test_commit_says_nothing_about_corrections_when_there_are_none(
    tmp_path: Path,
) -> None:
    client, *_ = scene(tmp_path)

    body = client.get("/commit").text

    assert "Library corrections" not in body


# --- History ------------------------------------------------------------------


def test_history_calls_a_correction_a_correction(tmp_path: Path) -> None:
    client, conn, settings, finding = scene(tmp_path)
    plan_id = accept_correction(conn, settings, finding["id"])
    execute_plan(conn, plan_id, settings)

    body = client.get("/history").text

    assert "Library correction" in body
    assert "moved 2 files" in body
    assert "Filed 2 files" not in body


def test_history_still_says_filed_for_an_inbox_commit(tmp_path: Path) -> None:
    from librairy.planner import OperationSpec, approve_plan, create_plan

    client, conn, settings, _ = scene(tmp_path)
    (settings.inbox_dir / "arrival.mkv").write_text("new", encoding="utf-8")
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    plan_id = create_plan(
        conn,
        [OperationSpec("move", "arrival.mkv", "library", "Shows/arrival.mkv")],
        settings,
    )
    approve_plan(conn, plan_id, settings)
    execute_plan(conn, plan_id, settings)

    body = client.get("/history").text

    assert "Filed 1 file" in body
    assert "Library correction" not in body


# --- after the correction -----------------------------------------------------


def test_a_committed_correction_is_not_found_again(tmp_path: Path) -> None:
    """Re-running the audit over the folder must not recreate a finding for a
    problem that has been fixed."""
    from librairy.audit import audit_library

    client, conn, settings, finding = scene(tmp_path, TRACK)
    plan_id = accept_correction(conn, settings, finding["id"])
    execute_plan(conn, plan_id, settings)
    scan_root(conn, "library", settings.library_dir, settings)

    audit_library(conn, settings, read_tags=False)

    reopened = conn.execute(
        "SELECT * FROM audit_findings WHERE relpath=? AND status='open'", (TRACK,)
    ).fetchone()
    assert reopened is None


def test_keeping_a_finding_stops_it_coming_back(tmp_path: Path) -> None:
    from librairy.audit import audit_library

    client, conn, settings, finding = scene(tmp_path, TRACK)
    post(client, f"/review/audit/{finding['id']}/keep")

    audit_library(conn, settings, read_tags=False)

    row = conn.execute("SELECT status FROM audit_findings WHERE id=?", (finding["id"],)).fetchone()
    assert row["status"] == "kept"
    assert "Accept correction" not in client.get("/review").text


# --- mobile -------------------------------------------------------------------


def test_the_correction_actions_stack_on_a_narrow_screen() -> None:
    """Accept and Keep must never be a mis-tap apart at 375px."""
    css = Path("src/librairy/web/static/pipboy.css").read_text(encoding="utf-8")
    blocks = css.split("@media (max-width: 40rem) {")[1:]
    stacked = [
        block
        for block in blocks
        if ".audit-actions { flex-direction: column; }" in block.split("\n}")[0]
    ]

    assert stacked, "the accept/keep pair must stack inside a narrow-screen block"
    assert ".audit-actions form, .audit-actions button { width: 100%; }" in stacked[0]


def test_the_audit_colour_is_never_the_only_signal() -> None:
    """Every state says what it is in words as well."""
    template = Path(
        "src/librairy/web/templates/partials/review_audit.html"
    ).read_text(encoding="utf-8")
    assert "LIBRARY AUDIT" in template
    assert "state_label" in template
    assert "badge-stale" in template
