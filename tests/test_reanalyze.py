"""Re-analysis of items already sitting in the review queue.

Analysis only ever ran on newly discovered items, so a better classifier, a
newly configured AI provider, or a catalog key added after the first scan never
reached anything already proposed — the queue silently kept its first answer
forever.
"""

from __future__ import annotations

from pathlib import Path

from librairy.classify import analyze_items, requeue_for_analysis
from librairy.config import Settings
from librairy.db import connect
from librairy.scanner import scan_root


def _settings(tmp_path: Path) -> Settings:
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        FILE_STABILITY_SECONDS=0,
        # OLLAMA_HOST defaults to host.docker.internal, so leaving it alone
        # means every analyze in here opens a socket to whatever happens to be
        # answering on the machine running the tests. These tests are about
        # requeueing, not about AI.
        OLLAMA_HOST="",
        _env_file=None,
    )
    for directory in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        directory.mkdir(parents=True, exist_ok=True)
    return settings


def _seeded(tmp_path: Path):
    settings = _settings(tmp_path)
    (settings.inbox_dir / "holiday.jpg").write_text("x", encoding="utf-8")
    (settings.inbox_dir / "notes.txt").write_text("x", encoding="utf-8")
    conn = connect(settings)
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    return conn, settings


def test_plain_analyze_does_not_touch_the_existing_queue(tmp_path: Path) -> None:
    conn, settings = _seeded(tmp_path)
    first = analyze_items(conn, settings)
    assert first.analyzed == 2

    second = analyze_items(conn, settings)

    assert second.analyzed == 0, "a second pass must not re-propose the same items"
    assert second.requeued == 0


def test_reanalyze_requeues_undecided_items(tmp_path: Path) -> None:
    conn, settings = _seeded(tmp_path)
    analyze_items(conn, settings)

    again = analyze_items(conn, settings, reanalyze=True)

    assert again.requeued == 2
    assert again.analyzed == 2


def test_reanalyze_never_touches_decided_items(tmp_path: Path) -> None:
    """Approved, committed and quarantined are decisions already made."""
    conn, settings = _seeded(tmp_path)
    analyze_items(conn, settings)
    decided = conn.execute("SELECT id FROM items ORDER BY id").fetchone()["id"]
    conn.execute("UPDATE items SET state='committed' WHERE id=?", (decided,))

    requeued = requeue_for_analysis(conn)

    assert requeued == 1
    state = conn.execute("SELECT state FROM items WHERE id=?", (decided,)).fetchone()["state"]
    assert state == "committed"


def test_reanalyze_skips_items_that_have_gone_missing(tmp_path: Path) -> None:
    conn, settings = _seeded(tmp_path)
    analyze_items(conn, settings)
    conn.execute("UPDATE items SET missing_since='now' WHERE relpath='notes.txt'")

    assert requeue_for_analysis(conn) == 1


def test_reanalyze_supersedes_rather_than_duplicating_proposals(tmp_path: Path) -> None:
    conn, settings = _seeded(tmp_path)
    analyze_items(conn, settings)

    analyze_items(conn, settings, reanalyze=True)

    live = conn.execute(
        "SELECT COUNT(*) AS n FROM proposals WHERE status != 'superseded'"
    ).fetchone()["n"]
    assert live == 2, "each item must keep exactly one live proposal"


def test_analysis_actually_puts_files_into_groups(tmp_path: Path) -> None:
    """`group_proposals` has existed since phase 2 and nothing ever called it.
    Every proposal ever made had a NULL group_id — 243 of them on the author's
    machine, and not one group row — so Review's default sort, which is
    "keeps albums and seasons together", put everything in Ungrouped. Only the
    tests, which seeded groups by hand, ever saw it work.
    """
    settings = _settings(tmp_path)
    conn = connect(settings)
    disc = settings.inbox_dir / "A Concert DVD5/VIDEO_TS"
    disc.mkdir(parents=True)
    for name in ("VIDEO_TS.IFO", "VTS_01_1.VOB", "VTS_01_2.VOB"):
        (disc / name).write_text(name, encoding="utf-8")
    scan_root(conn, "inbox", settings.inbox_dir, settings)

    analyze_items(conn, settings)

    groups = conn.execute(
        """
        SELECT g.kind, g.label, COUNT(*) AS files
        FROM proposals p JOIN groups g ON g.id = p.group_id
        GROUP BY g.id
        """
    ).fetchall()
    # One disc, one group, all three files in it.
    assert [(row["kind"], row["label"], row["files"]) for row in groups] == [
        ("disc", "A Concert DVD5", 3)
    ]


def test_a_folder_named_after_a_uuid_is_not_a_photo_event(tmp_path: Path) -> None:
    """iMessage keeps attachments in folders named after UUIDs, so every one of
    them arrived as its own "photo event" — a heading of noise per file."""
    from librairy.classify.grouping import GroupInput, group_proposals

    settings = _settings(tmp_path)
    conn = connect(settings)
    noise, named = (
        GroupInput(
            item_id=index,
            relpath=f"{folder}/photo.jpg",
            category="photos",
            clean_name="photo.jpg",
            dest_relpath=f"Photos/2024/{folder}/photo.jpg",
            fields={"event": folder},
        )
        for index, folder in enumerate(("01B583D3-1D28-4B3A-A5DD-9471447CFA27", "Italy"), start=1)
    )

    assert group_proposals(conn, [noise])[0].group_id is None
    assert group_proposals(conn, [named])[0].group_id is not None
