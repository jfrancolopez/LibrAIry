from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from librairy.config import Settings
from librairy.db import connect
from librairy.models import EvidenceEntry
from librairy.proposals import upsert_proposal
from librairy.web.app import create_app


def client_for(tmp_path: Path) -> tuple[TestClient, object]:
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        _env_file=None,
    )
    conn = connect(settings)
    client = TestClient(create_app(settings, conn))
    client.post("/setup", data={"password": "correct horse battery"})
    return client, conn


def test_review_renders_groups_filters_and_htmx_pagination(tmp_path: Path) -> None:
    client, conn = client_for(tmp_path)
    album = insert_group(conn, "album", "Kind of Blue")
    event = insert_group(conn, "photo_event", "Italy")
    seed_proposal(conn, "music/a.flac", "music", "Music/A.flac", 0.95, album)
    seed_proposal(conn, "music/b.flac", "music", "Music/B.flac", 0.94, album)
    # One file with a group of its own: a heading, a select-all and a section
    # margin for a decision you were going to make one row at a time anyway.
    seed_proposal(conn, "photos/a.jpg", "photos", "Photos/A.jpg", 0.75, event)
    seed_proposal(conn, "docs/a.txt", "documents", None, 0.4, None)

    page = client.get("/review")
    filtered = client.get("/review/list?category=music")

    assert "Kind of Blue" in page.text
    assert "2 files" in page.text  # the heading counts the group, not the preview
    assert "Italy" not in page.text
    assert "hx-get=\"/review/list\"" in page.text
    assert "music/a.flac" in filtered.text
    assert "photos/a.jpg" not in filtered.text


def test_review_evidence_labels_cloud_marker_and_pending_edit(tmp_path: Path) -> None:
    client, conn = client_for(tmp_path)
    item = insert_item(conn, "pending.bin")
    upsert_proposal(
        conn,
        item_id=item,
        category="misc",
        clean_name="pending.bin",
        dest_relpath=None,
        confidence=0.3,
        evidence=[
            EvidenceEntry("heuristic", "category", "unknown item fallback", 0.2),
            EvidenceEntry("ai", "category", "openai/gpt-4o-mini/cloud: guessed", 0.7),
        ],
    )

    response = client.get("/review")

    # Evidence renders as plain-language "why" lines, not bracket codes.
    assert "Looks like unknown item fallback" in response.text
    assert "AI · openai" in response.text
    assert ">Evidence</button>" in response.text
    assert "no destination yet" in response.text
    assert "Destination" in response.text


def test_review_large_seed_is_paginated_and_fast(tmp_path: Path) -> None:
    client, conn = client_for(tmp_path)
    evidence = [EvidenceEntry("heuristic", "category", "bulk", 0.5)]
    for index in range(5000):
        item = insert_item(conn, f"bulk/{index}.txt")
        upsert_proposal(
            conn,
            item_id=item,
            category="documents",
            clean_name=f"{index}.txt",
            dest_relpath=f"Documents/{index}.txt",
            confidence=0.5,
            evidence=evidence,
        )
    started = time.perf_counter()

    response = client.get("/review/list")
    elapsed = time.perf_counter() - started

    assert response.status_code == 200
    assert "of <strong>5000</strong>" in response.text  # 5000 loose files = 5000 decisions
    # Row checkboxes only — the header select-all is a checkbox too.
    #  Twenty-five *decisions*, not fifty files. Every proposal in this seed is
    #  loose, so a decision is a file and the page holds twenty-five of them;
    #  a seed of albums would hold twenty-five albums.
    assert response.text.count('name="proposal_id" type="checkbox"') == 25
    assert "Page <strong>1</strong> of <strong>200</strong>" in response.text
    assert "Next" in response.text
    assert elapsed < 1.0


def test_batch_approve_filtered_set_only_updates_matches(tmp_path: Path) -> None:
    client, conn = client_for(tmp_path)
    music_id = seed_proposal(conn, "music/a.flac", "music", "Music/A.flac", 0.9, None)
    doc_id = seed_proposal(conn, "docs/a.txt", "documents", "Documents/A.txt", 0.9, None)

    response = client.post(
        "/review/action",
        data={"action": "approve", "all_matching": "true", "category": "music"},
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )

    assert response.status_code == 200
    assert proposal_status(conn, music_id) == "approved"
    assert item_state(conn, music_id) == "approved"
    assert proposal_status(conn, doc_id) == "proposed"


def test_reject_and_postpone_transitions_leave_default_queue(tmp_path: Path) -> None:
    client, conn = client_for(tmp_path)
    rejected_id = seed_proposal(conn, "reject.txt", "documents", "Documents/reject.txt", 0.8, None)
    postponed_id = seed_proposal(conn, "later.txt", "documents", "Documents/later.txt", 0.8, None)

    reject = client.post(
        "/review/action",
        data={"action": "reject", "proposal_id": str(rejected_id)},
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )
    postpone = client.post(
        "/review/action",
        data={"action": "postpone", "proposal_id": str(postponed_id)},
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )
    queue = client.get("/review/list")

    assert reject.status_code == 200
    assert postpone.status_code == 200
    assert proposal_status(conn, rejected_id) == "rejected"
    assert item_state(conn, rejected_id) == "pending"
    assert proposal_status(conn, postponed_id) == "postponed"
    assert item_state(conn, postponed_id) == "postponed"
    assert "reject.txt" not in queue.text
    assert "later.txt" not in queue.text


def test_review_actions_are_csrf_protected_and_keyboard_controls_render(tmp_path: Path) -> None:
    client, conn = client_for(tmp_path)
    proposal_id = seed_proposal(conn, "a.txt", "documents", "Documents/a.txt", 0.9, None)

    blocked = client.post(
        "/review/action", data={"action": "approve", "proposal_id": str(proposal_id)}
    )
    page = client.get("/review")

    assert blocked.status_code == 403
    assert proposal_status(conn, proposal_id) == "proposed"
    assert "value=\"approve\"" in page.text
    assert "aria-label=\"select a.txt\"" in page.text


@pytest.mark.parametrize(
    "dest_relpath",
    ["../../etc/x", "/tmp/x", "Documents\\x.txt", "Documents/bad\x00.txt", "Documents/{token}.txt"],
)
def test_edit_rejects_hostile_destinations_without_saving(
    tmp_path: Path, dest_relpath: str
) -> None:
    client, conn = client_for(tmp_path)
    proposal_id = seed_proposal(conn, "a.txt", "documents", "Documents/a.txt", 0.9, None)

    response = client.post(
        f"/review/proposals/{proposal_id}/edit",
        data={
            "category": "documents",
            "clean_name": "safe.txt",
            "dest_relpath": dest_relpath,
        },
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )

    row = conn.execute(
        "SELECT clean_name, dest_relpath FROM proposals WHERE id=?", (proposal_id,)
    ).fetchone()
    assert response.status_code == 422
    assert row["clean_name"] == "a.txt"
    assert row["dest_relpath"] == "Documents/a.txt"


