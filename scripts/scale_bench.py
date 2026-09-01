"""Measure the current program at populations it has never been measured at.

`perf_smoke.py` answers a different question: it generates real files and walks
the whole pipeline, which is the right shape for "does this work end to end"
and the wrong shape for a million rows. Creating a million files to find out
whether a `SELECT` is bounded measures the filesystem, takes hours, and proves
nothing about the query.

So this synthesizes **database populations** instead. Every row it writes is a
row the real queries actually match — same tables, same states, same
constraints — and every surface is then measured through the same data function
the web route calls, with the statements counted.

    .venv/bin/python scripts/scale_bench.py --library 100000 --inbox 5000

Two numbers per surface, and the second one matters more:

    ms       how long the page's data took
    queries  how many statements it took

A slow query is a bad afternoon. A query *per row* is an architecture that
stops working, and it looks perfectly healthy until the table is large. Only
the count can tell you which one you have.

**This measures. It does not fix.** A bottleneck found here is a result, not a
work order: see `docs/ROADMAP.md`, M1-01.
"""

from __future__ import annotations

import argparse
import json
import random
import resource
import shutil
import sqlite3
import sys
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from librairy.config import Settings
from librairy.db import connect, database_path

NOW = "2026-09-01T12:00:00+00:00"
PAGE = 50
BATCH = 20_000

#  Roughly a personal library that has been fed cameras and CD rips for years.
#  The shares matter because the surfaces behave differently per category —
#  photographs group into large events, music into small albums, documents
#  hardly at all — and a uniform mix would flatter the grouping code.
MIX = (
    ("photos", 0.55, "Photos"),
    ("music", 0.25, "Music"),
    ("documents", 0.10, "Documents"),
    ("movies", 0.04, "Movies"),
    ("shows", 0.03, "Shows"),
    ("books", 0.02, "Books"),
    ("misc", 0.01, "Misc"),
)

#  How many files arrive as one decision, by category. A camera card is one
#  event of a few hundred; an album is a dozen; a scanned invoice is on its
#  own. These are the shapes that decide how much work Review actually is.
GROUP_SHAPE = {
    "photos": ("photo_event", 150),
    "music": ("album", 12),
    "shows": ("season", 10),
    "movies": (None, 1),
    "documents": (None, 1),
    "books": (None, 1),
    "misc": (None, 1),
}


@dataclass
class Population:
    library: int
    inbox: int
    findings: int
    quarantine: int
    history: int
    groups: int = 0
    proposals: int = 0
    build_seconds: float = 0.0
    db_bytes: int = 0


@dataclass
class Measurement:
    surface: str
    ms: float
    queries: int
    detail: str = ""
    status: str = "ok"


@dataclass
class Result:
    population: Population
    measurements: list[Measurement] = field(default_factory=list)
    decisions: dict[str, int] = field(default_factory=dict)
    throughput: dict[str, float] = field(default_factory=dict)
    peak_rss_mb: int = 0


