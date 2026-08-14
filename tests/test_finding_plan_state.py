"""The finding↔plan contract, from the live inconsistency that revealed it.

The live database held this:

    audit_findings.id = 4    status = open
    plans.id = 41831293-…    status = approved
    plan_ops                 executed_at = NULL

Two fields answering "has this been approved?" and disagreeing. Review believed
the status, drew a checkbox, and offered Approve change — which would have
created a *second* immutable plan over the same files and orphaned the first.

Every test below is one sentence of the contract:

    an active plan outranks the finding's status, everywhere;
    a second active plan cannot be created, by any route;
    re-auditing does not undo an approval;
    a source that changed makes the approval visibly outdated;
    sending it back withdraws the plan and touches no file.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from librairy import integrity
from librairy.audit import Finding, record_findings
from librairy.config import Settings
from librairy.correction_state import active_plan, active_plans, plan_drift
from librairy.corrections import (
    CorrectionRefused,
    accept_correction,
    pending_corrections,
    withdraw_approval,
    withdrawals_for,
)
from librairy.db import connect
from librairy.executor import execute_plan
from librairy.models import EvidenceEntry
from librairy.scanner import scan_root
from librairy.web import actionability as act
from librairy.web.app import create_app
from librairy.web.commit import commit_overview
from librairy.web.review import audit_view

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


def correction() -> Finding:
    return Finding(
        relpath=TRACK,
        kind="tag-path-mismatch",
        severity="high",
        summary="Tagged 'Queen' but filed under 'Pop'.",
        dest_relpath=DEST,
        evidence=[EvidenceEntry("tags", "artist", "Queen", 0.9)],
    )


def scene(tmp_path: Path):
    settings = settings_for(tmp_path)
    conn = connect(settings)
    path = settings.library_dir / TRACK
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("bytes of the song", encoding="utf-8")
    scan_root(conn, "library", settings.library_dir, settings)
    record_findings(conn, [_resolved(conn, correction())])
    return TestClient(create_app(settings, conn)), conn, settings


def _resolved(conn: sqlite3.Connection, finding: Finding) -> Finding:
    row = conn.execute(
        "SELECT id, fingerprint FROM items WHERE relpath=?", (finding.relpath,)
    ).fetchone()
    if row is not None:
        finding.item_id, finding.fingerprint = row["id"], row["fingerprint"]
    return finding


def finding_id(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT id FROM audit_findings WHERE relpath=?", (TRACK,)
    ).fetchone()["id"]


def desync(conn: sqlite3.Connection, ident: int) -> None:
    """Reproduce the live shape exactly: approved plan, finding back at `open`.

    Written directly rather than through the audit, so that the tests of *how
    the UI reads* this state do not depend on the bug that produced it still
    being reproducible. The lifecycle test below covers the production path.
    """
    conn.execute(
        "UPDATE audit_findings SET status='open' WHERE id=?", (ident,)
    )


def row_for(conn: sqlite3.Connection, settings: Settings, ident: int):
    for key in ("audit_waiting", "audit_dismissed"):
        for view in audit_view(conn, settings)[key]:
            if view["id"] == ident:
                return view
    for group in audit_view(conn, settings)["audit_groups"]:
        for view in group["findings"]:
            if view["id"] == ident:
                return view
    return None


# --- 1-3: the plan is the authority -------------------------------------------


def test_an_open_finding_with_no_plan_is_open(tmp_path: Path) -> None:
    _, conn, settings = scene(tmp_path)
    view = row_for(conn, settings, finding_id(conn))

    assert view["status_kind"] == act.READY
    assert view["can_approve"] is True


def test_an_approved_plan_makes_the_row_waiting_however_the_status_reads(
    tmp_path: Path,
) -> None:
    """The whole point. `status='open'` and an approved plan: the plan wins."""
    _, conn, settings = scene(tmp_path)
    ident = finding_id(conn)
    accept_correction(conn, settings, ident)
    desync(conn, ident)

    view = row_for(conn, settings, ident)

    assert view["status_kind"] == act.WAITING
    assert view["status_label"] == "Waiting for Commit"
    assert view["can_approve"] is False


def test_a_desynced_row_offers_no_approval_control(tmp_path: Path) -> None:
    client, conn, settings = scene(tmp_path)
    ident = finding_id(conn)
    accept_correction(conn, settings, ident)
    desync(conn, ident)

    body = client.get("/review").text
    start = body.index(f'id="finding-{ident}"')
    row = body[start : body.index("</article>", start)]

    assert "Approve change" not in row
    assert 'type="checkbox"' not in row or "disabled" in row
    assert "Waiting for Commit" in row


def test_a_desynced_row_still_reaches_commit(tmp_path: Path) -> None:
    """Review saying "waiting for Commit" while Commit shows nothing is the
    same disagreement seen from the other side."""
    _, conn, settings = scene(tmp_path)
    ident = finding_id(conn)
    accept_correction(conn, settings, ident)
    desync(conn, ident)

    pending = pending_corrections(conn)
    assert [row["id"] for row in pending] == [ident]
    assert commit_overview(conn, settings)["corrections"][0]["finding_id"] == ident


# --- 4-6: a second approval is impossible -------------------------------------


def test_a_second_approval_is_refused_by_the_service(tmp_path: Path) -> None:
    _, conn, settings = scene(tmp_path)
    ident = finding_id(conn)
    accept_correction(conn, settings, ident)
    desync(conn, ident)

    with pytest.raises(CorrectionRefused) as refusal:
        accept_correction(conn, settings, ident)

    assert "waiting for Commit" in str(refusal.value)
    assert len(active_plans(conn, ident)) == 1


def test_the_database_refuses_a_second_active_plan_without_the_service(
    tmp_path: Path,
) -> None:
    """The constraint that holds when the request never went through Python.

    A UI that hides a button and a service that raises are both code somebody
    can go around; this is the one that is still true afterwards.
    """
    _, conn, settings = scene(tmp_path)
    ident = finding_id(conn)
    accept_correction(conn, settings, ident)

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO plans(id, status, plan_hash, created_at, audit_finding_id)"
            " VALUES ('second', 'approved', 'x', '2026-01-01T00:00:00+00:00', ?)",
            (ident,),
        )


def test_a_finished_plan_cannot_be_promoted_behind_a_new_one(tmp_path: Path) -> None:
    """SQLite applies a partial unique index on UPDATE as well as INSERT."""
    _, conn, settings = scene(tmp_path)
    ident = finding_id(conn)
    first = accept_correction(conn, settings, ident)
    conn.execute("UPDATE plans SET status='done' WHERE id=?", (first,))
    conn.execute("UPDATE audit_findings SET status='open', plan_id=NULL WHERE id=?", (ident,))
    accept_correction(conn, settings, ident)

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE plans SET status='approved' WHERE id=?", (first,))


def test_the_bulk_endpoint_cannot_double_approve(tmp_path: Path) -> None:
    """The same request arriving twice — a second tab, a resubmitted form."""
    client, conn, settings = scene(tmp_path)
    ident = finding_id(conn)
    client.get("/review")
    token = client.cookies.get("csrf_token", "")
    for _ in range(2):
        client.post(
            "/review/audit/bulk",
            data={"action": "accept", "finding_id": str(ident), "csrf_token": token},
            headers={"x-csrf-token": token},
        )

    assert len(active_plans(conn, ident)) == 1


# --- 7: re-audit does not undo an approval ------------------------------------


def test_re_auditing_does_not_reopen_an_approved_correction(tmp_path: Path) -> None:
    """The production path that created the live inconsistency.

    The audit re-finds a problem it has already reported — it must, because the
    approved files have not moved yet — and used to write `status='open'`
    unconditionally while leaving `plan_id` in place.
    """
    _, conn, settings = scene(tmp_path)
    ident = finding_id(conn)
    accept_correction(conn, settings, ident)

    record_findings(conn, [_resolved(conn, correction())])

    row = conn.execute(
        "SELECT status, plan_id FROM audit_findings WHERE id=?", (ident,)
    ).fetchone()
    assert row["status"] == "accepted"
    assert row["plan_id"] is not None
    assert integrity.check(conn) == []


def test_re_auditing_still_refreshes_the_evidence(tmp_path: Path) -> None:
    """Protecting the decision must not freeze the facts."""
    _, conn, settings = scene(tmp_path)
    ident = finding_id(conn)
    accept_correction(conn, settings, ident)

    newer = _resolved(conn, correction())
    newer.summary = "Tagged 'Queen', and the album is now known."
    record_findings(conn, [newer])

    row = conn.execute("SELECT summary, status FROM audit_findings WHERE id=?", (ident,))
    updated = row.fetchone()
    assert updated["summary"] == "Tagged 'Queen', and the album is now known."
    assert updated["status"] == "accepted"


def test_retiring_never_deletes_a_finding_that_owns_a_plan(tmp_path: Path) -> None:
    """Deleting it would strand the plan in Commit with nothing to explain it."""
    _, conn, settings = scene(tmp_path)
    ident = finding_id(conn)
    accept_correction(conn, settings, ident)
    desync(conn, ident)

    record_findings(conn, [])  # the audit now finds nothing at all

    assert conn.execute(
        "SELECT COUNT(*) AS n FROM audit_findings WHERE id=?", (ident,)
    ).fetchone()["n"] == 1


# --- 8-9: an approval can go out of date --------------------------------------


def test_a_changed_source_makes_the_approval_outdated(tmp_path: Path) -> None:
    _, conn, settings = scene(tmp_path)
    ident = finding_id(conn)
    accept_correction(conn, settings, ident)
    (settings.library_dir / TRACK).write_text("re-tagged since", encoding="utf-8")

    assert plan_drift(conn, settings, active_plan(conn, ident).plan_id) == "changed"
    view = row_for(conn, settings, ident)
    assert view["status_kind"] == act.OUTDATED
    assert view["status_label"] == "Approval is outdated"
    assert view["can_approve"] is False


def test_an_outdated_approval_offers_no_commit_button(tmp_path: Path) -> None:
    """The executor would refuse it, so the page must not offer it."""
    client, conn, settings = scene(tmp_path)
    accept_correction(conn, settings, finding_id(conn))
    (settings.library_dir / TRACK).write_text("re-tagged since", encoding="utf-8")

    body = client.get("/commit").text

    assert "Commit this correction" not in body
    assert "Approval is outdated" in body
    assert "Remove old approval" in body


def test_a_missing_source_says_so_rather_than_changed(tmp_path: Path) -> None:
    _, conn, settings = scene(tmp_path)
    ident = finding_id(conn)
    accept_correction(conn, settings, ident)
    (settings.library_dir / TRACK).unlink()

    assert plan_drift(conn, settings, active_plan(conn, ident).plan_id) == "missing"


# --- 10-13: sending it back ---------------------------------------------------


def test_send_back_to_review_withdraws_the_plan(tmp_path: Path) -> None:
    _, conn, settings = scene(tmp_path)
    ident = finding_id(conn)
    plan_id = accept_correction(conn, settings, ident)

    withdraw_approval(conn, ident)

    assert active_plans(conn, ident) == []
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM plans WHERE id=?", (plan_id,)
    ).fetchone()["n"] == 0
    row = conn.execute("SELECT status, plan_id FROM audit_findings WHERE id=?", (ident,))
    assert tuple(row.fetchone()) == ("open", None)


def test_withdrawal_moves_no_file(tmp_path: Path) -> None:
    _, conn, settings = scene(tmp_path)
    ident = finding_id(conn)
    accept_correction(conn, settings, ident)

    withdraw_approval(conn, ident)

    assert (settings.library_dir / TRACK).is_file()
    assert not (settings.library_dir / DEST).exists()
    assert conn.execute("SELECT COUNT(*) AS n FROM history").fetchone()["n"] == 0


def test_a_withdrawn_approval_is_still_remembered(tmp_path: Path) -> None:
    """"Approved on Tuesday, changed your mind on Wednesday" is a real answer."""
    _, conn, settings = scene(tmp_path)
    ident = finding_id(conn)
    plan_id = accept_correction(conn, settings, ident)

    withdraw_approval(conn, ident)

    record = withdrawals_for(conn, ident)
    assert len(record) == 1
    assert record[0]["plan_id"] == plan_id
    assert record[0]["relpath"] == TRACK
    assert record[0]["op_count"] >= 1


def test_a_withdrawn_finding_can_be_approved_again(tmp_path: Path) -> None:
    _, conn, settings = scene(tmp_path)
    ident = finding_id(conn)
    accept_correction(conn, settings, ident)
    withdraw_approval(conn, ident)

    assert row_for(conn, settings, ident)["can_approve"] is True
    assert accept_correction(conn, settings, ident)


def test_a_desynced_row_can_still_be_sent_back(tmp_path: Path) -> None:
    """Requiring `status='accepted'` meant the one genuinely stuck correction
    in the live database could not be recalled at all."""
    _, conn, settings = scene(tmp_path)
    ident = finding_id(conn)
    accept_correction(conn, settings, ident)
    desync(conn, ident)

    withdraw_approval(conn, ident)

    assert active_plans(conn, ident) == []


# --- 14-15: after execution ---------------------------------------------------


def test_an_executed_correction_reads_as_corrected(tmp_path: Path) -> None:
    _, conn, settings = scene(tmp_path)
    ident = finding_id(conn)
    plan_id = accept_correction(conn, settings, ident)

    execute_plan(conn, plan_id, settings)

    assert active_plans(conn, ident) == []
    assert conn.execute(
        "SELECT status FROM audit_findings WHERE id=?", (ident,)
    ).fetchone()["status"] == "corrected"
    assert integrity.check(conn) == []


def test_undo_is_never_offered_before_execution(tmp_path: Path) -> None:
    """Two reversals, two words. Before Commit nothing has moved."""
    client, conn, settings = scene(tmp_path)
    accept_correction(conn, settings, finding_id(conn))

    body = client.get("/commit").text
    section = body[body.index("Library corrections") :]

    assert "Send back to Review" in section
    assert "Undo" not in section


# --- 16-18: the integrity checker ---------------------------------------------


def test_the_checker_names_the_live_shape(tmp_path: Path) -> None:
    _, conn, settings = scene(tmp_path)
    ident = finding_id(conn)
    accept_correction(conn, settings, ident)
    desync(conn, ident)

    issues = integrity.check(conn)

    assert [issue.kind for issue in issues] == [integrity.OPEN_WITH_ACTIVE_PLAN]
    assert issues[0].relpath == TRACK
    assert issues[0].repairable is True
    # Library-relative, never the host's layout.
    assert str(tmp_path) not in str(issues[0])


def test_repair_makes_the_status_agree_with_the_plan(tmp_path: Path) -> None:
    _, conn, settings = scene(tmp_path)
    ident = finding_id(conn)
    accept_correction(conn, settings, ident)
    desync(conn, ident)

    integrity.repair(conn, integrity.check(conn))

    assert conn.execute(
        "SELECT status FROM audit_findings WHERE id=?", (ident,)
    ).fetchone()["status"] == "accepted"
    assert integrity.check(conn) == []


def test_an_accepted_finding_with_no_plan_is_reopened(tmp_path: Path) -> None:
    _, conn, _ = scene(tmp_path)
    ident = finding_id(conn)
    conn.execute("UPDATE audit_findings SET status='accepted' WHERE id=?", (ident,))

    issues = integrity.check(conn)
    assert [issue.kind for issue in issues] == [integrity.ACCEPTED_WITHOUT_PLAN]

    integrity.repair(conn, issues)
    assert conn.execute(
        "SELECT status FROM audit_findings WHERE id=?", (ident,)
    ).fetchone()["status"] == "open"


def test_duplicate_active_plans_are_reported_and_never_repaired(
    tmp_path: Path,
) -> None:
    """Which of two approvals somebody meant is recorded nowhere.

    The database now prevents this from arising, so it is forced here — the
    checker still has to recognise it, because a database written by an older
    version can already contain it.
    """
    _, conn, settings = scene(tmp_path)
    ident = finding_id(conn)
    first = accept_correction(conn, settings, ident)
    conn.execute("DROP INDEX idx_plans_one_active_per_finding")
    conn.execute(
        "INSERT INTO plans(id, status, plan_hash, created_at, audit_finding_id)"
        " VALUES ('second', 'approved', 'x', '2026-01-01T00:00:00+00:00', ?)",
        (ident,),
    )

    issues = integrity.check(conn)

    assert [issue.kind for issue in issues] == [integrity.DUPLICATE_ACTIVE_PLANS]
    assert set(issues[0].plan_ids) == {first, "second"}
    with pytest.raises(integrity.RepairRefused):
        integrity.repair(conn, issues)


def test_repair_is_all_or_nothing(tmp_path: Path) -> None:
    """Repairing the easy row and leaving the ambiguous one produces a database
    that passes a partial check and still has the problem that mattered."""
    _, conn, settings = scene(tmp_path)
    ident = finding_id(conn)
    accept_correction(conn, settings, ident)
    desync(conn, ident)
    conn.execute("DROP INDEX idx_plans_one_active_per_finding")
    conn.execute(
        "INSERT INTO plans(id, status, plan_hash, created_at, audit_finding_id)"
        " VALUES ('second', 'approved', 'x', '2026-01-01T00:00:00+00:00', ?)",
        (ident,),
    )

    with pytest.raises(integrity.RepairRefused):
        integrity.repair(conn, integrity.check(conn))

    assert conn.execute(
        "SELECT status FROM audit_findings WHERE id=?", (ident,)
    ).fetchone()["status"] == "open"


def test_a_finished_plan_whose_finding_never_heard_is_repairable(
    tmp_path: Path,
) -> None:
    _, conn, settings = scene(tmp_path)
    ident = finding_id(conn)
    plan_id = accept_correction(conn, settings, ident)
    conn.execute(
        "UPDATE plans SET status='done', finished_at='2026-01-01T00:00:00+00:00'"
        " WHERE id=?",
        (plan_id,),
    )

    issues = integrity.check(conn)
    assert [issue.kind for issue in issues] == [integrity.FINISHED_PLAN_STILL_PENDING]

    integrity.repair(conn, issues)
    assert conn.execute(
        "SELECT status FROM audit_findings WHERE id=?", (ident,)
    ).fetchone()["status"] == "corrected"


def test_a_pending_approval_over_changed_bytes_is_reported_not_repaired(
    tmp_path: Path,
) -> None:
    _, conn, settings = scene(tmp_path)
    accept_correction(conn, settings, finding_id(conn))
    (settings.library_dir / TRACK).write_text("re-tagged since", encoding="utf-8")

    issues = integrity.check(conn, settings)

    assert [issue.kind for issue in issues] == [integrity.STALE_ACTIVE_PLAN]
    assert issues[0].repairable is False


def test_the_check_without_settings_touches_no_file(tmp_path: Path) -> None:
    """Which is what makes it safe to run against a live installation."""
    _, conn, settings = scene(tmp_path)
    accept_correction(conn, settings, finding_id(conn))
    (settings.library_dir / TRACK).unlink()

    assert integrity.check(conn) == []
