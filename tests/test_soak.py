"""What repeated, unchanged work must *not* leave behind.

Every other test in this suite asks whether an operation produces the right
answer. These ask what the hundredth identical repetition of it costs, which is
a different question and has a different failure mode: nothing is ever wrong,
and the installation slowly becomes unusable anyway.

Two shapes, and they are not interchangeable:

    convergence   repeating unchanged work writes nothing new. A workload that
                  adds one row per idle cycle is a soak failure even though
                  every individual cycle is correct
    boundedness   something that *must* grow has a stated ceiling and reaches
                  it. `backup_runs` keeps two hundred per destination; the test
                  is that the two hundred and first does not make it two hundred
                  and one

The measurements these were written from live in `scripts/soak.py`, which runs
the same workloads for far longer and reports slopes. This file holds the
conclusions at a size the ordinary suite can afford.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from librairy import backup_runs, destinations, transfer_run
from librairy.config import Settings
from librairy.db import connect
from librairy.scanner import scan_root
from librairy.transfer_plan import Scope
from librairy.worker import DB_CHECK_DUE_KEY, Worker, run_once

rclone_installed = pytest.mark.skipif(
    shutil.which("rclone") is None, reason="rclone is not installed"
)


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
    for path in (
        settings.appdata_dir, settings.inbox_dir,
        settings.library_dir, settings.quarantine_dir,
    ):
        path.mkdir(parents=True, exist_ok=True)
    return settings


def tables(conn) -> dict[str, int]:  # noqa: ANN001
    """Every table in the schema, counted. Not a hand-written list: one that
    misspelled a table name reported "nothing grew" while counting nothing."""
    names = [
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        if not str(row[0]).startswith("sqlite_")
    ]
    return {
        name: int(conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])  # noqa: S608
        for name in names
    }


def grew(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    return {
        name: after[name] - before.get(name, 0)
        for name in after
        if after[name] != before.get(name, 0)
    }


def open_files() -> int:
    for where in ("/proc/self/fd", "/dev/fd"):
        try:
            return len(os.listdir(where))
        except OSError:
            continue
    return 0


def library_with(settings: Settings, conn, count: int) -> None:  # noqa: ANN001
    for index in range(count):
        folder = settings.library_dir / f"Documents/{index % 4:02d}"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"filed-{index:03d}.txt").write_text(f"body {index}", encoding="utf-8")
    scan_root(conn, "library", settings.library_dir, settings)


# --- the worker ----------------------------------------------------------------------


def test_an_idle_worker_cycle_writes_nothing_durable_after_the_first(tmp_path) -> None:
    """The plainest soak question there is: does an installation with nothing to
    do change when nothing happens to it?

    The first cycles legitimately write — the day's metrics, the phase, the
    heartbeat, a scheduled verification — so the measurement starts *after*
    them. From there the answer has to be nothing at all, because there is
    nothing for it to be about.
    """
    settings = settings_for(tmp_path)
    conn = connect(settings)
    library_with(settings, conn, 12)

    for _ in range(12):
        run_once(conn, settings)
    before = tables(conn)
    for _ in range(30):
        run_once(conn, settings)

    assert grew(before, tables(conn)) == {}


def test_idle_worker_cycles_do_not_leak_a_file_descriptor(tmp_path) -> None:
    """`check_database` opened a connection through `with sqlite3.connect(...)`,
    which is a *transaction* manager and does not close anything. One database
    and one WAL were left open per idle cycle; a few hundred cycles would have
    reached the process limit and stopped the worker with "too many open files".

    Measured across cycles rather than asserted at one: the leak was invisible in
    any single cycle, which is exactly why it survived until a soak looked.
    """
    settings = settings_for(tmp_path)
    conn = connect(settings)
    library_with(settings, conn, 8)

    for _ in range(5):
        run_once(conn, settings)
    settled = open_files()
    for _ in range(25):
        run_once(conn, settings)

    assert open_files() <= settled


def test_the_whole_database_is_verified_daily_and_not_every_cycle(tmp_path) -> None:
    """`PRAGMA quick_check` reads every page — seconds, on a real library — and
    it ran on every idle cycle. Between two cycles thirty seconds apart there is
    nothing it can find that it did not find the first time.

    The gate is recorded rather than timed, so a worker restarted every hour
    does not get a free verification each time; this asserts the record, which
    is the thing that survives a restart.
    """
    settings = settings_for(tmp_path)
    conn = connect(settings)
    library_with(settings, conn, 6)
    worker = Worker(conn, settings)

    checks = []
    original = __import__("librairy.web.health", fromlist=["check_database"]).check_database

    def counted(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        checks.append(1)
        return original(*args, **kwargs)

    import librairy.web.health as health_module

    health_module.check_database = counted
    try:
        for _ in range(20):
            worker._database_check(settings)
    finally:
        health_module.check_database = original

    assert len(checks) == 1
    assert conn.execute(
        "SELECT 1 FROM worker_state WHERE key=?", (DB_CHECK_DUE_KEY,)
    ).fetchone() is not None


# --- pages ---------------------------------------------------------------------------


def test_polling_the_dashboard_changes_nothing(tmp_path) -> None:
    """The page a browser re-requests every five seconds, forever. One session is
    minted on the first request and nothing else may ever be written — a single
    row per poll is four hundred thousand rows in a month."""
    from fastapi.testclient import TestClient

    from librairy.web.app import create_app

    settings = settings_for(tmp_path)
    conn = connect(settings)
    library_with(settings, conn, 10)
    with TestClient(create_app(settings, conn)) as client:
        client.get("/dashboard")
        before = tables(conn)
        for _ in range(40):
            client.get("/dashboard")

        assert grew(before, tables(conn)) == {}


def test_reading_review_and_search_and_browse_changes_nothing(tmp_path) -> None:
    """Reading is reading. Every one of these pages has, at some point in this
    program's history, written something while drawing itself."""
    from fastapi.testclient import TestClient

    from librairy.web.app import create_app

    settings = settings_for(tmp_path)
    conn = connect(settings)
    library_with(settings, conn, 10)
    with TestClient(create_app(settings, conn)) as client:
        for path in ("/review", "/search?q=filed", "/browse", "/health"):
            client.get(path)
        before = tables(conn)
        for _ in range(10):
            for path in ("/review", "/search?q=filed", "/browse", "/health"):
                client.get(path)

        assert grew(before, tables(conn)) == {}


