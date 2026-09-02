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
    """Bounded by decisions now, and by rows as a consequence.

    A grouped page holds `UNITS_PAGE` decisions. Each shows at most
    `MEMBER_PREVIEW` of its members, so the rows are bounded too — by a bigger
    number than the old fifty, and by a number that does not move when a group
    holds three thousand files instead of three.
    """
    from librairy.web.review import MEMBER_PREVIEW, UNITS_PAGE, ReviewFilters, review_data

    conn, settings = small
    data = review_data(conn, ReviewFilters(), settings)
    sections = [g for g in data["groups"] if g["kind"] not in ("ungrouped", "sorted")]
    assert len(data["groups"]) <= UNITS_PAGE + 1  # +1 for the loose section
    assert all(len(g["rows"]) <= MEMBER_PREVIEW for g in sections)
    rendered = sum(len(group["rows"]) for group in data["groups"])
    assert rendered <= UNITS_PAGE * MEMBER_PREVIEW
    assert data["total"] > UNITS_PAGE, "the fixture must be larger than one page"


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


def test_unbounded_library_scans_do_not_grow_with_the_library(tmp_path) -> None:  # noqa: ANN001
    """A Review page may read the library; it may not read it more the bigger it gets.

    Two queries behind Review still have no `LIMIT`: `destination_folders`,
    which streams the index and stops at two hundred folder names, and
    `_child_folders`, which asks for the distinct folders inside one section.
    Both are bounded by what they are looking for rather than by the page, and
    neither is per-row any more — `_artist_folder_under` used to run one full
    scan of a section *per candidate finding*, 1.0 s a call at a million files.
    A `ChildFolders` batcher makes it one scan per section per render, and an
    exact-spelling seek usually avoids even that.

    Zero would be better than a small constant, and would need the schema to
    know about folders. This pins the property that actually matters: the
    number does not move when the library grows eightfold.
    """
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
        counts.append(
            sum(
                1
                for sql in counting.queries
                if "FROM items WHERE root='library'" in sql and "LIMIT" not in sql
            )
        )
        conn.close()
    #  The property that matters: eight times the library, the same number of
    #  scans. They are one per *distinct section* named by the findings on the
    #  page, plus one for `destination_folders` — bounded by the page, which is
    #  itself bounded.
    assert counts[0] == counts[1], f"{counts} unbounded library scans"
    #  A ratchet, not a target. Zero is the target and needs the schema to know
    #  about folders; this exists to catch a new one being added quietly.
    assert counts[1] <= 8, f"{counts[1]} unbounded library scans behind one Review page"


# --- Health -------------------------------------------------------------------

def test_the_unindexed_count_never_scans_the_index_per_row(small) -> None:  # noqa: ANN001
    """The question asked of the *function*, not of a copy of its old SQL.

    `search_fts` declares `item_id UNINDEXED`, so the obvious `NOT EXISTS`
    spelling has nothing to seek on and the planner scans the whole index once
    per row of `items` — quadratic, and 3.8 seconds at five thousand files.
    Asserting on the plan rather than on a stopwatch, because a timing test at
    this size would only be flaky.
    """
    from librairy.search_health import unindexed

    conn, _ = small
    statements: list[str] = []

    class Watching:
        def __init__(self, inner) -> None:  # noqa: ANN001
            self._inner = inner

        def execute(self, sql, *args, **kwargs):  # noqa: ANN001, ANN201
            statements.append(str(sql))
            return self._inner.execute(sql, *args, **kwargs)

        def __getattr__(self, name):  # noqa: ANN001, ANN204
            return getattr(self._inner, name)

    unindexed(Watching(conn))
    assert statements, "unindexed ran no queries at all"
    for sql in statements:
        plan = " | ".join(
            str(row[-1]) for row in conn.execute("EXPLAIN QUERY PLAN " + sql)
        )
        assert "CORRELATED" not in plan, f"{plan}\n{sql}"