def test_edit_suffixes_existing_file_and_live_proposal_collisions(tmp_path: Path) -> None:
    client, conn = client_for(tmp_path)
    settings = client.app.state.settings
    (settings.library_dir / "Documents").mkdir(parents=True)
    (settings.library_dir / "Documents/a.txt").write_text("existing", encoding="utf-8")
    first = seed_proposal(conn, "a.txt", "documents", "Documents/old.txt", 0.9, None)
    second = seed_proposal(conn, "b.txt", "documents", "Documents/b.txt", 0.9, None)

    first_response = client.post(
        f"/review/proposals/{first}/edit",
        data={"category": "documents", "clean_name": "a.txt", "dest_relpath": "Documents/a.txt"},
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )
    second_response = client.post(
        f"/review/proposals/{second}/edit",
        data={"category": "documents", "clean_name": "a.txt", "dest_relpath": "Documents/a.txt"},
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )

    assert "collision suffix applied" in first_response.text
    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert proposal_dest(conn, first) == "Documents/a (2).txt"
    assert proposal_dest(conn, second) == "Documents/a (3).txt"


def test_edit_route_does_not_mutate_filesystem(tmp_path: Path) -> None:
    client, conn = client_for(tmp_path)
    settings = client.app.state.settings
    settings.library_dir.mkdir(parents=True, exist_ok=True)
    before = sorted(
        path.relative_to(settings.library_dir) for path in settings.library_dir.rglob("*")
    )
    proposal_id = seed_proposal(conn, "a.txt", "documents", "Documents/a.txt", 0.9, None)

    response = client.post(
        f"/review/proposals/{proposal_id}/edit",
        data={"category": "documents", "clean_name": "b.txt", "dest_relpath": "Documents/b.txt"},
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )
    after = sorted(
        path.relative_to(settings.library_dir) for path in settings.library_dir.rglob("*")
    )

    assert response.status_code == 200
    assert before == after


def seed_proposal(
    conn,
    relpath: str,
    category: str,
    dest_relpath: str | None,
    confidence: float,
    group_id: int | None,
) -> int:
    item = insert_item(conn, relpath)
    proposal_id = upsert_proposal(
        conn,
        item_id=item,
        category=category,
        clean_name=Path(relpath).name,
        dest_relpath=dest_relpath,
        confidence=confidence,
        group_id=group_id,
        evidence=[EvidenceEntry("heuristic", "category", category, confidence)],
    )
    conn.execute("UPDATE items SET state='proposed' WHERE id=?", (item,))
    return proposal_id


def insert_item(conn, relpath: str, size: int = 1) -> int:
    cursor = conn.execute(
        """
        INSERT INTO items(root, relpath, size, mtime_ns, fingerprint, first_seen_at, last_seen_at)
        VALUES ('inbox', ?, ?, 1, ?, 'now', 'now')
        """,
        (relpath, size, relpath),
    )
    return int(cursor.lastrowid)


def insert_group(conn, kind: str, label: str) -> int:
    cursor = conn.execute(
        "INSERT INTO groups(kind, label, created_at) VALUES (?, ?, 'now')",
        (kind, label),
    )
    return int(cursor.lastrowid)


def proposal_status(conn, proposal_id: int) -> str:
    return conn.execute("SELECT status FROM proposals WHERE id=?", (proposal_id,)).fetchone()[0]


def proposal_dest(conn, proposal_id: int) -> str:
    return conn.execute(
        "SELECT dest_relpath FROM proposals WHERE id=?", (proposal_id,)
    ).fetchone()[0]


def item_state(conn, proposal_id: int) -> str:
    return conn.execute(
        """
        SELECT i.state
        FROM proposals p
        JOIN items i ON i.id = p.item_id
        WHERE p.id=?
        """,
        (proposal_id,),
    ).fetchone()[0]


def test_review_rows_carry_confidence_bands_and_row_actions(tmp_path: Path) -> None:
    client, conn = client_for(tmp_path)
    high = insert_item(conn, "sure.mp3")
    low = insert_item(conn, "unsure.bin")
    upsert_proposal(
        conn, item_id=high, category="music", clean_name="sure.mp3",
        dest_relpath="Music/A/B/sure.mp3", confidence=0.95,
        evidence=[EvidenceEntry("tags", "metadata", "embedded audio tags", 0.95)],
    )
    upsert_proposal(
        conn, item_id=low, category="misc", clean_name="unsure.bin",
        dest_relpath=None, confidence=0.3,
        evidence=[EvidenceEntry("heuristic", "category", "unknown", 0.3)],
    )

    page = client.get("/review").text

    # Colour band is paired with a text label, never colour alone.
    assert "conf-high" in page
    assert "conf-low" in page
    # The badge word is gone: the bar's length and the score carry it without
    # colour, and three encodings of one number was the thing taking up room.
    assert "95%" in page
    assert "30%" in page
    # Tags are the file speaking for itself; a bare filename guess is not.
    assert 'conf-part is-local' in page
    assert 'conf-part is-guess' in page
    # Per-row decisions, CSP-safe (htmx attributes, no inline handlers).
    assert 'hx-vals=\'{"action": "approve"' in page
    assert "onclick=" not in page


def test_quick_approve_confident_only_touches_high_confidence(tmp_path: Path) -> None:
    client, conn = client_for(tmp_path)
    sure = seed_proposal(conn, "sure.flac", "music", "Music/sure.flac", 0.95, None)
    unsure = seed_proposal(conn, "unsure.bin", "misc", "Misc/unsure.bin", 0.4, None)

    page = client.get("/review").text
    response = client.post(
        "/review/action",
        data={
            "action": "approve",
            "all_matching": "true",
            "state": "proposed",
            "min_confidence": "0.85",
        },
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )

    # The threshold and the count are both in the label: a bulk approve whose
    # scope you cannot see is a decision taken on trust.
    assert "Approve 1 at 85%+" in page
    assert response.status_code == 200
    assert proposal_status(conn, sure) == "approved"
    assert proposal_status(conn, unsure) == "proposed"


def test_wallet_files_are_flagged_in_the_queue(tmp_path: Path) -> None:
    """A wallet must not vanish into a bulk approve unnoticed."""
    client, conn = client_for(tmp_path)
    seed_proposal(conn, "backup/wallet.dat", "misc", "Misc/wallet.dat", 0.9, None)

    page = client.get("/review").text

    assert "possible wallet" in page
    assert "flag-crypto" in page
    assert "not a backed-up file" in page


def test_hidden_files_offer_a_rename_that_unhides_them(tmp_path: Path) -> None:
    client, conn = client_for(tmp_path)
    seed_proposal(conn, ".env", "documents", "Documents/env", 0.9, None)

    page = client.get("/review").text

    assert "flag-hidden" in page
    assert "Rename to env" in page


def test_possible_adult_content_is_marked_as_a_guess(tmp_path: Path) -> None:
    client, conn = client_for(tmp_path)
    seed_proposal(conn, "downloads/clip.XXX.1080p.mp4", "movies", "Movies/clip.mp4", 0.9, None)

    page = client.get("/review").text

    assert "possibly adult" in page
    assert "guess from the filename" in page


