"""Clearing entries whose file is gone: what it resolves, and what it keeps.

`forget_vanished` sounds more destructive than it is. It marks proposals
superseded and puts the item back to `discovered`. It deletes nothing — not the
item, not the proposal row, not its evidence, not the search entry, not
history, and least of all a file. These tests are what let a button say so.

Deterministic: no AI provider, no catalog, no network.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from librairy.config import Settings
from librairy.db import connect
from librairy.lifecycle import (
    LifecycleError,
    forget_vanished,
    vanished_count,
    vanished_entries,
)
from librairy.models import EvidenceEntry
from librairy.proposals import upsert_proposal
from librairy.scanner import scan_root
from librairy.search import SearchFilters, search_items
from librairy.web.app import create_app


def settings_for(tmp_path: Path) -> Settings:
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        HOST_INBOX_DIR=Path("/mnt/user/inbox"),
        HOST_LIBRARY_DIR=Path("/mnt/user/library"),
        FILE_STABILITY_SECONDS=0,
        _env_file=None,
    )
    for root in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        root.mkdir(parents=True, exist_ok=True)
    return settings


def client_for(tmp_path: Path):
    settings = settings_for(tmp_path)
    conn = connect(settings)
    client = TestClient(create_app(settings, conn))
    client.post("/setup", data={"password": "correct horse battery"})
    return client, conn, settings


def root_dir(settings: Settings, root: str) -> Path:
    return {"inbox": settings.inbox_dir, "library": settings.library_dir}[root]


def seed(
    conn,
    settings: Settings,
    relpath: str,
    *,
    root: str = "inbox",
    status: str = "proposed",
    gone: bool = False,
) -> int:
    """One classified file, optionally deleted behind LibrAIry's back."""
    path = root_dir(settings, root) / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(relpath, encoding="utf-8")
    scan_root(conn, root, root_dir(settings, root), settings)
    item_id = conn.execute(
        "SELECT id FROM items WHERE root=? AND relpath=?", (root, relpath)
    ).fetchone()[0]
    upsert_proposal(
        conn,
        item_id=item_id,
        category="shows",
        clean_name=Path(relpath).name,
        dest_relpath=f"Shows/{Path(relpath).name}",
        confidence=0.82,
        evidence=[EvidenceEntry("tvmaze", "show", "Best Shot", 0.82)],
    )
    if status != "proposed":
        conn.execute("UPDATE proposals SET status=? WHERE item_id=?", (status, item_id))
        if status in ("approved", "postponed"):
            conn.execute("UPDATE items SET state=? WHERE id=?", (status, item_id))
    if gone:
        path.unlink()
        scan_root(conn, root, root_dir(settings, root), settings)
    return item_id


def paths(entries) -> set[str]:
    return {entry["relpath"] for entry in entries}


# --- what is listed ---------------------------------------------------------