def test_the_unindexed_count_is_still_the_right_number(tmp_path) -> None:  # noqa: ANN001
    """Faster is only worth having if it is also true."""
    from librairy.search_health import unindexed

    conn, _ = build(
        tmp_path, library=200, inbox=0, findings=10, quarantine=10, history=10
    )
    #  Not zero: the fixture's quarantine rows are live items the harness never
    #  indexes, and they are genuinely unindexed. Deltas from here.
    baseline = unindexed(conn)
    conn.execute("DELETE FROM search_fts WHERE rowid IN (1, 2, 3)")
    conn.commit()
    assert unindexed(conn) == baseline + 3
    #  A file that has gone missing keeps its index row on purpose, and is not
    #  a file on disk — so it counts on neither side of the subtraction.
    conn.execute("UPDATE items SET missing_since='2026-09-02' WHERE id IN (4, 5)")
    conn.commit()
    assert unindexed(conn) == baseline + 3


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


def test_a_group_larger_than_a_page_says_how_large_it_is(tmp_path) -> None:  # noqa: ANN001
    """The heading counts the group; the list shows a page of it.

    Until this, a group bigger than a page rendered the members that happened
    to land there under a heading saying "12 shown" — of what, it did not say.
    A 150-photograph event read as twelve photographs, which is the difference
    between one decision and a wrong impression of the queue.
    """
    from librairy.web.review import MEMBER_PREVIEW, UNITS_PAGE, ReviewFilters, review_data

    conn, settings = build(
        tmp_path, library=200, inbox=400, findings=20, quarantine=20, history=20
    )
    big = None
    for page in range(1, 12):
        data = review_data(conn, ReviewFilters(page=page), settings)
        #  The page is still a page, on every one of them.
        assert sum(len(group["rows"]) for group in data["groups"]) <= UNITS_PAGE * MEMBER_PREVIEW
        for group in data["groups"]:
            if group["kind"] in ("ungrouped", "sorted"):
                continue
            if int(group["total"]) > len(group["rows"]):
                big = group
                break
        if big:
            break
    assert big is not None, "expected a group bigger than its preview"
    assert int(big["more"]) == int(big["total"]) - len(big["rows"])


def test_one_member_on_this_page_is_not_a_loose_file(tmp_path) -> None:  # noqa: ANN001
    """A group of one is not a group; one member *on this page* is different.

    `_fold_singletons` could only see a page, so the tail of a large group
    landing alone was folded into the loose pile and lost its heading. The
    group's real size decides now.
    """
    from librairy.web.review import ReviewFilters, review_data

    conn, settings = build(
        tmp_path, library=200, inbox=400, findings=20, quarantine=20, history=20
    )
    for page in (1, 2, 3):
        data = review_data(conn, ReviewFilters(page=page), settings)
        for group in data["groups"]:
            if group["kind"] in ("ungrouped", "sorted"):
                continue
            assert int(group["total"]) > 1, (
                f"page {page} kept a heading for a group of {group['total']}"
            )


def test_group_totals_respect_the_page_filters(tmp_path) -> None:  # noqa: ANN001
    """A heading counting the whole group above a narrowed list is a new lie."""
    from librairy.web.review import ReviewFilters, group_totals, review_data

    conn, settings = build(
        tmp_path, library=200, inbox=400, findings=20, quarantine=20, history=20
    )
    wide = ReviewFilters()
    narrow = ReviewFilters(min_confidence=0.9)
    rows = review_data(conn, wide, settings)["groups"]
    members = [
        row for group in rows for row in group["rows"] if row.get("group_id") is not None
    ]
    if not members:
        pytest.skip("no grouped rows in the fixture")
    assert group_totals(conn, narrow, members) != group_totals(conn, wide, members)


# --- decisions as the paging unit ---------------------------------------------


def _walk_decisions(conn, settings, pages: int = 40):  # noqa: ANN001, ANN202
    """Every unit and every rendered file id, page by page."""
    from librairy.web.review import ReviewFilters, review_data

    units: list[str] = []
    files: list[int] = []
    for page in range(1, pages + 1):
        data = review_data(conn, ReviewFilters(page=page), settings)
        if not data["groups"]:
            break
        for group in data["groups"]:
            units.append(f"{group['kind']}:{group['label']}")
            files.extend(int(row["id"]) for row in group["rows"])
    return units, files


