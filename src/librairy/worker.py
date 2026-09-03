from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import signal
import sqlite3
import time
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from librairy.ai.base import HealthResult
from librairy.backup import (
    BackupRunSummary,
    backup_due,
    record_backup_run,
    run_backup_once,
)
from librairy.classify import analyze_items
from librairy.config import Settings
from librairy.content.extract import process_content_extractions
from librairy.db import connect
from librairy.dedup import (
    detect_exact_duplicates,
    detect_similar_media,
    hash_size_colliding_library_files,
)
from librairy.duplicates import record_reports, record_similar_reports
from librairy.lifecycle import transition_item
from librairy.locks import acquire_lock
from librairy.models import EvidenceEntry
from librairy.planner import utc_now
from librairy.proposals import upsert_proposal
from librairy.quarantine import quarantine_operation
from librairy.resources import (
    BALANCED,
    PROCESSING_MODES,
    ProcessingMode,
    ai_mode,
    batch_limit,
    processing_mode,
)
from librairy.scanner import scan_root
from librairy.settings_service import effective_settings
from librairy.web.thumbs import prune_cache

LOGGER = logging.getLogger(__name__)

IDLE_SLEEP_SECONDS = 5.0
BUSY_SLEEP_SECONDS = 0.5
#  What the thumbnail cache is allowed to occupy. Each entry is a 320px JPEG,
#  so this is room for tens of thousands of them and still small beside any
#  library worth organising. Regenerating one costs a single ffmpeg call.
THUMBNAIL_CACHE_BYTES = 512 * 1024 * 1024
MAX_SLEEP_SECONDS = 60.0
# How often, while idle, to check whether the inbox changed under us.
INBOX_POLL_SECONDS = 2.0
#  When the providers were last asked whether they are answering, and the
#  default interval for asking. The interval a running worker actually uses
#  comes from the AI mode; this is what a caller with no mode gets, and it is
#  Normal's number. See `Worker._probe_due` and `librairy/resources.py`.
AI_PROBE_KEY = "ai_probe_at"
AI_PROBE_SECONDS = 60


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
    #  Files the pass declined to answer rather than guessing at. Reported
    #  beside `pending` and never folded into it: a pending file has an opinion
    #  and no destination, a held one has neither, on purpose. Not counted as
    #  work in its own right — `analyzed` already covers the cycle that held
    #  them, and they leave the queue until something changes.
    held: int = 0
    content_extracted: int = 0
    content_failed: int = 0
    backup_copied: int = 0
    backup_failed: int = 0
    #  Files that left quarantine without LibrAIry moving them — which is what
    #  emptying the delete queue looks like from here, and is not work.
    quarantine_vanished: int = 0
    #  Photographic companions established among arriving files. Not counted as
    #  work: it is a fact recorded *about* the inbox, and letting it hold the
    #  worker at a busy sleep interval would starve the library audit exactly
    #  the way `scanned` used to.
    companions_paired: int = 0

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

    @property
    def did_work(self) -> bool:
        """Whether this cycle actually *changed* anything.

        `work_found` includes `scanned`, and `scanned` counts every file the
        walk saw rather than the new ones — so it is non-zero for as long as
        the inbox contains anything at all. That is fine for deciding how long
        to sleep, and useless for deciding whether the worker is busy: on the
        live installation it meant "95" on every cycle, forever, which starved
        the library audit completely. Found by watching a queued run sit at
        `queued` while the worker cycled past it every two seconds.
        """
        return any(
            (
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
        #  How long `run_forever` waits between cycles, which is decided by the
        #  same mode the cycle itself ran under. Re-read every cycle, so
        #  switching to Quiet slows the loop down from the next cycle onwards
        #  without a restart.
        self.mode = PROCESSING_MODES[BALANCED]

    def request_stop(self, signum=None, frame=None) -> None:  # noqa: ARG002
        self.stop_requested = True

    def run_once(self) -> WorkerSummary:
        with acquire_lock(self.settings):
            settings = effective_settings(self.conn, self.settings)
            #  Read once for the cycle, so every workload in it is governed by
            #  the same answer. A mode changed halfway through a cycle takes
            #  effect on the next one, which is the only version of this that
            #  cannot half-apply. See `librairy/resources.py`.
            mode = processing_mode(self.conn)
            self.mode = mode
            # Before anything else, and on *every* cycle including busy ones.
            # Starting an optimization is gated behind an idle cycle; noticing
            # that one has finished is not, or a job that completed during a
            # busy hour would sit in `running` until the inbox went quiet — and
            # the encoder's own progress would stop advancing on screen while
            # the encoder was in fact still going.
            self._optimization_poll(settings)
            _set_worker_state(self.conn, "current_phase", "scan")
            scan = scan_root(self.conn, "inbox", settings.inbox_dir, settings)
            # Quarantine too, and for a reason the inbox does not have: the
            # delete queue is a folder LibrAIry explicitly asks people to empty
            # themselves. It is the one root guaranteed to change behind its
            # back, and until this ran, emptying it changed nothing LibrAIry
            # knew — the page went on listing files that were gone, offering to
            # restore them, and counting them in the queue you had just cleared.
            #
            # A walk, not a stat per row: readdir returns the whole directory in
            # one round trip, and unchanged files are matched on size and mtime
            # without being re-read.
            quarantine_scan = scan_root(
                self.conn, "quarantine", settings.quarantine_dir, settings
            )
            _set_worker_state(self.conn, "current_phase", "dedup")
            library_hashed = hash_size_colliding_library_files(
                self.conn, settings, limit=mode.hash_cap
            )
            candidates = detect_exact_duplicates(self.conn, settings)
            duplicate_candidates = _stage_quarantine_proposals(self.conn, candidates)
            similar_flags = detect_similar_media(self.conn, settings)
            # After czkawka, deliberately: the comparison reports what every
            # detector concluded, and czkawka's answer only exists once its
            # scan has run. Before it, every pair would read "nothing flagged".
            record_reports(self.conn, settings, candidates)
            # And the pairs only czkawka found: two encodes of one song, a
            # screenshot and its resize. Their bytes never match, so the exact
            # pass never sees them, and they are exactly the pairs where a
            # comparison is worth having.
            record_similar_reports(self.conn, settings)
            _set_worker_state(self.conn, "current_phase", "analyze")
            analysis = analyze_items(self.conn, settings, batch_limit(mode, settings.batch_size))
            _set_worker_state(self.conn, "current_phase", "content")
            content = process_content_extractions(
                self.conn, settings, batch_limit(mode, settings.batch_size)
            )
            #  After analysis, deliberately. Companion evidence is *supporting*
            #  evidence: an arriving JPEG whose metadata cannot be read must
            #  still be classified, proposed and reviewable, so nothing here is
            #  allowed to stand between a file and its Review row.
            #
            #  It runs on every cycle rather than only idle ones, because the
            #  thing it exists to fix is a camera card reaching Review before
            #  anybody knows which of its files are two halves of one picture —
            #  and that is exactly a busy cycle.
            _set_worker_state(self.conn, "current_phase", "companions")
            companions = self._inbox_companions(settings)
            # prune_cache was written with a byte budget and never called by
            # anything, so the thumbnail cache only ever grew: one JPEG per
            # image and per video ever previewed, kept forever on the same
            # volume as the index. Oldest-first, and it only ever deletes
            # files LibrAIry generated under appdata/thumbs.
            prune_cache(settings, THUMBNAIL_CACHE_BYTES)
            _set_worker_state(self.conn, "current_phase", "backup")
            # The schedule used to be stored and never read, so every cycle
            # drained the queue whatever the setting said.
            if backup_due(self.conn, settings):
                backup = run_backup_once(self.conn, settings, batch_size=settings.batch_size)
                record_backup_run(self.conn)
            else:
                backup = BackupRunSummary()
            summary = WorkerSummary(
                scanned=scan.discovered,
                hashed=scan.hashed,
                library_hashed=library_hashed,
                duplicate_candidates=duplicate_candidates,
                similar_flags=similar_flags,
                analyzed=analysis.analyzed,
                proposed=analysis.proposed,
                pending=analysis.pending,
                held=analysis.held,
                content_extracted=content.extracted,
                content_failed=content.failed,
                backup_copied=backup.copied,
                backup_failed=backup.failed,
                quarantine_vanished=quarantine_scan.missing,
                companions_paired=companions,
            )
            # Everything above is inbox work, and it has already happened.
            # A library audit is asked for, not needed, so it gets a bounded
            # slice of a cycle that changed nothing — a file dropped in the
            # inbox is never behind a library reconciliation, and the ordering
            # here is the whole guarantee.
            #
            # `did_work` and not `work_found`: the latter counts every file the
            # scan walked, so it is true for as long as the inbox is not empty
            # and the audit would never run at all.
            audit_stage = ""
            if not summary.did_work:
                _set_worker_state(self.conn, "current_phase", "audit")
                #  Quiet declines to *start* a slice. It never abandons one
                #  that is running: an audit run resumes from where it stopped,
                #  and the mode is about how much machine LibrAIry takes, not
                #  about throwing work away.
                audit_stage = self._audit_slice(settings) if mode.audits else ""
                _set_worker_state(self.conn, "audit_yielding", False)
                # Tier five, and last for a reason. Optimization is offered
                # only on a cycle that changed nothing *and* had no audit
                # slice to run — so an audit waiting for its turn always goes
                # first, and a busy inbox means nothing expensive begins.
                #
                # This only ever *starts* work. A running encode is not
                # suspended when a file lands in the inbox: stopping and
                # restarting an encoder repeatedly buys half-written output,
                # orphaned processes and corrupt staging, in exchange for
                # very little on a job that is separately constrained to a
                # small share of the machine.
                if not audit_stage:
                    #  Verifying the database reads every page of it, so it
                    #  belongs on an idle cycle and never on a page render.
                    #  Health reports what this last found. Same arrangement as
                    #  the FTS integrity check, for the same reason.
                    self._database_check(settings)
                    #  Off unless the owner turned it on, and on an idle cycle
                    #  either way: a decision nobody is being asked about is
                    #  not urgent. Nothing moves — it changes where a settled
                    #  decision waits, and Commit still has to be pressed.
                    self._settled_approvals(settings)
                    #  Ask the configured AI provider whether it is answering
                    #  again, and put back the files that were held because it
                    #  was not. Idle-cycle work like the rest of this tier: a
                    #  file already waiting is not made worse by waiting five
                    #  more seconds, and probing a dead endpoint must never
                    #  come between an arriving file and its Review row.
                    self._ai_recovery(settings)
                    #  Comparing the whole library against the whole index is
                    #  maintenance, not a render: Browse shows what this last
                    #  found and how old it is.
                    self._consistency_check(settings)
                    if mode.transcodes:
                        _set_worker_state(self.conn, "current_phase", "optimization")
                        self._optimization_slice(settings)
            else:
                # Recorded as a fact rather than inferred from a clock. The
                # progress panel needs to distinguish "waiting its turn" from
                # "stuck", and only this line knows which happened.
                _set_worker_state(self.conn, "audit_yielding", True)
            _set_worker_state(self.conn, "last_cycle_at", utc_now())
            _set_worker_state(self.conn, "current_phase", "idle")
            _set_worker_state(self.conn, "last_summary", asdict(summary))
            if audit_stage:
                _set_worker_state(self.conn, "last_audit_stage", audit_stage)
            return summary

    def _consistency_check(self, settings: Settings) -> None:
        """Compare the library against the index, and record what it finds.

        Wrapped like every other maintenance step. A comparison that fails must
        cost the worker nothing — Browse simply keeps showing the last one, with
        its age, which is exactly what it is for.
        """
        from librairy.consistency import library_consistency, record_consistency

        try:
            record_consistency(self.conn, library_consistency(self.conn, settings))
        except Exception:
            LOGGER.exception("library consistency check failed")

    def _settled_approvals(self, settings: Settings) -> None:
        """Move the settled decisions to Ready for Commit, if that is switched on.

        Wrapped like every other maintenance step: this is a convenience, and a
        convenience that breaks the worker would cost far more than it saves.
        """
        from librairy.settled_queue import approve_settled

        try:
            approve_settled(self.conn, settings)
        except Exception:
            LOGGER.exception("settled approvals failed")

    def _ai_recovery(self, settings: Settings) -> None:
        """Notice that the provider is back, and release what was waiting on it.

        Two steps, and the first is what keeps the second honest. `resume` only
        releases files whose provider has answered *since* they were held, and
        the only thing that writes that timestamp is a provider actually
        answering — so without a probe, an installation whose whole inbox is
        held would sit there forever with a perfectly healthy provider, because
        nothing was left to ask it.

        Nothing is probed when nothing is waiting on a probe. An installation
        with no held files never opens a socket here, and one whose held files
        are all `more-evidence` does not either — that reason is not about the
        provider and no amount of probing will move it.
        """
        from librairy import waiting

        try:
            mode = ai_mode(self.conn)
            #  Switched off means there is nothing to come back to. Held files
            #  stay held, visible and answerable by hand — which is what "Off"
            #  should mean — and no socket is opened to a provider the owner
            #  has said not to use.
            if mode.off or not waiting.awaiting_provider(self.conn):
                return
            if self._probe_due(mode.probe_seconds):
                probe_providers(self.conn, settings)
            released = waiting.resume_recovered(self.conn)
            if released:
                LOGGER.info("AI provider answered again; %d file(s) resumed", released)
        except Exception:
            LOGGER.exception("AI recovery check failed")

    def _probe_due(self, seconds: int = AI_PROBE_SECONDS) -> bool:
        """At most one round of health checks per interval, recorded not timed.

        The idle sleep backs off from five seconds to sixty, so "once a cycle"
        would mean twelve probes a minute on a quiet installation that has just
        gone quiet. Recorded in `worker_state` rather than held in memory so a
        restarted worker does not get a free round.

        The interval is the AI mode's: Limited asks every five minutes, Full
        Power every thirty seconds. It is the one number on that axis that is
        about the *worker* rather than about a call, which is why it lives with
        the rest of the mode rather than beside the constant below.
        """
        if seconds <= 0:
            return False
        now = utc_now()
        row = self.conn.execute(
            "SELECT value FROM worker_state WHERE key=?", (AI_PROBE_KEY,)
        ).fetchone()
        if row is not None:
            last = str(json.loads(row["value"]) or "")
            if last and _seconds_between(last, now) < seconds:
                return False
        _set_worker_state(self.conn, AI_PROBE_KEY, now)
        return True

    def _database_check(self, settings: Settings) -> None:
        """Record what a full verification finds. Never blocks the inbox.

        Wrapped like every other maintenance call: a check that cannot run must
        not be the thing that stops files being filed.
        """
        from librairy.web.health import check_database, record_database_health

        try:
            result, at = check_database(settings)
            record_database_health(self.conn, result, at)
        except Exception:  # noqa: BLE001 - maintenance must never break the worker
            LOGGER.exception("database check failed")

    def _optimization_poll(self, settings: Settings) -> str:
        """Advance a running encode by reading what it has reported. Never waits.

        Wrapped like every other maintenance call: the inbox is the job, and a
        broken encoder must not be the thing that stops files being filed.
        """
        from librairy import optimization_process as procs

        try:
            return procs.poll(self.conn, settings)
        except Exception:  # noqa: BLE001 - maintenance must never break the worker
            LOGGER.warning("optimization poll skipped", exc_info=True)
            return ""

    def _optimization_slice(self, settings: Settings) -> str:
        """Evaluate the next queued optimization, and record what it is waiting for.

        Wrapped for the same reason the audit slice is: the inbox is the job,
        and maintenance is the extra. A broken governor must never be the
        thing that stops files being filed.

        No encoder is reached from here yet. What this does today is decide
        and write down the answer, which is exactly the part worth proving
        before any CPU is spent.

        This module still moves no files, and `tests/test_worker.py` asserts
        it by reading this source. The invariant holds for maintenance work
        exactly as it does for everything else here.
        """
        from librairy import optimization_queue as queue

        try:
            job = queue.next_job(self.conn)
            if job is None:
                return ""
            state = queue.system_snapshot(self.conn, settings)
            state = _with_source_facts(self.conn, settings, job, state)
            # `Run now` writes `forced` into the job's run policy rather than
            # starting anything: a request handler cannot own a child process.
            # It lifts the clock here and nothing else — `decide` still applies
            # the fingerprint, protected roots, concurrency, the disk reserve
            # and the load ceiling, and the encoder still runs under the same
            # resource policy.
            forced = job["run_policy"] == queue.FORCED
            decision = queue.decide(
                job,
                state,
                run_policy=job["run_policy"],
                window=_window(self.conn, settings),
                forced=forced,
            )
            if decision.eligible:
                return self._launch(settings, job)
            if decision.reason == queue.SOURCE_CHANGED:
                queue.mark_stale(self.conn, job["id"])
            else:
                queue.set_waiting(self.conn, job["id"], decision.reason)
            return decision.reason
        except Exception:  # noqa: BLE001 - maintenance must never break the worker
            LOGGER.warning("optimization slice skipped", exc_info=True)
            return ""

    def _launch(self, settings: Settings, job) -> str:
        """Start the encoder and come straight back.

        The whole architecture in four lines: nothing here waits for the child.
        `run_once` returns, the next cycle files whatever landed in the inbox
        meanwhile, and `_optimization_poll` picks the encode up again.
        """
        from librairy import optimization_process as procs
        from librairy import optimization_queue as queue
        from librairy.optimization_exec import ExecutionRefused

        try:
            started = procs.launch(self.conn, settings, job)
        except ExecutionRefused as exc:
            # The advisor may still be right that this file is worth
            # converting. It is the conversion that cannot be done safely, and
            # saying so is more use than leaving it queued forever.
            queue.set_waiting(self.conn, job["id"], queue.UNSUPPORTED)
            self.conn.execute(
                "UPDATE optimization_jobs SET message=?, updated_at=? WHERE id=?",
                (str(exc), utc_now(), job["id"]),
            )
            return queue.UNSUPPORTED
        return "started" if started else "eligible"

    def _inbox_companions(self, settings: Settings) -> int:
        """Pair the arriving photographs, before anybody is asked to file them.

        The staged library audit already does this for filed files. Doing it
        only there had the order backwards: a camera card could reach Review
        with `IMG_1001.HEIC` and `IMG_1001.MOV` presented as two unrelated
        arrivals, and the pairing would appear afterwards — which is the one
        moment it is no longer useful.

        Same evidence function as the audit, different budget: one bounded
        exiftool batch per cycle over the inbox alone. Wrapped, because a
        missing binary or an unreadable file must never be the reason a file
        cannot be filed.
        """
        from librairy.photo_pairs import measure, pair

        try:
            measure(self.conn, settings, roots=("inbox",))
            return pair(self.conn, roots=("inbox",))
        except Exception:  # noqa: BLE001 - never let the extra break the job
            LOGGER.exception("inbox companion pairing failed")
            return 0

    def _audit_slice(self, settings: Settings) -> str:
        """One slice of a requested audit, if one is waiting.

        Wrapped because a broken audit must not stop the worker: the inbox is
        the job, and reconciliation is the extra.
        """
        from librairy.audit_job import advance

        try:
            return advance(self.conn, settings).stage
        except Exception:  # noqa: BLE001 - never let the extra break the job
            LOGGER.exception("audit slice failed")
            return ""

    def reconcile_optimizations(self) -> int:
        """Settle anything a previous worker left `running`. Never resumes it.

        A row claiming to be running with no worker behind it is the state that
        makes the queue page lie. Resuming is not the fix: nothing knows how
        much of a half-written output is valid, and spending another hour of
        CPU because a container was updated is not a decision to take on the
        user's behalf.
        """
        from librairy import optimization_process as procs

        try:
            settled = procs.reconcile(self.conn, self.settings)
        except Exception:  # noqa: BLE001 - never let it stop the worker starting
            LOGGER.warning("optimization reconcile skipped", exc_info=True)
            return 0
        if settled:
            LOGGER.warning("%s interrupted optimization job(s) settled", settled)
        return settled

    def run_forever(self) -> None:
        self.reconcile_optimizations()
        sleep_seconds = BUSY_SLEEP_SECONDS
        while not self.stop_requested:
            summary = self.run_once()
            #  `self.mode` is what the cycle that just ran was governed by, so
            #  a mode changed while the worker was busy takes effect on the
            #  pause after it rather than a cycle later.
            sleep_seconds = next_sleep(sleep_seconds, summary.work_found, self.mode)
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



def probe_providers(conn: sqlite3.Connection, settings: Settings) -> int:
    """Health-check every enabled provider and record what it said.

    A health check, not a classification: `GET /api/tags` and its equivalents,
    which cost a provider that is running almost nothing and a provider that is
    not running one refused connection. The point is the timestamp — a provider
    that answers writes `last_ok_at`, and that is the whole signal held files
    resume on.

    Failures are recorded rather than raised. A provider being down is the
    normal case here; it is why this runs at all.
    """
    from librairy.ai.orchestrator import provider_for_config
    from librairy.ai.registry import provider_chain
    from librairy.ai.status import upsert_provider_status

    answered = 0
    for config in provider_chain(conn, settings, record=False):
        try:
            provider = provider_for_config(config, settings)
            health = provider.health(settings.ai_timeout)
        except (OSError, RuntimeError, ValueError) as exc:
            upsert_provider_status(conn, config, HealthResult(False, error=exc.__class__.__name__))
            continue
        upsert_provider_status(conn, config, health)
        answered += int(health.ok)
    return answered


def _seconds_between(earlier: str, later: str) -> float:
    """How far apart two recorded timestamps are, or forever if one is unusable.

    A malformed row must mean "probe now", never a crash: this decides whether
    to open a socket, and a stored value nobody can parse is not a reason to
    stop checking whether the provider is back.
    """
    try:
        return (
            datetime.fromisoformat(later) - datetime.fromisoformat(earlier)
        ).total_seconds()
    except ValueError:
        return float("inf")


def next_sleep(previous: float, work_found: bool, mode: ProcessingMode | None = None) -> float:
    """How long to wait before the next cycle, under one processing mode.

    The mode defaults to Balanced, whose three numbers are the constants this
    function used to read directly — so a caller with no opinion gets exactly
    the previous behaviour, and the constants stay as the documented meaning of
    "normal".
    """
    mode = mode or PROCESSING_MODES[BALANCED]
    if work_found:
        return mode.busy_sleep
    return min(max(previous * 2, mode.idle_sleep), mode.max_sleep)


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


# States where the file is still in the queue at all.
#  'waiting' too: a file held because nothing could identify it is still
#  undecided, and an exact duplicate is a better answer than the one the
#  provider was going to give. Staging it does not need the provider to come
#  back first, and leaving it out meant a held file could sit there while its
#  twin was already in the library.
STAGEABLE = frozenset({"discovered", "proposed", "pending", "waiting"})
# ...but the item state cannot tell "analysis found nothing" from "the owner
# said no" — both land in 'pending'. The proposal status can, so it is what
# actually decides. Re-staging a rejected duplicate would argue with an answer
# already given, once a cycle, forever.
DECIDED = ("approved", "rejected", "postponed", "committed")


def _owner_has_decided(conn: sqlite3.Connection, item_id: int) -> bool:
    placeholders = ",".join("?" for _ in DECIDED)
    row = conn.execute(
        f"""
        SELECT 1 FROM proposals
        WHERE item_id = ? AND status != 'superseded' AND status IN ({placeholders})
        """,  # noqa: S608 - placeholders are generated from a module constant
        (item_id, *DECIDED),
    ).fetchone()
    return row is not None


def _stage_quarantine_proposals(conn: sqlite3.Connection, candidates) -> int:
    staged = 0
    for candidate in candidates:
        # 'proposed' as well as 'discovered': the duplicate check runs before
        # analysis, so a file whose twin only turned up on a later cycle -- or
        # while the rmlint cross-check was broken -- was already classified and
        # could never be staged. Undecided either way; nothing the owner has
        # answered is touched.
        if candidate.status != "confirmed" or candidate.duplicate.state not in STAGEABLE:
            continue
        if _owner_has_decided(conn, candidate.duplicate.id):
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


def _with_source_facts(conn, settings, job, state):
    """Fill in what the decision needs to know about this job's source file.

    Read here rather than inside `decide` so the decision stays a pure
    function of its inputs — every branch of it is then testable by building a
    `SystemState` rather than by arranging a filesystem.
    """
    from librairy import optimization_queue as queue
    from librairy.protected import protected_roots, protecting_root

    row = conn.execute(
        "SELECT fingerprint FROM items WHERE root=? AND relpath=? AND missing_since IS NULL",
        (job["root"], job["relpath"]),
    ).fetchone()
    return queue.SystemState(
        now=state.now,
        higher_priority_work=state.higher_priority_work,
        running_jobs=state.running_jobs,
        free_bytes=state.free_bytes,
        load_per_cpu=state.load_per_cpu,
        current_fingerprint=(row["fingerprint"] or "") if row else "",
        protected_by=protecting_root(job["relpath"], protected_roots(conn)),
    )


def _window(conn, settings) -> tuple[str, str]:
    """The configured maintenance window, defaulting to the small hours.

    01:00–06:00 is the default and it is documented as one: a NAS is usually
    idle then, and somebody who wants otherwise can say so. It decides *when*
    a job may begin and nothing else — the resource policy applies at two in
    the morning exactly as it does at two in the afternoon.
    """
    import json

    row = conn.execute(
        "SELECT value FROM settings WHERE key='optimization.window'"
    ).fetchone()
    if row is None:
        return ("01:00", "06:00")
    try:
        start, end = json.loads(row["value"])
    except (TypeError, ValueError):
        return ("01:00", "06:00")
    return (str(start), str(end))
