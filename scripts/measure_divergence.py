"""Measure the straightforward current-divergence table before believing in it.

`divergence.py` stores one row per file that is only at a destination, with no
cap, because a count with a thousand-path sample cannot be paged, searched or
worked through. That is the right *semantics*; whether it is a reasonable
*implementation* is a measurement, and this is the measurement.

    .venv/bin/python scripts/measure_divergence.py --rows 100000 300000 1000000

Six numbers per population, and each one is a question somebody will ask:

    first write        the initial scan of a destination nobody has compared
    rewrite            the hourly one, where nothing has changed
    reconcile          the one where a percent of them were removed by hand
    count              "how many are only there?"  — the status line
    first page         "show me"                   — page one
    deep page          "show me"                   — page eight thousand

Deep paging is measured twice, by cursor and by OFFSET, because those are the
two designs and only one of them stays flat. The cursor is the primary key, so
its cost does not depend on how far in the page is; an OFFSET has to walk every
row it is going to discard.

**This measures. It does not fix.** If the simple table is unreasonable at a
million rows, the answer is a bounded alternative chosen *afterwards* — a
stored manifest catalogue — and not one invented in advance of the number. See
`docs/ROADMAP.md`, M3-03.
"""

from __future__ import annotations

import argparse
import json
import resource
import shutil
import sys
import tempfile
import time
import tracemalloc
from collections.abc import Iterator
from pathlib import Path

from librairy import destinations, divergence
from librairy.config import Settings
from librairy.db import connect, database_path
from librairy.transfer_plan import DestinationFile, Entry

#  A percentage of the set removed between two comparisons — somebody tidied a
#  folder on the destination by hand. This is the case where reconciliation
#  actually deletes, which is the expensive branch.
REMOVED = 0.01

#  How far into the set the "deep page" sits. Near the end, because that is
#  where OFFSET is at its worst and a cursor is not.
DEEP = 0.8