def test_ordinary_files_carry_no_flag_markup(tmp_path: Path) -> None:
    client, conn = client_for(tmp_path)
    seed_proposal(conn, "Music/song.flac", "music", "Music/song.flac", 0.9, None)

    page = client.get("/review").text

    assert "flag-list" not in page


def test_pager_says_how_much_there_is_not_just_the_page_number(tmp_path: Path) -> None:
    """"page 3" alone tells you nothing about how much is left to get through."""
    client, conn = client_for(tmp_path)
    for index in range(120):
        seed_proposal(conn, f"inbox/file-{index}.mkv", "movies", f"Movies/f{index}.mkv", 0.9, None)

    first = client.get("/review/list").text
    middle = client.get("/review/list?page=2").text

    assert "Showing <strong>1–25</strong> of" in first
    assert "<strong>120</strong>" in first
    #  120 loose files are 120 decisions; twenty-five to a page.
    assert "Page <strong>1</strong> of <strong>5</strong>" in first
    assert "First" not in first  # already there
    assert "Showing <strong>26–50</strong> of <strong>120</strong>" in middle
    assert "« First" in middle
    assert "Last »" in middle


def test_pager_is_quiet_when_everything_fits_on_one_page(tmp_path: Path) -> None:
    client, conn = client_for(tmp_path)
    seed_proposal(conn, "inbox/only.mkv", "movies", "Movies/only.mkv", 0.9, None)

    body = client.get("/review/list").text

    assert "Showing <strong>1–1</strong> of <strong>1</strong>" in body
    assert "Page <strong>1</strong> of" not in body


def test_each_group_offers_a_select_all_box(tmp_path: Path) -> None:
    """Bulk actions used to need every row ticked by hand; the only shortcut
    was "every match", which is a far bigger commitment than "these ones"."""
    client, conn = client_for(tmp_path)
    for index in range(3):
        seed_proposal(conn, f"inbox/file-{index}.mkv", "movies", f"Movies/f{index}.mkv", 0.9, None)

    # The page-level "Select all shown" lives in the sticky toolbar, outside
    # every group, so review.js scopes it to the whole page.
    page = client.get("/review").text

    assert 'class="select-all"' in page
    assert "Select all" in page


def test_sorting_by_size_flattens_the_groups_and_orders_by_size(tmp_path: Path) -> None:
    """Sorting and grouping fight each other: rows herded back into albums do
    not arrive in the order you asked for. An explicit sort gives one list."""
    client, conn = client_for(tmp_path)
    for name, size in (("small.mkv", 10), ("huge.mkv", 9_000_000), ("mid.mkv", 5_000)):
        item = insert_item(conn, f"inbox/{name}", size=size)
        upsert_proposal(
            conn,
            item_id=item,
            category="movies",
            clean_name=name,
            dest_relpath=f"Movies/{name}",
            confidence=0.9,
            evidence=[EvidenceEntry("heuristic", "category", "extension", 0.9)],
        )

    body = client.get("/review/list?sort=largest").text
    order = [body.index(name) for name in ("huge.mkv", "mid.mkv", "small.mkv")]

    assert order == sorted(order)
    assert "8.6 MB" in body


def test_an_unknown_sort_falls_back_instead_of_failing(tmp_path: Path) -> None:
    """A stale bookmark is not a reason to show an error page."""
    client, conn = client_for(tmp_path)
    seed_proposal(conn, "inbox/a.mkv", "movies", "Movies/a.mkv", 0.9, None)

    response = client.get("/review/list?sort=DROP+TABLE+proposals")

    assert response.status_code == 200
    assert "a.mkv" in response.text


def test_dont_want_it_quarantines_rather_than_deleting(tmp_path: Path) -> None:
    """LibrAIry never deletes. "I don't want this" means quarantine, which is
    reversible, journaled, and goes through the same executor as everything."""
    client, conn = client_for(tmp_path)
    proposal_id = seed_proposal(conn, "inbox/junk.mkv", "movies", "Movies/junk.mkv", 0.9, None)

    response = client.post(
        "/review/action",
        data={"action": "discard", "proposal_id": str(proposal_id)},
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )
    row = conn.execute(
        "SELECT status, action, dest_root, dest_relpath FROM proposals WHERE id=?",
        (proposal_id,),
    ).fetchone()

    assert response.status_code == 200
    assert row["status"] == "approved"
    assert row["action"] == "quarantine"
    assert row["dest_root"] == "quarantine"
    assert row["dest_relpath"].endswith("inbox/junk.mkv")
    # The file itself has not been touched — that happens at commit, verified.
    assert item_state(conn, proposal_id) == "approved"


def test_discard_does_not_reach_into_decided_proposals(tmp_path: Path) -> None:
    client, conn = client_for(tmp_path)
    proposal_id = seed_proposal(conn, "inbox/a.mkv", "movies", "Movies/a.mkv", 0.9, None)
    conn.execute("UPDATE proposals SET status='committed'")

    client.post(
        "/review/action",
        data={"action": "discard", "proposal_id": str(proposal_id)},
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )

    assert proposal_status(conn, proposal_id) == "committed"


def test_the_category_field_is_a_menu_of_the_categories_that_exist(tmp_path: Path) -> None:
    """It was a free-text box that rejected anything but the eight valid
    categories without ever saying what they were."""
    client, conn = client_for(tmp_path)
    seed_proposal(conn, "inbox/a.mkv", "movies", "Movies/a.mkv", 0.9, None)

    body = client.get("/review/list").text

    assert '<select name="category">' in body
    for category in ("music", "movies", "shows", "photos", "documents", "books"):
        assert f'<option value="{category}"' in body


def test_a_duplicate_row_offers_the_comparison_and_says_what_each_tool_found(
    tmp_path: Path,
) -> None:
    """One sentence of evidence was all a duplicate used to get.

    The row has to advertise that there is a second copy, and the panel has to
    name every detector separately -- "rmlint agreed" and "rmlint was switched
    off" are different amounts of evidence and used to read the same.
    """
    from librairy.duplicates import SAME, compare, save_report
    from librairy.models import Item

    client, conn = client_for(tmp_path)
    proposal = seed_proposal(conn, "song.mp3", "misc", "quarantine/song.mp3", 1.0, None)
    inbox_id = conn.execute(
        "SELECT item_id FROM proposals WHERE id=?", (proposal,)
    ).fetchone()[0]
    library_id = conn.execute(
        """
        INSERT INTO items(root, relpath, size, mtime_ns, fingerprint, first_seen_at, last_seen_at)
        VALUES ('library', 'Music/song.mp3', 1, 1, 'song.mp3', 'now', 'now')
        RETURNING id
        """
    ).fetchone()[0]

    def as_item(item_id: int, root: str, relpath: str) -> Item:
        return Item(
            id=item_id,
            root=root,
            relpath=relpath,
            size=1,
            mtime_ns=1,
            fingerprint="song.mp3",
            state="discovered",
            first_seen_at="now",
            last_seen_at="now",
            missing_since=None,
        )

    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        _env_file=None,
    )
    save_report(
        conn,
        compare(
            conn,
            settings,
            as_item(inbox_id, "inbox", "song.mp3"),
            as_item(library_id, "library", "Music/song.mp3"),
            rmlint=SAME,
        ),
    )

    page = client.get("/review")
    panel = client.get(f"/review/duplicates/{inbox_id}")

    assert ">duplicate</span>" in page.text
    assert f'hx-get="/review/duplicates/{inbox_id}"' in page.text
    assert panel.status_code == 200
    assert "BLAKE2b fingerprint" in panel.text
    assert "rmlint" in panel.text
    assert "czkawka" in panel.text
    assert "Already in your library" in panel.text
    assert "Music/song.mp3" in panel.text
    assert "Quarantine the inbox copy" in panel.text


