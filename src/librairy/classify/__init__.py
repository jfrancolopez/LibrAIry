from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from librairy.ai.orchestrator import AIBatchState, apply_ai_if_needed
from librairy.classify.documents import classify_document_like
from librairy.classify.heuristics import classify_path
from librairy.classify.music import AUDIO_EXTS, classify_music
from librairy.classify.video import VIDEO_EXTS, classify_video
from librairy.config import Settings
from librairy.lifecycle import transition_item
from librairy.models import EvidenceEntry, Item
from librairy.proposals import upsert_proposal
from librairy.scanner import ready_items

CASCADE_EVIDENCE_SOURCES = (
    "heuristic",
    "tags",
    "catalog",
    "library-pattern",
    "ai",  # Phase 3 inserts the AI provider source here.
    "fallback",
)


@dataclass(frozen=True)
class AnalyzeSummary:
    analyzed: int
    proposed: int
    pending: int


def analyze_items(
    conn: sqlite3.Connection, settings: Settings, limit: int | None = None
) -> AnalyzeSummary:
    items = ready_items(conn, "inbox")
    if limit is not None:
        items = items[:limit]
    proposed = pending = 0
    ai_state = AIBatchState({})
    for item in items:
        item_model = _item_from_row(item)
        result = classify_item(
            settings.inbox_dir / item["relpath"],
            item["relpath"],
            settings,
            conn=conn,
            item=item_model,
            ai_state=ai_state,
        )
        proposal_id = upsert_proposal(
            conn,
            item_id=item["id"],
            category=result.category,
            clean_name=result.clean_name,
            dest_relpath=result.dest_relpath,
            confidence=result.confidence,
            evidence=list(result.evidence),
        )
        if result.dest_relpath:
            proposed += 1
            transition_item(conn, item["id"], "proposed")
        else:
            pending += 1
            transition_item(conn, item["id"], "pending")
        conn.execute("UPDATE proposals SET updated_at=updated_at WHERE id=?", (proposal_id,))
    return AnalyzeSummary(len(items), proposed, pending)


def classify_item(
    path: Path,
    relpath: str,
    settings: Settings,
    *,
    conn: sqlite3.Connection | None = None,
    item: Item | None = None,
    ai_state: AIBatchState | None = None,
):
    heuristic = classify_path(path, settings)
    if heuristic is not None:
        return _with_ai(conn, settings, item, ai_state, heuristic)
    suffix = Path(relpath).suffix.lower()
    if suffix in AUDIO_EXTS:
        return _with_ai(conn, settings, item, ai_state, classify_music(relpath, settings=settings))
    if suffix in VIDEO_EXTS:
        return _with_ai(conn, settings, item, ai_state, classify_video(relpath, settings=settings))
    if suffix:
        return _with_ai(
            conn, settings, item, ai_state, classify_document_like(relpath, settings=settings)
        )
    return _with_ai(conn, settings, item, ai_state, _unknown(relpath))


def _with_ai(
    conn: sqlite3.Connection | None,
    settings: Settings,
    item: Item | None,
    ai_state: AIBatchState | None,
    result,
):
    if conn is None or item is None or ai_state is None:
        return result
    return apply_ai_if_needed(conn, settings, item, result, ai_state)


def _item_from_row(row: sqlite3.Row) -> Item:
    return Item(
        id=row["id"],
        root=row["root"],
        relpath=row["relpath"],
        size=row["size"],
        mtime_ns=row["mtime_ns"],
        fingerprint=row["fingerprint"],
        state=row["state"],
        first_seen_at=row["first_seen_at"],
        last_seen_at=row["last_seen_at"],
        missing_since=row["missing_since"],
    )


@dataclass(frozen=True)
class UnknownResult:
    category: str
    clean_name: str
    dest_relpath: str | None
    confidence: float
    evidence: tuple[EvidenceEntry, ...]
    fields: dict[str, object]


def _unknown(relpath: str) -> UnknownResult:
    clean_name = Path(relpath).name
    return UnknownResult(
        "misc",
        clean_name,
        None,
        0.2,
        (EvidenceEntry("heuristic", "category", "unknown item fallback", 0.2),),
        {"clean_name": clean_name},
    )
