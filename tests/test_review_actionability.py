"""What a Library Review row lets you do, and why.

Every test here traces back to one real report against the live library:

    I select `Lipps Inc.`, press Accept corrections, and nothing happens.

That was completely accurate. `Music/Pop/Lipps Inc.` is a folder-naming
observation — `naming-inconsistency` is not in `EXECUTABLE_KINDS`, and the
executor cannot rename a subtree — so it could never be approved. But its
checkbox was enabled and unmarked, and the toolbar's Approve button disabled
itself when nothing eligible was selected. A disabled button issues no request,
so there was no error, no message and no change. The software said nothing at
all about a thing it had already decided was impossible.

So actionability is a value on the row now, not something inferred from which
button happened to render, and the tests below hold that line from both ends:
an observation can never look approvable, and a real correction still can.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from librairy.audit import Finding, record_findings, restore_suggestion
from librairy.config import Settings
from librairy.corrections import CorrectionRefused, accept_correction, withdraw_approval
from librairy.db import connect
from librairy.models import EvidenceEntry
from librairy.scanner import scan_root
from librairy.web import actionability as act
from librairy.web.app import create_app
from librairy.web.review import apply_audit_bulk, audit_view

TRACK = "Music/Pop/Queen/05 - Song.flac"
DEST = "Music/Rock/Queen/A Night at the Opera/05 - Song.flac"
# The real one, spelled as the live library spells it. A trailing dot is what
# Windows silently drops, which is why the audit mentions it at all.
LIPPS = "Music/Pop/Lipps Inc."


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


def lipps() -> Finding:
    """The live finding, reproduced. Note that it *has* a destination.

    That is the trap: `dest_relpath` looks like something to execute, and it is
    not. `Music/Pop/Lipps Inc` is a folder name, and renaming a folder is not a
    move the plan can express — every file inside it would have to move, as one
    coherent operation, with companions and collisions handled. Until that
    exists this is a thing to know, not a thing to press.
    """
    return Finding(
        relpath=LIPPS,
        kind="naming-inconsistency",
        severity="review",
        summary="Ends in a dot, which Windows silently drops.",
        dest_relpath="Music/Pop/Lipps Inc",
        evidence=[EvidenceEntry("filesystem", "folder", "Lipps Inc.", 0.9)],
    )


def scene(tmp_path: Path, *findings: Finding):
    settings = settings_for(tmp_path)
    conn = connect(settings)
    for relpath in (TRACK, f"{LIPPS}/01 - Funkytown.flac"):
        path = settings.library_dir / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"bytes of {relpath}", encoding="utf-8")
    scan_root(conn, "library", settings.library_dir, settings)
    resolved = []
    for finding in findings:
        row = conn.execute(
            "SELECT id, fingerprint FROM items WHERE relpath=?", (finding.relpath,)
        ).fetchone()
        if row is not None:
            finding.item_id, finding.fingerprint = row["id"], row["fingerprint"]
        resolved.append(finding)
    record_findings(conn, resolved)
    return TestClient(create_app(settings, conn)), conn, settings


def finding_id(conn, relpath: str) -> int:
    return conn.execute(
        "SELECT id FROM audit_findings WHERE relpath=?", (relpath,)
    ).fetchone()["id"]


def article(body: str, ident: int) -> str:
    """Just the one row, so a neighbour's markup cannot satisfy an assertion."""
    start = body.index(f'id="finding-{ident}"')
    return body[start : body.index("</article>", start)]


def token_of(client: TestClient) -> str:
    client.get("/review")
    return client.cookies.get("csrf_token", "")


def post(client: TestClient, url: str, **data):
    token = token_of(client)
    return client.post(
        url, data={**data, "csrf_token": token}, headers={"x-csrf-token": token}
    )


# --- the Lipps Inc. bug, from both ends ---------------------------------------


def test_an_observation_is_never_selectable_for_approval(tmp_path: Path) -> None:
    """The bug itself. The checkbox was enabled, so the selection was legal,
    so the button that could not act on it disabled itself silently."""
    client, conn, _ = scene(tmp_path, lipps())

    row = article(client.get("/review").text, finding_id(conn, LIPPS))

    assert "disabled" in row.split("</label>")[0]
    assert 'data-audit-eligible="1"' not in row


def test_an_observation_says_why_rather_than_omitting_a_button(tmp_path: Path) -> None:
    """A row that merely lacks an Approve control reads as a bug. Saying "no
    automatic correction is available" makes it a rule."""
    client, conn, _ = scene(tmp_path, lipps())

    row = article(client.get("/review").text, finding_id(conn, LIPPS))

    assert "Observation" in row
    assert "No automatic correction is available" in row
    assert ">Approve change</button>" not in row


