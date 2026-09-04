from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from librairy.config import Settings
from librairy.db import SCHEMA_VERSION, DatabaseVersionError, connect, user_version


def settings_for(tmp_path: Path) -> Settings:
    return Settings(APPDATA_DIR=tmp_path / "appdata", _env_file=None)


def test_fresh_db_migrates_to_current_schema(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path))

    assert user_version(conn) == SCHEMA_VERSION
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    expected_tables = {
        "items",
        "plans",
        "plan_ops",
        "history",
        "settings",
        "sessions",
        "groups",
        "proposals",
        "provider_status",
        "worker_state",
        "similar_media_flags",
        "quarantine_entries",
        "duplicate_reports",
        "review_undo",
        "search_fts",
    }
    assert expected_tables <= tables

    indexes = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
        )
    }
    assert indexes == {
        "idx_items_fingerprint",
        "idx_items_state",
        "idx_plan_ops_plan_id",
        "idx_history_plan_id",
        "idx_proposals_status",
        "idx_proposals_category",
        "idx_proposals_group_id",
        "idx_backup_policies_destination",
        "idx_proposals_group_key",
        "idx_groups_kind",
        "idx_provider_status_kind",
        "idx_provider_status_enabled",
        "idx_similar_media_flags_status",
        "idx_similar_media_flags_item_id",
        "idx_similar_media_flags_similar_item_id",
        "idx_proposals_tier",
        "idx_history_destination",
        # Files held because nothing could answer them: counted by reason, and
        # released oldest-first when a provider comes back.
        "idx_processing_waits_reason",
        "idx_processing_waits_resume",
        # Habits the owner promoted into policies, read once per Review page.
        "idx_decision_rules_enabled",
        # Tags that outlive the name they were written on, looked up by tag
        # for a Project's members and by item for a search index row.
        "idx_item_tags_tag",
        "idx_quarantine_entries_item_id",
        "idx_quarantine_entries_restored_at",
        "idx_duplicate_reports_other",
        "idx_content_extractions_error",
        "idx_backup_queue_state",
        "idx_backup_queue_item_id",
        "idx_vision_results_fingerprint",
        "idx_audit_findings_status",
        "idx_audit_findings_kind",
        "idx_plans_audit_finding",
        "idx_catalog_identity_scope",
        "idx_audit_runs_state",
        "idx_optimization_status",
        "idx_optimization_jobs_state",
        "idx_optimization_jobs_live",
        # A finding may have at most one active correction plan. Partial, so a
        # finished plan never blocks correcting the same folder again.
        "idx_plans_one_active_per_finding",
        "idx_plan_withdrawals_finding",
        "idx_plan_withdrawals_at",
        "idx_reconciliations_item",
        "idx_reconciliations_batch",
        # A quarantine decision is a plan too, and gets the same guarantee:
        # one active decision per held file.
        "idx_plans_quarantine_entry",
        "idx_plans_one_active_per_quarantine",
        "idx_plans_one_active_per_optimization",
        "idx_plans_optimization_job",
        "idx_quarantine_optimization_job",
        # One answer per conflicting file per merge.
        "idx_merge_choices_finding",
        # One answer per thing being placed: a folder, or one loose track.
        "idx_destination_choices_finding",
        # One answer per photograph in one visual group.
        "idx_similar_media_choices_finding",
        # The metadata cache, one row per item per tool.
        "idx_item_metadata_item",
        # Companion relationships, reachable from either side of the pair.
        "idx_item_relationships_low",
        "idx_item_relationships_high",
        # Decision memory, looked up by cue signature and settled by plan.
        "idx_decision_events_signature",
        "idx_decision_events_plan",
        # What a decision was told about the relationships it touches.
        "idx_plan_relationships_plan",
        # What the owner prefers, permits and protects.
        "idx_format_policy_kind",
        # Which later decision consumed the state an earlier one created.
        "idx_plan_ops_item",
        "idx_history_op",
    }

    #  The identity column migration 046 added, so a rebuild and a fresh
    #  database cannot drift apart about what Search indexes.
    search_columns = {row[1] for row in conn.execute("PRAGMA table_info(search_fts)")}
    assert "identity" in search_columns

    columns = {row[1] for row in conn.execute("PRAGMA table_info(provider_status)")}
    assert "available_models" in columns
    proposal_columns = {row[1] for row in conn.execute("PRAGMA table_info(proposals)")}
    assert {"action", "dest_root"} <= proposal_columns