def test_a_row_with_no_duplicate_does_not_advertise_a_comparison(tmp_path: Path) -> None:
    client, conn = client_for(tmp_path)
    seed_proposal(conn, "unique.mp3", "music", "Music/unique.mp3", 0.9, None)

    page = client.get("/review")

    assert ">duplicate</span>" not in page.text
    assert "/review/duplicates/" not in page.text


def test_the_comparison_panel_survives_a_file_that_is_no_longer_there(tmp_path: Path) -> None:
    """A preview that cannot be rendered must cost the picture, not the page."""
    client, conn = client_for(tmp_path)
    conn.executescript(
        """
        INSERT INTO items(id, root, relpath, size, mtime_ns, first_seen_at, last_seen_at)
        VALUES (7, 'inbox', 'gone.mp3', 1, 1, 'now', 'now'),
               (8, 'library', 'Music/gone.mp3', 1, 1, 'now', 'now');
        """
    )
    conn.execute(
        """
        INSERT INTO duplicate_reports(item_id, other_id, payload, created_at)
        VALUES (7, 8, ?, 'now')
        """,
        (
            '{"item_id": 7, "other_id": 8, "verdict": "identical", "summary": "s",'
            ' "recommendation": "r", "findings": [], "facts": [], "checked_at": "now"}',
        ),
    )

    response = client.get("/review/duplicates/7")

    assert response.status_code == 200
    assert "could not be read" in response.text


def test_saying_no_to_a_duplicate_is_an_answer_not_a_500(tmp_path: Path) -> None:
    """Review shows the same four buttons on a duplicate row as on any other,
    and two of them had nowhere legal to go: quarantine-proposed -> pending
    ("Not this") and -> postponed ("Later") both raised LifecycleError.
    """
    from librairy.lifecycle import transition_item

    client, conn = client_for(tmp_path)
    proposal = seed_proposal(conn, "dupe.txt", "misc", "quarantine/dupe.txt", 1.0, None)
    item = conn.execute("SELECT item_id FROM proposals WHERE id=?", (proposal,)).fetchone()[0]
    conn.execute("UPDATE proposals SET action='quarantine', dest_root='quarantine' WHERE id=?",
                 (proposal,))
    conn.execute("UPDATE items SET state='discovered' WHERE id=?", (item,))
    transition_item(conn, item, "quarantine-proposed")

    response = client.post(
        "/review/action",
        data={"action": "reject", "proposal_id": str(proposal), "state": "proposed"},
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )

    assert response.status_code == 200
    assert proposal_status(conn, proposal) == "rejected"
    assert conn.execute("SELECT state FROM items WHERE id=?", (item,)).fetchone()[0] == "pending"


def test_reanalyse_puts_the_file_back_in_the_queue_instead_of_a_dead_end(
    tmp_path: Path,
) -> None:
    """"Not this" answered a wrong guess by never guessing again: the file went
    to 'pending', left the queue, and came back only from the command line.
    Re-analyse is the answer people actually want — look again, with the keys
    and tools that were not configured the first time.
    """
    client, conn = client_for(tmp_path)
    proposal = seed_proposal(conn, "unknown.bin", "misc", "Misc/unknown.bin", 0.3, None)
    item = conn.execute("SELECT item_id FROM proposals WHERE id=?", (proposal,)).fetchone()[0]

    response = client.post(
        "/review/action",
        data={"action": "reanalyze", "proposal_id": str(proposal), "state": "proposed"},
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )

    assert response.status_code == 200
    # 'discovered' is the only state the analyzer looks at, and the proposal
    # stays live so the old guess is still on screen while it is re-checked.
    assert conn.execute("SELECT state FROM items WHERE id=?", (item,)).fetchone()[0] == "discovered"
    assert proposal_status(conn, proposal) == "proposed"
    assert "looking again" in client.get("/review").text


def test_reanalyse_is_undoable_like_every_other_decision(tmp_path: Path) -> None:
    client, conn = client_for(tmp_path)
    proposal = seed_proposal(conn, "unknown.bin", "misc", "Misc/unknown.bin", 0.3, None)
    item = conn.execute("SELECT item_id FROM proposals WHERE id=?", (proposal,)).fetchone()[0]
    csrf = client.cookies["csrf_token"]

    client.post(
        "/review/action",
        data={"action": "reanalyze", "proposal_id": str(proposal), "state": "proposed"},
        headers={"x-csrf-token": csrf},
    )
    undone = client.post("/review/undo", headers={"x-csrf-token": csrf})

    assert "Sent back for another look 1 file" not in undone.text  # the offer is consumed
    assert conn.execute("SELECT state FROM items WHERE id=?", (item,)).fetchone()[0] == "proposed"


def test_mark_for_deletion_gathers_files_in_one_folder_and_still_deletes_nothing(
    tmp_path: Path,
) -> None:
    """Quarantine says "not in my library". It could not say "and I am done
    with this one", which left emptying it a file-by-file job somewhere else.
    """
    client, conn = client_for(tmp_path)
    proposal = seed_proposal(conn, "junk.bin", "misc", "Misc/junk.bin", 0.4, None)

    response = client.post(
        "/review/action",
        data={"action": "mark_delete", "proposal_id": str(proposal), "state": "proposed"},
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )

    assert response.status_code == 200
    row = conn.execute(
        "SELECT status, action, dest_root, dest_relpath FROM proposals WHERE id=?", (proposal,)
    ).fetchone()
    # A normal quarantine move, aimed at the one folder you empty yourself.
    assert row["action"] == "quarantine"
    assert row["dest_root"] == "quarantine"
    assert row["dest_relpath"].startswith("_to-delete/")
    assert row["dest_relpath"].endswith("/junk.bin")
    # Still a plan, still hash-verified on commit, still nothing deleted here.
    assert row["status"] == "approved"


def test_the_bulk_approve_says_its_threshold_and_hides_when_it_would_do_nothing(
    tmp_path: Path,
) -> None:
    """A bulk action whose scope you cannot see is a decision taken on trust."""
    client, conn = client_for(tmp_path)
    seed_proposal(conn, "unsure.bin", "misc", "Misc/unsure.bin", 0.4, None)

    doubtful_only = client.get("/review").text
    seed_proposal(conn, "sure.flac", "music", "Music/sure.flac", 0.95, None)
    seed_proposal(conn, "also.flac", "music", "Music/also.flac", 0.9, None)
    with_confident = client.get("/review").text

    assert "at 85%+" not in doubtful_only
    assert "Approve 2 at 85%+" in with_confident


