"""What repeated, ordinary use costs — and, separately, what it *grows*.

A scale benchmark asks "how long does one page take with a million files in it".
This asks a different question, and the two answers are independent:

    steady-state    what one repetition costs, forever
    drift           what one repetition leaves behind, forever

A component can be expensive and stable, and be fine. A component that costs
half a millisecond and writes one durable row per idle cycle is a soak failure:
it is invisible on any single measurement and it is the reason an appliance that
was pleasant in week one is annoying in week four.

So every workload here reports both, and the second one is the interesting
column. `slope` is per thousand operations, fitted across the whole run rather
than taken from the endpoints, because a metric that jumps once at warm-up and
is then flat has a first-to-last difference and no drift.

    .venv/bin/python scripts/soak.py --cycles 400
    .venv/bin/python scripts/soak.py --only dashboard --cycles 5000 --json-out soak.json

Nothing here touches a real library. It builds one synthetic installation in a
temporary directory, drives it, and deletes it.
"""

from __future__ import annotations

import argparse
import gc
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, "src")

from librairy.config import Settings  # noqa: E402
from librairy.db import connect, database_path  # noqa: E402

#  Durable state, table by table — and *every* table, read from the schema
#  rather than listed here. The first version of this carried a hand-written
#  list, and `transfer_runs` was on it under a name the schema does not use, so
#  `row_counts` swallowed the error and every backup soak reported "no row
#  growth" while meaning "I was not looking". A watch list that can silently
#  omit the thing being watched is worse than no watch list.
SKIP_TABLES = ("sqlite_",)


def watched_tables(conn: sqlite3.Connection) -> list[str]:
    return [
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        if not str(row[0]).startswith(SKIP_TABLES)
    ]


# --- measuring -----------------------------------------------------------------------


def rss_bytes() -> int:
    """Resident set size *now*, not the high-water mark.

    `resource.getrusage` reports a peak, and a peak cannot tell a leak from a
    single expensive warm-up: both look like a number that went up and stayed
    there. A soak needs the live figure, so each platform is asked in its own
    way and nothing is imported that is not already a dependency.
    """
    try:
        with open("/proc/self/statm", encoding="utf-8") as handle:
            return int(handle.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except OSError:
        pass
    try:
        out = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(os.getpid())],
            capture_output=True, text=True, check=False, timeout=10,
        )
        return int(out.stdout.strip() or 0) * 1024
    except (OSError, ValueError, subprocess.SubprocessError):  # pragma: no cover
        return 0


def open_files() -> int:
    """How many file descriptors this process holds.

    `/proc/self/fd` on Linux, `/dev/fd` on macOS — both are the process's own
    table, both are a directory listing, and neither needs `lsof`.
    """
    for where in ("/proc/self/fd", "/dev/fd"):
        try:
            return len(os.listdir(where))
        except OSError:
            continue
    return 0  # pragma: no cover - neither exists


def child_processes() -> int:
    try:
        out = subprocess.run(
            ["pgrep", "-P", str(os.getpid())],
            capture_output=True, text=True, check=False, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover
        return 0
    return len([line for line in out.stdout.splitlines() if line.strip()])


def temp_files(base: Path) -> int:
    """Everything under the installation's own scratch space, recursively."""
    total = 0
    for name in ("tmp", "optimization", "thumbs", "previews"):
        root = base / name
        if root.is_dir():
            total += sum(1 for path in root.rglob("*") if path.is_file())
    return total


@dataclass
class Sample:
    """Everything worth knowing at one instant of a soak."""

    op: int
    seconds: float
    rss: int
    fds: int
    children: int
    db_bytes: int
    wal_bytes: int
    temps: int
    rows: dict[str, int] = field(default_factory=dict)


def take(
    op: int, seconds: float, conn: sqlite3.Connection, settings: Settings
) -> Sample:
    db = database_path(settings)
    wal = db.with_name(f"{db.name}-wal")
    return Sample(
        op=op,
        seconds=seconds,
        rss=rss_bytes(),
        fds=open_files(),
        children=child_processes(),
        db_bytes=db.stat().st_size if db.exists() else 0,
        wal_bytes=wal.stat().st_size if wal.exists() else 0,
        temps=temp_files(settings.appdata_dir),
        rows=row_counts(conn),
    )


def row_counts(conn: sqlite3.Connection) -> dict[str, int]:
    #  No `except`: a table that cannot be counted is a fault in the harness,
    #  and swallowing it is how a soak comes to report silence as health.
    return {
        table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])  # noqa: S608
        for table in watched_tables(conn)
    }