class Counting:
    """A connection that remembers how many statements it was asked to run.

    Same instrument as `tests/test_scale.py`, kept separate rather than
    imported: a benchmark that depends on the test suite cannot be run against
    an older checkout to compare.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.queries: list[str] = []

    def execute(self, sql, *args, **kwargs):  # noqa: ANN001, ANN201
        self.queries.append(" ".join(str(sql).split())[:120])
        return self._conn.execute(sql, *args, **kwargs)

    def executemany(self, sql, *args, **kwargs):  # noqa: ANN001, ANN201
        self.queries.append("MANY " + " ".join(str(sql).split())[:115])
        return self._conn.executemany(sql, *args, **kwargs)

    def __getattr__(self, name):  # noqa: ANN001, ANN204
        return getattr(self._conn, name)


# --- building -----------------------------------------------------------------


def settings_for(base: Path) -> Settings:
    settings = Settings(
        APPDATA_DIR=base / "appdata",
        INBOX_DIR=base / "inbox",
        LIBRARY_DIR=base / "library",
        QUARANTINE_DIR=base / "quarantine",
        FILE_STABILITY_SECONDS=0,
        AUTH_REQUIRED=False,
        _env_file=None,
    )
    for root in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        root.mkdir(parents=True, exist_ok=True)
    return settings


def _chunks(rows: Iterator[tuple], size: int = BATCH) -> Iterator[list[tuple]]:
    batch: list[tuple] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def _insert(conn: sqlite3.Connection, sql: str, rows: Iterator[tuple]) -> int:
    total = 0
    for batch in _chunks(rows):
        conn.executemany(sql, batch)
        total += len(batch)
    conn.commit()
    return total


def _category_for(index: int) -> tuple[str, str]:
    """Deterministic, and spread rather than banded.

    `index % 1000` looked fine and was not: consecutive ids walk the bands in
    order, so any population smaller than a thousand is entirely the first
    category. A four-hundred-file inbox came out 100% photographs, three groups
    formed instead of thirty, and the decision-scale number it produced was
    fiction. 613 is coprime with 1000, so this still visits every band exactly
    as often — just not in order.
    """
    position = ((index * 613) % 1000) / 1000
    running = 0.0
    for name, share, top in MIX:
        running += share
        if position < running:
            return name, top
    return MIX[-1][0], MIX[-1][2]


def synthesize(
    conn: sqlite3.Connection,
    *,
    library: int,
    inbox: int,
    findings: int,
    quarantine: int,
    history: int,
    seed: int = 7,
) -> Population:
    """Write a population the real queries will match.

    Deliberately not a uniform table of identical rows: states, categories,
    group shapes and statuses are all spread, because every surface measured
    below filters on one of them and a single-valued column is an index that
    never has to work.
    """
    started = time.perf_counter()
    rng = random.Random(seed)

    def library_rows() -> Iterator[tuple]:
        for n in range(1, library + 1):
            category, top = _category_for(n)
            folder = f"{top}/{category.title()} {n // 500:04d}"
            yield (
                n, "library", f"{folder}/file-{n:07d}.dat", 1024 + (n % 4096), n,
                f"fp{n:012d}", "committed", NOW, NOW,
            )

    written_library = _insert(
        conn,
        "INSERT INTO items(id, root, relpath, size, mtime_ns, fingerprint, state,"
        " first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?,?)",
        library_rows(),
    )

    # --- groups, then the inbox that belongs to them --------------------------
    #  Built first so a proposal can point at one. Arrivals of the same
    #  category gather into runs of the shape that category actually arrives
    #  in — a camera card, a rip, a season — and everything else stays loose.
    group_specs: list[tuple[int, str, str, str | None]] = []
    keys: dict[tuple[str, int], int] = {}
    seen: dict[str, int] = {}
    resolved: list[int | None] = []
    for n in range(1, inbox + 1):
        category, top = _category_for(n)
        kind, size = GROUP_SHAPE[category]
        if kind is None or size <= 1:
            resolved.append(None)
            continue
        position = seen.get(category, 0)
        seen[category] = position + 1
        key = (category, position // size)
        group_id = keys.get(key)
        if group_id is None:
            group_id = len(keys) + 1
            keys[key] = group_id
            group_specs.append(
                (group_id, kind, f"{category.title()} import {group_id:05d}", f"{top}/Filed")
            )
        resolved.append(group_id)

    _insert(
        conn,
        "INSERT INTO groups(id, kind, label, dest_base, created_at) VALUES (?,?,?,?,'" + NOW + "')",
        iter(group_specs),
    )

    def inbox_rows() -> Iterator[tuple]:
        for n in range(1, inbox + 1):
            yield (
                library + n, "inbox", f"drop-{n // 400:03d}/arrival-{n:07d}.dat",
                2048 + (n % 999), n, f"nfp{n:012d}", "proposed", NOW, NOW,
            )

    _insert(
        conn,
        "INSERT INTO items(id, root, relpath, size, mtime_ns, fingerprint, state,"
        " first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?,?)",
        inbox_rows(),
    )

    def proposal_rows() -> Iterator[tuple]:
        for n in range(1, inbox + 1):
            category, top = _category_for(n)
            #  Confidence spread across the three tiers Review already draws:
            #  most arrivals are confident, a real minority is not.
            confidence = rng.choice([0.95, 0.92, 0.88, 0.86, 0.74, 0.68, 0.55, 0.35])
            yield (
                n, library + n, category, f"file-{n:07d}.dat",
                f"{top}/Filed/file-{n:07d}.dat", confidence, resolved[n - 1],
                "proposed", "[]", NOW, NOW, "move", "library",
            )

    written_proposals = _insert(
        conn,
        "INSERT INTO proposals(id, item_id, category, clean_name, dest_relpath,"
        " confidence, group_id, status, evidence, created_at, updated_at, action,"
        " dest_root) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        proposal_rows(),
    )

    # --- findings, quarantine, history ---------------------------------------
    def finding_rows() -> Iterator[tuple]:
        kinds = ("loose-track", "artist-split", "missing-cover", "stray-file", "bad-name")
        statuses = ("open", "open", "open", "kept", "planned")
        for n in range(1, findings + 1):
            category, top = _category_for(n)
            yield (
                n, None, "library", f"{top}/Audit {n // 300:04d}/finding-{n:07d}.dat",
                kinds[n % len(kinds)], "medium", f"finding {n}", "library",
                f"{top}/Fixed/finding-{n:07d}.dat", "[]", f"afp{n:012d}",
                statuses[n % len(statuses)], NOW, NOW,
            )

    _insert(
        conn,
        "INSERT INTO audit_findings(id, item_id, root, relpath, kind, severity,"
        " summary, dest_root, dest_relpath, evidence, fingerprint, status,"
        " detected_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        finding_rows(),
    )

    def quarantine_item_rows() -> Iterator[tuple]:
        base = library + inbox
        for n in range(1, quarantine + 1):
            yield (
                base + n, "quarantine", f"2026-09-01/held-{n:07d}.dat", 4096, n,
                f"qfp{n:012d}", "discovered", NOW, NOW,
            )

    _insert(
        conn,
        "INSERT INTO items(id, root, relpath, size, mtime_ns, fingerprint, state,"
        " first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?,?)",
        quarantine_item_rows(),
    )

    def quarantine_rows() -> Iterator[tuple]:
        base = library + inbox
        reasons = ("exact_duplicate", "similar_media", "user")
        for n in range(1, quarantine + 1):
            yield (
                n, base + n, reasons[n % 3], "library",
                f"Music/Held {n // 400:04d}/held-{n:07d}.dat", NOW,
            )

    _insert(
        conn,
        "INSERT INTO quarantine_entries(id, item_id, reason, original_root,"
        " original_relpath, quarantined_at) VALUES (?,?,?,?,?,?)",
        quarantine_rows(),
    )

    def history_rows() -> Iterator[tuple]:
        for n in range(1, history + 1):
            category, top = _category_for(n)
            yield (
                n, NOW, f"plan-{n // 40:07d}", n, "move", "inbox",
                f"drop/old-{n:07d}.dat", "library", f"{top}/Filed/old-{n:07d}.dat",
                f"hfp{n:012d}", "ok",
            )

    _insert(
        conn,
        "INSERT INTO history(id, ts, plan_id, op_id, action, src_root, src_relpath,"
        " dest_root, dest_relpath, fingerprint, outcome) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        history_rows(),
    )

    # --- an approved commit queue --------------------------------------------
    #  A tenth of the inbox, approved and waiting, which is what Commit reads.
    approved = max(1, inbox // 10)
    _insert(
        conn,
        "INSERT INTO plans(id, status, plan_hash, created_at, approved_at)"
        " VALUES (?, 'approved', ?, ?, ?)",
        ((f"plan-a-{n:07d}", f"hash{n}", NOW, NOW) for n in range(1, approved + 1)),
    )
    _insert(
        conn,
        "INSERT INTO plan_ops(plan_id, seq, op_type, item_id, src_root, src_relpath,"
        " src_fingerprint, dest_root, dest_relpath) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            (
                f"plan-a-{n:07d}", 1, "move", library + n, "inbox",
                f"drop-{n // 400:03d}/arrival-{n:07d}.dat", f"nfp{n:012d}", "library",
                f"Music/Filed/file-{n:07d}.dat",
            )
            for n in range(1, approved + 1)
        ),
    )
    conn.execute(
        "UPDATE proposals SET status='approved' WHERE id <= ?", (approved,)
    )
    conn.commit()

    # --- the search index ----------------------------------------------------
    #  Written directly rather than through `rebuild_search_index`, which is one
    #  statement per item and is measured separately as throughput.
    def fts_rows() -> Iterator[tuple]:
        for n in range(1, library + 1):
            category, top = _category_for(n)
            yield (
                n, f"{top} file {n}", f"file-{n:07d}", f"tag{n % 97}",
                f"Artist {n % 5000}", f"Album {n % 20000}", f"Title {n}",
                f"Show {n % 400}", f"Genre {n % 40}", f"Event {n % 3000}",
                f"identity-{n}", category, "library", n,
            )

    _insert(
        conn,
        "INSERT INTO search_fts(rowid, name, clean_name, tags, artist, album, title,"
        " show, genre, event, identity, category, root, item_id)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        fts_rows(),
    )
    conn.execute("ANALYZE")
    conn.commit()

    return Population(
        library=written_library,
        inbox=inbox,
        findings=findings,
        quarantine=quarantine,
        history=history,
        groups=len(group_specs),
        proposals=written_proposals,
        build_seconds=round(time.perf_counter() - started, 2),
    )


# --- measuring ----------------------------------------------------------------


class Budget:
    """Stop a surface that is not going to finish, and say so.

    Without this the harness cannot report on a population where one page is
    unusable: it simply never returns, and "we could not measure it" is a much
    weaker finding than "it did not finish in sixty seconds". SQLite's progress
    handler is checked inside the query itself, so this interrupts a scan
    mid-flight rather than waiting politely for it to end.
    """

    def __init__(self, conn: sqlite3.Connection, seconds: float) -> None:
        self._conn = conn
        self._seconds = seconds
        self.deadline = 0.0

    def __enter__(self) -> Budget:
        self.deadline = time.monotonic() + self._seconds
        self._conn.set_progress_handler(self._check, 10_000)
        return self

    def __exit__(self, *exc: object) -> None:
        self._conn.set_progress_handler(None, 0)

    def _check(self) -> int:
        return 1 if time.monotonic() > self.deadline else 0


def _time(counting: Counting, call, *, budget: float) -> tuple[float, int, str]:  # noqa: ANN001
    before = len(counting.queries)
    started = time.perf_counter()
    status = "ok"
    with Budget(counting._conn, budget):  # noqa: SLF001
        try:
            call()
        except sqlite3.OperationalError as error:
            if "interrupted" not in str(error):
                raise
            status = f"EXCEEDED {budget:.0f}s budget"
        except Exception as error:  # noqa: BLE001
            status = f"FAILED {type(error).__name__}: {error}"[:120]
    ms = (time.perf_counter() - started) * 1000
    return round(ms, 1), len(counting.queries) - before, status


def _repeated(queries: list[str]) -> str:
    """The statement shape a surface ran most, and how often.

    A page costing four hundred queries is a fact; *which* statement it ran
    four hundred times is the fact you can act on, and guessing at it from the
    total is how the wrong thing gets optimized.
    """
    if not queries:
        return ""
    counts: dict[str, int] = {}
    for sql in queries:
        counts[sql] = counts.get(sql, 0) + 1
    shape, total = max(counts.items(), key=lambda pair: pair[1])
    if total < 3:
        return ""
    return f"x{total}: {shape[:100]}"


def measure(
    conn: sqlite3.Connection, settings: Settings, *, budget: float = 60.0
) -> list[Measurement]:
    from librairy import attention
    from librairy.search import SearchFilters, search_data
    from librairy.web.browse import browse_home
    from librairy.web.commit_queue import queue_rows, queue_summary
    from librairy.web.dashboard import dashboard_data
    from librairy.web.health import health_data
    from librairy.web.quarantine import quarantine_data
    from librairy.web.review import ReviewFilters, review_data

    counting = Counting(conn)
    out: list[Measurement] = []

    def record(name: str, call, detail: str = "") -> None:  # noqa: ANN001
        before = len(counting.queries)
        ms, queries, status = _time(counting, call, budget=budget)
        hottest = detail or _repeated(counting.queries[before:])
        out.append(Measurement(name, ms, queries, hottest, status))
        #  Progress on stderr, because a run at a million rows takes long
        #  enough that silence is indistinguishable from a hang.
        print(
            f"  {name:<28} {ms:9.1f} ms  {queries:6d} queries  {status}",
            file=sys.stderr,
            flush=True,
        )

    record("Review page 1", lambda: review_data(counting, ReviewFilters(), settings))
    record("Review page 50", lambda: review_data(counting, ReviewFilters(page=50), settings))
    #  `grouped` is derived from the sort, so any non-default sort is the
    #  flat list. Worth measuring: it is the same rows without the folding.
    record(
        "Review sorted (ungrouped)",
        lambda: review_data(counting, ReviewFilters(sort="name"), settings),
    )
    record("Dashboard", lambda: dashboard_data(counting, settings))
    record("Health", lambda: health_data(counting, settings))
    record("Health (attention)", lambda: attention.report(counting, settings))
    record("Commit summary", lambda: queue_summary(counting))
    record(
        "Commit page 1 (new files)",
        lambda: queue_rows(counting, settings, kind="new-file", page=1),
    )
    record(
        "Commit page 20 (new files)",
        lambda: queue_rows(counting, settings, kind="new-file", page=20),
    )
    record("Quarantine page 1", lambda: quarantine_data(counting, settings))
    record("Quarantine page 50", lambda: quarantine_data(counting, settings, page=50))
    record(
        "Search 'Album'",
        lambda: search_data(counting, settings, "Album", SearchFilters()),
    )
    record(
        "Search unfiltered",
        lambda: search_data(counting, settings, "", SearchFilters()),
    )
    record("Browse home", lambda: browse_home(counting, settings))
    return out


def decision_scale(conn: sqlite3.Connection) -> dict[str, int]:
    """How much work Review actually asks a person for.

    Three numbers, and the gap between the second and the third is the whole
    argument for M1-02:

        per_file          one decision per pending proposal
        ideal_grouped     one per coherent group, plus the loose files
        as_presented      what the current page structure produces, because
                          grouping happens *after* `LIMIT 50` — a group larger
                          than a page is split across pages by construction,
                          and singleton-folding can only see one page at a time
    """
    per_file = conn.execute(
        "SELECT COUNT(*) FROM proposals WHERE status='proposed'"
    ).fetchone()[0]
    ideal = conn.execute(
        """
        SELECT COUNT(*) FROM (
          SELECT group_id FROM proposals
          WHERE status='proposed' AND group_id IS NOT NULL
          GROUP BY group_id
          UNION ALL
          SELECT id FROM proposals WHERE status='proposed' AND group_id IS NULL
        )
        """
    ).fetchone()[0]

    #  Replaying the page structure rather than guessing at it: the same order
    #  Review uses, chunked the same way, folded the same way.
    presented = 0
    pages_touched: dict[int, int] = {}
    page_index = 0
    chunk: list[int | None] = []

    def close(chunk: list[int | None], page_index: int) -> int:
        for group_id in {g for g in chunk if g is not None}:
            pages_touched[group_id] = pages_touched.get(group_id, 0) + 1
        return _page_decisions(chunk)

    for row in conn.execute(
        "SELECT p.group_id FROM proposals p JOIN items i ON i.id = p.item_id"
        " WHERE p.status='proposed' AND i.missing_since IS NULL"
        " ORDER BY p.confidence DESC, p.id DESC"
    ):
        chunk.append(row[0])
        if len(chunk) == PAGE:
            page_index += 1
            presented += close(chunk, page_index)
            chunk = []
    if chunk:
        page_index += 1
        presented += close(chunk, page_index)
    split = sum(1 for pages in pages_touched.values() if pages > 1)

    return {
        "pending_proposals": per_file,
        "per_file_decisions": per_file,
        "ideal_grouped_decisions": ideal,
        "as_presented_today": presented,
        "groups_split_across_pages": split,
        "groups_total": len(pages_touched),
        "pages": -(-per_file // PAGE) if per_file else 0,
    }


def _page_decisions(chunk: list[int | None]) -> int:
    """One page's worth of *decisions*, folded the way `_fold_singletons` folds.

    Not sections. The `Ungrouped` heading is one section and fifty separate
    answers, and counting it as one decision would report the current page
    structure as cheaper than the ideal it is measured against — which is how
    a benchmark ends up arguing for the thing it was built to question.

    A group that has two or more members *on this page* is one decision here.
    A member that arrives alone on its page has been folded into the loose
    pile by `_fold_singletons` and is answered on its own, whatever it belongs
    to elsewhere.
    """
    counts: dict[int, int] = {}
    loose = 0
    for group_id in chunk:
        if group_id is None:
            loose += 1
        else:
            counts[group_id] = counts.get(group_id, 0) + 1
    grouped = sum(1 for total in counts.values() if total > 1)
    loose += sum(total for total in counts.values() if total == 1)
    return grouped + loose


def throughput(conn: sqlite3.Connection, settings: Settings, sample: int) -> dict[str, float]:
    """Per-second rates for the maintenance work the worker does on this data."""
    from librairy.search import sync_search_item

    out: dict[str, float] = {}

    started = time.perf_counter()
    for item_id in range(1, sample + 1):
        sync_search_item(conn, item_id)
    conn.commit()
    seconds = time.perf_counter() - started
    out["search_index_items_per_second"] = round(sample / seconds, 1) if seconds else 0.0

    started = time.perf_counter()
    conn.execute("SELECT COUNT(*) FROM items WHERE root='library'").fetchone()
    out["count_library_ms"] = round((time.perf_counter() - started) * 1000, 2)
    return out


def run(
    base: Path, *, library: int, inbox: int, sample: int, budget: float = 60.0
) -> Result:
    settings = settings_for(base)
    conn = connect(settings)
    population = synthesize(
        conn,
        library=library,
        inbox=inbox,
        #  Scaled with the library, because that is what produces them.
        findings=max(1_000, library // 20),
        quarantine=max(500, library // 100),
        history=max(1_000, library // 10),
    )
    population.db_bytes = database_path(settings).stat().st_size
    result = Result(population=population)
    print(f"  built in {population.build_seconds}s", file=sys.stderr, flush=True)
    result.measurements = measure(conn, settings, budget=budget)
    result.decisions = decision_scale(conn)
    result.throughput = throughput(conn, settings, sample)
    result.peak_rss_mb = peak_rss_mb()
    conn.close()
    return result


def peak_rss_mb() -> int:
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    #  Linux reports kilobytes, macOS bytes.
    return int(usage / (1024 * 1024)) if usage > 10**7 else int(usage / 1024)


def as_dict(result: Result) -> dict[str, object]:
    return {
        "population": {
            "library": result.population.library,
            "inbox": result.population.inbox,
            "proposals": result.population.proposals,
            "groups": result.population.groups,
            "findings": result.population.findings,
            "quarantine": result.population.quarantine,
            "history": result.population.history,
            "build_seconds": result.population.build_seconds,
            "db_bytes": result.population.db_bytes,
        },
        "surfaces": [
            {
                "surface": m.surface,
                "ms": m.ms,
                "queries": m.queries,
                "status": m.status,
                "hottest": m.detail,
            }
            for m in result.measurements
        ],
        "decisions": result.decisions,
        "throughput": result.throughput,
        "peak_rss_mb": result.peak_rss_mb,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--library", type=int, default=100_000)
    parser.add_argument("--inbox", type=int, default=5_000)
    parser.add_argument("--sample", type=int, default=2_000, help="items for throughput timing")
    parser.add_argument(
        "--budget", type=float, default=60.0, help="seconds before a surface is abandoned"
    )
    parser.add_argument("--base-dir", type=Path)
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args(argv)

    base = args.base_dir or Path(tempfile.mkdtemp(prefix="librairy-scale-"))
    base.mkdir(parents=True, exist_ok=True)
    try:
        result = run(
            base,
            library=args.library,
            inbox=args.inbox,
            sample=args.sample,
            budget=args.budget,
        )
        payload = json.dumps(as_dict(result), indent=2, sort_keys=True)
        if args.json_out:
            args.json_out.write_text(payload + "\n", encoding="utf-8")
        print(payload)
    finally:
        if args.base_dir is None:
            shutil.rmtree(base, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
