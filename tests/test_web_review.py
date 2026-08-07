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
    assert "2 shown" in page.text
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
    assert ">Why</button>" in response.text
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
    assert "of <strong>5000</strong>" in response.text
    # Row checkboxes only — the header select-all is a checkbox too.
    assert response.text.count('name="proposal_id" type="checkbox"') == 50
    assert "Page <strong>1</strong> of <strong>100</strong>" in response.text
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

    assert "Showing <strong>1–50</strong> of <strong>120</strong>" in first
    assert "Page <strong>1</strong> of <strong>3</strong>" in first
    assert "First" not in first  # already there
    assert "Showing <strong>51–100</strong> of <strong>120</strong>" in middle
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