def test_review_says_the_approved_pile_is_still_waiting_on_a_commit(tmp_path: Path) -> None:
    """"Approve" reads as "file it". It is a decision; the move is one more
    press, and nothing on the page said so."""
    client, conn = client_for(tmp_path)
    proposal = seed_proposal(conn, "sure.flac", "music", "Music/sure.flac", 0.95, None)

    before = client.get("/review").text
    client.post(
        "/review/action",
        data={"action": "approve", "proposal_id": str(proposal), "state": "proposed"},
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )
    after = client.get("/review").text

    assert "approved and waiting" not in before
    assert "1 file approved and waiting" in after
    assert 'href="/commit"' in after


def test_the_toast_says_what_just_happened_not_how_many_rows_changed(tmp_path: Path) -> None:
    """"1 proposal(s) updated" is the database's account of the press. After
    Re-analyse, which leaves the old guess on screen on purpose, it reads as
    nothing having happened."""
    client, conn = client_for(tmp_path)
    proposal = seed_proposal(conn, "unknown.bin", "misc", "Misc/unknown.bin", 0.3, None)
    csrf = client.cookies["csrf_token"]

    looking = client.post(
        "/review/action",
        data={"action": "reanalyze", "proposal_id": str(proposal), "state": "proposed"},
        headers={"x-csrf-token": csrf},
    )
    client.post("/review/undo", headers={"x-csrf-token": csrf})
    approved = client.post(
        "/review/action",
        data={"action": "approve", "proposal_id": str(proposal), "state": "proposed"},
        headers={"x-csrf-token": csrf},
    )

    assert "1 file back in the queue" in looking.text
    assert "The old guess stays until a better one lands" in looking.text
    assert "1 file approved. Nothing has moved yet" in approved.text


def test_a_group_of_one_gets_no_heading(tmp_path: Path) -> None:
    """Grouping earns its furniture by letting you decide an album at once. For
    a single file it is a heading, a select-all and a section margin over a
    decision you were going to make one row at a time regardless — and on real
    data every iMessage attachment arrived as its own "photo event"."""
    client, conn = client_for(tmp_path)
    alone = insert_group(conn, "photo_event", "A Folder Named Once")
    together = insert_group(conn, "album", "Kind of Blue")
    seed_proposal(conn, "photos/a.jpg", "photos", "Photos/A.jpg", 0.8, alone)
    seed_proposal(conn, "music/a.flac", "music", "Music/A.flac", 0.9, together)
    seed_proposal(conn, "music/b.flac", "music", "Music/B.flac", 0.9, together)

    page = client.get("/review").text

    assert "Kind of Blue" in page
    assert "A Folder Named Once" not in page
    # The file is still on the page, just without ceremony around it.
    assert "photos/a.jpg" in page


def test_a_grouped_row_shows_only_what_tells_it_apart(tmp_path: Path) -> None:
    """A DVD's nine rows each began with the same 52-character folder — the one
    already spelled out in the heading above them — and ended in the twelve
    characters that differ."""
    client, conn = client_for(tmp_path)
    disc = insert_group(conn, "disc", "A Concert DVD5")
    for name in ("VIDEO_TS.IFO", "VTS_01_1.VOB", "VTS_01_2.VOB"):
        seed_proposal(
            conn,
            f"A Concert DVD5/VIDEO_TS/{name}",
            "movies",
            f"Movies/General/A-Concert-(0)/VIDEO_TS/{name}",
            0.82,
            disc,
        )

    page = client.get("/review").text

    assert ">VTS_01_1.VOB<" in page
    # The whole path is still there to hover, and still in the destination.
    assert 'title="A Concert DVD5/VIDEO_TS/VTS_01_1.VOB"' in page


def test_the_editor_renders_above_the_preview_it_is_not_editing(tmp_path: Path) -> None:
    """Source order is the whole point of this control's placement.

    The edit form used to render last, below the preview: you clicked a
    destination at the top of the row and a form appeared underneath a
    photograph, which reads as editing the picture. What a control changes
    should be the thing directly above it.
    """
    client, conn = client_for(tmp_path)
    item_id = insert_item(conn, "holiday.jpg")
    upsert_proposal(
        conn, item_id=item_id, category="photos", clean_name="holiday.jpg",
        dest_relpath="Photos/2024/Trip/holiday.jpg", confidence=0.9,
        evidence=[EvidenceEntry("heuristic", "category", "image extension", 0.9)],
    )

    page = client.get("/review").text

    dest = page.index('class="proposal-dest"')
    editor = page.index('class="proposal-edit"')
    preview = page.index('class="proposal-preview"')
    assert dest < editor < preview, "destination, then its editor, then the preview"


def test_clicking_the_destination_focuses_the_destination_field(tmp_path: Path) -> None:
    client, conn = client_for(tmp_path)
    item_id = insert_item(conn, "holiday.jpg")
    upsert_proposal(
        conn, item_id=item_id, category="photos", clean_name="holiday.jpg",
        dest_relpath="Photos/2024/Trip/holiday.jpg", confidence=0.9,
        evidence=[EvidenceEntry("heuristic", "category", "image extension", 0.9)],
    )

    page = client.get("/review").text

    assert 'data-panel-focus="dest_relpath"' in page
    # And the field it names is really in the panel it opens.
    assert 'name="dest_relpath"' in page


def test_editing_still_validates_and_saves_from_its_new_position(tmp_path: Path) -> None:
    """Placement only. Every containment and sanitising rule is unchanged."""
    client, conn = client_for(tmp_path)
    item_id = insert_item(conn, "holiday.jpg")
    proposal_id = upsert_proposal(
        conn, item_id=item_id, category="photos", clean_name="holiday.jpg",
        dest_relpath="Photos/2024/Trip/holiday.jpg", confidence=0.9,
        evidence=[EvidenceEntry("heuristic", "category", "image extension", 0.9)],
    )

    saved = client.post(
        f"/review/proposals/{proposal_id}/edit",
        data={"category": "photos", "clean_name": "beach.jpg",
              "dest_relpath": "Photos/2024/Trip/beach.jpg"},
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )
    escape = client.post(
        f"/review/proposals/{proposal_id}/edit",
        data={"category": "photos", "clean_name": "beach.jpg",
              "dest_relpath": "../../etc/passwd"},
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )

    assert saved.status_code == 200
    row = conn.execute(
        "SELECT clean_name, dest_relpath FROM proposals WHERE id=?", (proposal_id,)
    ).fetchone()
    assert row["clean_name"] == "beach.jpg"
    assert row["dest_relpath"] == "Photos/2024/Trip/beach.jpg"
    assert escape.status_code >= 400, "a path out of the library is refused"
    assert (
        conn.execute(
            "SELECT dest_relpath FROM proposals WHERE id=?", (proposal_id,)
        ).fetchone()["dest_relpath"]
        == "Photos/2024/Trip/beach.jpg"
    ), "and leaves the stored destination alone"


