from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict

from librairy.models import Category, EvidenceEntry, Proposal
from librairy.planner import utc_now

VALID_EVIDENCE_SOURCES = {
    "heuristic",
    "tags",
    "acoustid",
    "musicbrainz",
    "tmdb",
    "library-pattern",
    "hashtag",
    "ai",
}


class ProposalError(RuntimeError):
    pass


def upsert_proposal(
    conn: sqlite3.Connection,
    *,
    item_id: int,
    category: Category,
    clean_name: str,
    dest_relpath: str | None,
    confidence: float,
    evidence: list[EvidenceEntry],
    group_id: int | None = None,
) -> int:
    validate_evidence(evidence)
    now = utc_now()
    encoded = encode_evidence(evidence)
    existing = conn.execute(
        "SELECT id FROM proposals WHERE item_id=? AND status != 'superseded'",
        (item_id,),
    ).fetchone()
    if existing is None:
        cursor = conn.execute(
            """
            INSERT INTO proposals(
              item_id, category, clean_name, dest_relpath, confidence, group_id,
              status, evidence, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'proposed', ?, ?, ?)
            """,
            (item_id, category, clean_name, dest_relpath, confidence, group_id, encoded, now, now),
        )
        return int(cursor.lastrowid)

    conn.execute(
        """
        UPDATE proposals SET category=?, clean_name=?, dest_relpath=?, confidence=?,
          group_id=?, status='proposed', evidence=?, updated_at=?
        WHERE id=?
        """,
        (category, clean_name, dest_relpath, confidence, group_id, encoded, now, existing["id"]),
    )
    return int(existing["id"])


def supersede_proposal(conn: sqlite3.Connection, item_id: int) -> None:
    conn.execute(
        "UPDATE proposals SET status='superseded', updated_at=? WHERE item_id=?",
        (utc_now(), item_id),
    )


def get_proposal(conn: sqlite3.Connection, proposal_id: int) -> Proposal | None:
    row = conn.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
    if row is None:
        return None
    return proposal_from_row(row)


def proposal_from_row(row: sqlite3.Row) -> Proposal:
    return Proposal(
        id=row["id"],
        item_id=row["item_id"],
        category=row["category"],
        clean_name=row["clean_name"],
        dest_relpath=row["dest_relpath"],
        confidence=row["confidence"],
        group_id=row["group_id"],
        status=row["status"],
        evidence=tuple(decode_evidence(row["evidence"])),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def encode_evidence(evidence: list[EvidenceEntry]) -> str:
    validate_evidence(evidence)
    return json.dumps([asdict(entry) for entry in evidence], sort_keys=True)


def decode_evidence(payload: str) -> list[EvidenceEntry]:
    entries = json.loads(payload)
    evidence = [EvidenceEntry(**entry) for entry in entries]
    validate_evidence(evidence)
    return evidence


def validate_evidence(evidence: list[EvidenceEntry]) -> None:
    for entry in evidence:
        if entry.source not in VALID_EVIDENCE_SOURCES:
            raise ProposalError(f"invalid evidence source: {entry.source}")
        if not 0.0 <= entry.weight <= 1.0:
            raise ProposalError("evidence weight must be between 0.0 and 1.0")
