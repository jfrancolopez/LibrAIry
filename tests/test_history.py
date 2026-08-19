from __future__ import annotations

from pathlib import Path

from librairy.config import Settings
from librairy.db import connect
from librairy.executor import execute_plan
from librairy.history import undo_op, undo_plan
from librairy.planner import OperationSpec, approve_plan, create_plan
from librairy.scanner import scan_root


def settings_for(tmp_path: Path) -> Settings:
    return Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        FILE_STABILITY_SECONDS=0,
        _env_file=None,
    )


def setup_committed_plan(tmp_path: Path, op_type: str = "move"):
    settings = settings_for(tmp_path)
    settings.inbox_dir.mkdir()
    settings.library_dir.mkdir()
    settings.quarantine_dir.mkdir()
    (settings.inbox_dir / "a.txt").write_text("a", encoding="utf-8")
    conn = connect(settings)
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    dest_root = "quarantine" if op_type == "quarantine" else "library"
    dest_relpath = "dupes/a.txt" if op_type == "quarantine" else "Documents/a.txt"
    plan_id = create_plan(
        conn,
        [OperationSpec(op_type, "a.txt", dest_root, dest_relpath)],
        settings,
    )
    approve_plan(conn, plan_id, settings)
    execute_plan(conn, plan_id, settings)
    return settings, conn, plan_id


def test_undo_plan_restores_original_tree_and_journals_undo(tmp_path: Path) -> None:
    settings, conn, plan_id = setup_committed_plan(tmp_path)

    results = undo_plan(conn, plan_id, settings)

    assert results[0].outcome == "ok"
    assert (settings.inbox_dir / "a.txt").read_text(encoding="utf-8") == "a"
    assert not (settings.library_dir / "Documents/a.txt").exists()
    actions = [
        row["action"]
        for row in conn.execute(
            "SELECT action FROM history WHERE plan_id=? ORDER BY id",
            (plan_id,),
        )
    ]
    assert actions == ["move", "undo_move"]


def test_undo_refuses_modified_destination(tmp_path: Path) -> None:
    settings, conn, plan_id = setup_committed_plan(tmp_path)
    moved = settings.library_dir / "Documents/a.txt"
    moved.write_text("changed", encoding="utf-8")

    result = undo_plan(conn, plan_id, settings)[0]

    assert result.outcome.startswith("undo_refused_changed")
    assert moved.read_text(encoding="utf-8") == "changed"
    assert not (settings.inbox_dir / "a.txt").exists()


def test_undo_of_undo_is_possible(tmp_path: Path) -> None:
    settings, conn, plan_id = setup_committed_plan(tmp_path)
    undo_plan(conn, plan_id, settings)
    undo_history_id = conn.execute(
        "SELECT id FROM history WHERE action='undo_move'"
    ).fetchone()[0]

    result = undo_op(conn, undo_history_id, settings)

    assert result.outcome == "ok"
    assert (settings.library_dir / "Documents/a.txt").read_text(encoding="utf-8") == "a"
    assert not (settings.inbox_dir / "a.txt").exists()


def test_undo_quarantine_restores_original_path(tmp_path: Path) -> None:
    settings, conn, plan_id = setup_committed_plan(tmp_path, op_type="quarantine")

    result = undo_plan(conn, plan_id, settings)[0]

    assert result.outcome == "ok"
    assert (settings.inbox_dir / "a.txt").read_text(encoding="utf-8") == "a"
    assert not (settings.quarantine_dir / "dupes/a.txt").exists()


def test_every_journalled_action_is_reachable_under_some_filter(tmp_path: Path) -> None:
    """A row visible only under "Everything" is a row nobody can find.

    The filters named one action each — `action='move'` — and the journal has
    written eight action values for years. `quarantine`, `mark_for_deletion`,
    `restore_quarantine` and `undo_quarantine` matched none of them, so the
    entry saying an optimization original had been preserved in Quarantine
    appeared under Everything and under nothing else. The visible symptom was
    arithmetic: four buckets summing to six above a page reading 7.
    """
    from librairy.history import HISTORY_KINDS, kind_counts

    settings, conn = _history_scene(tmp_path)
    # Every `action` any module in `src/librairy` writes to `history`, with the
    # destination root each one actually uses.
    written = [
        ("move", "library", "ok"),
        ("move", "quarantine", "ok"),
        ("quarantine", "quarantine", "ok"),
        ("mark_for_deletion", "quarantine", "ok"),
        ("restore_quarantine", "library", "ok"),
        ("undo_move", "inbox", "ok"),
        ("undo_quarantine", "library", "ok"),
        ("settings_change", "", "ollama -> lmstudio"),
        ("adoption_recovery", "library", "recovery_required failed"),
    ]
    for index, (action, dest_root, outcome) in enumerate(written, start=1):
        conn.execute(
            "INSERT INTO history(ts, plan_id, op_id, action, src_root, src_relpath,"
            " dest_root, dest_relpath, fingerprint, outcome)"
            " VALUES (?, 'p', ?, ?, 'library', ?, ?, ?, 'h', ?)",
            (f"2026-08-19T09:0{index}:00+00:00", index, action,
             f"a/{action}.flac", dest_root, f"b/{action}.flac", outcome),
        )

    counts = kind_counts(conn)

    #  Summing is not enough on its own — one row counted twice would hide one
    #  counted never. So: every row lands in exactly one bucket, and the
    #  buckets account for the whole journal.
    homeless = [
        action
        for action, _dest, _outcome in written
        if not any(
            conn.execute(
                f"SELECT COUNT(*) FROM history WHERE action=? AND ({predicate})",  # noqa: S608
                (action,),
            ).fetchone()[0]
            for key, (_label, predicate) in HISTORY_KINDS.items()
            if predicate
        )
    ]
    buckets = sum(counts[key] for key in HISTORY_KINDS if key != "all")

    assert homeless == [], "these appear only under Everything"
    assert buckets == counts["all"], "a row is counted twice or not at all"


def _history_scene(tmp_path: Path):  # noqa: ANN202
    from librairy.config import Settings
    from librairy.db import connect

    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        _env_file=None,
    )
    for directory in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        directory.mkdir(parents=True, exist_ok=True)
    return settings, connect(settings)
