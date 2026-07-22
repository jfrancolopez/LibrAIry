from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from librairy.config import Settings
from librairy.db import MIGRATION_001, SCHEMA_VERSION, connect, user_version
from librairy.models import EvidenceEntry
from librairy.proposals import (
    ProposalError,
    decode_evidence,
    encode_evidence,
    get_proposal,
    upsert_proposal,
)


def settings_for(tmp_path: Path) -> Settings:
    return Settings(APPDATA_DIR=tmp_path / "appdata", _env_file=None)


def insert_item(conn) -> int:
    cursor = conn.execute(
        """
        INSERT INTO items(root, relpath, size, mtime_ns, fingerprint, first_seen_at, last_seen_at)
        VALUES ('inbox', 'a.txt', 1, 1, 'abc', 'now', 'now')
        """
    )
    return int(cursor.lastrowid)


def test_fresh_db_includes_proposal_schema(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path))

    assert user_version(conn) == SCHEMA_VERSION >= 2
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('proposals','groups')"
        )
    }
    assert tables == {"proposals", "groups"}


def test_v1_database_migrates_through_proposal_schema(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    db_path = settings.appdata_dir / "librairy.db"
    db_path.parent.mkdir(parents=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(f"BEGIN;\n{MIGRATION_001}\nPRAGMA user_version=1;\nCOMMIT;")
    conn.execute("PRAGMA user_version=1")
    conn.close()

    migrated = connect(settings)

    assert user_version(migrated) == SCHEMA_VERSION
    migrated.execute("SELECT * FROM proposals")


def test_one_live_proposal_per_item_reanalysis_supersedes_cleanly(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path))
    item_id = insert_item(conn)
    evidence = [EvidenceEntry("heuristic", "category", "text extension", 0.4)]

    proposal_id = upsert_proposal(
        conn,
        item_id=item_id,
        category="documents",
        clean_name="a.txt",
        dest_relpath=None,
        confidence=0.4,
        evidence=evidence,
    )
    updated_id = upsert_proposal(
        conn,
        item_id=item_id,
        category="documents",
        clean_name="A.txt",
        dest_relpath="Documents/2026/A.txt",
        confidence=0.9,
        evidence=[EvidenceEntry("tags", "title", "embedded title", 0.9)],
    )

    assert updated_id == proposal_id
    rows = conn.execute("SELECT * FROM proposals WHERE item_id=?", (item_id,)).fetchall()
    assert len(rows) == 1
    assert rows[0]["clean_name"] == "A.txt"
    assert rows[0]["status"] == "proposed"


def test_evidence_round_trips_as_typed_entries(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path))
    item_id = insert_item(conn)
    evidence = [EvidenceEntry("heuristic", "category", "pdf extension", 0.6)]
    proposal_id = upsert_proposal(
        conn,
        item_id=item_id,
        category="documents",
        clean_name="scan.pdf",
        dest_relpath=None,
        confidence=0.5,
        evidence=evidence,
    )

    proposal = get_proposal(conn, proposal_id)

    assert proposal is not None
    assert proposal.evidence == tuple(evidence)
    assert decode_evidence(encode_evidence(evidence)) == evidence


def test_invalid_evidence_source_is_rejected() -> None:
    with pytest.raises(ProposalError, match="invalid evidence source"):
        encode_evidence([EvidenceEntry("bad-source", "field", "detail", 0.1)])  # type: ignore[arg-type]