def slope_per_1000(points: list[tuple[int, float]]) -> float:
    """Least squares, so one warm-up step is not mistaken for a trend.

    Endpoint arithmetic — last minus first — cannot tell "climbed once and
    settled" from "climbing steadily", and those are the two answers this whole
    script exists to distinguish.
    """
    count = len(points)
    if count < 2:
        return 0.0
    mean_x = sum(x for x, _ in points) / count
    mean_y = sum(y for _, y in points) / count
    spread = sum((x - mean_x) ** 2 for x, _ in points)
    if not spread:
        return 0.0
    covariance = sum((x - mean_x) * (y - mean_y) for x, y in points)
    return (covariance / spread) * 1000


def steady(values: list[float]) -> float:
    """The middle of the run, which is what "forever" looks like.

    The first samples are warm-up — an empty page cache, an unallocated WAL, a
    template compiled on first render — and averaging them in describes a moment
    the installation is only ever in once.
    """
    if not values:
        return 0.0
    ordered = sorted(values[len(values) // 4 :] or values)
    return ordered[len(ordered) // 2]


def report(name: str, samples: list[Sample]) -> dict[str, object]:
    if len(samples) < 2:
        return {"workload": name, "samples": len(samples)}
    first, last = samples[0], samples[-1]
    settled = samples[len(samples) // 4 :] or samples
    durations = [sample.seconds for sample in samples]
    thirds = max(1, len(durations) // 3)
    growth = {
        table: last.rows[table] - first.rows.get(table, 0)
        for table in last.rows
        if last.rows[table] != first.rows.get(table, 0)
    }
    return {
        "workload": name,
        "operations": last.op,
        "ms_steady": round(steady(durations) * 1000, 3),
        #  Three windows rather than two. A latency that rises and then falls
        #  back is a cache warming; one that rises across all three is the
        #  finding, and a before/after pair cannot tell them apart.
        "ms_early": round(sum(durations[:thirds]) / thirds * 1000, 3),
        "ms_middle": round(sum(durations[thirds : 2 * thirds]) / thirds * 1000, 3),
        "ms_late": round(sum(durations[-thirds:]) / thirds * 1000, 3),
        "rss_mb_steady": round(steady([s.rss for s in samples]) / 1024**2, 2),
        #  Fitted over everything *after* the first quarter. A workload that
        #  follows an expensive one starts with the previous one's pages still
        #  resident, and including that baseline reported a mixed soak as
        #  shrinking by 388 MB per thousand operations — an arithmetic fact
        #  about the sample before the workload, and a lie about the workload.
        "rss_mb_per_1000": round(
            slope_per_1000([(s.op, s.rss) for s in settled]) / 1024**2, 3
        ),
        #  The same slope over the second half only. A process that allocates
        #  its caches and then holds still has a whole-run slope and a late
        #  slope of nothing, and those are the two different answers a soak has
        #  to be able to give: "it grew once" and "it is still growing".
        "rss_mb_per_1000_late": round(
            slope_per_1000([(s.op, s.rss) for s in samples[len(samples) // 2 :]])
            / 1024**2,
            3,
        ),
        "fds_steady": round(steady([float(s.fds) for s in samples])),
        "fds_per_1000": round(slope_per_1000([(s.op, float(s.fds)) for s in samples]), 3),
        "children_max": max(s.children for s in samples),
        "db_kb_per_1000": round(
            slope_per_1000([(s.op, float(s.db_bytes)) for s in samples]) / 1024, 3
        ),
        "wal_kb_steady": round(steady([float(s.wal_bytes) for s in samples]) / 1024, 1),
        "wal_kb_final": round(last.wal_bytes / 1024, 1),
        "temps_final": last.temps,
        "row_growth": growth,
    }


# --- the installation ----------------------------------------------------------------


def build(base: Path, *, files: int) -> tuple[Settings, sqlite3.Connection]:
    settings = Settings(
        APPDATA_DIR=base / "appdata",
        INBOX_DIR=base / "inbox",
        LIBRARY_DIR=base / "library",
        QUARANTINE_DIR=base / "quarantine",
        FILE_STABILITY_SECONDS=0,
        AUTH_REQUIRED=False,
        OLLAMA_HOST="",
        _env_file=None,
    )
    for path in (
        settings.appdata_dir, settings.inbox_dir,
        settings.library_dir, settings.quarantine_dir,
    ):
        path.mkdir(parents=True, exist_ok=True)
    conn = connect(settings)
    from librairy.scanner import scan_root

    for index in range(files):
        folder = settings.library_dir / f"Documents/{index % 12:02d}"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"filed-{index:05d}.txt").write_text(f"body {index}", encoding="utf-8")
    scan_root(conn, "library", settings.library_dir, settings)
    return settings, conn


# --- the workloads -------------------------------------------------------------------
#
#  Each is a callable run over and over against an installation whose *inputs do
#  not change*. That is the whole design: anything that grows across repetitions
#  of unchanged work is growth with no cause, which is the definition of drift.


def workloads(settings: Settings, conn: sqlite3.Connection, base: Path) -> dict:
    from fastapi.testclient import TestClient

    from librairy import metrics
    from librairy.scanner import scan_root
    from librairy.web.app import create_app
    from librairy.worker import run_once

    #  Entered as a context manager, and that is not a detail. A bare
    #  `TestClient` starts a fresh event loop and portal for *every* request, and
    #  the first measurement taken with one showed the application leaking 4.6 MB
    #  per thousand dashboard polls — every byte of it asyncio machinery
    #  allocated by the harness. A soak instrument that drifts reports drift.
    client = TestClient(create_app(settings, conn))
    client.__enter__()

    def dashboard() -> None:
        client.get("/dashboard")

    def review() -> None:
        client.get("/review")

    def search() -> None:
        client.get("/search?q=filed")

    def browse() -> None:
        client.get("/browse")

    def health() -> None:
        client.get("/health")

    def worker() -> None:
        run_once(conn, settings)

    def scan() -> None:
        scan_root(conn, "library", settings.library_dir, settings)

    def rollup() -> None:
        metrics.rollup(conn)

    built = {
        "dashboard": dashboard,
        "review": review,
        "search": search,
        "browse": browse,
        "health": health,
        "worker": worker,
        "scan": scan,
        "rollup": rollup,
    }
    built.update(_transfer_workloads(settings, conn, base))
    built.update(_document_workloads(settings, conn, client))
    built["commit_undo"] = _commit_undo(settings, conn)
    return built


def _commit_undo(settings: Settings, conn: sqlite3.Connection):  # noqa: ANN202
    """File a handful of arrivals and put them back again, over and over.

    The one repeated workload that genuinely *should* leave durable state — a
    journal row per operation is the point of the journal — so the question here
    is not whether it grows but whether anything *else* does: a plan that is
    never finished, a proposal that is superseded and kept, an item row per
    round rather than per file.
    """
    from librairy.executor import execute_plan
    from librairy.history import undo_plan
    from librairy.planner import OperationSpec, approve_plan, create_plan
    from librairy.scanner import scan_root

    round_number = {"n": 0}

    def step() -> None:
        round_number["n"] += 1
        names = [f"arrival-{round_number['n']}-{index}.txt" for index in range(3)]
        for index, name in enumerate(names):
            (settings.inbox_dir / name).write_text(f"body {index}", encoding="utf-8")
        scan_root(conn, "inbox", settings.inbox_dir, settings)
        plan_id = create_plan(
            conn,
            [
                OperationSpec("move", name, "library", f"Documents/Filed/{name}")
                for name in names
            ],
            settings,
        )
        approve_plan(conn, plan_id, settings)
        execute_plan(conn, plan_id, settings)
        undo_plan(conn, plan_id, settings)
        for name in names:
            (settings.inbox_dir / name).unlink(missing_ok=True)
        scan_root(conn, "inbox", settings.inbox_dir, settings)

    return step


def _document_workloads(
    settings: Settings, conn: sqlite3.Connection, client
) -> dict:  # noqa: ANN001
    """Repeated work that shells out and writes temporary files.

    Poppler renders a page to an image, and that is the one path in the program
    where a repetition leaves something on disk by design. Whether it leaves
    *more* every time — a cache with no bound, a scratch file nobody removes, a
    child nobody reaps — is a question no in-process benchmark can answer, and
    it is the reason this workload runs the real binaries or does not run.
    """
    import shutil as _shutil
    import sys as _sys

    if _shutil.which("pdftoppm") is None or _shutil.which("pdftotext") is None:
        return {}
    _sys.path.insert(0, "tests")
    from support.documents import build_pdf

    from librairy.scanner import scan_root

    folder = settings.library_dir / "Documents/Manuals"
    folder.mkdir(parents=True, exist_ok=True)
    for index in range(8):
        (folder / f"manual-{index}.pdf").write_bytes(
            build_pdf(
                title=f"Manual {index}",
                author="Soak",
                lines=(f"page one of manual {index}", "installation and safety"),
                pages=2,
            )
        )
    scan_root(conn, "library", settings.library_dir, settings)
    pdfs = [
        int(row["id"])
        for row in conn.execute(
            "SELECT id FROM items WHERE relpath LIKE 'Documents/Manuals/%' ORDER BY id"
        )
    ]

    def thumbnails() -> None:
        for item_id in pdfs:
            client.get(f"/preview/items/{item_id}/thumb")

    def documents() -> None:
        from librairy.docmeta import facts_for_item

        for item_id in pdfs:
            row = conn.execute(
                "SELECT relpath FROM items WHERE id=?", (item_id,)
            ).fetchone()
            facts_for_item(
                conn, settings, item_id, settings.library_dir / str(row["relpath"])
            )

    return {"thumbnails": thumbnails, "documents": documents}


def _transfer_workloads(
    settings: Settings, conn: sqlite3.Connection, base: Path
) -> dict:
    """Repeated backup and mirror cycles, through real rclone.

    A subprocess per repetition is the one workload where "does it clean up
    after itself" is a question about something other than Python: a pipe left
    open, a child left unreaped, a temporary listing left on disk. None of it
    shows up in a stubbed run, which is why the rclone gate in M3 exists and why
    this uses the real binary or reports that it could not.
    """
    import shutil as _shutil

    from librairy import destinations, transfer_run
    from librairy.transfer_plan import Scope
    from librairy.worker import _listing_for

    if _shutil.which("rclone") is None:
        return {}
    target = base / "backup"
    target.mkdir(parents=True, exist_ok=True)
    backup_id = destinations.add_destination(
        conn, name="Soak backup", kind="local", target=str(target), modes=["backup"]
    )
    destinations.set_policy(
        conn, category="documents", destination_id=backup_id, mode="backup"
    )
    destination = destinations.destination(conn, backup_id)
    policy = next(
        policy for policy in destinations.policies(conn)
        if policy.destination_id == backup_id
    )

    backup_scope = Scope.of(policy)

    def backup() -> None:
        #  The listing is read from the destination every time, exactly as the
        #  worker reads it — passing `None` means "nobody could look", and a
        #  soak that measured *that* would be measuring a refusal.
        listing = _listing_for(conn, settings, destination, backup_scope)
        transfer_run.run_scope(conn, settings, backup_scope, destination, listing)

    mirror_dir = base / "mirror"
    mirror_dir.mkdir(parents=True, exist_ok=True)
    mirror_id = destinations.add_destination(
        conn, name="Soak mirror", kind="local", target=str(mirror_dir), modes=["mirror"]
    )
    destinations.set_policy(
        conn, category="documents", destination_id=mirror_id, mode="mirror"
    )
    mirror_destination = destinations.destination(conn, mirror_id)
    mirror_scope = Scope.folder("Documents", "mirror")

    def mirror() -> None:
        listing = _listing_for(conn, settings, mirror_destination, mirror_scope)
        transfer_run.run_scope(
            conn, settings, mirror_scope, mirror_destination, listing
        )

    return {"backup": backup, "mirror": mirror}


#  What a real installation is doing at any moment: a browser polling the
#  dashboard far more often than anything else, a worker cycle now and then, and
#  a person looking at a page occasionally. Isolated loops cannot show
#  contention between these, and contention is what a soak is for.
MIXED = (
    ("dashboard", 10),
    ("worker", 2),
    ("review", 1),
    ("search", 1),
    ("browse", 1),
    ("health", 1),
    ("scan", 1),
    ("backup", 1),
    ("mirror", 1),
    ("thumbnails", 1),
    ("documents", 1),
    ("commit_undo", 1),
)


def run(
    name: str,
    step,  # noqa: ANN001 - a zero-argument callable
    *,
    cycles: int,
    every: int,
    conn: sqlite3.Connection,
    settings: Settings,
) -> dict[str, object]:
    #  Every workload starts from a settled baseline. Without this, a run that
    #  follows an expensive one measures the *previous* workload's memory being
    #  released and reports it as this one shrinking by 350 MB per thousand
    #  operations — a slope with no relationship to anything it is named after.
    gc.collect()
    samples: list[Sample] = [take(0, 0.0, conn, settings)]
    for index in range(1, cycles + 1):
        started = time.perf_counter()
        step()
        elapsed = time.perf_counter() - started
        if index % every == 0 or index == cycles:
            samples.append(take(index, elapsed, conn, settings))
    return report(name, samples)


def mixed_step(available: dict):  # noqa: ANN001, ANN201
    """One pass of the weighted mixture, as a single repeated operation."""
    #  Whatever this installation can actually do. A machine without rclone or
    #  poppler still runs the mixture; it simply runs the part of it that exists,
    #  rather than failing and reporting nothing.
    plan = [
        available[name]
        for name, weight in MIXED
        if name in available
        for _ in range(weight)
    ]

    def step() -> None:
        for call in plan:
            call()

    return step


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Soak LibrAIry and report drift")
    parser.add_argument("--cycles", type=int, default=300)
    parser.add_argument("--files", type=int, default=400)
    parser.add_argument("--every", type=int, default=10)
    parser.add_argument("--only", action="append", default=[])
    parser.add_argument("--mixed", action="store_true")
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args(argv)

    #  The application logs every request at INFO, which for a soak is tens of
    #  thousands of lines and a measurable cost of its own. Silenced here rather
    #  than in the application: a real installation wants that log.
    logging.disable(logging.INFO)

    base = Path(tempfile.mkdtemp(prefix="librairy-soak-"))
    try:
        settings, conn = build(base, files=args.files)
        available = workloads(settings, conn, base)
        chosen = args.only or list(available)
        results = []
        if args.mixed:
            results.append(
                run(
                    "mixed",
                    mixed_step(available),
                    cycles=args.cycles,
                    every=max(1, args.every // 5),
                    conn=conn,
                    settings=settings,
                )
            )
        else:
            for name in chosen:
                results.append(
                    run(
                        name,
                        available[name],
                        cycles=args.cycles,
                        every=args.every,
                        conn=conn,
                        settings=settings,
                    )
                )
        payload = json.dumps(results, indent=2, sort_keys=True)
        if args.json_out:
            args.json_out.write_text(payload + "\n", encoding="utf-8")
        print(payload)
    finally:
        shutil.rmtree(base, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
