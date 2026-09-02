"""The bounded-page rule, held on the surfaces `test_scale.py` does not reach.

`test_scale.py` pins Quarantine and Commit: a page is fifty rows whatever the
table holds, counts come from SQL, and the number of statements does not grow
with the number of rows. That rule is the whole scalability story and it was
only ever enforced on two pages.

This file extends it to Review, Health and Search, and it is deliberately
uncomfortable reading: three of these tests are `xfail(strict=True)`. They are
not aspirations. They are defects that M1-01 measured, written down as the
invariant they violate, so that:

* the shape of the problem is in the test suite rather than in a report nobody
  re-reads, and
* the day one is fixed, `strict=True` turns the unexpected pass into a failure
  and the marker has to be removed deliberately.

Populations here are small on purpose. A correlated scan is visible in a query
plan at ten rows and in a statement count at two hundred; proving it again at a
million would only make the suite slow. The million-row numbers live in
`docs/performance.md`, produced by `scripts/scale_bench.py`.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

from librairy.config import Settings
from librairy.db import connect


def load_bench():
    path = Path(__file__).resolve().parents[1] / "scripts/scale_bench.py"
    spec = importlib.util.spec_from_file_location("scale_bench", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    #  Registered before execution: `scale_bench` defines dataclasses, and
    #  `@dataclass` resolves annotations through `sys.modules[cls.__module__]`.
    #  A module that is not there yet makes that lookup return None.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def settings_for(tmp_path: Path) -> Settings:
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        FILE_STABILITY_SECONDS=0,
        AUTH_REQUIRED=False,
        _env_file=None,
    )
    for root in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        root.mkdir(parents=True, exist_ok=True)
    return settings


class Counting:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.queries: list[str] = []

    def execute(self, sql, *args, **kwargs):  # noqa: ANN001, ANN201
        self.queries.append(" ".join(str(sql).split())[:120])
        return self._conn.execute(sql, *args, **kwargs)

    def executemany(self, sql, *args, **kwargs):  # noqa: ANN001, ANN201
        return self._conn.executemany(sql, *args, **kwargs)

    def __getattr__(self, name):  # noqa: ANN001, ANN204
        return getattr(self._conn, name)


def build(tmp_path: Path, **population):  # noqa: ANN201
    bench = load_bench()
    settings = settings_for(tmp_path)
    conn = connect(settings)
    bench.synthesize(conn, **population)
    return conn, settings


@pytest.fixture
def small(tmp_path):  # noqa: ANN001, ANN201
    return build(
        tmp_path, library=400, inbox=200, findings=100, quarantine=50, history=100
    )


# --- Review -------------------------------------------------------------------


def test_review_renders_one_bounded_page(small) -> None:  # noqa: ANN001
    from librairy.web.review import PAGE_SIZE, ReviewFilters, review_data

    conn, settings = small
    data = review_data(conn, ReviewFilters(), settings)
    rendered = sum(len(group["rows"]) for group in data["groups"])
    assert rendered <= PAGE_SIZE
    assert data["total"] > PAGE_SIZE, "the fixture must be larger than one page"


def test_review_counts_come_from_sql_not_from_the_page(small) -> None:  # noqa: ANN001
    from librairy.web.review import ReviewFilters, review_data

    conn, settings = small
    data = review_data(conn, ReviewFilters(), settings)
    rendered = sum(len(group["rows"]) for group in data["groups"])
    assert data["total"] > rendered


def test_review_queries_do_not_grow_with_the_findings_table(tmp_path) -> None:  # noqa: ANN001
    """The page is a page, whatever the audit has found.

    Two defects fixed here, and it took both: the per-finding plan lookup is
    batched, and `audit_view` renders a bounded page of subjects instead of
    every finding in the database. Either one alone still left a statement
    count that grew with `audit_findings`.
    """
    from librairy.web.review import ReviewFilters, review_data

    counts = []
    for findings in (50, 400):
        conn, settings = build(
            tmp_path / f"f{findings}",
            library=400,
            inbox=200,
            findings=findings,
            quarantine=50,
            history=100,
        )
        counting = Counting(conn)
        review_data(counting, ReviewFilters(), settings)
        counts.append(len(counting.queries))
        conn.close()
    #  Eight times the findings must not mean more statements: the page is the
    #  same fifty proposals and the same twenty-five subjects either way.
    assert counts[1] <= counts[0] * 1.2, f"{counts[0]} -> {counts[1]} queries"


def test_the_audit_section_renders_a_bounded_page(tmp_path) -> None:  # noqa: ANN001
    from librairy.web.review import AUDIT_PAGE, ReviewFilters, review_data

    conn, settings = build(
        tmp_path, library=400, inbox=50, findings=2_000, quarantine=50, history=100
    )
    data = review_data(conn, ReviewFilters(), settings)
    assert len(data["audit_groups"]) <= AUDIT_PAGE
    assert len(data["audit_waiting"]) <= AUDIT_PAGE
    assert len(data["audit_dismissed"]) <= AUDIT_PAGE


def test_the_audit_heading_counts_the_table_not_the_page(tmp_path) -> None:  # noqa: ANN001
    """The number above a bounded list has to describe the whole of it."""
    from librairy.web.review import ReviewFilters, review_data

    conn, settings = build(
        tmp_path, library=400, inbox=50, findings=2_000, quarantine=50, history=100
    )
    data = review_data(conn, ReviewFilters(), settings)
    rendered = sum(int(group["count"]) for group in data["audit_groups"])
    assert data["audit_open"] > rendered
    assert data["audit_subjects_total"] > data["audit_subjects_shown"]
    assert data["audit_more"] == data["audit_subjects_total"] - data["audit_subjects_shown"]


@pytest.mark.xfail(
    strict=True,
    reason=(
        "M1-01 measured this: destination_choice._artist_folder_under runs "
        "SELECT relpath FROM items WHERE root='library' — an unbounded scan of "
        "the whole library — once per candidate row on the page. "
        "See docs/performance.md and ROADMAP.md M1-02."
    ),
)
def test_review_queries_do_not_grow_with_the_library(tmp_path) -> None:  # noqa: ANN001
    from librairy.web.review import ReviewFilters, review_data

    counts = []
    for library in (400, 3_200):
        conn, settings = build(
            tmp_path / f"l{library}",
            library=library,
            inbox=200,
            findings=50,
            quarantine=50,
            history=100,
        )
        counting = Counting(conn)
        review_data(counting, ReviewFilters(), settings)
        scans = sum(
            1
            for sql in counting.queries
            if "FROM items WHERE root='library'" in sql and "LIMIT" not in sql
        )
        counts.append(scans)
        conn.close()
    assert counts == [0, 0], f"{counts} unbounded library scans behind one Review page"


# --- Health -------------------------------------------------------------------

UNINDEXED_SQL = """
SELECT COUNT(*) FROM items i
WHERE i.missing_since IS NULL
  AND NOT EXISTS (SELECT 1 FROM search_fts s WHERE s.item_id = i.id)