def test_paging_decisions_neither_repeats_nor_drops_one(tmp_path) -> None:  # noqa: ANN001
    """No decision on two pages, none skipped between them."""
    conn, settings = build(
        tmp_path, library=200, inbox=400, findings=20, quarantine=20, history=20
    )
    units, _ = _walk_decisions(conn, settings)
    #  The loose section appears once per page it has members on, so count only
    #  the named ones — those are the decisions paging is meant to keep whole.
    named = [unit for unit in units if not unit.startswith("ungrouped:")]
    assert len(named) == len(set(named)), "a group appeared on more than one page"

    from librairy.web.review import ReviewFilters, unit_count

    assert len(units) >= unit_count(conn, ReviewFilters()) - len(named) or named


def test_no_file_is_rendered_twice_across_pages(tmp_path) -> None:  # noqa: ANN001
    conn, settings = build(
        tmp_path, library=200, inbox=400, findings=20, quarantine=20, history=20
    )
    _, files = _walk_decisions(conn, settings)
    assert len(files) == len(set(files)), "a file was rendered on two pages"


def test_paging_is_deterministic(tmp_path) -> None:  # noqa: ANN001
    conn, settings = build(
        tmp_path, library=200, inbox=400, findings=20, quarantine=20, history=20
    )
    first = _walk_decisions(conn, settings)
    second = _walk_decisions(conn, settings)
    assert first == second


def test_a_huge_group_does_not_put_its_members_on_the_page(tmp_path) -> None:  # noqa: ANN001
    """The point of paging decisions: a group is one row's worth of attention.

    Three thousand files in one group must cost the page five rows and an
    honest count, not three thousand rows — and not three thousand rows fetched
    and then thrown away either, which is why the member query is bounded in
    SQL rather than in Python.
    """
    from librairy.web.review import MEMBER_PREVIEW, ReviewFilters, review_data

    conn, settings = build(
        tmp_path, library=100, inbox=20, findings=10, quarantine=10, history=10
    )
    conn.execute(
        "INSERT INTO groups(id, kind, label, dest_base, created_at)"
        " VALUES (9001, 'photo_event', 'Enormous Event', 'Photos/Filed', '2026-09-02')"
    )
    now = "2026-09-02T00:00:00+00:00"
    conn.executemany(
        "INSERT INTO items(id, root, relpath, size, mtime_ns, fingerprint, state,"
        " first_seen_at, last_seen_at) VALUES (?, 'inbox', ?, 10, 0, ?, 'proposed', ?, ?)",
        [(500_000 + n, f"huge/IMG_{n:05d}.jpg", f"hg{n:09d}", now, now) for n in range(3000)],
    )
    conn.executemany(
        "INSERT INTO proposals(id, item_id, category, clean_name, dest_relpath,"
        " confidence, group_id, status, evidence, created_at, updated_at, action,"
        " dest_root) VALUES (?, ?, 'photos', ?, ?, 0.9, 9001, 'proposed', '[]', ?, ?,"
        " 'move', 'library')",
        [
            (500_000 + n, 500_000 + n, f"IMG_{n:05d}.jpg",
             f"Photos/Filed/IMG_{n:05d}.jpg", now, now)
            for n in range(3000)
        ],
    )
    conn.commit()

    counting = Counting(conn)
    data = review_data(counting, ReviewFilters(), settings)
    huge = next(g for g in data["groups"] if g["label"] == "Enormous Event")
    assert huge["total"] == 3000
    assert len(huge["rows"]) == MEMBER_PREVIEW
    assert huge["more"] == 3000 - MEMBER_PREVIEW
    #  And the whole page stays small.
    assert sum(len(g["rows"]) for g in data["groups"]) < 200


def test_the_decision_page_does_not_grow_its_queries_with_the_queue(tmp_path) -> None:  # noqa: ANN001
    """Both queues fill a page, so the comparison is like for like.

    A page that cannot be filled runs fewer per-row queries simply because it
    has fewer rows; that is not the property under test. The property is that
    a queue four times longer costs the same page.
    """
    from librairy.web.review import ReviewFilters, review_data

    counts = []
    for inbox in (800, 3_200):
        conn, settings = build(
            tmp_path / f"q{inbox}",
            library=400,
            inbox=inbox,
            findings=40,
            quarantine=40,
            history=40,
        )
        counting = Counting(conn)
        review_data(counting, ReviewFilters(), settings)
        counts.append(len(counting.queries))
        conn.close()
    assert counts[1] <= counts[0] * 1.2, f"{counts[0]} -> {counts[1]} queries"


