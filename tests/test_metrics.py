"""A day's measurement, and the arithmetic that makes running it twice harmless.

Of forty-two tables, none recorded a measurement over time — so "was the Review
backlog smaller last Tuesday" had no answer at all. This is the smallest table
that gives it one, and most of what can go wrong with such a table is
arithmetic rather than plumbing:

    a "daily" row appended every worker cycle
    an event counter that double-counts on a retry
    a gauge and a count added together
    a chart whose cost grows with the library rather than with the window

Each of those is a test here. The design that makes most of them impossible —
every metric is a *recomputation* over authoritative data, never an increment,
and the primary key is (day, metric) — is worth stating because it is what
these tests are checking has not been abandoned.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from librairy import metrics
from librairy.config import Settings
from librairy.db import connect
from librairy.planner import utc_now


def settings_for(tmp_path: Path) -> Settings:
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        FILE_STABILITY_SECONDS=0,
        AUTH_REQUIRED=False,
        OLLAMA_HOST="",
        _env_file=None,
    )
    for directory in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        directory.mkdir(parents=True, exist_ok=True)
    return settings


def library(conn: sqlite3.Connection, count: int, size: int = 100) -> None:
    """`count` more files in the library, with distinct paths."""
    start = int(conn.execute("SELECT COUNT(*) FROM items").fetchone()[0])
    for index in range(start, start + count):
        conn.execute(
            "INSERT INTO items(root, relpath, size, mtime_ns, state, first_seen_at,"
            " last_seen_at) VALUES ('library', ?, ?, 0, 'committed', ?, ?)",
            (f"Music/Album/track-{index}.flac", size, utc_now(), utc_now()),
        )


def filed(conn: sqlite3.Connection, day: str, count: int) -> None:
    """`count` files that reached the library on `day`, as History records it."""
    for index in range(count):
        conn.execute(
            "INSERT INTO history(ts, action, src_root, src_relpath, dest_root,"
            " dest_relpath, outcome) VALUES (?, 'move', 'inbox', ?, 'library', ?, 'ok')",
            (f"{day}T12:00:0{index % 10}+00:00", f"in-{index}", f"Music/out-{index}"),
        )


def values(conn: sqlite3.Connection, day: str = "") -> dict[str, int]:
    day = day or metrics.today()
    return {
        str(row["metric"]): int(row["value"])
        for row in conn.execute("SELECT metric, value FROM metrics_daily WHERE day=?", (day,))
    }


def rows(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM metrics_daily").fetchone()[0])


# --- one period, however many times it is asked for ---------------------------------


def test_the_first_rollup_writes_one_day(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path))
    library(conn, 3)

    written = metrics.rollup(conn)

    assert written
    assert {str(row["day"]) for row in conn.execute("SELECT DISTINCT day FROM metrics_daily")} == {
        metrics.today()
    }
    assert values(conn)["library.files"] == 3


def test_running_the_same_day_again_replaces_it(tmp_path: Path) -> None:
    """The one failure mode a "daily rollup" invites: a worker cycling every
    five seconds and appending a row each time. The primary key makes it
    impossible rather than merely unlikely."""
    conn = connect(settings_for(tmp_path))
    library(conn, 3)
    metrics.rollup(conn)
    first = rows(conn)

    for _ in range(5):
        metrics.rollup(conn)

    assert rows(conn) == first


def test_todays_gauge_moves_with_the_library(tmp_path: Path) -> None:
    """A snapshot retaken says what is true now, not what was true this morning."""
    conn = connect(settings_for(tmp_path))
    library(conn, 3)
    metrics.rollup(conn)
    assert values(conn)["library.files"] == 3

    library(conn, 2)
    metrics.rollup(conn)

    assert values(conn)["library.files"] == 5
    assert values(conn)["library.bytes"] == 500


def test_an_event_count_does_not_double_on_a_rerun(tmp_path: Path) -> None:
    """Derived, never incremented — which is the whole reason a retry is safe.

    A counter that was `+= 1` as things happened would have to be right about
    every crash, every retry and every partially executed plan. This asks
    History the same question again and gets the same answer.
    """
    conn = connect(settings_for(tmp_path))
    filed(conn, metrics.today(), 4)

    metrics.rollup(conn)
    metrics.rollup(conn)
    metrics.rollup(conn)

    assert values(conn)["filed.files"] == 4


def test_a_count_only_covers_its_own_day(tmp_path: Path) -> None:
    """The day boundary, where an off-by-one would silently double a chart."""
    conn = connect(settings_for(tmp_path))
    day = metrics.today()
    start, end = metrics.bounds(day)
    filed(conn, day, 3)
    #  One second before the day starts and one at the moment the next begins.
    conn.execute(
        "INSERT INTO history(ts, action, src_root, src_relpath, dest_root,"
        " dest_relpath, outcome) VALUES (?, 'move', 'inbox', 'a', 'library', 'b', 'ok')",
        (f"{start}T00:00:00+00:00",),
    )
    conn.execute(
        "INSERT INTO history(ts, action, src_root, src_relpath, dest_root,"
        " dest_relpath, outcome) VALUES (?, 'move', 'inbox', 'a', 'library', 'b', 'ok')",
        (f"{end}T00:00:00+00:00",),
    )

    metrics.rollup(conn)

    #  Half-open: midnight belongs to the day that is starting, never to both.
    assert values(conn)["filed.files"] == 4


def test_a_day_is_a_utc_day(tmp_path: Path) -> None:
    """Chosen rather than inherited, and stated where it is decided.

    Every timestamp in this program is `utc_now()`. A rollup that took the
    container's local date would put a metric's boundary somewhere else from
    the rows it is derived from — so a file filed at 23:30 local would appear
    in one day's chart while its History row said another.
    """
    from datetime import UTC, datetime

    assert metrics.today() == datetime.now(UTC).date().isoformat()
    assert metrics.bounds("2026-02-28") == ("2026-02-28", "2026-03-01")
    assert metrics.bounds("2026-12-31") == ("2026-12-31", "2027-01-01")


# --- gauges and counts are different things -----------------------------------------


def test_every_metric_says_which_kind_it_is(tmp_path: Path) -> None:
    """Stored, not only declared. Somebody reading the raw table has to be able
    to tell a snapshot from a count: averaging two gauges is meaningful and
    adding them is not, and the other way round for counts."""
    conn = connect(settings_for(tmp_path))
    library(conn, 2)
    metrics.rollup(conn)

    kinds = {
        str(row["metric"]): str(row["kind"])
        for row in conn.execute("SELECT metric, kind FROM metrics_daily")
    }

    assert kinds["library.files"] == metrics.GAUGE
    assert kinds["filed.files"] == metrics.COUNT
    assert set(kinds.values()) <= {metrics.GAUGE, metrics.COUNT}


def test_every_metric_names_the_question_it_answers() -> None:
    """M3-01's own "do not": a metric with no product question behind it is one
    nobody reads and everybody has to keep working."""
    assert metrics.MEASURES
    for measure in metrics.MEASURES:
        assert measure.question, f"{measure.name} exists for no stated reason"
        assert measure.kind in (metrics.GAUGE, metrics.COUNT)


def test_gauges_and_counts_are_not_summed_together(tmp_path: Path) -> None:
    """A reader can aggregate correctly because the table tells it how.

    Ninety days of `library.files` averaged is a library's typical size; summed
    it is nonsense. Ninety days of `filed.files` summed is what was filed;
    averaged it is a daily rate. The test is that both are answerable from what
    is stored, without a lookup table somebody has to maintain elsewhere.
    """
    conn = connect(settings_for(tmp_path))
    library(conn, 10)
    filed(conn, metrics.today(), 4)
    metrics.rollup(conn)

    found = metrics.series(conn, ["library.files", "filed.files"], days=7)

    gauges = [point for point in found["library.files"] if point["kind"] == metrics.GAUGE]
    counts = [point for point in found["filed.files"] if point["kind"] == metrics.COUNT]
    assert [point["value"] for point in gauges] == [10]
    assert sum(int(point["value"]) for point in counts) == 4


# --- the ends of the range ----------------------------------------------------------


def test_an_empty_library_measures_zero_rather_than_nothing(tmp_path: Path) -> None:
    """A day with nothing in it is still a day. A chart with a gap where the
    library was empty would read as a chart with missing data."""
    conn = connect(settings_for(tmp_path))

    metrics.rollup(conn)

    assert values(conn)["library.files"] == 0
    assert values(conn)["library.bytes"] == 0
    #  And no category rows, because there are no folders — an empty
    #  distribution is empty rather than a row of noughts per possible name.
    assert metrics.distribution(conn) == []


def test_several_days_come_back_oldest_first(tmp_path: Path) -> None:
    conn = connect(settings_for(tmp_path))
    for offset, count in ((3, 1), (2, 5), (1, 9)):
        day = _days_before(offset)
        filed(conn, day, count)
        metrics.rollup(conn, day)

    found = metrics.series(conn, ["filed.files"], days=30)["filed.files"]

    assert [int(point["value"]) for point in found] == [1, 5, 9]
    assert [str(point["day"]) for point in found] == sorted(
        str(point["day"]) for point in found
    )


def test_a_read_is_bounded_by_the_window_and_not_by_the_library(
    tmp_path: Path,
) -> None:
    """The contract this table exists to provide.

    A Dashboard drawing ninety points has to cost the same on a library of four
    files and one of four million — which it does, because it reads days rather
    than items. Asserted as a *statement count*, because "it felt fast" is not
    a guarantee and a query added later would break it silently.
    """
    conn = connect(settings_for(tmp_path))
    library(conn, 500)
    for offset in range(1, 40):
        metrics.rollup(conn, _days_before(offset))

    counted = _Counting(conn)
    found = metrics.series(counted, ["library.files", "filed.files"], days=90)

    assert counted.statements == 1, "a series read cost more than one statement"
    assert len(found["library.files"]) == 39


def test_the_table_is_pruned_to_a_stated_retention(tmp_path: Path) -> None:
    """Bounded and stated, which is M3-01's acceptance. Two years of about
    twenty-six rows a day is under a megabyte, and "it is only kilobytes" is
    how every unbounded table starts."""
    conn = connect(settings_for(tmp_path))
    conn.execute(
        "INSERT INTO metrics_daily(day, metric, kind, value, taken_at)"
        " VALUES ('2019-01-01', 'library.files', 'gauge', 1, 'then')"
    )

    metrics.rollup(conn)

    assert conn.execute(
        "SELECT COUNT(*) FROM metrics_daily WHERE day='2019-01-01'"
    ).fetchone()[0] == 0
    assert metrics.KEEP_DAYS == 730


# --- the repair story ---------------------------------------------------------------


def test_a_deleted_day_is_recomputed_for_counts_and_gone_for_gauges(
    tmp_path: Path,
) -> None:
    """The honest asymmetry, and the whole of the repair story.

    A count is derived from rows that are still there, so it comes back. A
    gauge is a snapshot, and nobody can measure last Tuesday today — inventing
    a number for it is the one thing this table must never contain.
    """
    conn = connect(settings_for(tmp_path))
    yesterday = _days_before(1)
    filed(conn, yesterday, 6)
    metrics.rollup(conn, yesterday)
    assert values(conn, yesterday)["filed.files"] == 6

    metrics.forget(conn, yesterday)
    metrics.backfill(conn, days=7)

    assert values(conn, yesterday)["filed.files"] == 6, "a count did not come back"
    assert "library.files" not in values(conn, yesterday), (
        "a gauge was invented for a day nobody measured"
    )


def test_losing_the_whole_table_breaks_nothing(tmp_path: Path) -> None:
    """M3-01's acceptance, said plainly: deleting it degrades trends and breaks
    nothing. No decision, proposal, plan or file depends on a row in it."""
    conn = connect(settings_for(tmp_path))
    library(conn, 4)
    metrics.rollup(conn)
    before = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]

    metrics.forget(conn)

    assert rows(conn) == 0
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == before
    assert conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 0
    #  And the next rollup simply starts again.
    metrics.rollup(conn)
    assert values(conn)["library.files"] == 4


def test_backfill_recovers_history_an_upgrade_arrives_with(tmp_path: Path) -> None:
    """An upgrade has months of `history` behind it. A chart of what has been
    filed can start full instead of empty, and it costs one pass."""
    conn = connect(settings_for(tmp_path))
    for offset in (1, 2, 5):
        filed(conn, _days_before(offset), offset * 2)

    written = metrics.backfill(conn, days=30)

    assert written == 3
    found = metrics.series(conn, ["filed.files"], days=30)["filed.files"]
    assert [int(point["value"]) for point in found] == [10, 4, 2]


def test_backfill_writes_no_row_for_a_day_nothing_happened(tmp_path: Path) -> None:
    """A year of explicit noughts is a year of rows saying nothing happened,
    which the absence of a row already says."""
    conn = connect(settings_for(tmp_path))
    filed(conn, _days_before(2), 3)

    metrics.backfill(conn, days=30)

    assert rows(conn) == 1


# --- against the rest of the program ------------------------------------------------


def test_the_rollup_is_not_the_source_of_truth_for_anything_now(tmp_path: Path) -> None:
    """Current state is the Library. This is a record of what was.

    The check is structural: the Dashboard's live numbers and Health must not
    read this table, or a cache that can be wrong about the present would have
    become operational. See `librairy/metrics.py`.
    """
    import inspect

    from librairy import attention
    from librairy.web import dashboard

    for module in (dashboard, attention):
        source = inspect.getsource(module)
        assert "metrics_daily" not in source, (
            f"{module.__name__} reads recorded history for a live number"
        )


def test_the_worker_measures_after_the_inbox_and_not_instead_of_it(
    tmp_path: Path,
) -> None:
    """Inbox priority is unchanged, and this is where it could quietly stop being.

    Read from the source because the ordering *is* the guarantee: every piece
    of inbox work in a cycle has finished before a measurement is taken, so the
    rollup competes with nothing and delays nothing.
    """
    import inspect

    from librairy.worker import Worker

    source = inspect.getsource(Worker.run_once)
    assert source.index("analyze_items") < source.index("self._metrics_rollup()")
    assert source.index("self._inbox_companions") < source.index("self._metrics_rollup()")


def test_the_rollup_only_runs_when_it_is_due(tmp_path: Path) -> None:
    """Hourly, from the recorded moment rather than from a schedule — so a
    worker stopped for a week takes one snapshot when it returns rather than
    catching up on seven it cannot measure."""
    conn = connect(settings_for(tmp_path))

    assert metrics.due(conn), "a database that has never measured is due"
    metrics.rollup(conn)
    assert not metrics.due(conn)
    assert metrics.due(conn, seconds=0)


def _days_before(offset: int) -> str:
    from datetime import date, timedelta

    parts = [int(part) for part in metrics.today().split("-")]
    return (date(*parts) - timedelta(days=offset)).isoformat()


class _Counting:
    """A connection that counts the statements run through it."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.statements = 0

    def execute(self, sql, *args, **kwargs):  # noqa: ANN001, ANN201
        self.statements += 1
        return self._conn.execute(sql, *args, **kwargs)

    def __getattr__(self, name):  # noqa: ANN001, ANN204
        return getattr(self._conn, name)
