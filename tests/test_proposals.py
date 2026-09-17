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
    supersede_proposal,
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


def test_re_upserting_a_live_proposal_updates_it_in_place(tmp_path: Path) -> None:
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


def test_analysing_an_item_whose_only_proposal_was_superseded_revives_it(
    tmp_path: Path,
) -> None:
    """The case the test above is *named* after, and never actually reached.

    `test_re_upserting_a_live_proposal_updates_it_in_place` calls this function
    twice over a live row. It was called "...reanalysis supersedes cleanly" and
    read like coverage of the supersede path while never putting a row into
    `superseded` at all, which is why this shipped.

    What it missed: `upsert_proposal` looked for an existing row with
    `status != 'superseded'` while the table constrains `UNIQUE (item_id)` with
    no such exemption. With only a superseded row present the lookup answered
    "nothing here" and the insert went into the constraint:

        sqlite3.IntegrityError: UNIQUE constraint failed: proposals.item_id

    That is not a corner. The scanner supersedes a pending guess the moment a
    file's bytes change underneath it and puts the item back to 'discovered' so
    it is analysed again — an owner editing something that is waiting in Review
    — and the re-analysis then raised. The exception aborted the whole batch,
    so every file queued behind it went unanalysed, and the worker process died
    and was restarted until the supervisor gave up and took the container with
    it.
    """
    conn = connect(settings_for(tmp_path))
    item_id = insert_item(conn)

    first = upsert_proposal(
        conn,
        item_id=item_id,
        category="documents",
        clean_name="a.txt",
        dest_relpath="Documents/a.txt",
        confidence=0.4,
        evidence=[EvidenceEntry("heuristic", "category", "text extension", 0.4)],
    )
    #  What the scanner does when the file changes under the proposal.
    supersede_proposal(conn, item_id)
    assert conn.execute(
        "SELECT status FROM proposals WHERE id=?", (first,)
    ).fetchone()["status"] == "superseded"

    revived = upsert_proposal(
        conn,
        item_id=item_id,
        category="documents",
        clean_name="a-v2.txt",
        dest_relpath="Documents/a-v2.txt",
        confidence=0.9,
        evidence=[EvidenceEntry("tags", "title", "embedded title", 0.9)],
    )

    assert revived == first, "the item's one row is reused, not duplicated"
    rows = conn.execute("SELECT * FROM proposals WHERE item_id=?", (item_id,)).fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "proposed", "the fresh analysis is the live one"
    assert rows[0]["clean_name"] == "a-v2.txt"
    assert rows[0]["confidence"] == 0.9  # noqa: PLR2004


def test_the_lookup_that_decides_insert_or_update_matches_the_constraint(
    tmp_path: Path,
) -> None:
    """A narrower statement of the same defect, so a future edit cannot
    reintroduce it by putting the status filter back for a different reason.

    Whatever `upsert_proposal` uses to decide between INSERT and UPDATE has to
    count exactly what `UNIQUE (item_id)` counts. Any row, any status.
    """
    conn = connect(settings_for(tmp_path))
    item_id = insert_item(conn)
    upsert_proposal(
        conn,
        item_id=item_id,
        category="documents",
        clean_name="a.txt",
        dest_relpath=None,
        confidence=0.4,
        evidence=[EvidenceEntry("heuristic", "category", "text extension", 0.4)],
    )

    for status in ("superseded", "rejected", "postponed", "approved", "committed"):
        conn.execute("UPDATE proposals SET status=? WHERE item_id=?", (status, item_id))
        upsert_proposal(
            conn,
            item_id=item_id,
            category="documents",
            clean_name=f"a-{status}.txt",
            dest_relpath=None,
            confidence=0.5,
            evidence=[EvidenceEntry("heuristic", "category", "text extension", 0.5)],
        )
        count = conn.execute(
            "SELECT COUNT(*) FROM proposals WHERE item_id=?", (item_id,)
        ).fetchone()[0]
        assert count == 1, f"a second row appeared after re-analysing a {status} proposal"


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