def test_a_missing_entry_is_listed(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    seed(conn, settings, "gone.mkv", gone=True)

    assert paths(vanished_entries(conn)) == {"gone.mkv"}
    assert vanished_count(conn) == 1


def test_a_live_entry_is_not_listed(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    seed(conn, settings, "here.mkv")

    assert vanished_entries(conn) == []
    assert vanished_count(conn) == 0


def test_an_already_resolved_proposal_is_not_listed(tmp_path: Path) -> None:
    """A rejected proposal is not waiting on anybody. Its file going missing
    changes nothing about it, so there is nothing to clear — which is why the
    count of missing *records* and the count of *entries to clear* differ."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    seed(conn, settings, "rejected.mkv", status="rejected", gone=True)

    assert vanished_count(conn) == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM items WHERE missing_since IS NOT NULL"
    ).fetchone()[0] == 1


def test_the_two_roots_are_counted_apart(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    seed(conn, settings, "inbox-gone.mkv", root="inbox", gone=True)
    seed(conn, settings, "Shows/lib-gone.mkv", root="library", gone=True)

    assert vanished_count(conn) == 2
    assert vanished_count(conn, root="inbox") == 1
    assert vanished_count(conn, root="library") == 1
    assert paths(vanished_entries(conn, root="inbox")) == {"inbox-gone.mkv"}


def test_the_listing_carries_what_you_need_to_recognise_it(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    seed(conn, settings, "_drop/Test.Show.S01E01.mkv", gone=True)

    entry = vanished_entries(conn)[0]

    assert entry["relpath"] == "_drop/Test.Show.S01E01.mkv"
    assert entry["category"] == "shows"
    assert entry["missing_since"]
    assert "Best Shot" in entry["evidence"], "the reason it was classified that way"
    assert "host_path" not in dict(entry), "the listing selects no host path at all"


# --- what clearing does -----------------------------------------------------


def test_clearing_resolves_the_proposal_and_deletes_nothing(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    item_id = seed(conn, settings, "gone.mkv", gone=True)
    before = (
        conn.execute("SELECT COUNT(*) FROM items").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM proposals").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM search_fts").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM history").fetchone()[0],
    )

    assert forget_vanished(conn) == 1

    after = (
        conn.execute("SELECT COUNT(*) FROM items").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM proposals").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM search_fts").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM history").fetchone()[0],
    )
    assert before == after, "nothing is deleted, only re-labelled"
    row = conn.execute(
        "SELECT status, evidence FROM proposals WHERE item_id=?", (item_id,)
    ).fetchone()
    assert row["status"] == "superseded"
    assert "Best Shot" in row["evidence"], "the evidence stays in the row"
    item = conn.execute("SELECT state, missing_since FROM items WHERE id=?", (item_id,)).fetchone()
    assert item["state"] == "discovered"
    assert item["missing_since"] is not None, "still missing — clearing is not finding"


def test_the_file_itself_is_never_touched(tmp_path: Path) -> None:
    """The one it could still reach: a live file beside a vanished one."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    seed(conn, settings, "here.mkv")
    seed(conn, settings, "gone.mkv", gone=True)

    forget_vanished(conn)

    assert (settings.inbox_dir / "here.mkv").exists()
    assert sorted(p.name for p in settings.inbox_dir.iterdir()) == ["here.mkv"]


def test_clearing_leaves_live_entries_alone(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    live = seed(conn, settings, "here.mkv")
    seed(conn, settings, "gone.mkv", gone=True)

    assert forget_vanished(conn) == 1

    assert conn.execute(
        "SELECT status FROM proposals WHERE item_id=?", (live,)
    ).fetchone()[0] == "proposed"


def test_clearing_one_root_leaves_the_other(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    seed(conn, settings, "inbox-gone.mkv", root="inbox", gone=True)
    seed(conn, settings, "Shows/lib-gone.mkv", root="library", gone=True)

    assert forget_vanished(conn, root="inbox") == 1

    assert vanished_count(conn, root="inbox") == 0
    assert vanished_count(conn, root="library") == 1


def test_clearing_twice_is_a_no_op(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    seed(conn, settings, "gone.mkv", gone=True)

    assert forget_vanished(conn) == 1
    snapshot = [
        tuple(row) for row in conn.execute("SELECT id, status FROM proposals ORDER BY id")
    ]

    assert forget_vanished(conn) == 0
    assert forget_vanished(conn) == 0
    assert [
        tuple(row) for row in conn.execute("SELECT id, status FROM proposals ORDER BY id")
    ] == snapshot


def test_an_approved_entry_is_cleared_too(tmp_path: Path) -> None:
    """Approved and waiting is exactly the case that cannot commit."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    item_id = seed(conn, settings, "gone.mkv", status="approved", gone=True)

    assert forget_vanished(conn) == 1

    assert conn.execute("SELECT state FROM items WHERE id=?", (item_id,)).fetchone()[0] == (
        "discovered"
    )


def test_an_illegal_transition_is_refused_rather_than_written(tmp_path: Path) -> None:
    """It used to write items.state directly — the one place that skipped the
    lifecycle check. Nothing reachable is illegal today; this keeps it so."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    item_id = seed(conn, settings, "gone.mkv", gone=True)
    conn.execute("UPDATE items SET state='nonsense' WHERE id=?", (item_id,))

    with pytest.raises(LifecycleError):
        forget_vanished(conn)


def test_the_search_entry_stops_claiming_a_category_nobody_proposed(tmp_path: Path) -> None:
    """The index copies category off the live proposal. Superseding one without
    re-syncing left it asserting `shows` with no proposal saying so."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    item_id = seed(conn, settings, "gone.mkv", gone=True)
    assert conn.execute(
        "SELECT category FROM search_fts WHERE item_id=?", (item_id,)
    ).fetchone()[0] == "shows"

    forget_vanished(conn)

    assert conn.execute(
        "SELECT category FROM search_fts WHERE item_id=?", (item_id,)
    ).fetchone()[0] != "shows"
    assert conn.execute(
        "SELECT COUNT(*) FROM search_fts WHERE item_id=?", (item_id,)
    ).fetchone()[0] == 1, "re-synced, not removed"


# --- the surfaces around it -------------------------------------------------


def test_search_stays_free_of_it_before_and_after(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    seed(conn, settings, "gone.mkv", gone=True)
    assert search_items(conn, "gone", SearchFilters(root=None)) == []

    forget_vanished(conn)

    assert search_items(conn, "gone", SearchFilters(root=None)) == []


def test_review_and_commit_stay_free_of_it(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    seed(conn, settings, "gone.mkv", status="approved", gone=True)

    queue = client.get("/review").text.split('id="review-list"')[1]
    commit = client.get("/commit").text
    client.post(
        "/review/forget-missing",
        data={"csrf_token": client.cookies["csrf_token"], "root": "inbox"},
        headers={"x-csrf-token": client.cookies["csrf_token"]},
        follow_redirects=False,
    )

    assert "gone.mkv" not in queue
    assert "Nothing waiting to commit" in commit
    assert "gone.mkv" not in client.get("/review").text.split('id="review-list"')[1]


def test_history_still_answers_for_it(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    seed(conn, settings, "gone.mkv", gone=True)
    conn.execute(
        """
        INSERT INTO history(ts, plan_id, op_id, action, src_root, src_relpath,
                            dest_root, dest_relpath, fingerprint, outcome)
        VALUES ('2026-08-01T10:00:00+00:00', 'plan-ghost', 1, 'move', 'inbox',
                'gone.mkv', 'library', 'Shows/gone.mkv', 'abc', 'ok')
        """
    )

    forget_vanished(conn)

    assert "gone.mkv" in client.get("/history").text


def test_a_file_that_comes_back_leaves_the_list_without_being_cleared(tmp_path: Path) -> None:
    """No cleanup action is needed, and the list must not remember it."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    seed(conn, settings, "away.mkv", gone=True)
    assert vanished_count(conn) == 1

    (settings.inbox_dir / "away.mkv").write_text("back", encoding="utf-8")
    scan_root(conn, "inbox", settings.inbox_dir, settings)

    assert vanished_count(conn) == 0
    assert vanished_entries(conn) == []
    assert conn.execute(
        "SELECT status FROM proposals"
    ).fetchone()[0] == "proposed", "the decision survived the round trip"


# --- the page ---------------------------------------------------------------


def test_the_notice_lists_the_entries_and_scopes_its_button(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    seed(conn, settings, "_drop/Test.Show.S01E01.mkv", gone=True)
    seed(conn, settings, "Shows/lib-gone.mkv", root="library", gone=True)

    page = client.get("/review").text

    assert "Test.Show.S01E01.mkv" in page
    assert "lib-gone.mkv" in page
    # The whole path, root included — "/_drop/..." with the root missing is
    # not a location anybody can act on.
    assert "inbox/_drop/Test.Show.S01E01.mkv" in page
    assert "library/Shows/lib-gone.mkv" in page
    assert "Clear 1 inbox entry" in page
    assert "Clear 1 library entry" in page
    assert 'name="root" value="inbox"' in page


def test_the_confirmation_says_what_actually_happens(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    seed(conn, settings, "gone.mkv", gone=True)

    page = client.get("/review").text

    assert "no record is deleted" in page
    assert "History keeps everything" in page
    assert "no file is touched" in page.lower()
    for forbidden in ("delete the file", "delete files", "permanently"):
        assert forbidden not in page.lower()


def test_no_host_path_reaches_the_notice(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    seed(conn, settings, "gone.mkv", gone=True)

    notice = client.get("/review").text.split('id="review-list"')[0]

    assert "/mnt/user" not in notice
    assert str(settings.inbox_dir) not in notice
    assert str(tmp_path) not in notice


def test_nothing_is_said_when_there_is_nothing_to_say(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    seed(conn, settings, "here.mkv")

    review = client.get("/review").text
    dashboard = client.get("/dashboard").text

    assert "moved or deleted outside LibrAIry" not in review
    assert "forget-missing" not in review
    assert "no longer on disk" not in dashboard


def test_the_dashboard_explains_the_number_and_links_to_it(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    seed(conn, settings, "gone.mkv", gone=True)

    dashboard = client.get("/dashboard").text

    assert "no longer on disk" in dashboard
    assert 'href="/review"' in dashboard


def test_an_unknown_root_clears_nothing(tmp_path: Path) -> None:
    """Rather than falling back to every root, which is how a scoped button
    quietly becomes an unscoped one."""
    client, conn, settings = client_for(tmp_path)
    seed(conn, settings, "gone.mkv", gone=True)

    for payload in ({"root": "everything"}, {}):
        client.post(
            "/review/forget-missing",
            data={"csrf_token": client.cookies["csrf_token"], **payload},
            headers={"x-csrf-token": client.cookies["csrf_token"]},
            follow_redirects=False,
        )

    assert vanished_count(conn) == 1


# --- presentation -----------------------------------------------------------


def test_a_missing_item_does_not_report_zero_bytes_as_a_fact(tmp_path: Path) -> None:
    """The scanner keeps the last size it measured rather than zeroing it, so
    the number is real — it was the present tense that lied."""
    client, conn, settings = client_for(tmp_path)
    path = settings.inbox_dir / "gone.mkv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * 4096)
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    item_id = conn.execute("SELECT id FROM items").fetchone()[0]
    path.unlink()
    scan_root(conn, "inbox", settings.inbox_dir, settings)

    page = client.get(f"/items/{item_id}").text

    assert "last known size: 4.0 KB" in page
    assert "size: 0" not in page


def test_a_missing_item_that_was_empty_says_so_without_lying(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    item_id = seed(conn, settings, "empty.mkv")
    conn.execute("UPDATE items SET size=0 WHERE id=?", (item_id,))
    (settings.inbox_dir / "empty.mkv").unlink()
    scan_root(conn, "inbox", settings.inbox_dir, settings)

    page = client.get(f"/items/{item_id}").text

    assert "last known size: not recorded" in page
    assert "Not on disk" in page


def test_a_live_item_still_shows_a_plain_size(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    path = settings.inbox_dir / "here.mkv"
    path.write_bytes(b"x" * 2048)
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    item_id = conn.execute("SELECT id FROM items").fetchone()[0]

    page = client.get(f"/items/{item_id}").text

    assert "size: 2.0 KB" in page
    assert "last known size" not in page


def test_a_library_destination_reads_as_filing(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    item_id = seed(conn, settings, "gone.mkv", gone=True)

    detail = client.get(f"/items/{item_id}").text
    notice = client.get("/review").text

    assert "Would have been filed as" in detail
    assert "Would have been filed as" in notice
    assert "delete queue" not in notice


def test_a_quarantine_destination_is_not_called_filing(tmp_path: Path) -> None:
    """One of the author's seven was staged for quarantine before it vanished.
    Under a heading saying "destination" it read as one more filing decision."""
    client, conn, settings = client_for(tmp_path)
    item_id = seed(conn, settings, "aside.mkv", gone=True)
    conn.execute(
        "UPDATE proposals SET dest_root='quarantine', dest_relpath='2026-08-06/aside.mkv' "
        "WHERE item_id=?",
        (item_id,),
    )

    notice = client.get("/review").text

    assert "Set aside" in notice
    assert "Would have been filed as" not in notice


def test_a_delete_pile_destination_names_the_delete_queue(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    item_id = seed(conn, settings, "doomed.mkv", gone=True)
    conn.execute(
        "UPDATE proposals SET dest_root='quarantine', "
        "dest_relpath='_to-delete/2026-08-06/doomed.mkv' WHERE item_id=?",
        (item_id,),
    )

    notice = client.get("/review").text

    assert "Headed for the delete queue" in notice
    assert "Set aside" not in notice


def test_raw_proposal_statuses_do_not_reach_the_page(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    seed(conn, settings, "waiting.mkv", gone=True)
    seed(conn, settings, "approved.mkv", status="approved", gone=True)

    notice = client.get("/review").text.split('id="review-list"')[0]

    assert "Waiting for review" in notice
    assert "Approved, not committed" in notice
    assert "· proposed" not in notice
    assert "· approved" not in notice


def test_the_notice_reconciles_its_own_arithmetic(tmp_path: Path) -> None:
    """Seven clearable and eight missing is correct and looks like a bug unless
    the page says why. The author's eighth is a rejected proposal."""
    client, conn, settings = client_for(tmp_path)
    for index in range(3):
        seed(conn, settings, f"waiting-{index}.mkv", gone=True)
    seed(conn, settings, "rejected.mkv", status="rejected", gone=True)

    notice = client.get("/review").text.split('id="review-list"')[0]

    assert "3 file" in notice
    assert "1 other inbox record" in notice
    assert "already resolved" in notice
    assert "nothing to clear" in notice


def test_nothing_extra_is_said_when_every_missing_record_is_clearable(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    seed(conn, settings, "waiting.mkv", gone=True)

    notice = client.get("/review").text.split('id="review-list"')[0]

    assert "already resolved" not in notice


# --- the invariants this pass must not have broken --------------------------


def test_the_whole_missing_lifecycle_still_holds_end_to_end(tmp_path: Path) -> None:
    """One pass over everything the last three tasks established, so a change
    to presentation cannot quietly move any of it."""
    client, conn, settings = client_for(tmp_path)
    live = seed(conn, settings, "here.mkv")
    gone = seed(conn, settings, "gone.mkv", status="approved", gone=True)

    # Excluded from Search, from the Review queue, and from Commit.
    assert search_items(conn, "gone", SearchFilters(root=None)) == []
    assert "gone.mkv" not in client.get("/review").text.split('id="review-list"')[1]
    assert "Nothing waiting to commit" in client.get("/commit").text
    # Still reachable directly, and honest about it.
    detail = client.get(f"/items/{gone}")
    assert detail.status_code == 200
    assert "Not on disk" in detail.text
    assert "preview-expand" not in detail.text
    # The live one is unaffected throughout.
    assert len(search_items(conn, "here", SearchFilters(root=None))) == 1

    forget_vanished(conn, root="inbox")

    assert search_items(conn, "gone", SearchFilters(root=None)) == [], "still not searchable"
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 2
    assert conn.execute(
        "SELECT status FROM proposals WHERE item_id=?", (live,)
    ).fetchone()[0] == "proposed"

    (settings.inbox_dir / "gone.mkv").write_text("back", encoding="utf-8")
    scan_root(conn, "inbox", settings.inbox_dir, settings)

    assert conn.execute(
        "SELECT missing_since FROM items WHERE id=?", (gone,)
    ).fetchone()[0] is None
    assert vanished_count(conn) == 0
    page = client.get(f"/items/{gone}").text
    assert "Not on disk" not in page
    assert "last known size" not in page
