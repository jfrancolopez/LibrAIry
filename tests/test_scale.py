"""Pages that stay the same size while the database does not.

The design rule for this pass: **bounded SQL queries, server-side filters and
pagination, aggregate summaries.** If the browser is sent fifty rows at a time,
the UI stays simple whether the database holds fifty or fifty million — and no
front-end virtualisation is needed to make that true.

These tests hold the rule where it can actually break:

* the row count in the response does not grow with the table
* the count in a heading comes from `COUNT(*)`, not from `len(rows)`
* filtering happens in SQL, not by rendering everything and hiding some
* paging is deterministic — no row on two pages, none skipped
* the number of queries per page does not grow with the number of rows

The fixtures below are *database* populations, not real files. Ten thousand
rows is enough to catch every one of these mistakes; a million real files would
only make the suite slow and prove the same thing. Local timings are in the
report rather than asserted here, because a CI runner's clock is not evidence
about anybody's NAS.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from librairy.config import Settings
from librairy.db import connect
from librairy.web.commit_queue import PAGE_SIZE as COMMIT_PAGE_SIZE
from librairy.web.commit_queue import queue_rows, queue_summary
from librairy.web.quarantine import PAGE_SIZE as QUARANTINE_PAGE_SIZE
from librairy.web.quarantine import quarantine_data

# Big enough that an unbounded query is unmistakable in the result, small
# enough that building the fixture is not itself the slow part of the suite.
MANY = 10_000


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


class CountingConnection:
    """A connection that remembers how many statements it was asked to run.

    The interesting failure is not a slow query, it is *N* queries: one lookup
    per row turns fifty rows into fifty-one round trips and looks perfectly
    fine until the table is large. Counting is the only way to see it from a
    test.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.queries: list[str] = []

    def execute(self, sql, *args, **kwargs):  # noqa: ANN001, ANN201
        self.queries.append(" ".join(str(sql).split())[:90])
        return self._conn.execute(sql, *args, **kwargs)

    def __getattr__(self, name):  # noqa: ANN001, ANN204
        return getattr(self._conn, name)


@pytest.fixture(scope="module")
def big(tmp_path_factory: pytest.TempPathFactory):
    """One database with a large quarantine and a large commit queue."""
    tmp_path = tmp_path_factory.mktemp("scale")
    settings = settings_for(tmp_path)
    conn = connect(settings)
    now = "2026-08-14T00:00:00+00:00"

    conn.executemany(
        "INSERT INTO items(id, root, relpath, size, mtime_ns, fingerprint, state,"
        " first_seen_at, last_seen_at) VALUES (?, 'quarantine', ?, ?, 0, ?,"
        " 'discovered', ?, ?)",
        [
            (n, f"2026-08-14/file-{n:06d}.flac", 1024 * n, f"fp{n:06d}", now, now)
            for n in range(1, MANY + 1)
        ],
    )
    conn.executemany(
        "INSERT INTO quarantine_entries(id, item_id, reason, original_root,"
        " original_relpath, quarantined_at) VALUES (?, ?, 'exact_duplicate',"
        " 'library', ?, ?)",
        [(n, n, f"Music/Pop/file-{n:06d}.flac", now) for n in range(1, MANY + 1)],
    )
    # A commit queue of the same order, as approved inbox proposals.
    conn.executemany(
        "INSERT INTO items(id, root, relpath, size, mtime_ns, fingerprint, state,"
        " first_seen_at, last_seen_at) VALUES (?, 'inbox', ?, ?, 0, ?, 'discovered',"
        " ?, ?)",
        [
            (MANY + n, f"2026-08-14/new-{n:06d}.flac", 2048, f"nfp{n:06d}", now, now)
            for n in range(1, MANY + 1)
        ],
    )
    conn.executemany(
        "INSERT INTO proposals(id, item_id, category, clean_name, dest_relpath,"
        " confidence, status, evidence, created_at, updated_at, action, dest_root)"
        " VALUES (?, ?, 'music', ?, ?, 0.9, 'approved', '[]', ?, ?, 'move', 'library')",
        [
            (n, MANY + n, f"new-{n:06d}.flac", f"Music/Pop/new-{n:06d}.flac", now, now)
            for n in range(1, MANY + 1)
        ],
    )
    conn.commit()
    return conn, settings


