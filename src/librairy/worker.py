from __future__ import annotations

import argparse
import json
import signal
import sqlite3
import time
from dataclasses import asdict, dataclass

from librairy.classify import analyze_items
from librairy.config import Settings
from librairy.content.extract import process_content_extractions
from librairy.db import connect
from librairy.dedup import (
    detect_exact_duplicates,
    detect_similar_media,
    hash_size_colliding_library_files,
)
from librairy.lifecycle import transition_item
from librairy.locks import acquire_lock
from librairy.models import EvidenceEntry
from librairy.planner import utc_now
from librairy.proposals import upsert_proposal
from librairy.quarantine import quarantine_operation
from librairy.scanner import scan_root
from librairy.settings_service import effective_settings

IDLE_SLEEP_SECONDS = 5.0
BUSY_SLEEP_SECONDS = 0.5
MAX_SLEEP_SECONDS = 60.0


@dataclass(frozen=True)
class WorkerSummary:
    scanned: int
    hashed: int
    library_hashed: int
    duplicate_candidates: int
    similar_flags: int
    analyzed: int
    proposed: int
    pending: int
    content_extracted: int = 0
    content_failed: int = 0

    @property
    def work_found(self) -> bool:
        return any(
            (
                self.scanned,
                self.hashed,
                self.library_hashed,
                self.duplicate_candidates,
                self.similar_flags,
                self.analyzed,
                self.content_extracted,
            )
        )


class Worker:
    def __init__(self, conn: sqlite3.Connection, settings: Settings) -> None:
        self.conn = conn
        self.settings = settings
        self.stop_requested = False

    def request_stop(self, signum=None, frame=None) -> None:  # noqa: ARG002
        self.stop_requested = True

    def run_once(self) -> WorkerSummary:
        with acquire_lock(self.settings):
            settings = effective_settings(self.conn, self.settings)
            _set_worker_state(self.conn, "current_phase", "scan")
            scan = scan_root(self.conn, "inbox", settings.inbox_dir, settings)
            _set_worker_state(self.conn, "current_phase", "dedup")
            library_hashed = hash_size_colliding_library_files(self.conn, settings)
            duplicate_candidates = _stage_quarantine_proposals(
                self.conn,
                detect_exact_duplicates(self.conn, settings),
            )
            similar_flags = detect_similar_media(self.conn, settings)
            _set_worker_state(self.conn, "current_phase", "analyze")
            analysis = analyze_items(self.conn, settings, settings.batch_size)
            _set_worker_state(self.conn, "current_phase", "content")
            content = process_content_extractions(self.conn, settings, settings.batch_size)
            summary = WorkerSummary(
                scanned=scan.discovered,
                hashed=scan.hashed,
                library_hashed=library_hashed,
                duplicate_candidates=duplicate_candidates,
                similar_flags=similar_flags,
                analyzed=analysis.analyzed,
                proposed=analysis.proposed,
                pending=analysis.pending,
                content_extracted=content.extracted,
                content_failed=content.failed,
            )
            _set_worker_state(self.conn, "last_cycle_at", utc_now())
            _set_worker_state(self.conn, "current_phase", "idle")
            _set_worker_state(self.conn, "last_summary", asdict(summary))
            return summary

    def run_forever(self) -> None:
        sleep_seconds = BUSY_SLEEP_SECONDS
        while not self.stop_requested:
            summary = self.run_once()
            sleep_seconds = next_sleep(sleep_seconds, summary.work_found)
            _sleep_interruptibly(sleep_seconds, self)


def run_once(conn: sqlite3.Connection, settings: Settings) -> WorkerSummary:
    return Worker(conn, settings).run_once()


def run_forever(conn: sqlite3.Connection, settings: Settings) -> None:
    worker = Worker(conn, settings)
    signal.signal(signal.SIGTERM, worker.request_stop)
    signal.signal(signal.SIGINT, worker.request_stop)
    worker.run_forever()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="librairy worker")
    parser.add_argument("--once", action="store_true", help="Run one worker cycle and exit")
    args = parser.parse_args(argv)
    settings = Settings()
    conn = connect(settings)
    if args.once:
        run_once(conn, settings)
        return 0
    run_forever(conn, settings)
    return 0


def next_sleep(previous: float, work_found: bool) -> float:
    if work_found:
        return BUSY_SLEEP_SECONDS
    return min(max(previous * 2, IDLE_SLEEP_SECONDS), MAX_SLEEP_SECONDS)


def _sleep_interruptibly(seconds: float, worker: Worker) -> None:
    deadline = time.monotonic() + seconds
    while not worker.stop_requested and time.monotonic() < deadline:
        time.sleep(min(0.1, deadline - time.monotonic()))


def _set_worker_state(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO worker_state(key, value) VALUES (?, ?)",
        (key, json.dumps(value, sort_keys=True)),
    )


def _stage_quarantine_proposals(conn: sqlite3.Connection, candidates) -> int:
    staged = 0
    for candidate in candidates:
        if candidate.status != "confirmed" or candidate.duplicate.state != "discovered":
            continue
        op = quarantine_operation(candidate.duplicate.relpath)
        upsert_proposal(
            conn,
            item_id=candidate.duplicate.id,
            category="misc",
            clean_name=candidate.duplicate.relpath.rsplit("/", 1)[-1],
            dest_relpath=op.dest_relpath,
            confidence=1.0,
            evidence=[
                EvidenceEntry(
                    "heuristic",
                    "category",
                    f"exact duplicate of {candidate.keeper.root}:{candidate.keeper.relpath}",
                    1.0,
                )
            ],
            action="quarantine",
            dest_root="quarantine",
        )
        transition_item(conn, candidate.duplicate.id, "quarantine-proposed")
        staged += 1
    return staged