def settings_for(base: Path) -> Settings:
    settings = Settings(
        APPDATA_DIR=base / "appdata",
        INBOX_DIR=base / "inbox",
        LIBRARY_DIR=base / "library",
        QUARANTINE_DIR=base / "quarantine",
        AUTH_REQUIRED=False,
        _env_file=None,
    )
    for directory in (
        settings.appdata_dir,
        settings.inbox_dir,
        settings.library_dir,
        settings.quarantine_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    return settings


def entries(count: int, *, skip: int = 0) -> Iterator[Entry]:
    """The destination-only files, yielded rather than listed.

    Paths that look like a real photo library gone stale: a folder per month,
    a few hundred files in each. `skip` drops every nth one, which is how a
    comparison that finds fewer than last time is simulated.
    """
    for index in range(count):
        if skip and index % skip == 0:
            continue
        yield Entry(
            relpath=f"Photos/{2015 + index // 8000}/{index // 400:04d}/IMG_{index:07d}.jpg",
            difference=destinations.EXTRA,
            action=destinations.REPORT,
            destination_size=2_400_000 + index,
        )


def timed(call) -> tuple[float, object]:  # noqa: ANN001
    started = time.perf_counter()
    value = call()
    return (time.perf_counter() - started) * 1000, value


def measure(rows: int, base: Path) -> dict[str, object]:
    settings = settings_for(base)
    conn = connect(settings)
    destination_id = destinations.add_destination(
        conn,
        name="Studio",
        kind=destinations.LOCAL,
        target=str(base / "drive"),
        modes=[destinations.MIRROR],
    )

    def record(skip: int = 0) -> None:
        divergence.record(
            conn,
            destination_id=destination_id,
            category="photos",
            entries=entries(rows, skip=skip),
            complete=True,
        )
        conn.commit()

    first_ms, _ = timed(record)
    rewrite_ms, _ = timed(record)
    #  A hundredth of them removed by hand: every survivor is an upsert and the
    #  rest are one DELETE.
    reconcile_ms, _ = timed(lambda: record(skip=int(1 / REMOVED)))

    count_ms, found = timed(lambda: divergence.summary(conn, destination_id))
    page_ms, first_page = timed(lambda: divergence.page(conn, destination_id))

    deep_cursor = conn.execute(
        "SELECT relpath FROM backup_divergence WHERE destination_id=?"
        " ORDER BY relpath LIMIT 1 OFFSET ?",
        (destination_id, int(found.count * DEEP)),
    ).fetchone()[0]
    keyset_ms, deep = timed(
        lambda: divergence.page(conn, destination_id, after=deep_cursor)
    )
    offset_ms, _ = timed(
        lambda: conn.execute(
            "SELECT relpath, size, first_seen_at, last_seen_at, category"
            " FROM backup_divergence WHERE destination_id=?"
            " ORDER BY relpath LIMIT ? OFFSET ?",
            (destination_id, divergence.PAGE, int(found.count * DEEP)),
        ).fetchall()
    )

    size_mb = round(database_path(settings).stat().st_size / 1_000_000, 1)
    conn.close()
    return {
        "rows": found.count,
        "first_write_ms": round(first_ms),
        "rewrite_ms": round(rewrite_ms),
        "reconcile_ms": round(reconcile_ms),
        "count_ms": round(count_ms, 1),
        "first_page_ms": round(page_ms, 1),
        "deep_page_keyset_ms": round(keyset_ms, 1),
        "deep_page_offset_ms": round(offset_ms, 1),
        "db_mb": size_mb,
        "peak_rss_mb": round(
            resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            / (1_000_000 if sys.platform == "darwin" else 1_000)
        ),
        #  Proof the numbers above describe a set anybody can actually walk.
        "page_is_bounded": len(first_page) == divergence.PAGE and first_page.more,
        "deep_page_reachable": len(deep) > 0,
    }


def listing_memory(rows: int, base: Path) -> dict[str, object]:
    """What a destination listing costs to hold, and what comparing it adds.

    The open question from increment 5: the library side streams, so the
    destination listing is the remaining memory-bound half. `transfer_listing`
    has to hold it — a directory walk and `rclone lsjson` both produce the whole
    thing — so the number worth knowing is what that costs, and whether
    comparing it doubles the bill.

    `compare` builds a dictionary of the destination *and* a set of every
    library path. `destination_only` merges two sorted streams and builds
    neither. Both are measured, because the difference is the thing.
    """
    from librairy.transfer_plan import compare, destination_only

    settings = settings_for(base)
    conn = connect(settings)
    conn.executemany(
        "INSERT INTO items(root, relpath, size, mtime_ns, state, first_seen_at,"
        " last_seen_at) VALUES ('library', ?, 2400000, 0, 'committed', ?, ?)",
        [(entry.relpath, "now", "now") for entry in entries(rows)],
    )
    policy = destinations.Policy(
        id=1, category="photos", destination_id=1, mode=destinations.MIRROR, enabled=True
    )

    tracemalloc.start()
    listing = [
        DestinationFile(entry.relpath, entry.destination_size) for entry in entries(rows)
    ]
    held = tracemalloc.get_traced_memory()[1]

    tracemalloc.reset_peak()
    #  Consumed and discarded: the question is the high-water mark, not the
    #  result. Nothing divergent here, so both walk the whole of both sides.
    sum(1 for _ in destination_only(conn, policy, listing))
    merged = tracemalloc.get_traced_memory()[1] - held

    tracemalloc.reset_peak()
    compare(
        __import__("librairy.transfer_plan", fromlist=["library_files"]).library_files(
            conn, "photos"
        ),
        listing,
        destinations.MIRROR,
    )
    compared = tracemalloc.get_traced_memory()[1] - held
    tracemalloc.stop()
    conn.close()
    return {
        "rows": rows,
        "listing_mb": round(held / 1_000_000, 1),
        "merge_extra_mb": round(merged / 1_000_000, 1),
        "compare_extra_mb": round(compared / 1_000_000, 1),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rows", type=int, nargs="+", default=[100_000, 300_000, 1_000_000])
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--listing",
        action="store_true",
        help="also measure what holding a destination listing costs",
    )
    args = parser.parse_args(argv)

    if args.listing:
        return _listing_report(args.rows)

    results = []
    for rows in args.rows:
        base = Path(tempfile.mkdtemp(prefix="divergence-bench-"))
        try:
            results.append(measure(rows, base))
        finally:
            shutil.rmtree(base, ignore_errors=True)

    if args.json:
        print(json.dumps(results, indent=2))
        return 0
    header = (
        f"{'rows':>10}  {'write':>7}  {'rewrite':>8}  {'reconcile':>10}"
        f"  {'count':>7}  {'page 1':>7}  {'deep/key':>9}  {'deep/off':>9}  {'db':>7}"
    )
    print(header)
    print("-" * len(header))
    for result in results:
        print(
            f"{result['rows']:>10,}  {result['first_write_ms']:>6}ms"
            f"  {result['rewrite_ms']:>7}ms  {result['reconcile_ms']:>9}ms"
            f"  {result['count_ms']:>6}ms  {result['first_page_ms']:>6}ms"
            f"  {result['deep_page_keyset_ms']:>8}ms  {result['deep_page_offset_ms']:>8}ms"
            f"  {result['db_mb']:>5}MB"
        )
    print(f"\npeak RSS {results[-1]['peak_rss_mb']} MB (high-water for the whole run)")
    return 0


def _listing_report(populations: list[int]) -> int:
    header = f"{'rows':>10}  {'listing held':>13}  {'merge adds':>11}  {'compare adds':>13}"
    print(header)
    print("-" * len(header))
    for rows in populations:
        base = Path(tempfile.mkdtemp(prefix="divergence-listing-"))
        try:
            result = listing_memory(rows, base)
        finally:
            shutil.rmtree(base, ignore_errors=True)
        print(
            f"{result['rows']:>10,}  {result['listing_mb']:>11}MB"
            f"  {result['merge_extra_mb']:>9}MB  {result['compare_extra_mb']:>11}MB"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