def test_a_folder_rename_is_not_executable_merely_because_it_has_a_destination(
    tmp_path: Path,
) -> None:
    """`dest_relpath` is not the test, and must never become it. A future
    detector could set a destination without anybody having reasoned about
    what executing it would mean."""
    client, conn, settings = scene(tmp_path, lipps())
    ident = finding_id(conn, LIPPS)

    assert conn.execute(
        "SELECT dest_relpath FROM audit_findings WHERE id=?", (ident,)
    ).fetchone()["dest_relpath"]

    try:
        accept_correction(conn, settings, ident)
    except CorrectionRefused as exc:
        assert "observation" in str(exc)
    else:  # pragma: no cover - the whole point is that this cannot happen
        raise AssertionError("a folder rename was turned into a plan")


def test_a_real_correction_is_still_selectable_and_approvable(tmp_path: Path) -> None:
    """The other end of the line. Making observations inert must not make
    corrections inert with them."""
    client, conn, _ = scene(tmp_path, correction())

    row = article(client.get("/review").text, finding_id(conn, TRACK))

    assert "disabled" not in row.split("</label>")[0]
    assert 'data-audit-eligible="1"' in row
    assert ">Approve change</button>" in row


def test_selecting_only_an_observation_reports_a_result(tmp_path: Path) -> None:
    """Even reaching the endpoint directly — a second tab, a stale page, curl —
    produces a sentence rather than a shrug."""
    client, conn, settings = scene(tmp_path, lipps())

    result = apply_audit_bulk(conn, settings, "accept", [finding_id(conn, LIPPS)])

    assert "Selected: 1" in result
    assert "Observation only: 1" in result
    assert "Nothing was approved" in result


# --- approving moves the row somewhere -----------------------------------------


def test_approving_takes_the_row_out_of_the_undecided_list(tmp_path: Path) -> None:
    client, conn, settings = scene(tmp_path, correction())
    ident = finding_id(conn, TRACK)

    accept_correction(conn, settings, ident)
    view = audit_view(conn, settings)

    assert [row["id"] for row in view["audit_waiting"]] == [ident]
    assert ident not in [
        row["id"] for group in view["audit_groups"] for row in group["findings"]
    ]


def test_an_approved_row_is_rendered_under_waiting_for_commit(tmp_path: Path) -> None:
    client, conn, settings = scene(tmp_path, correction())
    accept_correction(conn, settings, finding_id(conn, TRACK))

    body = client.get("/review").text

    assert "Waiting for Commit" in body
    assert "Nothing has moved yet" in body


def test_the_bulk_result_counts_every_outcome_by_name(tmp_path: Path) -> None:
    client, conn, settings = scene(tmp_path, correction(), lipps())

    result = apply_audit_bulk(
        conn, settings, "accept", [finding_id(conn, TRACK), finding_id(conn, LIPPS)]
    )

    assert "Selected: 2" in result
    assert "Approved: 1" in result
    assert "Observation only: 1" in result
    # Never the sentence this replaced.
    assert "item(s) updated" not in result


# --- taking an approval back ---------------------------------------------------


def test_removing_an_approval_returns_the_row_to_review(tmp_path: Path) -> None:
    client, conn, settings = scene(tmp_path, correction())
    ident = finding_id(conn, TRACK)
    accept_correction(conn, settings, ident)

    withdraw_approval(conn, ident)

    row = conn.execute(
        "SELECT status, plan_id FROM audit_findings WHERE id=?", (ident,)
    ).fetchone()
    assert row["status"] == "open"
    assert row["plan_id"] is None
    assert conn.execute("SELECT COUNT(*) c FROM plans").fetchone()["c"] == 0


def test_removing_an_approval_moves_no_files(tmp_path: Path) -> None:
    """It is not Undo, and the difference is that nothing has happened yet."""
    client, conn, settings = scene(tmp_path, correction())
    ident = finding_id(conn, TRACK)
    accept_correction(conn, settings, ident)

    withdraw_approval(conn, ident)

    assert (settings.library_dir / TRACK).is_file()
    assert not (settings.library_dir / DEST).exists()
    assert conn.execute("SELECT COUNT(*) c FROM history").fetchone()["c"] == 0


def test_a_started_correction_cannot_be_unapproved(tmp_path: Path) -> None:
    """Withdrawal is safe *because* nothing executed. Once an operation has
    run there is a journal entry and a moved file, and the reversal for that
    is Undo — which verifies hashes and restores paths."""
    client, conn, settings = scene(tmp_path, correction())
    ident = finding_id(conn, TRACK)
    accept_correction(conn, settings, ident)
    plan_id = conn.execute(
        "SELECT plan_id FROM audit_findings WHERE id=?", (ident,)
    ).fetchone()["plan_id"]
    conn.execute(
        "UPDATE plan_ops SET executed_at='2026-08-14T00:00:00+00:00' WHERE plan_id=?",
        (plan_id,),
    )

    try:
        withdraw_approval(conn, ident)
    except CorrectionRefused as exc:
        assert "already run" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("an executed correction was unapproved")


