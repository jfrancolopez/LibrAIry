from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from librairy.classify.documents import classify_document_like
from librairy.classify.heuristics import classify_path
from librairy.classify.music import AUDIO_EXTS, classify_music
from librairy.classify.video import VIDEO_EXTS, classify_video
from librairy.config import Settings
from librairy.models import EvidenceEntry
from librairy.proposals import upsert_proposal
from librairy.scanner import ready_items


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
    for item in items:
        result = classify_item(settings.inbox_dir / item["relpath"], item["relpath"], settings)
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
        else:
            pending += 1
        conn.execute("UPDATE proposals SET updated_at=updated_at WHERE id=?", (proposal_id,))
    return AnalyzeSummary(len(items), proposed, pending)


def classify_item(path: Path, relpath: str, settings: Settings):
    heuristic = classify_path(path, settings)
    if heuristic is not None:
        return heuristic
    suffix = Path(relpath).suffix.lower()
    if suffix in AUDIO_EXTS:
        return classify_music(relpath, settings=settings)
    if suffix in VIDEO_EXTS:
        return classify_video(relpath, settings=settings)
    if suffix:
        return classify_document_like(relpath, settings=settings)
    return _unknown(relpath)


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