# --- what a group action is about ---------------------------------------------


def mixed_album(conn) -> int:
    """Twelve tracks in one group: seven certain, five not."""
    album = insert_group(conn, "album", "Mixed Album")
    for n in range(12):
        seed_proposal(
            conn,
            f"mixed/{n:02d}.flac",
            "music",
            f"Music/Mixed/{n:02d}.flac",
            0.95 if n < 7 else 0.40,
            album,
        )
    return album


def test_a_narrowed_group_says_both_counts_and_acts_on_the_smaller(tmp_path: Path) -> None:
    """A group has two counts and the page must never blur them.

    Twelve files in the group, seven above the confidence bound. The heading
    says how big the group is *and* how much of it this view is about; the
    button names the number it will actually touch, and says "matching" rather
    than "all" because it is not all of them.
    """
    client, conn = client_for(tmp_path)
    mixed_album(conn)

    whole = client.get("/review/list").text
    narrowed = client.get("/review/list?min_confidence=0.9").text

    #  Unnarrowed, one number is the truth and the page says it once.
    assert "12 files" in whole
    assert "Approve all 12" in whole
    assert "match this view" not in whole

    #  Narrowed, both — and the action is about the seven.
    assert "12 files" in narrowed
    assert "7 match this view" in narrowed
    assert "Approve 7 matching" in narrowed
    assert "Approve all 12" not in narrowed


def test_a_group_action_posts_every_filter_it_was_drawn_under(tmp_path: Path) -> None:
    """The button's count and the server's resolution must be the same view.

    `hx-vals` carried state, category and sort but not the confidence bounds,
    so a button reading "7" posted a view in which twelve matched — the server
    resolved the unit honestly, under the wrong filters, and approved five
    files nothing on the page mentioned.
    """
    client, conn = client_for(tmp_path)
    mixed_album(conn)

    narrowed = client.get("/review/list?min_confidence=0.9").text
    assert "min_confidence" in narrowed

    approve = next(
        line for line in narrowed.splitlines() if "hx-vals" in line and "approve" in line
    )
    assert "0.9" in approve, approve


def test_acting_on_a_narrowed_group_leaves_the_rest_alone(tmp_path: Path) -> None:
    """Seven approved, five untouched, and the toast says seven."""
    client, conn = client_for(tmp_path)
    album = mixed_album(conn)

    response = client.post(
        "/review/action",
        data={"action": "approve", "unit": f"g{album}", "min_confidence": "0.9"},
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )

    assert response.status_code == 200
    approved = conn.execute(
        "SELECT COUNT(*) FROM proposals WHERE group_id=? AND status='approved'", (album,)
    ).fetchone()[0]
    still_proposed = conn.execute(
        "SELECT COUNT(*) FROM proposals WHERE group_id=? AND status='proposed'", (album,)
    ).fetchone()[0]
    assert (approved, still_proposed) == (7, 5)
    assert "7 files approved" in response.text


def test_a_group_action_that_matches_nothing_says_so(tmp_path: Path) -> None:
    """The filters moved under the button between drawing and pressing.

    Nothing is touched, and the reply must not read like a success: "0 files
    approved. Nothing has moved yet — commit to file them" is a sentence about
    work that did not happen.
    """
    client, conn = client_for(tmp_path)
    album = mixed_album(conn)

    response = client.post(
        "/review/action",
        data={"action": "approve", "unit": f"g{album}", "min_confidence": "0.99"},
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )

    assert response.status_code == 200
    assert "Nothing matched, so nothing changed." in response.text
    assert "approved" not in response.text.split("badge-ok")[1][:200]
    untouched = conn.execute(
        "SELECT COUNT(*) FROM proposals WHERE group_id=? AND status='proposed'", (album,)
    ).fetchone()[0]
    assert untouched == 12


def test_paging_and_expanding_carry_the_whole_filter(tmp_path: Path) -> None:
    """A link that carries some of the filters means a different page.

    The pager and the "Show more" button both built their query string by hand
    and both left out the confidence bounds, so the second page of a narrowed
    view was a page of something else — and members of a group could vanish
    between one page of an expansion and the next.
    """
    client, conn = client_for(tmp_path)
    mixed_album(conn)
    for n in range(60):
        seed_proposal(conn, f"loose/{n:02d}.txt", "documents", f"Docs/{n:02d}.txt", 0.95, None)

    narrowed = client.get("/review/list?min_confidence=0.9&has_destination=yes").text
    checked = 0
    for link in [line for line in narrowed.splitlines() if "hx-get=" in line]:
        if "/review/list?" in link or "/review/group/" in link:
            assert "min_confidence=0.9" in link, link
            assert "has_destination=yes" in link, link
            checked += 1
    #  A pager and an expansion, or the assertions above were about nothing.
    assert checked >= 2, narrowed


# --- one foundation, four faces -----------------------------------------------


def seed_member(conn, relpath, category, dest, confidence, group, evidence) -> int:  # noqa: ANN001
    """A proposal with identity evidence of its own, for the media faces."""
    item = insert_item(conn, relpath)
    proposal = upsert_proposal(
        conn,
        item_id=item,
        category=category,
        clean_name=Path(relpath).name,
        dest_relpath=dest,
        confidence=confidence,
        group_id=group,
        evidence=evidence,
    )
    conn.execute("UPDATE items SET state='proposed' WHERE id=?", (item,))
    return proposal


def photo_event(conn, count: int = 8) -> int:
    group = insert_group(conn, "photo_event", "Backyard")
    for n in range(count):
        seed_proposal(
            conn,
            f"party/DSC_{4100 + n:04d}.JPG",
            "photos",
            f"Photos/2024/Backyard/dsc_{4100 + n:04d}.jpg",
            0.93,
            group,
        )
    return group


def album(conn) -> int:
    group = insert_group(conn, "album", "Pink Floyd - The Dark Side of the Moon")
    tracks = [(1, "Speak to Me"), (2, "Breathe"), (3, "On the Run"), (4, "Time")]
    for number, title in tracks:
        seed_member(
            conn,
            f"dsotm/{number:02d} - {title}.flac",
            "music",
            f"Music/Rock/Pink Floyd/Dark Side/{number:02d} - {title}.flac",
            0.94,
            group,
            [
                EvidenceEntry("tags", "track", str(number), 0.94),
                EvidenceEntry("tags", "title", title, 0.94),
                EvidenceEntry("tags", "album", "The Dark Side of the Moon", 0.94),
            ],
        )
    return group


def test_a_photo_group_is_drawn_as_pictures(tmp_path: Path) -> None:
    """Deciding about a camera card is looking, not reading.

    The question "does this one belong with the others" is answered by the
    picture in about a second and by a filename never.
    """
    client, conn = client_for(tmp_path)
    photo_event(conn)

    page = client.get("/review/list").text

    assert 'class="member-list is-photos"' in page
    assert "/preview/items/" in page
    #  And it is still one decision with the same controls as any other.
    assert "Approve all 8" in page
    assert 'name="proposal_id"' in page