# --- Quarantine ---------------------------------------------------------------


def test_quarantine_renders_one_page_of_ten_thousand(big) -> None:
    conn, settings = big

    data = quarantine_data(conn, settings)

    assert len(data["entries"]) == QUARANTINE_PAGE_SIZE
    assert data["total"] == MANY
    assert data["page_count"] == MANY // QUARANTINE_PAGE_SIZE


def test_quarantine_counts_come_from_sql_not_from_the_page(big) -> None:
    """The heading must be able to say 10,000 while the page holds 50."""
    conn, settings = big

    data = quarantine_data(conn, settings)

    assert data["counts"]["held"] == MANY
    assert data["held"] == MANY
    assert len(data["entries"]) < data["held"]


def test_quarantine_queries_do_not_grow_with_the_page(big) -> None:
    """Every fact on a row comes from the page query or one shared lookup.

    A `pending_request` per row would pass every functional test in the suite
    and quietly issue fifty-one queries per page.
    """
    conn, settings = big
    counting = CountingConnection(conn)

    quarantine_data(counting, settings)

    assert len(counting.queries) < 12, counting.queries


def test_quarantine_paging_is_deterministic(big) -> None:
    conn, settings = big

    first = quarantine_data(conn, settings, page=1)["entries"]
    second = quarantine_data(conn, settings, page=2)["entries"]

    ids_first = [row["id"] for row in first]
    ids_second = [row["id"] for row in second]
    assert len(set(ids_first) & set(ids_second)) == 0
    assert ids_first == sorted(ids_first, reverse=True)
    assert max(ids_second) < min(ids_first)


def test_quarantine_filters_in_sql(big) -> None:
    """An empty view returns no rows — it does not return everything and hide
    them, which is what "filtering" in a template amounts to."""
    conn, settings = big

    restored = quarantine_data(conn, settings, view="restored")

    assert restored["entries"] == []
    assert restored["total"] == 0


def test_a_bad_view_falls_back_rather_than_erroring(big) -> None:
    conn, settings = big

    data = quarantine_data(conn, settings, view="../../etc/passwd")

    assert data["view"] == "held"


# --- Commit -------------------------------------------------------------------


def test_commit_summary_counts_without_loading_rows(big) -> None:
    conn, _settings = big

    summary = queue_summary(conn)

    assert summary["decisions"] == MANY
    new_files = next(g for g in summary["groups"] if g["type"] == "new-file")
    assert new_files["decisions"] == MANY


def test_commit_renders_one_bounded_page(big) -> None:
    conn, settings = big

    rows = queue_rows(conn, settings, kind="new-file", page=1)

    assert len(rows) == COMMIT_PAGE_SIZE


def test_commit_paging_is_deterministic(big) -> None:
    conn, settings = big

    first = queue_rows(conn, settings, kind="new-file", page=1)
    second = queue_rows(conn, settings, kind="new-file", page=2)

    names_first = [row["subject"] for row in first]
    names_second = [row["subject"] for row in second]
    assert set(names_first) & set(names_second) == set()
    assert len(set(names_first)) == len(names_first)


def test_commit_summary_queries_are_a_fixed_number(big) -> None:
    conn, _settings = big
    counting = CountingConnection(conn)

    queue_summary(counting)

    # Two aggregates: one over proposals, one over plans. Not one per type,
    # and certainly not one per row.
    assert len(counting.queries) == 2, counting.queries


def test_the_whole_commit_page_stays_bounded(big) -> None:
    """The response, not just the query. This is what reaches the browser."""
    from librairy.web.commit import commit_overview

    conn, settings = big
    data = commit_overview(conn, settings, kind="new-file")

    rendered = sum(len(group["rows"]) for group in data["queue_groups"])
    assert rendered <= COMMIT_PAGE_SIZE
    assert data["summary"]["decisions"] == MANY


def test_commit_page_is_quick_enough_to_be_worth_measuring(big) -> None:
    """Not a threshold on CI's clock — a guard against an accidental O(n).

    Ten seconds is not a performance target; it is the difference between "a
    bounded query" and "it loaded ten thousand rows". The real numbers are in
    the report.
    """
    from librairy.web.commit import commit_overview

    conn, settings = big
    started = time.monotonic()
    commit_overview(conn, settings)
    assert time.monotonic() - started < 10
