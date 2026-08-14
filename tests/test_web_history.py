from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from librairy.config import Settings
from librairy.db import connect
from librairy.executor import execute_plan
from librairy.planner import OperationSpec, approve_plan, create_plan
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


def test_history_lists_commit_plan_detail_and_single_op_undo(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    plan_id = seed_committed_plan(settings, conn, ["a.txt"])
    history_id = conn.execute("SELECT id FROM history WHERE action='move'").fetchone()[0]
    plan_hash = conn.execute("SELECT plan_hash FROM plans WHERE id=?", (plan_id,)).fetchone()[0]

    history_page = client.get("/history")
    detail = client.get(f"/history/plans/{plan_id}")
    undo = client.post(f"/history/undo/{history_id}", headers=csrf(client))

    assert plan_id in history_page.text
    assert plan_hash in detail.text
    assert "Documents/a.txt" in detail.text
    assert undo.status_code == 200
    assert ">undo</span>" in undo.text
    assert (settings.inbox_dir / "a.txt").read_text(encoding="utf-8") == "a.txt"
    assert not (settings.library_dir / "Documents/a.txt").exists()


def test_whole_plan_undo_restores_pre_commit_tree_and_journals(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    plan_id = seed_committed_plan(settings, conn, ["a.txt", "b.txt"])

    response = client.post(f"/history/plans/{plan_id}/undo", headers=csrf(client))

    actions = [
        row["action"]
        for row in conn.execute(
            "SELECT action FROM history WHERE plan_id=? ORDER BY id", (plan_id,)
        )
    ]
    assert response.status_code == 200
    assert response.text.count(">undo</span>") == 2
    assert (settings.inbox_dir / "a.txt").exists()
    assert (settings.inbox_dir / "b.txt").exists()
    assert not (settings.library_dir / "Documents/a.txt").exists()
    assert actions == ["move", "move", "undo_move", "undo_move"]


def test_fingerprint_mismatch_refusal_renders_expected_and_actual(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    seed_committed_plan(settings, conn, ["a.txt"])
    history_id = conn.execute("SELECT id FROM history WHERE action='move'").fetchone()[0]
    expected = conn.execute(
        "SELECT fingerprint FROM history WHERE id=?", (history_id,)
    ).fetchone()[0]
    (settings.library_dir / "Documents/a.txt").write_text("changed", encoding="utf-8")

    response = client.post(f"/history/undo/{history_id}", headers=csrf(client))

    assert response.status_code == 200
    assert "undo_refused_changed" in response.text
    assert f"expected={expected}" in response.text
    assert "actual=" in response.text
    assert (settings.library_dir / "Documents/a.txt").read_text(encoding="utf-8") == "changed"


def test_journal_rows_are_read_only_without_edit_affordances(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    seed_committed_plan(settings, conn, ["a.txt"])

    response = client.get("/history")

    assert response.status_code == 200
    assert "edit" not in response.text.lower()
    assert "contenteditable" not in response.text.lower()
    assert "Undo" in response.text


def seed_committed_plan(settings: Settings, conn, names: list[str]) -> str:
    for name in names:
        (settings.inbox_dir / name).write_text(name, encoding="utf-8")
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    plan_id = create_plan(
        conn,
        [OperationSpec("move", name, "library", f"Documents/{name}") for name in names],
        settings,
    )
    approve_plan(conn, plan_id, settings)
    execute_plan(conn, plan_id, settings)
    return plan_id


def csrf(client: TestClient) -> dict[str, str]:
    return {"x-csrf-token": client.cookies["csrf_token"]}


def test_history_timeline_groups_by_plan_and_deep_links_to_browse(tmp_path: Path) -> None:
    client, conn, settings = client_for(tmp_path)
    plan_id = seed_committed_plan(settings, conn, ["a.txt", "b.txt"])

    page = client.get("/history").text

    # Grouped git-log style, with an undo-plan control and the plan link.
    assert "timeline-plan" in page
    assert f"/history/plans/{plan_id}" in page
    # Short like a git hash: a full UUID is 414px of button on a phone.
    assert f"Plan {plan_id[:8]}<" in page
    assert "Undo plan" in page
    # The plan says what it did, rather than making you count the rows.
    assert "Filed 2 files" in page
    # Committed destinations deep-link into Browse at the containing folder.
    assert '/browse/documents' in page


def test_history_search_finds_a_move_by_part_of_its_path(tmp_path: Path) -> None:
    """The journal only grows, and "scroll until you find it" stops working
    somewhere in the low hundreds."""
    client, conn, _ = client_for(tmp_path)
    for name in ("Ozymandias.mkv", "Bohemian Rhapsody.flac", "invoice.pdf"):
        conn.execute(
            """
            INSERT INTO history(ts, plan_id, op_id, action, src_root, src_relpath,
                                dest_root, dest_relpath, fingerprint, outcome)
            VALUES ('now', 'p1', 1, 'move', 'inbox', ?, 'library', ?, 'fp', 'ok')
            """,
            (name, f"Shows/{name}"),
        )

    hit = client.get("/history?q=ozy")
    miss = client.get("/history?q=nothing-like-this")

    assert "Ozymandias.mkv" in hit.text
    assert "Bohemian" not in hit.text
    assert "Showing 1 of 1 matching" in hit.text
    assert "Nothing here matches" in miss.text


def test_history_search_is_case_insensitive_and_survives_wildcards(tmp_path: Path) -> None:
    """Underscores are in half the filenames here, so a stray LIKE wildcard
    must not be treated as a syntax error or silently match everything."""
    client, conn, _ = client_for(tmp_path)
    conn.execute(
        """
        INSERT INTO history(ts, plan_id, op_id, action, src_root, src_relpath,
                            dest_root, dest_relpath, fingerprint, outcome)
        VALUES ('now', 'p1', 1, 'move', 'inbox', 'my_file.mkv', 'library', 'Movies/my_file.mkv',
                'fp', 'ok')
        """
    )

    assert "my_file.mkv" in client.get("/history?q=MY_FILE").text
    assert client.get("/history?q=%").status_code == 200


# --- the information model --------------------------------------------------


def journal_row(conn, **kw) -> None:
    row = {
        "ts": "2026-08-04T10:00:00+00:00",
        "plan_id": "p1",
        "action": "move",
        "src_root": "inbox",
        "src_relpath": "a.txt",
        "dest_root": "library",
        "dest_relpath": "Documents/a.txt",
        "outcome": "ok",
        **kw,
    }
    conn.execute(
        """
        INSERT INTO history(ts, plan_id, action, src_root, src_relpath,
                            dest_root, dest_relpath, outcome)
        VALUES (:ts, :plan_id, :action, :src_root, :src_relpath,
                :dest_root, :dest_relpath, :outcome)
        """,
        row,
    )


def test_the_filters_are_only_things_the_journal_can_tell_apart(tmp_path: Path) -> None:
    """The journal has three action values. Approvals and re-analysis are not
    in it at all, so offering them as filters would offer categories the data
    can never fill."""
    client, conn, _ = client_for(tmp_path)
    journal_row(conn, src_relpath="filed.txt")
    journal_row(conn, src_relpath="dupe.txt", dest_root="quarantine", dest_relpath="dupe.txt")
    journal_row(conn, src_relpath="back.txt", action="undo_move", dest_root="inbox")
    journal_row(conn, src_relpath="theme", action="settings_change", outcome="a -> b")
    journal_row(conn, src_relpath="broke.txt", outcome="skipped_missing")

    page = client.get("/history").text

    for label in ("Filed", "Quarantined", "Undone", "Settings", "Failed"):
        assert label in page
    # Categories the journal cannot distinguish are not offered.
    assert "Approvals" not in page
    assert ">Analysis<" not in page


def test_each_filter_shows_its_real_count_and_narrows_the_list(tmp_path: Path) -> None:
    client, conn, _ = client_for(tmp_path)
    for index in range(3):
        journal_row(conn, src_relpath=f"filed{index}.txt")
    journal_row(conn, src_relpath="dupe.txt", dest_root="quarantine", dest_relpath="dupe.txt")

    everything = client.get("/history").text
    quarantined = client.get("/history?kind=quarantined").text

    # The count beside the chip is the count the filter actually yields.
    assert "Quarantined <span class=\"chip-count\">1</span>" in everything
    assert "dupe.txt" in quarantined
    assert "filed0.txt" not in quarantined
    assert "Showing 1 of 1 matching" in quarantined


def test_a_filter_and_the_find_box_compose(tmp_path: Path) -> None:
    """Narrowing to Filed and then typing a name searches inside the filter
    rather than replacing it."""
    client, conn, _ = client_for(tmp_path)
    journal_row(conn, src_relpath="Ozymandias.mkv", dest_relpath="Shows/Ozymandias.mkv")
    journal_row(
        conn, src_relpath="Ozymandias-copy.mkv", dest_root="quarantine",
        dest_relpath="Ozymandias-copy.mkv",
    )

    both = client.get("/history?q=ozymandias").text
    filed_only = client.get("/history?kind=filed&q=ozymandias").text

    assert "Ozymandias-copy.mkv" in both
    assert "Ozymandias-copy.mkv" not in filed_only
    assert "Shows/Ozymandias.mkv" in filed_only
    # And the search box keeps the filter, so submitting it again does not
    # silently widen back to everything.
    assert '<input type="hidden" name="kind" value="filed">' in filed_only


def test_entries_are_grouped_under_a_day_heading(tmp_path: Path) -> None:
    client, conn, _ = client_for(tmp_path)
    today = datetime.now(UTC).date().isoformat()
    journal_row(conn, ts=f"{today}T09:30:00+00:00", src_relpath="new.txt", plan_id="p-today")
    journal_row(conn, ts="2026-01-02T09:30:00+00:00", src_relpath="old.txt", plan_id="p-old")

    page = client.get("/history").text

    assert "Today" in page
    assert "2 January 2026" in page
    assert "09:30" in page


def test_grouping_is_presentation_only_and_loses_no_journal_row(tmp_path: Path) -> None:
    """Aggregation happens at render time. Every individual entry is still
    listed, and still individually undoable."""
    client, conn, _ = client_for(tmp_path)
    for index in range(4):
        journal_row(conn, src_relpath=f"f{index}.txt", dest_relpath=f"Documents/f{index}.txt")

    page = client.get("/history").text
    rows = conn.execute("SELECT COUNT(*) FROM history").fetchone()[0]

    assert rows == 4, "no journal row was merged away"
    for index in range(4):
        assert f"Documents/f{index}.txt" in page
    assert page.count("/history/undo/") == 4
    assert "Filed 4 files" in page


def test_history_pages_past_the_first_screenful(tmp_path: Path) -> None:
    client, conn, _ = client_for(tmp_path)
    for index in range(55):
        journal_row(conn, src_relpath=f"f{index}.txt", dest_relpath=f"Documents/f{index}.txt")

    first = client.get("/history").text
    second = client.get("/history?page=2").text

    assert "Older" in first
    assert "Newer" not in first
    # Newest first: the oldest rows are the ones on page two.
    assert "Documents/f0.txt" in second
    assert "Documents/f54.txt" in first
    assert "Documents/f54.txt" not in second


def test_an_unknown_filter_falls_back_to_everything(tmp_path: Path) -> None:
    client, conn, _ = client_for(tmp_path)
    journal_row(conn, src_relpath="a.txt")

    page = client.get("/history?kind=../../etc/passwd")

    assert page.status_code == 200
    assert "Documents/a.txt" in page.text