def test_an_album_is_a_list_of_tracks_in_track_order(tmp_path: Path) -> None:
    """Twelve squares of one cover would be twelve copies of one fact.

    And an album listed 4, 3, 2, 1 is wrong to anybody who has ever seen an
    album — members are ordered by where they are going, which spells track
    order for music, episode order for television and shutter order for a
    camera card.
    """
    client, conn = client_for(tmp_path)
    album(conn)

    page = client.get("/review/list").text

    assert 'class="member-list is-music"' in page
    assert "Speak to Me" in page
    assert page.index("Speak to Me") < page.index("Breathe") < page.index("On the Run")
    #  Where the identity came from, named rather than scored.
    assert "from tags" in page


def test_a_season_shows_the_episode_not_the_release_name(tmp_path: Path) -> None:
    client, conn = client_for(tmp_path)
    group = insert_group(conn, "season", "Breaking Bad Season 01")
    for number, title in ((1, "Pilot"), (2, "Cat's in the Bag...")):
        seed_member(
            conn,
            f"bb/S01E{number:02d}.BluRay.x264-GROUP.mkv",
            "shows",
            f"Shows/Breaking Bad/Season 01/S01E{number:02d} - {title}.mkv",
            0.91,
            group,
            [
                EvidenceEntry("tvmaze", "title", title, 0.91),
                EvidenceEntry("tvmaze", "season", "1", 0.91),
                EvidenceEntry("tvmaze", "episode", str(number), 0.91),
            ],
        )

    page = client.get("/review/list").text

    assert 'class="member-list is-video"' in page
    assert "S01E01" in page
    assert "Pilot" in page
    assert "from tvmaze" in page


def test_a_mixed_group_stays_a_list(tmp_path: Path) -> None:
    """No single medium, so no medium's face. A grid of thumbnails over three
    photographs and a spreadsheet is a worse answer than a list."""
    client, conn = client_for(tmp_path)
    group = insert_group(conn, "project", "Kitchen rebuild")
    seed_proposal(conn, "kit/plan.pdf", "documents", "Projects/Kitchen/plan.pdf", 0.9, group)
    seed_proposal(conn, "kit/before.jpg", "photos", "Projects/Kitchen/before.jpg", 0.9, group)

    page = client.get("/review/list").text

    assert 'class="member-list is-rows"' in page
    assert "is-photos" not in page


def test_expanding_a_group_keeps_its_face(tmp_path: Path) -> None:
    """A group must not change shape halfway down as somebody expands it."""
    client, conn = client_for(tmp_path)
    group = photo_event(conn, count=8)

    more = client.get(f"/review/group/g{group}?page=2&state=proposed&sort=confidence").text

    assert "photo-cell" in more
    assert "/preview/items/" in more
    #  And the control that fetched it is renewed rather than duplicated.
    assert more.count("Show more") <= 1


def test_a_document_row_shows_its_first_page(tmp_path: Path) -> None:
    """Nothing groups documents, so the document's face is its row.

    `PDF → Documents` was the whole row for a 643-page manual, and a novel and
    a bank statement are told apart by their cover at a glance.
    """
    client, conn = client_for(tmp_path)
    seed_member(
        conn,
        "papers/manual.pdf",
        "documents",
        "Documents/Manuals/2024 CR-V Owner's Manual.pdf",
        0.9,
        None,
        [
            EvidenceEntry("document", "type", "Manual", 0.9),
            EvidenceEntry("document", "pdf title metadata", "2024 CR-V Owner's Manual", 0.9),
        ],
    )

    page = client.get("/review/list").text

    assert 'class="doc-page"' in page
    #  The apostrophe is escaped in the markup, so match either side of it.
    assert "2024 CR-V Owner" in page
    assert "Manual" in page


def test_a_cell_opens_the_ordinary_row(tmp_path: Path) -> None:
    """One row implementation, not four.

    A face answers "is this the right one"; the destination, the evidence, the
    edit panel and the four actions are the row, fetched when wanted.
    """
    client, conn = client_for(tmp_path)
    photo_event(conn, count=6)
    proposal = conn.execute("SELECT id FROM proposals LIMIT 1").fetchone()[0]

    row = client.get(f"/review/proposals/{proposal}/row")

    assert row.status_code == 200
    assert f'id="proposal-{proposal}"' in row.text
    assert "Approve" in row.text
    assert client.get("/review/proposals/999999/row").status_code == 404


# --- the odd ones out ---------------------------------------------------------


def test_a_member_going_elsewhere_becomes_its_own_decision(tmp_path: Path) -> None:
    """One file that is not going where its group is going.

    The whole "three wrong among a hundred and fifty" case. It is derived, not
    stored — `groups` still says what belongs together — and it is a unit in
    every sense: its own count, its own heading, its own action, and the group
    it came out of does not include it in any of the three.
    """
    from librairy.web.review import ReviewFilters, unit_proposal_ids

    client, conn = client_for(tmp_path)
    group = insert_group(conn, "photo_event", "Backyard")
    conn.execute("UPDATE groups SET dest_base='Photos/2024/Backyard' WHERE id=?", (group,))
    for n in range(6):
        seed_proposal(
            conn,
            f"party/DSC_{4100 + n:04d}.JPG",
            "photos",
            f"Photos/2024/Backyard/dsc_{4100 + n:04d}.jpg",
            0.93,
            group,
        )
    odd = seed_proposal(
        conn, "party/receipt.jpg", "photos", "Documents/Receipts/receipt.jpg", 0.93, group
    )

    page = client.get("/review/list").text
    filters = ReviewFilters()

    assert "the odd ones out" in page
    assert "Not going where the rest of Backyard is going" in page
    #  Six in the group, one beside it, and neither count includes the other.
    assert "Approve all 6" in page
    assert "Approve this one" in page
    assert len(unit_proposal_ids(conn, filters, f"g{group}")) == 6
    assert odd not in unit_proposal_ids(conn, filters, f"g{group}")
    assert unit_proposal_ids(conn, filters, f"x{group}") == [odd]


def test_an_exception_of_one_is_never_folded_away(tmp_path: Path) -> None:
    """A group of one is not a group; an exception of one is the entire point."""
    client, conn = client_for(tmp_path)
    group = insert_group(conn, "album", "Some Album")
    conn.execute("UPDATE groups SET dest_base='Music/Rock/Some Album' WHERE id=?", (group,))
    for n in range(4):
        seed_proposal(
            conn, f"al/{n}.flac", "music", f"Music/Rock/Some Album/{n}.flac", 0.94, group
        )
    seed_proposal(conn, "al/stray.flac", "music", "Music/Pop/stray.flac", 0.94, group)

    page = client.get("/review/list").text

    #  The stray keeps its heading and its reason rather than dropping into the
    #  loose pile, where nothing would say it had been anywhere else.
    assert "the odd ones out" in page
    assert page.count("Not going where the rest of") == 1


