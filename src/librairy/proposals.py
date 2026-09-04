from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict

from librairy.catalogs import CATALOGS
from librairy.confidence_tiers import tier_for
from librairy.models import Category, EvidenceEntry, Proposal
from librairy.planner import utc_now
from librairy.search import sync_search_item

# Sources that are not catalogs: local signals, and the AI fallback.
LOCAL_EVIDENCE_SOURCES = frozenset(
    {
        "heuristic",
        "tags",
        "library-pattern",
        "hashtag",
        "ai",
        # A local model that opened the file and looked at the picture. Its own
        # source rather than "ai", because Review draws them differently and
        # because "it read the name" and "it saw the thing" are not the same
        # claim about a file.
        "vision",
        # Not a catalog and not a guess: the file is *named* like cover art and
        # the folder it arrived in was identified as one album or film.
        "artwork",
        # What the Library Audit reads. It records evidence in this same shape,
        # and until these were listed here `decode_evidence` rejected every
        # audit finding — so the evidence was stored faithfully and then thrown
        # away by the one function that renders it, and every Why panel on the
        # audit said "No evidence recorded".
        #
        # Neither is a catalog and neither is a guess: one is the shape of the
        # library on disk, the other is the file's own bytes.
        "filesystem",
        "fingerprint",
        # The same idea for the files that describe media rather than being it
        # — subtitles, playlists, .nfo. Its own source because Review says
        # "Companion file", not "Cover art", when it explains one.
        "companion",
        # What a document said about itself: the title out of a PDF's Info
        # dictionary, an EPUB's OPF, an ISBN or a DOI in the front matter. Its
        # own source rather than "heuristic", because it is not one — it is the
        # file speaking — and because `ai.redact` drops exactly this source
        # before anything leaves the machine.
        "document",
    }
)
# Every catalog is a legal evidence source by definition, derived from the
# registry rather than restated here. Listing them twice is how adding a
# catalog turns into a ProposalError that aborts the whole analyze batch the
# first time that catalog actually matches something.
VALID_EVIDENCE_SOURCES = LOCAL_EVIDENCE_SOURCES | {catalog.slug for catalog in CATALOGS}


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
    action: str = "move",
    dest_root: str = "library",
    group_id: int | None = None,
    group_key: str | None = None,
    group_hint: str | None = None,
) -> int:
    validate_evidence(evidence)
    validate_action(action, dest_root)
    now = utc_now()
    encoded = encode_evidence(evidence)
    #  Decided here because here is where the evidence is written: a tier that
    #  was computed at some other moment would describe an older analysis. See
    #  `librairy/confidence_tiers.py` for what each one means.
    tier = tier_for(evidence, confidence, dest_relpath)
    existing = conn.execute(
        "SELECT id FROM proposals WHERE item_id=? AND status != 'superseded'",
        (item_id,),
    ).fetchone()
    if existing is None:
        cursor = conn.execute(
            """
            INSERT INTO proposals(
              item_id, category, clean_name, dest_relpath, confidence, action, dest_root, group_id,
              group_key, group_hint, status, evidence, tier, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'proposed', ?, ?, ?, ?)
            """,
            (
                item_id,
                category,
                clean_name,
                dest_relpath,
                confidence,
                action,
                dest_root,
                group_id,
                group_key,
                group_hint,
                encoded,
                tier,
                now,
                now,
            ),
        )
        proposal_id = int(cursor.lastrowid)
        sync_search_item(conn, item_id)
        return proposal_id

    conn.execute(
        """
        UPDATE proposals SET category=?, clean_name=?, dest_relpath=?, confidence=?,
          action=?, dest_root=?, group_id=?, group_key=?, group_hint=?,
          status='proposed', evidence=?, tier=?, updated_at=?
        WHERE id=?
        """,
        (
            category,
            clean_name,
            dest_relpath,
            confidence,
            action,
            dest_root,
            group_id,
            group_key,
            group_hint,
            encoded,
            tier,
            now,
            existing["id"],
        ),
    )
    sync_search_item(conn, item_id)
    return int(existing["id"])


def supersede_proposal(conn: sqlite3.Connection, item_id: int) -> None:
    conn.execute(
        "UPDATE proposals SET status='superseded', updated_at=? WHERE item_id=?",
        (utc_now(), item_id),
    )
    sync_search_item(conn, item_id)


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
        action=row["action"],
        dest_root=row["dest_root"],
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


def validate_action(action: str, dest_root: str) -> None:
    if action not in {"move", "quarantine"}:
        raise ProposalError(f"invalid proposal action: {action}")
    expected_root = "quarantine" if action == "quarantine" else "library"
    if dest_root != expected_root:
        raise ProposalError(f"{action} proposals must target {expected_root}")


# What a proposal's status means to somebody who did not write the schema.
# `proposed` is the machine's guess and `pending` is not a proposal status at
# all — printing either raw makes the reader translate, and they can only
# translate it wrong.
PROPOSAL_LABELS = {
    "proposed": "Waiting for review",
    "approved": "Approved, not committed",
    "postponed": "Postponed",
    "rejected": "Rejected",
    "committed": "Committed",
    "superseded": "Superseded",
}


def proposal_label(status: str | None) -> str:
    """A readable name for a proposal status, or the raw value if it is new.

    Falling back to the raw value rather than to "unknown": if a status is
    added and nobody updates this map, an odd-looking word on screen is a
    better bug report than a confident wrong label.
    """
    if not status:
        return ""
    return PROPOSAL_LABELS.get(status, status)
