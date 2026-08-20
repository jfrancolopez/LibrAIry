from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from librairy.config import Settings
from librairy.db import connect
from librairy.executor import execute_plan
from librairy.models import EvidenceEntry
from librairy.planner import approve_plan, create_plan
from librairy.proposals import upsert_proposal
from librairy.quarantine import quarantine_operation
from librairy.scanner import scan_root
from librairy.web.app import create_app


def client_for(tmp_path: Path) -> tuple[TestClient, object, Settings]:
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        FILE_STABILITY_SECONDS=0,
        _env_file=None,
    )
    settings.inbox_dir.mkdir()
    settings.library_dir.mkdir()
    settings.quarantine_dir.mkdir()
    conn = connect(settings)
    client = TestClient(create_app(settings, conn))
    client.post("/setup", data={"password": "correct horse battery"})
    return client, conn, settings


def test_quarantine_restore_round_trips_file_and_journals(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    entry_id = seed_executed_quarantine(conn, settings)

    response = client.post(
        f"/quarantine/restore/{entry_id}",
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )

    # A request now, not an immediate move. The file goes back at Commit, the
    # same way every other file movement in LibrAIry happens.
    assert response.status_code == 200
    assert "Restore requested" in response.text
    assert "Waiting for Commit" in response.text
    assert (settings.quarantine_dir / "2026-07-22/dupe.txt").exists()
    assert not (settings.inbox_dir / "dupe.txt").exists()
    plan = conn.execute(
        "SELECT id, status FROM plans WHERE quarantine_entry_id=?", (entry_id,)
    ).fetchone()
    assert plan["status"] == "approved"

    # And committing it does what the row promised.
    execute_plan(conn, plan["id"], settings)
    assert (settings.inbox_dir / "dupe.txt").read_text(encoding="utf-8") == "dupe"
    assert not (settings.quarantine_dir / "2026-07-22/dupe.txt").exists()
    assert conn.execute(
        "SELECT restored_at FROM quarantine_entries WHERE id=?", (entry_id,)
    ).fetchone()[0] is not None


def test_quarantine_screen_lists_staged_and_similar_flags_without_delete(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    seed_staged_quarantine(conn)
    seed_similar_flag(conn, settings)
    seed_executed_quarantine(conn, settings)

    response = client.get("/quarantine")

    assert response.status_code == 200
    assert "LibrAIry never deletes anything" in response.text
    assert "copy.txt" in response.text
    assert "inbox:dupe.txt" in response.text or "dupe.txt" in response.text
    # Not "your call". That wording was rejected: it puts the burden on the
    # reader without saying what the software has and has not done.
    assert "looks similar" in response.text.lower()
    assert "your call" not in response.text.lower()
    assert "similarity 0.91" in response.text
    # The page now tells you where to go and delete things yourself, so the
    # invariant is "no control that deletes", not "the word never appears".
    # The invariant is "nothing here deletes a file", not "the word never
    # appears". `Delete queue` posts a *request* to move a file into a folder
    # you empty yourself; there is still no control anywhere that unlinks one.
    for control in ("hx-delete", "/quarantine/purge", "/quarantine/destroy"):
        assert control not in response.text
    assert "Nothing is deleted" in response.text or "never deletes" in response.text


def test_staged_quarantine_approve_and_unstage_actions(tmp_path: Path) -> None:
    client, conn, _ = client_for(tmp_path)
    proposal_id = seed_staged_quarantine(conn)

    approve = client.post(
        f"/quarantine/staged/{proposal_id}/approve",
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )
    assert approve.status_code == 200
    assert "Approved. It moves out on the next commit" in approve.text
    assert proposal_status(conn, proposal_id) == "approved"

    conn.execute("UPDATE proposals SET status='proposed' WHERE id=?", (proposal_id,))
    conn.execute(
        """
        UPDATE items SET state='quarantine-proposed'
        WHERE id=(SELECT item_id FROM proposals WHERE id=?)
        """,
        (proposal_id,),
    )
    unstage = client.post(
        f"/quarantine/staged/{proposal_id}/unstage",
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )

    assert unstage.status_code == 200
    assert "Kept. It will be filed normally" in unstage.text
    row = conn.execute(
        "SELECT action, dest_root FROM proposals WHERE id=?", (proposal_id,)
    ).fetchone()
    assert dict(row) == {"action": "move", "dest_root": "library"}


def seed_executed_quarantine(conn, settings: Settings) -> int:
    (settings.inbox_dir / "dupe.txt").write_text("dupe", encoding="utf-8")
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    plan_id = create_plan(conn, [quarantine_operation("dupe.txt", date="2026-07-22")], settings)
    approve_plan(conn, plan_id, settings)
    execute_plan(conn, plan_id, settings)
    return int(conn.execute("SELECT id FROM quarantine_entries").fetchone()[0])


def seed_staged_quarantine(conn) -> int:
    item_id = insert_item(conn, "copy.txt", "quarantine-proposed")
    return upsert_proposal(
        conn,
        item_id=item_id,
        category="documents",
        clean_name="copy.txt",
        dest_relpath="2026-07-22/copy.txt",
        confidence=0.99,
        evidence=[EvidenceEntry("heuristic", "duplicate", "same fingerprint", 0.99)],
        action="quarantine",
        dest_root="quarantine",
    )


def seed_similar_flag(conn, settings: Settings) -> None:
    (settings.inbox_dir / "left.jpg").write_text("left", encoding="utf-8")
    (settings.inbox_dir / "right.jpg").write_text("right", encoding="utf-8")
    left = insert_item(conn, "left.jpg", "proposed", size=4)
    right = insert_item(conn, "right.jpg", "proposed", size=5)
    conn.execute(
        """
        INSERT INTO similar_media_flags(item_id, similar_item_id, kind, score, created_at)
        VALUES (?, ?, 'image', 0.91, 'now')
        """,
        (left, right),
    )


def insert_item(conn, relpath: str, state: str, size: int = 1) -> int:
    cursor = conn.execute(
        """
        INSERT INTO items(
          root, relpath, size, mtime_ns, fingerprint, state, first_seen_at, last_seen_at
        )
        VALUES ('inbox', ?, ?, 1, ?, ?, 'now', 'now')
        """,
        (relpath, size, relpath, state),
    )
    return int(cursor.lastrowid)


def proposal_status(conn, proposal_id: int) -> str:
    return conn.execute("SELECT status FROM proposals WHERE id=?", (proposal_id,)).fetchone()[0]


def test_quarantine_staged_rows_show_from_to_and_why(tmp_path: Path) -> None:
    client, conn, _ = client_for(tmp_path)
    seed_staged_quarantine(conn)

    page = client.get("/quarantine").text

    assert 'class="from-to"' in page
    assert "Evidence" in page
    # Humanized, not a bracket code.
    assert "duplicate: same fingerprint" in page


def test_marking_a_held_file_for_deletion_gathers_it_without_deleting_it(
    tmp_path: Path,
) -> None:
    """The out-tray had one answer — "put it back" — so being finished with a
    file meant going through quarantine by hand in a file manager. This is the
    other answer: one folder to empty deliberately, yourself.
    """
    client, conn, settings = client_for(tmp_path)
    entry_id = seed_executed_quarantine(conn, settings)

    response = client.post(
        f"/quarantine/delete-queue/{entry_id}",
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )

    # Nothing moves on the click. This used to move the file inside the request
    # handler, with no plan and nothing in Commit, and the only feedback was a
    # line appended to the bottom of the page.
    assert response.status_code == 200
    assert "Nothing is deleted" in response.text
    assert (settings.quarantine_dir / "2026-07-22/dupe.txt").exists()
    assert not (settings.quarantine_dir / "_to-delete/2026-07-22/dupe.txt").exists()
    plan = conn.execute(
        "SELECT id, status FROM plans WHERE quarantine_entry_id=?", (entry_id,)
    ).fetchone()
    assert plan["status"] == "approved"

    # It moves at Commit, and it is still not deleted.
    execute_plan(conn, plan["id"], settings)
    moved = settings.quarantine_dir / "_to-delete/2026-07-22/dupe.txt"
    assert moved.read_text(encoding="utf-8") == "dupe"
    assert not (settings.quarantine_dir / "2026-07-22/dupe.txt").exists()
    assert conn.execute("SELECT relpath FROM items WHERE root='quarantine'").fetchone()[0] == (
        "_to-delete/2026-07-22/dupe.txt"
    )
    assert "delete queue" in client.get("/quarantine?view=delete-queue").text.lower()


def test_a_file_in_the_delete_pile_can_still_be_put_back(tmp_path: Path) -> None:
    """Marked is not deleted, and the entry remembers where the file came from
    rather than where it is sitting now."""
    client, conn, settings = client_for(tmp_path)
    entry_id = seed_executed_quarantine(conn, settings)
    csrf = client.cookies["csrf_token"]

    # Into the delete queue, for real, through Commit.
    client.post(f"/quarantine/delete-queue/{entry_id}", headers={"x-csrf-token": csrf})
    plan_id = conn.execute(
        "SELECT id FROM plans WHERE quarantine_entry_id=?", (entry_id,)
    ).fetchone()[0]
    execute_plan(conn, plan_id, settings)
    assert (settings.quarantine_dir / "_to-delete/2026-07-22/dupe.txt").exists()

    # And back out again: the entry remembers where the file came from, not
    # where it is sitting now, so the delete queue is not a one-way door.
    restored = client.post(f"/quarantine/restore/{entry_id}", headers={"x-csrf-token": csrf})
    assert restored.status_code == 200
    restore_plan = conn.execute(
        "SELECT id FROM plans WHERE quarantine_entry_id=? AND status='approved'", (entry_id,)
    ).fetchone()[0]
    execute_plan(conn, restore_plan, settings)

    assert (settings.inbox_dir / "dupe.txt").read_text(encoding="utf-8") == "dupe"
    assert not (settings.quarantine_dir / "_to-delete/2026-07-22/dupe.txt").exists()


def test_a_staged_duplicate_can_go_straight_to_the_delete_pile(tmp_path: Path) -> None:
    """Otherwise being finished with a duplicate that has not moved yet costs
    two commits: one into quarantine, another to move it along."""
    client, conn, _ = client_for(tmp_path)
    proposal_id = seed_staged_quarantine(conn)

    response = client.post(
        f"/quarantine/staged/{proposal_id}/mark-delete",
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )

    assert response.status_code == 200
    row = conn.execute(
        "SELECT status, action, dest_root, dest_relpath FROM proposals WHERE id=?", (proposal_id,)
    ).fetchone()
    assert row["status"] == "approved"
    assert row["action"] == "quarantine"
    assert row["dest_relpath"].startswith("_to-delete/")
    assert "Nothing is deleted" in response.text


# --- a stale page, or a crafted request -------------------------------------
#
# The three staged buttons all wrote first and checked the lifecycle second, so
# an item that could not legally be approved produced an unhandled
# LifecycleError — a 500 — and `mark-delete` had already retargeted the
# proposal at the delete queue by the time it threw. None of this needed a
# crafted request: `Move it out` then `Keep it` on one un-reloaded page did it.

STAGED_ROUTES = ("approve", "unstage", "mark-delete")
# `quarantine-proposed` is the normal staged state, so it is not here.
ILLEGAL_STATES = ("committed", "quarantined")


def staged_snapshot(conn, proposal_id: int) -> tuple:
    row = conn.execute(
        "SELECT p.status, p.action, p.dest_root, p.dest_relpath, i.state "
        "FROM proposals p JOIN items i ON i.id = p.item_id WHERE p.id=?",
        (proposal_id,),
    ).fetchone()
    return tuple(row)


@pytest.mark.parametrize("route", STAGED_ROUTES)
@pytest.mark.parametrize("state", ILLEGAL_STATES)
def test_an_ineligible_staged_action_is_refused_not_a_fault(
    tmp_path: Path, route: str, state: str
) -> None:
    client, conn, settings = client_for(tmp_path)
    proposal_id = seed_staged_quarantine(conn)
    conn.execute(
        "UPDATE items SET state=? WHERE id=(SELECT item_id FROM proposals WHERE id=?)",
        (state, proposal_id),
    )
    before = staged_snapshot(conn, proposal_id)
    files_before = sorted(p.name for p in tmp_path.rglob("*") if p.is_file())
    plans_before = conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0]

    response = client.post(
        f"/quarantine/staged/{proposal_id}/{route}",
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )

    assert response.status_code == 409
    assert "no longer eligible" in response.text or "no longer waiting" in response.text
    # Nothing half-applied: not the proposal, not the item, not a plan, not a
    # file. A refusal that leaves a row rewritten is worse than the fault.
    assert staged_snapshot(conn, proposal_id) == before
    assert conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == plans_before
    assert sorted(p.name for p in tmp_path.rglob("*") if p.is_file()) == files_before

    # And again, because a person who gets a refusal presses the button again.
    again = client.post(
        f"/quarantine/staged/{proposal_id}/{route}",
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )
    assert again.status_code == 409
    assert staged_snapshot(conn, proposal_id) == before
    assert settings.quarantine_dir.exists()


@pytest.mark.parametrize("route", STAGED_ROUTES)
def test_a_staged_action_on_a_proposal_that_is_gone_is_refused(
    tmp_path: Path, route: str
) -> None:
    client, _, _ = client_for(tmp_path)

    response = client.post(
        f"/quarantine/staged/4242/{route}",
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )

    assert response.status_code == 409
    assert "no longer exists" in response.text


def test_approving_then_keeping_a_staged_row_is_not_a_fault(tmp_path: Path) -> None:
    """Two buttons on one page, pressed in order. This was the 500.

    Withdrawing an approval passes through `discovered`: the answer is taken
    back before the machine's suggestion stands again. Going straight from
    `approved` to `proposed` is what the lifecycle forbids, so that a duplicate
    found late cannot overwrite an answer already given.
    """
    client, conn, _ = client_for(tmp_path)
    proposal_id = seed_staged_quarantine(conn)
    csrf = client.cookies["csrf_token"]

    assert client.post(
        f"/quarantine/staged/{proposal_id}/approve", headers={"x-csrf-token": csrf}
    ).status_code == 200
    kept = client.post(
        f"/quarantine/staged/{proposal_id}/unstage", headers={"x-csrf-token": csrf}
    )

    assert kept.status_code == 200
    assert "filed normally" in kept.text
    row = conn.execute(
        "SELECT p.status, p.action, p.dest_root, i.state FROM proposals p "
        "JOIN items i ON i.id = p.item_id WHERE p.id=?",
        (proposal_id,),
    ).fetchone()
    assert (row["status"], row["action"], row["dest_root"], row["state"]) == (
        "proposed",
        "move",
        "library",
        "proposed",
    )


def test_a_staged_answer_lands_on_the_page_it_changed(tmp_path: Path) -> None:
    """Not one line swapped into a `<div>` at the foot of a long page, ending
    "Reload the page to see the list catch up"."""
    client, conn, _ = client_for(tmp_path)
    proposal_id = seed_staged_quarantine(conn)

    response = client.post(
        f"/quarantine/staged/{proposal_id}/approve",
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )

    assert "Reload the page" not in response.text
    assert "moves out on the next commit" in response.text
    assert "<h1>Quarantine</h1>" in response.text


# --- what a quarantine row says, and why -----------------------------------


def test_a_hand_quarantined_file_is_not_called_a_duplicate(tmp_path: Path) -> None:
    """Every entry recorded `exact_duplicate` regardless of why the file was
    set aside, so a file you sent here yourself from Review was described on
    this page as a byte-for-byte copy of something you already have."""
    client, conn, settings = client_for(tmp_path)
    (settings.inbox_dir / "unwanted.txt").write_text("mine", encoding="utf-8")
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    item_id = int(
        conn.execute("SELECT id FROM items WHERE relpath='unwanted.txt'").fetchone()[0]
    )
    # No duplicate evidence anywhere: this file is here because you said so.
    upsert_proposal(
        conn,
        item_id=item_id,
        category="documents",
        clean_name="unwanted.txt",
        dest_relpath="unwanted.txt",
        confidence=0.9,
        evidence=[EvidenceEntry("heuristic", "category", "you sent it here", 0.9)],
        action="quarantine",
        dest_root="quarantine",
    )
    plan_id = create_plan(conn, [quarantine_operation("unwanted.txt")], settings)
    approve_plan(conn, plan_id, settings)
    execute_plan(conn, plan_id, settings)

    reason = conn.execute("SELECT reason FROM quarantine_entries").fetchone()[0]
    body = client.get("/quarantine").text

    assert reason == "user"
    assert "byte-for-byte copy" not in body
    assert "you said you did not want it" in body
    assert "no reason recorded" not in body


def staged_duplicate(tmp_path: Path, *, twin: bool = True):
    """An inbox file whose bytes are already in the library, staged to be set
    aside — which is what the worker's duplicate pass produces."""
    client, conn, settings = client_for(tmp_path)
    (settings.inbox_dir / "dupe.txt").write_text("dupe", encoding="utf-8")
    if twin:
        filed = settings.library_dir / "Documents" / "dupe.txt"
        filed.parent.mkdir(parents=True, exist_ok=True)
        filed.write_text("dupe", encoding="utf-8")
        scan_root(conn, "library", settings.library_dir, settings)
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    item_id = int(conn.execute("SELECT id FROM items WHERE relpath='dupe.txt'").fetchone()[0])
    upsert_proposal(
        conn,
        item_id=item_id,
        category="documents",
        clean_name="dupe.txt",
        dest_relpath="dupe.txt",
        confidence=0.99,
        evidence=[
            EvidenceEntry(
                "heuristic", "duplicate", "exact duplicate of library:Documents/dupe.txt", 0.99
            )
        ],
        action="quarantine",
        dest_root="quarantine",
    )
    plan_id = create_plan(conn, [quarantine_operation("dupe.txt")], settings)
    approve_plan(conn, plan_id, settings)
    return client, conn, settings, plan_id


def test_a_duplicate_is_still_recorded_as_one(tmp_path: Path) -> None:
    client, conn, settings, plan_id = staged_duplicate(tmp_path)

    execute_plan(conn, plan_id, settings)

    assert conn.execute("SELECT reason FROM quarantine_entries").fetchone()[0] == "exact_duplicate"
    assert "byte-for-byte copy" in client.get("/quarantine").text


def test_the_held_copy_says_which_file_it_matches(tmp_path: Path) -> None:
    """"A copy of something you already have" without saying *what* leaves no
    way to judge whether restoring this one is worth doing."""
    client, conn, settings, plan_id = staged_duplicate(tmp_path)

    execute_plan(conn, plan_id, settings)

    entry = conn.execute("SELECT duplicate_of FROM quarantine_entries").fetchone()
    assert entry["duplicate_of"] is not None
    assert "library/Documents/dupe.txt" in client.get("/quarantine").text


def test_a_duplicate_whose_library_copy_has_gone_is_not_set_aside(
    tmp_path: Path,
) -> None:
    """The safety property of the whole workflow.

    The library copy is the only thing that makes the arrival redundant. If it
    is deleted between staging and Commit — by hand, by another tool, by a
    restore — quarantining the arrival leaves no copy anywhere. Nothing was
    deleted and nothing was overwritten, and the file is gone.
    """
    client, conn, settings, plan_id = staged_duplicate(tmp_path)
    (settings.library_dir / "Documents" / "dupe.txt").unlink()

    summary = execute_plan(conn, plan_id, settings)

    assert summary.done == 0
    assert summary.skipped_changed == 1
    assert (settings.inbox_dir / "dupe.txt").is_file()
    assert conn.execute("SELECT COUNT(*) FROM quarantine_entries").fetchone()[0] == 0
    outcome = conn.execute("SELECT outcome FROM history ORDER BY id DESC").fetchone()
    assert "no_matching_library_copy" in outcome["outcome"]


def test_a_file_you_set_aside_yourself_needs_no_twin(tmp_path: Path) -> None:
    """The check is about duplicate evidence, not about quarantine. Sending a
    file to Quarantine from Review is a decision, and it has no twin to lose."""
    client, conn, settings, plan_id = staged_duplicate(tmp_path, twin=False)
    conn.execute(
        "UPDATE proposals SET evidence=? WHERE item_id IN"
        " (SELECT id FROM items WHERE root='inbox')",
        ('[{"source": "heuristic", "field": "category", "detail": "you sent it", '
         '"weight": 1.0}]',),
    )

    summary = execute_plan(conn, plan_id, settings)

    assert summary.done == 1
    assert conn.execute("SELECT reason FROM quarantine_entries").fetchone()[0] == "user"


def test_a_moved_out_row_leads_with_the_name_and_hides_the_rest(tmp_path: Path) -> None:
    """A table of four columns could not reflow: the full relpath wrapped to
    four lines on a phone and pushed the actions off the screen."""
    client, conn, settings = client_for(tmp_path)
    seed_executed_quarantine(conn, settings)

    body = client.get("/quarantine").text

    assert "<table" not in body.split("qlist")[1].split("</section>")[0]
    assert 'class="qrow' in body
    # The name identifies the row; the path and the plan are behind Details.
    assert "qrow-name" in body
    assert "<summary>Details</summary>" in body
    assert "came from" in body


def test_quarantine_actions_and_delete_pile_semantics_are_unchanged(tmp_path: Path) -> None:
    """The rework is presentation. Nothing about what the buttons do moved."""
    client, conn, settings = client_for(tmp_path)
    entry_id = seed_executed_quarantine(conn, settings)

    body = client.get("/quarantine").text

    assert f"/quarantine/restore/{entry_id}" in body
    assert f"/quarantine/delete-queue/{entry_id}" in body
    assert "LibrAIry never deletes anything." in body
    assert "delete them yourself" in body


def test_quarantine_empty_state_says_how_files_get_here(tmp_path: Path) -> None:
    client, _conn, _settings = client_for(tmp_path)

    body = client.get("/quarantine").text

    assert "Nothing is being held." in body
    assert "Nothing has been moved out yet." in body
    assert "never on their own" in body


def test_quarantine_rows_can_show_what_the_file_actually_is(tmp_path: Path) -> None:
    """Deciding whether a file goes back is a question about what it is, and a
    UUID filename does not answer it."""
    client, conn, settings = client_for(tmp_path)
    entry_id = seed_executed_quarantine(conn, settings)
    item_id = int(
        conn.execute("SELECT item_id FROM quarantine_entries WHERE id=?", (entry_id,)).fetchone()[0]
    )

    body = client.get("/quarantine").text

    assert f'data-preview-url="/preview/items/{item_id}"' in body
    assert f'id="qpreview-{entry_id}"' in body
    # The preview card carries an expand control, so the page must carry the
    # viewer it opens — otherwise Quarantine grows the dead button Browse had.
    assert 'id="lightbox"' in body
    assert "/static/lightbox.js" in body


def test_a_staged_quarantine_can_be_previewed_before_you_decide(tmp_path: Path) -> None:
    client, conn, _settings = client_for(tmp_path)
    proposal_id = seed_staged_quarantine(conn)
    item_id = int(
        conn.execute("SELECT item_id FROM proposals WHERE id=?", (proposal_id,)).fetchone()[0]
    )

    body = client.get("/quarantine").text

    assert f'data-preview-url="/preview/items/{item_id}"' in body
    assert f'id="spreview-{proposal_id}"' in body


# --- what Undo leaves behind ---------------------------------------------------
#
# Found while proving the optimization-adoption architecture, and nothing to do
# with optimization: undoing any quarantine put the file back and left the item
# row reading `quarantined`. Not cosmetic — `quarantined` may legally only
# become `discovered`, so the row was nearly frozen, and the index went on
# describing an inbox file as a quarantined one.


def test_undoing_a_quarantine_puts_the_item_back_too(tmp_path: Path) -> None:
    from librairy.history import undo_plan
    from librairy.quarantine import quarantine_operation

    _client, conn, settings = client_for(tmp_path)
    (settings.inbox_dir / "dupe.txt").write_text("dupe", encoding="utf-8")
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    plan_id = create_plan(
        conn, [quarantine_operation("dupe.txt", date="2026-08-15")], settings
    )
    approve_plan(conn, plan_id, settings)
    execute_plan(conn, plan_id, settings)
    assert conn.execute("SELECT state FROM items").fetchone()[0] == "quarantined"

    undo_plan(conn, plan_id, settings)

    row = conn.execute("SELECT root, relpath, state FROM items").fetchone()
    assert (row["root"], row["relpath"], row["state"]) == (
        "inbox",
        "dupe.txt",
        "discovered",
    )
    assert (settings.inbox_dir / "dupe.txt").exists()


def test_undoing_a_quarantine_reindexes_the_file(tmp_path: Path) -> None:
    """The index copies the item's root, so it had to be told as well."""
    from librairy.history import undo_plan
    from librairy.quarantine import quarantine_operation

    _client, conn, settings = client_for(tmp_path)
    (settings.inbox_dir / "dupe.txt").write_text("dupe", encoding="utf-8")
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    plan_id = create_plan(
        conn, [quarantine_operation("dupe.txt", date="2026-08-15")], settings
    )
    approve_plan(conn, plan_id, settings)
    execute_plan(conn, plan_id, settings)
    assert conn.execute("SELECT root FROM search_fts").fetchone()[0] == "quarantine"

    undo_plan(conn, plan_id, settings)

    assert conn.execute("SELECT root FROM search_fts").fetchone()[0] == "inbox"
