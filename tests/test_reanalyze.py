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