def test_reopening_db_is_noop(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    connect(settings).close()
    conn = connect(settings)

    assert user_version(conn) == SCHEMA_VERSION


def test_wal_and_foreign_keys_are_active(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path))

    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_migration_011_closes_proposals_for_files_already_filed(tmp_path: Path) -> None:
    """Anyone who committed before this release has proposals stuck at
    'proposed' for files that already moved — a review queue asking them to
    move a file to where it already is. Upgrading clears them, and touches
    nothing that still has somewhere to go.
    """
    settings = settings_for(tmp_path)
    conn = connect(settings)
    conn.executescript(
        """
        INSERT INTO items(id, root, relpath, size, mtime_ns, fingerprint,
                          first_seen_at, last_seen_at)
        VALUES (1, 'library', 'Movies/A.mkv', 1, 1, 'fp1', 'now', 'now'),
               (2, 'inbox',   'b.mkv',        1, 1, 'fp2', 'now', 'now'),
               (3, 'library', 'Movies/C.mkv', 1, 1, 'fp3', 'now', 'now');
        INSERT INTO proposals(item_id, category, clean_name, dest_relpath, confidence,
                              status, action, dest_root, evidence, created_at, updated_at)
        -- already standing at its destination: the move happened
        VALUES (1, 'movies', 'A.mkv', 'Movies/A.mkv', 0.9,
                'proposed', 'move', 'library', '[]', 'now', 'now'),
        -- still in the inbox: real work, must survive
               (2, 'movies', 'B.mkv', 'Movies/B.mkv', 0.9,
                'proposed', 'move', 'library', '[]', 'now', 'now'),
        -- in the library but proposed somewhere else: also real, must survive
               (3, 'movies', 'C.mkv', 'Movies/Reorganised/C.mkv', 0.9,
                'proposed', 'move', 'library', '[]', 'now', 'now');
        """
    )
    # Pinned to 10, not SCHEMA_VERSION - 1: this is about migration 011 alone,
    # and rewinding one step from whatever the head happens to be replays some
    # other migration instead. Rewinding replays 011 *and everything after it*,
    # so anything a later migration creates has to be put back first.
    conn.executescript(
        """
        DROP INDEX IF EXISTS idx_backup_policies_destination;
        DROP TABLE IF EXISTS backup_policies;
        DROP TABLE IF EXISTS backup_destinations;
        DROP TABLE IF EXISTS metrics_daily;
        DROP INDEX IF EXISTS idx_proposals_group_key;
        DROP INDEX IF EXISTS idx_item_tags_tag;
        DROP TABLE IF EXISTS item_tags;
        DROP TABLE IF EXISTS projects;
        DROP INDEX IF EXISTS idx_decision_rules_enabled;
        DROP TABLE IF EXISTS decision_rules;
        DROP INDEX IF EXISTS idx_processing_waits_reason;
        DROP INDEX IF EXISTS idx_processing_waits_resume;
        DROP TABLE IF EXISTS processing_waits;
        DROP INDEX IF EXISTS idx_reconciliations_item;
        DROP INDEX IF EXISTS idx_reconciliations_batch;
        DROP TABLE IF EXISTS reconciliations;
        DROP TABLE IF EXISTS duplicate_reports;
        DROP TABLE IF EXISTS review_undo;
        DROP TABLE IF EXISTS vision_results;
        DROP INDEX IF EXISTS idx_audit_runs_state;
        DROP INDEX IF EXISTS idx_optimization_status;
        DROP INDEX IF EXISTS idx_optimization_jobs_state;
        DROP INDEX IF EXISTS idx_optimization_jobs_live;
        DROP TABLE IF EXISTS audit_runs;
        DROP TABLE IF EXISTS optimization_jobs;
        DROP TABLE IF EXISTS optimization_opportunities;
        DROP INDEX IF EXISTS idx_plan_ops_item;
        DROP INDEX IF EXISTS idx_history_op;
        DROP INDEX IF EXISTS idx_format_policy_kind;
        DROP TABLE IF EXISTS format_policy_scopes;
        DROP INDEX IF EXISTS idx_plan_relationships_plan;
        DROP TABLE IF EXISTS plan_relationships;
        DROP INDEX IF EXISTS idx_decision_events_signature;
        DROP INDEX IF EXISTS idx_decision_events_plan;
        DROP TABLE IF EXISTS decision_events;
        DROP TABLE IF EXISTS decision_suppressions;
        DROP INDEX IF EXISTS idx_item_relationships_low;
        DROP INDEX IF EXISTS idx_item_relationships_high;
        DROP TABLE IF EXISTS item_relationships;
        DROP TABLE IF EXISTS document_work_choices;
        DROP INDEX IF EXISTS idx_item_metadata_item;
        DROP TABLE IF EXISTS item_metadata;
        DROP INDEX IF EXISTS idx_similar_media_choices_finding;
        DROP TABLE IF EXISTS similar_media_choices;
        DROP TABLE IF EXISTS track_identity;
        DROP INDEX IF EXISTS idx_catalog_identity_scope;
        DROP TABLE IF EXISTS catalog_identity;
        DROP INDEX IF EXISTS idx_plans_audit_finding;
        -- Before the column it is built on, or SQLite refuses the drop. Both
        -- indexes on `audit_finding_id` have to go for this to look like a
        -- database from before that column existed.
        DROP INDEX IF EXISTS idx_plans_one_active_per_finding;
        DROP INDEX IF EXISTS idx_plans_one_active_per_quarantine;
        DROP INDEX IF EXISTS idx_plans_one_active_per_optimization;
        DROP INDEX IF EXISTS idx_plans_optimization_job;
        DROP INDEX IF EXISTS idx_quarantine_optimization_job;
        DROP INDEX IF EXISTS idx_plans_quarantine_entry;
        DROP INDEX IF EXISTS idx_plan_withdrawals_finding;
        DROP INDEX IF EXISTS idx_plan_withdrawals_at;
        DROP TABLE IF EXISTS plan_withdrawals;
        -- Migrations 030 and 031. Before `audit_findings` below, which both
        -- of them point at.
        DROP INDEX IF EXISTS idx_merge_choices_finding;
        DROP TABLE IF EXISTS merge_choices;
        DROP INDEX IF EXISTS idx_destination_choices_finding;
        DROP TABLE IF EXISTS destination_choices;
        -- On a table migration 010 created, so it outlives the drops above
        -- and has to come off by name.
        ALTER TABLE backup_queue DROP COLUMN verified;
        ALTER TABLE similar_media_flags DROP COLUMN dismissed_fingerprints;
        ALTER TABLE plans DROP COLUMN relationships_checked;
        ALTER TABLE plans DROP COLUMN restore_of_plan_id;
        ALTER TABLE plans DROP COLUMN coherent;
        ALTER TABLE plans DROP COLUMN optimization_job_id;
        ALTER TABLE quarantine_entries DROP COLUMN optimization_job_id;
        ALTER TABLE plans DROP COLUMN quarantine_entry_id;
        ALTER TABLE plans DROP COLUMN audit_finding_id;
        ALTER TABLE plan_ops DROP COLUMN role;
        DROP TABLE IF EXISTS audit_findings;
        PRAGMA user_version=10;
        """
    )
    conn.close()

    reopened = connect(settings)
    statuses = dict(reopened.execute("SELECT item_id, status FROM proposals").fetchall())

    assert statuses == {1: "committed", 2: "proposed", 3: "proposed"}


def test_newer_database_version_is_rejected(tmp_path: Path) -> None:
    db_path = tmp_path / "appdata" / "librairy.db"
    db_path.parent.mkdir()
    conn = sqlite3.connect(db_path)
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
    conn.close()

    with pytest.raises(DatabaseVersionError, match="newer than this code supports"):
        connect(settings_for(tmp_path))


def test_two_connections_can_write_without_database_locked(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    connect(settings).close()
    errors: list[BaseException] = []

    def writer(start: int) -> None:
        try:
            conn = connect(settings)
            for index in range(start, start + 20):
                conn.execute(
                    "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
                    (f"key-{index}", f'{{"value": {index}}}'),
                )
            conn.close()
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [
        threading.Thread(target=writer, args=(0,)),
        threading.Thread(target=writer, args=(20,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    conn = connect(settings)
    #  The forty this test wrote, counted by name rather than by counting the
    #  whole table: `settings` is where the program keeps its own state too, so
    #  a total is a number that changes whenever anything else records
    #  something — which is what it did when automatic approval started
    #  stamping its generation boundary there.
    written = conn.execute(
        "SELECT COUNT(*) FROM settings WHERE key LIKE 'key-%'"
    ).fetchone()[0]
    assert written == 40