"""


@pytest.mark.xfail(
    strict=True,
    reason=(
        "M1-01 measured this: search_fts declares item_id UNINDEXED, so the "
        "NOT EXISTS runs a full scan of the FTS table for every row of items — "
        "quadratic, 3.8 seconds at 5,000 items, and Health asks for it twice per "
        "render. See docs/performance.md and ROADMAP.md M1-01."
    ),
)
def test_the_unindexed_count_is_not_a_correlated_scan(small) -> None:  # noqa: ANN001
    conn, _ = small
    plan = [tuple(row) for row in conn.execute("EXPLAIN QUERY PLAN " + UNINDEXED_SQL)]
    detail = " | ".join(str(row[-1]) for row in plan)
    assert "CORRELATED" not in detail, detail


def test_health_reads_and_writes_nothing(small) -> None:  # noqa: ANN001
    """The invariant that must not drift, whatever the cost of reading."""
    from librairy.web.health import health_data

    conn, settings = small
    before = conn.total_changes
    health_data(conn, settings)
    assert conn.total_changes == before


# --- Search -------------------------------------------------------------------


def test_search_queries_do_not_grow_with_the_library(tmp_path) -> None:  # noqa: ANN001
    from librairy.search import SearchFilters, search_data

    counts = []
    for library in (400, 3_200):
        conn, settings = build(
            tmp_path / f"s{library}",
            library=library,
            inbox=100,
            findings=50,
            quarantine=50,
            history=100,
        )
        counting = Counting(conn)
        search_data(counting, settings, "Album", SearchFilters())
        counts.append(len(counting.queries))
        conn.close()
    assert counts[1] <= counts[0] * 1.2, f"{counts[0]} -> {counts[1]} queries"


def test_search_renders_one_bounded_page(tmp_path) -> None:  # noqa: ANN001
    from librairy.search import PAGE_SIZE, SearchFilters, search_data

    conn, settings = build(
        tmp_path, library=2_000, inbox=50, findings=50, quarantine=50, history=50
    )
    data = search_data(conn, settings, "Album", SearchFilters())
    assert len(data["results"]) <= PAGE_SIZE


# --- opt-in, and genuinely large ----------------------------------------------


@pytest.mark.scale
def test_the_bounded_surfaces_stay_bounded_at_fifty_thousand(tmp_path) -> None:  # noqa: ANN001
    """The pages that hold, held at a population worth the name.

    Deselected by default — it builds fifty thousand rows and takes seconds
    rather than milliseconds. Run it with `-m scale`. Review and Health are
    deliberately absent: they do not finish, which is the finding, and a test
    that waited for them would just be a slow way of saying so.
    """
    from librairy.web.commit_queue import queue_rows, queue_summary
    from librairy.web.dashboard import dashboard_data
    from librairy.web.quarantine import quarantine_data

    conn, settings = build(
        tmp_path, library=50_000, inbox=2_000, findings=1_000, quarantine=1_000, history=5_000
    )
    counting = Counting(conn)

    dashboard_data(counting, settings)
    dashboard_queries = len(counting.queries)
    assert dashboard_queries < 40, f"{dashboard_queries} statements behind the dashboard"

    before = len(counting.queries)
    queue_summary(counting)
    assert len(counting.queries) - before < 10

    before = len(counting.queries)
    rows = queue_rows(counting, settings, kind="new-file", page=1)
    assert len(rows) <= 50
    assert len(counting.queries) - before < 20

    before = len(counting.queries)
    data = quarantine_data(counting, settings)
    assert len(data["entries"]) <= 50
    assert len(counting.queries) - before < 60
