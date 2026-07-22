from __future__ import annotations

from pathlib import Path

import pytest

from librairy.config import Settings
from librairy.db import connect
from librairy.lifecycle import LifecycleError, assert_transition, state_counts, transition_item
from librairy.models import EvidenceEntry
from librairy.proposals import upsert_proposal
from librairy.scanner import ready_items, scan_root


def settings_for(tmp_path: Path) -> Settings:
    return Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        FILE_STABILITY_SECONDS=0,
        _env_file=None,
    )


def test_legal_and_illegal_transitions_are_explicit() -> None:
    assert_transition("discovered", "proposed")

    with pytest.raises(LifecycleError):
        assert_transition("committed", "approved")


def test_transition_item_and_state_counts(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    settings.inbox_dir.mkdir()
    (settings.inbox_dir / "a.txt").write_text("a", encoding="utf-8")
    conn = connect(settings)
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    item_id = conn.execute("SELECT id FROM items").fetchone()[0]

    transition_item(conn, item_id, "pending")

    assert state_counts(conn) == {"pending": 1}


def test_changed_fingerprint_resets_proposed_item_and_supersedes_proposal(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    settings.inbox_dir.mkdir()
    path = settings.inbox_dir / "a.txt"
    path.write_text("a", encoding="utf-8")
    conn = connect(settings)
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    item_id = conn.execute("SELECT id FROM items").fetchone()[0]
    upsert_proposal(
        conn,
        item_id=item_id,
        category="documents",
        clean_name="a.txt",
        dest_relpath="Documents/a.txt",
        confidence=0.9,
        evidence=[EvidenceEntry("heuristic", "category", "test", 0.9)],
    )
    transition_item(conn, item_id, "proposed")

    path.write_text("changed", encoding="utf-8")
    scan_root(conn, "inbox", settings.inbox_dir, settings)

    assert (
        conn.execute("SELECT state FROM items WHERE id=?", (item_id,)).fetchone()[0] == "discovered"
    )
    assert (
        conn.execute("SELECT status FROM proposals WHERE item_id=?", (item_id,)).fetchone()[0]
        == "superseded"
    )
    assert len(ready_items(conn)) == 1


def test_ready_items_excludes_pending_and_proposed(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    settings.inbox_dir.mkdir()
    (settings.inbox_dir / "a.txt").write_text("a", encoding="utf-8")
    conn = connect(settings)
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    item_id = conn.execute("SELECT id FROM items").fetchone()[0]

    transition_item(conn, item_id, "pending")

    assert ready_items(conn) == []
