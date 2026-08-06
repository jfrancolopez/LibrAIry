from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import sqlite3
import time
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path

from librairy.backup import run_backup_once
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
# How often, while idle, to check whether the inbox changed under us.
INBOX_POLL_SECONDS = 2.0


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
    backup_copied: int = 0
    backup_failed: int = 0

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
                self.backup_copied,
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
            _set_worker_state(self.conn, "current_phase", "backup")
            backup = run_backup_once(self.conn, settings, batch_size=settings.batch_size)
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
                backup_copied=backup.copied,
                backup_failed=backup.failed,
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


def inbox_signature(inbox_dir: Path) -> str:
    """A cheap fingerprint that changes when anything is added to the inbox.

    Hashes the names of everything in the tree, plus each directory's mtime.
    Names are what os.walk already collected, so they cost no extra syscalls,
    and they are what makes a drop detectable *immediately*: directory mtimes
    alone come from a coarse kernel clock, so a file landing in the same tick
    as the previous poll leaves the timestamp untouched and the drop invisible
    until something else disturbs the folder.

    Directory mtimes stay in because they also move when a file is replaced
    in place under a name that was already there. The inbox is a staging area,
    not a library, so this stays small and fast however big the library grows.
    """
    # os.walk swallows its own errors, so a missing inbox would otherwise hash
    # to a perfectly stable "empty tree" and look like a healthy quiet one. An
    # absent inbox is the scanner's problem to report; here it is just "nothing
    # to wake up for".
    if not inbox_dir.is_dir():
        return ""
    hasher = hashlib.blake2b(digest_size=16)
    for root, dirnames, filenames in os.walk(inbox_dir):
        # Deterministic order, or the same tree hashes differently each pass.
        # os.walk reads dirnames back after the yield, so sorting in place is
        # also what keeps the traversal order stable.
        dirnames.sort()
        mtime = ""
        with suppress(OSError):
            mtime = str(os.stat(root).st_mtime_ns)
        hasher.update(f"{root}:{mtime}:{','.join(sorted(filenames))}\n".encode())
    return hasher.hexdigest()


def _sleep_interruptibly(seconds: float, worker: Worker) -> None:
    """Sleep, but cut it short the moment something lands in the inbox.

    The idle backoff climbs to a minute, which is fine for a quiet system and
    far too long to wait for a file you just dropped in. Polling one cheap
    fingerprint every couple of seconds turns "up to 60s" into "about 2s"
    without a filesystem-watching dependency.
    """
    deadline = time.monotonic() + seconds
    baseline = inbox_signature(worker.settings.inbox_dir)
    next_check = time.monotonic() + INBOX_POLL_SECONDS
    while not worker.stop_requested and time.monotonic() < deadline:
        time.sleep(min(0.1, deadline - time.monotonic()))
        if time.monotonic() < next_check:
            continue
        next_check = time.monotonic() + INBOX_POLL_SECONDS
        if inbox_signature(worker.settings.inbox_dir) != baseline:
            return


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