def test_rescanning_an_unchanged_library_finds_nothing_new(tmp_path) -> None:
    """The scanner had a fast path for unchanged files that was only fast about
    hashing: it still wrote the row, looked the id up and rewrote the search
    index entry — a `DELETE` and an `INSERT` into FTS5 per file per scan. The
    FTS segment tables grew on every pass over a library nobody had touched, and
    a rescan of 800 files went from 212 ms to 51 ms when it stopped.

    The index stays correct because every writer of the things it derives from
    calls `sync_search_item` for itself; the scanner's copy was re-asserting
    what was already true.
    """
    settings = settings_for(tmp_path)
    conn = connect(settings)
    library_with(settings, conn, 20)

    before = tables(conn)
    for _ in range(10):
        scan_root(conn, "library", settings.library_dir, settings)

    assert grew(before, tables(conn)) == {}


def test_a_changed_file_still_reaches_the_search_index(tmp_path) -> None:
    """The other half of the fast path, and the one that would make it a bug:
    skipping work for an unchanged file must not skip it for a changed one."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    library_with(settings, conn, 4)
    edited = settings.library_dir / "Documents/00/notes.txt"
    edited.write_text("first", encoding="utf-8")
    scan_root(conn, "library", settings.library_dir, settings)

    item = conn.execute(
        "SELECT id, fingerprint FROM items WHERE relpath=?", ("Documents/00/notes.txt",)
    ).fetchone()
    assert conn.execute(
        "SELECT COUNT(*) FROM search_fts WHERE rowid=?", (item["id"],)
    ).fetchone()[0] == 1

    #  Same path, different bytes and a different mtime: the scanner has to
    #  re-hash it rather than take the unchanged branch.
    edited.write_text("second and longer", encoding="utf-8")
    os.utime(edited, (0, 0))
    summary = scan_root(conn, "library", settings.library_dir, settings)

    after = conn.execute(
        "SELECT fingerprint FROM items WHERE relpath=?", ("Documents/00/notes.txt",)
    ).fetchone()
    assert summary.hashed == 1
    assert after["fingerprint"] != item["fingerprint"]
    assert conn.execute(
        "SELECT COUNT(*) FROM search_fts WHERE rowid=?", (item["id"],)
    ).fetchone()[0] == 1


# --- transfers -----------------------------------------------------------------------


@rclone_installed
def test_a_second_unchanged_backup_sends_nothing(tmp_path) -> None:
    """The first run copies; the second finds everything current and moves no
    bytes. Through real rclone, because "did it transfer" is a question about
    what the binary decided and a stub would be answering for it."""
    from librairy.worker import _listing_for

    settings = settings_for(tmp_path)
    conn = connect(settings)
    library_with(settings, conn, 6)
    target = tmp_path / "backup"
    target.mkdir()
    destination_id = destinations.add_destination(
        conn, name="Soak", kind="local", target=str(target), modes=["backup"]
    )
    destinations.set_policy(
        conn, category="documents", destination_id=destination_id, mode="backup"
    )
    destination = destinations.destination(conn, destination_id)
    scope = Scope.folder("Documents", "backup")

    def once():  # noqa: ANN202
        listing = _listing_for(conn, settings, destination, scope)
        return transfer_run.run_scope(conn, settings, scope, destination, listing)[1]

    first = once()
    second = once()

    assert first.outcome == "ok"
    assert first.files == 6
    assert second.outcome == "ok"
    assert second.files == 0


@rclone_installed
def test_repeating_a_mirror_comparison_does_not_grow_the_divergence_set(tmp_path) -> None:
    """Comparing the same two sides ten times is one answer, not ten."""
    from librairy.worker import _listing_for

    settings = settings_for(tmp_path)
    conn = connect(settings)
    library_with(settings, conn, 6)
    target = tmp_path / "mirror"
    target.mkdir()
    (target / "Documents").mkdir()
    (target / "Documents/only-here.txt").write_text("extra", encoding="utf-8")
    destination_id = destinations.add_destination(
        conn, name="Soak mirror", kind="local", target=str(target), modes=["mirror"]
    )
    destination = destinations.destination(conn, destination_id)
    scope = Scope.folder("Documents", "mirror")

    def once() -> None:
        listing = _listing_for(conn, settings, destination, scope)
        transfer_run.run_scope(conn, settings, scope, destination, listing)

    once()
    before = int(conn.execute("SELECT COUNT(*) FROM backup_divergence").fetchone()[0])
    for _ in range(8):
        once()
    after = int(conn.execute("SELECT COUNT(*) FROM backup_divergence").fetchone()[0])

    assert before == after


def test_the_run_history_stops_at_its_stated_retention(tmp_path) -> None:
    """`backup_runs` is allowed to grow — it is a history — so the soak question
    is whether it stops. Written straight against the table rather than through
    rclone: the bound is the point, not the transfer."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    destination_id = destinations.add_destination(
        conn, name="Soak", kind="local", target=str(tmp_path / "b"), modes=["backup"]
    )

    for _ in range(backup_runs.KEEP_RUNS + 25):
        run_id = backup_runs.begin(
            conn,
            destination_id=destination_id,
            category="documents",
            mode="backup",
            origin="policy",
        )
        backup_runs.finish(conn, run_id, succeeded=True, transferred=0, outcome="ok")

    held = int(conn.execute("SELECT COUNT(*) FROM backup_runs").fetchone()[0])
    assert held == backup_runs.KEEP_RUNS