def test_the_pre_commit_reversal_is_not_called_undo(tmp_path: Path) -> None:
    """Two reversals, two words. `Undo` after execution puts files back;
    `Remove approval` before it puts a decision back. One word for both is how
    somebody comes to believe Undo will rescue a commit they never made."""
    client, conn, settings = scene(tmp_path, correction())
    accept_correction(conn, settings, finding_id(conn, TRACK))

    row = article(client.get("/review").text, finding_id(conn, TRACK))

    assert "Remove approval" in row
    assert ">Undo<" not in row


# --- dismissal is a decision, not a deletion -----------------------------------


def test_dismissing_hides_the_row_without_deleting_the_record(tmp_path: Path) -> None:
    client, conn, settings = scene(tmp_path, lipps())
    ident = finding_id(conn, LIPPS)

    post(client, f"/review/audit/{ident}/keep")

    view = audit_view(conn, settings)
    assert [row["id"] for row in view["audit_dismissed"]] == [ident]
    assert ident not in [
        row["id"] for group in view["audit_groups"] for row in group["findings"]
    ]
    assert conn.execute("SELECT COUNT(*) c FROM audit_findings").fetchone()["c"] == 1


def test_dismissing_says_where_the_row_went(tmp_path: Path) -> None:
    client, conn, _ = scene(tmp_path, lipps())

    response = post(client, f"/review/audit/{finding_id(conn, LIPPS)}/keep")

    assert response.status_code == 200
    assert "Suggestion dismissed" in response.text
    assert "can be restored" in response.text


def test_a_dismissed_row_is_visible_and_offers_restore(tmp_path: Path) -> None:
    client, conn, _ = scene(tmp_path, lipps())
    ident = finding_id(conn, LIPPS)
    post(client, f"/review/audit/{ident}/keep")

    row = article(client.get("/review").text, ident)

    assert "Dismissed" in row
    assert "Restore suggestion" in row


def test_restoring_puts_it_back_in_the_active_list(tmp_path: Path) -> None:
    client, conn, settings = scene(tmp_path, lipps())
    ident = finding_id(conn, LIPPS)
    post(client, f"/review/audit/{ident}/keep")

    response = post(client, f"/review/audit/{ident}/restore")

    assert "Suggestion restored" in response.text
    assert audit_view(conn, settings)["audit_open"] == 1


def test_restore_only_applies_to_a_dismissed_suggestion(tmp_path: Path) -> None:
    """An approved or applied row has its own reversal. One control that
    sometimes means three things is how people stop trusting all three."""
    client, conn, settings = scene(tmp_path, correction())
    ident = finding_id(conn, TRACK)
    accept_correction(conn, settings, ident)

    assert restore_suggestion(conn, ident) is False
    assert (
        conn.execute("SELECT status FROM audit_findings WHERE id=?", (ident,)).fetchone()[
            "status"
        ]
        == "accepted"
    )


def test_an_identical_audit_leaves_a_dismissed_suggestion_dismissed(
    tmp_path: Path,
) -> None:
    """Otherwise the same question comes back every week and the list stops
    being read — which is the failure mode dismissal exists to prevent."""
    client, conn, settings = scene(tmp_path, lipps())
    ident = finding_id(conn, LIPPS)
    post(client, f"/review/audit/{ident}/keep")

    record_findings(conn, [lipps()])

    assert (
        conn.execute("SELECT status FROM audit_findings WHERE id=?", (ident,)).fetchone()[
            "status"
        ]
        == "kept"
    )


def test_bulk_dismissal_cannot_reach_an_approved_row(tmp_path: Path) -> None:
    client, conn, settings = scene(tmp_path, correction())
    ident = finding_id(conn, TRACK)
    accept_correction(conn, settings, ident)

    apply_audit_bulk(conn, settings, "keep", [ident])

    assert (
        conn.execute("SELECT status FROM audit_findings WHERE id=?", (ident,)).fetchone()[
            "status"
        ]
        == "accepted"
    )


# --- the concept itself ---------------------------------------------------------


def test_only_ready_is_approvable() -> None:
    """A set with one member, so the next state added has to decide
    deliberately instead of inheriting approvability by accident."""
    assert set(act.APPROVABLE) == {act.READY}
    for value in act.LABEL:
        assert act.can_approve(value) == (value == act.READY)


def test_every_state_has_a_label_and_a_note() -> None:
    assert set(act.LABEL) == set(act.EXPLANATION)
    assert set(act.OUTCOME_TEXT) == set(act.LABEL)
    assert set(act.BULK_ORDER) == set(act.LABEL)


def test_no_state_is_labelled_with_a_database_word() -> None:
    """`open`, `accepted` and `kept` are columns. Nobody reading the page has
    ever thought of their music that way."""
    for label in act.LABEL.values():
        assert label.lower() not in {"open", "accepted", "kept", "corrected"} or label == (
            "Corrected"
        )