def test_turning_the_split_off_returns_exactly_the_old_behaviour(tmp_path: Path) -> None:
    """The roadmap's own acceptance test. Both thresholds are switchable."""
    from librairy.web.review import OUTLIER_SETTING, ReviewFilters, unit_proposal_ids

    client, conn = client_for(tmp_path)
    group = insert_group(conn, "photo_event", "Backyard")
    conn.execute("UPDATE groups SET dest_base='Photos/2024/Backyard' WHERE id=?", (group,))
    for n in range(4):
        seed_proposal(
            conn,
            f"party/DSC_{4100 + n:04d}.JPG",
            "photos",
            f"Photos/2024/Backyard/dsc_{4100 + n:04d}.jpg",
            0.93,
            group,
        )
    seed_proposal(conn, "party/odd.jpg", "photos", "Documents/odd.jpg", 0.93, group)

    conn.execute(
        "INSERT INTO settings(key, value) VALUES (?, 'false')", (OUTLIER_SETTING,)
    )
    conn.commit()

    page = client.get("/review/list").text

    assert "the odd ones out" not in page
    assert "Approve all 5" in page
    assert len(unit_proposal_ids(conn, ReviewFilters(), f"g{group}")) == 5


def test_the_number_to_look_at_is_a_control_that_shows_them(tmp_path: Path) -> None:
    """A number you cannot reach is not much better than no number.

    "3 to look at" over a hundred and fifty files left reading a hundred and
    fifty rows as the way to find three.
    """
    client, conn = client_for(tmp_path)
    group = insert_group(conn, "photo_event", "Backyard")
    conn.execute("UPDATE groups SET dest_base='Photos/2024/Backyard' WHERE id=?", (group,))
    for n in range(12):
        seed_proposal(
            conn,
            f"party/DSC_{4100 + n:04d}.JPG",
            "photos",
            f"Photos/2024/Backyard/dsc_{4100 + n:04d}.jpg",
            #  Two nobody is sure about, buried past the five that are previewed.
            0.40 if n in (9, 10) else 0.95,
            group,
        )

    page = client.get("/review/list").text
    assert "2 to look at" in page
    assert "only=attention" in page

    looked = client.get(
        f"/review/group/g{group}?only=attention&state=proposed&sort=confidence"
    ).text

    #  Exactly the two, and a way back to the whole group.
    assert looked.count('name="proposal_id"') == 2
    assert "DSC_4109.JPG" in looked
    assert "DSC_4110.JPG" in looked
    assert "DSC_4100.JPG" not in looked
    assert "Show all 12" in looked


# --- how much attention a decision is worth -----------------------------------


def settled_and_guessed(conn) -> tuple[int, int]:  # noqa: ANN001
    """One file identified by its own ISBN, one guessed at with a high score."""
    settled = seed_member(
        conn,
        "books/dune.epub",
        "books",
        "Books/Frank Herbert/Dune/Dune.epub",
        0.70,
        None,
        [EvidenceEntry("document", "isbn", "9780441013593", 0.95)],
    )
    guessed = seed_member(
        conn,
        "docs/notes.pdf",
        "documents",
        "Documents/2024/notes.pdf",
        0.92,
        None,
        [EvidenceEntry("heuristic", "category", "a year in the filename", 0.92)],
    )
    return settled, guessed


def test_the_settled_batch_names_its_count_and_says_what_settled_each(tmp_path: Path) -> None:
    """A high score and an identity are not the same claim.

    The confident button takes the green bars; this one takes the files that
    were identified from themselves, which is a different set and a different
    sentence.
    """
    client, conn = client_for(tmp_path)
    settled_and_guessed(conn)

    page = client.get("/review").text

    assert "Approve 1 settled" in page
    #  And the row can answer "why am I here" without being opened.
    assert "9780441013593" in client.get("/review/list").text


def test_approving_the_settled_leaves_the_guesses_alone(tmp_path: Path) -> None:
    client, conn = client_for(tmp_path)
    settled, guessed = settled_and_guessed(conn)

    response = client.post(
        "/review/action",
        data={"action": "approve", "settled": "true"},
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )

    assert response.status_code == 200
    assert proposal_status(conn, settled) == "approved"
    assert proposal_status(conn, guessed) == "proposed"


def test_something_else_in_question_keeps_a_settled_file_out_of_the_batch(
    tmp_path: Path,
) -> None:
    """A second copy of a file you already have is not a filing decision."""
    from librairy.inbox_duplicates import EVIDENCE_PREFIX

    client, conn = client_for(tmp_path)
    settled, _ = settled_and_guessed(conn)
    also = seed_member(
        conn,
        "books/dune-again.epub",
        "books",
        "Books/Frank Herbert/Dune/Dune.epub",
        0.70,
        None,
        [
            EvidenceEntry("document", "isbn", "9780441013593", 0.95),
            EvidenceEntry("fingerprint", "blake2b", f"{EVIDENCE_PREFIX} Books/Dune.epub", 0.99),
        ],
    )

    client.post(
        "/review/action",
        data={"action": "approve", "settled": "true"},
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )

    assert proposal_status(conn, settled) == "approved"
    assert proposal_status(conn, also) == "proposed"


def test_the_tier_is_a_filter_of_its_own(tmp_path: Path) -> None:
    client, conn = client_for(tmp_path)
    settled_and_guessed(conn)

    only = client.get("/review/list?tier=settled").text

    assert "dune.epub" in only
    assert "notes.pdf" not in only


def test_nothing_is_approved_on_its_own_unless_that_is_switched_on(tmp_path: Path) -> None:
    """M1-05's third acceptance criterion, and the safer default.

    Approving on somebody's behalf, silently, on the first cycle after an
    upgrade is not a thing to switch on for them.
    """
    from librairy.settled_queue import SETTING, approve_settled

    client, conn = client_for(tmp_path)
    settled, _ = settled_and_guessed(conn)
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        _env_file=None,
    )

    assert approve_settled(conn, settings) == 0
    assert proposal_status(conn, settled) == "proposed"

    conn.execute("INSERT INTO settings(key, value) VALUES (?, 'true')", (SETTING,))
    conn.commit()

    assert approve_settled(conn, settings) == 1
    assert proposal_status(conn, settled) == "approved"
    #  Still in the inbox, still waiting for Commit, and takeable back.
    assert conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 0
    assert client.get("/review").status_code == 200


def test_an_automatic_approval_teaches_the_learner_nothing(tmp_path: Path) -> None:
    """A program that learns from its own decisions is citing itself.

    `docs/architecture/decision-memory.md` lists "what the classifier produced
    on its own" among the things that are never lessons, and an automatic
    approval is exactly that.
    """
    from librairy.settled_queue import SETTING, approve_settled

    _, conn = client_for(tmp_path)
    settled_and_guessed(conn)
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        _env_file=None,
    )
    conn.execute("INSERT INTO settings(key, value) VALUES (?, 'true')", (SETTING,))
    conn.commit()

    before = conn.execute("SELECT COUNT(*) FROM decision_events").fetchone()[0]
    assert approve_settled(conn, settings) == 1
    after = conn.execute("SELECT COUNT(*) FROM decision_events").fetchone()[0]

    assert after == before