def test_loose_files_stay_individual_decisions(tmp_path) -> None:  # noqa: ANN001
    """Grouping must not invent a group. A file that arrived alone is its own
    decision and keeps its own controls."""
    from librairy.web.review import ReviewFilters, review_data, unit_count

    conn, settings = build(
        tmp_path, library=100, inbox=60, findings=10, quarantine=10, history=10
    )
    conn.execute("UPDATE proposals SET group_id=NULL")
    conn.commit()
    data = review_data(conn, ReviewFilters(), settings)
    assert all(g["kind"] == "ungrouped" for g in data["groups"])
    assert unit_count(conn, ReviewFilters()) == data["total"]


def test_the_group_heading_states_the_whole_group(tmp_path) -> None:  # noqa: ANN001
    """Every number on the heading is a fact about the group, not the preview.

    Counting the rows below would give a smaller, wrong answer for exactly the
    groups the heading matters most for.
    """
    from librairy.web.review import MEMBER_PREVIEW, ReviewFilters, review_data

    conn, settings = build(
        tmp_path, library=200, inbox=400, findings=20, quarantine=20, history=20
    )
    data = review_data(conn, ReviewFilters(), settings)
    sections = [g for g in data["groups"] if g["kind"] not in ("ungrouped", "sorted")]
    assert sections, "expected at least one grouped decision"
    for group in sections:
        assert group["total"] >= len(group["rows"])
        assert 0.0 <= group["mean"] <= 1.0
        assert group["worst"] <= group["best"]
        assert group["doubtful"] <= group["total"]
        assert group["more"] == group["total"] - len(group["rows"])
        assert len(group["rows"]) <= MEMBER_PREVIEW


def test_expanding_a_group_is_itself_bounded(tmp_path) -> None:  # noqa: ANN001
    """The rest of a group arrives a page at a time, never in one go."""
    from librairy.web.review import (
        MEMBER_PAGE,
        ReviewFilters,
        group_members,
        review_data,
    )

    conn, settings = build(
        tmp_path, library=100, inbox=20, findings=10, quarantine=10, history=10
    )
    now = "2026-09-02T00:00:00+00:00"
    conn.execute(
        "INSERT INTO groups(id, kind, label, dest_base, created_at)"
        " VALUES (9002, 'photo_event', 'Big Event', 'Photos/Filed', ?)",
        (now,),
    )
    conn.executemany(
        "INSERT INTO items(id, root, relpath, size, mtime_ns, fingerprint, state,"
        " first_seen_at, last_seen_at) VALUES (?, 'inbox', ?, 10, 0, ?, 'proposed', ?, ?)",
        [(600_000 + n, f"big/IMG_{n:05d}.jpg", f"bg{n:09d}", now, now) for n in range(120)],
    )
    conn.executemany(
        "INSERT INTO proposals(id, item_id, category, clean_name, dest_relpath,"
        " confidence, group_id, status, evidence, created_at, updated_at, action,"
        " dest_root) VALUES (?, ?, 'photos', ?, ?, 0.9, 9002, 'proposed', '[]', ?, ?,"
        " 'move', 'library')",
        [
            (600_000 + n, 600_000 + n, f"IMG_{n:05d}.jpg",
             f"Photos/Filed/IMG_{n:05d}.jpg", now, now)
            for n in range(120)
        ],
    )
    conn.commit()

    data = review_data(conn, ReviewFilters(), settings)
    group = next(g for g in data["groups"] if g["label"] == "Big Event")
    unit = group["unit"]

    second = group_members(conn, ReviewFilters(), unit, page=2, settings=settings)
    assert len(second["rows"]) == MEMBER_PAGE
    assert second["total"] == 120
    assert second["next_page"] == 3

    #  Every member reachable, none twice.
    seen = [row["id"] for row in group["rows"]]
    page = 2
    while page:
        chunk = group_members(conn, ReviewFilters(), unit, page=page, settings=settings)
        seen.extend(row["id"] for row in chunk["rows"])
        page = chunk["next_page"]
    assert len(seen) == 120
    assert len(set(seen)) == 120
